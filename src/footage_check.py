"""Modesty / suitability check for stock footage, done by Gemini (vision, free tier) on frames of
each clip the first time it is about to be used. Verdicts are kept in data/footage_checks.json so a
clip is checked once and a rejected clip never comes back.

Rejected: any woman or person (crowds, hands and silhouettes included), skin / immodest clothing,
crosses, idols, statues, churches or temples, alcohol, smoking, music instruments, pigs, gambling,
logos / watermarks / text, violence, or anything disrespectful to Islam.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
from datetime import date
from pathlib import Path

import requests

from .util import FFMPEG, ROOT, media_duration, read_json, write_json

log = logging.getLogger("quran-shorts.footage_check")
PATH = ROOT / "data" / "footage_checks.json"
# lite models first: a simple yes/no task, and each model has its own small free daily quota (20/day)
MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-lite-latest", "gemini-2.5-flash-lite",
          "gemini-3.5-flash", "gemini-3.7-flash", "gemini-flash-latest"]
PROMPT = """These are frames from one stock video that may be used behind Quran recitation and hadith
in a respectful Islamic channel. Answer with JSON only: {"ok": true|false, "reason": "<short English>"}.
Set ok=false if ANY frame shows: a woman or girl; any person at all (also far away, in a crowd, a
hand, a silhouette or a reflection); bare skin or immodest clothing; a cross, idol, statue, church,
temple or other religious symbol that is not Islamic; alcohol, smoking or drugs; a musical instrument;
a pig; gambling; a logo, watermark or readable text; violence or blood; anything mocking or
disrespectful to Islam. Animals, nature, space, buildings without people, mosques without people,
cozy empty rooms, abstract graphics and flowers are fine."""


class CheckUnavailable(Exception):
    pass


def _frames(p: Path, work: Path) -> list[str]:
    d = media_duration(p)
    out = []
    for i, f in enumerate((0.12, 0.5, 0.88)):
        img = work / f"_chk_{i}.jpg"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-ss", f"{d * f:.2f}", "-i", str(p), "-frames:v", "1",
                        "-vf", "scale=-2:640", str(img)], check=True)
        out.append(base64.b64encode(img.read_bytes()).decode())
    return out


def _ask(frames: list[str]) -> dict:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise CheckUnavailable("no GEMINI_API_KEY")
    parts = [{"text": PROMPT}] + [{"inline_data": {"mime_type": "image/jpeg", "data": f}} for f in frames]
    for model in MODELS:
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                              headers={"x-goog-api-key": key}, timeout=60,
                              json={"contents": [{"parts": parts}],
                                    "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                                                         "thinkingConfig": {"thinkingLevel": "low"}}})
            if r.status_code in (404, 429) or r.status_code >= 500:
                continue
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            v = json.loads(text)
            return {"ok": bool(v.get("ok")), "reason": str(v.get("reason", ""))[:120], "model": model}
        except (requests.RequestException, KeyError, ValueError) as e:
            log.warning("check with %s failed: %s", model, e)
    raise CheckUnavailable("all vision models busy")


def verdict(clip_id: str) -> dict | None:
    return read_json(PATH, {}).get(clip_id)


def is_ok(cfg, clip_id: str, path: Path, work: Path) -> bool:
    """True if the clip passed (now or before). Raises CheckUnavailable if it can't be checked."""
    known = verdict(clip_id)
    if known is not None:
        return known["ok"]
    v = _ask(_frames(path, work))
    data = read_json(PATH, {})
    data[clip_id] = v | {"at": date.today().isoformat()}
    write_json(PATH, data)
    log.info("footage %s: %s %s", clip_id, "OK" if v["ok"] else "REJECTED", v["reason"])
    return v["ok"]
