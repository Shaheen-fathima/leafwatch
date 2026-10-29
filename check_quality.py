"""Measure how the photo-quality gate sees YOUR images (no model needed).
   python check_quality.py photo1.jpg photo2.jpg ...
Prints sharpness / brightness / contrast and the pixel-level verdict using the blur floor from artifacts/calib.json
(or the default 30). Use it on a few clear and a few blurred photos of your own to confirm the thresholds suit your camera;
the crop-confidence / uncertainty part of the gate needs the trained model and runs inside the server."""
import json, sys
from pathlib import Path
from PIL import Image
from quality import image_stats
import engine as E

cal = Path(__file__).parent / "artifacts" / "calib.json"
floor = json.loads(cal.read_text()).get("blur_min", E.CFG["blur_min_default"]) if cal.exists() else E.CFG["blur_min_default"]
c = E.CFG
print(f"blur floor (blur_min) = {floor:.1f}  ->  clearly blurry below {floor * c['blur_soft_factor']:.1f}, 'soft' between that and {floor:.1f}\n")
for f in sys.argv[1:]:
    s = image_stats(Image.open(f).convert("RGB")); why = []
    if s["blur"] < floor * c["blur_soft_factor"]: why.append("BLURRY (reject)")
    elif s["blur"] < floor: why.append("soft (accepted only if the model is very sure)")
    if s["brightness"] < c["dark_mean"]: why.append("TOO DARK (reject)")
    if s["brightness"] > c["bright_mean"]: why.append("TOO BRIGHT (reject)")
    if s["contrast"] < c["min_contrast"]: why.append("LOW CONTRAST (reject)")
    print(f"{f}: sharpness {s['blur']:.1f}  brightness {s['brightness']:.0f}  contrast {s['contrast']:.0f}  ->  {'; '.join(why) or 'pixels OK'}")
