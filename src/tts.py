"""Hadith narration with Gemini text-to-speech (free tier, same GEMINI_API_KEY as the agent).

Every reading is checked before it is used: Whisper transcribes the audio and it must match the
hadith text (spelling-insensitive) — a model that skips, adds or misreads words (one test model
read the instructions aloud) is rejected and the next model is tried. If none passes, the caller
falls back to a text-only video. Returns the audio and Whisper's word timings for syncing screens.
"""
from __future__ import annotations

import base64
import difflib
import logging
import os
import re
import subprocess
from pathlib import Path

import requests

from .util import FFMPEG

log = logging.getLogger("quran-shorts.tts")
BASE = "https://generativelanguage.googleapis.com/v1beta"
STYLE = "بصوت رجل هادئ وخاشع ووقور، بتمهّل ووضوح ومخارج حروف سليمة، اقرأ:"


class TTSError(Exception):
    pass


def _key(w: str) -> str:
    w = re.sub(r"[ؐ-ًؚ-ٰٟـ«»(),.،:؛!?\-]", "", w)
    return w.translate(str.maketrans("أإآٱ", "اااا")).replace("ى", "ي").replace("ة", "ه")


def match(text: str, heard: str) -> float:
    a = [_key(w) for w in text.split() if _key(w)]
    b = [_key(w) for w in heard.split() if _key(w)]
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _speak(model: str, voice: str, text: str, out: Path) -> Path:
    r = requests.post(f"{BASE}/models/{model}:generateContent", timeout=180,
                      headers={"x-goog-api-key": os.environ.get("GEMINI_API_KEY", "")},
                      json={"contents": [{"parts": [{"text": f"{STYLE}\n{text}"}]}],
                            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}})
    if not r.ok:
        raise TTSError(f"{model}: HTTP {r.status_code} {r.text[:150]}")
    part = r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
    raw = base64.b64decode(part["data"])
    mime = part.get("mimeType", "").lower()
    src = out.with_suffix(".raw")
    src.write_bytes(raw)
    inp = ["-i", str(src)] if "wav" in mime else ["-f", "s16le", "-ar", re.search(r"rate=(\d+)", mime).group(1)
                                                   if "rate=" in mime else "24000", "-ac", "1", "-i", str(src)]
    subprocess.run([FFMPEG, "-v", "error", "-y", *inp, "-ar", "48000", "-ac", "1", str(out)], check=True)
    return out


def narrate(cfg, text: str, work: Path) -> tuple[Path, list[dict], str]:
    """(wav, whisper words [{w, s, e}], model) for a reading that passed the check."""
    from . import asr
    h = cfg.get("hadith") or {}
    if not os.environ.get("GEMINI_API_KEY"):
        raise TTSError("GEMINI_API_KEY missing")
    need = float(h.get("tts_min_match", 0.9))
    errors = []
    for model in h.get("tts_models") or ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]:
        for attempt in range(2):
            out = work / f"voice_{model}_{attempt}.wav"
            try:
                _speak(model, h.get("voice", "Charon"), text, out)
            except (TTSError, requests.RequestException, KeyError, subprocess.CalledProcessError) as e:
                errors.append(str(e)[:120])
                break
            words = asr.transcribe(cfg, out)
            heard = " ".join(w["w"] for w in words)
            score = match(text, heard)
            log.info("tts %s try %d: %.0f%% match", model, attempt + 1, score * 100)
            if score >= need:
                return out, words, model
            errors.append(f"{model}: only {score:.0%} matched")
    raise TTSError("; ".join(errors) or "no TTS model")
