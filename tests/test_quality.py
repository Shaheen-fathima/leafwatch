"""PROBLEM 1: unclear / blurry / wrong photos must never become evidence, points, requests, outbreaks or alerts."""
import os
import engine as E
from quality import image_stats
from PIL import Image
import io
from helpers import *


def _imgq(kind):
    return image_stats(Image.open(io.BytesIO(jpeg(kind))).convert("RGB"))


def test_pixel_stats_separate_sharp_from_blurry():
    assert _imgq("sharp")["blur"] > 120.6 * 5 and _imgq("blurry")["blur"] < 120.6 * 0.5


def test_blurry_photo_is_rejected_and_changes_nothing():
    A = fresh_app(); f = add_farmer(A, "Shahin"); A.PRED = StubPredictor("Tomato - Late Blight")
    before = snapshot(A); files_before = set(os.listdir(A.UPLOADS))
    r = http_report(A, f, jpeg("blurry"))
    assert r["retake_required"] is True and r["status"] == "rejected" and "blurry" in r["reasons"]
    assert r["evidence_added"] is False and r["points_earned"] == 0
    assert snapshot(A) == before, "a rejected photo changed the database"
    assert set(os.listdir(A.UPLOADS)) == files_before, "a rejected photo was kept on disk"


def test_each_bad_photo_kind_is_rejected_without_side_effects():
    A = fresh_app(); f = add_farmer(A, "Shahin"); A.PRED = StubPredictor("Tomato - Late Blight")
    before = snapshot(A)
    for kind, code in (("dark", "too_dark"), ("bright", "too_bright"), ("flat", "low_contrast")):
        r = http_report(A, f, jpeg(kind)); assert r["retake_required"] and code in r["reasons"], (kind, r["reasons"])
    assert snapshot(A) == before


def test_uncertain_crop_is_rejected():
    A = fresh_app(); f = add_farmer(A, "Shahin"); A.PRED = StubPredictor("Tomato - Late Blight", p=0.22, u=0.9)
    before = snapshot(A); r = http_report(A, f, jpeg("sharp"))
    assert r["retake_required"] and ("high_uncertainty" in r["reasons"] or "crop_uncertain" in r["reasons"]); assert snapshot(A) == before


def test_clear_photo_is_accepted_and_saved():
    A = fresh_app(); f = add_farmer(A, "Shahin"); A.PRED = StubPredictor("Tomato - Late Blight")
    r = http_report(A, f, jpeg("sharp"))
    assert r["retake_required"] is False and r["status"] == "accepted" and r["points_earned"] == 5
    assert count(A, "reports") == 1 and len(os.listdir(A.UPLOADS)) == 1


def test_rejected_photo_never_triggers_alert_even_after_good_reports():
    A = fresh_app(); ids, _ = three_sick_neighbours(A)           # a pending outbreak exists
    A.PRED = StubPredictor("Tomato - Late Blight"); extra = add_farmer(A, "Extra")
    before = snapshot(A); r = http_report(A, extra, jpeg("blurry"))
    assert r["retake_required"] and snapshot(A) == before


def test_soft_but_sure_photo_is_accepted_with_warning_only():
    imgq = dict(blur=100.0, brightness=120.0, contrast=40.0)                 # between 50% and 100% of the floor 120.6
    A = fresh_app(); probs = pred("Tomato - Late Blight", p=0.97, u=0.05)["probs"]
    ok = E.assess_quality(probs, 0.05, imgq, A.CLS, A.CFG, 120.6); assert ok["status"] == "ok" and ok["soft_focus"]
    bad = E.assess_quality(pred("Tomato - Late Blight", p=0.5)["probs"], 0.45, imgq, A.CLS, A.CFG, 120.6); assert bad["status"] == "retake"
