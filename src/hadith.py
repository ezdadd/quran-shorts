"""Hadith Shorts — authentic hadiths only, shown as text over our own calm footage.

    python -m src.hadith --top-up --max-new 1          # every 3 h (daily.yml): today's next hadith
    python -m src.hadith --generate 3 --topic "التوبة والرجاء"     # from the /generate menu

Source: data/hadiths.json, a snapshot of HadeethEnc (hadeethenc.com — Encyclopedia of Translated
Prophetic Hadiths; build it with `python -m tools.fetch_hadiths`). Only صحيح / حسن hadiths short
enough for one Short are used. The text, narration intro, collector and grade are shown exactly as
the source gives them — nothing is rephrased. No voice is added: calm nature ambience only.

Each video is hosted, put in the publish queue and sent to Telegram for review (like link clips);
if you don't act for publishing.auto_approve_hours it is checked and approved automatically.
"""
from __future__ import annotations

import argparse
import logging
import random
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import ambience, footage, templates
from .render import (CSS_STYLE, FPS, H, W, Frame, build_background, readable_color, write_concat,
                     zone_luma)
from .textrender import TextRenderer
from .util import ROOT, cache_dir, load_config, path, read_json, run_ffmpeg, setup_logging, write_json

log = logging.getLogger("quran-shorts.hadith")
DATA = ROOT / "data" / "hadiths.json"
OUT = ROOT / "output" / "hadith"
QUEUE = ROOT / "queue"
CENTER_Y, MAX_H, MAX_LINES = 960, 820, 5
INTRO_Y, SOURCE_Y = 300, 1560
COOLDOWN_DAYS = 120
TAGS = ["#حديث", "#حديث_شريف", "#السنة_النبوية", "#رسول_الله", "#صلى_الله_عليه_وسلم", "#موعظة", "#اسلام",
        "#تذكير", "#الرسول"]


class HadithError(Exception):
    pass


# ---------------------------------------------------------------- choosing
def load() -> list[dict]:
    """Usable hadiths. Entries that bundle two narrations ("الحديث الأول ... الحديث الثاني") are left
    out: one Short = one hadith with one clear source line."""
    d = read_json(DATA, {"hadiths": {}})
    return [h for h in d["hadiths"].values() if h.get("keep") and "الحديث الثاني" not in (h.get("attribution") or "")
            and not re.search(r"\sوَ?عَنْ\s", " " + h.get("text", ""))]


def topics() -> list[str]:
    seen: list[str] = []
    for h in load():
        for t in h.get("topics") or []:
            if t not in seen:
                seen.append(t)
    return seen


def pick(history: dict, rng: random.Random, n: int, topic: str | None, today: date) -> list[dict]:
    cutoff = (today - timedelta(days=COOLDOWN_DAYS)).isoformat()
    used = {k for k, d in history.get("hadith_used", {}).items() if d >= cutoff}
    pool = [h for h in load() if h["id"] not in used and (not topic or topic in (h.get("topics") or []))]
    rng.shuffle(pool)
    return pool[:n]


def split_intro(h: dict) -> tuple[str, str]:
    """(narration intro for the top line, the words themselves for the card). When the source gives
    no separate intro, everything before the opening « is the intro; nothing is reworded."""
    intro, body = (h.get("intro") or "").strip(), h["text"].strip()
    if not intro and "«" in body and body.index("«") > 0:
        i = body.index("«")
        intro, body = body[:i].strip(), body[i:].strip()
    return intro, body


def reading_seconds(words: int) -> float:
    """Comfortable on-screen reading pace for Arabic with tashkeel, plus time to take it in."""
    return max(12.0, min(58.0, 3.0 + words * 0.55))


# ---------------------------------------------------------------- rendering
def _pages(tr: TextRenderer, words: list[str], kw: dict) -> list[list[str]]:
    info = tr.layout_ayah(words, None, max_h=100000, **kw)
    if info["lines"] <= MAX_LINES:
        return [words]
    n_pages = -(-info["lines"] // MAX_LINES)
    per = -(-len(words) // n_pages)
    out, i = [], 0
    while i < len(words):           # break after a comma / full stop near the even split if possible
        j = min(len(words), i + per)
        if j < len(words):
            near = [k for k in range(max(i + 3, j - 4), min(len(words), j + 4)) if re.search(r"[،,.:؛]$", words[k - 1])]
            if near:
                j = min(near, key=lambda k: abs(k - j))
        out.append(words[i:j])
        i = j
    return out


LEAD, TAIL = 0.8, 1.8


def _voice(cfg, h: dict, work: Path) -> tuple[Path, list[dict], str, int] | None:
    """Narration that passed the Whisper check, or None (-> text-only video). Also returns how many
    spoken words come before the Prophet's words (the intro), to time the screens."""
    if not (cfg.get("hadith") or {}).get("narrate", True):
        return None
    from . import tts
    intro, body = split_intro(h)
    attr = h.get("attribution") or ""
    spoken = " ".join(x for x in (intro, body, attr if len(attr.split()) <= 6 else "") if x)
    try:
        wav, words, model = tts.narrate(cfg, spoken, work)
    except Exception as e:                  # no voice that reads it exactly: text only, never a misreading
        log.warning("narration failed (%s) — text-only video", e)
        return None
    return wav, words, model, len(intro.split())


def render(cfg, h: dict, vid: str, rng: random.Random, history: dict) -> tuple[Path, dict]:
    from .util import media_duration
    work = cache_dir(cfg, "work", vid)
    voice = _voice(cfg, h, work)
    if voice and LEAD + media_duration(voice[0]) + TAIL > 59:
        log.warning("narration too long for a Short — text-only video")
        voice = None
    total = round(LEAD + media_duration(voice[0]) + TAIL, 2) if voice else round(reading_seconds(h["words"]), 2)
    tpl = templates.choose_clip(cfg, rng)
    tf = cfg["templates"]["fonts"]               # fonts made for full tashkeel (same as the ayat)
    font = templates._pick(rng, {k: v["weight"] for k, v in tf.items()})
    tpl.font, tpl.position, tpl.animation, tpl.show_title = font, "center", "whole", False
    tpl.label_font = "Amiri-Regular.ttf"         # intro / source lines must be easy to read
    tpl.color = templates._pick(rng, cfg["templates"].get("text_colors") or {"white": 1})
    category = footage.pick_category(cfg, rng)
    clips = footage.get_clips(cfg, category, total, history, rng)
    bg = build_background(cfg, clips, total, tpl, work / "bg.mp4")
    luma = zone_luma(bg, total, CENTER_Y, work)
    color, style = readable_color(tpl, luma)
    f = tf[font]
    kw = dict(style=CSS_STYLE[style], color=cfg["templates"]["colors"][color], accent="#F2D27A",
              size=int(f["size"] * 0.82), center_y=CENTER_Y, open_bracket=False, close_bracket=False,
              attach_marks=f.get("attach_marks", False), weight=f.get("css_weight", 400), min_size=52)
    frames: list[Frame] = []
    with TextRenderer(cfg) as tr:
        tr.use_fonts(f["file"], label_font=tpl.label_font)
        intro, body = split_intro(h)
        words = body.split()
        pages = _pages(tr, words, kw)
        if voice:                           # each screen appears when the voice reaches its words
            heard, n_intro = voice[1], voice[3]
            n_spoken = n_intro + len(words)
            def at(i: int) -> float:
                k = min(len(heard) - 1, round(i * len(heard) / max(1, n_spoken)))
                return LEAD + heard[k]["s"] if heard else LEAD
            starts, i = [], n_intro
            for pw in pages:
                starts.append(at(i) - 0.25)
                i += len(pw)
            starts[0] = min(starts[0], LEAD + 0.3)      # first screen right away, under the intro
            ends = starts[1:] + [total - 0.5]
        else:
            starts, t, span = [], 0.6, total - 1.4
            for pw in pages:
                starts.append(t)
                t += span * len(pw) / len(words)
            ends = starts[1:] + [total - 0.8]
        for pw, a, b_ in zip(pages, starts, ends):
            info = tr.layout_ayah(pw, None, max_h=MAX_H, **kw)
            fr = Frame(tr.state(info).image, a, b_)    # one screen at a time, faded in / out
            fr.fade_in = fr.fade_out = True
            frames.append(fr)
        concat = write_concat(frames, total, work, tpl.entrance)
        b = cfg.get("branding", {})
        src_line = " · ".join(x for x in (h.get("attribution"), h.get("grade")) if x)
        labels = [{"text": intro, "y": INTRO_Y, "size": 44},
                  {"text": src_line, "y": SOURCE_Y, "size": 42, "pill": True},
                  {"text": b.get("handle", ""), "y": b.get("y", 1735), "size": b.get("size", 32), "pill": True}]
        lab = work / "_labels.png"
        tr.labels([x for x in labels if x["text"]]).save(lab)

    from . import sounds                     # one of our library sounds under the narration, else nature
    amb = (sounds.pick(cfg, "hadith", rng, work) if voice else None) or ambience.pick(cfg, category, rng)
    out = OUT / f"{vid}.mp4"
    OUT.mkdir(parents=True, exist_ok=True)
    args = ["-i", str(bg), "-f", "concat", "-safe", "0", "-i", str(concat), "-loop", "1", "-t", f"{total:.3f}",
            "-i", str(lab)]
    if amb:
        args += ["-stream_loop", "-1", "-i", str(amb)]
    else:                                   # platforms want an audio track: quiet silence
        args += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    bed = (f"[3:a]atrim=0:{total:.3f},asetpts=N/SR/TB,aresample=48000,aformat=channel_layouts=stereo,"
           f"loudnorm=I=-20:TP=-2,afade=t=in:d=1.5,afade=t=out:st={total - 2:.3f}:d=2")
    fc = [f"[1:v]fps={FPS},format=rgba[txt]", "[2:v]format=rgba,fade=t=in:st=0.2:d=0.6:alpha=1[lab]",
          # bright footage: darken the whole picture a little (there is no card behind the words)
          ("[0:v]colorchannelmixer=rr=.72:gg=.72:bb=.72[bg0];[bg0]" if style == "dim" else "[0:v]")
          + "[txt]overlay=0:0:format=auto[v1]",
          f"[v1][lab]overlay=0:0:format=auto,fade=t=in:st=0:d=0.5,fade=t=out:st={total - 0.7:.3f}:d=0.7,"
          f"format=yuv420p[vout]"]
    if voice:                               # the narration on top, nature sounds low underneath
        args += ["-i", str(voice[0])]
        ms = int(LEAD * 1000)
        lo, hi = cfg["ambience"]["level_db"]
        fc += [f"{bed},volume={rng.uniform(lo, hi):.1f}dB[amb]",
               f"[4:a]adelay={ms}|{ms},apad=whole_dur={total:.3f},aresample=48000,aformat=channel_layouts=stereo,"
               f"loudnorm=I=-16:TP=-1.5:LRA=11[vo]",
               f"[vo][amb]amix=inputs=2:normalize=0:duration=first,loudnorm=I=-14:TP=-1.5:LRA=11,"
               f"aresample=48000,atrim=0:{total:.3f}[aout]"]
    else:
        fc.append(f"{bed}[aout]")
    v = cfg["video"]
    run_ffmpeg([*args, "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]", "-t", f"{total:.3f}",
                "-r", str(FPS), "-c:v", "libx264", "-preset", v["preset"], "-crf", str(v["crf"]),
                "-maxrate", v["maxrate"], "-bufsize", v["bufsize"], "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-b:a", v["audio_bitrate"], "-movflags", "+faststart", str(out)], "hadith compose")
    look = tpl.to_dict() | {"applied_color": color, "applied_background": style, "text_zone_luma": round(luma, 3)}
    return out, {"duration": total, "look": look, "footage": [c.id for c in clips], "category": category,
                 "ambience": amb.name if amb else None,
                 "narration": {"model": voice[2], "voice": (cfg.get("hadith") or {}).get("voice", "Charon")}
                 if voice else None}


def captions(cfg, rng: random.Random, h: dict) -> dict:
    """The Prophet's ﷺ words first (what people search for), then collector + grade."""
    src_line = " · ".join(x for x in (h.get("attribution"), h.get("grade")) if x)
    words = split_intro(h)[1]
    out = {}
    for p in ("youtube", "tiktok", "instagram", "facebook"):
        tags = rng.sample(TAGS, 4)
        if p == "youtube":
            tags.append("#shorts")
        body = f"قال رسول الله ﷺ:\n{words}\n{src_line}\n\n{' '.join(tags)}"
        out[p] = {"caption": body, "hashtags": tags}
    short = re.sub(r"[«»]", "", words).rstrip(".،").split()
    out["youtube"]["title"] = ("حديث شريف | " + " ".join(short[:12]) + ("…" if len(short) > 12 else ""))[:100]
    return out


# ---------------------------------------------------------------- making + queueing
def make(cfg, n: int, topic: str | None = None) -> list[Path]:
    from .clips import publish_prep
    tz = ZoneInfo(cfg["timezone"])
    today = datetime.now(tz).date()
    hp = path(cfg, "history")
    history = read_json(hp, {})
    rng = random.Random()
    chosen = pick(history, rng, n, topic, today)
    if not chosen:
        raise HadithError("مفيش أحاديث جديدة متاحة" + (f" في «{topic}»" if topic else ""))
    queued = []
    for h in chosen:
        k = 1
        while (QUEUE / f"h-{today:%Y%m%d}-{k:02d}.json").exists() or (OUT / f"h-{today:%Y%m%d}-{k:02d}.json").exists():
            k += 1
        vid = f"h-{today:%Y%m%d}-{k:02d}"
        try:
            mp4, info = render(cfg, h, vid, rng, history)
        except Exception as e:              # a broken render: skip it, take the next hadith
            log.error("[%s] render failed (%s), skipping hadith %s", vid, e, h["id"])
            continue
        rec = {"id": vid, "type": "hadith", "kind": "hadith", "date": today.isoformat(),
               "file": str(mp4.relative_to(ROOT)).replace("\\", "/"), "duration": info["duration"],
               "hadith": {k_: h.get(k_) for k_ in ("id", "title", "intro", "text", "attribution", "grade", "source")},
               "topic": topic or (h.get("topics") or [""])[0], "template": info["look"],
               "footage": info["footage"], "ambience": info["ambience"], "narration": info["narration"],
               "platforms": captions(cfg, rng, h), "published": {}, "approved": False}
        rp = OUT / f"{vid}.json"
        write_json(rp, rec)
        queued.append(publish_prep(cfg, rp))
        history.setdefault("hadith_used", {})[h["id"]] = today.isoformat()
        for c in info["footage"]:
            history.setdefault("footage_used", {})[c] = today.isoformat()
        write_json(hp, history)
        log.info("[%s] hadith %s (%s, %d words, %.0fs) -> queued", vid, h["id"], h.get("grade"), h["words"],
                 info["duration"])
    return queued


def today_count(cfg) -> int:
    from .review import store
    today = datetime.now(ZoneInfo(cfg["timezone"])).date().isoformat()
    revs = store.load()
    return sum(1 for p in QUEUE.glob("h-*.json")
               if (r := read_json(p, {})).get("date") == today and not store.is_rejected(r, revs))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--generate", type=int)
    ap.add_argument("--topic", default="")
    ap.add_argument("--top-up", action="store_true", help="up to daily.hadith_per_day for today")
    ap.add_argument("--max-new", type=int)
    a = ap.parse_args(argv)
    setup_logging()
    cfg = load_config()
    n = a.generate or 0
    if a.top_up:
        n = max(0, int(cfg["daily"].get("hadith_per_day", 2)) - today_count(cfg))
    if a.max_new is not None:
        n = min(n, a.max_new)
    if n <= 0:
        log.info("no hadith video needed now")
        return
    import os
    from .review import telegram
    chat = telegram.owner() if os.environ.get("TELEGRAM_BOT_TOKEN") else None
    try:
        queued = make(cfg, n, a.topic or None)
    except HadithError as e:
        log.error("%s", e)
        if chat:
            telegram.api("sendMessage", chat_id=chat, text=f"📜 {e}")
        return
    if chat:
        for q in queued:
            telegram.send_one(cfg, q)


if __name__ == "__main__":
    main()
