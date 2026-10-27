"""Local speech recognition (faster-whisper on CPU). Used to time recordings that have no
published ayah timings and, in phase 2, to transcribe lectures. Results are cached per file.

The model's text is used for timing/alignment only — Quran text on screen always comes from
the API, never from recognition.
"""
from __future__ import annotations

import hashlib
import logging
import subprocess
import time
from pathlib import Path

import numpy as np

from .util import FFMPEG, cache_dir, read_json, write_json

log = logging.getLogger(__name__)

_model = None


def _load_model(cfg):
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        a = cfg.get("asr", {})
        t = time.time()
        _model = WhisperModel(a.get("model", "large-v3-turbo"), device="cpu",
                              compute_type=a.get("compute_type", "int8"),
                              cpu_threads=a.get("cpu_threads", 8))
        log.info("whisper model loaded in %.0fs", time.time() - t)
    return _model


def _decode(path: Path) -> np.ndarray:
    # decode with FFmpeg ourselves (faster-whisper's PyAV path breaks with newer PyAV)
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-ac", "1", "-ar", "16000",
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def _chunks(audio: np.ndarray, sr: int = 16000, target: float = 50.0):
    """Split long audio into ~50 s pieces at the quietest 0.1 s inside a +-8 s window. On this
    CPU build, segments past ~1-2 min make MKL fail to allocate; cutting at a pause means no
    word is split."""
    n = len(audio)
    start = 0
    while start < n:
        end = start + int(target * sr)
        if end + int(10 * sr) >= n:
            yield start / sr, audio[start:]
            return
        lo, hi = end - int(8 * sr), end + int(8 * sr)
        win = int(0.1 * sr)
        frames = audio[lo:hi][: (hi - lo) // win * win].reshape(-1, win)
        cut = lo + int(np.argmin((frames ** 2).mean(axis=1))) * win + win // 2
        yield start / sr, audio[start:cut]
        start = cut


def transcribe(cfg, path: Path) -> list[dict]:
    """Word list [{w, s, e, p}] (text, start, end, probability), cached by file content."""
    st = path.stat()
    key = hashlib.sha1(f"{path.name}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]
    cp = cache_dir(cfg, "asr") / f"{path.stem}_{key}.json"
    data = read_json(cp, None)
    if data is not None:
        return data["words"]
    audio = _decode(path)
    model = _load_model(cfg)
    t = time.time()
    words = []
    for off, chunk in _chunks(audio):
        segs, _ = model.transcribe(chunk, language="ar", word_timestamps=True, vad_filter=True,
                                   beam_size=5, condition_on_previous_text=False)
        for s in segs:
            for w in s.words or []:
                words.append({"w": w.word.strip(), "s": round(off + w.start, 3),
                              "e": round(off + w.end, 3), "p": round(w.probability, 3)})
    dur = len(audio) / 16000
    log.info("transcribed %s: %.0fs audio in %.0fs", path.name, dur, time.time() - t)
    write_json(cp, {"file": str(path), "duration": dur, "words": words})
    return words
