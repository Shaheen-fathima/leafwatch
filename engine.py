"""LeafWatch core engine (pure numpy, no web / no torch).

Pipeline per incoming report:
  1. dependency   -> how much of this report repeats an earlier observation (evidence clusters)
  2. fusion       -> Bayesian fusion of calibrated image probabilities + farmer reliability
                     + spatio-temporal kernel => P(outbreak)   (conflicts reduce P)
  3. decision     -> expected-cost choice: ALERT / VERIFY / DEFER
"""
import math, re
import numpy as np

CFG = dict(
    # dependency (evidence clustering)
    emb_lo=0.90, emb_hi=0.98,      # cosine range mapped to image-dependence 0..1 (re-fitted by train.py)
    dhash_dup=6, dhash_exact=2,    # hamming distance of 64-bit dHash
    same_plot_m=40.0, loc_scale_m=300.0, dep_time_s=2 * 86400, burst_s=600, min_novelty=0.05,
    min_sources=2,                  # an ALERT needs >=2 independent farmers; one source can never confirm itself
    max_conflict_alert=0.50,        # an ALERT is downgraded to VERIFY while the reports disagree this much (conflict index 0..1)
    field_budget=1.25,             # max evidence one farmer's own plot can contribute per 24 h (one field = one observation unit)
    exact_dep=0.95, exact_dup_weight=0.0,   # re-upload of the very same photo: kept in the database, contributes NO evidence
    # fusion
    fuse_radius_m=5000.0, fuse_space_m=3000.0, fuse_time_s=2 * 86400, fuse_window_s=5 * 86400,
    prior_outbreak=0.05, prior_report=0.15, unc_gamma=0.6, rel_default=0.8, cand_min_p=0.30,
    # decision (relative costs)
    cost_fa=1.0, cost_miss=4.0, cost_verify=0.12, v_sens=0.85, v_spec=0.90,
    # alerts
    alert_radius_m=5000.0, verify_radius_m=1500.0, cooldown_s=12 * 3600, verify_cooldown_s=6 * 3600,
    verify_fallback_radius_m=5000.0,   # nobody within verify_radius_m? ask the nearest farmers inside this wider radius (0 = never widen)
    remind_after_s=3 * 3600,           # a verification request unanswered after this long gets ONE reminder (requests expire after 24 h)
    pts_new=5, pts_verify=15,          # points: genuinely new photo / answering a verification request (once per report)
    # photo-quality gate (see assess_quality). blur_min itself comes from artifacts/calib.json (learned from your dataset)
    blur_min_default=30.0, blur_soft_factor=0.5,        # sharpness < blur_min*factor = clearly blurry; between = "soft"
    soft_ok_crop_conf=0.90, soft_ok_u=0.40,             # a merely soft photo is accepted only if the model is this sure
    dark_mean=35.0, bright_mean=240.0, min_contrast=8.0,   # mean grey level 0-255 / grey-level std
    crop_min_conf=0.60, crop_min_margin=0.15, u_reject=0.70,
    healthy_min_p=0.60, healthy_max_u=0.50,             # below this a "healthy" prediction is reported as inconclusive
)


def haversine_m(la1, lo1, la2, lo2):
    p1, p2 = math.radians(la1), math.radians(la2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 2 * 6371000.0 * math.asin(min(1.0, math.sqrt(a)))


def logit(p):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def sigmoid(x):
    return 1 / (1 + math.exp(-max(min(x, 50), -50)))


def hamming(a, b):
    return bin(int(a) ^ int(b)).count("1")


def split_class(n):
    """'Tomato - Late Blight' / 'Tomato___Late_blight' -> ('Tomato', 'Late blight')"""
    parts = re.split(r"\s-\s|___", n, maxsplit=1)
    return parts[0].replace("_", " ").strip(), (parts[1] if len(parts) > 1 else "unknown").replace("_", " ").strip()


class Classes:
    """Parses folder names like 'Tomato - Late Blight' or 'Tomato___Late_blight'."""

    def __init__(self, names):
        self.names = list(names)
        self.crop, self.dis = [], []
        for n in self.names:
            c, d = split_class(n)
            self.crop.append(c); self.dis.append(d)
        self.healthy = [d.lower() == "healthy" for d in self.dis]
        self.by_crop = {}
        for i, c in enumerate(self.crop):
            self.by_crop.setdefault(c, []).append(i)


# ------------------------------------------------------------------ 1. dependency
def dependency(new, old, cfg=CFG):
    """P(new and old are the same underlying observation). Similarity alone is never enough:
    image similarity is gated by location and time; exact image copies are always dependent."""
    ham = hamming(new["dhash"], old["dhash"])
    if ham <= cfg["dhash_exact"]:
        return 0.95                                   # same photo re-uploaded
    dist = haversine_m(new["lat"], new["lon"], old["lat"], old["lon"])
    dt = abs(new["ts"] - old["ts"])
    cos = float(np.dot(new["emb"], old["emb"])) if np.shape(new["emb"]) == np.shape(old["emb"]) else 0.0   # old row from another model: no image evidence
    d_img = max(0.0, min(1.0, (cos - cfg["emb_lo"]) / max(1e-6, cfg["emb_hi"] - cfg["emb_lo"])))
    if ham <= cfg["dhash_dup"]:
        d_img = max(d_img, 0.9)
    loc = 1.0 if dist <= cfg["same_plot_m"] else math.exp(-(dist - cfg["same_plot_m"]) / cfg["loc_scale_m"])
    d = 0.9 * d_img * loc * math.exp(-dt / cfg["dep_time_s"])
    if new["farmer_id"] == old["farmer_id"] and dist <= cfg["same_plot_m"] and dt <= cfg["burst_s"]:
        d = max(d, 0.45)                              # burst of uploads from the same spot: probably same plant
    return d


def novelty(new, history, cfg=CFG):
    """Returns (weight in [min_novelty,1], dependency score, parent report id or None).
    Reports are never deleted: a dependent report is only down-weighted."""
    best, parent = 0.0, None
    for old in history:
        if old.get("id") is not None and old["id"] == new.get("id"):
            continue
        d = dependency(new, old, cfg)
        if d > best:
            best, parent = d, old
    field = sum(h.get("w", 1.0) for h in history if h["farmer_id"] == new["farmer_id"] and abs(new["ts"] - h["ts"]) <= 86400
                and haversine_m(new["lat"], new["lon"], h["lat"], h["lon"]) <= cfg["same_plot_m"])
    cap = max(cfg["min_novelty"], cfg["field_budget"] - field)   # different photos of one's own plot never add up to a village outbreak
    pid = parent["id"] if parent is not None and best >= 0.5 else None
    if best >= cfg["exact_dep"]:                                  # the very same photo again: it is not evidence at all
        return cfg["exact_dup_weight"], best, pid
    return max(cfg["min_novelty"], min(1.0 - best, cap)), best, pid


def field_parent(new, history, cfg=CFG):
    """Most recent earlier report by the same farmer on the same plot within 24 h (the 'field' evidence unit), or None."""
    best = None
    for h in history:
        if h["farmer_id"] == new["farmer_id"] and abs(new["ts"] - h["ts"]) <= 86400 \
                and haversine_m(new["lat"], new["lon"], h["lat"], h["lon"]) <= cfg["same_plot_m"]:
            if best is None or h["ts"] > best["ts"]:
                best = h
    return best


# ------------------------------------------------------------------ 2. fusion
def fuse(cls, target, now, center, reports, rel, cfg=CFG, use_unc=True, use_rel=True):
    crop, idx = cls.crop[target], cls.by_crop[cls.crop[target]]
    base = logit(cfg["prior_report"])
    tot = pos = neg = neff = 0.0
    sup, clat, clon, cw, n_raw, srcs = [], 0.0, 0.0, 0.0, 0, set()
    for r in reports:
        age = max(0.0, now - r["ts"])
        if age > cfg["fuse_window_s"]:
            continue
        dist = haversine_m(center[0], center[1], r["lat"], r["lon"])
        if dist > cfg["fuse_radius_m"]:
            continue
        P = r["probs"]
        m = float(P[idx].sum())
        if m < 0.5:
            continue                                   # photo of a different crop
        if use_unc:
            p, disc = float(P[target]) / m, 1.0 - cfg["unc_gamma"] * r["u"]
        else:                                          # ablation: hard label, ignores calibration/uncertainty
            j = idx[int(np.argmax(P[idx]))]
            p, disc = (0.9 if j == target else 0.05), 1.0
        rl = rel(r["farmer_id"]) if use_rel else cfg["rel_default"]
        q = rl * p + (1 - rl) * cfg["prior_report"]
        k = math.exp(-dist / cfg["fuse_space_m"]) * math.exp(-age / cfg["fuse_time_s"])
        c = r.get("w", 1.0) * k * disc
        llr = c * (logit(q) - base)
        tot += llr; neff += c; n_raw += 1
        if llr > 0:
            pos += llr; sup.append(r.get("id"))
            if c >= 0.2: srcs.add(r["farmer_id"])
            clat += llr * r["lat"]; clon += llr * r["lon"]; cw += llr
        else:
            neg -= llr
    P_out = sigmoid(logit(cfg["prior_outbreak"]) + tot)
    conflict = 2 * min(pos, neg) / (pos + neg) if (pos + neg) > 0 else 0.0
    cen = (clat / cw, clon / cw) if cw > 0 else tuple(center)
    return dict(P=P_out, n_eff=neff, n_raw=n_raw, n_src=len(srcs), conflict=conflict, centroid=cen, support=sup)


# ------------------------------------------------------------------ 3. decision
def decide(P, cfg=CFG):
    cfa, cm, cv, se, sp = cfg["cost_fa"], cfg["cost_miss"], cfg["cost_verify"], cfg["v_sens"], cfg["v_spec"]
    best = lambda x: min((1 - x) * cfa, x * cm)
    ppos = P * se + (1 - P) * (1 - sp)
    pneg = 1 - ppos
    post_pos = P * se / ppos if ppos > 0 else 0.0
    post_neg = P * (1 - se) / pneg if pneg > 0 else 0.0
    costs = dict(alert=(1 - P) * cfa, defer=P * cm, verify=cv + ppos * best(post_pos) + pneg * best(post_neg))
    return min(costs, key=costs.get), costs


def decide_full(f, cfg=CFG):
    """Cost-optimal action, then the 'at least min_sources independent farmers' rule. Returns (action, cost_action, costs, note)."""
    cost_action, costs = decide(f["P"], cfg)
    action, note = cost_action, None
    if cost_action == "alert" and f["n_src"] < cfg["min_sources"]:
        action = "verify"                                 # ask a second farmer to confirm before alerting the whole area
        note = (f"Lowest expected cost is ALERT, but only {f['n_src']} independent farmer(s) support it "
                f"(rule: at least {cfg['min_sources']}), so the action is VERIFY.")
    elif cost_action == "alert" and f.get("conflict", 0.0) >= cfg.get("max_conflict_alert", 1.1):
        action = "verify"                                 # strong disagreement between farmers: get more evidence before anyone is alerted
        note = (f"Lowest expected cost is ALERT, but the reports conflict too much ({f['conflict']:.0%} >= "
                f"{cfg['max_conflict_alert']:.0%}), so the action is VERIFY.")
    return action, cost_action, costs, note


def healthy_context(new, history, cls, top, now, rel, cfg=CFG):
    """A healthy report is not automatically 'counter-evidence' for everything. It only matters for disease candidates of the SAME
    crop reported within the fusion radius/window. Returns one entry per such candidate with the evidence picture including this report."""
    crop, seen, out = cls.crop[top], set(), []
    for h in history:
        if now - h["ts"] > cfg["fuse_window_s"] or haversine_m(new["lat"], new["lon"], h["lat"], h["lon"]) > cfg["fuse_radius_m"]:
            continue
        j = int(np.argmax(h["probs"]))
        if cls.crop[j] != crop or cls.healthy[j] or j in seen:
            continue
        seen.add(j)
        f = fuse(cls, j, now, (new["lat"], new["lon"]), list(history) + [new], rel, cfg)
        out.append(dict(candidate=f"{cls.crop[j]} - {cls.dis[j]}", P=f["P"], n_eff=f["n_eff"], n_src=f["n_src"], conflict=f["conflict"]))
    return sorted(out, key=lambda x: -x["P"])


def evaluate(new, history, cls, rel, now=None, cfg=CFG, use_dep=True, use_unc=True, use_rel=True):
    """Full per-report evaluation. `new` needs: farmer_id lat lon ts probs emb dhash u. history = earlier reports."""
    now = new["ts"] if now is None else now
    hist = [h for h in history if now - h["ts"] <= 2 * cfg["dep_time_s"]]
    # Photos of DIFFERENT crops are different observations: a wrong-crop photo must not use up the plot budget of the farmer's correct
    # retake (or make it look like a repeat). The very same file (exact dHash) is still caught whatever the crop.
    ncrop = cls.crop[int(np.argmax(new["probs"]))]
    same_obs = lambda h: len(h["probs"]) != len(new["probs"]) or cls.crop[int(np.argmax(h["probs"]))] == ncrop or hamming(new["dhash"], h["dhash"]) <= cfg["dhash_dup"]
    hist_dep = [h for h in hist if same_obs(h)]
    if use_dep:
        w, dscore, parent = novelty(new, hist_dep, cfg)
    else:
        w, dscore, parent = 1.0, 0.0, None
    fp = field_parent(new, hist_dep, cfg) if (use_dep and parent is None and w < 0.5) else None
    kind = "exact_duplicate" if dscore >= cfg["exact_dep"] else "similar_photo" if parent is not None else "same_plot" if w < 0.5 else "new"
    new = dict(new, w=w)
    out = dict(novelty=w, dep_score=dscore, dep_parent=parent, field_parent=(fp["id"] if fp is not None else None), repeat_kind=kind,
               candidate=None, fusion=None, action="none", cost_action=None, costs=None, override=None, context=[])
    top = int(np.argmax(new["probs"]))
    if cls.healthy[top]:
        out["context"] = healthy_context(new, history, cls, top, now, rel, cfg)
        return out
    if float(new["probs"][top]) < cfg["cand_min_p"]:
        return out
    out["candidate"] = dict(idx=top, crop=cls.crop[top], disease=cls.dis[top])
    f = fuse(cls, top, now, (new["lat"], new["lon"]), list(history) + [new], rel, cfg, use_unc, use_rel)
    out["fusion"] = f
    out["action"], out["cost_action"], out["costs"], out["override"] = decide_full(f, cfg)
    if kind == "exact_duplicate":
        out["action"] = "repeat"                          # same photo again: nothing new to decide, no request, no alert
    return out


# ------------------------------------------------------------------ 4. photo-quality gate
QUALITY_MESSAGES = {
    "blurry": "The image is blurry.",
    "too_dark": "The photo is too dark.",
    "too_bright": "The photo is over-exposed (too bright).",
    "low_contrast": "The photo has almost no contrast (flat, foggy or covered lens).",
    "crop_uncertain": "The crop is not identifiable with enough confidence.",
    "crop_ambiguous": "The model cannot tell which of two crops this is.",
    "high_uncertainty": "The prediction is highly uncertain.",
}


def crop_confidence(probs, cls):
    """Probability mass per crop (sum over that crop's classes) -> (best crop, its mass, margin to the runner-up)."""
    mass = {}
    for i, c in enumerate(cls.crop):
        mass[c] = mass.get(c, 0.0) + float(probs[i])
    ranked = sorted(mass.items(), key=lambda kv: kv[1], reverse=True)
    conf = ranked[0][1] if ranked else 0.0
    return (ranked[0][0] if ranked else None), conf, conf - (ranked[1][1] if len(ranked) > 1 else 0.0)


def assess_quality(probs, u, imgq, cls, cfg=CFG, blur_min=None):
    """Decide whether a photo may become evidence.  Uses BOTH the pixels (sharpness, exposure, contrast) and what the model says
    (crop mass/margin, uncertainty), so a blurred photo cannot slip through just because the model happens to be confident, and a
    clear photo is not rejected just because one measurement is borderline.
        imgq = dict(blur, brightness, contrast)   (quality.image_stats)       blur_min = calibrated sharpness floor
    Returns dict(status 'ok'|'retake', reasons [codes], messages [text], soft_focus bool, crop, crop_conf, crop_margin)."""
    floor = cfg["blur_min_default"] if blur_min is None else float(blur_min)
    crop, conf, margin = crop_confidence(probs, cls)
    reasons, soft = [], False
    sharp = imgq["blur"]
    if sharp < floor * cfg["blur_soft_factor"]:
        reasons.append("blurry")
    elif sharp < floor:                                   # borderline: only acceptable if the model is clearly sure about the crop
        soft = True
        if not (conf >= cfg["soft_ok_crop_conf"] and u < cfg["soft_ok_u"]):
            reasons.append("blurry")
    if imgq["brightness"] < cfg["dark_mean"]:
        reasons.append("too_dark")
    elif imgq["brightness"] > cfg["bright_mean"]:
        reasons.append("too_bright")
    if imgq["contrast"] < cfg["min_contrast"]:
        reasons.append("low_contrast")
    if conf < cfg["crop_min_conf"]:
        reasons.append("crop_uncertain")
    elif margin < cfg["crop_min_margin"]:
        reasons.append("crop_ambiguous")
    if u >= cfg["u_reject"]:
        reasons.append("high_uncertainty")
    return dict(status="retake" if reasons else "ok", reasons=reasons, messages=[QUALITY_MESSAGES[r] for r in reasons],
                soft_focus=bool(soft and "blurry" not in reasons), crop=crop, crop_conf=conf, crop_margin=margin)


def summarise_prediction(probs, u, cls, cfg=CFG):
    """Plain verdict for an ACCEPTED photo: 'healthy' only if the model is actually sure, otherwise 'inconclusive'."""
    top = int(np.argmax(probs)); p = float(probs[top])
    if cls.healthy[top]:
        return "healthy" if (p >= cfg["healthy_min_p"] and u < cfg["healthy_max_u"]) else "inconclusive"
    return "disease_candidate" if p >= cfg["cand_min_p"] else "inconclusive"


def find_active(outbreaks, crop, disease, centroid, now, cfg=CFG):
    for o in outbreaks:
        if o["crop"] == crop and o["disease"] == disease and (o.get("status") == "pending" or now - o["ts"] < cfg["cooldown_s"]) \
                and haversine_m(o["lat"], o["lon"], centroid[0], centroid[1]) <= cfg["alert_radius_m"]:
            return o
    return None


class Pipeline:
    """In-memory version of the whole system (used by simulate.py and tests)."""

    def __init__(self, cls, cfg=CFG, use_dep=True, use_unc=True, use_rel=True):
        self.cls, self.cfg, self.flags = cls, cfg, (use_dep, use_unc, use_rel)
        self.hist, self.outbreaks, self.alerts, self.verify_log = [], [], [], []
        self.rel_ab = {}

    def rel(self, fid):
        a, b = self.rel_ab.get(fid, (3, 2))
        return a / (a + b)

    def step(self, r):
        res = evaluate(r, self.hist, self.cls, self.rel, cfg=self.cfg, use_dep=self.flags[0],
                       use_unc=self.flags[1], use_rel=self.flags[2])
        r = dict(r, w=res["novelty"], id=len(self.hist) + 1)
        self.hist.append(r)
        c = res["candidate"]
        if res["action"] == "alert":
            cen = res["fusion"]["centroid"]
            if find_active(self.outbreaks, c["crop"], c["disease"], cen, r["ts"], self.cfg) is None:
                o = dict(crop=c["crop"], disease=c["disease"], lat=cen[0], lon=cen[1], ts=r["ts"], P=res["fusion"]["P"])
                self.outbreaks.append(o); self.alerts.append(o)
        elif res["action"] == "verify":
            self.verify_log.append((r["ts"], c["crop"], c["disease"]))
        return res
