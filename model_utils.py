"""Model loading + per-image prediction with calibrated probabilities and uncertainty."""
import json, math
from pathlib import Path
import numpy as np
from PIL import Image
import torch, torch.nn as nn
from torchvision import models, transforms
from quality import blur_score, dhash          # torch-free helpers (re-exported: train.py / simulate.py import them from here)

ART = Path(__file__).parent / "artifacts"
IMG = 224
MEAN, STD = [0.485, 0.456, 0.406], [0.229, 0.224, 0.225]
EVAL_TF = transforms.Compose([transforms.Resize((IMG, IMG)), transforms.ToTensor(), transforms.Normalize(MEAN, STD)])


def build_model(n_classes, pretrained=True):
    m = None
    if pretrained:
        try:
            m = models.mobilenet_v2(weights=models.MobileNet_V2_Weights.IMAGENET1K_V1)
        except Exception as e:                      # offline -> still works, just trains from scratch
            print("! could not download ImageNet weights (%s). Training from scratch." % type(e).__name__)
    if m is None:
        m = models.mobilenet_v2(weights=None)
    m.classifier = nn.Sequential(nn.Dropout(0.2), nn.Linear(m.last_channel, n_classes))
    return m


def forward_emb(m, x):
    f = m.features(x)
    e = torch.flatten(nn.functional.adaptive_avg_pool2d(f, 1), 1)
    return m.classifier(e), e


class Predictor:
    def __init__(self, art=ART):
        self.classes = json.loads((art / "classes.json").read_text(encoding="utf-8"))
        self.calib = json.loads((art / "calib.json").read_text(encoding="utf-8"))
        self.T = float(self.calib.get("temperature", 1.0))
        self.model = build_model(len(self.classes), pretrained=False)
        self.model.load_state_dict(torch.load(art / "model.pt", map_location="cpu"))
        self.model.eval()

    @torch.no_grad()
    def predict(self, img: Image.Image):
        img = img.convert("RGB")
        logits, emb = forward_emb(self.model, EVAL_TF(img).unsqueeze(0))
        probs = torch.softmax(logits[0] / self.T, 0).numpy().astype(np.float64)
        emb = emb[0].numpy().astype(np.float64); emb /= (np.linalg.norm(emb) + 1e-9)
        ent = float(-(probs * np.log(probs + 1e-12)).sum() / math.log(len(probs)))
        blur = blur_score(img)
        blurry = blur < self.calib.get("blur_min", 30.0)
        u = min(1.0, ent + (0.2 if blurry else 0.0))
        return dict(probs=probs, emb=emb, dhash=dhash(img), u=u, entropy=ent, blur=blur, blurry=blurry)
