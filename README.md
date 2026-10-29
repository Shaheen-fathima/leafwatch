# LeafWatch (பயிர் காவல்): verified, uncertainty-aware crop-disease outbreak alerts

Farmer photographs a leaf -> model predicts the disease with a **calibrated uncertainty score** -> server checks for
**repeated** and **conflicting** reports -> fuses all evidence into an **outbreak probability** -> chooses
**ALERT / VERIFY / DEFER** by expected cost -> only a verified outbreak is pushed live (WebSocket) to farmers **within 5 km**.

## 1. Setup (Python 3.10 to 3.12)
```
cd leafwatch
python -m venv venv
venv\Scripts\activate            (Linux/macOS: source venv/bin/activate)
pip install -r requirements.txt
```
Smaller CPU-only PyTorch: `pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu`

## 2. Train on your Kaggle dataset
Expected layout: `dataset_clean/train/<Crop - Disease>/*.jpg` (optional `dataset_clean/valid`). Your folders already match.
```
python train.py --data "C:\Users\<you>\Documents\Cloud project\cloud\dataset_clean"
```
* Defaults: MobileNetV2 with ImageNet weights, 4 epochs, 400 images per class (quick on CPU).
  For the full set: `--max_per_class 0 --epochs 8` (slow on CPU, much faster with an NVIDIA GPU).
* Writes `artifacts/`: `model.pt`, `classes.json`, `calib.json` (temperature + duplicate-photo thresholds learned from your data), `metrics.json` (accuracy, ECE before/after calibration).
* Internet is needed once to download ImageNet weights. Offline it trains from scratch and warns you.
* Check the printed validation accuracy. If it prints a low-accuracy warning, train longer before trusting the probabilities.

## 3. Run
```
python -m uvicorn app:app --host 127.0.0.1 --port 8000      (this PC only)
python -m uvicorn app:app --host 0.0.0.0 --port 8000        (or double-click run.bat: phones on the same Wi-Fi can connect)
```
Open http://127.0.0.1:8000, join (name + GPS or tap the map), upload a leaf photo.
Phones on the same Wi-Fi: http://PC-IP:8000 (browsers block GPS on plain http, so tap the map there).
Map tiles, Leaflet and the Mukta font load from the internet.

**Live demo with your real images** (server running, browser open and joined near Chennai):
```
python demo_seed.py --data "C:\...\dataset_clean" --crop Tomato --disease "Late Blight"
```
You will see: the repeated photo counted at about 5 percent, a "healthy" neighbour creating conflict, a verify request,
then a second independent farmer triggering the alert that reaches your browser instantly.

## 4. Evaluate (this is what establishes novelty)
```
python simulate.py --data "C:\...\dataset_clean" --per_class 25 --episodes 150 --seeds 5
```
Runs your real model on real dataset images, replayed as simulated report streams (duplicates, conflicting and unreliable
farmers, isolated false alarms, quiet days) through 7 methods: single report, majority vote, fixed threshold, ours without
each module, ours full. Prints false-alert %, recall, precision, detection delay, verify requests (mean +- std over seeds)
and saves `artifacts/sim_results.json`. It is a **simulation**: label it so in the paper, and add real or independently
verified reports if you can.

Tests (no trained model needed): `pip install -r requirements-dev.txt` then `python -m pytest` (54 tests, see CHANGES.md) and `python test_engine.py`.
Needs your trained model / dataset: `python test_alerts.py` | `python test_api.py "<dataset_clean>\train"`.
Check how the photo-quality gate sees your own photos: `python check_quality.py clear.jpg blurry.jpg`.

## 5. Your 6 problems and where they are solved
| Problem | Code | What happens |
|---|---|---|
| 1 Wrong or blurry photo | `quality.py`, `engine.assess_quality`, `app.report` | pixel checks (sharpness vs the floor learned in `calib.json`, brightness, contrast) AND model checks (crop probability mass, margin, uncertainty). A rejected photo creates no database row: no evidence, no points, no request, no alert. A merely *soft* photo is accepted only if the model is very sure |
| 2 Same plant uploaded many times | `engine.dependency`, `engine.novelty`, `db.exact_duplicates` | image embedding + dHash + distance + time + same-farmer burst. Reports are never deleted. The very same photo (anywhere) counts **0**; near-copies 0.05 to 0.5; one farmer's own plot adds at most `field_budget` = 1.25 evidence per 24 h. Repeats earn **0 points** and trigger nothing. Photos from other places keep full weight |
| 3 Farmers disagree | `engine.fuse` | Bayesian log-odds fusion of calibrated probability x farmer reliability (Beta prior, learned from admin verdicts) x distance/time kernel; "healthy" reports lower the probability; conflict index shown |
| 4 When to alert | `engine.decide` | expected cost of ALERT / VERIFY (asks neighbours within 1.5 km for a photo) / DEFER. An alert also needs at least 2 independent farmers |
| 5 False alarms destroy trust | `app.ingest`, `app.deliver_alerts`, `/api/admin/outbreaks/{id}/verify` | the engine's ALERT needs >= 2 independent farmers; 12 h cooldown; one alert row per farmer per outbreak (database-enforced); admin confirm/reject updates reliability (each farmer judged once) and a rejection withdraws the alert on the farmers' pages. Optional **approval mode**: `set LEAFWATCH_REQUIRE_ADMIN_APPROVAL=1` means nobody is alerted until the admin approves |
| 6 Farmers do not report | scope limit | points and badges are only a small nudge. Participation, trust and accessibility are **not solved by this software**; they need field work (see CHANGES.md, "Limitations / future work") |

All knobs live in `engine.CFG`. The costs (`cost_miss` = 4 x `cost_fa`) are assumptions: set them with agronomists.

## 6. Honest status and limits
* Tested in my sandbox on a **synthetic toy dataset**: engine unit tests, API + WebSocket + 5 km geofence + admin flow, training script, simulator, live server start, frontend JavaScript syntax. **Not yet run on your Kaggle data, and the web page was not opened in a real browser.** The first run may need small fixes (paths, package versions). Send me the traceback.
* PlantVillage-style images are lab-clean, so field accuracy will be lower, and calibration on your validation split does not guarantee calibration in the field.
* SQLite + in-process WebSocket = one server, hundreds of farmers. To scale: PostgreSQL/PostGIS, Redis pub/sub, OTP login, HTTPS, rate limiting, SMS/WhatsApp for offline phones (none of this is implemented).
* Admin key is a simple header and farmers have no login. Public map positions are rounded to about 110 m.
* In `simulate.py` the "ours - no X" ablations still keep the 2-independent-farmers rule, so the separate gain of the dependency module looks small there. Report that honestly.


## Security and alert delivery (updated)

Before starting LeafWatch, configure an administrator key. There is **no default admin password**.

**Windows PowerShell (current terminal only):**
```powershell
$env:LEAFWATCH_ADMIN_KEY = "choose-a-long-unique-secret"
$env:LEAFWATCH_REQUIRE_ADMIN_APPROVAL = "1"
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Open LeafWatch as a farmer normally. The Admin tab is hidden unless the user successfully authenticates using the server-configured key. Admin overview and approve/reject endpoints independently check the key on the backend. Never publish or share the admin key, and do not expose this development server directly to the public internet.

Public outbreak alerts require admin approval by default. Approval is refused unless the saved outbreak evidence includes at least two distinct farmers with non-duplicate evidence. Alerts are persisted only for registered farmers whose last submitted/registered location is within 5 km of the outbreak centre. Use **Enable live location** to opt into browser GPS updates while the page remains open; browser permission is required, and tracking stops when the user clicks Stop live location or closes the page. This is not background tracking when the browser is closed.

Verification responses only fulfil a pending request and earn verification points when submitted by the invited farmer and the predicted crop matches the requested crop. A healthy leaf of the requested crop remains a valid conflicting response.

Important: this is still a local prototype. Farmer IDs are currently client-selected identifiers rather than full account authentication. For deployment with real users, add verified farmer login/session tokens, HTTPS, rate limiting, audit logging, and server-side authorization for farmer-specific routes before hosting publicly.


## v3 audit release: what changed (read CHANGES.md "v3" for details)
* **Approval is ON by default; there is no default admin password.** Set `LEAFWATCH_ADMIN_KEY` (run.bat asks for it). Only an admin-approved outbreak with >= 2 independent farmers can alert, once per farmer, inside 5 km. Rejection never alerts and blocks re-sends for 12 h. `LEAFWATCH_REQUIRE_ADMIN_APPROVAL=0` restores the old behaviour for demos only.
* **Verification answers**: a sick OR healthy photo of the requested crop answers the farmer's own pending request (button or normal Report tab), once, +15 points; duplicates and wrong crops do not. If nobody lives within 1.5 km the nearest farmers within 5 km are asked.
* **Decision**: ALERT is downgraded to VERIFY when reports conflict >= 50 % (new `max_conflict_alert`), besides the existing >= 2 independent farmers rule.
* **Participation**: one reminder after 3 h, request expiry 24 h, response-rate statistics. Real participation still needs a field trial (USER_TEST_PLAN.md).
* **Tests**: `pip install -r requirements-dev.txt` then `python -m pytest` (tests/). `python check_integrity.py leafwatch.db` audits your real database read-only. `python smoke_real_model.py photo.jpg ...` judges the quality gate with YOUR model.
* **Hosting**: see HOSTING.md. A local PC is not "always online".

