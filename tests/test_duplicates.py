"""PROBLEM 2: the same evidence must never be counted twice, and a duplicate must never earn points."""
from helpers import *


def test_exact_duplicate_adds_no_evidence_no_points_no_action():
    A = fresh_app(); f = add_farmer(A, "Sana"); dh, emb = 0xABCDEF123456, unit(64)
    r1, _ = report(A, f, "Tomato - Late Blight", dhash=dh, emb=emb)
    pts1 = farmer(A, f)["points"]; assert r1["points_earned"] == 5 and r1["novelty"] == 1.0
    r2, _ = report(A, f, "Tomato - Late Blight", dhash=dh, emb=emb)
    assert r2["novelty"] == 0.0 and r2["repeat_kind"] == "exact_duplicate" and r2["points_earned"] == 0
    assert r2["outbreak"]["action"] == "repeat" and r2["outbreak"]["requests_created"] == 0 and r2["outbreak"]["alerts_created"] == 0
    assert farmer(A, f)["points"] == pts1, "duplicate changed the farmer's points"
    assert r2["outbreak"]["P"] <= r1["outbreak"]["P"] + 1e-9, "duplicate raised the outbreak probability"


def test_dashboard_counts_agree_after_a_repeat():
    A = fresh_app(); f = add_farmer(A, "Sana"); dh = 0x1234ABCD5678
    report(A, f, "Tomato - Late Blight", dhash=dh); report(A, f, "Tomato - Late Blight", dhash=dh)
    s = A.stats()
    assert (s["reports"], s["clusters"], s["duplicates_merged"], s["alerts_sent"]) == (2, 1, 1, 0), s       # = what the Admin screen showed on Oct 27


def test_same_photo_from_another_farmer_or_place_counts_zero_and_earns_nothing():
    A = fresh_app(); a = add_farmer(A, "A"); b = add_farmer(A, "B", north_m=2000); dh = 0x0F0F0F0F0F0F
    report(A, a, "Tomato - Late Blight", dhash=dh)
    r, _ = report(A, b, "Tomato - Late Blight", dhash=dh)
    assert r["novelty"] == 0.0 and r["points_earned"] == 0 and farmer(A, b)["points"] == 0
    assert r["outbreak"]["independent_farmers"] == 1, "a copy of someone else's photo was counted as an independent farmer"


def test_many_different_photos_of_one_plot_never_become_an_outbreak():
    A = fresh_app(); f = add_farmer(A, "Solo"); acts = []
    for _ in range(6): acts.append(report(A, f, "Tomato - Late Blight")[0]["outbreak"]["action"])
    assert "alert" not in acts and count(A, "outbreaks") == 0 and count(A, "alerts") == 0
    assert farmer(A, f)["points"] <= 5 + 5, "extra photos of the same plot kept earning points"      # first photo 5; later same-plot photos 0


def test_different_places_keep_full_weight_and_points():
    A = fresh_app(); a = add_farmer(A, "A"); b = add_farmer(A, "B", north_m=1200)
    ra, _ = report(A, a, "Tomato - Late Blight"); rb, _ = report(A, b, "Tomato - Late Blight")
    assert ra["novelty"] == 1.0 and rb["novelty"] > 0.9
    assert rb["points_earned"] == 20 and rb["request_answer"] == "confirms"       # 5 (new photo) + 15 (A's report had asked B to verify, B's photo answered it)


def test_duplicate_never_closes_a_verification_request_or_pays_the_bonus():
    A = fresh_app(); s = add_farmer(A, "Shahin"); n = add_farmer(A, "Sana", north_m=800); dh = 0xCAFEBABE1234
    report(A, s, "Tomato - Late Blight", dhash=dh)                                    # creates a request for Sana
    req = one(A, "SELECT id FROM requests WHERE farmer_id=?", n)["id"]
    r, _ = report(A, n, "Tomato - Late Blight", dhash=dh, request_id=req)             # Sana re-uploads Shahin's file
    assert r["request_fulfilled"] is False and r["points_earned"] == 0 and r["request_not_answered_reason"] == "not_new_evidence"
    assert one(A, "SELECT status FROM requests WHERE id=?", req)["status"] == "pending"
