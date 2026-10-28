"""Clips from links: a sheikh's advice, a famous recitation, a trending religious clip — taken as
AUDIO ONLY and rebuilt on our own visuals (nature footage + waveform + synced subtitles; any Quran
quotation is shown with the exact verified text). Runs on GitHub Actions, started from Telegram.

    python -m src.clips --req '{"id": "...", "url": "...", "start": 80, "end": 125,
                                "sheikh": "الشيخ ...", "title": "...", "n": 1}'
    python -m src.clips --req '{"id": "...", "file_id": "<telegram file id>"}'   # sent as a file

Every clip goes to Telegram for review and, once approved, into the publish queue.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import re
from datetime import datetime, timezone
from pathlib import Path

from . import asr
from .lectures import build_segments, candidates, pick, render_clip, review_dir
from .textrender import TextRenderer
from .util import FFMPEG, ROOT, cache_dir, load_config, media_duration, run_ffmpeg, setup_logging, write_json

log = logging.getLogger("quran-shorts.clips")
QUEUE = ROOT / "queue"


class ClipError(Exception):
    pass


# ------------------------------------------------------------------ request text parsing
URL_RE = re.compile(r"https?://\S+")
TIME_RE = re.compile(r"(?<![\w/])((?:\d{1,2}:)?\d{1,2}:\d{2}|\d{1,5})\s*[-–—]\s*((?:\d{1,2}:)?\d{1,2}:\d{2}|\d{1,5})(?![\w/])")


def _secs(t: str) -> float:
    parts = [float(x) for x in t.split(":")]
    s = 0.0
    for x in parts:
        s = s * 60 + x
    return s


def parse_request(text: str) -> dict:
    """Pull url / start-end / sheikh / title / count out of a free-form Telegram message.
        https://... 1:20-2:05
        الشيخ محمد متولي الشعراوي
        عنوان: الرزق
        3 مقاطع"""
    req: dict = {}
    m = URL_RE.search(text)
    if m:
        req["url"] = m.group(0).rstrip(").,،")
    rest = URL_RE.sub(" ", text)
    t = TIME_RE.search(rest)
    if t:
        a, b = _secs(t.group(1)), _secs(t.group(2))
        if b > a:
            req["start"], req["end"] = a, b
        rest = rest.replace(t.group(0), " ")
    for line in rest.splitlines():
        line = line.strip()
        if re.match(r"^(الشيخ|شيخ|القارئ|قارئ)\b", line):
            req["sheikh"] = re.sub(r"^(شيخ|قارئ)\s*[:：]\s*", "", line).strip()
        elif re.match(r"^(عنوان|العنوان)\s*[:：]", line):
            req["title"] = re.sub(r"^(عنوان|العنوان)\s*[:：]\s*", "", line).strip()
        n = re.search(r"(\d)\s*(مقاطع|مقطع|clips?)", line)
        if n:
            req["n"] = max(1, min(5, int(n.group(1))))
    return req


# ------------------------------------------------------------------ fetching audio
def fetch_audio(cfg, req: dict, work: Path) -> tuple[Path, dict]:
    """Download the source audio (yt-dlp for links, Bot API for files sent in the chat)."""
    if req.get("file_id"):
        from .review import telegram
        f = telegram.api("getFile", file_id=req["file_id"])
        import requests
        url = f"https://api.telegram.org/file/bot{telegram._token()}/{f['file_path']}"
        src = work / ("src" + Path(f["file_path"]).suffix)
        with requests.get(url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(src, "wb") as o:
                for chunk in r.iter_content(1 << 20):
                    o.write(chunk)
        return src, {"uploader": None, "title": None, "webpage_url": None}
    import yt_dlp
    opts = {"format": "bestaudio/best", "outtmpl": str(work / "src.%(ext)s"), "quiet": True,
            "noplaylist": True, "no_warnings": True, "retries": 3}
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(req["url"], download=True)
    except Exception as e:
        msg = str(e)
        if "Sign in" in msg or "bot" in msg.lower() or "403" in msg:
            raise ClipError("الموقع منع التحميل من سيرفر GitHub. ابعت لينك من تيك توك/فيسبوك/إنستجرام، "
                            "أو ابعت الفيديو نفسه هنا في الشات (أقل من 20 ميجا).") from e
        raise ClipError(f"مقدرتش أحمّل اللينك: {msg[:200]}") from e
    files = [p for p in work.glob("src.*") if p.suffix not in (".part", ".json")]
    if not files:
        raise ClipError("التحميل خلص من غير ملف")
    return files[0], {"uploader": info.get("uploader") or info.get("channel"),
                      "title": info.get("title"), "webpage_url": info.get("webpage_url")}


def cut_audio(src: Path, out: Path, start: float = 0.0, dur: float | None = None) -> Path:
    args = ["-ss", f"{start:.3f}"] + (["-t", f"{dur:.3f}"] if dur else []) + ["-i", str(src), "-vn",
                                                                           "-ac", "1", "-ar", "44100",
                                                                           "-c:a", "libmp3lame", "-q:a", "2",
                                                                           str(out)]
    run_ffmpeg(args, "cut clip audio")
    return out


# ------------------------------------------------------------------ captions
def clip_meta(cfg, rng: random.Random, rec: dict) -> dict:
    c = cfg["captions"]
    cl = cfg.get("clips", {})
    pool = list(cl.get("hashtags_pool") or ["#موعظة", "#تذكير", "#دروس_دينية", "#اسلام", "#راحة_نفسية",
                                            "#كلام_يريح_القلب", "#قرآن", "#shorts"])
    sheikh_tag = ("#" + re.sub(r"^(الشيخ|القارئ)\s+", "", rec["sheikh_name"]).replace(" ", "_")) \
        if rec.get("sheikh_name") else None
    out = {}
    for p in ("youtube", "tiktok", "instagram", "facebook"):
        tags = rng.sample(pool, min(len(pool), rng.randint(*c["hashtag_count"])))
        if sheikh_tag:
            tags.insert(0, sheikh_tag)
        body = "\n".join(x for x in (rec.get("title") or rng.choice(cl.get("openers") or ["تذكير ينفع القلب"]),
                                     rec.get("sheikh_name") or "") if x)
        body += "\n\n" + " ".join(tags)
        if cl.get("credit_source", True) and rec.get("source_credit"):
            body += f"\n\nالمصدر: {rec['source_credit']}"
        out[p] = {"caption": body.strip(), "hashtags": tags}
    out["youtube"]["title"] = f"{rec.get('title') or 'تذكير'}{' | ' + rec['sheikh_name'] if rec.get('sheikh_name') else ''}"[:100]
    return out


# ------------------------------------------------------------------ main job
def make_clips(cfg, req: dict) -> list[Path]:
    cl = cfg.get("clips", {})
    lo, hi = cl.get("min_seconds", 20), cl.get("max_seconds", 60)
    rid = req.get("id") or hashlib.sha1(json.dumps(req, sort_keys=True).encode()).hexdigest()[:8]
    work = cache_dir(cfg, "clips", rid)
    src, info = fetch_audio(cfg, req, work)
    total = media_duration(src)
    n = int(req.get("n") or 1)

    windows: list[tuple[float, float]]
    if req.get("start") is not None:
        a, b = float(req["start"]), min(float(req["end"]), total)
        if b - a > hi + 30:
            raise ClipError(f"المدة اللي اخترتها {b - a:.0f} ث — أقصى حاجة {hi} ث تقريبًا")
        windows = [(a, b)]
        audio = cut_audio(src, work / "part.mp3", a, b - a)
        words = asr.transcribe(cfg, audio)
        base = a
    else:
        scan = min(total, cl.get("scan_minutes", 12) * 60)
        audio = cut_audio(src, work / "part.mp3", 0, scan)
        words = asr.transcribe(cfg, audio)
        base = 0.0
        if total <= hi + 15:
            windows = [(0.0, total)]
        else:
            chosen = pick(candidates(words, lo, hi), n)
            if not chosen:
                raise ClipError("ملقتش جزء مناسب (جملة كاملة 20-60 ث). ابعت اللينك تاني ومعاه وقت البداية والنهاية، مثلًا 1:20-2:05")
            windows = [(c["start"], c["end"]) for c in chosen]

    today = datetime.now(timezone.utc).date()
    outs = []
    with TextRenderer(cfg) as tr:
        for k, (a, b) in enumerate(windows):
            rel_a, rel_b = a - base, b - base
            ws = [w for w in words if w["s"] >= rel_a - 0.05 and w["e"] <= rel_b + 0.3]
            if not ws:
                raise ClipError("مفيش كلام مفهوم في الجزء ده")
            w0 = words.index(ws[0])
            clip = {"start": ws[0]["s"], "end": ws[-1]["e"], "w0": w0, "w1": w0 + len(ws), "score": 0}
            segs, flags = build_segments(cfg, words, clip)
            pad_a, pad_b = max(0.0, clip["start"] - 0.15), clip["end"] + 0.35
            cid = f"c-{today:%Y%m%d}-{rid}" + (f"-{k + 1}" if len(windows) > 1 else "")
            rec = {
                "id": cid, "type": "lecture", "kind": "clip", "approved": False, "date": today.isoformat(),
                "sheikh_id": "clip", "sheikh_name": req.get("sheikh") or "",
                "title": req.get("title") or "",
                "source": {"file": str(audio.relative_to(ROOT)).replace("\\", "/"),
                           "url": info.get("webpage_url") or req.get("url"), "channel": info.get("uploader")},
                "source_credit": info.get("uploader"),
                "clip": {"start": round(pad_a, 3), "end": round(pad_b, 3), "score": 0},
                "layout": "audio", "face_x": None, "flags": flags,
                "transcript": " ".join(w["w"] for w in ws),
            }
            for s in segs:      # segment times relative to the padded clip start
                d = clip["start"] - pad_a
                s["start"], s["end"] = round(s["start"] + d, 3), round(s["end"] + d, 3)
                for w in (s.get("words") or []) if s.get("type") == "speech" else []:
                    w["s"], w["e"] = round(w["s"] + d, 3), round(w["e"] + d, 3)
            rec["segments"] = segs
            rec["platforms"] = clip_meta(cfg, random.Random(cid), rec)
            out = render_clip(cfg, rec, tr)
            rec["file"] = str(out.relative_to(ROOT)).replace("\\", "/")
            rec["duration"] = round(pad_b - pad_a, 2)
            write_json(review_dir() / f"{cid}.json", rec)
            outs.append(review_dir() / f"{cid}.json")
            log.info("[%s] %.0f-%.0fs from %s -> %s", cid, a, b, info.get("webpage_url") or "file", out.name)
    return outs


def publish_prep(cfg, rec_path: Path) -> Path:
    """Host the MP4 and put the record in the publish queue (waiting for approval)."""
    from .publish import hosting
    from .util import read_json
    r = read_json(rec_path, {})
    mp4 = rec_path.with_suffix(".mp4")
    if hosting.configured():
        r["hosting"] = hosting.upload(mp4, r["id"]) | {"deleted": False}
    r["queued_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    r["local_record"] = str(rec_path.relative_to(ROOT)).replace("\\", "/")
    write_json(rec_path, r)
    QUEUE.mkdir(exist_ok=True)
    q = QUEUE / rec_path.name
    write_json(q, r)
    return q


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--req", required=True, help="JSON request (see module doc)")
    a = ap.parse_args(argv)
    setup_logging()
    cfg = load_config()
    req = json.loads(a.req)
    import os
    from .review import telegram
    chat = telegram.owner() if os.environ.get("TELEGRAM_BOT_TOKEN") else None
    try:
        recs = make_clips(cfg, req)
        for p in recs:
            q = publish_prep(cfg, p)
            if chat:
                telegram.send_one(cfg, q)
    except ClipError as e:
        log.error("%s", e)
        if chat:
            telegram.api("sendMessage", chat_id=chat, text=f"⚠️ المقطع ده متعملش:\n{e}",
                         **({"reply_to_message_id": req["msg_id"]} if req.get("msg_id") else {}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
