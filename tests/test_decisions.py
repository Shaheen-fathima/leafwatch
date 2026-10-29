"""PROBLEM 4: ALERT / VERIFY / DEFER chosen from probability, independent farmers, conflict (and, in app, admin approval)."""
import pytest
import engine as E
from helpers import *


def test_single_confident_report_is_verify_not_alert():
    A = fresh_app(); f = add_farmer(A, "Solo"); o = report(A, f, "Tomato - Late Blight")[0]["outbreak"]
    assert o["action"] == "verify" and o["independent_farmers"] == 1 and count(A, "outbreaks") == 0 and count(A, "alerts") == 0


def test_one_independent_farmer_can_never_alert_even_with_many_reports():           # the Oct 27 Potato Late Blight case
    A = fresh_app(); f = add_farmer(A, "Solo")
    for _ in range(3): o = report(A, f, "Potato - Late Blight")[0]["outbreak"]
    assert o["action"] == "verify" and o["independent_farmers"] == 1 and count(A, "alerts") == 0 and count(A, "outbreaks") == 0


def test_rule_overrides_cost_when_fewer_than_two_independent_farmers():
    f1 = dict(P=0.95, n_src=1, conflict=0.0); f2 = dict(P=0.95, n_src=2, conflict=0.0)
    a1, ca1, _, note1 = E.decide_full(f1); a2, ca2, _, note2 = E.decide_full(f2)
    assert (ca1, a1) == ("alert", "verify") and "independent" in note1 and (ca2, a2, note2) == ("alert", "alert", None)


def test_three_independent_farmers_give_alert_but_only_a_pending_outbreak():
    A = fresh_app(); ids, outs = three_sick_neighbours(A)
    assert [o["outbreak"]["action"] for o in outs] == ["verify", "alert", "alert"]
    o = outs[-1]["outbreak"]; assert o["alert_status"] == "pending_admin" and o["outbreak_status"] == "pending" and o["alerts_created"] == 0
    assert count(A, "outbreaks", "status='pending'") == 1 and count(A, "alerts") == 0 and A.stats()["pending_approval"] == 1


def test_strong_conflict_blocks_alert_even_with_three_sick_farmers():             # engine rule added in v3
    A = fresh_app()
    hs = [add_farmer(A, f"h{i}", north_m=100 * i + 50) for i in range(3)]
    for f in hs: report(A, f, "Tomato - Healthy", p=0.96)
    ids = [add_farmer(A, f"s{i}", north_m=100 * i) for i in range(3)]
    last = [report(A, f, "Tomato - Late Blight")[0]["outbreak"] for f in ids][-1]
    assert last["conflict"] >= 0.5 and last["independent_farmers"] == 3 and last["action"] == "verify" and "conflict" in last["override"]
    assert count(A, "outbreaks") == 0 and count(A, "alerts") == 0


def test_defer_when_neighbours_say_healthy_and_the_sick_report_is_weak():
    A = fresh_app()
    for i in range(4): report(A, add_farmer(A, f"h{i}", north_m=80 * i), "Tomato - Healthy", p=0.97, u=0.05)
    s = add_farmer(A, "weak", north_m=40); o = report(A, s, "Tomato - Late Blight", p=0.40, u=0.5)[0]["outbreak"]
    assert o["action"] == "defer" and o["requests_created"] == 0 and count(A, "outbreaks") == 0 and count(A, "alerts") == 0 and count(A, "requests") == 0


def test_decide_is_monotonic_defer_verify_alert():
    assert [E.decide(p)[0] for p in (0.01, 0.3, 0.9)] == ["defer", "verify", "alert"]


def test_healthy_or_inconclusive_report_is_never_a_candidate():
    A = fresh_app(); f = add_farmer(A, "H"); o = report(A, f, "Tomato - Healthy", p=0.96)[0]["outbreak"]
    assert o["action"] == "none" and o["candidate"] is None and count(A, "requests") == 0 and count(A, "outbreaks") == 0
    g = add_farmer(A, "W", north_m=3000); o2 = report(A, g, "Tomato - Late Blight", p=0.2, u=0.6)[0]["outbreak"]
    assert o2["action"] == "none" and count(A, "outbreaks") == 0


def test_verify_cases_have_no_approve_button_data_and_cannot_be_approved():
    """A VERIFY candidate is not an outbreak row at all, so there is nothing to approve. No evidence rule is bypassed."""
    A = fresh_app(); f = add_farmer(A, "Solo"); report(A, f, "Tomato - Late Blight")
    ov = A.admin_overview(x_admin_key=ADMIN)
    assert ov["open_outbreaks"] == [] and len(ov["verifications"]) == 1
    with pytest.raises(A.HTTPException) as e: A.review(1, True)
    assert e.value.status_code == 404
