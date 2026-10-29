"""HTTP / WebSocket layer through FastAPI's real TestClient. Needs `pip install -r requirements.txt` (fastapi + httpx).
All logic is already covered function-by-function in the other test files; these tests check the wiring: routes, status codes, headers, websocket push.
NOTE FOR THE REVIEWER: these tests were written without being able to install FastAPI in the authoring sandbox, so they have NOT been executed there."""
import io, time
import pytest
tc = pytest.importorskip("fastapi.testclient")
from helpers import *


def _client(approval=True):
    A = fresh_app(approval=approval); A.PRED = StubPredictor("Tomato - Late Blight"); return A, tc.TestClient(A.app)


def _post_photo(cli, fid, kind="sharp"):
    return cli.post("/api/report", data=dict(farmer_id=fid, lat=BASE_LAT, lon=BASE_LON), files=dict(image=("leaf.jpg", io.BytesIO(jpeg(kind)), "image/jpeg")))


def test_http_blurry_photo_is_rejected_and_sharp_is_accepted():
    A, cli = _client(); f = cli.post("/api/farmers", json=dict(name="Shahin", lat=BASE_LAT, lon=BASE_LON)).json()["id"]
    r = _post_photo(cli, f, "blurry"); assert r.status_code == 200 and r.json()["retake_required"] is True and cli.get("/api/stats").json()["reports"] == 0
    r = _post_photo(cli, f, "sharp"); assert r.status_code == 200 and r.json()["status"] == "accepted" and cli.get("/api/stats").json()["reports"] == 1


def test_http_admin_routes_need_the_key():
    A, cli = _client()
    assert cli.get("/api/admin/overview").status_code == 401
    assert cli.get("/api/admin/overview", headers={"X-Admin-Key": "wrong"}).status_code == 401
    assert cli.get("/api/admin/overview", headers={"X-Admin-Key": ADMIN}).status_code == 200
    assert cli.post("/api/admin/auth", json=dict(key="wrong")).status_code == 401 and cli.post("/api/admin/auth", json=dict(key=ADMIN)).status_code == 200
    assert cli.post("/api/admin/outbreaks/1/verify", json=dict(confirmed=True)).status_code == 401


def test_http_admin_disabled_without_configured_key():
    A = fresh_app(admin_key=None); cli = tc.TestClient(A.app)
    assert cli.post("/api/admin/auth", json=dict(key="admin123")).status_code == 503


def test_http_alert_reaches_farmer_inside_5km_over_websocket_only_after_approval():
    A, cli = _client(); mk = lambda n, d: cli.post("/api/farmers", json=dict(name=n, lat=BASE_LAT + dlat(d), lon=BASE_LON)).json()["id"]
    reporters = [mk(f"R{i}", 0) for i in range(3)]; near, far = mk("near", 3000), mk("far", 8000)
    with cli.websocket_connect(f"/ws/{near}") as wn, cli.websocket_connect(f"/ws/{far}") as wf:
        for f in reporters: _post_photo(cli, f)
        assert cli.get(f"/api/farmers/{near}/alerts").json() == []                                   # pending: nothing yet
        oid = cli.get("/api/admin/overview", headers={"X-Admin-Key": ADMIN}).json()["open_outbreaks"][0]["id"]
        r = cli.post(f"/api/admin/outbreaks/{oid}/verify", json=dict(confirmed=True), headers={"X-Admin-Key": ADMIN}); assert r.status_code == 200
        msg = wn.receive_json(); assert msg["type"] == "alert" and msg["dist_km"] < 5
    assert len(cli.get(f"/api/farmers/{near}/alerts").json()) == 1 and cli.get(f"/api/farmers/{far}/alerts").json() == []
    assert cli.post(f"/api/admin/outbreaks/{oid}/verify", json=dict(confirmed=True), headers={"X-Admin-Key": ADMIN}).status_code == 409
