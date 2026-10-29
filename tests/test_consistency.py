"""Database consistency: after a rich mixed scenario every audit rule of check_integrity.py must hold, and the audit must catch tampering."""
import sqlite3
import check_integrity as CI
from helpers import *


def _audit(A):
    c = sqlite3.connect(str(A.db.DB_PATH)); c.row_factory = sqlite3.Row
    try: return CI.audit(c)
    finally: c.close()


def _busy_scenario():
    A = fresh_app(); s = add_farmer(A, "Shahin"); n = add_farmer(A, "Sana", north_m=900); dh = 0xBEEFBEEF1234
    r, notes = report(A, s, "Tomato - Late Blight", dhash=dh); report(A, s, "Tomato - Late Blight", dhash=dh)          # duplicate
    report(A, n, "Tomato - Healthy", p=0.96)                                                                         # conflicting answer
    ids = [add_farmer(A, f"t{i}", north_m=2000 + 20 * i) for i in range(3)]
    for f in ids: report(A, f, "Tomato - Late Blight")
    for oid in [r_["id"] for r_ in q(A, "SELECT id FROM outbreaks WHERE status='pending'")]: A.review(oid, True)
    report(A, add_farmer(A, "late", north_m=2100), "Tomato - Late Blight")
    return A


def test_audit_passes_after_a_busy_mixed_scenario():
    A = _busy_scenario(); res = _audit(A); bad = [r for r in res if not r[1]]
    assert not bad, bad; assert len(res) >= 12 and count(A, "alerts") > 0


def test_audit_catches_tampering():
    A = _busy_scenario()
    with A.db.tx() as c:
        c.execute("INSERT INTO outbreaks(crop,disease,lat,lon,ts,P,n_eff,evidence,status) VALUES('Tomato','Late Blight',13,80,?,0.9,1,'[]','pending')", (time.time(),))
        oid = c.execute("SELECT MAX(id) FROM outbreaks").fetchone()[0]
        c.execute("INSERT INTO alerts(farmer_id,outbreak_id,msg_en,msg_ta,dist_km,ts) VALUES(1,?,?,?,?,?)", (oid, "x", "x", 9.0, time.time()))
        c.execute("UPDATE reports SET pts=9 WHERE repeat_kind='exact_duplicate'")
    failed = [r[0] for r in _audit(A) if not r[1]]
    for prefix in ("P5", "5km", "P2"): assert any(n.startswith(prefix) for n in failed), (prefix, failed)


def test_all_counts_shown_on_the_admin_dashboard_agree_with_the_tables():
    A = _busy_scenario(); s = A.stats()
    assert s["reports"] == count(A, "reports") and s["alerts_sent"] == count(A, "alerts") and s["farmers"] == count(A, "farmers")
    assert s["farmers_alerted"] == one(A, "SELECT COUNT(DISTINCT farmer_id) n FROM alerts")["n"]
    assert s["confirmed"] == count(A, "outbreaks", "status='confirmed'") and s["verify_requests"] == count(A, "requests")
    ov = A.admin_overview(x_admin_key=ADMIN)
    for o in ov["reviewed_outbreaks"] + ov["open_outbreaks"]: assert o["alerts_persisted"] == count(A, "alerts", "outbreak_id=?", o["id"])
