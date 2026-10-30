"""Our sounds library: any audio you share from Facebook / Instagram / YouTube / TikTok in Telegram
with a caption "صوت: <name>" — someone talking, a song, a nasheed, nature, anything. Saved on
Cloudinary (quran-shorts/sounds/), listed in data/sounds.json, shown with /sounds.

Nothing is refused: Gemini listens once and labels the sound (speech / music / nasheed / nature /
mixed) so the list stays organised.

RULE: Quran and hadith videos never use these sounds. A library sound is used only when you ask for
it — the 🎬 button in /sounds makes a new video from that sound on our footage (src/clips.py).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

from .util import ROOT, cache_dir, media_duration, read_json, run_ffmpeg, write_json

log = logging.getLogger("quran-shorts.sounds")
PATH = ROOT / "data" / "sounds.json"
MAX_SECONDS = 180
KINDS = {"speech": "🗣 كلام", "music": "🎵 موسيقى/أغنية", "nasheed": "🎤 نشيد", "nature": "🌿 طبيعة",
         "ambient": "🌫 خلفية هادية", "mixed": "🔀 مختلط", "unknown": "❔"}
SPOKEN = ("speech", "mixed", "nasheed", "unknown")   # worth transcribing into subtitles


class SoundError(Exception):
    pass


def load() -> dict:
    return read_json(PATH, {})


def classify(mp3: Path) -> tuple[str, str]:
    """(kind, short description) — Gemini listens to the first minute. Never refuses anything."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return "unknown", ""
    prompt = ('Listen to this audio and answer JSON only: {"kind": "speech|music|nasheed|nature|ambient|mixed", '
              '"summary": "<max 8 words, Arabic>"}. speech = mainly someone talking; music = song or instruments; '
              'nasheed = singing without instruments; nature = rain, wind, sea, birds...; ambient = calm '
              'background tone without words; mixed = talking over music.')
    data = base64.b64encode(mp3.read_bytes()).decode()
    for model in ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-lite-latest", "gemini-3.5-flash"):
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                              headers={"x-goog-api-key": key}, timeout=120,
                              json={"contents": [{"parts": [{"text": prompt},
                                                            {"inline_data": {"mime_type": "audio/mp3", "data": data}}]}],
                                    "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}})
            if not r.ok:
                continue
            v = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
            kind = v.get("kind") if v.get("kind") in KINDS else "unknown"
            return kind, str(v.get("summary", ""))[:80]
        except (requests.RequestException, KeyError, ValueError) as e:
            log.warning("sound label %s: %s", model, e)
    return "unknown", ""


def add(cfg, name: str, req: dict) -> dict:
    from .clips import ClipError, fetch_audio
    from .publish import hosting
    name = re.sub(r"\s+", " ", name).strip()[:40]
    if not name:
        raise SoundError("اكتب اسم للصوت بعد «صوت:»")
    work = cache_dir(cfg, "sounds", re.sub(r"\W+", "_", name, flags=re.U) or "s")
    try:
        src, info = fetch_audio(cfg, req, work)
    except ClipError as e:
        raise SoundError(str(e)) from e
    dur = min(media_duration(src), MAX_SECONDS)
    mp3 = work / "sound.mp3"
    run_ffmpeg(["-i", str(src), "-t", f"{dur:.2f}", "-vn", "-ac", "2", "-ar", "48000",
                "-af", "loudnorm=I=-18:TP=-2", "-b:a", "160k", str(mp3)], "sound encode")
    probe = work / "probe.mp3"
    run_ffmpeg(["-i", str(mp3), "-t", "60", "-ac", "1", "-b:a", "64k", str(probe)], "sound probe")
    kind, summary = classify(probe)
    slug = "sound-" + (re.sub(r"\W+", "-", name, flags=re.U).strip("-") or "x")
    h = hosting.upload(mp3, f"sounds/{slug}")
    lib = load()
    lib[name] = {"url": h["url"], "public_id": h.get("public_id"), "duration": round(dur, 1),
                 "kind": kind, "summary": summary, "source": info.get("webpage_url") or req.get("url"),
                 "added": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    write_json(PATH, lib)
    log.info("sound %r saved (%.0fs, %s)", name, dur, kind)
    return lib[name]


def remove(name: str) -> bool:
    from .publish import hosting
    lib = load()
    s = lib.pop(name, None)
    if not s:
        return False
    try:
        if s.get("public_id"):
            hosting.delete(s["public_id"])
    except Exception as e:
        log.warning("delete sound %s: %s", name, e)
    write_json(PATH, lib)
    return True
