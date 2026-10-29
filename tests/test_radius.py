"""5 km delivery boundary. The alert centre is the evidence centroid; three reporters stand at the same spot so the centre is exact."""
from helpers import *


def _approved(distances_m):
    A = fresh_app(); ids, outs = three_sick_neighbours(A); oid = one(A, "SELECT id FROM outbreaks")["id"]
    ob = one(A, "SELECT lat, lon FROM outbreaks")
    far = {}
    for d in distances_m:
        with A.db.tx() as c:
            far[d] = c.execute("INSERT INTO farmers(name,lat,lon,created) VALUES(?,?,?,?)", (f"d{d}", ob["lat"] + dlat(d), ob["lon"], time.time())).lastrowid
    res, notes = A.review(oid, True); return A, far, res, notes, oid


def test_inside_5km_alerted_outside_not_with_boundary_cases():
    A, far, res, notes, oid = _approved([500, 4000, 4990, 5010, 5500, 8000, 20000])
    got = {d for d, f in far.items() if A.alerts(f)}
    assert got == {500, 4000, 4990}, got
    for d in (5010, 5500, 8000, 20000): assert A.alerts(far[d]) == [] and far[d] not in [n[0] for n in notes]
    assert all(a["dist_km"] <= 5.0 for f in far.values() for a in A.alerts(f))


def test_distance_stored_in_alert_is_correct():
    A, far, res, notes, oid = _approved([2500]); a = A.alerts(far[2500])[0]; assert abs(a["dist_km"] - 2.5) < 0.02


def test_farmer_who_moves_into_range_later_gets_one_alert_when_outbreak_grows():
    A, far, res, notes, oid = _approved([8000]); f = far[8000]; assert A.alerts(f) == []
    ob = one(A, "SELECT lat FROM outbreaks")
    with A.db.tx() as c: c.execute("UPDATE farmers SET lat=? WHERE id=?", (ob["lat"] + dlat(3000), f))        # farmer moved to 3 km
    n1 = report(A, add_farmer(A, "w1", north_m=20), "Tomato - Late Blight")[1]; n2 = report(A, add_farmer(A, "w2", north_m=40), "Tomato - Late Blight")[1]
    assert len(A.alerts(f)) == 1 and sum(1 for n in n1 + n2 if n[0] == f and n[1]["type"] == "alert") == 1


def test_farmer_who_moves_out_of_range_keeps_history_but_gets_nothing_new():
    A, far, res, notes, oid = _approved([1000]); f = far[1000]; assert len(A.alerts(f)) == 1
    ob = one(A, "SELECT lat FROM outbreaks")
    with A.db.tx() as c: c.execute("UPDATE farmers SET lat=? WHERE id=?", (ob["lat"] + dlat(9000), f))
    report(A, add_farmer(A, "w1", north_m=20), "Tomato - Late Blight"); assert len(A.alerts(f)) == 1 and count(A, "alerts", "farmer_id=?", f) == 1
