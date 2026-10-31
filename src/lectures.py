"""Phase 2 — lecture shorts (30–60 s) with animated Arabic subtitles, reviewed before publishing.

    python -m src.lectures prepare [--sheikh samir_mostafa] [--per-file 5]
    python -m src.lectures list
    python -m src.lectures render <id>      # re-render after editing the JSON (subtitle text, title)
    python -m src.lectures approve <id>

Pipeline: faster-whisper transcript (word timings, cached) -> sentence units split on pauses ->
candidate windows that start and end on clear pauses -> scored -> rendered to output/review/.
Quran quotes inside a clip are detected against the full Quran text and shown with the exact
API text; hadith cues are flagged for manual checking. Nothing is published until approved.
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import re
import subprocess
from pathlib import Path

from PIL import Image

from . import asr, quran
from .align import norm
from .render import CSS_STYLE, FPS, H, W, Frame, write_concat
from .textrender import TextRenderer
from .util import (FFPROBE, ROOT, cache_dir, http_json, load_config, read_json, run_ffmpeg,
                   setup_logging, write_json)

log = logging.getLogger("quran-shorts.lectures")

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm"}
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac"}
SHEIKHS = {"samir_mostafa": "الشيخ سمير مصطفى", "amgad_samir": "الشيخ أمجد سمير"}
# a clip should not open mid-thought
CONNECTIVES = {"و", "ف", "ثم", "لكن", "ولكن", "بس", "يعني", "عشان", "علشان", "لأن", "لان", "وبعدين",
               "فلما", "فإن", "فان", "أو", "او", "وده", "ودي", "فده", "فدي", "طيب", "يبقى", "يبقي"}
# a clip must not end on a word that needs a continuation
DANGLING = {"من", "في", "فى", "على", "عن", "إلى", "الى", "لـ", "ان", "أن", "إن", "لا", "ما", "و", "ف",
            "يا", "اللي", "ده", "دي", "لو", "لما", "عشان", "علشان", "بس", "مش", "انه", "إنه", "أنه", "كان",
            "يعني", "ثم", "او", "أو", "لأن", "لان", "اللى", "هل", "مع", "بين", "حتى", "كل", "قال", "بيقول"}
HADITH_CUES = ["رسول الله", "النبي", "صلى الله عليه وسلم", "عليه الصلاة والسلام", "الحديث",
               "رواه", "حديث", "قال صلى"]


def review_dir() -> Path:
    d = ROOT / "output" / "review"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- full Quran index
_qindex = None


def quran_index(cfg):
    """3-gram index over the whole Quran (alquran.cloud quran-uthmani), loose spelling keys."""
    global _qindex
    if _qindex is not None:
        return _qindex
    p = cache_dir(cfg, "api") / "quran-uthmani-full.json"
    data = read_json(p, None)
    if data is None:
        data = http_json("https://api.alquran.cloud/v1/quran/quran-uthmani")["data"]
        write_json(p, data)
    basmala_n = None
    seq: list[tuple[int, int, int, str]] = []   # (surah, ayah, word index, key)
    for s in data["surahs"]:
        for a in s["ayahs"]:
            toks = [t for t in a["text"].split() if norm(t)]   # drop standalone pause marks
            if s["number"] == 1 and a["numberInSurah"] == 1:
                basmala_n = len(toks)
            elif a["numberInSurah"] == 1 and s["number"] != 9 and basmala_n:
                toks = toks[basmala_n:]
            for i, t in enumerate(toks):
                seq.append((s["number"], a["numberInSurah"], i, norm(t)))
    idx: dict[tuple, list[int]] = {}
    for k in range(len(seq) - 2):
        idx.setdefault((seq[k][3], seq[k + 1][3], seq[k + 2][3]), []).append(k)
    _qindex = (seq, idx)
    return _qindex


def find_quotes(cfg, words: list[dict], min_words: int = 4) -> list[dict]:
    """Runs of >= min_words consecutive spoken words that match consecutive Quran words."""
    seq, idx = quran_index(cfg)
    keys = [norm(w["w"]) for w in words]
    out = []
    i = 0
    while i < len(keys) - 2:
        best = None
        for k in idx.get((keys[i], keys[i + 1], keys[i + 2]), []):
            n = 3
            while i + n < len(keys) and k + n < len(seq) and keys[i + n] == seq[k + n][3]:
                n += 1
            if best is None or n > best[1]:
                best = (k, n)
        if best and best[1] >= min_words:
            k, n = best
            out.append({"w0": i, "w1": i + n, "from": seq[k][:3], "to": seq[k + n - 1][:3]})
            i += n
        else:
            i += 1
    return out


def quote_text(cfg, q: dict) -> tuple[str, list[str], str]:
    """Exact Quran.com words for a detected quote; falls back to whole ayat if word counts
    between the two sources do not line up."""
    (s, a0, i0), (_, a1, i1) = q["from"], q["to"]
    words: list[str] = []
    exact = True
    for a in range(a0, a1 + 1):
        _, ws, _ = quran.load_text(cfg, s, a)
        lo = i0 if a == a0 else 0
        hi = i1 + 1 if a == a1 else len(ws)
        if hi > len(ws):
            exact = False
            lo, hi = 0, len(ws)
        words += ws[lo:hi]
    ref = f"{s}:{a0}" if a0 == a1 else f"{s}:{a0}-{a1}"
    return " ".join(words), words, ref if exact else ref + " (full ayah shown — check)"


# ---------------------------------------------------------------- clip selection
def units(words: list[dict]) -> list[dict]:
    """Sentence-like units split at pauses or sentence punctuation."""
    out, start = [], 0
    for i, w in enumerate(words):
        gap = words[i + 1]["s"] - w["e"] if i + 1 < len(words) else 9.0
        if gap >= 0.45 or re.search(r"[.!?؟]$", w["w"]):
            out.append({"i0": start, "i1": i + 1, "s": words[start]["s"], "e": w["e"], "gap": gap})
            start = i + 1
    return out


def candidates(words: list[dict], lo: float, hi: float) -> list[dict]:
    us = units(words)
    cands = []
    for a in range(len(us)):
        gap_before = us[a]["s"] - us[a - 1]["e"] if a else 9.0
        if gap_before < 0.45:
            continue
        for b in range(a, len(us)):
            dur = us[b]["e"] - us[a]["s"]
            if dur > hi:
                break
            if dur < lo or us[b]["gap"] < 0.5:
                continue
            ws = words[us[a]["i0"]:us[b]["i1"]]
            first = re.sub(r"[^ء-ي]", "", ws[0]["w"])
            last = re.sub(r"[^ء-ي]", "", ws[-1]["w"])
            if last in DANGLING or len(ws) / dur < 1.3:   # cut mid-phrase / mostly silence
                continue
            prev = words[us[a]["i0"] - 1]["w"] if us[a]["i0"] else "."
            score = 0.0
            score += 2.0 if us[b]["gap"] >= 0.9 else 1.0
            score += 2.5 if re.search(r"[.!?؟]$", ws[-1]["w"]) else -1.5
            score += 1.5 if re.search(r"[.!?؟]$", prev) else 0.0
            score -= 2.5 if first in CONNECTIVES else 0.0
            score += 3.0 * (sum(w["p"] for w in ws) / len(ws) - 0.7)
            score -= abs(dur - 45) / 15
            cands.append({"w0": us[a]["i0"], "w1": us[b]["i1"], "start": us[a]["s"], "end": us[b]["e"],
                          "score": round(score, 3)})
    return cands


def pick(cands: list[dict], n: int, min_gap: float = 5.0) -> list[dict]:
    chosen: list[dict] = []
    for c in sorted(cands, key=lambda c: -c["score"]):
        if all(c["end"] + min_gap <= x["start"] or c["start"] >= x["end"] + min_gap for x in chosen):
            chosen.append(c)
        if len(chosen) >= n:
            break
    return sorted(chosen, key=lambda c: c["start"])


# ---------------------------------------------------------------- subtitle model
def build_segments(cfg, words: list[dict], clip: dict) -> tuple[list[dict], list[dict]]:
    """Subtitle segments (times relative to clip start) + review flags."""
    t0 = clip["start"]
    ws = words[clip["w0"]:clip["w1"]]
    quotes = find_quotes(cfg, ws)
    segs: list[dict] = []
    flags: list[dict] = []
    in_quote = {}
    for q in quotes:
        for i in range(q["w0"], q["w1"]):
            in_quote[i] = q
    line: list[dict] = []

    def flush():
        if line:
            segs.append({"type": "speech", "start": round(line[0]["s"] - t0, 3),
                         "end": round(line[-1]["e"] - t0, 3),
                         "words": [{"w": w["w"], "s": round(w["s"] - t0, 3), "e": round(w["e"] - t0, 3),
                                    "p": w.get("p")} for w in line],
                         "text": " ".join(w["w"] for w in line)})
            line.clear()

    i = 0
    while i < len(ws):
        if i in in_quote:
            flush()
            q = in_quote[i]
            text, qwords, ref = quote_text(cfg, q)
            segs.append({"type": "quran", "start": round(ws[q["w0"]]["s"] - t0, 3),
                         "end": round(ws[q["w1"] - 1]["e"] - t0, 3), "ref": ref, "text": text,
                         "words": qwords})
            i = q["w1"]
            continue
        w = ws[i]
        gap = ws[i + 1]["s"] - w["e"] if i + 1 < len(ws) else 9.0
        line.append(w)
        if len(line) >= 6 or gap >= 0.35 or sum(len(x["w"]) for x in line) >= 30:
            flush()
        i += 1
    flush()
    full = " ".join(w["w"] for w in ws)
    for cue in HADITH_CUES:
        if cue in full:
            flags.append({"type": "hadith", "note": f"الكلام فيه «{cue}» — راجع نص الحديث في الترجمة"})
            break
    for s in segs:
        if s["type"] == "quran":
            flags.append({"type": "quran", "note": f"آية مكتشفة {s['ref']} — اتعرضت بنص الـ API، اتأكد إن الشيخ قالها كدا"})
    if any(w["p"] < 0.5 for w in ws):
        flags.append({"type": "asr", "note": "في كلمات التفريغ مش متأكد منها — راجع النص"})
    return segs, flags


# ---------------------------------------------------------------- rendering
SUB_FONT = "NotoNaskhArabic[wght].ttf"
QURAN_FONT = "ScheherazadeNew-Bold.ttf"


def text_frames(tr: TextRenderer, cfg, segs: list[dict], center_y: int, total: float,
                sub: dict | None = None, style: str = "band", label_font: str = "Amiri-Regular.ttf") -> list[Frame]:
    """sub = the subtitle font entry from templates.sub_fonts ({file, size, css_weight})."""
    sub = sub or {"file": SUB_FONT, "size": 70, "css_weight": 700}
    frames: list[Frame] = []
    kw = dict(color="#FFFFFF", accent="#F2D27A", center_y=center_y, max_h=330, min_size=52,
              open_bracket=False, close_bracket=False)
    for seg in segs:
        if seg["type"] == "quran":
            tr.use_fonts(QURAN_FONT, label_font=label_font)
            info = tr.layout_ayah(seg["words"], None, style=style, size=68, **{**kw, "color": "#F2D27A",
                                  "open_bracket": True, "close_bracket": True, "max_h": 420})
            fr = Frame(tr.state(info).image, seg["start"], seg["end"] + 0.3)
            fr.fade_in = fr.fade_out = True
            frames.append(fr)
            continue
        tr.use_fonts(sub["file"], label_font=label_font)
        edited = seg["text"].strip() != " ".join(w["w"] for w in seg["words"])
        disp = seg["text"].split() if edited else [w["w"] for w in seg["words"]]
        info = tr.layout_ayah(disp, None, style=style, size=sub.get("size", 70),
                              weight=sub.get("css_weight", 400), **kw)
        if edited:   # user changed the text: show the line whole for its duration
            frames.append(Frame(tr.state(info).image, seg["start"], seg["end"]))
            continue
        for k, w in enumerate(seg["words"]):
            nxt = seg["words"][k + 1]["s"] if k + 1 < len(seg["words"]) else seg["end"]
            frames.append(Frame(tr.state(info, word=k).image, w["s"] if k else seg["start"], nxt))
    # keep frames ordered and non-overlapping; each line holds until the next one starts
    frames.sort(key=lambda f: f.start)
    for a, b in zip(frames, frames[1:]):
        if b.start - a.end < 0.6:
            a.end = b.start
        a.end = min(a.end, b.start)
    if frames:
        frames[-1].end = min(total - 0.2, max(frames[-1].end, frames[-1].end + 0.4))
    return [f for f in frames if f.end - f.start > 1e-3]


def face_center_x(src: Path, start: float, end: float) -> float | None:
    import cv2
    det = cv2.FaceDetectorYN.create(str(ROOT / "assets" / "models" / "face_detection_yunet_2023mar.onnx"),
                                    "", (320, 320), 0.7)
    cap = cv2.VideoCapture(str(src))
    xs = []
    t = start + 0.5
    while t < end:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, f = cap.read()
        if not ok:
            break
        h, w = f.shape[:2]
        s = cv2.resize(f, (w // 3, h // 3))
        det.setInputSize((s.shape[1], s.shape[0]))
        _, faces = det.detect(s)
        if faces is not None and len(faces):
            x, y, fw, fh = faces[0][:4] * 3
            xs.append((x + fw / 2) / w)
        t += 2.0
    cap.release()
    return round(float(sorted(xs)[len(xs) // 2]), 4) if xs else None


def render_clip(cfg, rec: dict, tr: TextRenderer, tries: int = 3) -> Path:
    """Render, and if it breaks (FFmpeg crash, bad footage file…) try again with other footage and
    a safe look (no camera move / effects) instead of failing the whole job."""
    from .templates import Template, safe
    for attempt in range(tries):
        try:
            return _render_clip_once(cfg, rec, tr, attempt)
        except Exception as e:
            if attempt == tries - 1:
                raise
            log.error("[%s] render failed (try %d), retrying with other footage and a safe look: %s",
                      rec["id"], attempt + 1, str(e).strip()[:300])
            if rec.get("look"):
                rec["look"] = safe(Template(**rec["look"])).to_dict()


def _render_clip_once(cfg, rec: dict, tr: TextRenderer, attempt: int = 0) -> Path:
    src = ROOT / rec["source"]["file"]
    start, end = rec["clip"]["start"], rec["clip"]["end"]
    total = round(end - start, 3)
    work = cache_dir(cfg, "work", rec["id"])
    is_video = src.suffix.lower() in VIDEO_EXT
    layout = rec["layout"]
    # one random look per clip, saved in the record so a re-render (subtitle edit) keeps it
    from .templates import Template, choose_clip
    if not rec.get("look"):
        rec["look"] = choose_clip(cfg, random.Random(rec["id"])).to_dict()
    tpl = Template(**rec["look"])
    sub = (cfg["templates"].get("sub_fonts") or {}).get(tpl.font)

    # ---- video base
    args = ["-ss", f"{start:.3f}", "-t", f"{total:.3f}", "-i", str(src)]
    fc = []
    geo = None
    if is_video and layout == "screen":
        # the clip itself playing on a device in one of our scenes (laptop, phone, TV...); a retry
        # takes another scene and a plain look
        from . import screens
        if not rec.get("screen") or attempt:
            old = (rec.get("screen") or {}).get("scene")
            look = screens.choose(cfg, random.Random(f"{rec['id']}#{attempt}"), rec.get("portrait", True),
                                  {old} if old and attempt else set())
            rec["screen"] = screens.safe(look) if attempt else look
        base = work / "screen.mp4"
        try:
            geo = screens.compose(cfg, rec["screen"], src, start, total, base, work)
        except screens.NoPicture as e:      # nothing to show on a screen: its sound on our footage
            log.info("[%s] %s — using our footage instead", rec["id"], e)
            rec["layout"], is_video = "audio", False
            rec.pop("screen", None)
    if geo:
        args = ["-i", str(base)]
        fc.append(f"[0:v]fps={FPS},setsar=1[base]")
        sub_y = geo["sub_y"]
    elif is_video and layout == "crop":
        cx = rec.get("face_x") or 0.5
        fc.append(f"[0:v]crop=ih*9/16:ih:'max(0,min(iw-ih*9/16,{cx}*iw-ih*9/32))':0,"
                  f"scale={W}:{H},setsar=1,fps={FPS}[base]")
        sub_y, video_bottom = 1380, None
    elif is_video:
        top = 250
        fc.append(f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,"
                  f"crop={W}:{H},boxblur=30:3,eq=brightness=-0.18:saturation=0.8[bgb];"
                  f"[b]scale={W}:-2,setsar=1[fg];[bgb][fg]overlay=0:{top},fps={FPS}[base]")
        sub_y = 1490
    else:
        # audio-only lecture: calm nature footage + waveform
        from . import footage
        from .render import build_background
        rng = random.Random(rec["id"] if not attempt else f"{rec['id']}#{attempt}")   # retry: other footage
        cat = footage.pick_category(cfg, rng)
        clips = footage.get_clips(cfg, cat, total, {}, rng)
        bg = build_background(cfg, clips, total, tpl, work / "bg.mp4")
        args = ["-i", str(bg)] + args
        fc.append(f"[1:a]asplit[aw][am];[aw]showwaves=s={W}x220:mode=cline:rate={FPS}:colors=white@0.55,format=rgba[wv];"
                  f"[0:v]colorchannelmixer=rr=.6:gg=.6:bb=.6[dk];[dk][wv]overlay=0:820[base]")
        sub_y = 1330
        rec["footage_credit"] = sorted({c.credit for c in clips if c.credit})
    a_src = "[am]" if not is_video else "[0:a]"

    # ---- text layers
    frames = text_frames(tr, cfg, rec["segments"], sub_y, total, sub,
                         CSS_STYLE.get(tpl.background, "band"), tpl.label_font)
    concat = write_concat(frames, total, work, tpl.entrance)
    tr.use_fonts((sub or {}).get("file", SUB_FONT), label_font=tpl.label_font)
    b = cfg.get("branding", {})
    photo = ROOT / rec["sheikh_photo"] if rec.get("sheikh_photo") else None
    photo = photo if photo and photo.exists() and not geo else None    # the sheikh is on screen already
    title_y = 470 if photo else 120          # the framed photo takes the top of the screen
    if geo or rec.get("no_text"):           # shared video: a hook line on top (own style), no subtitles
        hook = " · ".join(x for x in (rec.get("title") or random.Random(rec["id"]).choice(
            (cfg.get("clips") or {}).get("openers") or ["اسمع الكلام ده"]), rec.get("sheikh_name")) if x)
        ts = (rec.get("screen") or {}).get("title", "plain")
        labels = [{"text": hook, "y": geo["title_y"] if geo else 120, "size": 62 if ts == "plain" else 52,
                   **({"box": ts[4:]} if ts.startswith("box_") else {})}]
    else:
        labels = [{"text": rec["title"], "y": title_y, "size": 50}] if rec.get("title") else []
        labels.append({"text": rec["sheikh_name"], "y": b.get("y", 1735) - 75, "size": 42})
    labels.append({"text": b.get("handle", ""), "y": b.get("y", 1735), "size": b.get("size", 32), "pill": True})
    labels = [x for x in labels if x["text"]]
    lab_png = work / "_labels.png"
    lab = tr.labels(labels)
    if photo:                                # sheikh's photo in the circular Islamic frame
        from .badge import badge
        bd = badge(photo, 300)
        lab.alpha_composite(bd, ((W - bd.width) // 2, 110))
    lab.save(lab_png)

    inputs = args + ["-f", "concat", "-safe", "0", "-i", str(concat),
                     "-loop", "1", "-t", f"{total:.3f}", "-i", str(lab_png)]
    txt_idx = 1 if is_video else 2
    lab_idx = txt_idx + 1
    fc.append(f"[{txt_idx}:v]fps={FPS},format=rgba[txt]")
    fc.append(f"[{lab_idx}:v]format=rgba[lab]")
    fc.append("[base][txt]overlay=0:0:format=auto[v1];[v1][lab]overlay=0:0:format=auto,"
              f"fade=t=in:st=0:d=0.3,fade=t=out:st={total - 0.5:.3f}:d=0.5,format=yuv420p[vout]")
    fc.append(f"{a_src}afade=t=in:d=0.25,afade=t=out:st={total - 0.6:.3f}:d=0.6,"
              f"loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]")
    out = review_dir() / f"{rec['id']}.mp4"
    v = cfg["video"]
    run_ffmpeg([*inputs, "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]",
                "-t", f"{total:.3f}", "-r", str(FPS), "-c:v", "libx264", "-preset", v["preset"],
                "-crf", str(v["crf"]), "-maxrate", v["maxrate"], "-bufsize", v["bufsize"],
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", v["audio_bitrate"],
                "-movflags", "+faststart", str(out)], "lecture compose")
    from .qc import ensure                  # broken render -> render_clip retries with a safe look
    # a video you shared plays whole (up to clips.full_max_seconds); everything else stays a Short
    ensure(out, total, max_len=float((cfg.get("clips") or {}).get("full_max_seconds", 180)) + 1
           if rec.get("full") else 61)
    return out


def lecture_meta(cfg, rng: random.Random, rec: dict) -> dict:
    c = cfg["captions"]
    tags_pool = ["#سمير_مصطفى" if rec["sheikh_id"] == "samir_mostafa" else "#أمجد_سمير",
                 "#دروس_دينية", "#موعظة", "#تذكير", "#اسلام", "#راحة_نفسية", "#shorts"]
    out = {}
    for p in ("youtube", "tiktok", "instagram", "facebook"):
        tags = rng.sample(tags_pool, rng.randint(*c["hashtag_count"]))
        body = f"{rec.get('title') or ''}\n{rec['sheikh_name']}\n\n{' '.join(tags)}"
        if rec.get("source_credit"):
            body += f"\n\nالمصدر: {rec['source_credit']}"
        out[p] = {"caption": body.strip(), "hashtags": tags}
    out["youtube"]["title"] = f"{rec.get('title') or 'موعظة'} | {rec['sheikh_name']}"[:100]
    return out


# ---------------------------------------------------------------- commands
def cmd_prepare(cfg, args):
    lc = cfg.get("lectures", {})
    lo, hi = lc.get("min_seconds", 30), lc.get("max_seconds", 60)
    roots = [ROOT / "assets" / "lectures" / s for s in ([args.sheikh] if args.sheikh else SHEIKHS)]
    existing = {p.stem for p in review_dir().glob("*.json")}
    with TextRenderer(cfg) as tr:
        for root in roots:
            if not root.is_dir():
                continue
            for src in sorted(root.iterdir()):
                if src.suffix.lower() not in VIDEO_EXT | AUDIO_EXT:
                    continue
                info = read_json(src.with_suffix(".info.json"), {})
                sheikh = root.name
                words = asr.transcribe(cfg, src)
                from .agent import memory     # fixes learned from your Telegram corrections
                memory.apply_rules(words, memory.rules(int((cfg.get("agent") or {}).get("learn_min_count", 2))))
                cands = candidates(words, lo, hi)
                chosen = pick(cands, args.per_file)
                log.info("%s: %d words, %d candidate windows, picked %d", src.name, len(words),
                         len(cands), len(chosen))
                is_video = src.suffix.lower() in VIDEO_EXT
                for clip in chosen:
                    cid = f"l-{sheikh}-{src.stem}-{int(clip['start']):04d}"
                    if cid in existing and not args.force:
                        continue
                    segs, flags = build_segments(cfg, words, clip)
                    layout = "audio"
                    face_x = None
                    if is_video:
                        h = int(subprocess.run([FFPROBE, "-v", "error",
                                                "-select_streams", "v:0", "-show_entries", "stream=height",
                                                "-of", "csv=p=0", str(src)],
                                               capture_output=True, text=True).stdout.strip() or 0)
                        face_x = face_center_x(src, clip["start"], clip["end"])
                        # crop only when the source is tall enough to fill 1920 without heavy upscaling
                        layout = "crop" if (h >= 1280 and face_x is not None) else "fit"
                    rec = {
                        "id": cid, "type": "lecture", "approved": False,
                        "sheikh_id": sheikh, "sheikh_name": info.get("sheikh_name") or SHEIKHS.get(sheikh, sheikh),
                        "title": info.get("title", ""),
                        "source": {"file": str(src.relative_to(ROOT)).replace("\\", "/"),
                                   "url": info.get("webpage_url"), "channel": info.get("channel")},
                        "source_credit": info.get("channel"),
                        "clip": {"start": round(clip["start"] - 0.15, 3), "end": round(clip["end"] + 0.35, 3),
                                 "score": clip["score"]},
                        "layout": layout, "face_x": face_x,
                        "flags": flags,
                        "segments": [],
                        "transcript": " ".join(w["w"] for w in words[clip["w0"]:clip["w1"]]),
                    }
                    shift = clip["start"] - rec["clip"]["start"]
                    for s in segs:   # make segment times relative to the padded clip start
                        s["start"] = round(s["start"] + shift, 3)
                        s["end"] = round(s["end"] + shift, 3)
                        if s["type"] == "speech":
                            for w in s["words"]:
                                w["s"] = round(w["s"] + shift, 3)
                                w["e"] = round(w["e"] + shift, 3)
                    rec["segments"] = segs
                    rec["platforms"] = lecture_meta(cfg, random.Random(cid), rec)
                    write_json(review_dir() / f"{cid}.json", rec)
                    out = render_clip(cfg, rec, tr)
                    log.info("[%s] %.0f-%.0fs layout=%s flags=%s -> %s", cid, clip["start"], clip["end"],
                             layout, [f["type"] for f in flags] or "none", out.name)


def cmd_render(cfg, args):
    p = review_dir() / f"{args.id}.json"
    rec = read_json(p, None)
    if rec is None:
        raise SystemExit(f"no such clip {args.id}")
    rec["platforms"] = lecture_meta(cfg, random.Random(rec["id"]), rec)
    write_json(p, rec)
    with TextRenderer(cfg) as tr:
        print(render_clip(cfg, rec, tr))


def cmd_list(cfg, args):
    for p in sorted(review_dir().glob("l-*.json")):
        r = read_json(p, {})
        mark = "✔" if r.get("approved") else "…"
        dur = r["clip"]["end"] - r["clip"]["start"]
        print(f"{mark} {r['id']}  {dur:4.0f}s  flags={[f['type'] for f in r.get('flags', [])]}  {r['transcript'][:60]}")


def cmd_approve(cfg, args):
    p = review_dir() / f"{args.id}.json"
    rec = read_json(p, None)
    if rec is None:
        raise SystemExit(f"no such clip {args.id}")
    rec["approved"] = True
    write_json(p, rec)
    print(f"approved {args.id} — it will enter the publishing queue")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Lecture shorts (phase 2)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("prepare")
    pp.add_argument("--sheikh", choices=list(SHEIKHS))
    pp.add_argument("--per-file", type=int, default=5)
    pp.add_argument("--force", action="store_true", help="re-render clips already in review")
    sub.add_parser("list")
    for name in ("render", "approve"):
        sp = sub.add_parser(name)
        sp.add_argument("id")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    cfg = load_config()
    {"prepare": cmd_prepare, "render": cmd_render, "list": cmd_list, "approve": cmd_approve}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
