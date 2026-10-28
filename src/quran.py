"""Quran text + recitation.

Text never comes from memory: it is fetched from Quran.com API v4 (text_uthmani, with the
word list that matches the recitation word timings) and cross-checked letter-by-letter
against alquran.cloud (quran-uthmani). A mismatch rejects the passage.

Recitation is ayah-by-ayah MP3 from EveryAyah.com, so each ayah's timing is the exact file
duration. Word timings come from Quran.com segments only when Quran.com's audio file for that
reciter is the same recording (duration within 0.15 s).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .util import cache_dir, download, http_json, media_duration, read_json, run_ffmpeg, write_json

log = logging.getLogger(__name__)

QURAN_COM = "https://api.quran.com/api/v4"
ALQURAN_CLOUD = "https://api.alquran.cloud/v1"
EVERYAYAH = "https://everyayah.com/data"

ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


class TextMismatch(Exception):
    pass


@dataclass
class Ayah:
    surah: int
    ayah: int
    text: str                       # full text_uthmani, exactly as returned
    words: list[str]                # same text split into recited words (pause marks attached)
    audio: Path
    duration: float
    word_times: list[tuple[float, float]] | None = None   # seconds, relative to this ayah's file

    @property
    def key(self) -> str:
        return f"{self.surah}:{self.ayah}"


@dataclass
class Recitation:
    reciter_id: str
    reciter_name: str
    surah: int
    surah_name: str
    ayat: list[Ayah] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return sum(a.duration for a in self.ayat)

    @property
    def has_word_times(self) -> bool:
        return all(a.word_times for a in self.ayat)

    @property
    def ref(self) -> str:
        a, b = self.ayat[0].ayah, self.ayat[-1].ayah
        return f"{self.surah}:{a}" if a == b else f"{self.surah}:{a}-{b}"


def to_arabic_digits(n: int) -> str:
    return str(n).translate(ARABIC_DIGITS)


# ---------------------------------------------------------------- comparison
# tatweel, small waw/yeh, rub el hizb, sajdah sign, and standalone hamza (Tanzil writes
# "لَءَايَٰتٍ" where Quran.com writes the hamza as a mark: "لَـَٔايَـٰتٍ")
_DROP = {"ـ", "ۥ", "ۦ", "۞", "۩", "ء"}


def skeleton(text: str) -> str:
    """Letters only: removes all diacritics, Quranic annotation marks and spaces."""
    out = []
    # NFD splits hamza carriers (أ -> ا + ٔ): the two sources encode hamza differently
    for ch in unicodedata.normalize("NFD", text):
        if ch in _DROP or ch.isspace():
            continue
        if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue
        if ch == "ٱ":          # alef wasla -> alef
            ch = "ا"
        out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------- API calls (cached)
def chapters(cfg) -> dict[int, dict]:
    p = cache_dir(cfg, "api") / "chapters.json"
    data = read_json(p, None)
    if data is None:
        data = http_json(f"{QURAN_COM}/chapters", params={"language": "ar"})["chapters"]
        write_json(p, data)
    return {c["id"]: c for c in data}


def _quran_com_verse(cfg, surah: int, ayah: int, recitation_id: int) -> dict:
    p = cache_dir(cfg, "api", "qurancom", str(recitation_id)) / f"{surah:03d}{ayah:03d}.json"
    data = read_json(p, None)
    if data is None:
        data = http_json(
            f"{QURAN_COM}/verses/by_key/{surah}:{ayah}",
            params={"words": "true", "word_fields": "text_uthmani", "fields": "text_uthmani",
                    "audio": recitation_id},
        )["verse"]
        write_json(p, data)
    return data


def _alquran_cloud_text(cfg, surah: int, ayah: int) -> str:
    p = cache_dir(cfg, "api", "alqurancloud") / f"{surah:03d}{ayah:03d}.json"
    data = read_json(p, None)
    if data is None:
        data = http_json(f"{ALQURAN_CLOUD}/ayah/{surah}:{ayah}/quran-uthmani")["data"]
        write_json(p, data)
    if data["surah"]["number"] != surah or data["numberInSurah"] != ayah:
        raise TextMismatch(f"alquran.cloud returned wrong ayah for {surah}:{ayah}")
    return data["text"]


def _segments_to_times(segs: list, n_words: int) -> list[tuple[float, float]] | None:
    times: list[tuple[float, float] | None] = [None] * n_words
    for s in segs:
        if len(s) == 4:
            pos, start, end = s[1], s[2], s[3]
        elif len(s) == 3:
            pos, start, end = s
        else:
            return None
        if not (1 <= pos <= n_words):
            return None
        times[pos - 1] = (start / 1000.0, end / 1000.0)
    if any(t is None for t in times):
        return None
    for (a0, _), (b0, _) in zip(times, times[1:]):
        if b0 < a0:
            return None
    return times  # type: ignore[return-value]


def _audio_url(u: str) -> str:
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("http"):
        return u
    return "https://verses.quran.com/" + u.lstrip("/")


# ---------------------------------------------------------------- main entry
def load_text(cfg, surah: int, ayah: int, recitation_id: int = 6) -> tuple[str, list[str], dict]:
    """Verified ayah text + word list (and the raw Quran.com verse for its audio segments)."""
    v = _quran_com_verse(cfg, surah, ayah, recitation_id)
    if v["verse_key"] != f"{surah}:{ayah}":
        raise TextMismatch(f"Quran.com returned {v['verse_key']} for {surah}:{ayah}")
    # Displayed text = Quran.com word list (it carries the full mark set, e.g. ۭ for iqlab,
    # that the verse-level field omits). Pause marks come attached as "word ۚ".
    words = [w["text_uthmani"] for w in v["words"] if w["char_type_name"] == "word"]
    text = " ".join(words)
    if skeleton(text) != skeleton(v["text_uthmani"]):
        raise TextMismatch(f"{surah}:{ayah}: word list does not rebuild the verse text")

    if cfg["quran"].get("cross_check", True):
        other = skeleton(_alquran_cloud_text(cfg, surah, ayah))
        if ayah == 1 and surah not in (1, 9):
            # alquran.cloud prefixes the basmala to ayah 1; strip it (basmala text = its 1:1)
            basmala = skeleton(_alquran_cloud_text(cfg, 1, 1))
            if other.startswith(basmala):
                other = other[len(basmala):]
        if skeleton(text) != other:
            raise TextMismatch(f"{surah}:{ayah}: Quran.com and alquran.cloud letters differ")
    return text, words, v


def load_ayah(cfg, reciter: dict, surah: int, ayah: int) -> Ayah:
    # quran_com_id is optional: without it the text still comes from Quran.com (via Husary's
    # recitation id) but there is no word-level timing, so words are not highlighted one by one
    qc_id = reciter.get("quran_com_id")
    text, words, v = load_text(cfg, surah, ayah, qc_id or 6)
    if not qc_id:
        v = {**v, "audio": {}}
    folder = reciter["everyayah_folder"]
    audio = download(f"{EVERYAYAH}/{folder}/{surah:03d}{ayah:03d}.mp3",
                     cache_dir(cfg, "audio", folder) / f"{surah:03d}{ayah:03d}.mp3")
    dur = media_duration(audio)

    word_times = None
    a = v.get("audio") or {}
    if a.get("segments"):
        qc_url = _audio_url(a["url"])
        qc_file = cache_dir(cfg, "audio", "qurancom", str(reciter["quran_com_id"])) / f"{surah:03d}{ayah:03d}.mp3"
        try:
            download(qc_url, qc_file)
            if abs(media_duration(qc_file) - dur) <= 0.15:
                word_times = _segments_to_times(a["segments"], len(words))
            else:
                log.debug("%s:%s Quran.com audio differs from EveryAyah; no word timing", surah, ayah)
        except Exception as e:  # word timing is optional
            log.warning("word timing unavailable for %s:%s (%s)", surah, ayah, e)
    if word_times and word_times[-1][1] > dur + 0.2:
        word_times = None
    return Ayah(surah, ayah, text, words, audio, dur, word_times)


def load_recitation(cfg, reciter_id: str, surah: int, first: int, last: int) -> Recitation:
    reciter = cfg["reciters"][reciter_id]
    chap = chapters(cfg)[surah]
    if not (1 <= first <= last <= chap["verses_count"]):
        raise ValueError(f"bad range {surah}:{first}-{last}")
    name = reciter["name_ar"] + (f" · {reciter['label_ar']}" if reciter.get("label_ar") else "")
    rec = Recitation(reciter_id, name, surah, chap["name_arabic"])
    for n in range(first, last + 1):
        if reciter.get("source") == "mp3quran_aligned":
            from .align import load_ayah_aligned
            rec.ayat.append(load_ayah_aligned(cfg, reciter_id, surah, n))
        else:
            rec.ayat.append(load_ayah(cfg, reciter, surah, n))
    return rec


def build_recitation_audio(rec: Recitation, out: Path) -> Path:
    """Decode each ayah to WAV, re-measure its exact decoded length (MP3 padding makes the
    container duration drift), then concatenate with no gaps. Updates Ayah.duration."""
    parts = []
    for a in rec.ayat:
        wav = out.parent / f"_ayah_{a.surah:03d}{a.ayah:03d}.wav"
        run_ffmpeg(["-i", str(a.audio), "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(wav)],
                   "decode ayah")
        a.duration = media_duration(wav)
        parts.append(wav)
    args: list[str] = []
    for p in parts:
        args += ["-i", str(p)]
    n = len(parts)
    fc = "".join(f"[{i}:a]" for i in range(n)) + f"concat=n={n}:v=0:a=1[out]"
    run_ffmpeg([*args, "-filter_complex", fc, "-map", "[out]", "-c:a", "pcm_s16le", str(out)],
               "concat recitation")
    for p in parts:
        p.unlink(missing_ok=True)
    return out
