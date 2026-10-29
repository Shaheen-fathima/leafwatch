"""LeafWatch backend (FastAPI).  Run:  python -m uvicorn app:app --host 127.0.0.1 --port 8000"""
import asyncio, io, json, os, sqlite3, statistics, threading, time, uuid, hmac
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
import numpy as np
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image, UnidentifiedImageError

import db, engine as E
from quality import image_stats

BASE = Path(__file__).parent
UPLOADS = Path(os.getenv("LEAFWATCH_UPLOADS") or BASE / "uploads"); UPLOADS.mkdir(parents=True, exist_ok=True)
# No insecure built-in admin password. Set LEAFWATCH_ADMIN_KEY in the environment.
ADMIN_KEY = os.getenv("LEAFWATCH_ADMIN_KEY", "").strip()
# Safe default: no public alert until the single admin approves the outbreak.
REQUIRE_APPROVAL = os.getenv("LEAFWATCH_REQUIRE_ADMIN_APPROVAL", "1").strip() != "0"
MAX_BYTES = 10 * 1024 * 1024
LOCK = threading.Lock()


@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(reminder_loop())
    try: yield
    finally: task.cancel()

app = FastAPI(title="LeafWatch", lifespan=lifespan)
db.init()

PRED, CLS, CFG, METRICS, CALIB = None, None, dict(E.CFG), {}, {}
if os.getenv("LEAFWATCH_NO_MODEL") != "1":
    try:
        from model_utils import Predictor
        PRED = Predictor(); CLS = E.Classes(PRED.classes); CALIB = dict(PRED.calib)
        CFG.update({k: PRED.calib[k] for k in ("emb_lo", "emb_hi") if k in PRED.calib})
        mp = BASE / "artifacts" / "metrics.json"
        METRICS = json.loads(mp.read_text()) if mp.exists() else {}
    except Exception as e:
        print("!! model not loaded:", e, "-> run train.py first")


@app.exception_handler(sqlite3.Error)
async def database_error(request, exc):
    print(f"!! LeafWatch database error on {request.method} {request.url.path}: {exc!r}")      # shows the real cause in the terminal
    # The whole report/review is one transaction, so on any database error NOTHING was saved. Say exactly that.
    return JSONResponse(status_code=503, content={"detail": f"Database error ({exc}): nothing was saved and nobody was alerted. Please try again."})


def due_reminders(c, now):
    """Participation: ONE reminder per verification request still unanswered after CFG['remind_after_s'] (and not expired at 24 h).
    Marks the request as reminded and returns websocket payloads. Pure database logic, so it is testable."""
    out = []
    for q in c.execute("SELECT * FROM requests WHERE status='pending' AND reminded_ts IS NULL AND ts<=? AND ts>?", (now - CFG["remind_after_s"], now - 86400)).fetchall():
        c.execute("UPDATE requests SET reminded_ts=? WHERE id=?", (now, q["id"]))
        out.append((q["farmer_id"], dict(type="reminder", request_id=q["id"], crop=q["crop"], disease=q["disease"])))
    return out


async def reminder_loop():
    while True:
        try:
            await asyncio.sleep(300)
            with LOCK, db.tx() as c: notes = due_reminders(c, time.time())
            for fid, payload in notes: await hub.send(fid, payload)
        except asyncio.CancelledError:
            raise
        except Exception as e:                       # a failed reminder run must never take the server down
            print("!! reminder loop:", repr(e))


# ------------------------------------------------------------ websocket hub
class Hub:
    def __init__(self): self.conns = {}
    async def add(self, fid, ws): await ws.accept(); self.conns.setdefault(fid, []).append(ws)
    def drop(self, fid, ws):
        if ws in self.conns.get(fid, []): self.conns[fid].remove(ws)
    async def send(self, fid, payload):
        for ws in list(self.conns.get(fid, [])):
            try: await ws.send_json(payload)
            except Exception: self.drop(fid, ws)
hub = Hub()


@app.websocket("/ws/{fid}")
async def ws_endpoint(ws: WebSocket, fid: int):
    await hub.add(fid, ws)
    try:
        while True: await ws.receive_text()           # keep-alive pings from the browser
    except WebSocketDisconnect:
        hub.drop(fid, ws)


# ------------------------------------------------------------ models
class FarmerIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)

class LocIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)

class VerifyIn(BaseModel):
    confirmed: bool = True
    action: Optional[str] = None
    note: Optional[str] = None

class CandidateDecisionIn(BaseModel):
    action: str = Field(min_length=1, max_length=30)
    note: Optional[str] = None

class AdminAuthIn(BaseModel):
    key: str = Field(min_length=1, max_length=256)

class ReadIn(BaseModel):
    ids: Optional[list[int]] = None


def badge(p):
    return "Guardian" if p >= 400 else "Scout" if p >= 150 else "Sprout" if p >= 50 else "Seedling"

def farmer_dict(r):
    d = dict(r); d["reliability"] = round(d["a"] / (d["a"] + d["b"]), 3); d["badge"] = badge(d["points"]); return d

FAILS = []                                   # timestamps of recent failed admin attempts (in memory; per process)
MAX_FAILS, FAIL_WINDOW_S = 8, 300

def need_admin(key):
    """Admin check: configured key required, constant-time compare (bytes, so odd characters cannot crash it), and a lock-out after
    MAX_FAILS wrong keys in FAIL_WINDOW_S seconds (anti brute force). Needs HTTPS in real hosting: the key travels in a header."""
    if not ADMIN_KEY:
        raise HTTPException(503, "Admin login is not configured. Set LEAFWATCH_ADMIN_KEY and restart LeafWatch.")
    now = time.time(); FAILS[:] = [t for t in FAILS if now - t < FAIL_WINDOW_S]
    if len(FAILS) >= MAX_FAILS:
        raise HTTPException(429, "Too many wrong admin keys. Wait a few minutes.")
    if not key or not hmac.compare_digest(key.encode("utf-8"), ADMIN_KEY.encode("utf-8")):
        FAILS.append(now); raise HTTPException(401, "bad admin key")

@app.post("/api/admin/auth")
def admin_auth(body: AdminAuthIn):
    """Validate admin credentials without exposing the configured secret."""
    need_admin(body.key)
    return {"ok": True, "role": "admin", "approval_required": REQUIRE_APPROVAL}


# ------------------------------------------------------------ farmers
@app.post("/api/farmers")
def add_farmer(f: FarmerIn):
    with db.tx() as c:
        cur = c.execute("INSERT INTO farmers(name,lat,lon,created) VALUES(?,?,?,?)", (f.name.strip(), f.lat, f.lon, time.time()))
        return farmer_dict(c.execute("SELECT * FROM farmers WHERE id=?", (cur.lastrowid,)).fetchone())

@app.get("/api/farmers/{fid}")
def get_farmer(fid: int):
    with db.tx() as c:
        r = c.execute("SELECT * FROM farmers WHERE id=?", (fid,)).fetchone()
    if not r: raise HTTPException(404, "farmer not found")
    return farmer_dict(r)

@app.put("/api/farmers/{fid}/location")
def set_loc(fid: int, loc: LocIn):
    with db.tx() as c:
        if not c.execute("UPDATE farmers SET lat=?,lon=? WHERE id=?", (loc.lat, loc.lon, fid)).rowcount: raise HTTPException(404, "farmer not found")
    return {"ok": True}

@app.get("/api/farmers/{fid}/alerts")
def alerts(fid: int):
    """Persistent alerts of ONE farmer (read from the alerts table, newest first)."""
    with db.tx() as c:
        rows = c.execute("SELECT a.*, o.crop, o.disease, o.status, o.P FROM alerts a JOIN outbreaks o ON o.id=a.outbreak_id WHERE a.farmer_id=? AND o.status!='pending' ORDER BY a.ts DESC, a.id DESC LIMIT 50", (fid,)).fetchall()
    return [dict(r) for r in rows]

@app.post("/api/farmers/{fid}/alerts/read")
def mark_alerts_read(fid: int, body: Optional[ReadIn] = None):
    with db.tx() as c:
        if not c.execute("SELECT 1 FROM farmers WHERE id=?", (fid,)).fetchone(): raise HTTPException(404, "farmer not found")
        if body and body.ids:
            n = c.execute(f"UPDATE alerts SET read=1 WHERE farmer_id=? AND read=0 AND id IN ({','.join('?' * len(body.ids))})", (fid, *body.ids)).rowcount
        else:
            n = c.execute("UPDATE alerts SET read=1 WHERE farmer_id=? AND read=0", (fid,)).rowcount
    return {"marked": n}

@app.get("/api/farmers/{fid}/requests")
def my_requests(fid: int):
    with db.tx() as c:
        rows = c.execute("SELECT * FROM requests WHERE farmer_id=? AND status='pending' AND ts>? ORDER BY ts DESC", (fid, time.time() - 86400)).fetchall()
    now = time.time()
    return [dict(r, age_h=round((now - r["ts"]) / 3600, 1), reminder=(now - r["ts"]) >= CFG["remind_after_s"]) for r in rows]

@app.get("/api/farmers/{fid}/reports")
def my_reports(fid: int):
    with db.tx() as c:
        rows = c.execute("SELECT id,ts,crop,top_class,top_p,u,w,action,post_p,image,repeat_kind,pts FROM reports WHERE farmer_id=? ORDER BY ts DESC LIMIT 30", (fid,)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------ alert + verification delivery (all inside the caller's transaction)
def pack_msgs(o, dist_km):
    en = f"Outbreak confirmed: {o['disease']} on {o['crop']} about {dist_km:.1f} km from you. Inspect your crop today and avoid moving plant material."
    ta = f"உங்கள் இடத்திலிருந்து சுமார் {dist_km:.1f} கி.மீ தொலைவில் {o['crop']} பயிரில் {o['disease']} நோய்ப் பரவல் உறுதிசெய்யப்பட்டது. இன்றே உங்கள் பயிரைச் சோதிக்கவும்."
    return en, ta


def deliver_alerts(c, o, now):
    """Create the MISSING alert rows for an outbreak: one per farmer inside the alert radius of the outbreak centre, never two for
    the same farmer and outbreak. Idempotent, so it is safe to call again when the outbreak grows or is confirmed.
    Returns (rows created, websocket notifications)."""
    have = {r[0] for r in c.execute("SELECT farmer_id FROM alerts WHERE outbreak_id=?", (o["id"],))}
    notes, n = [], 0
    for fr in c.execute("SELECT * FROM farmers").fetchall():
        if fr["id"] in have: continue
        if fr["lat"] is None or fr["lon"] is None: continue
        d = E.haversine_m(o["lat"], o["lon"], fr["lat"], fr["lon"])
        if d > CFG["alert_radius_m"]: continue
        en, ta = pack_msgs(o, d / 1000)
        c.execute("INSERT INTO alerts(farmer_id,outbreak_id,msg_en,msg_ta,dist_km,ts) VALUES(?,?,?,?,?,?)", (fr["id"], o["id"], en, ta, d / 1000, now))
        notes.append((fr["id"], dict(type="alert", outbreak_id=o["id"], crop=o["crop"], disease=o["disease"], msg_en=en, msg_ta=ta,
                                     dist_km=round(d / 1000, 2), lat=o["lat"], lon=o["lon"])))
        n += 1
    c.execute("UPDATE outbreaks SET alerted=(SELECT COUNT(*) FROM alerts WHERE outbreak_id=?) WHERE id=?", (o["id"], o["id"]))
    return n, notes


def create_verify_requests(c, farmer_id, lat, lon, cand, report_id, hist, now):
    """Ask nearby farmers for a confirming photo. Returns (status, notes, rows created, farmers in range, radius used in metres):
       requested / already_pending (an open request for this crop+disease already covers this area) / no_eligible_farmers.
    The nearest farmers within verify_radius_m are asked first; if nobody lives that close the nearest farmers inside the wider
    verify_fallback_radius_m are asked (a neighbour 3 km away is still an independent witness)."""
    for p in c.execute("SELECT lat,lon FROM requests WHERE crop=? AND disease=? AND status='pending' AND ts>?", (cand["crop"], cand["disease"], now - CFG["verify_cooldown_s"])):
        if p["lat"] is not None and p["lon"] is not None and E.haversine_m(lat, lon, p["lat"], p["lon"]) <= CFG["fuse_radius_m"]:
            return "already_pending", [], 0, 0, CFG["verify_radius_m"]
    busy = {h["farmer_id"] for h in hist if now - h["ts"] < 86400}
    ranked = sorted(((E.haversine_m(lat, lon, fr["lat"], fr["lon"]), fr) for fr in c.execute("SELECT * FROM farmers WHERE id!=? AND lat IS NOT NULL AND lon IS NOT NULL", (farmer_id,))), key=lambda x: x[0])
    radius = CFG["verify_radius_m"]
    near = [x for x in ranked if x[0] <= radius]
    pick = [x for x in near if x[1]["id"] not in busy][:5]
    if not pick and CFG.get("verify_fallback_radius_m", 0) > radius:
        radius = CFG["verify_fallback_radius_m"]
        near = [x for x in ranked if x[0] <= radius]
        pick = [x for x in near if x[1]["id"] not in busy][:5]
    if not pick:
        return "no_eligible_farmers", [], 0, len(near), radius
    notes = []
    for _, fr in pick:
        qid = c.execute("INSERT INTO requests(farmer_id,crop,disease,lat,lon,ts,report_id) VALUES(?,?,?,?,?,?,?)", (fr["id"], cand["crop"], cand["disease"], lat, lon, now, report_id)).lastrowid
        notes.append((fr["id"], dict(type="verify", request_id=qid, crop=cand["crop"], disease=cand["disease"])))
    return "requested", notes, len(pick), len(near), radius


# ------------------------------------------------------------ core: report ingestion
def ingest(farmer_id, lat, lon, pred, image_name, request_id):
    """One report = ONE database transaction (report row, points, request fulfilment, outbreak, alerts, verification requests).
    If anything fails, everything rolls back and the caller gets an error instead of a false 'alerted' message."""
    notes = []                                         # (farmer_id, payload) to push over websocket AFTER the commit
    now = time.time()
    with LOCK, db.tx() as c:
        f = c.execute("SELECT * FROM farmers WHERE id=?", (farmer_id,)).fetchone()
        if not f: raise HTTPException(404, "farmer not found")
        hist = db.recent_reports(c, lat, lon, now - CFG["fuse_window_s"])
        hist += db.exact_duplicates(c, pred["dhash"], now - 2 * CFG["dep_time_s"], CFG["dhash_exact"], {h["id"] for h in hist})   # same photo anywhere
        hist = [h for h in hist if len(h["probs"]) == len(CLS.names)]       # rows written by a model with a different class list cannot be fused
        ids = {h["farmer_id"] for h in hist} | {farmer_id}
        relmap = {r["id"]: r["a"] / (r["a"] + r["b"]) for r in c.execute(f"SELECT id,a,b FROM farmers WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))}
        new = dict(farmer_id=farmer_id, lat=lat, lon=lon, ts=now, probs=pred["probs"], emb=pred["emb"], dhash=pred["dhash"], u=pred["u"])
        res = E.evaluate(new, hist, CLS, lambda i: relmap.get(i, 0.6), now, CFG)
        top = int(np.argmax(pred["probs"]))
        by_id = {h["id"]: h for h in hist}
        link = by_id.get(res["dep_parent"]) or by_id.get(res["field_parent"])         # earlier report this one repeats (same cluster)
        cluster_id = (link["cluster_id"] or link["id"]) if link else None
        fu, cand, action = res["fusion"], res["candidate"], res["action"]
        # points: only a genuinely NEW photo earns them. Exact copies, near copies and extra photos of the same plot earn 0.
        fresh = res["novelty"] >= 0.5 and res["repeat_kind"] == "new"
        pts = CFG["pts_new"] if fresh else 0
        # Verification requests this report answers. Rules: the request must belong to THIS farmer and still be pending, the photo must
        # show the requested CROP, be new evidence (not a repeat) and conclusive. Healthy = valid answer ("contradicts"). It does not
        # matter whether the farmer used the "Photograph now" button (request_id) or the normal Report tab.
        verdict = E.summarise_prediction(pred["probs"], float(pred["u"]), CLS, CFG)
        answers, why_not = [], None
        pending = [dict(q) for q in c.execute("SELECT * FROM requests WHERE farmer_id=? AND status='pending' AND ts>? ORDER BY ts", (farmer_id, now - 86400))]
        if not fresh:
            why_not = "not_new_evidence"
        elif verdict not in ("healthy", "disease_candidate"):
            why_not = "inconclusive_photo"
        else:
            for q in pending:
                if q["crop"].casefold() != CLS.crop[top].casefold(): continue
                answers.append((q["id"], "contradicts" if CLS.healthy[top] else "confirms" if CLS.dis[top].casefold() == q["disease"].casefold() else "other_disease"))
            if not answers: why_not = "wrong_crop" if pending else "nothing_pending"
        if request_id and not any(q == request_id for q, _ in answers) and why_not is None: why_not = "not_your_open_request"
        rid = db.insert_report(c, dict(new, crop=CLS.crop[top], top_class=CLS.names[top], top_p=float(pred["probs"][top]), w=res["novelty"],
              dep_score=res["dep_score"], cluster_id=cluster_id, image=image_name, action=action, post_p=fu["P"] if fu else None,
              n_eff=fu["n_eff"] if fu else None, n_src=fu["n_src"] if fu else None, conflict=fu["conflict"] if fu else None, costs=res["costs"],
              repeat_kind=res["repeat_kind"], note=res["override"], pts=pts))
        if cluster_id is None:
            cluster_id = rid; c.execute("UPDATE reports SET cluster_id=? WHERE id=?", (rid, rid))
        done = [(qid, kind) for qid, kind in answers
                if c.execute("UPDATE requests SET status='done', answered_report_id=?, answer=?, answered_ts=? WHERE id=? AND farmer_id=? AND status='pending'", (rid, kind, now, qid, farmer_id)).rowcount]
        request_done = bool(done)
        if request_done: pts += CFG["pts_verify"]                      # once per report, however many requests it closed
        c.execute("UPDATE reports SET pts=? WHERE id=?", (pts, rid))
        c.execute("UPDATE farmers SET points=points+? WHERE id=?", (pts, farmer_id))

        info = dict(candidate=None, P=None, n_eff=None, independent_farmers=None, conflict=None, action=action, cost_action=res["cost_action"],
                    override=res["override"], costs=res["costs"], sent_to=0, alerts_created=0, requests_created=0, alert_status="none",
                    verify_status=None, outbreak_id=None, outbreak_status=None, context=res["context"])
        if cand:
            info.update(candidate=f"{cand['crop']} - {cand['disease']}", P=fu["P"], n_eff=fu["n_eff"], independent_farmers=fu["n_src"], conflict=fu["conflict"])
            cen = fu["centroid"]
            support = [rid if x is None else x for x in fu["support"]]
            if action == "alert":
                act = [dict(r) for r in c.execute("SELECT * FROM outbreaks WHERE crop=? AND disease=? AND (status='pending' OR ts>?)", (cand["crop"], cand["disease"], now - CFG["cooldown_s"]))]
                ex = E.find_active(act, cand["crop"], cand["disease"], cen, now, CFG)
                if ex and ex["status"] == "rejected":
                    # an admin already called this a false alarm inside the cooldown window: stay quiet and say so
                    info.update(alert_status="suppressed_rejected", outbreak_id=ex["id"], outbreak_status="rejected")
                elif ex:
                    c.execute("UPDATE outbreaks SET P=?, n_eff=?, evidence=? WHERE id=?", (max(ex["P"], fu["P"]), fu["n_eff"], json.dumps(support), ex["id"]))
                    info.update(outbreak_id=ex["id"], outbreak_status=ex["status"], alert_status="pending_admin")
                    if ex["status"] == "confirmed" or (ex["status"] == "active" and not REQUIRE_APPROVAL):
                        n, nn = deliver_alerts(c, ex, now); notes += nn          # only farmers who do not have this alert yet
                        info.update(alert_status="extended", alerts_created=n, sent_to=n)
                    elif REQUIRE_APPROVAL:
                        info.update(alert_status="pending_admin", outbreak_status=ex["status"])
                else:
                    status = "pending" if REQUIRE_APPROVAL else "active"
                    oid = c.execute("INSERT INTO outbreaks(crop,disease,lat,lon,ts,P,n_eff,evidence,status) VALUES(?,?,?,?,?,?,?,?,?)",
                                    (cand["crop"], cand["disease"], cen[0], cen[1], now, fu["P"], fu["n_eff"], json.dumps(support), status)).lastrowid
                    info.update(outbreak_id=oid, outbreak_status=status, alert_status="pending_admin")
                    if status == "active":
                        n, nn = deliver_alerts(c, dict(id=oid, crop=cand["crop"], disease=cand["disease"], lat=cen[0], lon=cen[1]), now); notes += nn
                        info.update(alert_status="sent", alerts_created=n, sent_to=n)
            elif action == "verify":
                st, nn, n, in_range, rad = create_verify_requests(c, farmer_id, lat, lon, cand, rid, hist, now)
                notes += nn
                info.update(verify_status=st, requests_created=n, sent_to=n, farmers_in_range=in_range, verify_radius_km=round(rad / 1000, 1))
                c.execute("UPDATE reports SET verify_status=? WHERE id=?", (st, rid))
        fr = c.execute("SELECT points FROM farmers WHERE id=?", (farmer_id,)).fetchone()
        out = dict(report_id=rid, novelty=round(res["novelty"], 3), dep_score=round(res["dep_score"], 3), duplicate_of=res["dep_parent"], repeat_kind=res["repeat_kind"],
                   cluster_id=cluster_id, points_earned=pts, points=fr["points"], badge=badge(fr["points"]), request_fulfilled=request_done,
                   requests_answered=[dict(request_id=q, answer=k) for q, k in done], request_answer=(done[0][1] if done else None),
                   request_not_answered_reason=(why_not if (request_id and not request_done) else None), outbreak=info)

    # ---- read back what is REALLY in the database (the transaction is committed now); the response reports these numbers
    with db.tx() as c:
        o = out["outbreak"]
        if o["outbreak_id"] and o["alerts_created"]:
            o["alerts_created"] = o["sent_to"] = c.execute("SELECT COUNT(*) FROM alerts WHERE outbreak_id=? AND ts=?", (o["outbreak_id"], now)).fetchone()[0]
        if o["requests_created"]:
            o["requests_created"] = o["sent_to"] = c.execute("SELECT COUNT(*) FROM requests WHERE report_id=?", (rid,)).fetchone()[0]
    return out, notes


def retake_response(q, pred, imgq, name):
    return dict(retake_required=True, retake=True, status="rejected", reasons=q["reasons"], reason_messages=q["messages"],
                blurry="blurry" in q["reasons"], uncertainty=round(float(pred["u"]), 3), crop_confidence=round(float(q["crop_conf"]), 3),
                crop_margin=round(float(q["crop_margin"]), 3), sharpness=round(imgq["blur"], 1), brightness=round(imgq["brightness"], 1),
                top=[], image=name, evidence_added=False, points_earned=0,
                message="Photo needs to be retaken. Retake it in daylight with one leaf in focus. It was not used as evidence, no points were given and nobody was alerted.")


@app.post("/api/report")
async def report(farmer_id: int = Form(...), lat: float = Form(...), lon: float = Form(...), request_id: Optional[int] = Form(None), image: UploadFile = File(...)):
    if PRED is None: raise HTTPException(503, "Model not loaded. Run train.py first (see README).")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180): raise HTTPException(422, "invalid coordinates")
    with db.tx() as c:
        if not c.execute("SELECT 1 FROM farmers WHERE id=?", (farmer_id,)).fetchone(): raise HTTPException(404, "farmer not found")
    raw = await image.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES: raise HTTPException(413, "image larger than 10 MB")
    try:
        img = Image.open(io.BytesIO(raw)); img.load(); img = img.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError):
        raise HTTPException(415, "file is not a valid image")
    if min(img.size) < 64: raise HTTPException(422, "image too small (min 64 px)")
    img.thumbnail((1600, 1600))
    name = f"{uuid.uuid4().hex}.jpg"; img.save(UPLOADS / name, quality=88)
    pred = await run_in_threadpool(PRED.predict, img)
    order = np.argsort(-pred["probs"])[:3]

    # ---- quality gate: pixels (sharpness / exposure / contrast) AND model (crop mass, margin, uncertainty).
    # A rejected photo creates NO database row at all: no evidence, no points, no request, no outbreak, no alert.
    imgq = image_stats(img)
    q = E.assess_quality(pred["probs"], float(pred["u"]), imgq, CLS, CFG, CALIB.get("blur_min"))
    if q["status"] != "ok":
        try: (UPLOADS / name).unlink()                 # a rejected photo is not kept anywhere: not in the database, not on disk
        except OSError: pass
        return retake_response(q, pred, imgq, name)

    out, notes = await run_in_threadpool(ingest, farmer_id, lat, lon, pred, name, request_id)
    for fid, payload in notes: await hub.send(fid, payload)
    verdict = E.summarise_prediction(pred["probs"], float(pred["u"]), CLS, CFG)
    out["top"] = [dict(name=CLS.names[i], crop=CLS.crop[i], disease=CLS.dis[i], p=round(float(pred["probs"][i]), 4)) for i in order]
    out.update(uncertainty=round(float(pred["u"]), 3), blurry=bool(q["soft_focus"]), retake=verdict == "inconclusive", assessment=verdict, image=name,
               evidence_added=out["novelty"] > 0, retake_required=False, status="accepted",
               quality=dict(status="ok", soft_focus=q["soft_focus"], sharpness=round(imgq["blur"], 1), brightness=round(imgq["brightness"], 1),
                            contrast=round(imgq["contrast"], 1), crop_confidence=round(q["crop_conf"], 3), crop_margin=round(q["crop_margin"], 3)))
    return out


# ------------------------------------------------------------ public views
@app.get("/api/reports/recent")
def recent():
    with db.tx() as c:
        rows = c.execute("SELECT id,lat,lon,ts,crop,top_class,top_p,u,w FROM reports WHERE ts>? ORDER BY ts DESC LIMIT 400", (time.time() - 5 * 86400,)).fetchall()
    return [dict(r, lat=round(r["lat"], 3), lon=round(r["lon"], 3)) for r in rows]   # ~110 m privacy rounding

@app.get("/api/outbreaks")
def outbreaks():
    """Outbreaks farmers may see on the map: announced (active) or confirmed. Pending-approval and rejected ones are not public."""
    with db.tx() as c:
        rows = c.execute("SELECT * FROM outbreaks WHERE status IN ('active','confirmed') AND ts>? ORDER BY ts DESC LIMIT 100", (time.time() - 7 * 86400,)).fetchall()
    return [dict(r, evidence=json.loads(r["evidence"] or "[]")) for r in rows]

@app.get("/api/stats")
def stats():
    now = time.time()
    with db.tx() as c:
        q = lambda s, *a: c.execute(s, a).fetchone()[0]
        vw = verify_watchlist(c, now)
        return dict(farmers=q("SELECT COUNT(*) FROM farmers"), reports=q("SELECT COUNT(*) FROM reports"),
                    clusters=q("SELECT COUNT(DISTINCT COALESCE(cluster_id,id)) FROM reports"),
                    duplicates_merged=q("SELECT COUNT(*) FROM reports WHERE cluster_id IS NOT NULL AND cluster_id!=id"),
                    down_weighted=q("SELECT COUNT(*) FROM reports WHERE w<0.5"), outbreaks=q("SELECT COUNT(*) FROM outbreaks"),
                    open_outbreaks=q("SELECT COUNT(*) FROM outbreaks WHERE status IN ('active','pending')"),
                    pending_approval=q("SELECT COUNT(*) FROM outbreaks WHERE status='pending'"),
                    confirmed=q("SELECT COUNT(*) FROM outbreaks WHERE status='confirmed'"),
                    rejected=q("SELECT COUNT(*) FROM outbreaks WHERE status='rejected'"),
                    deferred=q("SELECT COUNT(*) FROM outbreaks WHERE status='deferred'"),
                    alerts_sent=q("SELECT COUNT(*) FROM alerts"), farmers_alerted=q("SELECT COUNT(DISTINCT farmer_id) FROM alerts"),
                    alerts_unread=q("SELECT COUNT(*) FROM alerts WHERE read=0"),
                    verify_requests=q("SELECT COUNT(*) FROM requests"), verify_answered=q("SELECT COUNT(*) FROM requests WHERE status='done'"),
                    verify_expired=q("SELECT COUNT(*) FROM requests WHERE status='pending' AND ts<=?", now - 86400),
                    verify_response_rate=(lambda sent, done: round(done / sent, 3) if sent else None)(q("SELECT COUNT(*) FROM requests"), q("SELECT COUNT(*) FROM requests WHERE status='done'")),
                    verify_median_answer_min=(lambda xs: round(statistics.median(xs) / 60, 1) if xs else None)([r[0] for r in c.execute("SELECT answered_ts-ts FROM requests WHERE status='done' AND answered_ts IS NOT NULL")]),
                    verify_pending=q("SELECT COUNT(*) FROM requests WHERE status='pending' AND ts>?", now - 86400),
                    verify_candidates=len(vw), approval_required=REQUIRE_APPROVAL, model=METRICS, model_loaded=PRED is not None)


# ------------------------------------------------------------ admin
def live_view(c, crop, disease, center, now):
    """Evidence picture RIGHT NOW for a crop+disease candidate (re-fuses everything stored nearby), so the dashboard reflects reports that
    arrived after the one that raised the question (e.g. a neighbour's healthy photo)."""
    if CLS is None: return None
    tgt = next((i for i in range(len(CLS.names)) if CLS.crop[i] == crop and CLS.dis[i] == disease), None)
    if tgt is None: return None
    rows = [r for r in db.recent_reports(c, center[0], center[1], now - CFG["fuse_window_s"]) if len(r["probs"]) == len(CLS.names)]
    ids = {r["farmer_id"] for r in rows}
    rel = {r["id"]: r["a"] / (r["a"] + r["b"]) for r in c.execute(f"SELECT id,a,b FROM farmers WHERE id IN ({','.join('?' * len(ids) or '0')})", tuple(ids))}
    f = E.fuse(CLS, tgt, now, center, rows, lambda i: rel.get(i, 0.6), CFG)
    action, cost_action, costs, note = E.decide_full(f, CFG)
    return dict(P=f["P"], n_eff=f["n_eff"], independent_farmers=f["n_src"], conflict=f["conflict"], action=action, cost_action=cost_action, costs=costs, note=note)


def verify_watchlist(c, now):
    """Candidates for which the engine chose VERIFY in the last 24 h and that have not become an outbreak since.
    Built only from rows that exist in the database, so it can never disagree with 'verification asked' shown to the farmer."""
    rows = c.execute("SELECT r.*, f.name AS fname FROM reports r JOIN farmers f ON f.id=r.farmer_id WHERE r.action='verify' AND (r.verify_status IS NULL OR r.verify_status NOT IN ('rejected','deferred','confirmed')) AND r.ts>? ORDER BY r.ts", (now - 86400,)).fetchall()
    groups = {}
    for r in rows:
        crop, dis = E.split_class(r["top_class"])
        g = groups.setdefault((crop, dis), dict(crop=crop, disease=dis, reports=[], report_ids=[]))
        g["reports"].append(r); g["report_ids"].append(r["id"])
    out = []
    for (crop, dis), g in groups.items():
        last, first = g["reports"][-1], g["reports"][0]
        if c.execute("SELECT 1 FROM outbreaks WHERE crop=? AND disease=? AND ts>=?", (crop, dis, first["ts"])).fetchone():
            continue                                                       # already escalated to an outbreak
        q = ",".join("?" * len(g["report_ids"]))
        rc = {r["status"]: r["n"] for r in c.execute(f"SELECT status, COUNT(*) AS n FROM requests WHERE report_id IN ({q}) GROUP BY status", g["report_ids"])}
        ans = {(r["answer"] or "unknown"): r["n"] for r in c.execute(f"SELECT answer, COUNT(*) AS n FROM requests WHERE report_id IN ({q}) AND status='done' GROUP BY answer", g["report_ids"])}
        expired = c.execute(f"SELECT COUNT(*) FROM requests WHERE report_id IN ({q}) AND status='pending' AND ts<=?", (*g["report_ids"], now - 86400)).fetchone()[0]
        # what OTHER farmers reported after the question was raised, whether or not they were asked (a neighbour may simply use the Report tab)
        reporters = {r["farmer_id"] for r in g["reports"]}
        seen = dict(confirming=0, healthy=0, other=0); who = set()
        for r in c.execute("SELECT farmer_id,lat,lon,top_class FROM reports WHERE ts>=? AND w>0 AND farmer_id NOT IN (%s)" % ",".join("?" * len(reporters)), (first["ts"], *reporters)):
            rc_, rd_ = E.split_class(r["top_class"])
            if rc_ != crop or E.haversine_m(last["lat"], last["lon"], r["lat"], r["lon"]) > CFG["fuse_radius_m"]: continue
            seen["healthy" if rd_.lower() == "healthy" else "confirming" if rd_ == dis else "other"] += 1; who.add(r["farmer_id"])
        
        lv = live_view(c, crop, dis, (last["lat"], last["lon"]), now)
        sent_cnt = sum(rc.values())
        done_cnt = rc.get("done", 0)
        pending_cnt = rc.get("pending", 0) - expired
        
        if sent_cnt == 0:
            comp_st = "no_eligible_farmers"
        elif pending_cnt > 0:
            comp_st = "requested"
        elif done_cnt > 0:
            if ans.get("contradicts", 0) > 0 or seen.get("healthy", 0) > 0 or (lv and lv.get("conflict", 0) >= 0.35):
                comp_st = "conflict_detected"
            elif ans.get("confirms", 0) > 0 or seen.get("confirming", 0) > 0:
                comp_st = "confirmed_by_neighbour"
            else:
                comp_st = "completed"
        elif expired > 0 and done_cnt == 0:
            comp_st = "expired"
        else:
            comp_st = last["verify_status"] or "completed"
            
        can_appr = bool(lv and lv.get("independent_farmers", 0) >= int(CFG.get("min_sources", 2)))
        
        out.append(dict(id=last["id"], report_ids=g["report_ids"], crop=crop, disease=dis, first_ts=first["ts"], last_ts=last["ts"],
                        n_reports=len(g["reports"]), reporters=sorted({r["fname"] for r in g["reports"]}),
                        lat=last["lat"], lon=last["lon"], verify_status=comp_st, raw_verify_status=last["verify_status"],
                        requests_sent=sent_cnt, requests_pending=pending_cnt,
                        requests_expired=expired, requests_done=done_cnt, answers=ans, other_farmers=dict(seen, farmers=len(who)),
                        can_approve=can_appr, can_reject=True, can_defer=True, can_reverify=(pending_cnt <= 0),
                        at_report=dict(P=last["post_p"], n_eff=last["n_eff"], independent_farmers=last["n_src"], conflict=last["conflict"],
                        costs=json.loads(last["costs"]) if last["costs"] else None, note=last["note"]), now=lv))
    return out


def reviewed_watchlist(c, now):
    """Verifications that have been reviewed (rejected or deferred) by admin."""
    rows = c.execute("SELECT r.*, f.name AS fname FROM reports r JOIN farmers f ON f.id=r.farmer_id WHERE r.verify_status IN ('rejected','deferred') AND r.ts>? ORDER BY r.ts DESC LIMIT 50", (now - 7 * 86400,)).fetchall()
    groups = {}
    for r in rows:
        crop, dis = E.split_class(r["top_class"])
        g = groups.setdefault((crop, dis, r["verify_status"]), dict(id=r["id"], crop=crop, disease=dis, status=r["verify_status"], note=r["note"], last_ts=r["ts"], reporters=set()))
        g["reporters"].add(r["fname"])
        if r["ts"] > g["last_ts"]: g["last_ts"] = r["ts"]
    return [dict(v, reporters=sorted(v["reporters"])) for v in groups.values()]


def outbreak_view(c, o):
    d = dict(o); d["evidence"] = json.loads(d["evidence"] or "[]")
    d["alerts_persisted"] = c.execute("SELECT COUNT(*) FROM alerts WHERE outbreak_id=?", (o["id"],)).fetchone()[0]   # real rows, not attempts
    d["independent_farmers"] = independent_in_evidence(c, o); d["can_approve"] = d["independent_farmers"] >= int(CFG.get("min_sources", 2))
    if o["status"] in ("pending", "active"):                  # evidence picture RIGHT NOW (healthy neighbours may have reported since the outbreak was raised)
        d["now"] = live_view(c, o["crop"], o["disease"], (o["lat"], o["lon"]), time.time())
    d["alerts_read"] = c.execute("SELECT COUNT(*) FROM alerts WHERE outbreak_id=? AND read=1", (o["id"],)).fetchone()[0]
    return d


@app.get("/api/admin/overview")
def admin_overview(x_admin_key: str = Header(default="")):
    need_admin(x_admin_key)
    now = time.time()
    with db.tx() as c:
        op = [outbreak_view(c, r) for r in c.execute("SELECT * FROM outbreaks WHERE status IN ('active','pending') ORDER BY ts DESC LIMIT 100")]
        done = [outbreak_view(c, r) for r in c.execute("SELECT * FROM outbreaks WHERE status IN ('confirmed','rejected','deferred') ORDER BY COALESCE(reviewed_ts, ts) DESC LIMIT 50")]
        return dict(approval_required=REQUIRE_APPROVAL, open_outbreaks=op, reviewed_outbreaks=done,
                    verifications=verify_watchlist(c, now), reviewed_verifications=reviewed_watchlist(c, now))


def independent_in_evidence(c, o):
    """Distinct farmers whose genuinely NEW reports (w >= 0.5) are in the outbreak's saved evidence. Approval needs >= min_sources."""
    ev_ids = [int(x) for x in json.loads(o["evidence"] or "[]") if str(x).isdigit()]
    if not ev_ids: return 0
    return c.execute(f"SELECT COUNT(DISTINCT farmer_id) FROM reports WHERE id IN ({','.join('?' * len(ev_ids))}) AND w>=0.5", tuple(ev_ids)).fetchone()[0]


def review(oid, confirmed=True, action=None):
    notes = []
    with LOCK, db.tx() as c:
        o = c.execute("SELECT * FROM outbreaks WHERE id=?", (oid,)).fetchone()
        if not o: raise HTTPException(404, "outbreak not found")
        if o["status"] not in ("active", "pending"): raise HTTPException(409, f"already {o['status']}")
        now = time.time()
        
        if action == "defer":
            c.execute("UPDATE outbreaks SET status='deferred', reviewed_ts=? WHERE id=?", (now, oid))
            return dict(status="deferred", message="Outbreak deferred for observation.", reliability_updates=0, alerts_created=0, alerts_total=0), notes

        # A manual click cannot bypass the minimum independent-source rule.
        if confirmed:
            independent = independent_in_evidence(c, o)
            if independent < int(CFG.get("min_sources", 2)):
                raise HTTPException(409, f"Cannot approve: only {independent} independent farmer(s) in saved outbreak evidence; at least {int(CFG.get('min_sources', 2))} are required.")
        c.execute("UPDATE outbreaks SET status=?, reviewed_ts=? WHERE id=?", ("confirmed" if confirmed else "rejected", now, oid))
        created = 0
        if confirmed:                                        # approval is what releases alerts in approval mode; otherwise it only fills gaps
            created, notes = deliver_alerts(c, dict(o), now)
        else:                                                # tell the farmers who already got the alert that it was withdrawn
            for r in c.execute("SELECT farmer_id FROM alerts WHERE outbreak_id=?", (oid,)):
                notes.append((r["farmer_id"], dict(type="alert_update", outbreak_id=oid, status="rejected")))
        # reliability: each FARMER is judged once, weighted by how much their reports really counted (repeats count ~0), and
        # earns the +20 only once, only if a genuinely new report of theirs said 'disease' and the admin confirmed it.
        ev = set(json.loads(o["evidence"] or "[]")); upd = {}
        for r in db.recent_reports(c, o["lat"], o["lon"], o["ts"] - CFG["fuse_window_s"], km=CFG["fuse_radius_m"] / 1000 + 1):
            if r["crop"] != o["crop"] or E.haversine_m(o["lat"], o["lon"], r["lat"], r["lon"]) > CFG["fuse_radius_m"]: continue
            said = r["id"] in ev or r["top_class"].endswith(o["disease"])
            right = said == confirmed
            wgt = (1.0 if said else 0.25) * r["w"]
            u = upd.setdefault(r["farmer_id"], [0.0, 0.0, 0])
            u[0 if right else 1] += wgt
            if right and said and r["w"] >= 0.5: u[2] = 20
        for fid, (da, db_, pts) in upd.items():
            c.execute("UPDATE farmers SET a=a+?, b=b+?, points=points+? WHERE id=?", (da, db_, pts, fid))
        total = c.execute("SELECT COUNT(*) FROM alerts WHERE outbreak_id=?", (oid,)).fetchone()[0]
    return dict(status="confirmed" if confirmed else "rejected", reliability_updates=len(upd), alerts_created=created, alerts_total=total), notes


def review_verification(report_id: int, action: str, note: Optional[str] = None):
    action = (action or "").lower().strip()
    if action not in ("reject", "defer", "confirm", "reverify"):
        raise HTTPException(400, f"invalid action: {action}. Must be reject, defer, confirm, or reverify.")
    notes = []
    with LOCK, db.tx() as c:
        rep = c.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        if not rep: raise HTTPException(404, "report not found")
        crop, dis = E.split_class(rep["top_class"])
        now = time.time()
        # Find all reports in this candidate group (same crop & disease within the fusion window)
        cand_reps = c.execute("SELECT * FROM reports WHERE crop=? AND top_class LIKE ? AND action='verify' AND ts>?",
                              (crop, f"%{dis}%", now - 86400)).fetchall()
        if not cand_reps: cand_reps = [rep]
        rep_ids = [r["id"] for r in cand_reps]
        q_ids = ",".join("?" * len(rep_ids))
        
        # Check if already decided
        if all(r["verify_status"] in ("rejected", "deferred", "confirmed") for r in cand_reps):
            st = cand_reps[0]["verify_status"]
            raise HTTPException(409, f"Candidate already {st}")
        
        if action == "reject":
            c.execute(f"UPDATE reports SET verify_status='rejected', note=? WHERE id IN ({q_ids})", (note or "Rejected by admin as false alarm", *rep_ids))
            c.execute(f"UPDATE requests SET status='cancelled' WHERE report_id IN ({q_ids}) AND status='pending'", rep_ids)
            upd = {}
            for r in c.execute(f"SELECT farmer_id, w, top_class FROM reports WHERE id IN ({q_ids})", rep_ids).fetchall():
                u = upd.setdefault(r["farmer_id"], [0.0, 0.0, 0])
                u[1] += 1.0 * r["w"]  # b (wrong)
            for r in c.execute(f"SELECT farmer_id FROM requests WHERE report_id IN ({q_ids}) AND answer='contradicts'", rep_ids).fetchall():
                u = upd.setdefault(r["farmer_id"], [0.0, 0.0, 0])
                u[0] += 1.0  # a (right)
            for fid, (da, db_, pts) in upd.items():
                c.execute("UPDATE farmers SET a=a+?, b=b+?, points=points+? WHERE id=?", (da, db_, pts, fid))
            return dict(status="rejected", message="Candidate rejected as false alarm.", reliability_updates=len(upd)), notes
            
        elif action == "defer":
            c.execute(f"UPDATE reports SET verify_status='deferred', note=? WHERE id IN ({q_ids})", (note or "Deferred by admin", *rep_ids))
            return dict(status="deferred", message="Candidate deferred for further observation."), notes
            
        elif action == "reverify":
            st, nn, n, in_range, rad = create_verify_requests(c, rep["farmer_id"], rep["lat"], rep["lon"],
                                                              dict(crop=crop, disease=dis), rep["id"],
                                                              [dict(r) for r in cand_reps], now)
            notes += nn
            c.execute(f"UPDATE reports SET verify_status='requested' WHERE id IN ({q_ids})", rep_ids)
            return dict(status="reverified", requests_created=n, farmers_in_range=in_range, verify_radius_km=round(rad / 1000, 1)), notes
            
        elif action == "confirm":
            lv = live_view(c, crop, dis, (rep["lat"], rep["lon"]), now)
            independent = lv["independent_farmers"] if lv else 0
            min_src = int(CFG.get("min_sources", 2))
            if independent < min_src:
                raise HTTPException(409, f"Cannot approve: only {independent} independent farmer(s) in saved evidence; at least {min_src} are required.")
            oid = c.execute("INSERT INTO outbreaks(crop,disease,lat,lon,ts,P,n_eff,evidence,status,reviewed_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (crop, dis, rep["lat"], rep["lon"], now, lv["P"], lv["n_eff"], json.dumps(rep_ids), "confirmed", now)).lastrowid
            c.execute(f"UPDATE reports SET verify_status='confirmed', action='alert' WHERE id IN ({q_ids})", rep_ids)
            created, alert_notes = deliver_alerts(c, dict(id=oid, crop=crop, disease=dis, lat=rep["lat"], lon=rep["lon"]), now)
            notes += alert_notes
            upd = {}
            for r in cand_reps:
                u = upd.setdefault(r["farmer_id"], [0.0, 0.0, 0])
                u[0] += 1.0 * r["w"]
                if r["w"] >= 0.5: u[2] = 20
            for fid, (da, db_, pts) in upd.items():
                c.execute("UPDATE farmers SET a=a+?, b=b+?, points=points+? WHERE id=?", (da, db_, pts, fid))
            total = c.execute("SELECT COUNT(*) FROM alerts WHERE outbreak_id=?", (oid,)).fetchone()[0]
            return dict(status="confirmed", outbreak_id=oid, alerts_created=created, alerts_total=total, reliability_updates=len(upd)), notes


@app.post("/api/admin/outbreaks/{oid}/verify")
async def admin_verify(oid: int, v: VerifyIn, x_admin_key: str = Header(default="")):
    need_admin(x_admin_key)
    act = v.action or ("confirm" if v.confirmed else "reject")
    out, notes = await run_in_threadpool(review, oid, v.confirmed, act)
    for fid, payload in notes: await hub.send(fid, payload)
    return out


@app.post("/api/admin/verifications/{report_id}/decision")
async def admin_verify_decision(report_id: int, body: CandidateDecisionIn, x_admin_key: str = Header(default="")):
    need_admin(x_admin_key)
    out, notes = await run_in_threadpool(review_verification, report_id, body.action, body.note)
    for fid, payload in notes: await hub.send(fid, payload)
    return out


@app.post("/api/admin/verifications/{report_id}/verify")
async def admin_verify_candidate(report_id: int, v: VerifyIn, x_admin_key: str = Header(default="")):
    act = v.action or ("confirm" if v.confirmed else "reject")
    return await admin_verify_decision(report_id, CandidateDecisionIn(action=act, note=v.note), x_admin_key)


@app.get("/")
def index(): return FileResponse(BASE / "static" / "index.html")
app.mount("/uploads", StaticFiles(directory=UPLOADS), name="uploads")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")