"""Fills a RUNNING server with a realistic demo using your real dataset images (so you can watch alerts live).
   python demo_seed.py --data "<dataset_clean>" [--server http://127.0.0.1:8000] [--crop Tomato] [--disease "Late Blight"]
Open the web app first (join as a farmer near 13.0827, 80.2707), then run this: you will get a live verify request, then an alert."""
import argparse, glob, random, time, httpx
ap = argparse.ArgumentParser(); ap.add_argument("--data", required=True); ap.add_argument("--server", default="http://127.0.0.1:8000")
ap.add_argument("--crop", default="Tomato"); ap.add_argument("--disease", default="Late Blight"); ap.add_argument("--lat", type=float, default=13.0827); ap.add_argument("--lon", type=float, default=80.2707)
a = ap.parse_args(); random.seed(3)
root = a.data + ("/train" if glob.glob(a.data + "/train") else "")
imgs = lambda c: sorted(glob.glob(f"{root}/{c}/*"))
sick, healthy = f"{a.crop} - {a.disease}", f"{a.crop} - Healthy"
assert imgs(sick) and imgs(healthy), f"folders '{sick}' / '{healthy}' not found under {root}"
c = httpx.Client(base_url=a.server, timeout=60)
names = ["Murugan", "Selvi", "Kumar", "Lakshmi", "Raja", "Valli", "Anbu", "Meena"]
farmers = [c.post("/api/farmers", json=dict(name=n, lat=a.lat + random.uniform(-.02, .02), lon=a.lon + random.uniform(-.02, .02))).json() for n in names]
def send(f, path, note):
    r = c.post("/api/report", data=dict(farmer_id=f["id"], lat=f["lat"], lon=f["lon"]), files=dict(image=("leaf.jpg", open(path, "rb"), "image/jpeg"))).json()
    o = r["outbreak"]; print(f"{f['name']:8s} {note:34s} -> {r['top'][0]['name'][:26]:26s} unc {r['uncertainty']:.2f} counted {r['novelty']:.2f} P={o['P'] if o['P'] is None else round(o['P'],2)} {o['action']}"); time.sleep(1)
S = imgs(sick); random.shuffle(S)
send(farmers[0], S[0], "first sick leaf")
for k in range(3): send(farmers[0], S[0], "SAME photo again (duplicate)")
send(farmers[1], imgs(healthy)[0], "neighbour says healthy (conflict)")
send(farmers[2], S[1], "independent farmer, sick")
send(farmers[3], S[2], "second independent farmer, sick")
print("\nDone. Check the map, the Alerts tab and Admin tab in the browser.")
