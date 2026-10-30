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
from .lectures import build_segments, candidates, pick, render_clip, review_dir, units
from .textrender import TextRenderer
from .util import FFMPEG, ROOT, cache_dir, load_config, media_duration, run_ffmpeg, setup_logging, write_json

log = logging.getLogger("quran-shorts.clips")
QUEUE = ROOT / "queue"


class ClipError(Exception):
    pass


from .clip_request import URL_RE, parse_request  # noqa: F401  (re-exported)


# ------------------------------------------------------------------ fetching audio
def fetch_audio(cfg, req: dict, work: Path) -> tuple[Path, dict]:
    """Download the source audio (yt-dlp for links, Bot API for files sent in the chat, plain download
    for a sound from our library)."""
    if req.get("direct_url"):
        from .util import download
        return download(req["direct_url"], work / "src.mp3"), {"uploader": None, "title": None, "webpage_url": None}
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


# ------------------------------------------------------------------ series splitting
def split_series(words: list[dict], target: float = 60.0, lo: float = 40.0) -> list[tuple[float, float]]:
    """Consecutive parts of about `target` seconds covering everything, each cut at the end of a
    sentence / clear pause (never mid-word); a tiny last part is merged into the one before."""
    us = units(words)
    parts: list[list[float]] = []
    i = 0
    while i < len(us):
        start, best, best_score = us[i]["s"], None, -1e9
        j = i
        while j < len(us) and us[j]["e"] - start <= target:
            dur = us[j]["e"] - start
            if dur >= lo:
                sc = (2.0 if us[j]["gap"] >= 0.9 else 0.0) \
                     + (2.0 if re.search(r"[.!?؟]$", words[us[j]["i1"] - 1]["w"]) else 0.0) + dur / target
                if sc > best_score:
                    best, best_score = j, sc
            j += 1
        if best is None:
            best = max(i, j - 1)
        parts.append([us[i]["s"], us[best]["e"]])
        i = best + 1
    if len(parts) > 1 and parts[-1][1] - parts[-1][0] < 15 and parts[-1][1] - parts[-2][0] <= target + 20:
        parts[-2][1] = parts.pop()[1]
    return [(a, b) for a, b in parts]


# ------------------------------------------------------------------ main job
def make_clips(cfg, req: dict) -> list[Path]:
    cl = cfg.get("clips", {})
    lo, hi = cl.get("min_seconds", 20), cl.get("max_seconds", 60)
    rid = req.get("id") or hashlib.sha1(json.dumps(req, sort_keys=True).encode()).hexdigest()[:8]
    work = cache_dir(cfg, "clips", rid)
    src, info = fetch_audio(cfg, req, work)
    total = media_duration(src)
    # the range to use: what you asked for, else the whole thing (up to max_series_parts minutes)
    max_parts = int(cl.get("max_series_parts", 10))
    if req.get("start") is not None:
        A, B = float(req["start"]), min(float(req["end"]), total)
    else:
        A, B = 0.0, min(total, max_parts * hi + 30)
    truncated = B < total - 1 and req.get("start") is None
    audio = cut_audio(src, work / "part.mp3", A, B - A)
    # a library sound without speech (song, nature...): no transcription, no subtitles
    words = [] if req.get("no_subs") else asr.transcribe(cfg, audio)
    from .agent import memory
    fixed = memory.apply_rules(words, memory.rules(int((cfg.get("agent") or {}).get("learn_min_count", 2))))
    if fixed:
        log.info("applied %d learned fixes from your past corrections", fixed)
    base = A
    plain = not words
    if plain and not req.get("sound_name"):
        raise ClipError("مفيش كلام مفهوم في المقطع ده")

    windows: list[tuple[float, float]]
    series = False
    if plain:                                                # equal parts of <= one minute, in order
        n = max(1, -(-int(B - A) // hi))
        windows = [(A + (B - A) * k / n, A + (B - A) * (k + 1) / n) for k in range(n)][:max_parts]
        series = len(windows) > 1
    elif B - A <= hi + 5:
        windows = [(A, B)]                                   # one minute or less: one Reel
    elif req.get("n"):                                       # "3 مقاطع": the best N moments
        chosen = pick(candidates(words, lo, hi), int(req["n"]))
        if not chosen:
            raise ClipError("ملقتش جزء مناسب (جملة كاملة 20-60 ث). ابعت اللينك تاني ومعاه وقت البداية والنهاية، مثلًا 1:20-2:05")
        windows = [(base + c["start"], base + c["end"]) for c in chosen]
    else:                                                    # longer: consecutive parts 1..N
        windows = [(base + a, base + b) for a, b in split_series(words, hi, max(lo, hi - 20))][:max_parts]
        series = len(windows) > 1

    today = datetime.now(timezone.utc).date()
    outs = []
    with TextRenderer(cfg) as tr:
        for k, (a, b) in enumerate(windows):
            rel_a, rel_b = a - base, b - base
            if plain:
                ws, segs, flags = [], [], []
                clip = {"start": rel_a, "end": rel_b}
                pad_a, pad_b = rel_a, rel_b
            else:
                ws = [w for w in words if w["s"] >= rel_a - 0.05 and w["e"] <= rel_b + 0.3]
                if not ws:
                    raise ClipError("مفيش كلام مفهوم في الجزء ده")
                w0 = words.index(ws[0])
                clip = {"start": ws[0]["s"], "end": ws[-1]["e"], "w0": w0, "w1": w0 + len(ws), "score": 0}
                segs, flags = build_segments(cfg, words, clip)
                pad_a, pad_b = max(0.0, clip["start"] - 0.15), clip["end"] + 0.35
            sid = f"c-{today:%Y%m%d}-{rid}"
            cid = sid + (f"-p{k + 1:02d}" if series else (f"-{k + 1}" if len(windows) > 1 else ""))
            title = req.get("title") or ""
            if series:
                title = (f"{title} · " if title else "") + f"الجزء {k + 1} من {len(windows)}"
            rec = {
                "id": cid, "type": "lecture", "kind": "clip", "approved": False, "date": today.isoformat(),
                "sheikh_id": "clip", "sheikh_name": req.get("sheikh") or "",
                "sheikh_photo": _library_photo(req.get("sheikh")),   # sent before for this sheikh
                "title": title,
                "series": {"id": sid, "part": k + 1, "of": len(windows), "truncated": truncated} if series else None,
                "source": {"file": str(audio.relative_to(ROOT)).replace("\\", "/"),
                           "url": info.get("webpage_url") or req.get("url"), "channel": info.get("uploader")},
                "source_credit": info.get("uploader"),
                "clip": {"start": round(pad_a, 3), "end": round(pad_b, 3), "score": 0},
                "layout": "audio", "face_x": None, "flags": flags,
                "transcript": " ".join(w["w"] for w in ws),
                "sound": req.get("sound_name"),          # made on request from the sounds library
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


def _clip_audio(cfg, r: dict, vid: str) -> Path:
    """The clip's own audio, taken back out of its hosted MP4 (times match the subtitles)."""
    from .util import download
    work = cache_dir(cfg, "clips", "edit-" + vid)
    mp4 = download(r["hosting"]["url"], work / "old.mp4")
    audio = work / "audio.wav"
    run_ffmpeg(["-i", str(mp4), "-vn", "-ac", "1", "-ar", "44100", str(audio)], "extract audio")
    return audio


def _rerender(cfg, r: dict, vid: str, audio: Path, stamp: str = "edited_at") -> tuple[Path, dict]:
    """Render the clip again from `audio` with r's (edited) subtitles, re-host it, re-queue it."""
    dur = media_duration(audio)
    orig_source, orig_clip = r["source"], r["clip"]
    r["source"] = {**orig_source, "file": str(audio.relative_to(ROOT)).replace("\\", "/")}
    r["clip"] = {"start": 0.0, "end": round(dur, 3), "score": 0}
    with TextRenderer(cfg) as tr:
        render_clip(cfg, r, tr)
    r["source"], r["clip"] = orig_source, orig_clip
    r[stamp] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rec_path = review_dir() / f"{vid}.json"
    old_tg = r.pop("telegram", {}) or {}
    write_json(rec_path, r)
    return publish_prep(cfg, rec_path), old_tg       # re-host (same id, overwritten) + queue record


def _queued(vid: str) -> dict:
    from .util import read_json
    r = read_json(QUEUE / f"{vid}.json", None)
    if r is None:
        raise ClipError(f"مش لاقي المقطع {vid}")
    if r.get("published"):
        raise ClipError("المقطع ده اتجدول/اتنشر خلاص — التعديل مش هيوصل للمنصات")
    return r


def _library_photo(name: str | None) -> str | None:
    from . import badge
    p = badge.find(name or "")
    return str(p.relative_to(ROOT)).replace("\\", "/") if p else None


def add_photo(cfg, req: dict) -> tuple[Path, dict]:
    """Your photo of the sheikh (sent as a reply in Telegram): save it in the library under his name,
    and rebuild the clip with it in the circular Islamic frame."""
    from . import badge
    from .review import telegram
    import requests as rq
    vid = req["photo"]
    r = _queued(vid)
    f = telegram.api("getFile", file_id=req["file_id"])
    data = rq.get(f"https://api.telegram.org/file/bot{telegram._token()}/{f['file_path']}", timeout=60).content
    name = re.sub(r"\s+", " ", req.get("name") or "").strip()
    p = badge.save(name, data, vid)
    r["sheikh_photo"] = str(p.relative_to(ROOT)).replace("\\", "/")
    if name and not r.get("sheikh_name"):
        r["sheikh_name"] = name if name.startswith("الشيخ") else f"الشيخ {name}"
    q, old_tg = _rerender(cfg, r, vid, _clip_audio(cfg, r, vid), stamp="photo_at")
    log.info("[%s] sheikh photo added (%s)", vid, p.name)
    return q, old_tg


def rerender_edit(cfg, req: dict) -> tuple[Path, dict]:
    """Apply your subtitle edits (from Telegram) to a queued clip and rebuild it."""
    from .review.edit import apply_edits
    from .agent import memory
    from .review.edit import speech_segments
    vid = req["edit"]
    r = _queued(vid)
    segs = speech_segments(r)
    learned = [{"line": int(k), "old": segs[int(k) - 1].get("text") or " ".join(w["w"] for w in segs[int(k) - 1]["words"]),
                "new": v} for k, v in req["edits"].items() if 1 <= int(k) <= len(segs)]
    n = apply_edits(r, req["edits"])
    if not n:
        raise ClipError("مفيش سطر اتعدل (اتأكد من أرقام السطور)")
    r.pop("auto", None)                 # you edited it: your approval decides from here
    memory.record(r, learned)           # the agent learns from every correction you make
    q, old_tg = _rerender(cfg, r, vid, _clip_audio(cfg, r, vid))
    log.info("[%s] %d subtitle lines edited, re-rendered", vid, n)
    return q, old_tg


def autofix(cfg, req: dict) -> dict:
    """The correction agent's turn on a clip you haven't touched for agent.delay_hours: fix the
    subtitles, re-render if anything changed, and approve it (or hold it with the reason)."""
    from .agent.corrector import correct
    from .review import store
    from .review.edit import apply_edits
    vid = req["autofix"]
    manual = bool(req.get("manual"))    # you pressed 🤖: correct now, never approve on its own
    r = _queued(vid)
    e = store.get(vid)
    if not manual and (e.get("status") or e.get("tags") is not None or r.get("edited_at") or r.get("auto")):
        log.info("[%s] you already acted on it (or the agent did) — skipping", vid)
        return {"skipped": True}
    audio = _clip_audio(cfg, r, vid)
    res = correct(cfg, r, audio)
    res["manual"] = manual
    log.info("[%s] agent (%s): %s, confidence %s, %d edits, dropped %s, tools %s", vid,
             "on request" if manual else "timer", res["decision"], res["confidence"], len(res["edits"]),
             res["dropped"], res["tool_calls"])
    r["auto"] = {"status": "fixed" if manual else "approved" if res["decision"] == "approve" else "held",
                 "decision": res["decision"],
                 "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "confidence": res["confidence"], "summary": res["summary"], "why": res["why"],
                 "changes": res["changes"], "dropped": res["dropped"], "locked": res["locked"]}
    res["rerendered"] = False
    if res["edits"] and (manual or res["decision"] == "approve"):   # on request: you review it anyway
        apply_edits(r, res["edits"])
        res["queue"], res["old_tg"] = _rerender(cfg, r, vid, audio, stamp="auto_edited_at")
        res["rerendered"] = True
    else:                               # unchanged video (or held: edits only suggested to you)
        write_json(QUEUE / f"{vid}.json", r)
        local = ROOT / r.get("local_record", "")
        if r.get("local_record") and local.exists():
            write_json(local, r)
        res["queue"], res["old_tg"] = QUEUE / f"{vid}.json", r.get("telegram") or {}
    return res


def _agent_report(vid: str, res: dict) -> str:
    lines = [f"🤖 المصحح راجع 🆔 {vid} (ثقة: {res['confidence']})"]
    if res.get("summary"):
        lines.append(res["summary"])
    if res.get("manual"):
        lines.append("👀 النسخة المصححة فوق — راجعها ووافق ✅ أو ارفض ❌ (مش هيتوافق عليها لوحدها)."
                     + (f"\n⚠️ خد بالك: {res['why']}" if res["why"] else ""))
    elif res["decision"] == "approve":
        lines.append("✅ اتوافق عليه تلقائيًا — لسه تقدر ترفضه بـ ❌ قبل ما يتنشر.")
    else:
        lines.append(f"⏸ مستنيك: {res['why']}")
    if res["changes"]:
        verb = "اتعدل" if res["rerendered"] else "اقتراحات (رد بيها على رسالة 📝 لو موافق)"
        lines.append(f"\n✏️ {verb}:")
        for c in res["changes"]:
            lines.append(f"{c['line']}: {c['new'] if c['new'] is not None else 'حذف'}" +
                         (f"   ← كان: {c['old']}" if res["rerendered"] else ""))
    elif res["decision"] == "approve" or res.get("manual"):
        lines.append("مفيش أخطاء في الترجمة.")
    if res["dropped"]:
        lines.append("\n🔒 " + " · ".join(res["dropped"]))
    return "\n".join(lines)[:4000]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--req", required=True, help="JSON request (see module doc)")
    a = ap.parse_args(argv)
    setup_logging()
    cfg = load_config()
    req = json.loads(a.req)
    import os
    from .agent.corrector import AgentError
    from .review import telegram
    chat = telegram.owner() if os.environ.get("TELEGRAM_BOT_TOKEN") else None
    try:
        if req.get("autofix"):
            res = autofix(cfg, req)
            if res.get("skipped") or not chat:
                return
            vid = req["autofix"]
            old = res.get("old_tg") or {}
            if res["rerendered"]:
                if old.get("message_id"):
                    try:
                        telegram.api("editMessageCaption", chat_id=chat, message_id=old["message_id"],
                                     caption="🤖 النسخة دي اتصححت — شوف الجديدة تحت")
                    except telegram.TelegramError:
                        pass
                telegram.send_one(cfg, res["queue"])
            elif old.get("message_id"):
                telegram.refresh_caption(res["queue"])
            from .util import read_json
            tg = (read_json(res["queue"], {}).get("telegram") or {})
            telegram.api("sendMessage", chat_id=chat, text=_agent_report(vid, res),
                         **({"reply_to_message_id": tg["message_id"]} if tg.get("message_id") else {}))
            return
        if req.get("sound") or req.get("sound_del"):     # the sounds library
            from . import sounds
            if req.get("sound_del"):
                done = sounds.remove(req["sound_del"])
                if chat:
                    telegram.api("sendMessage", chat_id=chat, text=f"🗑 الصوت «{req['sound_del']}» "
                                 + ("اتمسح." if done else "مش موجود."))
                return
            try:
                s = sounds.add(cfg, req["sound"], req)
                msg = (f"🔊 اتحفظ الصوت «{req['sound']}» ({int(s['duration'])} ث) — "
                       f"{sounds.KINDS.get(s['kind'], '❔')}" + (f": {s['summary']}" if s.get("summary") else "") +
                       "\n/sounds تشوف المكتبة، و🎬 جنب أي صوت يعمل منه فيديو.")
            except sounds.SoundError as e:
                msg = f"🔊 الصوت «{req['sound']}» متحفظش: {e}"
            if chat:
                telegram.api("sendMessage", chat_id=chat, text=msg,
                             **({"reply_to_message_id": req["msg_id"]} if req.get("msg_id") else {}))
            return
        if req.get("edit") or req.get("photo"):
            q, old_tg = rerender_edit(cfg, req) if req.get("edit") else add_photo(cfg, req)
            if chat:
                if old_tg.get("message_id"):
                    try:
                        telegram.api("editMessageCaption", chat_id=chat, message_id=old_tg["message_id"],
                                     caption="✏️ النسخة دي اتعدلت — شوف الجديدة تحت" if req.get("edit")
                                     else "🖼 اتحطت صورة الشيخ — شوف النسخة الجديدة تحت")
                    except telegram.TelegramError:
                        pass
                telegram.send_one(cfg, q)
            return
        recs = make_clips(cfg, req)
        queued = [publish_prep(cfg, p) for p in recs]
        if chat:
            for q in queued:
                telegram.send_one(cfg, q)
            telegram.send_series_summary(cfg, queued)
    except AgentError as e:             # the agent could not finish: the clip just waits for you
        log.error("agent: %s", e)
        if chat:
            import json
            vid = req.get("autofix")
            telegram.api("sendMessage", chat_id=chat,
                         text=f"🤖 المصحح مقدرش يراجع 🆔 {vid}: {e}\nالمقطع مستني موافقتك زي الأول، "
                              "أو دوس «أعد التصحيح» يجرب تاني دلوقتي.",
                         reply_markup=json.dumps({"inline_keyboard": [[{"text": "🔁 أعد التصحيح",
                                                                        "callback_data": f"f:{vid}"}]]}))
        raise SystemExit(1)
    except ClipError as e:
        log.error("%s", e)
        if chat:
            telegram.api("sendMessage", chat_id=chat, text=f"⚠️ المقطع ده متعملش:\n{e}",
                         **({"reply_to_message_id": req["msg_id"]} if req.get("msg_id") else {}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
