"""Read-only audit of a LeafWatch database.   python check_integrity.py [path\\to\\leafwatch.db]
Prints PASS/FAIL per rule and exits with code 1 if any rule fails. It never writes to the database."""
import sqlite3, sys, json
from pathlib import Path

RADIUS_KM = 5.0


def audit(c):
    """Return [(rule, ok, detail)]. `c` is an open sqlite3 connection (row_factory=Row)."""
    def q(sql, *a):
        try: return [dict(r) for r in c.execute(sql, a).fetchall()]
        except sqlite3.OperationalError:               # a column that only newer schemas have: the rule cannot apply to this old database
            return []
    n = lambda sql, *a: c.execute(sql, a).fetchone()[0]
    cols = lambda t: {r[1] for r in c.execute(f"PRAGMA table_info({t})")}
    out = []
    def rule(name, bad, detail=""):
        out.append((name, not bad, "" if not bad else f"{len(bad) if hasattr(bad, '__len__') else bad} violation(s) {detail} {str(bad)[:200]}"))
    rule("P5  no alert exists for a PENDING (unapproved) outbreak", q("SELECT a.id FROM alerts a JOIN outbreaks o ON o.id=a.outbreak_id WHERE o.status='pending'"))
    rule("P5  nothing was sent after an outbreak was REJECTED", q("SELECT a.id FROM alerts a JOIN outbreaks o ON o.id=a.outbreak_id WHERE o.status='rejected' AND o.reviewed_ts IS NOT NULL AND a.ts>o.reviewed_ts"))
    rule("P5  one alert per farmer per outbreak (no duplicate notifications)", q("SELECT farmer_id, outbreak_id, COUNT(*) n FROM alerts GROUP BY farmer_id, outbreak_id HAVING n>1"))
    rule("5km every stored alert is within 5 km of its outbreak", q("SELECT id, dist_km FROM alerts WHERE dist_km>?", RADIUS_KM + 1e-6))
    rule("P5  every alert points to an existing outbreak", q("SELECT a.id FROM alerts a LEFT JOIN outbreaks o ON o.id=a.outbreak_id WHERE o.id IS NULL"))
    bad = []
    for o in q("SELECT * FROM outbreaks WHERE status='confirmed'"):
        ids = [int(x) for x in json.loads(o["evidence"] or "[]") if str(x).isdigit()]
        k = n(f"SELECT COUNT(DISTINCT farmer_id) FROM reports WHERE id IN ({','.join('?' * len(ids)) or 'NULL'}) AND w>=0.5", *ids) if ids else 0
        if k < 2: bad.append((o["id"], k))
    rule("P4/5 every confirmed outbreak has >= 2 independent farmers in its evidence (old pre-v3 data may be listed)", bad)
    rc = cols("reports")
    rule("P4  no report chose ALERT with fewer than 2 independent farmers", q("SELECT id, n_src FROM reports WHERE action='alert' AND n_src IS NOT NULL AND n_src<2"))
    if "pts" in cols("reports"):
        answered = "AND NOT EXISTS (SELECT 1 FROM requests r WHERE r.answered_report_id=reports.id)" if "answered_report_id" in cols("requests") else ""
        rule("P2  repeats / down-weighted reports earned 0 points", q(f"SELECT id, pts FROM reports WHERE pts>0 AND (COALESCE(repeat_kind,'new')!='new' OR w<0.5) {answered}"))
        rule("P2  exact-duplicate photos have weight 0 and action 'repeat' or 'none'", q("SELECT id FROM reports WHERE repeat_kind='exact_duplicate' AND (w>0 OR action NOT IN ('repeat','none'))"))
        rule("P2  farmer points are at least the sum of their report points", q("SELECT f.id, f.points, s FROM farmers f JOIN (SELECT farmer_id, SUM(COALESCE(pts,0)) s FROM reports GROUP BY farmer_id) r ON r.farmer_id=f.id WHERE f.points < s"))
    rule("P3  an answered request points at the answering report", q("SELECT id FROM requests WHERE status='done' AND answered_report_id IS NULL AND answered_ts IS NOT NULL") if "answered_ts" in cols("requests") else [])
    if "answered_report_id" in cols("requests"):
        rule("P3  no request is still 'pending' although its farmer already answered it", q("SELECT q.id FROM requests q WHERE q.status='pending' AND EXISTS (SELECT 1 FROM reports r WHERE r.farmer_id=q.farmer_id AND r.ts>q.ts AND r.w>=0.5 AND r.crop=q.crop AND r.pts>=15)"))
        rule("P3  no request exists for a duplicate (repeat) report", q("SELECT q.id FROM requests q JOIN reports r ON r.id=q.report_id WHERE r.repeat_kind='exact_duplicate'"))
    rule("P3  every request points at an existing report (when set)", q("SELECT q.id FROM requests q LEFT JOIN reports r ON r.id=q.report_id WHERE q.report_id IS NOT NULL AND r.id IS NULL"))
    rule("integrity sqlite PRAGMA integrity_check", [] if c.execute("PRAGMA integrity_check").fetchone()[0] == "ok" else ["not ok"])
    return out


def main(path):
    c = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True); c.row_factory = sqlite3.Row
    res = audit(c); fails = [r for r in res if not r[1]]
    for name, ok, detail in res: print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "\n        " + detail))
    print(f"\n{len(res) - len(fails)} passed, {len(fails)} failed"); return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "leafwatch.db"))
