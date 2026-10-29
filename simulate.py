"""Simulation benchmark: replays crowdsourced report streams (duplicates, conflicts, unreliable farmers, wrong photos) through
7 methods. Image outputs come from the REAL trained model run on REAL dataset images.
   python simulate.py --data "<dataset_clean>"  --episodes 150 --seeds 5
NOTE: this is a *simulation*: it shows behaviour under the stated noise model, not real-world farmer impact."""
import argparse, json, math, random
from pathlib import Path
import numpy as np
import engine as E

ART = Path(__file__).parent / "artifacts"
LAT0, LON0 = 13.0827, 80.2707
ap = argparse.ArgumentParser()
ap.add_argument("--data"); ap.add_argument("--per_class", type=int, default=25)
ap.add_argument("--oracle", action="store_true", help="sanity check: replace model outputs by ~95%%-accurate synthetic ones")
ap.add_argument("--episodes", type=int, default=150); ap.add_argument("--seeds", type=int, default=5)
a = ap.parse_args()


def build_pool():
    f = ART / "sim_pool.npz"
    if f.exists() and not a.data:
        z = np.load(f, allow_pickle=True); return list(z["classes"]), z["probs"], z["emb"], [int(x, 16) for x in z["dh"]], z["u"], z["label"]
    from PIL import Image
    from model_utils import Predictor
    P = Predictor(); root = Path(a.data); root = root / "valid" if (root / "valid").is_dir() else root / "train" if (root / "train").is_dir() else root
    rng = random.Random(0); probs, emb, dh, u, lab = [], [], [], [], []
    for ci, cname in enumerate(P.classes):
        files = sorted((root / cname).glob("*")); rng.shuffle(files)
        for fp in files[: a.per_class]:
            try: r = P.predict(Image.open(fp))
            except Exception: continue
            probs.append(r["probs"]); emb.append(r["emb"]); dh.append(format(r["dhash"], "x")); u.append(r["u"]); lab.append(ci)
    np.savez(f, classes=np.array(P.classes), probs=np.array(probs), emb=np.array(emb), dh=np.array(dh), u=np.array(u), label=np.array(lab))
    print(f"pool: {len(lab)} real images -> {f}")
    return P.classes, np.array(probs), np.array(emb), [int(x, 16) for x in dh], np.array(u), np.array(lab)

names, PR, EM, DH, U, LB = build_pool()
if a.oracle:
    _r = np.random.default_rng(5); PR = np.full((len(LB), len(names)), 0.01); U = np.full(len(LB), 0.15)
    for i, l in enumerate(LB): PR[i, l if _r.random() < 0.93 else _r.integers(len(names))] = 0.9
    PR /= PR.sum(1, keepdims=True)
cls = E.Classes(names)
by_label = {c: np.where(LB == c)[0] for c in range(len(names))}
crops = [c for c, ids in cls.by_crop.items() if any(cls.healthy[i] for i in ids) and any(not cls.healthy[i] and len(by_label[i]) for i in ids)]
assert crops, "need at least one crop with a healthy class and a disease class"
xy = lambda dx, dy: (LAT0 + dy / 111320.0, LON0 + dx / (111320.0 * math.cos(math.radians(LAT0))))


def ev(rng, fid, pool_i, x, y, ts, copy_of=None):
    lat, lon = xy(x, y)
    emb, dh = EM[pool_i], DH[pool_i]
    if copy_of is not None:                                   # re-upload of the same photo (small compression noise)
        emb = emb + 0.02 * rng.normal(size=emb.shape); emb = emb / np.linalg.norm(emb)
        for b in rng.choice(64, int(rng.integers(0, 3)), replace=False): dh ^= (1 << int(b))
    return dict(farmer_id=fid, lat=lat, lon=lon, ts=ts, probs=PR[pool_i], emb=emb, dhash=int(dh), u=float(U[pool_i]), crop=cls.crop[LB[pool_i]])


def episode(rng, farmers, bad):
    kind = rng.choice(["outbreak", "false_alarm", "quiet"], p=[0.4, 0.4, 0.2])
    crop = crops[rng.integers(len(crops))]; ids = cls.by_crop[crop]
    healthy = [i for i in ids if cls.healthy[i]][0]; dis = [i for i in ids if not cls.healthy[i] and len(by_label[i])]; D = dis[rng.integers(len(dis))]
    pick = lambda c: int(rng.choice(by_label[c]))
    evs = []; t0 = 0.0
    if kind == "outbreak":
        cx, cy = rng.uniform(-2500, 2500, 2)
        for f in farmers:
            x, y = f["xy"]; d = math.hypot(x - cx, y - cy)
            if d < 2000 and rng.random() < 0.65:
                t = t0 + rng.exponential(5 * 3600)
                lbl = D if rng.random() < 0.85 else healthy
                if f["bad"]: lbl = int(rng.choice(ids))
                i = pick(lbl); evs.append(ev(rng, f["id"], i, x, y, t))
                for _ in range(int(rng.choice([0, 0, 0, 1, 2, 3]))): evs.append(ev(rng, f["id"], i, x + rng.normal(0, 8), y + rng.normal(0, 8), t + rng.uniform(20, 300), copy_of=i))
            elif d < 5000 and rng.random() < 0.25:
                evs.append(ev(rng, f["id"], pick(healthy if not f["bad"] else int(rng.choice(ids))), x, y, t0 + rng.uniform(0, 36 * 3600)))
    elif kind == "false_alarm":                                   # one sick plant, one over-eager farmer, healthy neighbours
        f = farmers[rng.integers(len(farmers))]; x, y = f["xy"]; t = rng.uniform(0, 3600); i = pick(D)
        for k in range(int(rng.integers(3, 7))):
            if k % 2 == 0: evs.append(ev(rng, f["id"], i, x + rng.normal(0, 6), y + rng.normal(0, 6), t + k * 90 + rng.uniform(0, 60), copy_of=i))
            else: evs.append(ev(rng, f["id"], pick(D), x + rng.normal(0, 6), y + rng.normal(0, 6), t + k * 90 + rng.uniform(0, 60)))
        for g in farmers:
            if g["id"] != f["id"] and math.hypot(g["xy"][0] - x, g["xy"][1] - y) < 3000 and rng.random() < 0.4:
                evs.append(ev(rng, g["id"], pick(healthy if not g["bad"] else int(rng.choice(ids))), *g["xy"], rng.uniform(0, 24 * 3600)))
    else:
        for g in rng.choice(len(farmers), 8, replace=False):
            g = farmers[g]; evs.append(ev(rng, g["id"], pick(healthy if not g["bad"] else int(rng.choice(ids))), *g["xy"], rng.uniform(0, 24 * 3600)))
    evs.sort(key=lambda e: e["ts"])
    first = next((e["ts"] for e in evs if int(np.argmax(e["probs"])) == D), None)
    return kind, crop, cls.dis[D], evs, first


def ours_factory(**fl):
    return lambda: E.Pipeline(cls, E.CFG, **fl)

METHODS = {
    "single report (any disease report alerts)": "single", "majority vote (>=2 reports)": "majority", "fixed threshold (>=3 reports)": "fixed3",
    "ours - no dependency handling": dict(use_dep=False), "ours - no uncertainty": dict(use_unc=False),
    "ours - no reliability": dict(use_rel=False), "ours (full)": dict(),
}


def run_method(m, ep, rel_ab):
    kind, crop, dis, evs, first = ep
    alerts, hist, verifies = [], [], 0
    pipe = E.Pipeline(cls, E.CFG, **m) if isinstance(m, dict) else None
    if pipe: pipe.rel_ab = rel_ab
    for e in evs:
        top = int(np.argmax(e["probs"])); c, d = cls.crop[top], cls.dis[top]
        if pipe:
            n0, v0 = len(pipe.alerts), len(pipe.verify_log); pipe.step(e)
            if len(pipe.alerts) > n0: alerts.append((pipe.alerts[-1]["crop"], pipe.alerts[-1]["disease"], e["ts"]))
            verifies += len(pipe.verify_log) - v0
        else:
            near = [h for h in hist if h[0] == c and E.haversine_m(h[3], h[4], e["lat"], e["lon"]) < 3000 and e["ts"] - h[2] < 48 * 3600]
            near.append((c, top, e["ts"], e["lat"], e["lon"], float(e["probs"][top])))
            if not cls.healthy[top]:
                votes = sum(1 for h in near if h[1] == top)
                ok = {"single": float(e["probs"][top]) >= 0.5, "majority": len(near) >= 2 and votes / len(near) > 0.5,
                      "fixed3": sum(1 for h in near if h[1] == top and h[5] >= 0.5) >= 3}[m]
                if ok and not any(x[0] == c and x[1] == d for x in alerts): alerts.append((c, d, e["ts"]))
            hist.append(near[-1])
    if pipe:                                                      # admin feedback after the episode -> reliability learning
        for h in pipe.hist:
            if cls.crop[int(np.argmax(h["probs"]))] != crop: continue
            said = cls.dis[int(np.argmax(h["probs"]))] == dis; truth = kind == "outbreak"; w = 1.0 if said else 0.25
            a_, b_ = rel_ab.get(h["farmer_id"], (3, 2)); rel_ab[h["farmer_id"]] = (a_ + w, b_) if said == truth else (a_, b_ + w)
    hit = [t for (c, d, t) in alerts if c == crop and d == dis]
    return dict(alerted=bool(alerts), hit=bool(hit) and kind == "outbreak", delay=(min(hit) - first) / 3600 if hit and kind == "outbreak" and first is not None else None,
                false_alert=bool(alerts) and (kind != "outbreak" or not hit), verifies=verifies, kind=kind)


rows = {k: [] for k in METHODS}
for s in range(a.seeds):
    rng = np.random.default_rng(s)
    farmers = [dict(id=i + 1, xy=tuple(rng.uniform(-4500, 4500, 2)), bad=bool(rng.random() < 0.2)) for i in range(40)]
    rel = {k: {} for k in METHODS}; acc = {k: [] for k in METHODS}
    for _ in range(a.episodes):
        ep = episode(rng, farmers, None)
        for k, m in METHODS.items(): acc[k].append(run_method(m, ep, rel[k]))
    for k, rs in acc.items():
        out = [r for r in rs if r["kind"] == "outbreak"]; non = [r for r in rs if r["kind"] != "outbreak"]; al = [r for r in rs if r["alerted"]]
        rows[k].append(dict(recall=np.mean([r["hit"] for r in out]), far=np.mean([r["false_alert"] for r in non]),
                            precision=(sum(1 for r in al if not r["false_alert"]) / len(al)) if al else float("nan"),
                            delay=np.nanmean([r["delay"] for r in out if r["delay"] is not None] or [np.nan]), verifies=np.mean([r["verifies"] for r in rs])))
f = lambda k, key, pct=False: (lambda v: f"{(np.nanmean(v)*(100 if pct else 1)):.1f}±{(np.nanstd(v)*(100 if pct else 1)):.1f}")([r[key] for r in rows[k]])
print(f"\n{a.episodes} episodes x {a.seeds} seeds | classes={len(names)} | pool images={len(LB)} | mean±std over seeds\n")
print(f"{'method':46s} {'false-alert %':>14s} {'recall %':>12s} {'precision %':>13s} {'delay h':>10s} {'verify req/ep':>14s}")
for k in METHODS: print(f"{k:46s} {f(k,'far',True):>14s} {f(k,'recall',True):>12s} {f(k,'precision',True):>13s} {f(k,'delay'):>10s} {f(k,'verifies'):>14s}")
mp = ART / "metrics.json"
if mp.exists(): m = json.loads(mp.read_text()); print(f"\nimage model: val acc {m['val_acc']:.3f} | ECE raw {m['ece_raw']:.3f} -> calibrated {m['ece_calibrated']:.3f}")
(ART / "sim_results.json").write_text(json.dumps({k: {kk: [float(r[kk]) for r in v] for kk in v[0]} for k, v in rows.items()}, indent=1))
