"""Shared test helpers. Every test builds its OWN temporary database + upload folder, so tests never touch your leafwatch.db.

The neural network is replaced by a stub that returns the probabilities a test asks for (the real model.pt cannot be judged by logic
tests). Everything else is the real code: engine, SQLite, quality gate (on real JPEG bytes), request/alert/approval logic."""
import asyncio, io, os, sys, tempfile, time
import numpy as np
from PIL import Image, ImageFilter

NAMES = ["Tomato - Healthy", "Tomato - Late Blight", "Tomato - Early Blight", "Potato - Healthy", "Potato - Late Blight"]
BASE_LAT, BASE_LON = 13.0827, 80.2707
M_PER_DEG = 111194.9266                       # metres per degree of latitude for the engine's haversine (R = 6 371 000 m)
ADMIN = "unit-test-admin-key"
RNG = np.random.default_rng(7)


def dlat(metres):                             # latitude offset that is `metres` north (longitude unchanged)
    return metres / M_PER_DEG


def fresh_app(approval=True, admin_key=ADMIN, db_path=None, uploads=None):
    """Import app.py from scratch against a new temp database. Calling it again with the SAME db_path simulates a server restart."""
    tmp = tempfile.mkdtemp(prefix="leafwatch_test_")
    os.environ.update(LEAFWATCH_DB=str(db_path or os.path.join(tmp, "t.db")), LEAFWATCH_UPLOADS=str(uploads or os.path.join(tmp, "up")),
                      LEAFWATCH_NO_MODEL="1")
    if approval is None: os.environ.pop("LEAFWATCH_REQUIRE_ADMIN_APPROVAL", None)          # None = the shipped default (variable not set at all)
    else: os.environ["LEAFWATCH_REQUIRE_ADMIN_APPROVAL"] = "1" if approval else "0"
    if admin_key is None: os.environ.pop("LEAFWATCH_ADMIN_KEY", None)
    else: os.environ["LEAFWATCH_ADMIN_KEY"] = admin_key
    for m in ("app", "db"): sys.modules.pop(m, None)
    import app as A, engine as E
    A.CLS = E.Classes(NAMES); A.CALIB = {"blur_min": 120.6}
    return A


def unit(n):
    v = RNG.normal(size=n); return v / np.linalg.norm(v)


def pred(cls_name, p=0.95, u=0.10, dhash=None, emb=None):
    i = NAMES.index(cls_name); pr = np.full(len(NAMES), (1 - p) / (len(NAMES) - 1)); pr[i] = p
    return dict(probs=pr, emb=unit(64) if emb is None else emb, dhash=int(RNG.integers(1, 2 ** 62)) if dhash is None else dhash, u=u)


def add_farmer(A, name, north_m=0.0, east_m=0.0):
    with A.db.tx() as c:
        return c.execute("INSERT INTO farmers(name,lat,lon,created) VALUES(?,?,?,?)", (name, BASE_LAT + dlat(north_m), BASE_LON + east_m / 112000.0, time.time())).lastrowid


def farmer(A, fid):
    with A.db.tx() as c: return dict(c.execute("SELECT * FROM farmers WHERE id=?", (fid,)).fetchone())


def where(A, fid):
    f = farmer(A, fid); return f["lat"], f["lon"]


def report(A, fid, cls_name, p=0.95, u=0.10, dhash=None, emb=None, request_id=None, at=None):
    """One report through the real ingest(). `at` = (lat, lon) or None for the farmer's registered spot. Returns (response dict, websocket notes)."""
    lat, lon = at or where(A, fid)
    return A.ingest(fid, lat, lon, pred(cls_name, p, u, dhash, emb), "x.jpg", request_id)


def q(A, sql, *args):
    with A.db.tx() as c: return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(A, sql, *args):
    r = q(A, sql, *args); return r[0] if r else None


def count(A, table, where_sql="1=1", *args):
    return one(A, f"SELECT COUNT(*) n FROM {table} WHERE {where_sql}", *args)["n"]


def snapshot(A):
    """Row counts + total points: used to prove that a rejected photo changed NOTHING."""
    return dict(reports=count(A, "reports"), outbreaks=count(A, "outbreaks"), alerts=count(A, "alerts"), requests=count(A, "requests"),
                points=one(A, "SELECT COALESCE(SUM(points),0) s FROM farmers")["s"])


def jpeg(kind="sharp", size=300):
    """Synthetic leaf-like photo as JPEG bytes. kinds: sharp | blurry | dark | bright | flat."""
    rs = np.random.default_rng(3)
    base = rs.integers(0, 255, (size, size, 3), dtype=np.uint8)
    img = Image.fromarray(base)
    if kind == "blurry": img = img.filter(ImageFilter.GaussianBlur(12))
    elif kind == "dark": img = Image.fromarray((base * 0.05).astype(np.uint8))
    elif kind == "bright": img = Image.fromarray((235 + base * 0.08).astype(np.uint8))
    elif kind == "flat": img = Image.new("RGB", (size, size), (90, 140, 70))
    b = io.BytesIO(); img.save(b, "JPEG", quality=92); return b.getvalue()


class StubPredictor:
    """Replaces the neural network inside A.report(): confident prediction of `cls_name`, new random embedding/hash per call."""
    def __init__(self, cls_name="Tomato - Late Blight", p=0.95, u=0.10): self.cls_name, self.p, self.u = cls_name, p, u
    def predict(self, img): return pred(self.cls_name, self.p, self.u)


def http_report(A, fid, raw, lat=None, lon=None, request_id=None):
    """Call the real async /api/report handler (photo bytes -> quality gate -> ingest) without an HTTP server."""
    la, lo = (lat, lon) if lat is not None else where(A, fid)
    up = A.UploadFile(file=io.BytesIO(raw), filename="leaf.jpg")
    return asyncio.run(A.report(farmer_id=fid, lat=la, lon=lo, request_id=request_id, image=up))


def three_sick_neighbours(A, cls_name="Tomato - Late Blight", north_m=0.0):
    """Three independent farmers at the SAME spot, each with a different confident sick photo -> engine decision ALERT."""
    ids = [add_farmer(A, f"S{i}", north_m=north_m) for i in range(3)]
    return ids, [report(A, f, cls_name)[0] for f in ids]
