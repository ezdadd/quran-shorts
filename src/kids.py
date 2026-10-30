"""Kids' story videos: a short illustrated story that leads to an authentic hadith.

    python -m src.kids --top-up --max-new 1      # every 3 h (daily.yml) up to kids.per_day
    python -m src.kids --generate 2              # from the /generate menu

1. a short hadith for children (character, parents, good deeds) from data/hadiths.json
2. Gemini writes 2-3 story scenes in simple Arabic — a boy as the hero; never a prophet, companion,
   woman or girl. The hadith itself is NEVER written by the model: the code adds it word for word.
3. one Flux illustration per scene (Cloudflare Workers AI, free tier), each checked by Gemini vision
   (no women / girls, no drawn faces, no extra limbs or heads, no text, nothing un-Islamic); a
   rejected picture is drawn again with another seed
4. warm narration (Gemini TTS) checked word for word with Whisper
5. picture-book video: the illustration as a card with a slow zoom over a blurred copy, subtitles in
   time with the voice, the hadith's collector + grade under the card

Then like a hadith video: hosted, queued, sent to Telegram, auto-checked after 2 h if untouched.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import random
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from . import ambience, hadith, templates
from .render import CSS_STYLE, FPS, H, W, Frame, write_concat
from .textrender import TextRenderer
from .util import ROOT, cache_dir, load_config, media_duration, path, read_json, run_ffmpeg, setup_logging, write_json

log = logging.getLogger("quran-shorts.kids")
OUT = ROOT / "output" / "kids"
QUEUE = ROOT / "queue"
GEM = "https://generativelanguage.googleapis.com/v1beta/models"
TEXT_MODELS = ["gemini-3.5-flash", "gemini-3.7-flash", "gemini-3.8-flash", "gemini-flash-latest",
               "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]   # lite: when the daily quota is used up
VISION_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-lite-latest", "gemini-3.5-flash"]
KID_TOPICS = ("الأخلاق", "بر الوالدين", "فضائل الأعمال والذكر")
# Flux has no negative prompt: naming a thing ("no crosses") draws it, so the style says only what to
# draw; whatever slips through is caught by the vision check in safe()
STYLE = ("Soft warm children's picture-book illustration, flat pastel colors, clean simple shapes, gentle "
         "morning light, calm and cozy. The child is one small boy seen from behind, wearing a long modest "
         "robe and a small white cap. Simple natural surroundings: trees, grass, houses, sky.")
HEROES = ["عمر", "يوسف", "زياد", "حمزة", "آدم"]
TTS_STYLE = "بصوت رجل دافئ ولطيف بيحكي قصة للأطفال، بتمهّل ووضوح ومخارج حروف سليمة، اقرأ:"
CARD, CARD_X, CARD_Y = 1000, 40, 280
LEAD, TAIL, XF = 0.8, 1.6, 0.6


class KidsError(Exception):
    pass


# ---------------------------------------------------------------- Gemini helpers
def _gemini(models: list[str], parts: list[dict], schema: dict | None = None) -> dict:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise KidsError("GEMINI_API_KEY missing")
    gc = {"temperature": 0.7 if schema else 0, "responseMimeType": "application/json"}
    if schema:
        gc["responseSchema"] = schema
    from .agent.gemini import _thinking
    for m in models:
        try:
            r = requests.post(f"{GEM}/{m}:generateContent", headers={"x-goog-api-key": key}, timeout=90,
                              json={"contents": [{"parts": parts}],
                                    "generationConfig": {**gc, "thinkingConfig": _thinking(m)}})
            if r.status_code in (404, 429) or r.status_code >= 500:
                log.warning("gemini %s: HTTP %s", m, r.status_code)
                continue
            r.raise_for_status()
            return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        except (requests.RequestException, KeyError, ValueError) as e:
            log.warning("gemini %s: %s", m, e)
    raise KidsError("Gemini busy (free daily quota?)")


STORY_SCHEMA = {"type": "OBJECT", "properties": {
    "suitable": {"type": "BOOLEAN"}, "title": {"type": "STRING"},
    "scenes": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "narration": {"type": "STRING"}, "image": {"type": "STRING"}}, "required": ["narration", "image"]}},
    "moral": {"type": "STRING"}, "closing_image": {"type": "STRING"}},
    "required": ["suitable", "title", "scenes", "moral", "closing_image"]}


def write_story(h: dict, rng: random.Random) -> dict:
    _, words = hadith.split_intro(h)
    hero = rng.choice(HEROES)
    prompt = f"""اكتب قصة قصيرة جدًا للأطفال (من 5 لـ 9 سنين) بتمهّد للحديث النبوي ده وبتشرح معناه:
{words}
{h.get('title') or ''}

قواعد لازم تتبع حرفيًا:
- suitable: false لو الحديث مش مناسب لطفل صغير (فيه عقاب أو عذاب أو موت أو دم أو قتال أو تخويف أو معنى صعب)، وساعتها سيب الباقي فاضي.
- 2 أو 3 مشاهد بس. كل مشهد: narration جملة أو جملتين بالعربية الفصحى البسيطة (من غير تشكيل، أقل من 25 كلمة).
- البطل ولد صغير اسمه {hero} (نفس الاسم في كل القصة والعنوان). ممنوع أي نبي أو صحابي أو ملاك كشخصية أو بالاسم في القصة.
- ممنوع أي ست أو بنت أو أم تظهر أو تتذكر في القصة. الكبار رجال بس (جد، أب، جار، معلم).
- ممنوع تكتب نص الحديث أو تقتبس منه: هيتضاف بعد القصة كما هو.
- مفيش موسيقى ولا سحر ولا عنف ولا خوف.
- moral: جملة واحدة قصيرة بالفصحى البسيطة فيها الدرس (أقل من 15 كلمة).
- title: عنوان لطيف (أقل من 6 كلمات).
- image لكل مشهد: وصف بالإنجليزي للمشهد بس (مين فين بيعمل إيه)، من غير ستايل ولا أسماء ولا تعبيرات وش
  (ممنوع smiling / crying / looking at): وصف حركة الجسم والمكان بس. closing_image: مشهد هادي بدون ناس
  (مثلًا صحن مسجد بفوانيس، أو حديقة وقت الغروب).
"""
    s = _gemini(TEXT_MODELS, [{"text": prompt}], STORY_SCHEMA)
    if not s.get("suitable", True):
        raise KidsError("hadith not suitable for small children")
    s["scenes"] = [x for x in s.get("scenes") or [] if x.get("narration")][:3]
    if len(s["scenes"]) < 2:
        raise KidsError("story too short")
    banned = re.compile(r"(نبي|رسول|صحاب|ملاك|أمه|أمي|ماما|أم |بنت|أخته|جدته|خالته|عمته)")
    for x in s["scenes"] + [{"narration": s["moral"]}]:
        if banned.search(x["narration"]):
            raise KidsError(f"story broke a rule: {x['narration'][:60]}")
    return s


# ---------------------------------------------------------------- illustrations
def draw(prompt: str, out: Path) -> Path:
    acc, tok = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip(), os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not (acc and tok):
        raise KidsError("CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN missing")
    r = requests.post(f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/run/@cf/black-forest-labs/flux-1-schnell",
                      headers={"Authorization": f"Bearer {tok}"}, timeout=120,
                      json={"prompt": f"{STYLE} Scene: {prompt}.", "steps": 8})
    d = r.json()
    if not d.get("success"):
        raise KidsError(f"image: {d.get('errors')}")
    out.write_bytes(base64.b64decode(d["result"]["image"]))
    return out


VISION_PROMPT = """This is an illustration for an Islamic children's video. Answer JSON only:
{"ok": true|false, "reason": "<short English>"}. ok=false if it shows: any woman or girl (also in hijab);
a clearly drawn face with eyes, nose or mouth; extra, missing or deformed heads, limbs or bodies; any text,
letters or logo; a halo or a holy/prophet-like figure; a cross, idol, statue, church or temple; alcohol,
pigs, music instruments; immodest clothing; anything scary or violent. Faceless or back-view boys,
back-view men, animals, nature and buildings are fine."""


def safe(img: Path) -> tuple[bool, str]:
    from PIL import Image
    import io
    im = Image.open(img).convert("RGB")
    im.thumbnail((768, 768))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    v = _gemini(VISION_MODELS, [{"text": VISION_PROMPT},
                                {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()}}])
    return bool(v.get("ok")), str(v.get("reason", ""))[:100]


def illustrate(prompt: str, work: Path, name: str, tries: int = 4) -> Path:
    last = ""
    for k in range(tries):
        p = draw(prompt, work / f"{name}_{k}.jpg")
        ok, why = safe(p)
        log.info("illustration %s try %d: %s %s", name, k + 1, "OK" if ok else "REJECTED", why)
        if ok:
            return p
        last = why
    raise KidsError(f"no acceptable picture for «{prompt[:40]}» ({last})")


# ---------------------------------------------------------------- video
def _scene_clip(img: Path, dur: float, out: Path) -> Path:
    z = 0.06
    fc = (f"[0:v]split[a][b];"
          f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=28:2,"
          f"eq=brightness=-0.16:saturation=0.85,drawbox=x={CARD_X - 6}:y={CARD_Y - 6}:w={CARD + 12}:h={CARD + 12}:"
          f"color=white@0.85:t=6[bg];"
          f"[b]scale=w='2*trunc({CARD}*(1+{z}*min(t/{dur:.3f},1))/2)':h=-2:eval=frame,crop={CARD}:{CARD}[fg];"
          f"[bg][fg]overlay={CARD_X}:{CARD_Y},fps={FPS},format=yuv420p[v]")
    run_ffmpeg(["-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.3f}", "-i", str(img), "-filter_complex", fc,
                "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-t", f"{dur:.3f}", str(out)],
               "kids scene")
    return out


def render(cfg, story: dict, h: dict, images: list[Path], vid: str, rng: random.Random) -> tuple[Path, dict]:
    from . import tts
    from .qc import ensure
    work = cache_dir(cfg, "work", vid)
    _, words = hadith.split_intro(h)
    attr = h.get("attribution") or ""
    texts = [s["narration"] for s in story["scenes"]] + [f"قال رسول الله صلى الله عليه وسلم: {words}",
                                                         story["moral"]]
    shown = texts[:-2] + [f"قال رسول الله ﷺ: {words}", story["moral"]]
    kc = cfg.get("kids") or {}
    wav, heard, model = tts.narrate(cfg, " ".join(texts), work, voice=kc.get("voice", "Puck"), style=TTS_STYLE)
    total = round(LEAD + media_duration(wav) + TAIL, 2)
    if total > 59:
        raise KidsError(f"story too long for a Short ({total:.0f}s)")
    # when each part starts: the voice reaching its first word
    counts = [len(t.split()) for t in texts]
    n_all = sum(counts)
    starts, i = [], 0
    for c in counts:
        k = min(len(heard) - 1, round(i * len(heard) / max(1, n_all)))
        starts.append(0.0 if not starts else LEAD + heard[k]["s"] - 0.2)
        i += c
    ends = starts[1:] + [total]
    # pictures: story scenes, then the closing picture for the hadith and the moral
    pics = images[:len(story["scenes"])] + [images[-1], images[-1]]
    durs = [e - s for s, e in zip(starts, ends)]
    # merge the last two (same picture) into one clip
    pics, durs = pics[:-1], durs[:-2] + [durs[-2] + durs[-1]]
    clips = [_scene_clip(p, d + (XF if k < len(pics) - 1 else 0), work / f"scene_{k}.mp4")
             for k, (p, d) in enumerate(zip(pics, durs))]
    args, fc, last, off = [], [], "0:v", 0.0
    for k, c in enumerate(clips):
        args += ["-i", str(c)]
    for k in range(1, len(clips)):
        off += durs[k - 1]
        fc.append(f"[{last}][{k}:v]xfade=transition=fade:duration={XF}:offset={max(0.1, off - XF / 2):.3f}[x{k}]")
        last = f"x{k}"
    bg = work / "bg.mp4"
    run_ffmpeg([*args, "-filter_complex", ";".join(fc) + f";[{last}]trim=0:{total:.3f},setpts=PTS-STARTPTS[v]"
                if fc else f"[0:v]trim=0:{total:.3f},setpts=PTS-STARTPTS[v]", "-map", "[v]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", str(bg)], "kids background")
    # subtitles + labels
    sub = (cfg["templates"].get("sub_fonts") or {}).get("readex") or {"file": "ReadexPro[HEXP,wght].ttf",
                                                                      "css_weight": 600, "size": 64}
    frames: list[Frame] = []
    with TextRenderer(cfg) as tr:
        tr.use_fonts(sub["file"], label_font="Amiri-Regular.ttf")
        for k, (t, a, b) in enumerate(zip(shown, starts, ends)):
            hadith_line = k == len(shown) - 2
            info = tr.layout_ayah(t.split(), None, style=CSS_STYLE["shadow"],
                                  color="#F2D27A" if hadith_line else "#FFFFFF", accent="#F2D27A",
                                  size=int(sub.get("size", 64)) - (6 if hadith_line else 0), center_y=1500,
                                  max_h=380, open_bracket=False, close_bracket=False,
                                  weight=sub.get("css_weight", 600), min_size=44)
            fr = Frame(tr.state(info).image, max(0.3, a), b - 0.1)
            fr.fade_in = fr.fade_out = True
            frames.append(fr)
        concat = write_concat(frames, total, work, "rise")
        brand = cfg.get("branding", {})
        lab = work / "_labels.png"
        tr.labels([{"text": story["title"], "y": 120, "size": 56},
                   {"text": " · ".join(x for x in (attr, h.get("grade")) if x), "y": 200,
                    "size": 36, "pill": True},
                   {"text": brand.get("handle", ""), "y": brand.get("y", 1735), "size": brand.get("size", 32),
                    "pill": True}]).save(lab)
    amb = ambience.pick(cfg, "flowers", rng)
    out = OUT / f"{vid}.mp4"
    OUT.mkdir(parents=True, exist_ok=True)
    ms = int(LEAD * 1000)
    lo, hi = cfg["ambience"]["level_db"]
    fc = [f"[1:v]fps={FPS},format=rgba[txt]", "[2:v]format=rgba[lab]",
          "[0:v][txt]overlay=0:0:format=auto[v1]",
          f"[v1][lab]overlay=0:0:format=auto,fade=t=in:st=0:d=0.5,fade=t=out:st={total - 0.7:.3f}:d=0.7,"
          f"format=yuv420p[vout]",
          f"[3:a]adelay={ms}|{ms},apad=whole_dur={total:.3f},aresample=48000,aformat=channel_layouts=stereo,"
          f"loudnorm=I=-16:TP=-1.5:LRA=11[vo]"]
    args = ["-i", str(bg), "-f", "concat", "-safe", "0", "-i", str(concat), "-loop", "1", "-t", f"{total:.3f}",
            "-i", str(lab), "-i", str(wav)]
    if amb:
        args += ["-stream_loop", "-1", "-i", str(amb)]
        fc += [f"[4:a]atrim=0:{total:.3f},asetpts=N/SR/TB,aresample=48000,aformat=channel_layouts=stereo,"
               f"loudnorm=I=-20:TP=-2,volume={rng.uniform(lo, hi) - 4:.1f}dB[amb]",
               f"[vo][amb]amix=inputs=2:normalize=0:duration=first,loudnorm=I=-14:TP=-1.5:LRA=11,"
               f"atrim=0:{total:.3f}[aout]"]
    else:
        fc.append(f"[vo]loudnorm=I=-14:TP=-1.5:LRA=11,atrim=0:{total:.3f}[aout]")
    v = cfg["video"]
    run_ffmpeg([*args, "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]", "-t", f"{total:.3f}",
                "-r", str(FPS), "-c:v", "libx264", "-preset", v["preset"], "-crf", str(v["crf"]),
                "-maxrate", v["maxrate"], "-bufsize", v["bufsize"], "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-b:a", v["audio_bitrate"], "-movflags", "+faststart", str(out)], "kids compose")
    ensure(out, total)
    return out, {"duration": total, "narration": model, "texts": shown}


# ---------------------------------------------------------------- making + queueing
def pick(history: dict, rng: random.Random) -> dict | None:
    used = set(history.get("kids_used", {}))
    pool = [h for h in hadith.load() if h["id"] not in used and h["words"] <= 20
            and any(t in KID_TOPICS for t in h.get("topics") or [])]
    return rng.choice(pool) if pool else None


def captions(story: dict, h: dict, rng: random.Random) -> dict:
    _, words = hadith.split_intro(h)
    src = " · ".join(x for x in (h.get("attribution"), h.get("grade")) if x)
    tags = ["#قصص_أطفال", "#أطفال_مسلمين", "#حديث", "#أخلاق", "#تربية_إسلامية", "#قصص_إسلامية"]
    out = {}
    for p in ("youtube", "tiktok", "instagram", "facebook"):
        t = rng.sample(tags, 4) + (["#shorts"] if p == "youtube" else [])
        out[p] = {"caption": f"🌟 {story['title']}\n{story['moral']}\n\nقال رسول الله ﷺ: {words}\n{src}\n\n{' '.join(t)}",
                  "hashtags": t}
    out["youtube"]["title"] = f"قصة للأطفال | {story['title']}"[:100]
    return out


def make(cfg, n: int) -> list[Path]:
    from .clips import publish_prep
    today = datetime.now(ZoneInfo(cfg["timezone"])).date()
    hp = path(cfg, "history")
    history = read_json(hp, {})
    rng = random.Random()
    queued = []
    for _ in range(n):
        h = pick(history, rng)
        if not h:
            raise KidsError("مفيش أحاديث مناسبة للأطفال لسه")
        k = 1
        while (QUEUE / f"k-{today:%Y%m%d}-{k:02d}.json").exists():
            k += 1
        vid = f"k-{today:%Y%m%d}-{k:02d}"
        try:
            story = write_story(h, rng)
            work = cache_dir(cfg, "work", vid)
            imgs = [illustrate(s["image"], work, f"s{i}") for i, s in enumerate(story["scenes"])]
            imgs.append(illustrate(story["closing_image"], work, "close"))
            mp4, info = render(cfg, story, h, imgs, vid, rng)
        except Exception as e:              # this hadith didn't work out: next time another one
            log.error("[%s] kids video failed: %s", vid, e)
            history.setdefault("kids_used", {})[h["id"]] = f"failed {today}"
            write_json(hp, history)
            continue
        rec = {"id": vid, "type": "kids", "kind": "kids", "date": today.isoformat(),
               "file": str(mp4.relative_to(ROOT)).replace("\\", "/"), "duration": info["duration"],
               "hadith": {k_: h.get(k_) for k_ in ("id", "title", "intro", "text", "attribution", "grade", "source")},
               "story": story, "texts": info["texts"], "topic": (h.get("topics") or [""])[0],
               "narration": info["narration"], "category": "kids",
               "platforms": captions(story, h, rng), "published": {}, "approved": False}
        rp = OUT / f"{vid}.json"
        write_json(rp, rec)
        queued.append(publish_prep(cfg, rp))
        history.setdefault("kids_used", {})[h["id"]] = today.isoformat()
        write_json(hp, history)
        log.info("[%s] kids story «%s» on hadith %s (%.0fs) -> queued", vid, story["title"], h["id"], info["duration"])
    return queued


def today_count(cfg) -> int:
    from .review import store
    today = datetime.now(ZoneInfo(cfg["timezone"])).date().isoformat()
    revs = store.load()
    return sum(1 for p in QUEUE.glob("k-*.json")
               if (r := read_json(p, {})).get("date") == today and not store.is_rejected(r, revs))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--generate", type=int)
    ap.add_argument("--top-up", action="store_true")
    ap.add_argument("--max-new", type=int)
    a = ap.parse_args(argv)
    setup_logging()
    cfg = load_config()
    if not (cfg.get("kids") or {}).get("enabled", True):
        return
    n = a.generate or 0
    if a.top_up:
        n = max(0, int((cfg.get("kids") or {}).get("per_day", 1)) - today_count(cfg))
    if a.max_new is not None:
        n = min(n, a.max_new)
    if n <= 0:
        log.info("no kids video needed now")
        return
    from .review import telegram
    chat = telegram.owner() if os.environ.get("TELEGRAM_BOT_TOKEN") else None
    try:
        queued = make(cfg, n)
    except KidsError as e:
        log.error("%s", e)
        if chat:
            telegram.api("sendMessage", chat_id=chat, text=f"🧒 {e}")
        return
    if chat:
        for q in queued:
            telegram.send_one(cfg, q)


if __name__ == "__main__":
    main()
