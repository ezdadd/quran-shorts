"""Pick a nature-ambience file that matches the footage category. No music, ever."""
from __future__ import annotations

import logging
import random
from pathlib import Path

from .util import ROOT

log = logging.getLogger(__name__)

AUDIO_EXT = {".mp3", ".wav", ".ogg", ".flac", ".m4a"}


def _files(folders: list[str]) -> list[Path]:
    out: list[Path] = []
    for folder in folders:
        d = ROOT / "assets" / "ambience" / folder
        if d.is_dir():
            out += [p for p in d.iterdir() if p.suffix.lower() in AUDIO_EXT]
    return out


def pick(cfg, category: str, rng: random.Random) -> Path | None:
    acfg = cfg["ambience"]
    if not acfg.get("enabled", True):
        return None
    files = _files(acfg["map"].get(category, []))
    if not files:                           # empty folder / no mapping (space, graphics...): a calm general
        files = _files(acfg.get("fallback") or ["wind", "rain", "forest"])  # sound, never a silent video
        if files:
            log.info("no ambience for %s — using a general calm one", category)
    if not files:
        log.warning("no ambience files at all in assets/ambience/")
        return None
    return rng.choice(sorted(files))
