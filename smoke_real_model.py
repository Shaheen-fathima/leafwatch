"""Run YOUR trained model + the photo-quality gate on real photos. No database is touched.
    python smoke_real_model.py clear.jpg blurry.jpg other.jpg
Prints, per photo: sharpness / brightness / contrast, top-3 classes, uncertainty, and whether the gate would accept or ask for a retake.
Use it to judge the gate on REAL phone photos (the automated tests use synthetic images and a stub model)."""
import sys
from PIL import Image
import engine as E
from quality import image_stats
from model_utils import Predictor

P = Predictor(); cls = E.Classes(P.classes); blur_min = P.calib.get("blur_min")
cfg = dict(E.CFG); print(f"sharpness floor from calib.json (blur_min) = {blur_min}\n")
for path in sys.argv[1:]:
    img = Image.open(path).convert("RGB"); img.thumbnail((1600, 1600)); pr = P.predict(img); st = image_stats(img)
    q = E.assess_quality(pr["probs"], float(pr["u"]), st, cls, cfg, blur_min)
    top = sorted(range(len(pr["probs"])), key=lambda i: -pr["probs"][i])[:3]
    print(path); print(f"   sharpness {st['blur']:.1f} · brightness {st['brightness']:.0f} · contrast {st['contrast']:.0f} · uncertainty {pr['u']:.2f}")
    print("   top:", ", ".join(f"{cls.names[i]} {pr['probs'][i]:.0%}" for i in top))
    print("   GATE:", "ACCEPTED" + (" (soft focus warning)" if q["soft_focus"] else "") if q["status"] == "ok" else "RETAKE -> " + "; ".join(q["messages"]), "\n")
