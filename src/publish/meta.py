"""Per-platform caption + hashtags for a generated video."""
from __future__ import annotations

import random

from .. import quran

PLATFORMS = ("youtube", "tiktok", "instagram", "facebook")


def quran_meta(cfg, rng: random.Random, rec: quran.Recitation, credits: list[str]) -> dict:
    c = cfg["captions"]
    a, b = rec.ayat[0].ayah, rec.ayat[-1].ayah
    ayat = quran.to_arabic_digits(a) if a == b else f"{quran.to_arabic_digits(a)}-{quran.to_arabic_digits(b)}"
    ref_line = f"سورة {rec.surah_name} ({ayat})"
    reciter_line = f"بصوت القارئ {rec.reciter_name}"
    lo, hi = c["hashtag_count"]
    out = {}
    for p in PLATFORMS:
        n = rng.randint(lo, hi)
        tags = list(c["hashtags_core"][:2]) + rng.sample(c["hashtags_pool"], max(0, n - 2))
        rng.shuffle(tags)
        if p == "youtube" and "#shorts" not in tags:
            tags.append("#shorts")
        opener = rng.choice(c["openers"])
        body = f"{opener}\n{ref_line}\n{reciter_line}\n\n{' '.join(tags)}"
        out[p] = {"caption": body, "hashtags": tags}
    out["youtube"]["title"] = f"{rng.choice(c['openers'])} | {ref_line} - {rec.reciter_name}"[:100]
    if credits:
        for p in PLATFORMS:
            out[p]["caption"] += "\n\n" + " · ".join(credits)
    return out
