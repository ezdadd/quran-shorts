"""Sheikh photo in a circular Islamic frame (gold rub el hizb star + rings), drawn with Pillow.

    badge(photo, size) -> RGBA image to paste on the labels layer of a clip.
Library: data/sheikhs/<slug>.jpg + data/sheikhs/index.json {name: file} — a photo sent once in
Telegram is reused for every later clip of the same sheikh.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from .util import ROOT, read_json, write_json

LIB = ROOT / "data" / "sheikhs"
INDEX = LIB / "index.json"
GOLD, GOLD_DARK, INK = (242, 210, 122, 255), (176, 138, 62, 255), (14, 20, 26, 235)


def _star(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    """Eight-pointed star (two squares, 45° apart) as one polygon outline."""
    pts = []
    for k in range(16):
        ang = math.pi / 8 * k - math.pi / 2
        rr = r if k % 2 == 0 else r * 0.80
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    return pts


def badge(photo: Path, size: int = 300) -> Image.Image:
    s = 4                                   # draw large, shrink once: smooth edges
    S = size * s
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = S / 2
    d.polygon(_star(c, c, S * 0.50), fill=GOLD_DARK)                # star behind, gold edge
    d.polygon(_star(c, c, S * 0.475), fill=INK)
    for k in range(8):                                               # small gold dots on the star points
        ang = math.pi / 4 * k - math.pi / 2
        x, y = c + S * 0.43 * math.cos(ang), c + S * 0.43 * math.sin(ang)
        d.ellipse((x - S * 0.012, y - S * 0.012, x + S * 0.012, y + S * 0.012), fill=GOLD)
    ring = S * 0.355
    d.ellipse((c - ring, c - ring, c + ring, c + ring), fill=GOLD)          # outer gold ring
    d.ellipse((c - ring * 0.955, c - ring * 0.955, c + ring * 0.955, c + ring * 0.955), fill=GOLD_DARK)
    r_in = ring * 0.92
    face = ImageOps.fit(Image.open(photo).convert("RGB"), (int(2 * r_in), int(2 * r_in)), centering=(0.5, 0.38))
    mask = Image.new("L", face.size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, face.size[0] - 1, face.size[1] - 1), fill=255)
    img.paste(face, (int(c - r_in), int(c - r_in)), mask)
    d.ellipse((c - r_in, c - r_in, c + r_in, c + r_in), outline=GOLD, width=int(S * 0.008))  # thin inner line
    out = img.resize((size, size), Image.LANCZOS)
    shadow = out.split()[3].filter(ImageFilter.GaussianBlur(10)).point(lambda v: int(v * 0.55))
    final = Image.new("RGBA", (size + 40, size + 40), (0, 0, 0, 0))
    final.paste(Image.new("RGBA", out.size, (0, 0, 0, 255)), (20, 26), shadow)
    final.alpha_composite(out, (20, 20))
    return final


# ---------------------------------------------------------------- library
def _norm(name: str) -> str:
    n = re.sub(r"^(الشيخ|الشيخة|فضيلة|الإمام|الامام|الدكتور|د\.)\s+", "", (name or "").strip())
    return re.sub(r"\s+", " ", n.replace("أ", "ا").replace("إ", "ا").replace("ة", "ه"))


def find(name: str) -> Path | None:
    key = _norm(name)
    if not key:
        return None
    f = read_json(INDEX, {}).get(key)
    return LIB / f if f and (LIB / f).exists() else None


def save(name: str, image_bytes: bytes, fallback: str) -> Path:
    """Store the photo under the sheikh's name (or the clip id if no name)."""
    LIB.mkdir(parents=True, exist_ok=True)
    key = _norm(name)
    slug = re.sub(r"\W+", "_", key, flags=re.U).strip("_") or fallback
    p = LIB / f"{slug}.jpg"
    from io import BytesIO
    Image.open(BytesIO(image_bytes)).convert("RGB").save(p, quality=90)
    if key:
        idx = read_json(INDEX, {})
        idx[key] = p.name
        write_json(INDEX, idx)
    return p
