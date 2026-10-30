"""Quality check of every rendered video, before it is hosted or sent for review.

check(mp4, expect) -> list of problems (empty = fine):
  * 1080x1920 picture and an audio stream
  * length as planned (and Shorts-sized)
  * not mostly black (a broken render / missing footage)
  * not silent (a lost recitation / narration), unless the video is meant to be quiet
The makers raise on a problem, so the usual "drop it and make another" path takes over.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .util import FFMPEG, FFPROBE

W, H = 1080, 1920


class QCError(RuntimeError):
    pass


def check(mp4: Path, expect: float | None = None, allow_silent: bool = False, max_len: float = 61) -> list[str]:
    probs: list[str] = []
    try:
        info = json.loads(subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json",
                                          str(mp4)], capture_output=True, text=True, check=True).stdout)
    except (subprocess.CalledProcessError, ValueError) as e:
        return [f"unreadable file ({e})"]
    streams = info.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not v or (int(v.get("width", 0)), int(v.get("height", 0))) != (W, H):
        probs.append(f"picture is not {W}x{H}")
    if not a:
        probs.append("no audio track")
    dur = float((info.get("format") or {}).get("duration") or 0)
    if not 4 <= dur <= max_len:
        probs.append(f"length {dur:.1f}s is not Shorts-sized")
    if expect and abs(dur - expect) > 1.5:
        probs.append(f"length {dur:.1f}s, planned {expect:.1f}s")
    if probs:
        return probs
    log = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", str(mp4),
                          "-vf", "blackdetect=d=0.8:pix_th=0.08", "-af", "volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    black = sum(float(x) for x in re.findall(r"black_duration:([\d.]+)", log))
    if dur and black / dur > 0.25:
        probs.append(f"{black:.0f}s of {dur:.0f}s is black")
    m = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", log)
    mean = float(m.group(1)) if m and m.group(1) != "-inf" else -99.0
    if not allow_silent and mean < -45:
        probs.append(f"sound is almost silent ({mean:.0f} dB)")
    return probs


def ensure(mp4: Path, expect: float | None = None, allow_silent: bool = False, max_len: float = 61) -> None:
    probs = check(mp4, expect, allow_silent, max_len)
    if probs:
        raise QCError("quality check failed: " + "; ".join(probs))
