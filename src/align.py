"""Ayah timing for recordings without published timings (e.g. Minshawi's 1967 recording on
mp3quran.net): recognise the whole surah, align the recognised words to the API text of every
ayah, then cut each ayah into its own WAV so the normal video pipeline can use it.

An ayah is accepted only if enough of its words were matched; videos made from aligned
recordings are always marked for manual review before publishing.
"""
from __future__ import annotations

import difflib
import logging
import re
import unicodedata
from pathlib import Path

from . import asr, quran
from .util import cache_dir, download, http_json, media_duration, read_json, run_ffmpeg, write_json

log = logging.getLogger(__name__)

MIN_MATCH = 0.6     # fraction of an ayah's words that must be matched


class AlignError(Exception):
    pass


def norm(word: str) -> str:
    """Loose spelling key: Uthmani and plain spellings differ mostly in alef/hamza/ya forms."""
    s = quran.skeleton(word)
    s = re.sub("[اأإآٱءؤئ]", "", s.replace("ؤ", "و").replace("ئ", "ي"))
    s = s.replace("ى", "ي").replace("ة", "ه")
    return "".join(ch for ch in s if unicodedata.category(ch).startswith("L"))


def _surah_list(cfg, reciter: dict) -> set[int]:
    p = cache_dir(cfg, "api") / f"mp3quran_{reciter['mp3quran_moshaf']}.json"
    data = read_json(p, None)
    if data is None:
        data = http_json("https://mp3quran.net/api/v3/reciters", params={"language": "ar"})
        write_json(p, data)
    for r in data["reciters"]:
        for m in r["moshaf"]:
            if m["id"] == reciter["mp3quran_moshaf"]:
                return {int(x) for x in m["surah_list"].split(",") if x}
    raise AlignError("moshaf not found on mp3quran")


def surah_timings(cfg, reciter_id: str, surah: int) -> dict:
    """{ayah: {"start", "end", "match", "words": [(s, e) | None]}} for the whole surah (cached)."""
    reciter = cfg["reciters"][reciter_id]
    out_p = cache_dir(cfg, "align", reciter_id) / f"{surah:03d}.json"
    cached = read_json(out_p, None)
    if cached is not None:
        return {int(k): v for k, v in cached["ayat"].items()}
    if surah not in _surah_list(cfg, reciter):
        raise AlignError(f"surah {surah} not in this recording")
    audio = download(f"{reciter['mp3quran_server']}{surah:03d}.mp3",
                     cache_dir(cfg, "rare", reciter_id) / f"{surah:03d}.mp3")
    rec_words = asr.transcribe(cfg, audio)

    n_ayat = quran.chapters(cfg)[surah]["verses_count"]
    ref: list[tuple[int, int]] = []        # (ayah, word index) per reference word
    ref_keys: list[str] = []
    ayah_words: dict[int, list[str]] = {}
    for a in range(1, n_ayat + 1):
        _, ws, _ = quran.load_text(cfg, surah, a)
        ayah_words[a] = ws
        for i, w in enumerate(ws):
            ref.append((a, i))
            ref_keys.append(norm(w))
    hyp_keys = [norm(w["w"]) for w in rec_words]

    sm = difflib.SequenceMatcher(a=ref_keys, b=hyp_keys, autojunk=False)
    match: dict[tuple[int, int], dict] = {}
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            match[ref[blk.a + k]] = rec_words[blk.b + k]

    result: dict[int, dict] = {}
    for a, ws in ayah_words.items():
        times = [match.get((a, i)) for i in range(len(ws))]
        got = [t for t in times if t]
        result[a] = {"match": round(len(got) / len(ws), 3),
                     "first": got[0]["s"] if got else None, "last": got[-1]["e"] if got else None,
                     "words": [(t["s"], t["e"]) if t else None for t in times]}
    # boundaries: middle of the silence between consecutive ayat
    total = media_duration(audio)
    keys = sorted(result)
    for i, a in enumerate(keys):
        r = result[a]
        if r["first"] is None:
            r["start"] = r["end"] = None
            continue
        prev = next((result[k] for k in reversed(keys[:i]) if result[k]["last"] is not None), None)
        nxt = next((result[k] for k in keys[i + 1:] if result[k]["first"] is not None), None)
        r["start"] = round(max(0.0, (prev["last"] + r["first"]) / 2 if prev else r["first"] - 0.3), 3)
        r["end"] = round((r["last"] + nxt["first"]) / 2 if nxt else min(total, r["last"] + 0.8), 3)
    write_json(out_p, {"surah": surah, "audio": str(audio), "ayat": result})
    return result


def load_ayah_aligned(cfg, reciter_id: str, surah: int, ayah: int) -> quran.Ayah:
    """Same contract as quran.load_ayah, but audio is cut from the aligned surah recording."""
    text, words, _ = quran.load_text(cfg, surah, ayah)
    t = surah_timings(cfg, reciter_id, surah)[ayah]
    if t["start"] is None or t["match"] < MIN_MATCH:
        raise AlignError(f"{surah}:{ayah} alignment too weak ({t['match']:.0%} words matched)")
    src = cache_dir(cfg, "rare", reciter_id) / f"{surah:03d}.mp3"
    cut = cache_dir(cfg, "rare", reciter_id, "ayat") / f"{surah:03d}{ayah:03d}.wav"
    if not cut.exists():
        run_ffmpeg(["-ss", f"{t['start']:.3f}", "-to", f"{t['end']:.3f}", "-i", str(src),
                    "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(cut)], "cut ayah")
    dur = media_duration(cut)
    word_times = None
    if all(t["words"]) and len(t["words"]) == len(words):
        word_times = [(max(0.0, s - t["start"]), max(0.0, e - t["start"])) for s, e in t["words"]]
    return quran.Ayah(surah, ayah, text, words, cut, dur, word_times)
