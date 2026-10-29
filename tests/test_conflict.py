"""PROBLEM 3: conflicting farmer reports. Shahin = diseased tomato, Sana = healthy tomato. Verification requests, counts and points must be right."""
from helpers import *


def _scenario(north_m=800):
    A = fresh_app(); shahin = add_farmer(A, "Shahin"); sana = add_farmer(A, "Sana", north_m=north_m)
    r, notes = report(A, shahin, "Tomato - Late Blight")
    return A, shahin, sana, r, notes


def test_request_reaches_the_neighbour_and_is_stored():
    A, shahin, sana, r, notes = _scenario()
    o = r["outbreak"]; assert o["action"] == "verify" and o["verify_status"] == "requested" and o["requests_created"] == 1
    assert [n[0] for n in notes] == [sana] and notes[0][1]["type"] == "verify"
    req = one(A, "SELECT * FROM requests"); assert req["farmer_id"] == sana and req["status"] == "pending" and req["report_id"] == r["report_id"]
    assert [x["id"] for x in A.my_requests(sana)] == [req["id"]] and A.my_requests(shahin) == []


def test_neighbour_3km_away_is_still_asked_via_wider_radius():
    A, shahin, sana, r, notes = _scenario(north_m=3000)                    # the Oct 27 database had Sana ~3 km away: no request was possible
    o = r["outbreak"]; assert o["verify_status"] == "requested" and o["verify_radius_km"] == 5.0 and o["requests_created"] == 1


def test_nobody_within_5km_means_no_request_and_the_admin_sees_it():
    A, shahin, sana, r, notes = _scenario(north_m=7000)
    assert r["outbreak"]["verify_status"] == "no_eligible_farmers" and count(A, "requests") == 0
    with A.db.tx() as c: w = A.verify_watchlist(c, time.time())
    assert len(w) == 1 and w[0]["requests_sent"] == 0 and w[0]["verify_status"] == "no_eligible_farmers"


def test_healthy_answer_via_photograph_now_button_closes_request_and_pays():
    A, shahin, sana, r, notes = _scenario(); qid = notes[0][1]["request_id"]
    r2, _ = report(A, sana, "Tomato - Healthy", p=0.96, request_id=qid)
    assert r2["request_fulfilled"] is True and r2["request_answer"] == "contradicts" and r2["points_earned"] == 20     # 5 new photo + 15 verification
    row = one(A, "SELECT * FROM requests WHERE id=?", qid)
    assert row["status"] == "done" and row["answer"] == "contradicts" and row["answered_report_id"] == r2["report_id"] and row["answered_ts"]
    assert farmer(A, sana)["points"] == 20 and A.my_requests(sana) == []
    assert r2["outbreak"]["action"] == "none" and r2["outbreak"]["context"], "healthy report shows no comparison with the neighbour's disease report"
    assert r2["outbreak"]["context"][0]["conflict"] > 0


def test_healthy_answer_from_the_normal_report_tab_also_closes_request():
    """Sana does not press 'Photograph now'; she just uploads in the Report tab. The request must still be answered (it used to stay pending forever)."""
    A, shahin, sana, r, notes = _scenario()
    r2, _ = report(A, sana, "Tomato - Healthy", p=0.96)                     # no request_id
    assert r2["request_fulfilled"] is True and r2["points_earned"] == 20
    assert one(A, "SELECT status, answer FROM requests") == {"status": "done", "answer": "contradicts"}


def test_admin_counts_match_the_database_and_show_the_conflict():
    A, shahin, sana, r, notes = _scenario(); report(A, sana, "Tomato - Healthy", p=0.96)
    with A.db.tx() as c: w = A.verify_watchlist(c, time.time())
    assert len(w) == 1; v = w[0]
    assert (v["requests_sent"], v["requests_done"], v["requests_pending"], v["answers"]) == (1, 1, 0, {"contradicts": 1})
    assert v["other_farmers"] == dict(confirming=0, healthy=1, other=0, farmers=1)
    assert v["now"]["conflict"] > 0 and v["now"]["P"] < v["at_report"]["P"], "healthy neighbour did not lower the outbreak probability"
    s = A.stats(); assert (s["verify_requests"], s["verify_answered"], s["verify_pending"]) == (1, 1, 0) and s["verify_response_rate"] == 1.0
    assert s["alerts_sent"] == 0 and s["open_outbreaks"] == 0                           # still VERIFY: nobody alerted


def test_confirming_answer_is_recorded_as_confirms():
    A, shahin, sana, r, notes = _scenario(); r2, _ = report(A, sana, "Tomato - Late Blight")
    assert r2["request_answer"] == "confirms" and one(A, "SELECT answer FROM requests")["answer"] == "confirms"


def test_wrong_crop_or_inconclusive_photo_does_not_answer():
    A, shahin, sana, r, notes = _scenario(); qid = notes[0][1]["request_id"]
    r2, _ = report(A, sana, "Potato - Healthy", p=0.96, request_id=qid)
    assert not r2["request_fulfilled"] and r2["request_not_answered_reason"] == "wrong_crop" and one(A, "SELECT status FROM requests")["status"] == "pending"
    assert r2["points_earned"] == 5                                                        # still a genuine new photo
    r3, _ = report(A, sana, "Tomato - Healthy", p=0.45, u=0.6, request_id=qid)              # model not sure it is healthy
    assert not r3["request_fulfilled"] and r3["request_not_answered_reason"] == "inconclusive_photo"


def test_only_the_invited_farmer_can_answer_and_only_once():
    A, shahin, sana, r, notes = _scenario(); qid = notes[0][1]["request_id"]; other = add_farmer(A, "Other", north_m=900)
    r2, _ = report(A, other, "Tomato - Healthy", p=0.96, request_id=qid)
    assert not r2["request_fulfilled"] and one(A, "SELECT status FROM requests")["status"] == "pending", "a stranger answered somebody else's request"
    report(A, sana, "Tomato - Healthy", p=0.96, request_id=qid)
    r4, _ = report(A, sana, "Tomato - Healthy", p=0.96, request_id=qid)                      # second photo, request already done
    assert not r4["request_fulfilled"] and r4["points_earned"] <= 5
    assert farmer(A, sana)["points"] <= 20 + 5, "verification bonus was paid twice"
