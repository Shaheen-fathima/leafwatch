"""Torch-free image-quality measurements (pixels only).

Kept separate from model_utils.py so that the photo-quality gate can be tested and used without PyTorch.
`blur_score` is byte-for-byte the same metric train.py used to learn `blur_min` in calib.json, so the
calibrated threshold stays valid.
"""
import numpy as np
from PIL import Image


def _gray(img, size=256):
    return np.asarray(img.convert("L").resize((size, size)), dtype=np.float32)


def blur_score(img):
    """Variance of the Laplacian on a 256x256 grey copy. Low = few sharp edges = blurry."""
    g = _gray(img)
    lap = g[1:-1, 1:-1] * 4 - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
    return float(lap.var())


def image_stats(img):
    """Pixel statistics used by the quality gate: sharpness, mean brightness (0-255) and contrast (std)."""
    g = _gray(img)
    lap = g[1:-1, 1:-1] * 4 - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
    return dict(blur=float(lap.var()), brightness=float(g.mean()), contrast=float(g.std()))


def dhash(img, size=8):
    g = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    a = np.asarray(g, dtype=np.int16)
    bits = (a[:, 1:] > a[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)
