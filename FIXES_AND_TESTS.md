# LeafWatch patch notes

## Alert-system fix
- An `ALERT` engine decision now creates/updates an outbreak candidate for admin review; it does not immediately claim the outbreak is confirmed or notify farmers.
- Confirming an active outbreak creates persistent alert rows for every farmer currently within the configured 5 km radius and pushes a WebSocket notification where a farmer is connected.
- Rejecting an outbreak sends no alerts.
- Admin response now reports the number of alerts created, and the UI labels the pre-review state as a candidate awaiting admin review.
- Alert tests use a temporary database and a fake predictor; they never delete or modify the user's database.

## Checks run on this package
- `python -m compileall -q ...` — passed.
- `python test_engine.py` — passed (duplicate evidence, conflict handling, uncertain predictions, decision thresholds, and independent-source behavior).
- `python test_alerts.py` — passed (admin confirmation/rejection, persisted alerts, 5 km geofence, and no alert before confirmation).

## Important limitation
The supplied ZIP did not contain trained model artifacts (`artifacts/classes.json`, model weights, calibration file) or the dataset. The engine and alert-flow tests pass, but real-image inference was not testable from this ZIP alone. Copy your existing trained artifacts into `artifacts/`, or train with your dataset before using image prediction.
