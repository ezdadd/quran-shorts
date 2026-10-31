"""Build / grow the scene library for 'screen' videos (data/screens.json).

    python -m tools.build_screens               # search, survey, check; keeps what was decided before

1. searches Pixabay for green-screen devices (laptop, phone, TV, monitor, tablet...)
2. keeps scenes with ONE steady green screen fully in the picture (screens.survey)
3. Gemini vision checks them in batches (6 scenes per request, to spare the free quota): no women,
   no people beyond a man's hands, no crosses / idols / Halloween / Christmas, no alcohol, no music
   instruments, no news-studio text... A rejected scene is remembered and never checked again.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import sys
from pathlib import Path

import cv2
import numpy as np
import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import screens  # noqa: E402
from src.util import ROOT, cache_dir, load_config, read_json, setup_logging, write_json  # noqa: E402

log = logging.getLogger("quran-shorts.build_screens")
LIB = ROOT / "data" / "screens.json"
QUERIES = ["laptop green screen", "laptop chroma key", "computer green screen desk", "monitor green screen",
           "imac green screen", "phone green screen", "smartphone green screen table", "phone chroma key",
           "phone green screen hand", "mobile mockup green screen", "tablet green screen", "ipad green screen",
           "tv green screen", "television green screen living room", "old tv green screen", "vintage tv chroma key",
           "smart tv mockup green screen", "cinema screen green screen", "projector screen green screen",
           "billboard green screen", "laptop mockup", "phone mockup", "tv mockup",
           # more phones: vertical clips go on a phone most of the time
           "vertical phone green screen", "smartphone green screen vertical", "phone on table green screen",
           "phone on desk chroma key", "phone coffee green screen", "phone sofa green screen",
           "phone car green screen", "holding phone green screen", "smartphone in hand chroma key",
           "iphone green screen", "mobile phone chroma key vertical", "scrolling phone green screen",
           "phone stand green screen", "phone tripod green screen"]
KINDS = [("phone", ("phone", "smartphone", "mobile", "iphone")), ("tablet", ("tablet", "ipad")),
         ("tv", ("tv", "television")), ("laptop", ("laptop", "notebook", "macbook")),
         ("monitor", ("monitor", "computer", "imac", "desktop")), ("cinema", ("cinema", "projector", "theater")),
         ("billboard", ("billboard",))]
VISION = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-lite-latest", "gemini-2.5-flash-lite",
          "gemini-3.5-flash"]
PROMPT = """Each numbered image shows 3 frames of one stock video: a device with a green screen in a scene.
Our respectful Islamic channel will play a lecture on that green screen. Answer JSON only:
{"results": [{"n": <number>, "ok": true|false, "reason": "<short English>"}]} with one entry per scene.
ok=false if the scene shows: a woman or girl, or a woman's hands (painted nails, bracelets, feminine
rings); a person's face or body (a man's hands / sleeves holding or typing are fine); bare skin or
immodest clothing; a cross, idol, statue, Buddha, church, temple, Halloween (pumpkins, skulls, witches),
Christmas items, or any non-Islamic religious symbol; alcohol, wine glasses, beer, smoking; a musical
instrument; a pig; gambling; readable text overlays, news-studio graphics, tickers or watermarks;
anything scary or violent; more than one green screen. Device brand logos, furniture, plants, books,
coffee, fruit, rooms and nature are fine."""


def kind_of(tags: str, portrait: bool) -> str:
    t = tags.lower()
    for k, words in KINDS:
        if any(w in t for w in words):
            return "phone" if k in ("tablet",) and portrait else k
    return "phone" if portrait else "screen"


def search(key: str) -> dict:
    out = {}
    for q in QUERIES:
        for page in (1, 2):
            r = requests.get("https://pixabay.com/api/videos/", timeout=30,
                             params={"key": key, "q": q, "per_page": 100, "page": page, "safesearch": "true"}).json()
            for h in r.get("hits", []):
                vs = [v for v in h["videos"].values() if v.get("url")]
                best = min(vs, key=lambda v: (v["width"] < 1600, abs(v["width"] - 1920)))
                out[str(h["id"])] = {"tags": h["tags"], "dur": h["duration"], "url": best["url"],
                                     "w": best["width"], "h": best["height"], "page": h["pageURL"],
                                     "user": h.get("user"),
                                     "thumb": h["videos"]["medium"].get("thumbnail") or best.get("thumbnail")}
            if len(r.get("hits", [])) < 100:
                break
    return out


def thumb_ok(url: str) -> bool:
    try:
        im = Image.open(io.BytesIO(requests.get(url, timeout=20).content)).convert("RGB")
    except Exception:
        return False
    bgr = cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)
    m = screens.green_mask(bgr)
    q = screens.find_quad(bgr, m)
    if q is None:
        return False
    area = cv2.contourArea(q)
    return 0.03 < area / m.size < 0.6


def strip(p: Path) -> Image.Image:
    cap = cv2.VideoCapture(str(p))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    ims = []
    for f in (0.1, 0.5, 0.9):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * f))
        ok, fr = cap.read()
        if ok:
            fr = cv2.resize(fr, (int(fr.shape[1] * 300 / fr.shape[0]), 300))
            ims.append(Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)))
    cap.release()
    out = Image.new("RGB", (sum(i.width for i in ims), 300))
    x = 0
    for i in ims:
        out.paste(i, (x, 0))
        x += i.width
    return out


def vision(batch: list[tuple[str, Image.Image]]) -> dict:
    key = os.environ["GEMINI_API_KEY"]
    parts = [{"text": PROMPT}]
    for n, (_, im) in enumerate(batch, 1):
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        parts += [{"text": f"Scene {n}:"}, {"inline_data": {"mime_type": "image/jpeg",
                                                           "data": base64.b64encode(buf.getvalue()).decode()}}]
    for m in VISION:
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                          headers={"x-goog-api-key": key}, timeout=120,
                          json={"contents": [{"parts": parts}], "generationConfig": {
                              "temperature": 0, "responseMimeType": "application/json",
                              "thinkingConfig": {"thinkingLevel": "low"}}})
        if r.status_code in (404, 429) or r.status_code >= 500:
            continue
        try:
            res = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])["results"]
            return {batch[int(x["n"]) - 1][0]: x for x in res if 1 <= int(x["n"]) <= len(batch)}
        except (KeyError, ValueError, IndexError, TypeError) as e:
            log.warning("vision %s: %s", m, e)
    raise RuntimeError("vision models busy (free quota?)")


def main():
    setup_logging()
    cfg = load_config()
    lib = read_json(LIB, {"scenes": {}, "rejected": {}})
    cands = search(os.environ["PIXABAY_API_KEY"])
    new = {k: v for k, v in cands.items() if k not in lib["scenes"] and k not in lib["rejected"]}
    log.info("%d results, %d new", len(cands), len(new))
    work = cache_dir(cfg, "screens")
    surveyed = []
    for vid, c in new.items():
        if c["dur"] < 4 or not thumb_ok(c["thumb"]):
            lib["rejected"][vid] = "no clear green screen"
            continue
        p = work / f"{vid}.mp4"
        try:
            if not p.exists():
                p.write_bytes(requests.get(c["url"], timeout=300).content)
            info = screens.survey(p)
        except Exception as e:
            lib["rejected"][vid] = str(e)[:80]
            p.unlink(missing_ok=True)
            continue
        c.update(portrait=bool(info["portrait"]), aspect=round(info["aspect"], 3),
                 box=[round(x, 1) for x in info["box"]], size=list(info["size"]),
                 kind=kind_of(c["tags"], bool(info["portrait"])))
        c.pop("thumb", None)
        surveyed.append((vid, c, p))
        write_json(LIB, lib)
    log.info("%d scenes passed the survey; checking them", len(surveyed))
    for i in range(0, len(surveyed), 6):
        chunk = surveyed[i:i + 6]
        try:
            res = vision([(vid, strip(p)) for vid, _, p in chunk])
        except RuntimeError as e:
            log.warning("%s — stopping here, run again tomorrow", e)
            break
        for vid, c, p in chunk:
            v = res.get(vid)
            if v and v.get("ok"):
                lib["scenes"][vid] = c
                log.info("✓ %s %s (%s)", vid, c["kind"], c["tags"][:50])
            elif v:
                lib["rejected"][vid] = "vision: " + str(v.get("reason", ""))[:80]
                p.unlink(missing_ok=True)
        write_json(LIB, lib)
    from collections import Counter
    log.info("library: %d scenes %s", len(lib["scenes"]), dict(Counter(s["kind"] for s in lib["scenes"].values())))


if __name__ == "__main__":
    main()
