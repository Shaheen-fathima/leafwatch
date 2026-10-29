"""PROBLEM 5: only an admin-approved, valid outbreak may alert farmers, only once, only inside 5 km."""
import json
import pytest
from helpers import *


def _pending(A):
    ids, outs = three_sick_neighbours(A); oid = one(A, "SELECT id FROM outbreaks")["id"]; return ids, oid


def test_pending_outbreak_is_invisible_to_farmers_and_the_public():
    A = fresh_app(); ids, oid = _pending(A); bystander = add_farmer(A, "Near", north_m=1000)
    assert count(A, "alerts") == 0 and A.alerts(bystander) == [] and A.outbreaks() == []
    assert A.stats()["alerts_sent"] == 0 and A.stats()["farmers_alerted"] == 0 and A.stats()["pending_approval"] == 1


def test_more_reports_while_pending_still_alert_nobody_and_do_not_duplicate_the_outbreak():
    A = fresh_app(); ids, oid = _pending(A)
    for i in range(3):
        o = report(A, add_farmer(A, f"x{i}", north_m=50 * i), "Tomato - Late Blight")[0]["outbreak"]
        assert o["alert_status"] == "pending_admin" and o["alerts_created"] == 0
    assert count(A, "outbreaks") == 1 and count(A, "alerts") == 0


def test_admin_approval_alerts_each_eligible_farmer_exactly_once():
    A = fresh_app(); ids, oid = _pending(A); near = add_farmer(A, "Near", north_m=2000)
    res, notes = A.review(oid, True)
    assert res["status"] == "confirmed" and res["alerts_created"] == 4 and res["alerts_total"] == 4     # 3 reporters + 1 neighbour, all <5 km
    assert sorted(n[0] for n in notes) == sorted(ids + [near]) and all(n[1]["type"] == "alert" for n in notes)
    assert len(A.alerts(near)) == 1 and [o["id"] for o in A.outbreaks()] == [oid]
    with pytest.raises(A.HTTPException) as e: A.review(oid, True)                                 # double click / second admin
    assert e.value.status_code == 409 and count(A, "alerts") == 4


def test_later_reports_never_resend_to_farmers_who_already_have_the_alert():
    A = fresh_app(); ids, oid = _pending(A); A.review(oid, True)
    notes_all = []
    for i in range(3): notes_all += report(A, add_farmer(A, f"y{i}", north_m=30 * i), "Tomato - Late Blight")[1]
    per = q(A, "SELECT farmer_id, COUNT(*) n FROM alerts GROUP BY farmer_id")
    assert all(r["n"] == 1 for r in per), per
    sent_to = [n[0] for n in notes_all if n[1]["type"] == "alert"]; assert len(sent_to) == len(set(sent_to)) and not set(sent_to) & set(ids)


def test_database_itself_refuses_a_duplicate_alert_row():
    A = fresh_app(); ids, oid = _pending(A); A.review(oid, True)
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        with A.db.tx() as c: c.execute("INSERT INTO alerts(farmer_id,outbreak_id,msg_en,msg_ta,dist_km,ts) VALUES(?,?,?,?,?,?)", (ids[0], oid, "x", "x", 0.1, time.time()))


def test_rejection_sends_nothing_and_blocks_resend():
    A = fresh_app(); ids, oid = _pending(A)
    res, notes = A.review(oid, False)
    assert res["status"] == "rejected" and res["alerts_created"] == 0 and count(A, "alerts") == 0 and notes == []
    assert A.outbreaks() == []
    for i in range(3):                                           # more sick reports inside the 12 h cooldown
        o, n = report(A, add_farmer(A, f"z{i}", north_m=40 * i), "Tomato - Late Blight")
        assert o["outbreak"]["alert_status"] in ("suppressed_rejected", "none") and not [x for x in n if x[1]["type"] == "alert"]
    assert count(A, "alerts") == 0 and count(A, "outbreaks") == 1
    with pytest.raises(A.HTTPException) as e: A.review(oid, True)
    assert e.value.status_code == 409 and count(A, "alerts") == 0


def test_rejection_after_approval_withdraws_alerts_on_farmer_pages():
    A = fresh_app(); A_ids, oid = _pending(A)
    # legacy path: an 'active' outbreak whose alerts were already sent (old v2 data) can still be rejected and is then withdrawn
    with A.db.tx() as c: c.execute("UPDATE outbreaks SET status='active' WHERE id=?", (oid,))
    with A.db.tx() as c: A.deliver_alerts(c, dict(c.execute("SELECT * FROM outbreaks WHERE id=?", (oid,)).fetchone()), time.time())
    assert count(A, "alerts") == 3
    res, notes = A.review(oid, False)
    assert {n[1]["type"] for n in notes} == {"alert_update"} and A.alerts(A_ids[0])[0]["status"] == "rejected" and A.outbreaks() == []


def test_approval_is_refused_when_evidence_has_fewer_than_two_independent_farmers():
    A = fresh_app(); f = add_farmer(A, "Solo"); r, _ = report(A, f, "Tomato - Late Blight")
    with A.db.tx() as c:
        oid = c.execute("INSERT INTO outbreaks(crop,disease,lat,lon,ts,P,n_eff,evidence,status) VALUES('Tomato','Late Blight',?,?,?,?,?,?,'pending')",
                        (BASE_LAT, BASE_LON, time.time(), 0.9, 1.0, json.dumps([r["report_id"]]))).lastrowid
    near = add_farmer(A, "Near", north_m=500)
    with pytest.raises(A.HTTPException) as e: A.review(oid, True)
    assert e.value.status_code == 409 and count(A, "alerts") == 0 and A.alerts(near) == []
    A.review(oid, False)                                          # rejecting is always allowed
    assert one(A, "SELECT status FROM outbreaks")["status"] == "rejected"


def test_admin_overview_flags_whether_approval_is_allowed():
    A = fresh_app(); ids, oid = _pending(A); ov = A.admin_overview(x_admin_key=ADMIN)
    o = ov["open_outbreaks"][0]; assert o["can_approve"] and o["independent_farmers"] == 3 and o["alerts_persisted"] == 0 and o["now"]["action"] == "alert"


def test_with_approval_switched_off_old_behaviour_is_still_available_for_demos():
    A = fresh_app(approval=False); ids, outs = three_sick_neighbours(A)
    assert outs[1]["outbreak"]["alert_status"] == "sent"                    # the 2nd independent farmer triggers it; the 3rd only extends it
    assert outs[2]["outbreak"]["alert_status"] == "extended" and outs[2]["outbreak"]["alerts_created"] == 0 and count(A, "alerts") == 3


def test_default_configuration_requires_admin_approval():
    """Nothing is set in the environment (what a normal run.bat start looks like): approval must be ON."""
    A = fresh_app(approval=None); assert A.REQUIRE_APPROVAL is True
    ids, outs = three_sick_neighbours(A); assert count(A, "alerts") == 0 and count(A, "outbreaks", "status='pending'") == 1


def test_alerts_of_a_pending_outbreak_are_never_shown_even_if_a_row_somehow_exists():
    """Defence in depth: even a hand-inserted / corrupted alert row for an unapproved outbreak is hidden from the farmer."""
    A = fresh_app(); ids, oid = _pending(A)
    with A.db.tx() as c: c.execute("INSERT INTO alerts(farmer_id,outbreak_id,msg_en,msg_ta,dist_km,ts) VALUES(?,?,?,?,?,?)", (ids[0], oid, "x", "x", 0.1, time.time()))
    assert A.alerts(ids[0]) == []
