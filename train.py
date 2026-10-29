"""Train MobileNetV2 on a Kaggle PlantVillage-style folder dataset, then calibrate it.
   python train.py --data "C:/Users/you/Documents/Cloud project/cloud/dataset_clean"
Outputs in ./artifacts: model.pt, classes.json, calib.json (temperature, dependency thresholds), metrics.json
"""
import argparse, json, random, time
from pathlib import Path
import numpy as np, torch, torch.nn as nn
from torch.utils.data import DataLoader, Subset
from torchvision import datasets, transforms
from model_utils import build_model, forward_emb, EVAL_TF, MEAN, STD, ART, blur_score

ap = argparse.ArgumentParser()
ap.add_argument("--data", required=True)
ap.add_argument("--epochs", type=int, default=4)
ap.add_argument("--batch", type=int, default=32)
ap.add_argument("--max_per_class", type=int, default=400, help="0 = use every image (slow on CPU)")
ap.add_argument("--workers", type=int, default=0)
ap.add_argument("--lr", type=float, default=1e-3)
ap.add_argument("--no_pretrained", action="store_true")
a = ap.parse_args()
random.seed(0); torch.manual_seed(0); np.random.seed(0)
dev = "cuda" if torch.cuda.is_available() else "cpu"
root = Path(a.data)
tr_dir = root / "train" if (root / "train").is_dir() else root
va_dir = next((root / n for n in ("valid", "val", "validation") if (root / n).is_dir()), None)

train_tf = transforms.Compose([transforms.RandomResizedCrop(224, scale=(0.6, 1)), transforms.RandomHorizontalFlip(),
                               transforms.ColorJitter(0.25, 0.25, 0.2), transforms.ToTensor(), transforms.Normalize(MEAN, STD)])
ds_tr = datasets.ImageFolder(tr_dir, train_tf)
classes = ds_tr.classes
print(f"{len(classes)} classes, {len(ds_tr)} train images, device={dev}")


def pick(ds, per_class, skip=()):
    by = {}
    for i, (_, y) in enumerate(ds.samples):
        by.setdefault(y, []).append(i)
    out = []
    for y, ids in by.items():
        random.shuffle(ids)
        out += [i for i in ids if i not in skip][: per_class or len(ids)]
    return out

if va_dir:
    ds_va = datasets.ImageFolder(va_dir, EVAL_TF)
    assert ds_va.classes == classes, "train/valid class folders differ"
    tr_idx = pick(ds_tr, a.max_per_class); va_idx = pick(ds_va, max(30, a.max_per_class // 4) if a.max_per_class else 0)
else:   # no valid folder: hold out 12% of train
    ds_va = datasets.ImageFolder(tr_dir, EVAL_TF)
    all_idx = pick(ds_tr, 0); random.shuffle(all_idx)
    cut = int(len(all_idx) * 0.12); va_idx, rest = all_idx[:cut], set(all_idx[cut:])
    by = {}
    for i in rest: by.setdefault(ds_tr.samples[i][1], []).append(i)
    tr_idx = [i for ids in by.values() for i in (ids[: a.max_per_class] if a.max_per_class else ids)]
    va_idx = va_idx[:3000]
dl_tr = DataLoader(Subset(ds_tr, tr_idx), a.batch, shuffle=True, num_workers=a.workers)
dl_va = DataLoader(Subset(ds_va, va_idx), 64, num_workers=a.workers)
print(f"using {len(tr_idx)} train / {len(va_idx)} validation images")

model = build_model(len(classes), not a.no_pretrained).to(dev)
lossf = nn.CrossEntropyLoss(label_smoothing=0.05)


@torch.no_grad()
def collect(dl):
    model.eval(); L, Y = [], []
    for x, y in dl:
        L.append(forward_emb(model, x.to(dev))[0].cpu()); Y.append(y)
    return torch.cat(L), torch.cat(Y)


best, t0 = 0, time.time()
for ep in range(a.epochs):
    freeze = ep == 0 and not a.no_pretrained
    for p in model.features.parameters(): p.requires_grad = not freeze          # epoch 0: head only (fast, stable)
    params = [p for p in model.parameters() if p.requires_grad]
    opt = torch.optim.AdamW(params, lr=a.lr if (freeze or a.no_pretrained) else a.lr / 10, weight_decay=1e-4)
    model.train()
    for i, (x, y) in enumerate(dl_tr):
        opt.zero_grad(); loss = lossf(model(x.to(dev)), y.to(dev)); loss.backward(); opt.step()
        if i % 50 == 0: print(f"  ep{ep+1} it{i}/{len(dl_tr)} loss {loss.item():.3f}", flush=True)
    L, Y = collect(dl_va); acc = (L.argmax(1) == Y).float().mean().item()
    print(f"epoch {ep+1}/{a.epochs}  val acc {acc:.4f}  ({time.time()-t0:.0f}s)")
    if acc >= best:
        best = acc; ART.mkdir(exist_ok=True); torch.save(model.state_dict(), ART / "model.pt")

# ---------- calibration (temperature scaling) ----------
model.load_state_dict(torch.load(ART / "model.pt", map_location=dev)); model.to(dev)
L, Y = collect(dl_va)


def ece(logits, y, T=1.0, bins=15):
    p = torch.softmax(logits / T, 1); conf, pred = p.max(1); ok = (pred == y).float(); e = 0.0
    for lo in np.linspace(0, 1, bins + 1)[:-1]:
        m = (conf > lo) & (conf <= lo + 1 / bins)
        if m.any(): e += m.float().mean().item() * abs(ok[m].mean().item() - conf[m].mean().item())
    return e

logT = torch.zeros(1, requires_grad=True)
opt = torch.optim.LBFGS([logT], lr=0.1, max_iter=100)
def closure():
    opt.zero_grad(); l = nn.functional.cross_entropy(L / logT.exp(), Y); l.backward(); return l
opt.step(closure)
T = min(10.0, max(0.5, float(logT.exp().item())))
if best < 0.6: print('! WARNING: validation accuracy is low (%.2f): train longer / more data before trusting the probabilities' % best)
e0, e1 = ece(L, Y), ece(L, Y, T)
if e1 > e0: T, e1 = 1.0, e0          # never keep a temperature that makes calibration worse
print(f"temperature {T:.3f}   ECE before {e0:.4f} -> after {e1:.4f}")

# ---------- calibrate the 'same photo?' embedding thresholds + blur floor ----------
aug = transforms.Compose([transforms.Resize((256, 256)), transforms.RandomResizedCrop(224, scale=(0.75, 1)), transforms.RandomHorizontalFlip(),
                          transforms.ColorJitter(0.2, 0.2, 0.2), transforms.ToTensor(), transforms.Normalize(MEAN, STD)])
rng = random.Random(1); picks = rng.sample(range(len(va_idx)), min(200, len(va_idx))); E1, E2, blurs = [], [], []
from PIL import Image
model.eval()
with torch.no_grad():
    for k in picks:
        path = ds_va.samples[va_idx[k]][0]; im = Image.open(path).convert("RGB"); blurs.append(blur_score(im))
        v1 = forward_emb(model, EVAL_TF(im).unsqueeze(0).to(dev))[1][0]; v2 = forward_emb(model, aug(im).unsqueeze(0).to(dev))[1][0]
        E1.append(torch.nn.functional.normalize(v1, dim=0).cpu().numpy()); E2.append(torch.nn.functional.normalize(v2, dim=0).cpu().numpy())
E1, E2 = np.array(E1), np.array(E2)
same = (E1 * E2).sum(1); S = E1 @ E1.T; diff = S[np.triu_indices(len(E1), 1)]
lo = float(np.percentile(diff, 95)); hi = float(np.median(same))
if hi < lo + 0.02: lo = hi - 0.05
calib = dict(temperature=T, emb_lo=round(lo, 4), emb_hi=round(min(hi, 0.999), 4), blur_min=float(np.percentile(blurs, 2)))
print("dependency thresholds:", calib)
(ART / "classes.json").write_text(json.dumps(classes, ensure_ascii=False, indent=1), encoding="utf-8")
(ART / "calib.json").write_text(json.dumps(calib, indent=1))
(ART / "metrics.json").write_text(json.dumps(dict(val_acc=float(best), ece_raw=float(e0), ece_calibrated=float(e1), temperature=T, n_classes=len(classes),
                                                  n_train=len(tr_idx), n_val=len(va_idx)), indent=1))
print("saved to", ART.resolve())
