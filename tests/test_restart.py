"""Server restart / upgrade behaviour: nothing lost, nothing re-sent, old databases upgraded safely (with a backup)."""
import os, sqlite3
import pytest
from helpers import *

OLD_SCHEMA = """
CREATE TABLE farmers(id INTEGER PRIMARY KEY, name TEXT, lat REAL, lon REAL, a REAL DEFAULT 3, b REAL DEFAULT 2, points INTEGER DEFAULT 0, created REAL);
CREATE TABLE reports(id INTEGER PRIMARY KEY, farmer_id INTEGER, lat REAL, lon REAL, ts REAL, crop TEXT, top_class TEXT, top_p REAL,
  u REAL, probs TEXT, emb BLOB, dhash TEXT, w REAL, dep_score REAL, cluster_id INTEGER, image TEXT, action TEXT, post_p REAL);
CREATE TABLE outbreaks(id INTEGER PRIMARY KEY, crop TEXT, disease TEXT, lat REAL, lon REAL, ts REAL, P REAL, n_eff REAL, status TEXT DEFAULT 'active', evidence TEXT, alerted INTEGER DEFAULT 0);
CREATE TABLE alerts(id INTEGER PRIMARY KEY, farmer_id INTEGER, outbreak_id INTEGER, msg_en TEXT, msg_ta TEXT, dist_km REAL, ts REAL, read INTEGER DEFAULT 0);
CREATE TABLE requests(id INTEGER PRIMARY KEY, farmer_id INTEGER, crop TEXT, disease TEXT, lat REAL, lon REAL, ts REAL, status TEXT DEFAULT 'pending');
"""


def test_restart_keeps_everything_and_resends_nothing():
    A = fresh_app(); ids, outs = three_sick_neighbours(A); oid = one(A, "SELECT id FROM outbreaks")["id"]
    snap = snapshot(A); path, up = A.db.DB_PATH, A.UPLOADS; backups = set(os.listdir(path.parent / "backups")) if (path.parent / "backups").exists() else set()
    B = fresh_app(db_path=path, uploads=up)                                    # "restart"
    assert snapshot(B) == snap and one(B, "SELECT status FROM outbreaks")["status"] == "pending" and count(B, "alerts") == 0
    assert (set(os.listdir(path.parent / "backups")) if (path.parent / "backups").exists() else set()) == backups, "an up-to-date database was backed up again"
    B.review(oid, True); n_alerts = count(B, "alerts"); assert n_alerts == 3
    C = fresh_app(db_path=path, uploads=up)                                    # second restart after approval
    assert count(C, "alerts") == n_alerts and len(C.alerts(ids[0])) == 1 and one(C, "SELECT status FROM outbreaks")["status"] == "confirmed"
    with pytest.raises(C.HTTPException): C.review(oid, True)
    assert count(C, "alerts") == n_alerts


def test_websocket_hub_is_empty_after_restart_but_alerts_stay_readable_through_the_api():
    A = fresh_app(); ids, outs = three_sick_neighbours(A); A.review(one(A, "SELECT id FROM outbreaks")["id"], True)
    B = fresh_app(db_path=A.db.DB_PATH, uploads=A.UPLOADS)
    assert B.hub.conns == {} and [a["status"] for a in B.alerts(ids[0])] == ["confirmed"]       # the page re-reads /alerts when its socket reconnects


def test_pending_verification_requests_survive_a_restart():
    A = fresh_app(); s = add_farmer(A, "S"); n = add_farmer(A, "N", north_m=700); report(A, s, "Tomato - Late Blight")
    B = fresh_app(db_path=A.db.DB_PATH, uploads=A.UPLOADS); assert len(B.my_requests(n)) == 1
    r, _ = report(B, n, "Tomato - Healthy", p=0.96); assert r["request_fulfilled"]


def test_old_v2_database_is_upgraded_in_place_with_a_backup_and_no_data_loss():
    tmp = tempfile.mkdtemp(); path = os.path.join(tmp, "old.db"); c = sqlite3.connect(path); c.executescript(OLD_SCHEMA)
    c.execute("INSERT INTO farmers VALUES(1,'Shahin',13.0827,80.2707,3,2,5,1.0)"); c.execute("INSERT INTO requests(id,farmer_id,crop,disease,lat,lon,ts,status) VALUES(7,1,'Tomato','Late Blight',13,80,1.0,'pending')")
    c.execute("PRAGMA user_version=2"); c.commit(); c.close()
    A = fresh_app(db_path=path, uploads=os.path.join(tmp, "up"))
    assert one(A, "PRAGMA user_version")["user_version"] == 3
    assert one(A, "SELECT name, points FROM farmers WHERE id=1") == {"name": "Shahin", "points": 5}
    assert one(A, "SELECT id, status, answer, answered_ts, reminded_ts FROM requests") == {"id": 7, "status": "pending", "answer": None, "answered_ts": None, "reminded_ts": None}
    assert len(os.listdir(os.path.join(tmp, "backups"))) == 1
    fresh_app(db_path=path, uploads=os.path.join(tmp, "up")); assert len(os.listdir(os.path.join(tmp, "backups"))) == 1      # idempotent


def test_audit_script_also_works_on_an_old_v2_database():
    import check_integrity as CI
    tmp = tempfile.mkdtemp(); path = os.path.join(tmp, "old.db"); c = sqlite3.connect(path); c.executescript(OLD_SCHEMA); c.commit(); c.close()
    c = sqlite3.connect(path); c.row_factory = sqlite3.Row
    res = CI.audit(c); c.close(); assert res and all(ok for _, ok, _ in res), [r for r in res if not r[1]]
