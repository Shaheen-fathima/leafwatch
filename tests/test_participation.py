"""PROBLEM 6 (software part only): requests are shown, answers pay points, unanswered requests get ONE reminder, response rate is measurable.
Whether real farmers take part is NOT something code can prove: see README 'Problem 6' and USER_TEST_PLAN.md."""
from helpers import *


def _asked():
    A = fresh_app(); s = add_farmer(A, "Shahin"); n = add_farmer(A, "Sana", north_m=900); report(A, s, "Tomato - Late Blight")
    return A, s, n, one(A, "SELECT * FROM requests")


def _age(A, qid, hours):
    with A.db.tx() as c: c.execute("UPDATE requests SET ts=? WHERE id=?", (time.time() - hours * 3600, qid))


def test_request_is_displayed_with_age_and_reminder_flag():
    A, s, n, req = _asked(); v = A.my_requests(n)[0]; assert v["id"] == req["id"] and v["reminder"] is False and v["age_h"] < 0.1
    _age(A, req["id"], 4); v = A.my_requests(n)[0]; assert v["reminder"] is True and v["age_h"] >= 4


def test_exactly_one_reminder_per_unanswered_request():
    A, s, n, req = _asked(); _age(A, req["id"], 4)
    with A.db.tx() as c: first = A.due_reminders(c, time.time())
    with A.db.tx() as c: second = A.due_reminders(c, time.time())
    assert [(f, p["type"], p["request_id"]) for f, p in first] == [(n, "reminder", req["id"])] and second == []


def test_no_reminder_for_fresh_answered_or_expired_requests():
    A, s, n, req = _asked()
    with A.db.tx() as c: assert A.due_reminders(c, time.time()) == []                       # too young
    _age(A, req["id"], 30)
    with A.db.tx() as c: assert A.due_reminders(c, time.time()) == []                       # expired (>24 h)
    assert A.my_requests(n) == []
    A2, s2, n2, req2 = _asked(); _age(A2, req2["id"], 4); report(A2, n2, "Tomato - Healthy", p=0.96)
    with A2.db.tx() as c: assert A2.due_reminders(c, time.time()) == []                     # answered


def test_points_and_badge_progress_for_helping():
    A, s, n, req = _asked(); report(A, n, "Tomato - Healthy", p=0.96)
    f = farmer(A, n); assert f["points"] == 20 and A.farmer_dict(f)["badge"] == "Seedling"


def test_response_rate_and_time_to_answer_are_measured_for_user_testing():
    A, s, n, req = _asked(); assert A.stats()["verify_response_rate"] == 0.0 and A.stats()["verify_median_answer_min"] is None
    _age(A, req["id"], 1); report(A, n, "Tomato - Healthy", p=0.96); st = A.stats()
    assert st["verify_response_rate"] == 1.0 and 59 <= st["verify_median_answer_min"] <= 61


def test_expired_requests_are_counted_not_hidden():
    A, s, n, req = _asked(); _age(A, req["id"], 30); st = A.stats()
    assert st["verify_expired"] == 1 and st["verify_pending"] == 0 and st["verify_response_rate"] == 0.0
