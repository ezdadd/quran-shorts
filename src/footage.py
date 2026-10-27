"""Nature footage: Pexels (primary), Pixabay (fallback), local folder (last resort).

Pexels docs (checked 2026-10): GET https://api.pexels.com/v1/videos/search, header
`Authorization: <key>`, params query/orientation/size/per_page(<=80); 200 req/h, 20k/month.
The old /videos/ path is being deprecated. Credit the videographer.
Pixabay docs: GET https://pixabay.com/api/videos/?key=..&q=..; 100 req/60 s; results must be
cached 24 h and files downloaded (no hotlinking).
"""
from __future__ import annotations

import logging
import os
import random
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .util import ROOT, cache_dir, download, http_json, media_duration, read_json, write_json

log = logging.getLogger(__name__)


@dataclass
class Clip:
    id: str              # "pexels:123" / "pixabay:456" / "local:file.mp4"
    category: str
    path: Path
    duration: float
    width: int
    height: int
    credit: str          # "Video by X on Pexels"
    page_url: str


def _blocked(text: str, words: list[str]) -> bool:
    tokens = set(re.split(r"[^a-z]+", text.lower()))
    return any(w in tokens for w in words)


def _cover_ok(w: int, h: int, cfg) -> bool:
    """Can a 1080x1920 crop be cut from w x h without upscaling?"""
    need_w, need_h = cfg["footage"]["min_width"], cfg["footage"]["min_height"]
    scale = max(need_w / w, need_h / h)
    return scale <= 1.0


# ---------------------------------------------------------------- providers
def _pexels(cfg, query: str, rng: random.Random) -> list[dict]:
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        return []
    cache = cache_dir(cfg, "api", "pexels") / f"{re.sub(r'\W+', '_', query)}.json"
    data = read_json(cache, None)
    if data is None or data.get("_date") != date.today().isoformat():
        data = http_json("https://api.pexels.com/v1/videos/search",
                         headers={"Authorization": key},
                         params={"query": query, "orientation": "portrait", "size": "medium",
                                 "per_page": 40})
        data["_date"] = date.today().isoformat()
        write_json(cache, data)
    out = []
    for v in data.get("videos", []):
        files = [f for f in v["video_files"] if f.get("file_type") == "video/mp4"
                 and f.get("width") and f.get("height") and _cover_ok(f["width"], f["height"], cfg)]
        if not files:
            continue
        best = min(files, key=lambda f: f["width"] * f["height"])  # smallest that still covers
        out.append({"id": f"pexels:{v['id']}", "url": best["link"], "w": best["width"],
                    "h": best["height"], "duration": v.get("duration", 0), "page": v["url"],
                    "credit": f"Video by {v['user']['name']} on Pexels", "text": v["url"]})
    return out


def _pixabay(cfg, query: str, rng: random.Random) -> list[dict]:
    key = os.environ.get("PIXABAY_API_KEY")
    if not key:
        return []
    cache = cache_dir(cfg, "api", "pixabay") / f"{re.sub(r'\W+', '_', query)}.json"
    data = read_json(cache, None)
    if data is None or data.get("_date") != date.today().isoformat():
        data = http_json("https://pixabay.com/api/videos/",
                         params={"key": key, "q": query, "video_type": "film", "per_page": 50,
                                 "safesearch": "true"})
        data["_date"] = date.today().isoformat()
        write_json(cache, data)
    out = []
    for v in data.get("hits", []):
        for size in ("medium", "large"):   # smallest that still covers 1080x1920
            f = v.get("videos", {}).get(size) or {}
            if f.get("url") and f.get("width") and _cover_ok(f["width"], f["height"], cfg):
                out.append({"id": f"pixabay:{v['id']}", "url": f["url"], "w": f["width"],
                            "h": f["height"], "duration": v.get("duration", 0),
                            "page": v["pageURL"], "credit": f"Video by {v['user']} on Pixabay",
                            "text": v.get("tags", "") + " " + v["pageURL"]})
                break
    return out


def _local(cfg, category: str) -> list[dict]:
    d = ROOT / "assets" / "footage" / category
    return [{"id": f"local:{category}/{p.name}", "path": p, "page": "", "credit": "", "text": p.stem}
            for p in sorted(d.glob("*.mp4"))] if d.is_dir() else []


# ---------------------------------------------------------------- selection
def pick_category(cfg, rng: random.Random, avoid: set[str] = frozenset()) -> str:
    w = {k: v for k, v in cfg["footage"]["category_weights"].items() if k not in avoid}
    if not w:
        w = cfg["footage"]["category_weights"]
    keys = list(w)
    return rng.choices(keys, weights=[w[k] for k in keys])[0]


def get_clips(cfg, category: str, need_seconds: float, history: dict, rng: random.Random) -> list[Clip]:
    """Return 1+ clips of one category whose total length covers need_seconds (looping the
    last one if the pool runs dry)."""
    fcfg = cfg["footage"]
    cutoff = (date.today() - timedelta(days=fcfg["reuse_cooldown_days"])).isoformat()
    used = {k for k, d in history.get("footage_used", {}).items() if d >= cutoff}
    queries = list(fcfg["categories"][category])
    rng.shuffle(queries)

    last_used = history.get("footage_used", {})
    candidates: list[dict] = []
    cooled: list[dict] = []   # recently used — only if nothing fresh is left
    for provider in fcfg["providers"]:
        for q in queries[:2] if provider != "local" else [None]:
            try:
                if provider == "pexels":
                    found = _pexels(cfg, q, rng)
                elif provider == "pixabay":
                    found = _pixabay(cfg, q, rng)
                else:
                    found = _local(cfg, category)
            except Exception as e:
                log.warning("%s search failed for %r: %s", provider, q, e)
                continue
            for c in found:
                if _blocked(c["text"], fcfg["blocked_words"]):
                    continue
                if any(c["id"] == x["id"] for x in candidates + cooled):
                    continue
                (cooled if c["id"] in used else candidates).append(c)
        if candidates:
            break
    if candidates:
        rng.shuffle(candidates)
    elif cooled:
        log.warning("all %s footage is on cooldown — reusing least recently used clips", category)
        cooled.sort(key=lambda c: last_used.get(c["id"], ""))
        candidates = cooled
    else:
        raise RuntimeError(f"no footage for category {category!r} (set PEXELS_API_KEY or add "
                           f"clips to assets/footage/{category}/)")

    clips: list[Clip] = []
    total = 0.0
    for c in candidates:
        if "path" in c:
            p = c["path"]
        else:
            ext = ".mp4"
            p = cache_dir(cfg, "footage", category) / (c["id"].replace(":", "_") + ext)
            try:
                download(c["url"], p)
            except Exception as e:
                log.warning("download failed %s: %s", c["id"], e)
                continue
        try:
            dur = media_duration(p)
        except Exception:
            continue
        if dur < 4:
            continue
        clips.append(Clip(c["id"], category, p, dur, c.get("w", 0), c.get("h", 0),
                          c["credit"], c["page"]))
        total += dur - 1.0  # crossfade overlap
        if total >= need_seconds or len(clips) >= 4:
            break
    if not clips:
        raise RuntimeError(f"all footage downloads failed for {category!r}")
    return clips
