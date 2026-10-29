"""Verification candidate review, action buttons, conflict handling, deferral, and Shahin-Sana-Rubika test scenarios."""
import asyncio, time
import pytest
from helpers import *

SHAHIN_LOC = (13.0827, 80.2707)
SANA_LOC = (13.0890, 80.2780)    # ~1.06 km from Shahin
RUBIKA_LOC = (13.1060, 80.2820)  # ~2.87 km from Shahin
FAR_LOC = (13.1500, 80.3500)     # ~11.1 km from Shahin (outside 5 km radius)


def _setup_shahin_sana_rubika(A):
    shahin = add_farmer(A, "Shahin", north_m=0.0)
    # Put Sana and Rubika at their exact coordinates
    with A.db.tx() as c:
        c.execute("UPDATE farmers SET lat=?, lon=? WHERE id=?", (SHAHIN_LOC[0], SHAHIN_LOC[1], shahin))
        sana = c.execute("INSERT INTO farmers(name,lat,lon,created) VALUES(?,?,?,?)",
                         ("Sana", SANA_LOC[0], SANA_LOC[1], time.time())).lastrowid
        rubika = c.execute("INSERT INTO farmers(name,lat,lon,created) VALUES(?,?,?,?)",
                           ("Rubika", RUBIKA_LOC[0], RUBIKA_LOC[1], time.time())).lastrowid
        far = c.execute("INSERT INTO farmers(name,lat,lon,created) VALUES(?,?,?,?)",
                        ("FarFarmer", FAR_LOC[0], FAR_LOC[1], time.time())).lastrowid
    return shahin, sana, rubika, far


def test_shahin_sana_conflict_scenario_with_admin_rejection():
    """Shahin reports sick tomato, Sana reports healthy tomato -> conflict detected -> admin rejects as false alarm."""
    A = fresh_app()
    shahin, sana, rubika, far = _setup_shahin_sana_rubika(A)

    # 1. Shahin submits diseased Tomato report
    r1, notes1 = report(A, shahin, "Tomato - Late Blight")
    assert r1["outbreak"]["action"] == "verify"
    assert count(A, "requests", "farmer_id=?", sana) == 1

    # 2. Sana submits healthy Tomato report
    r2, _ = report(A, sana, "Tomato - Healthy", p=0.96)
    assert r2["request_fulfilled"] is True and r2["request_answer"] == "contradicts"

    # 3. Check admin overview: candidate should show conflict and action buttons
    ov = A.admin_overview(x_admin_key=ADMIN)
    assert ov["open_outbreaks"] == []
    assert len(ov["verifications"]) == 1
    v = ov["verifications"][0]
    assert v["verify_status"] == "conflict_detected"
    assert v["requests_sent"] == 1 and v["requests_done"] == 1 and v["requests_pending"] == 0
    assert v["answers"] == {"contradicts": 1}
    assert v["can_reject"] is True
    assert v["can_defer"] is True
    assert v["can_approve"] is False  # Cannot approve with only 1 diseased report and active contradiction!

    # 4. Attempting to confirm when ineligible must return 409
    with pytest.raises(A.HTTPException) as exc:
        A.review_verification(v["id"], "confirm")
    assert exc.value.status_code == 409

    # 5. Admin rejects the verification candidate as false alarm
    res, n = A.review_verification(v["id"], "reject", note="Field inspection confirmed healthy")
    assert res["status"] == "rejected"
    assert res["reliability_updates"] >= 1

    # 6. Verify database updates and reliability adjustment
    f_shahin = farmer(A, shahin)
    f_sana = farmer(A, sana)
    # Shahin was wrong (penalized b), Sana was right (rewarded a)
    assert f_shahin["b"] > 2.0
    assert f_sana["a"] > 3.0

    # 7. Candidate removed from active watchlist and stats updated
    st = A.stats()
    assert st["verify_candidates"] == 0
    ov2 = A.admin_overview(x_admin_key=ADMIN)
    assert len(ov2["verifications"]) == 0
    assert len(ov2["reviewed_verifications"]) == 1
    assert ov2["reviewed_verifications"][0]["status"] == "rejected"

    # 8. Duplicate decision rejected with 409
    with pytest.raises(A.HTTPException) as exc2:
        A.review_verification(v["id"], "reject")
    assert exc2.value.status_code == 409


def test_admin_defer_verification_candidate():
    """Admin defers an inconclusive verification candidate."""
    A = fresh_app()
    shahin, sana, rubika, far = _setup_shahin_sana_rubika(A)
    r1, _ = report(A, shahin, "Tomato - Late Blight")

    ov = A.admin_overview(x_admin_key=ADMIN)
    v = ov["verifications"][0]
    res, _ = A.review_verification(v["id"], "defer", note="Wait for more photos")
    assert res["status"] == "deferred"

    # Removed from active candidates
    assert len(A.verify_watchlist(A.db.tx().__enter__(), time.time())) == 0
    st = A.stats()
    assert st["verify_candidates"] == 0


def test_admin_reverify_candidate():
    """Admin requests additional verification from nearby farmers."""
    A = fresh_app()
    shahin, sana, rubika, far = _setup_shahin_sana_rubika(A)
    r1, _ = report(A, shahin, "Tomato - Late Blight")
    # Sana answers
    report(A, sana, "Tomato - Healthy", p=0.96)

    ov = A.admin_overview(x_admin_key=ADMIN)
    v = ov["verifications"][0]
    res, notes = A.review_verification(v["id"], "reverify")
    assert res["status"] == "reverified"
    assert res["requests_created"] >= 1  # Rubika is asked!


def test_outbreak_defer_action():
    """Admin defers an open outbreak candidate."""
    A = fresh_app()
    ids, outs = three_sick_neighbours(A)
    oid = one(A, "SELECT id FROM outbreaks WHERE status='pending'")["id"]

    res, _ = A.review(oid, False, action="defer")
    assert res["status"] == "deferred"
    assert one(A, "SELECT status FROM outbreaks WHERE id=?", oid)["status"] == "deferred"
    assert A.stats()["open_outbreaks"] == 0


def test_shahin_rubika_confirmation_alerts_sana_excludes_far():
    """Shahin & Rubika both report sick tomato -> ALERT -> approved -> alerts Sana (<5km), excludes FarFarmer (>5km)."""
    A = fresh_app()
    shahin, sana, rubika, far = _setup_shahin_sana_rubika(A)

    # Shahin reports sick
    r1, _ = report(A, shahin, "Tomato - Late Blight")
    # Rubika reports sick (independent confirmation)
    r2, _ = report(A, rubika, "Tomato - Late Blight")

    # Now we have 2 independent sick reports -> ALERT
    assert r2["outbreak"]["action"] == "alert"
    oid = r2["outbreak"]["outbreak_id"]
    assert oid is not None

    # Before approval, nobody has an alert
    assert count(A, "alerts") == 0
    assert A.alerts(sana) == []
    assert A.alerts(far) == []

    # Admin approves
    res, notes = A.review(oid, True)
    assert res["status"] == "confirmed"

    # Sana (~1.06 km) and Rubika (~2.87 km) are within 5 km of outbreak
    sana_alerts = A.alerts(sana)
    assert len(sana_alerts) == 1
    assert sana_alerts[0]["dist_km"] < 5.0
    assert 0.9 < sana_alerts[0]["dist_km"] < 2.0

    # FarFarmer (~11 km) is outside 5 km radius: MUST have 0 alerts
    far_alerts = A.alerts(far)
    assert len(far_alerts) == 0

    # Repeated approval or restart does not create duplicate alerts
    with pytest.raises(A.HTTPException):
        A.review(oid, True)
    assert count(A, "alerts", "farmer_id=?", sana) == 1
