# LeafWatch v3 (audit release, Oct 28 2026)

Built on your `leafwatch_updated_source.zip` (secure admin login, approval default, crop-match rule, min-sources check on approval are all kept).

| # | Finding in the audited code | Fix |
|---|---|---|
| A | A farmer who answered a verification request from the normal Report tab left it `pending` forever (only the "Photograph now" button with `request_id` closed it); a healthy answer through the button worked only if the photo was "new". Admin counts and points then disagreed with reality | Any new, conclusive photo of the requested crop closes the farmer's OWN pending request(s): `status=done`, `answer` (confirms / contradicts / other_disease), `answered_report_id`, `answered_ts`; +15 points once per report; duplicates, wrong crop, inconclusive photos and other farmers cannot answer. Reasons are returned and shown |
| B | Oct 27 test: Sana lived ~3 km from Shahin, outside the 1.5 km request radius, so no request could exist | If nobody is within 1.5 km, the nearest farmers within 5 km are asked (`verify_fallback_radius_m`) |
| C | A wrong-crop photo used up the farmer's plot budget, so the correct retake counted 25 % and could not answer a request | Dependency / plot budget only compares photos of the same crop (an identical file is still caught) |
| D | Conflict only lowered P; 3 sick + 3 healthy farmers (conflict 57 %) still gave ALERT | `max_conflict_alert = 0.50`: ALERT becomes VERIFY with a stated reason |
| E | Admin could not see that an outbreak had become doubtful after it was raised, or that approval would be refused | Pending/active cards show P / conflict / engine decision NOW and the independent-farmer count; Approve is disabled (Reject allowed) when < 2 |
| F | Admin lock-out / crash: non-ASCII admin key raised TypeError (500); unlimited guessing | bytes compare, 8 wrong keys in 5 min -> 429 |
| G | Rejected photos were deleted from the database path but still saved on disk | rejected photo file is removed |
| H | Pending outbreaks could be extended after 12 h by a new duplicate outbreak row | pending outbreaks stay matchable until reviewed |
| I | Requests: no reminder, no expiry statistics | one reminder after 3 h; `verify_response_rate`, `verify_median_answer_min`, `verify_expired` in `/api/stats` |
| J | Legacy `test_alerts.py` / `test_api.py` ran against your live database, expected instant alerts and `admin123` (they were in `.pytest_cache/lastfailed`) | removed; replaced by the isolated suite in `tests/` and `smoke_real_model.py` |
| K | Admin page only refreshed on tab open; websocket reconnect did not re-read missed alerts | admin auto-refresh every 15 s while open; alerts re-read on reconnect |

Schema version 3 (additive columns on `requests`); first start backs up an older database into `backups/`.

---

# LeafWatch: debugging release notes

## 0. Read this first (honest scope)
* The ZIP you sent contained **no `leafwatch.db`, no trained model (`artifacts/model.pt`, `classes.json`, `calib.json`) and no dataset**, and PyTorch could not be installed in my sandbox. So **I could not run your real model**. All tests use a stub in place of the neural network (everything else is real: FastAPI, SQLite, the engine, the pixel-quality code on real JPEG bytes, a headless Chromium run of the web page). They prove the surrounding logic, **not your model's accuracy**, and the quality thresholds were checked on *synthetic* leaf images, not real photos (see section 6).
* Your model files, database and uploads are **not in my ZIP and are not touched**. Extract the ZIP *over* your existing project folder (section 4).

## 1. Bugs found and fixes
| # | Problem | Root cause found in the code | Fix |
|---|---|---|---|
| 1a | Blurry / dark / wrong-crop photos | The gate lived inline in the endpoint, used only a blur flag + model numbers, no exposure checks, and could not be tested | `quality.py` (torch-free pixel metrics) + `engine.assess_quality` (pixels AND model). Rejects: clearly blurry (<50% of the calibrated floor), soft-and-unsure, too dark, over-exposed, no contrast, crop mass < 60%, uncertainty >= 70%. A photo that is merely *soft* but where the model is >= 90% sure of the crop is accepted with a warning, so valid photos just under the dataset's 2nd-percentile floor are not rejected. Rejected photos write **nothing** to the database |
| 1b | "Looks healthy" on weak predictions | Any top-class "Healthy" was shown as healthy | Server returns `assessment`; "healthy" only if p >= 0.60 and uncertainty < 0.50, otherwise "Not conclusive" |
| 1c | Rejection reasons recomputed in the browser | UI guessed reasons from numbers | Server returns `reasons` + `reason_messages`; UI lists them |
| 2a | Duplicate earned `+1 point`, still moved P (37% to 39%) | `pts = 5 if novelty >= .5 else 1`; exact copies kept weight 0.05 | Repeats earn **0** points; the same photo (found **anywhere**, not just within 12 km) has weight **0** and action `repeat` (no request, no alert). Verification bonus (+15) only for a genuinely new photo |
| 2b | Counters disagreed (6 reports / 5 clusters / 3 "repeats") | "Down-weighted" counted same-plot photos that were never merged into a cluster | Same-plot photos join the field cluster; stats now `clusters`, `duplicates_merged`, `down_weighted` agree. UI says why (exact / similar / same plot) |
| 2c | Admin "confirm" paid +20 per duplicate upload and reliability counted each upload | Per-report loop | Each farmer judged once, weighted by the report's real weight; +20 once |
| 3a | "Verification asked" but Admin showed nothing | Verify only created farmer requests; the admin list showed only already-alerted outbreaks | New `GET /api/admin/overview` (admin key): outbreaks to decide, **Awaiting verification** (built from saved rows, with *current* P / conflict re-computed), reviewed. `reports` store n_eff, independent farmers, conflict, costs, verify status |
| 3b | Healthy report gave no context | Only recomputed on the next disease report | Healthy reports now show nearby same-crop disease candidates (<= 5 km, 5 days) with P and conflict; other crop / far away = explicitly "nothing to compare" |
| 4a | "Verification asked" shown even when nobody was asked | Message ignored `sent_to == 0` | Server returns `verify_status` = requested / already_pending / no_eligible_farmers; counts are **re-read from the database after commit** |
| 4b | One pending request anywhere blocked requests in every village | Global duplicate check | Only a pending request within 5 km blocks a new one; nearest farmers asked first |
| 4c | Costs favoured ALERT but action said VERIFY | Silent override (`min_sources`) | `cost_action`, `override` text and `independent_farmers` returned and displayed. **Thresholds/costs unchanged** |
| 4d | Failures | n/a | Each report / review is one transaction; `sqlite3.Error` returns 503 "nothing was saved"; tested incl. a failure half-way through alert delivery (everything rolls back) |
| 5a | Rejected outbreak reported as "extends an outbreak already announced" | `find_active` returned rejected rows | `alert_status = suppressed_rejected`, clear message, no update of the rejected row |
| 5b | Farmers who joined/moved into range later never got the alert | Alerts only created with a new outbreak | `deliver_alerts` is idempotent (also on extension and on confirm); unique index `(farmer_id, outbreak_id)` |
| 5c | `read` column unused, badge never cleared | n/a | `POST /api/farmers/{id}/alerts/read`; badge = unread + pending requests; rejected alerts shown as "false alarm, withdrawn" and pushed live |
| 5d | Three browser tabs could not be three farmers (shared `localStorage`) - a likely reason a farmer's Alerts page looked empty | Single stored identity | Identity per tab (`sessionStorage`) + farmer switcher + "New farmer". Alerts always load for the farmer of that tab |
| 5e | Dashboard "alerts" | n/a | Dashboard shows saved rows (`alerts_persisted`), `farmers_alerted`, unread |
| 5f | Optional | Policy was "alert first, admin confirms afterwards" (as in your README) | New opt-in **approval mode** (`LEAFWATCH_REQUIRE_ADMIN_APPROVAL=1`): outbreak is `pending`, nobody alerted until approved. **Default behaviour unchanged** |
| 5g | A farmer visiting another field reported at his HOME location, so alerts/requests went to his home area | Page always sent the registered location | Report tab: "Photo taken at" (GPS / pick on map / my location), used for this report only; profile unchanged. Backend already used the per-report location. Only `static/index.html` changed |
| 5h | Could not type a location; page showed the HTTP status ("Service Unavailable") instead of the server's message | Join dialog only had GPS / map; `api()` mis-read string error details | Typed Latitude/Longitude boxes in the Join dialog and for the photo spot; real server message shown; database errors are also printed in the terminal |
| 9 | Old rows from a different model (other class list / embedding size) would crash every new report | Shape mismatch | Incompatible old rows are ignored as evidence (never deleted) |

Unchanged on purpose: admin key default (`admin123`) and header, all thresholds/costs in `engine.CFG` (new quality knobs added, documented there), model files, class mapping, training code, the 5 km radius, ports.

## 2. Tests
`python -m pytest` -> **54 passed** (also `python test_engine.py` passed, and the original `test_alerts.py` passed against a stub). Mutation check: re-introducing four of the original bugs (duplicate points, gate off, exact copies at 5%, radius ignored) made the suite fail each time.

| Area (your list) | Where |
|---|---|
| 1 clear accepted; 2 blurry rejected; 3 wrong/uncertain crop; dark/bright/flat | `tests/test_quality_gate.py` |
| 4 repeat down-weighted; 5 no false independent evidence; different plants keep weight; same plot | `tests/test_duplicates.py` |
| 6 conflicting / unrelated reports; 7 Alert / Verify / Defer; 8 verification persistence + Admin display | `tests/test_conflict_decision.py` |
| 9 admin approve / reject; 10 nearby alerted; 11 far not; 12 correct farmer's page; 13 duplicate alerts suppressed; failures; websocket | `tests/test_alert_flow.py` |
| 14 existing data intact after upgrade (+ backup, idempotent, old DB works in the app) | `tests/test_migration.py` |

Also run: Python import/syntax of all files; `node --check` of the page's JavaScript; a headless-Chromium run of the real page with three farmers (blur rejected, clear, repeat shows 0% / +0 points, healthy-vs-sick conflict, alert reaches the near farmer's Alerts tab and not the farmer 8 km away, admin dashboard, reject withdraws the alert): no JS errors.

**NOT run** (cannot be here): your real trained model (no torch / no `model.pt`), `train.py`, `simulate.py` (needs the model + dataset), `test_api.py` (needs dataset), real phone photos, real GPS, the web page with live OpenStreetMap tiles, Windows itself (written for Windows; tested on Linux). `simulate.py` numbers will shift slightly (exact repeats now weigh 0): re-run it before quoting results.

## 3. Windows / VS Code
1. Extract the ZIP **over** your existing `leafwatch` folder (Overwrite). Your `leafwatch.db`, `artifacts\`, `uploads\` are not in the ZIP so they stay as they are.
2. VS Code: File > Open Folder > `leafwatch`. Terminal (PowerShell): `venv\Scripts\activate` (or create: `python -m venv venv`, then activate), `pip install -r requirements.txt`. For tests also `pip install -r requirements-dev.txt`.
3. If PowerShell blocks activation: `Set-ExecutionPolicy -Scope Process Bypass`.
4. Start: `python -m uvicorn app:app --host 127.0.0.1 --port 8000` then open http://127.0.0.1:8000 . Optional approval mode: `$env:LEAFWATCH_REQUIRE_ADMIN_APPROVAL="1"` before starting. Different admin key: `$env:LEAFWATCH_ADMIN_KEY="..."`.
5. To test three farmers use **three browser tabs** (each tab keeps its own farmer; use the "Farmer" drop-down / "+ New farmer"). Sign in as admin in the Admin tab with the admin key.

## 4. Safe use of your existing database
* On the first start after the upgrade the app **copies** `leafwatch.db` to `backups\leafwatch.db.pre-v2-<date-time>.bak` (once), then only *adds* columns/indexes. No row is deleted or rewritten; the unit test compares every old value before/after.
* Extra manual safety: stop the server and copy `leafwatch.db` yourself before the first run.
* To go back: stop the server, put the `.bak` file back as `leafwatch.db`, and restore the old code.
* The ZIP contains no secrets and no database.

## 5. Manual test checklist (three tabs: Shahin, Sana 0.5 km away, Rubika ~8 km away)
1. **Photo quality**: upload a heavily blurred photo -> "Photo needs to be retaken" + reason list; Admin: reports count unchanged, no points, no alerts. Upload a sharp photo -> normal result. `python check_quality.py` on your photos shows the numbers.
2. **Duplicates**: upload the same sharp photo twice -> second says "Counted as 0% new evidence", `+0 points`, probability and evidence unchanged; Admin: clusters +0, repeats merged +1.
3. **Conflict**: Shahin sick photo; Sana (near) healthy photo -> Sana sees "Nearby ... reports: probability now X%, conflict Y%". Rubika healthy photo at 8 km -> nothing to compare.
4. **Decision**: first sick report -> "verification requested" (or "no farmer within 1.5 km to ask": then it is listed under Admin > Awaiting verification). Costs line shows the minimum is the chosen action, or the stated rule override. A second independent sick farmer -> alert.
5. **Alerts**: Sana's Alerts tab shows the alert (badge, then cleared once opened); Rubika's is empty; Admin "Alerts saved / Farmers alerted" match; Reject -> alert shows "false alarm, withdrawn" on Sana's page; further reports for 12 h say "No new alert".
6. **Participation**: not a software feature (below).

## 6. Limitations / known issues
* **Quality-gate thresholds are not validated on real photos.** The sharpness floor comes from your dataset (`blur_min`, 2nd percentile of PlantVillage-style lab images); phone photos of leaves in a field have different statistics. Variance-of-Laplacian depends on image content (a plain background lowers it), and it cannot tell motion blur from focus blur. Use `check_quality.py` on your own clear/blurry photos and adjust `engine.CFG` (`blur_soft_factor`, `dark_mean`, ...) if needed. About 2% of clean dataset images fall under the floor by construction and are only accepted when the model is very sure.
* The model was never trained on blurred or non-leaf images; it can be confidently wrong on them (e.g. a photo of a hand). The pixel checks help for blur/exposure, but there is **no leaf detector**. Adding blur/background augmentation and a "not a leaf" class in `train.py` is recommended future work.
* `crop_ambiguous` (margin rule) can never fire before `crop_uncertain` with the default 0.60 floor (mathematically the margin is then >= 0.2); it is kept for when you lower the floor.
* "Independent farmer" = a different account. Two accounts run by one person, or the same leaf photographed with a different camera, cannot be told apart.
* No login: anyone knowing a farmer id can read that farmer's alerts and the live websocket (unchanged from before; the admin key is still a plain header). Public map positions are rounded.
* After an admin rejection, alerts for the same crop/disease/area are suppressed for the 12 h cooldown even if new evidence arrives.
* **Farmer participation (Problem 6)** is a research/field limitation, not a code bug: points and badges are a nudge only; trust, language/literacy, phone access and willingness to report are not solved here. Future work: user study with farmers, local-language voice UI, SMS/WhatsApp, incentives, agronomist-set costs (`cost_miss`, `cost_fa` are assumptions).
* SQLite + in-process websocket = one server (see README section 6).

## 7. Files changed
| File | Why |
|---|---|
| `app.py` | rewritten core: quality gate call, transactional ingest, verification statuses, idempotent alert delivery, approval mode, admin overview, read state, DB-error handling, env overrides for DB/uploads |
| `engine.py` | exact-copy weight 0, repeat classification, field cluster parent, `decide_full` (override explained), healthy-report context, `assess_quality`, `summarise_prediction`, config knobs, shape guard |
| `db.py` | idempotent migration + automatic backup, new columns, global exact-duplicate lookup |
| `quality.py` (new) | torch-free pixel metrics (same sharpness metric as before) |
| `model_utils.py` | imports `blur_score`/`dhash` from `quality.py` (behaviour unchanged) |
| `static/index.html` | per-tab farmer identity + switcher, server-driven messages, unread alerts, new admin view |
| `check_quality.py` (new) | measure your own photos against the gate |
| `tests/`, `pytest.ini`, `requirements-dev.txt` (new) | automated tests |
| `README.md`, `CHANGES.md` | documentation |
| not changed | `train.py`, `simulate.py`, `demo_seed.py`, `run.bat`, `requirements.txt`, `test_engine.py`, `test_alerts.py`, `test_api.py` |
