"""Pick a nature-ambience file that matches the footage category. No music, ever."""
from __future__ import annotations

import logging
import random
from pathlib import Path

from .util import ROOT

log = logging.getLogger(__name__)

AUDIO_EXT = {".mp3", ".wav", ".ogg", ".flac", ".m4a"}


def pick(cfg, category: str, rng: random.Random) -> Path | None:
    acfg = cfg["ambience"]
    if not acfg.get("enabled", True):
        return None
    files: list[Path] = []
    for folder in acfg["map"].get(category, []):
        d = ROOT / "assets" / "ambience" / folder
        if d.is_dir():
            files += [p for p in d.iterdir() if p.suffix.lower() in AUDIO_EXT]
    if not files:
        log.warning("no ambience files for %s (assets/ambience/%s/) — video will have recitation only",
                    category, "|".join(acfg["map"].get(category, [])))
        return None
    return rng.choice(sorted(files))
