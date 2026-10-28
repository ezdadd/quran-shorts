"""Telegram review bot — runs entirely on GitHub Actions (no PC, no server).

    python -m src.review.telegram send     # daily run: post every new video for review
    python -m src.review.telegram poll     # hourly: read button presses / tag edits / commands

In the chat (only TELEGRAM_CHAT_ID is obeyed):
  ✅ / ❌ buttons under each video        approve / reject (a rejected one is replaced)
  ↩️ button                              back to "waiting"
  reply to a video with  #a #b #c        replace its hashtags
  reply to a video with  +#a -#b         add / remove hashtags
  /status                                what is waiting / approved / scheduled
"""
from __future__ import annotations

import argparse
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from ..util import ROOT, load_config, read_json, setup_logging, write_json
from . import store

log = logging.getLogger("quran-shorts.telegram")
OUTPUT = ROOT / "output"
STATE = ROOT / "data" / "telegram_state.json"
URL_IN = re.compile(r"https?://\S+")
ID_RE = re.compile(r"🆔\s*([\w-]+)")
STATUS = {"approved": "✅ اتوافق عليه", "rejected": "❌ مرفوض — هيتعمل بديل", None: "⏳ مستني موافقتك"}


HELP = """📎 مقطع من لينك: ابعت لينك (تيك توك / فيسبوك / إنستجرام / يوتيوب) أو الفيديو نفسه، وممكن تضيف:
• وقت: 1:20-2:05
• سطر: الشيخ محمد متولي الشعراوي
• سطر: عنوان: الرزق
• الفيديو الأطول من دقيقة بيتقسم أجزاء ورا بعض (الجزء 1، 2، ...) وتوافق عليهم مرة واحدة
• عدد: 3 مقاطع = أحسن 3 لحظات بس بدل التقسيم
هاخد الصوت بس وأركبه على فيديو طبيعة بترجمة، ويوصلك هنا للمراجعة.

✅ / ❌ تحت أي فيديو = موافقة / رفض
↩️ رد على فيديو بـ #وسم1 #وسم2 أو +#وسم -#وسم لتعديل الهاشتاجات
📝 رد على رسالة «نص الترجمة» بـ  3: النص الصح  أو  5: حذف  لتعديل الكلام اللي على الفيديو
/status حالة الفيديوهات"""


class TelegramError(Exception):
    pass


def _token() -> str:
    t = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not t:
        raise TelegramError("TELEGRAM_BOT_TOKEN missing")
    return t


def owner() -> str:
    return str(os.environ.get("TELEGRAM_CHAT_ID", "")).strip()


def api(method: str, files=None, http_timeout=60, **params):
    r = requests.post(f"https://api.telegram.org/bot{_token()}/{method}", data=params, files=files,
                      timeout=http_timeout)
    body = r.json()
    if not body.get("ok"):
        raise TelegramError(f"{method}: {body.get('description')}")
    return body["result"]


def _media(msg: dict) -> dict | None:
    for k in ("video", "audio", "voice", "video_note", "document"):
        if msg.get(k):
            return msg[k]
    return None


def _kb(video_id: str, status: str | None) -> str:
    import json
    if status in ("approved", "rejected"):
        rows = [[{"text": "↩️ تراجع", "callback_data": f"u:{video_id}"}]]
    else:
        rows = [[{"text": "✅ موافقة", "callback_data": f"a:{video_id}"},
                 {"text": "❌ رفض وبدّل", "callback_data": f"r:{video_id}"}]]
    return json.dumps({"inline_keyboard": rows})


def _record(video_id: str) -> tuple[Path | None, dict]:
    for p in list(OUTPUT.glob(f"*/{video_id}.json")) + list((ROOT / "queue").glob(f"{video_id}.json")):
        if p.parent.name != "visual_test":
            return p, read_json(p, {})
    return None, {}


def _caption(r: dict, reviews: dict) -> str:
    e = store.get(r["id"], reviews)
    cap = store.final_caption(r, "tiktok", reviews)
    tpl = r.get("template", "")
    head = f"🗓 {r.get('date', '')}   🆔 {r['id']}"
    if r.get("kind") == "clip":
        head += "\n🎙 مقطع من لينك" + (f" · {r['sheikh_name']}" if r.get("sheikh_name") else "")
        if r.get("series"):
            head += f"\n📚 الجزء {r['series']['part']} من {r['series']['of']}"
        kinds = {f.get("type") for f in r.get("flags") or []}
        if "quran" in kinds:
            head += "\n📖 فيه آية — اتعرضت بالنص الموثّق"
        if "hadith" in kinds:
            head += "\n⚠️ فيه حديث — اتأكد إن الكلام صح قبل الموافقة"
        if "asr" in kinds:
            head += "\n⚠️ الترجمة فيها كلمات مش واضحة — راجعها"
    elif r.get("type") == "lecture":
        head += "\n🎙 درس"
    s = f"{head}\n\n{cap}\n\nالحالة: {STATUS.get(e.get('status'))}"
    if not e.get("status"):
        s += "\n↩️ رد على الفيديو بهاشتاجات جديدة لتعديلها (+#وسم ‎-#وسم للإضافة/الحذف)"
    return s[:1024]


def _days(cfg) -> list[date]:
    today = datetime.now(ZoneInfo(cfg["timezone"])).date()
    return [today + timedelta(days=i) for i in range(cfg["publishing"].get("days_ahead", 2) + 1)]


# --------------------------------------------------------------------------- send (daily run)
def host_new(cfg) -> None:
    """Host every new MP4 right away: the CI runner is thrown away, and the video is needed
    again on its publish day (and for review if Telegram is set up later)."""
    from ..publish import hosting
    if not hosting.configured():
        return
    for day in _days(cfg):
        for p in sorted((OUTPUT / day.isoformat()).glob("q-*.json")):
            r = read_json(p, {})
            mp4 = p.with_suffix(".mp4")
            if mp4.exists() and not r.get("rejected") and not (r.get("hosting") or {}).get("url"):
                r["hosting"] = hosting.upload(mp4, r["id"]) | {"deleted": False}
                write_json(p, r)


def send(cfg) -> int:
    """Post every generated, not-yet-sent video (today .. today+days_ahead) for review, hosting
    it first so the publish run on a later day can still fetch it."""
    reviews = store.load()
    sent = 0
    waiting_today = 0
    days = _days(cfg)
    for day in days:
        for p in sorted((OUTPUT / day.isoformat()).glob("q-*.json")):
            r = read_json(p, {})
            if store.is_rejected(r, reviews):
                continue
            if not store.get(r["id"], reviews).get("status") and day == days[0] and not r.get("approved"):
                waiting_today += 1
            if (r.get("telegram") or {}).get("message_id") or r.get("published"):
                continue
            try:
                from ..publish.runner import video_file
                mp4 = video_file(p, r)          # local, or fetched back from hosting
            except Exception as e:
                log.warning("%s: cannot send (%s)", r["id"], e)
                continue
            with open(mp4, "rb") as f:
                msg = api("sendVideo", files={"video": (mp4.name, f, "video/mp4")}, http_timeout=300,
                          chat_id=owner(), caption=_caption(r, reviews), supports_streaming="true",
                          width=1080, height=1920, reply_markup=_kb(r["id"], None))
            r["telegram"] = {"message_id": msg["message_id"], "chat_id": owner()}
            write_json(p, r)
            sent += 1
            log.info("sent %s for review", r["id"])
    if waiting_today:
        api("sendMessage", chat_id=owner(),
            text=f"⏳ {waiting_today} فيديو من فيديوهات النهارده لسه مستنيين موافقتك — "
                 "اللي هتوافق عليه هيتجدول في الأوقات اللي فاضلة النهارده.")
    return sent


def send_one(cfg, path: Path) -> None:
    """Post one record (clip / queue item) for review right away."""
    r = read_json(path, {})
    from ..publish.runner import video_file
    mp4 = video_file(path, r)
    with open(mp4, "rb") as f:
        msg = api("sendVideo", files={"video": (mp4.name, f, "video/mp4")}, http_timeout=300,
                  chat_id=owner(), caption=_caption(r, store.load()), supports_streaming="true",
                  width=1080, height=1920, reply_markup=_kb(r["id"], None))
    r["telegram"] = {"message_id": msg["message_id"], "chat_id": owner()}
    if r.get("kind") == "clip":
        from .edit import numbered_text
        t = api("sendMessage", chat_id=owner(), text=numbered_text(r), reply_to_message_id=msg["message_id"])
        r["telegram"]["text_msg_id"] = t["message_id"]
    write_json(path, r)
    log.info("sent %s for review", r["id"])


def series_records(sid: str) -> list[tuple[Path, dict]]:
    out = [(p, read_json(p, {})) for p in (ROOT / "queue").glob(f"{sid}-p*.json")]
    return sorted(out, key=lambda x: (x[1].get("series") or {}).get("part", 0))


def _series_kb(sid: str, n: int) -> str:
    import json
    return json.dumps({"inline_keyboard": [[{"text": f"✅ موافقة على الكل ({n})", "callback_data": f"A:{sid}"},
                                            {"text": "❌ رفض الكل", "callback_data": f"R:{sid}"}]]})


def send_series_summary(cfg, queued: list[Path]) -> None:
    recs = [read_json(q, {}) for q in queued]
    ser = [r for r in recs if r.get("series")]
    if not ser:
        return
    sid, n = ser[0]["series"]["id"], ser[0]["series"]["of"]
    note = "\n⚠️ الفيديو أطول من الحد، فاتعمل أول جزء منه بس." if ser[0]["series"].get("truncated") else ""
    api("sendMessage", chat_id=owner(), reply_markup=_series_kb(sid, n),
        text=f"📚 سلسلة {sid}: {n} أجزاء ورا بعض (الجزء 1 → {n}).\n"
             f"وافق على الكل مرة واحدة من هنا، أو على كل جزء لوحده.\n"
             f"هتتنشر بالترتيب، وأي جزء مرفوض بيتشال من السلسلة.{note}")


# --------------------------------------------------------------------------- poll (hourly)
def _unschedule(r: dict) -> list[str]:
    """Rejecting something already scheduled: remove it from Buffer / Facebook."""
    from ..publish import buffer
    done = []
    for pl, info in (r.get("published") or {}).items():
        if info.get("status") not in ("scheduled", "buffer", None):
            continue
        try:
            if pl == "facebook" and info.get("video_id"):
                requests.delete(f"https://graph.facebook.com/{load_config()['publishing']['facebook_api_version']}/"
                                f"{info['video_id']}", params={"access_token": os.environ.get("FB_PAGE_TOKEN")},
                                timeout=30)
                done.append(pl)
            elif info.get("post_id"):
                buffer.gql("""mutation($i: DeletePostInput!) { deletePost(input: $i) {
                               __typename } }""", {"i": {"id": info["post_id"]}})
                done.append(pl)
        except Exception as e:
            log.warning("unschedule %s %s: %s", r["id"], pl, e)
    return done


def _status_text(cfg, reviews: dict) -> str:
    lines = []
    for day in _days(cfg):
        recs = [read_json(p, {}) for p in sorted((OUTPUT / day.isoformat()).glob("q-*.json"))]
        recs = [r for r in recs if not r.get("rejected")]
        if not recs:
            lines.append(f"🗓 {day}: لسه متعملش")
            continue
        lines.append(f"🗓 {day}:")
        for r in recs:
            st = store.get(r["id"], reviews).get("status") or ("approved" if r.get("approved") else None)
            pub = ", ".join(r.get("published") or {}) or ""
            lines.append(f"  {STATUS.get(st, st).split(' —')[0]}  {r['id']}  {(r.get('reference') or {}).get('surah_name', '')} {(r.get('reference') or {}).get('from', '')}-{(r.get('reference') or {}).get('to', '')}"
                         + (f"  📤 {pub}" if pub else ""))
    return "\n".join(lines)[:4000]


def poll(cfg) -> bool:
    """Process pending updates. Returns True when the daily workflow should run again now
    (a video for today was approved, or something was rejected and needs a replacement)."""
    state = read_json(STATE, {"offset": 0})
    reviews = store.load()
    me = owner()
    today = datetime.now(ZoneInfo(cfg["timezone"])).date().isoformat()
    rerun = False
    clip_requests: list[dict] = []
    updates = api("getUpdates", offset=state["offset"], timeout=0,
                  allowed_updates='["message","callback_query"]')
    for u in updates:
        state["offset"] = u["update_id"] + 1
        cq, msg = u.get("callback_query"), u.get("message")
        if cq:
            if str(cq["from"]["id"]) != me and str(cq["message"]["chat"]["id"]) != me:
                continue
            action, vid = cq["data"].split(":", 1)
            if action in ("A", "R"):            # a whole series at once
                status = "approved" if action == "A" else "rejected"
                parts = series_records(vid)
                for p_, r_ in parts:
                    store.set_status(reviews, r_["id"], status, by=cq["from"].get("username", ""))
                    tg = r_.get("telegram") or {}
                    if tg.get("message_id"):
                        try:
                            api("editMessageCaption", chat_id=me, message_id=tg["message_id"],
                                caption=_caption(r_, reviews), reply_markup=_kb(r_["id"], status))
                        except TelegramError:
                            pass
                rerun = rerun or status == "approved"
                try:
                    api("editMessageText", chat_id=cq["message"]["chat"]["id"], message_id=cq["message"]["message_id"],
                        text=f"📚 سلسلة {vid}: " + ("✅ اتوافق على كل الأجزاء — هتتنشر بالترتيب"
                                                   if status == "approved" else "❌ اترفضت كلها"))
                except TelegramError:
                    pass
                try:
                    api("answerCallbackQuery", callback_query_id=cq["id"], text="تم")
                except TelegramError:
                    pass
                continue
            path, r = _record(vid)
            status = {"a": "approved", "r": "rejected", "u": None}[action]
            if status:
                store.set_status(reviews, vid, status, by=cq["from"].get("username", ""))
            else:
                reviews.get(vid, {}).pop("status", None)
            note = ""
            if status == "rejected" and r:
                gone = _unschedule(r)
                note = f" (اتشال من: {', '.join(gone)})" if gone else ""
            if status in ("approved", "rejected") and r.get("date", "") <= today:
                rerun = True
            if status == "rejected":
                rerun = True        # make a replacement
            if r:
                try:
                    api("editMessageCaption", chat_id=cq["message"]["chat"]["id"],
                        message_id=cq["message"]["message_id"], caption=_caption(r, reviews),
                        reply_markup=_kb(vid, status))
                except TelegramError as e:
                    log.warning("edit caption: %s", e)
            try:                    # only works within a few minutes of the press — the
                api("answerCallbackQuery", callback_query_id=cq["id"],    # caption edit is what shows
                    text={"approved": "اتوافق ✅", "rejected": "اترفض ❌" + note, None: "رجع للانتظار"}[status])
            except TelegramError:
                pass
        elif msg:
            if str(msg["chat"]["id"]) != me:
                continue
            text = (msg.get("text") or "").strip()
            reply = msg.get("reply_to_message")
            if text.startswith("/help"):
                api("sendMessage", chat_id=me, text=HELP)
            elif text.startswith("/status") or text.startswith("/start"):
                api("sendMessage", chat_id=me, text=_status_text(cfg, reviews) or "مفيش فيديوهات")
            elif not reply and (URL_IN.search(text) or _media(msg)):
                media = _media(msg)
                from ..clip_request import parse_request
                req = parse_request(text or msg.get("caption") or "")
                if media:
                    if (media.get("file_size") or 0) > 20 * 1024 * 1024:
                        api("sendMessage", chat_id=me, reply_to_message_id=msg["message_id"],
                            text="الملف أكبر من 20 ميجا (حد تيليجرام للبوتات). ابعت لينك بداله أو قص الجزء المطلوب.")
                        continue
                    req["file_id"] = media["file_id"]
                req.update(id=str(msg["message_id"]), msg_id=msg["message_id"])
                clip_requests.append(req)
                if req.get("start") is not None:
                    a, b = int(req["start"]), int(req["end"])
                    when = f" من {a // 60}:{a % 60:02d} لـ {b // 60}:{b % 60:02d}"
                else:
                    when = " (هختار أحسن جزء: جملة كاملة 20-60 ث)"
                api("sendMessage", chat_id=me, reply_to_message_id=msg["message_id"],
                    text=f"✅ استلمت{when}. هاخد الصوت بس وأركبه على فيديو من عندنا بترجمة، "
                         "وهيوصلك هنا للمراجعة خلال حوالي 10-20 دقيقة.")
            elif reply and text and (reply.get("text") or "").startswith("📝"):
                from .edit import parse_edits
                m = ID_RE.search(reply.get("text") or "")
                edits = parse_edits(text)
                if not m or not edits:
                    api("sendMessage", chat_id=me, reply_to_message_id=msg["message_id"],
                        text="اكتب رقم السطر وبعده النص الصح، سطر لكل تعديل. مثال:\n3: النص الصح\n5: حذف")
                    continue
                vid = m.group(1)
                reviews.get(vid, {}).pop("status", None)      # edited -> needs a fresh look
                clip_requests.append({"id": f"edit-{msg['message_id']}", "edit": vid, "edits": edits,
                                      "msg_id": msg["message_id"]})
                api("sendMessage", chat_id=me, reply_to_message_id=msg["message_id"],
                    text=f"✏️ استلمت {len(edits)} تعديل. هتوصلك النسخة الجديدة هنا خلال حوالي 10 دقايق.")
            elif reply and text:
                m = ID_RE.search(reply.get("caption") or reply.get("text") or "")
                path, r = _record(m.group(1)) if m else (None, {})
                if not r:
                    api("sendMessage", chat_id=me, reply_to_message_id=msg["message_id"],
                        text="مش لاقي الفيديو ده. رد على رسالة الفيديو نفسها.")
                    continue
                cur = store.get(r["id"], reviews).get("tags") or r["platforms"]["tiktok"].get("hashtags", [])
                new = store.edit_tags(cur, text)
                reviews.setdefault(r["id"], {})["tags"] = new
                api("sendMessage", chat_id=me, reply_to_message_id=msg["message_id"],
                    text="🏷 الهاشتاجات الجديدة:\n" + (" ".join(new) or "(من غير هاشتاجات)"))
                tg = r.get("telegram") or {}
                if tg.get("message_id"):
                    try:
                        api("editMessageCaption", chat_id=me, message_id=tg["message_id"],
                            caption=_caption(r, reviews),
                            reply_markup=_kb(r["id"], store.get(r["id"], reviews).get("status")))
                    except TelegramError:
                        pass
                if (r.get("published") or {}) and any(i.get("status") == "scheduled"
                                                      for i in r["published"].values()):
                    api("sendMessage", chat_id=me,
                        text="⚠️ الفيديو ده اتجدول خلاص بالهاشتاجات القديمة — التعديل هيطبق على المنصات اللي لسه متجدولتش بس.")
    store.save(reviews)
    write_json(STATE, state)
    # handed to the workflow, which starts one clip job per request
    import json
    out = ROOT / ".cache" / "clip_requests.jsonl"
    out.parent.mkdir(exist_ok=True)
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in clip_requests), encoding="utf-8")
    return rerun


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["send", "poll"])
    a = ap.parse_args(argv)
    setup_logging()
    cfg = load_config()
    if a.cmd == "send":
        host_new(cfg)
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not owner():
        log.warning("Telegram not configured — skipping")
        return
    if a.cmd == "send":
        log.info("sent %d videos for review", send(cfg))
    else:
        rerun = poll(cfg)
        log.info("poll done, rerun daily: %s", rerun)
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as f:
                f.write(f"rerun={'true' if rerun else 'false'}\n")


if __name__ == "__main__":
    main()
