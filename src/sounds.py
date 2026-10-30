"""Our sounds library: audio you share from Facebook / Instagram / YouTube / TikTok in Telegram with a
caption "صوت: <name>". Saved on Cloudinary (quran-shorts/sounds/), listed in data/sounds.json,
and used as the quiet background under narrated hadiths (config sounds.use_in).

Before a sound is kept, Gemini listens to it: anything with musical instruments / music is refused.
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


class SoundError(Exception):
    pass


def load() -> dict:
    return read_json(PATH, {})


def _music_check(mp3: Path) -> tuple[bool, str]:
    """(ok, reason) — Gemini listens to the first minute."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return True, "not checked (no key)"
    prompt = ('Listen to this audio. Answer JSON only: {"music": true|false, "reason": "<short English>"}. '
              'music=true if there is ANY musical instrument, beat, drum, synth or background music. '
              'Voice only (speech, Quran, nasheed sung without instruments), nature sounds, rain, wind, birds '
              'and silence are music=false.')
    data = base64.b64encode(mp3.read_bytes()).decode()
    for model in ("gemini-3.5-flash", "gemini-3.7-flash", "gemini-flash-latest"):
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                              headers={"x-goog-api-key": key}, timeout=120,
                              json={"contents": [{"parts": [{"text": prompt},
                                                            {"inline_data": {"mime_type": "audio/mp3", "data": data}}]}],
                                    "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}})
            if not r.ok:
                continue
            v = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
            return not v.get("music"), str(v.get("reason", ""))[:120]
        except (requests.RequestException, KeyError, ValueError) as e:
            log.warning("music check %s: %s", model, e)
    return True, "not checked (models busy)"


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
    ok, reason = _music_check(probe)
    if not ok:
        raise SoundError(f"الصوت ده فيه موسيقى/آلات ({reason}) — مش هيتحفظ.")
    slug = "sound-" + (re.sub(r"\W+", "-", name, flags=re.U).strip("-") or "x")
    h = hosting.upload(mp3, f"sounds/{slug}")
    lib = load()
    lib[name] = {"url": h["url"], "public_id": h.get("public_id"), "duration": round(dur, 1),
                 "source": info.get("webpage_url") or req.get("url"), "check": reason,
                 "added": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    write_json(PATH, lib)
    log.info("sound %r saved (%.0fs)", name, dur)
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


def pick(cfg, use: str, rng, work: Path) -> Path | None:
    """A random library sound for `use` ("hadith"), downloaded to work/, or None."""
    if use not in ((cfg.get("sounds") or {}).get("use_in") or []):
        return None
    lib = load()
    if not lib or rng.random() > float((cfg.get("sounds") or {}).get("chance", 0.7)):
        return None
    name = rng.choice(sorted(lib))
    from .util import download
    safe = re.sub(r"\W+", "_", name, flags=re.U)
    try:
        return download(lib[name]["url"], work / f"bed_{safe}.mp3")
    except Exception as e:
        log.warning("sound %s unavailable: %s", name, e)
        return None
