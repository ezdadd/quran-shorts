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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from ..util import ROOT, load_config, read_json, setup_logging, write_json
from . import store

log = logging.getLogger("quran-shorts.telegram")
OUTPUT = ROOT / "output"
STATE = ROOT / "data" / "telegram_state.json"
URL_IN = re.compile(r"https?://\S+")
COMMANDS_VERSION = 3
GENERATE: list[dict] = []   # /generate requests in this poll -> one daily.yml run with the job list
GENERATE_MAX = 10           # videos per poll
GEN_COUNTS = (1, 3, 5, 10)
ASK_PASSAGE = "✍️ اكتب اسم السورة والآيات"
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def _themes(cfg) -> list[str]:
    return list((cfg.get("quran") or {}).get("theme_weights") or {})


def _hadith_topics() -> list[str]:
    """Topics in data/hadiths.json (read directly: the poller doesn't load the video code)."""
    seen: list[str] = []
    for h in (read_json(ROOT / "data" / "hadiths.json", {}).get("hadiths") or {}).values():
        for t in h.get("topics") or [] if h.get("keep") else []:
            if t not in seen:
                seen.append(t)
    return seen


def _reciters(cfg) -> list[tuple[str, str]]:
    """(id, label) for every reciter usable from the menu (not the ones that need a human check)."""
    out = []
    for rid, r in (cfg.get("reciters") or {}).items():
        if r.get("requires_review") or float(r.get("weight", 1)) <= 0:
            continue
        label = r.get("name_ar", rid) + (" · مجوّد" if "mujawwad" in rid else "")
        out.append((rid, label))
    return out


def _gen_kb(rows: list[list[tuple[str, str]]]) -> str:
    import json
    return json.dumps({"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]})


def _reciter_rows(cfg, prefix: str, back: str) -> list[list[tuple[str, str]]]:
    rec = _reciters(cfg)
    rows = [[("🎲 أي شيخ", f"{prefix}:-")]]
    rows += [[(lab, f"{prefix}:{i}") for i, (_, lab) in enumerate(rec)][k:k + 2] for k in range(0, len(rec), 2)]
    rows.append([("⬅️ رجوع", back)])
    return rows


def generate_menu(cfg, data: str) -> tuple[str, str | None]:
    """(text, keyboard) for each menu step.  callback data:
    g:t                 type        g:q            Quran: theme or a chosen surah
    g:q:<t>             sheikh      g:q:<t>:<r>    count      g:q:<t>:<r>:<n>  -> start
    g:P:<s.a-b>:<r>     a passage you typed -> start (one video)"""
    p = data.split(":")
    if data == "g:t":
        return ("⚙️ عايز تعمل إيه؟", _gen_kb([[("📖 آيات قرآن", "g:q")],
                                            [("📜 حديث شريف", "g:h")],
                                            [("🎙 مقطع من لينك", "g:l")],
                                            [("📊 الطابور (/que)", "g:que")]]))
    if data == "g:q":
        th = _themes(cfg)
        rows = [[("🎲 أي موضوع (الأغلب حكمة وصبر ووعظ)", "g:q:-")]]
        rows += [[(t, f"g:q:{i}") for i, t in enumerate(th)][k:k + 2] for k in range(0, len(th), 2)]
        rows += [[("✍️ سورة وآيات بالاسم", "g:s")], [("⬅️ رجوع", "g:t")]]
        return "📖 آيات قرآن — اختار الموضوع، أو اكتب سورة وآيات بعينها:", _gen_kb(rows)
    if data == "g:h":                   # hadith: topic -> count
        ht = _hadith_topics()
        rows = [[("🎲 أي موضوع", "g:h:-")]]
        rows += [[(t, f"g:h:{i}") for i, t in enumerate(ht)][k:k + 2] for k in range(0, len(ht), 2)]
        rows.append([("⬅️ رجوع", "g:t")])
        return ("📜 حديث شريف (صحيح أو حسن بس، بمصدره ودرجته) — اختار الموضوع:", _gen_kb(rows))
    if p[1] == "h" and len(p) == 3:
        name = "أي موضوع" if p[2] == "-" else _hadith_topics()[int(p[2])]
        return (f"📜 {name}\nكام فيديو؟", _gen_kb([[(str(n), f"{data}:{n}") for n in (1, 2, 3, 5)],
                                                  [("⬅️ رجوع", "g:h")]]))
    theme = lambda t: "أي موضوع" if t == "-" else _themes(cfg)[int(t)]          # noqa: E731
    reciter = lambda r: "أي شيخ" if r == "-" else _reciters(cfg)[int(r)][1]     # noqa: E731
    if p[1] == "q" and len(p) == 3:
        return f"📖 {theme(p[2])} — اختار الشيخ:", _gen_kb(_reciter_rows(cfg, data, "g:q"))
    if p[1] == "q" and len(p) == 4:
        return (f"📖 {theme(p[2])} · {reciter(p[3])}\nكام فيديو؟",
                _gen_kb([[(str(n), f"{data}:{n}") for n in GEN_COUNTS], [("⬅️ رجوع", f"g:q:{p[2]}")]]))
    if p[1] == "Q":                     # the passage you typed was understood: pick the sheikh
        return f"📖 {p[2].replace('.', ':')} — اختار الشيخ:", _gen_kb(_reciter_rows(cfg, f"g:P:{p[2]}", "g:q"))
    return "", None


def _enqueue(cfg, job: dict) -> str:
    GENERATE.append(job)
    n = job["count"]
    what = f"سورة {job['passage_name']}" if job.get("passage") else \
        (f"«{job['theme']}»" if job.get("theme") else "أي موضوع")
    who = dict(_reciters(cfg)).get(job.get("reciter") or "", "أي شيخ")
    return (f"✅ هعمل {n} فيديو قرآن — {what} — {who}.\nهيوصلوك هنا للمراجعة خلال حوالي {3 * n + 5} دقيقة، "
            "وبعدها بيدخلوا الطابور ويتنشروا بالترتيب على الأيام الجاية.")


def generate_callback(cfg, cq: dict) -> None:
    """Button presses in the /generate menu."""
    data, chat, mid = cq["data"], cq["message"]["chat"]["id"], cq["message"]["message_id"]
    p = data.split(":")
    note, kb = "", None
    if data == "g:que":                 # the queue counts as a new message; the menu stays
        api("sendMessage", chat_id=chat, text=queue_text(cfg, store.load(), read_json(STATE, {})))
        try:
            api("answerCallbackQuery", callback_query_id=cq["id"])
        except TelegramError:
            pass
        return
    if p[1] == "h" and len(p) == 4:     # hadith topic + count -> start
        topic = "" if p[2] == "-" else _hadith_topics()[int(p[2])]
        n = max(1, min(GENERATE_MAX, int(p[3])))
        GENERATE.append({"kind": "hadith", "count": n, "theme": topic, "reciter": "", "passage": ""})
        text, note = (f"✅ هعمل {n} فيديو حديث" + (f" عن «{topic}»" if topic else "") +
                      f".\nهيوصلوك هنا للمراجعة خلال حوالي {2 * n + 5} دقيقة."), "اتبدأ"
    elif data == "g:l":
        text, kb = ("🎙 ابعت اللينك (تيك توك / فيسبوك / إنستجرام / يوتيوب) أو الفيديو نفسه هنا في الشات، وممكن تضيف:\n"
                    "• وقت: 1:20-2:05\n• الشيخ ...\n• عنوان: ...\n"
                    "الأطول من دقيقة بيتقسم أجزاء ورا بعض."), _gen_kb([[("⬅️ رجوع", "g:t")]])
    elif data == "g:s":                 # ask for the passage as a reply (force_reply opens the keyboard)
        import json
        api("sendMessage", chat_id=chat, text=f"{ASK_PASSAGE} كرد على الرسالة دي، مثلًا:\n"
                                              "الكهف 1-10\nالبقرة 286\n18:1-10\n"
                                              "أو اسم السورة بس وأنا أختار مقطع منها.",
            reply_markup=json.dumps({"force_reply": True, "input_field_placeholder": "الكهف 1-10"}))
        text, note = "✍️ مستني اسم السورة والآيات (تحت).", "اكتب السورة"
    elif p[1] == "P" and len(p) == 4:   # typed passage + sheikh -> one video
        s, ab = p[2].split(".")
        a, _, b = ab.partition("-")
        rid = "" if p[3] == "-" else _reciters(cfg)[int(p[3])][0]
        text = _enqueue(cfg, {"count": 1, "theme": "", "reciter": rid, "passage": f"{s}:{a}-{b or a}",
                              "passage_name": _surah_names().get(int(s), s) + f" {a}" + (f"-{b}" if b and b != a else "")})
        note = "اتبدأ"
    elif p[1] == "q" and len(p) == 5:   # theme + sheikh + count -> start
        rid = "" if p[3] == "-" else _reciters(cfg)[int(p[3])][0]
        theme = "" if p[2] == "-" else _themes(cfg)[int(p[2])]
        text = _enqueue(cfg, {"count": max(1, min(GENERATE_MAX, int(p[4]))), "theme": theme, "reciter": rid,
                              "passage": ""})
        note = "اتبدأ"
    else:
        text, kb = generate_menu(cfg, data)
    try:
        params = {"chat_id": chat, "message_id": mid, "text": text}
        if kb:
            params["reply_markup"] = kb
        api("editMessageText", **params)
    except TelegramError as e:
        log.warning("generate menu: %s", e)
    try:
        api("answerCallbackQuery", callback_query_id=cq["id"], text=note)
    except TelegramError:
        pass


# ---------------------------------------------------------------- typed surah / ayat
_surahs: dict | None = None
_TASHKEEL = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭـ]")


def _surah_list() -> list[dict]:
    global _surahs
    if _surahs is None:
        r = requests.get("https://api.quran.com/api/v4/chapters", params={"language": "ar"}, timeout=30)
        r.raise_for_status()
        _surahs = {c["id"]: c for c in r.json()["chapters"]}
    return list(_surahs.values())


def _surah_names() -> dict[int, str]:
    try:
        return {c["id"]: c["name_arabic"] for c in _surah_list()}
    except requests.RequestException:
        return {}


def _key(name: str) -> str:
    s = _TASHKEEL.sub("", name).replace("سورة", "").strip()
    s = s.translate(str.maketrans("أإآٱ", "اااا")).replace("ة", "ه").replace("ى", "ي").replace(" ", "")
    return s[2:] if s.startswith("ال") else s


def parse_passage(cfg, text: str) -> tuple[int, int, int] | str:
    """'الكهف 1-10' / 'سورة البقرة ٢٨٦' / '18:1-10' / 'الكهف' -> (surah, from, to), or an error text."""
    t = text.translate(_AR_DIGITS).strip()
    m = re.match(r"^\s*(\d{1,3})\s*[:：]\s*(\d{1,3})(?:\s*[-–]\s*(\d{1,3}))?\s*$", t)
    chapters = {c["id"]: c for c in _surah_list()}
    if m:
        s, a, b = int(m.group(1)), int(m.group(2)), int(m.group(3) or m.group(2))
    else:
        nums = [int(x) for x in re.findall(r"\d{1,3}", t)]
        name = _key(re.sub(r"[\d\-–:]+", " ", t))
        found = [c for c in chapters.values() if _key(c["name_arabic"]) == name] or \
                [c for c in chapters.values() if name and name in _key(c["name_arabic"])]
        if not found:
            return "مش لاقي سورة بالاسم ده. اكتبها زي المصحف، مثلًا: الكهف 1-10"
        s = found[0]["id"]
        if not nums:                     # surah only: one of our prepared passages from it, at random
            import json
            import random
            ps = [p for p in json.loads((ROOT / cfg["paths"]["passages"]).read_text(encoding="utf-8"))["passages"]
                  if p["surah"] == s and p["from"] > 1]
            if not ps:
                return f"اكتب أرقام الآيات كمان، مثلًا: {found[0]['name_arabic']} 1-5"
            p = random.choice(ps)
            return s, p["from"], p["to"]
        a, b = nums[0], nums[1] if len(nums) > 1 else nums[0]
    if s not in chapters:
        return "رقم السورة لازم من 1 لـ 114"
    n = chapters[s]["verses_count"]
    if not (1 <= a <= b <= n):
        return f"سورة {chapters[s]['name_arabic']} فيها {n} آية بس"
    if a == 1 and s != 1:   # some reciters' ayah 1 audio carries the basmala, the text never does
        if b == 1:
            return "الآية 1 لوحدها مش بتنفع (البسملة بتختلف بين القراء) — ابدأ من آية 2"
        a = 2
    if b - a + 1 > 20:
        return "كتير على فيديو دقيقة — اختار 20 آية أو أقل (الطويلة بتتقص لو عدت الدقيقة)"
    return s, a, b


ID_RE = re.compile(r"🆔\s*([\w-]+)")
STATUS = {"approved": "✅ اتوافق عليه", "rejected": "❌ مرفوض — هيتعمل بديل", None: "⏳ مستني موافقتك",
          "waiting": "⏳ مستني موافقتك", "auto": "🤖 اتوافق تلقائيًا (المصحح) — تقدر ترفضه قبل النشر"}


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
🤖 لو مراجعتش مقطع لينك أو درس خلال ساعتين، المصحح بيراجع الترجمة (فصحى وعامية) ويصححها
   ويوافق لوحده — إلا لو فيه آية أو حديث مش واضح أو مش واثق، ساعتها بيستناك. الآيات والأحاديث مش بيلمسها.
   ↩️ أو ❌ بعدها بيلغوا موافقته.
/generate 5  اعمل 5 فيديوهات قرآن جديدة جاهزة للطابور (من 1 لـ 10)
/que عدد الفيديوهات في كل حالة (متوافق ومستني النشر، اتنشر، مرفوض، مستني موافقتك، تحت التجهيز، اللي شاركته)
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
        if video_id.startswith(("c-", "l-")):      # spoken clips: run the correction agent now
            rows[0].insert(1, {"text": "🤖 صحّح", "callback_data": f"f:{video_id}"})
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
    if r.get("kind") == "hadith":
        hd = r.get("hadith") or {}
        head += f"\n📜 حديث · {hd.get('attribution', '')} · {hd.get('grade', '')}" + \
            (f"\n🏷 {r['topic']}" if r.get("topic") else "") + (f"\n🔗 {hd['source']}" if hd.get("source") else "")
    elif r.get("kind") == "clip":
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
    st = e.get("status")
    if (st is None and (r.get("auto") or {}).get("status") == "approved") or \
            (st == "approved" and e.get("by") == "auto"):
        st = "auto"
    s = f"{head}\n\n{cap}\n\nالحالة: {STATUS.get(st)}"
    if (r.get("auto") or {}).get("status") == "held" and not e.get("status"):
        s += f"\n🤖 المصحح مستنيك: {r['auto'].get('why', '')}"
    if not e.get("status"):
        s += "\n↩️ رد على الفيديو بهاشتاجات جديدة لتعديلها (+#وسم ‎-#وسم للإضافة/الحذف)"
    return s[:1024]


def fetch_updates(state: dict) -> tuple[list[dict], bool]:
    """New updates: from the instant Cloudflare Worker (webhook mode, worker/bot.js) when it is set
    up, else straight from Telegram (getUpdates). Returns (updates, came_from_worker)."""
    w, k = os.environ.get("WORKER_URL"), os.environ.get("WORKER_KEY")
    if w and k:
        import json
        got: dict[int, dict] = {}
        if os.environ.get("UPDATE_JSON"):           # handed over directly with this run (instant path)
            try:
                u = json.loads(os.environ["UPDATE_JSON"])
                got[u["update_id"]] = u
            except (ValueError, KeyError):
                pass
        try:                                        # plus anything a superseded run left behind
            r = requests.get(w.rstrip("/") + "/updates", headers={"x-worker-key": k}, timeout=30)
            r.raise_for_status()
            for u in r.json():
                got.setdefault(u["update_id"], u)
        except requests.RequestException as e:
            log.warning("worker unreachable (%s)", e)
        done = set(state.get("done_ids", []))      # never handle one update twice
        new = [got[i] for i in sorted(got) if i not in done]
        state["done_ids"] = sorted(done | set(got))[-500:]
        state["ack_ids"] = sorted(got)              # delete all of them from the Worker afterwards
        return new, True
    try:
        return api("getUpdates", offset=state["offset"], timeout=0,
                   allowed_updates='["message","callback_query"]'), False
    except TelegramError as e:          # webhook is set: Telegram keeps the updates for the Worker
        log.warning("getUpdates: %s", e)
        return [], False


def write_menu_data(cfg) -> None:
    """data/bot_menu.json — the themes and sheikhs the Worker shows in the /generate menu
    (same order as here, so the button numbers mean the same thing on both sides)."""
    path = ROOT / "data" / "bot_menu.json"
    data = {"themes": _themes(cfg), "reciters": [list(x) for x in _reciters(cfg)],
            "hadith_topics": _hadith_topics()}
    if read_json(path, None) != data:
        write_json(path, data)


def quran_check(cfg, r: dict) -> str | None:
    """Automatic checks standing in for your review of a Quran video. None = fine, else the reason.
    (The ayah text itself was already verified against two sources when the video was made.)"""
    from ..publish import hosting
    v = cfg["video"]
    if r.get("kind") == "hadith":       # text exactly as HadeethEnc gives it, authentic grades only
        hd = r.get("hadith") or {}
        if not hd.get("text") or not hd.get("attribution"):
            return "نص الحديث أو مصدره ناقص"
        if not any(g in (hd.get("grade") or "") for g in ("صحيح", "حسن")):
            return f"درجة الحديث مش صحيح/حسن ({hd.get('grade')})"
    elif not r.get("ayat") or any(not a.get("text") for a in r["ayat"]):
        return "نص الآيات ناقص"
    if not 5 <= float(r.get("duration") or 0) <= float(v["max_duration"]) + 1:
        return f"مدة الفيديو غلط ({r.get('duration')} ث)"
    if not all(((r.get("platforms") or {}).get(p) or {}).get("caption") for p in ("youtube", "tiktok")):
        return "الوصف ناقص"
    url = (r.get("hosting") or {}).get("url")
    if not url or (r.get("hosting") or {}).get("deleted") or not hosting.url_is_public(url):
        return "ملف الفيديو مش موجود على الاستضافة"
    return None


def fixed_auto(cfg, reviews: dict) -> bool:
    """A clip you sent to the agent with 🤖 and then didn't review for agent.delay_hours: approve it
    — but only when the agent was confident (its decision was "approve"); otherwise it keeps
    waiting for you. Your ✅/❌/↩️ always wins."""
    now = datetime.now(timezone.utc)
    delay = timedelta(hours=float((cfg.get("agent") or {}).get("delay_hours", 2)))
    rerun = False
    for p in sorted((ROOT / "queue").glob("*.json")):
        r = read_json(p, {})
        a, tg = r.get("auto") or {}, r.get("telegram") or {}
        if a.get("status") != "fixed" or a.get("decision") != "approve" or r.get("published") \
                or store.get(r["id"], reviews).get("status") or not tg.get("message_id"):
            continue
        sent = tg.get("sent_at") or a.get("at")
        if not sent or now - datetime.fromisoformat(sent) < delay:
            continue
        store.set_status(reviews, r["id"], "approved", by="auto")
        log.info("%s auto-approved after its requested correction", r["id"])
        try:
            api("editMessageCaption", chat_id=owner(), message_id=tg["message_id"], caption=_caption(r, reviews),
                reply_markup=_kb(r["id"], "approved"))
        except TelegramError:
            pass
        rerun = True
    return rerun


def quran_auto(cfg, reviews: dict, today: str) -> bool:
    """Daily Quran videos you haven't touched for publishing.auto_approve_hours: run the checks and
    approve them (or reject them, so a replacement is made). Your ✅/❌/↩️ always wins. Recordings
    that need a human (review_required) are never auto-approved. Returns True when today's
    schedule needs another publishing run."""
    pc = cfg["publishing"]
    if not pc.get("auto_approve", True):
        return False
    now = datetime.now(timezone.utc)
    delay = timedelta(hours=float(pc.get("auto_approve_hours", 2)))
    rerun = False
    for p in sorted(OUTPUT.glob("*/q-*.json")) + sorted((ROOT / "queue").glob("h-*.json")):   # Quran + hadith
        r = read_json(p, {})
        tg = r.get("telegram") or {}
        if not tg.get("message_id") or r.get("published") or r.get("rejected") or r.get("review_required") \
                or store.get(r["id"], reviews).get("status") or r.get("date", "") < today:
            continue
        sent = tg.get("sent_at")
        if sent and now - datetime.fromisoformat(sent) < delay:
            continue
        problem = quran_check(cfg, r)
        store.set_status(reviews, r["id"], "rejected" if problem else "approved", by="auto")
        log.info("%s auto-%s %s", r["id"], "rejected" if problem else "approved", problem or "")
        if problem:
            api("sendMessage", chat_id=owner(), reply_to_message_id=tg["message_id"],
                text=f"🤖 الفيديو ده فشل في الفحص التلقائي ({problem}) — اترفض وهيتعمل بداله.")
        try:
            api("editMessageCaption", chat_id=owner(), message_id=tg["message_id"], caption=_caption(r, reviews),
                reply_markup=_kb(r["id"], store.get(r["id"], reviews).get("status")))
        except TelegramError:
            pass
        if problem or r.get("date") == today:
            rerun = True                # replace it / schedule today's video now
    return rerun


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
            r["telegram"] = {"message_id": msg["message_id"], "chat_id": owner(),
                             "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            write_json(p, r)
            sent += 1
            log.info("sent %s for review", r["id"])
    if waiting_today:
        api("sendMessage", chat_id=owner(),
            text=f"⏳ {waiting_today} فيديو من فيديوهات النهارده مستنيين — لو معملتش حاجة خلال "
                 f"{cfg['publishing'].get('auto_approve_hours', 2):g} ساعة، بيتفحصوا تلقائي ويتوافق عليهم "
                 "ويتجدولوا لوحدهم. ❌ أي وقت قبل النشر بيلغي.")
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
    r["telegram"] = {"message_id": msg["message_id"], "chat_id": owner(),
                     "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if r.get("kind") == "clip":
        from .edit import numbered_text
        t = api("sendMessage", chat_id=owner(), text=numbered_text(r), reply_to_message_id=msg["message_id"])
        r["telegram"]["text_msg_id"] = t["message_id"]
    write_json(path, r)
    log.info("sent %s for review", r["id"])


def refresh_caption(path: Path) -> None:
    """Re-draw a sent video's caption (after the agent approved / held it)."""
    r = read_json(path, {})
    tg = r.get("telegram") or {}
    if not tg.get("message_id"):
        return
    reviews = store.load()
    try:
        api("editMessageCaption", chat_id=owner(), message_id=tg["message_id"], caption=_caption(r, reviews),
            reply_markup=_kb(r["id"], store.get(r["id"], reviews).get("status")))
    except TelegramError as e:
        log.warning("refresh caption %s: %s", r["id"], e)


def agent_due(cfg, reviews: dict, state: dict) -> list[dict]:
    """Clips you haven't touched for agent.delay_hours -> one correction-agent job each.
    Only lecture / link clips; Quran recitation videos never go to the agent."""
    a = cfg.get("agent") or {}
    if not a.get("enabled", True) or os.environ.get("AGENT_KEY_SET") == "false":
        return []                       # no ANTHROPIC_API_KEY secret yet: clips simply wait for you
    now = datetime.now(timezone.utc)
    delay = timedelta(hours=float(a.get("delay_hours", 2)))
    started = state.setdefault("agent_started", {})
    if any(now - datetime.fromisoformat(v["at"]) < timedelta(minutes=15) for v in started.values()):
        return []                       # the last agent job is probably still running
    out = []
    for p in sorted((ROOT / "queue").glob("*.json")):
        r = read_json(p, {})
        if r.get("kind") != "clip" and r.get("type") != "lecture":
            continue
        e = store.get(r["id"], reviews)
        if r.get("published") or r.get("auto") or r.get("edited_at") or e.get("status") \
                or e.get("tags") is not None or not (r.get("telegram") or {}).get("message_id"):
            continue
        sent = (r.get("telegram") or {}).get("sent_at") or r.get("queued_at")
        if not sent or now - datetime.fromisoformat(sent) < delay:
            continue
        prev = started.get(r["id"])
        if prev and (prev.get("tries", 0) >= 2 or now - datetime.fromisoformat(prev["at"]) < timedelta(hours=3)):
            continue                    # running now, or already failed twice
        started[r["id"]] = {"at": now.isoformat(timespec="seconds"), "tries": (prev or {}).get("tries", 0) + 1}
        out.append({"id": f"auto-{r['id']}", "autofix": r["id"]})
        if len(out) >= int(a.get("max_per_poll", 1)):
            break                       # one at a time: parallel jobs hit the free tier's rate limit
    return out


def _running_jobs() -> dict[str, int]:
    """Clip / daily jobs queued or running on GitHub right now (videos being made)."""
    repo, tok = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    out = {"clip": 0, "daily": 0}
    if not repo or not tok:
        return out
    for wf, k in (("clip.yml", "clip"), ("daily.yml", "daily")):
        for st in ("in_progress", "queued"):
            try:
                r = requests.get(f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/runs",
                                 params={"status": st, "per_page": 50}, timeout=20,
                                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
                out[k] += r.json().get("total_count", 0) if r.ok else 0
            except requests.RequestException:
                pass
    return out


def queue_text(cfg, reviews: dict, state: dict) -> str:
    """/que — how many videos are in each state."""
    now = datetime.now(timezone.utc)
    recs = []
    for p in sorted(OUTPUT.glob("*/q-*.json")):
        r = read_json(p, {})
        if (r.get("telegram") or {}).get("message_id") or r.get("published"):   # real ones, not old tests
            recs.append(r)
    recs += [read_json(p, {}) for p in sorted((ROOT / "queue").glob("*.json"))]
    c = {k: [] for k in ("approved", "scheduled", "published", "waiting", "held", "rejected")}
    for r in recs:
        pub = r.get("published") or {}
        dues = [datetime.fromisoformat(i["due"]) for i in pub.values() if i.get("due")]
        if store.is_rejected(r, reviews):
            c["rejected"].append(r["id"])
        elif pub and (any(i.get("status") == "sent" for i in pub.values()) or any(d <= now for d in dues)):
            c["published"].append(r["id"])
        elif pub:
            c["scheduled"].append(r["id"])
        elif store.is_approved(r, reviews):
            c["approved"].append(r["id"])
        else:
            c["held" if (r.get("auto") or {}).get("status") == "held" else "waiting"].append(r["id"])
    jobs = _running_jobs()
    shared = state.get("shared")
    n_shared = len(shared) if shared is not None else len({m.group(1) for r in recs
                                                            if (m := re.match(r"(c-\d{8}-[^-]+)", r["id"]))})

    def ids(k: str, n: int = 4) -> str:
        return ("  (" + "، ".join(c[k][:n]) + ("…" if len(c[k]) > n else "") + ")") if c[k] else ""
    return "\n".join([
        "📊 الطابور",
        f"✅ متوافق عليه ومستني النشر: {len(c['approved'])}{ids('approved')}",
        f"📤 متجدول على المنصات (لسه ميعاده مجاش): {len(c['scheduled'])}",
        f"🎬 اتنشر: {len(c['published'])}",
        f"⏳ لسه متوافقش عليه: {len(c['waiting']) + len(c['held'])}"
        + (f" (منهم {len(c['held'])} المصحح مستنيك فيهم)" if c["held"] else "") + ids("waiting"),
        f"❌ اترفض: {len(c['rejected'])}",
        f"⚙️ تحت التجهيز دلوقتي: {jobs['clip']} مقطع لينك" + (f" + تجهيز فيديوهات اليوم" if jobs["daily"] else ""),
        f"🔗 لينكات/فيديوهات شاركتها مع البوت: {n_shared}",
    ])


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
    updates, via_worker = fetch_updates(state)
    write_menu_data(cfg)
    for u in updates:
        state["offset"] = u["update_id"] + 1
        cq, msg = u.get("callback_query"), u.get("message")
        if cq:
            if str(cq["from"]["id"]) != me and str(cq["message"]["chat"]["id"]) != me:
                continue
            if cq["data"].startswith("g:"):     # the /generate menu
                generate_callback(cfg, cq)
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
            if action == "f":                   # 🤖 correct now (you approve afterwards)
                state.setdefault("agent_started", {})[vid] = {
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tries": 2}
                clip_requests.append({"id": f"fix-{vid}", "autofix": vid, "manual": True})
                api("sendMessage", chat_id=me, reply_to_message_id=cq["message"]["message_id"],
                    text=f"🤖 المصحح بدأ على 🆔 {vid} — النسخة المصححة هتوصلك هنا خلال 5-10 دقايق "
                         "ومعاها التعديلات. مش هيوافق لوحده: الموافقة منك.")
                try:
                    api("answerCallbackQuery", callback_query_id=cq["id"], text="🤖 بدأ")
                except TelegramError:
                    pass
                continue
            path, r = _record(vid)
            status = {"a": "approved", "r": "rejected", "u": None}[action]
            if status:
                store.set_status(reviews, vid, status, by=cq["from"].get("username", ""))
            else:               # ↩️ = "wait for me": also cancels an approval made by the agent
                store.set_status(reviews, vid, "waiting", by=cq["from"].get("username", ""))
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
            elif text.startswith("/generate"):
                m = re.search(r"\d+", text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
                if not m:               # no number: show the menu (type -> theme -> count)
                    t, kb = generate_menu(cfg, "g:t")
                    api("sendMessage", chat_id=me, text=t, reply_markup=kb)
                    continue
                n = max(1, min(GENERATE_MAX, int(m.group())))
                GENERATE.append({"count": n, "theme": "", "reciter": "", "passage": ""})
                api("sendMessage", chat_id=me, reply_to_message_id=msg["message_id"],
                    text=f"⚙️ هعمل {n} فيديو قرآن جديد (آيات حكمة وصبر ووعظ أغلبها). هيوصلوك هنا للمراجعة "
                         f"خلال حوالي {3 * n + 5} دقيقة. بعد موافقتك بيدخلوا الطابور ويتنشروا بالترتيب على "
                         "الأيام الجاية في أحسن المواعيد.")
            elif text.startswith("/que") or text.startswith("/queue"):
                api("sendMessage", chat_id=me, text=queue_text(cfg, reviews, state))
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
                state.setdefault("shared", []).append({"id": req["id"], "at": datetime.now(timezone.utc)
                                                       .isoformat(timespec="seconds"),
                                                       "url": req.get("url"), "file": bool(media)})
                if req.get("start") is not None:
                    a, b = int(req["start"]), int(req["end"])
                    when = f" من {a // 60}:{a % 60:02d} لـ {b // 60}:{b % 60:02d}"
                else:
                    when = " (هختار أحسن جزء: جملة كاملة 20-60 ث)"
                api("sendMessage", chat_id=me, reply_to_message_id=msg["message_id"],
                    text=f"✅ استلمت{when}. هاخد الصوت بس وأركبه على فيديو من عندنا بترجمة، "
                         "وهيوصلك هنا للمراجعة خلال حوالي 10-20 دقيقة.")
            elif reply and text and (reply.get("text") or "").startswith(ASK_PASSAGE):
                try:                    # the surah / ayat you typed for /generate
                    res = parse_passage(cfg, text)
                except requests.RequestException:
                    res = "مش قادر أوصل لقائمة السور دلوقتي — جرب تاني كمان شوية"
                if isinstance(res, str):
                    import json
                    api("sendMessage", chat_id=me, reply_to_message_id=msg["message_id"],
                        text=f"{ASK_PASSAGE} — {res}",
                        reply_markup=json.dumps({"force_reply": True, "input_field_placeholder": "الكهف 1-10"}))
                    continue
                s_, a_, b_ = res
                t, kb = generate_menu(cfg, f"g:Q:{s_}.{a_}-{b_}")
                api("sendMessage", chat_id=me, reply_to_message_id=msg["message_id"],
                    text=t.replace(f"{s_}:{a_}-{b_}", f"سورة {_surah_names().get(s_, s_)} {a_}"
                                   + (f"-{b_}" if b_ != a_ else "")), reply_markup=kb)
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
    clip_requests += agent_due(cfg, reviews, state)
    rerun = quran_auto(cfg, reviews, today) or rerun
    rerun = fixed_auto(cfg, reviews) or rerun
    if state.get("commands") != COMMANDS_VERSION:     # the bot's "/" menu
        try:
            import json
            api("setMyCommands", commands=json.dumps([
                {"command": "que", "description": "عدد الفيديوهات في كل حالة"},
                {"command": "generate", "description": "اعمل فيديوهات قرآن جديدة للطابور (مثلا /generate 5)"},
                {"command": "status", "description": "فيديوهات الأيام الجاية"},
                {"command": "help", "description": "إزاي تستخدم البوت"}]))
            state["commands"] = COMMANDS_VERSION
        except TelegramError as e:
            log.warning("setMyCommands: %s", e)
    store.save(reviews)
    write_json(STATE, state)
    if via_worker and state.get("ack_ids"):     # processed: remove them from the Worker's store
        try:
            requests.post(os.environ["WORKER_URL"].rstrip("/") + "/ack", timeout=30,
                          headers={"x-worker-key": os.environ["WORKER_KEY"]},
                          json={"ids": state.pop("ack_ids")}).raise_for_status()
            write_json(STATE, state)
        except requests.RequestException as e:
            log.warning("worker ack: %s", e)
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
        log.info("poll done, rerun daily: %s, generate: %s", rerun, GENERATE)
        import json
        (ROOT / ".cache").mkdir(exist_ok=True)
        if GENERATE:            # all of this poll's requests in ONE daily.yml run (it keeps a single
            jobs, left = [], GENERATE_MAX    # pending run, so separate runs would cancel each other)
            for g in GENERATE:
                n = min(g["count"], left)
                if n > 0:
                    jobs.append({k: g.get(k, "") for k in ("theme", "reciter", "passage")} |
                                {"count": n, "kind": g.get("kind", "quran")})
                    left -= n
            (ROOT / ".cache" / "generate_jobs.json").write_text(json.dumps(jobs, ensure_ascii=False),
                                                                 encoding="utf-8")
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as f:
                f.write(f"rerun={'true' if rerun else 'false'}\n")
                f.write(f"generate={'true' if GENERATE else ''}\n")


if __name__ == "__main__":
    main()
