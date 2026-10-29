"""Sanity tests for the engine (run: python test_engine.py)."""
import numpy as np, engine as E

cls = E.Classes(["Tomato - Healthy", "Tomato - Late Blight", "Tomato - Early Blight", "Potato - Healthy", "Potato - Late Blight"])
rng = np.random.default_rng(0)
LAT, LON = 13.0, 80.0

def vec(n=64):
    v = rng.normal(size=n); return v / np.linalg.norm(v)

def rep(fid, dlat=0, dlon=0, ts=0, cls_i=1, p=0.95, emb=None, dh=None, u=0.2):
    probs = np.full(5, (1 - p) / 4); probs[cls_i] = p
    return dict(farmer_id=fid, lat=LAT + dlat, lon=LON + dlon, ts=ts, probs=probs,
                emb=vec() if emb is None else emb, dhash=int(rng.integers(0, 2**62)) if dh is None else dh, u=u)

# classes parsing
assert cls.crop[1] == "Tomato" and cls.dis[1] == "Late Blight" and cls.healthy[0] and not cls.healthy[1]

# 1) one confident report => VERIFY (not alert)
p = E.Pipeline(cls); r1 = p.step(rep(1)); assert r1["action"] == "verify", r1["action"]

# 2) five uploads of the SAME plant by one farmer must NOT trigger an alert
p = E.Pipeline(cls); e = vec(); dh = 0xABCDEF
acts = [p.step(rep(1, ts=i * 60, emb=e + 0.01 * vec(), dh=dh ^ (1 << i), cls_i=1))["action"] for i in range(5)]
assert "alert" not in acts, acts
nov = [h["w"] for h in p.hist]; assert nov[0] == 1.0 and max(nov[1:]) < 0.2, nov

# 3) without dependency handling the same duplicates inflate P (0.99 vs ~0.78); and one farmer alone can never self-confirm an alert
def spam_then_neighbour(dep):
    p = E.Pipeline(cls, use_dep=dep); e = vec(); dh = 0xABCDEF; acts = []
    for i in range(3): acts.append(p.step(rep(1, ts=i * 60, emb=e + 0.01 * vec(), dh=dh ^ (1 << i)))["action"])
    assert "alert" not in acts, acts                                   # single source -> at most VERIFY
    return p.step(rep(2, dlat=0.0009, ts=400))["fusion"]["P"]
assert spam_then_neighbour(False) > spam_then_neighbour(True) + 0.1

# 4) three independent farmers ~300 m apart => ALERT
p = E.Pipeline(cls); acts = [p.step(rep(f, dlat=0.0027 * f, ts=f * 600))["action"] for f in (1, 2, 3)]
assert acts[-1] == "alert", acts

# 5) one farmer's different photos of his own plot: 2nd photo keeps only the remaining field budget, a 3rd adds ~nothing,
#    and a single farmer alone can never trigger an alert; a DIFFERENT farmer next door is full independent evidence
p = E.Pipeline(cls); w1 = p.step(rep(1, ts=0))["novelty"]; w2 = p.step(rep(1, dlat=0.0001, ts=3000))["novelty"]; w3 = p.step(rep(1, dlat=0.0001, ts=6000))["novelty"]
assert w1 == 1.0 and 0.2 <= w2 <= 0.3 and w3 <= 0.06 and w1 + w2 + w3 <= 1.4, (w1, w2, w3)
assert p.step(rep(2, dlat=0.0002, ts=7000))["novelty"] > 0.9

# 6) conflict: disagreeing healthy reports lower P
base = E.Pipeline(cls); [base.step(rep(f, dlat=0.002 * f, ts=f * 600)) for f in (1, 2)]
pa = base.step(rep(3, dlat=0.006, ts=1800))["fusion"]["P"]
conf = E.Pipeline(cls); [conf.step(rep(f, dlat=0.002 * f, ts=f * 600)) for f in (1, 2)]
for f in (7, 8, 9): conf.step(rep(f, dlat=0.002 * f, ts=1000 + f, cls_i=0, p=0.95))
pb = conf.step(rep(3, dlat=0.006, ts=1800))["fusion"]
assert pb["P"] < pa and pb["conflict"] > 0.2, (pa, pb)

# 7) uncertain image (flat probs) -> no candidate; other crop ignored
flat = rep(1, p=0.2); assert E.Pipeline(cls).step(flat)["action"] == "none"

# 8) decision monotonic: defer < verify < alert as P grows
seq = [E.decide(x)[0] for x in (0.03, 0.3, 0.9)]; assert seq == ["defer", "verify", "alert"], seq
print("all engine tests passed")
