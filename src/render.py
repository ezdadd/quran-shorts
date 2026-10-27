"""FFmpeg composition: footage background -> text overlay stream -> recitation + ambience.

Text animation is pre-computed in Python as a timeline of transparent PNG states and fed to
FFmpeg as one stream through the concat demuxer, so every reveal/highlight lands on the exact
ayah/word timestamp.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageStat

from . import quran
from .footage import Clip
from .templates import Template, readable_color
from .textrender import TextRenderer
from .util import media_duration, run_ffmpeg

log = logging.getLogger(__name__)

W, H, FPS = 1080, 1920, 30
XFADE = 1.0
FADE_IN, FADE_OUT = 0.35, 0.25
CENTER_Y = {"center": 900, "lower": 1180}
MAX_TEXT_H = {"center": 860, "lower": 660}


class TooLong(Exception):
    pass


@dataclass
class Frame:
    image: Image.Image
    start: float
    end: float
    fade_in: bool = False
    fade_out: bool = False


# ---------------------------------------------------------------- background
def build_background(cfg, clips: list[Clip], total: float, tpl: Template, out: Path) -> Path:
    # cycle clips until they cover the duration (crossfades eat XFADE per joint)
    segs: list[tuple[Clip, float]] = []
    covered = 0.0
    i = 0
    while covered < total:
        c = clips[i % len(clips)]
        remaining = total - covered + (XFADE if segs else 0)
        d = min(c.duration, max(remaining, XFADE + 1.5))
        segs.append((c, d))
        covered += d - (XFADE if len(segs) > 1 else 0)
        i += 1
        if i > 40:
            raise RuntimeError("footage too short to cover video")

    args: list[str] = []
    fc: list[str] = []
    for k, (c, d) in enumerate(segs):
        args += ["-t", f"{d:.3f}", "-i", str(c.path)]
        fc.append(f"[{k}:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                  f"fps={FPS},setsar=1,format=yuv420p,trim=0:{d:.3f},setpts=PTS-STARTPTS[s{k}]")
    last = "s0"
    offset = 0.0
    for k in range(1, len(segs)):
        offset += segs[k - 1][1] - XFADE
        fc.append(f"[{last}][s{k}]xfade=transition=fade:duration={XFADE}:offset={offset:.3f}[x{k}]")
        last = f"x{k}"

    post = []
    if tpl.ken_burns:
        z = 0.08
        post.append(f"scale=w='2*trunc({W}*(1+{z}*t/{total:.3f})/2)':h=-2:eval=frame,crop={W}:{H}")
    if tpl.grade == "warm":
        post.append("colorbalance=rs=.06:gs=.02:bs=-.06:rm=.04:bm=-.04,eq=saturation=1.06")
    elif tpl.grade == "cool":
        post.append("colorbalance=rs=-.05:bs=.06:rm=-.03:bm=.04,eq=saturation=1.03")
    if tpl.vignette:
        post.append("vignette=PI/5")
    post.append(f"trim=0:{total:.3f},setpts=PTS-STARTPTS,format=yuv420p")
    fc.append(f"[{last}]" + ",".join(post) + "[bg]")

    run_ffmpeg([*args, "-filter_complex", ";".join(fc), "-map", "[bg]", "-an",
                "-c:v", "libx264", "-preset", cfg["video"]["preset"], "-crf", "16",
                "-r", str(FPS), str(out)], "background")
    return out


def zone_luma(bg: Path, total: float, center_y: int, work: Path, samples: int = 6) -> float:
    """Brightest mean luma of the text zone over several frames (foam/clouds come and go)."""
    worst = 0.0
    top = max(0, center_y - 380)
    for k in range(samples):
        at = total * (k + 0.5) / samples
        png = work / "_luma.png"
        run_ffmpeg(["-ss", f"{at:.2f}", "-i", str(bg), "-frames:v", "1", str(png)], "luma frame")
        img = Image.open(png).convert("L")
        zone = img.crop((90, top, W - 90, min(H, center_y + 380)))
        worst = max(worst, ImageStat.Stat(zone).mean[0] / 255.0)
    return worst


# ---------------------------------------------------------------- text timeline
PAUSE_MARKS = {chr(c) for c in range(0x06D6, 0x06DD)}
AYAH_END = "۝"


def _word_clock(a: quran.Ayah, start: float) -> list[float]:
    """Absolute start time of every word: real timing if available, else by letter count."""
    if a.word_times:
        return [start + t[0] for t in a.word_times]
    lens = [len(quran.skeleton(w)) or 1 for w in a.words]
    tot = sum(lens)
    return [start + max(0.0, a.duration * sum(lens[:i]) / tot - 0.3) for i in range(len(lens))]


def _paginate(tr: TextRenderer, a: quran.Ayah, kw: dict, max_lines: int) -> list[tuple[int, int]]:
    """Split a long ayah into screens of <= max_lines lines at the full font size, breaking at
    a pause mark when one is near. Words are never altered, only distributed across screens."""
    n = len(a.words)
    info = tr.layout_ayah(a.words, AYAH_END + quran.to_arabic_digits(a.ayah), max_h=100000, **kw)
    if info["lines"] <= max_lines:
        return [(0, n)]
    pages = -(-info["lines"] // max_lines)
    per = n / pages
    breaks = [0]
    for k in range(1, pages):
        target = round(k * per)
        lo = max(breaks[-1] + 1, target - int(per * 0.3))
        hi = min(n - 1, target + int(per * 0.3))
        good = [i for i in range(lo, hi + 1) if PAUSE_MARKS & set(a.words[i - 1])]
        breaks.append(min(good, key=lambda i: abs(i - target)) if good else max(lo, min(hi, target)))
    breaks.append(n)
    return list(zip(breaks, breaks[1:]))


def build_text_frames(tr: TextRenderer, cfg, rec: quran.Recitation, tpl: Template,
                      color: str, style: str, lead: float, total: float) -> tuple[list[Frame], tuple]:
    tcfg = cfg["templates"]
    f = tcfg["fonts"][tpl.font]
    tr.use_fonts(f["file"])
    hexcol = tcfg["colors"][color]
    accent = tcfg["colors"]["gold"] if color != "gold" else "#FFFFFF"
    css_style = {"band": "band", "shadow": "shadow", "blur": "soft", "dim": "soft"}[style]
    kw = dict(style=css_style, color=hexcol, accent=accent, size=f["size"],
              center_y=CENTER_Y[tpl.position], attach_marks=f.get("attach_marks", False),
              weight=f.get("css_weight", 400), min_size=tcfg.get("min_size", 64))
    max_lines = tcfg.get("max_lines", {}).get(tpl.position, 4)

    frames: list[Frame] = []
    boxes = []
    t = lead
    for idx, a in enumerate(rec.ayat):
        start, end = t, t + a.duration
        t = end
        if idx == len(rec.ayat) - 1:
            end = total - 0.5          # hold the last ayah until the outro fade
        clock = _word_clock(a, start)
        pages = _paginate(tr, a, kw, max_lines)
        for pi, (i0, i1) in enumerate(pages):
            first, last = pi == 0, pi == len(pages) - 1
            ps = start - (0.4 if idx == 0 else 0.0) if first else clock[i0] - 0.12
            pe = end if last else clock[i1] - 0.12
            info = tr.layout_ayah(a.words[i0:i1],
                                  AYAH_END + quran.to_arabic_digits(a.ayah) if last else None,
                                  max_h=MAX_TEXT_H[tpl.position], open_bracket=first,
                                  close_bracket=last, **kw)
            boxes.append(info["box"])
            words = a.words[i0:i1]
            page: list[Frame] = []
            if tpl.animation == "word" and a.word_times:
                cuts = [ps] + clock[i0 + 1:i1] + [start + a.word_times[i1 - 1][1]]
                for k in range(1, len(cuts)):
                    cuts[k] = min(max(cuts[k], cuts[k - 1]), pe)
                for w in range(len(words)):
                    page.append(Frame(tr.state(info, word=w).image, cuts[w], cuts[w + 1]))
                page.append(Frame(tr.state(info, word=len(words)).image, cuts[-1], pe))
            elif tpl.animation == "line" and info["lines"] > 1:
                firsts = [info["line"].index(k) for k in range(info["lines"])]
                ls = [ps] + [max(ps, clock[i0 + w] - 0.15) for w in firsts[1:]] + [pe]
                for k in range(info["lines"]):
                    page.append(Frame(tr.state(info, up_to_line=k).image, ls[k], max(ls[k], ls[k + 1])))
            else:
                page.append(Frame(tr.state(info).image, ps, pe))
            page = [fr for fr in page if fr.end - fr.start > 1e-3]
            if page:
                page[0].fade_in = True
                page[-1].fade_out = True
            frames += page
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes); y1 = max(b[1] + b[3] for b in boxes)
    return frames, (int(x0), int(y0), int(x1), int(y1))


def _scaled(img: Image.Image, k: float) -> Image.Image:
    r, g, b, a = img.split()
    return Image.merge("RGBA", (r, g, b, a.point(lambda v: int(v * k))))


def write_concat(frames: list[Frame], total: float, work: Path) -> Path:
    """Expand states into PNG frames with fade in/out per screen and write an ffconcat list."""
    d = work / "text"
    d.mkdir(exist_ok=True)
    blank = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    entries: list[tuple[Image.Image, float]] = []
    cursor = 0.0
    step = 1.0 / FPS
    for fr in frames:
        if fr.start > cursor + 1e-3:
            entries.append((blank, fr.start - cursor))
        s, e = max(fr.start, cursor), fr.end
        if e - s <= 1e-3:
            continue
        n_in = int(FADE_IN * FPS) if fr.fade_in else 0
        n_out = int(FADE_OUT * FPS) if fr.fade_out else 0
        body = (e - s) - (n_in + n_out) * step
        if body < 0:
            n_in = n_out = 0
            body = e - s
        for i in range(n_in):
            entries.append((_scaled(fr.image, (i + 1) / (n_in + 1)), step))
        entries.append((fr.image, body))
        for i in range(n_out):
            entries.append((_scaled(fr.image, 1 - (i + 1) / (n_out + 1)), step))
        cursor = e
    if cursor < total:
        entries.append((blank, total - cursor))

    lines = ["ffconcat version 1.0"]
    names: dict[int, str] = {}
    for img, dur in entries:
        if id(img) not in names:
            name = f"f{len(names):05d}.png"
            img.save(d / name, compress_level=1)
            names[id(img)] = name
        lines += [f"file '{names[id(img)]}'", f"duration {dur:.4f}"]
    lines.append(f"file '{names[id(entries[-1][0])]}'")
    lst = d / "list.ffconcat"
    lst.write_text("\n".join(lines), encoding="utf-8")
    return lst


def blur_mask(box: tuple, work: Path) -> Path:
    x0, y0, x1, y1 = box
    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).rounded_rectangle((x0 - 10, y0 - 10, x1 + 10, y1 + 10), radius=48, fill=255)
    m = m.filter(ImageFilter.GaussianBlur(18))
    p = work / "_mask.png"
    Image.merge("RGBA", (m, m, m, m)).save(p)
    return p


# ---------------------------------------------------------------- final compose
def compose(cfg, rng: random.Random, tr: TextRenderer, rec: quran.Recitation, clips: list[Clip],
            ambience: Path | None, tpl: Template, out_mp4: Path, work: Path) -> dict:
    v = cfg["video"]
    work.mkdir(parents=True, exist_ok=True)
    rec_wav = quran.build_recitation_audio(rec, work / "recitation.wav")
    lead, tail = v["lead_in"], v["tail"]
    total = round(lead + rec.duration + tail, 3)
    if total > v["max_duration"]:
        raise TooLong(f"{rec.ref} by {rec.reciter_id}: {total:.1f}s > {v['max_duration']}s")

    bg = build_background(cfg, clips, total, tpl, work / "bg.mp4")
    luma = zone_luma(bg, total, CENTER_Y[tpl.position], work)
    color, style = readable_color(tpl, luma)
    log.info("text zone luma %.2f -> colour %s, backing %s", luma, color, style)

    frames, box = build_text_frames(tr, cfg, rec, tpl, color, style, lead, total)
    concat = write_concat(frames, total, work)

    labels = []
    if tpl.show_title:
        a, b = rec.ayat[0].ayah, rec.ayat[-1].ayah
        rng_txt = quran.to_arabic_digits(a) if a == b else f"{quran.to_arabic_digits(a)}–{quran.to_arabic_digits(b)}"
        labels.append({"text": f"سورة {rec.surah_name} · {rng_txt}", "y": 150, "size": 46})
    brand = cfg.get("branding") or {}
    if tpl.show_reciter:
        y = brand.get("y", 1735) - 75 if brand.get("handle") else 1700
        labels.append({"text": f"القارئ {rec.reciter_name}", "y": y, "size": 38, "opacity": 0.9})
    if brand.get("handle"):
        labels.append({"text": brand["handle"], "y": brand.get("y", 1735),
                       "size": brand.get("size", 32), "opacity": brand.get("opacity", 0.9),
                       "pill": True})
    labels_png = None
    if labels:
        tr.use_fonts(cfg["templates"]["fonts"][tpl.font]["file"])
        labels_png = work / "_labels.png"
        tr.labels(labels).save(labels_png)

    # ---- inputs
    args = ["-i", str(bg), "-f", "concat", "-safe", "0", "-i", str(concat), "-i", str(rec_wav)]
    n = 3
    amb_idx = mask_idx = lab_idx = None
    amb_copies: list[int] = []
    amb_offset = 0.0
    if ambience:
        amb_dur = media_duration(ambience)
        if amb_dur >= total + 1.0:
            # long file: start at a random point so the same file sounds different each video
            amb_offset = round(rng.uniform(0, amb_dur - total - 0.5), 2)
            args += ["-ss", f"{amb_offset:.2f}", "-i", str(ambience)]; amb_idx = n; n += 1
        else:
            # short file: chain enough copies, each joined with a crossfade (no audible seam)
            xf = min(2.0, amb_dur / 4)
            k = 1
            while k * amb_dur - (k - 1) * xf < total + 0.5:
                k += 1
            for _ in range(k):
                args += ["-i", str(ambience)]; amb_copies.append(n); n += 1
            amb_idx = amb_copies[0]
    if style == "blur":
        args += ["-loop", "1", "-t", f"{total:.3f}", "-i", str(blur_mask(box, work))]; mask_idx = n; n += 1
    if labels_png:
        args += ["-loop", "1", "-t", f"{total:.3f}", "-i", str(labels_png)]; lab_idx = n; n += 1

    fc = []
    base = "[0:v]"
    if style == "dim":
        fc.append(f"{base}colorchannelmixer=rr=.62:gg=.62:bb=.62[bgd]"); base = "[bgd]"
    elif style == "blur":
        fc.append(f"{base}split[b0][b1]")
        fc.append("[b1]boxblur=22:2,colorchannelmixer=rr=.7:gg=.7:bb=.7,format=rgba[blr]")
        fc.append(f"[{mask_idx}:v]format=rgba,alphaextract[m]")
        fc.append("[blr][m]alphamerge[bm]")
        fc.append("[b0][bm]overlay=0:0:format=auto[bgd]"); base = "[bgd]"
    fc.append(f"[1:v]fps={FPS},format=rgba[txt]")
    fc.append(f"{base}[txt]overlay=0:0:format=auto[v1]")
    vlast = "[v1]"
    if lab_idx is not None:
        fc.append(f"[{lab_idx}:v]format=rgba,fade=t=in:st=0.2:d=0.6:alpha=1,"
                  f"fade=t=out:st={total - 0.9:.3f}:d=0.6:alpha=1[lab]")
        fc.append(f"{vlast}[lab]overlay=0:0:format=auto[v2]"); vlast = "[v2]"
    fc.append(f"{vlast}fade=t=in:st=0:d=0.5,fade=t=out:st={total - 0.6:.3f}:d=0.6,format=yuv420p[vout]")

    lead_ms = int(lead * 1000)
    fc.append(f"[2:a]adelay={lead_ms}|{lead_ms},apad=whole_dur={total:.3f},"
              f"loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[rec]")
    a_out = "[rec]"
    amb_db = None
    if amb_idx is not None:
        lo, hi = cfg["ambience"]["level_db"]
        amb_db = round(rng.uniform(lo, hi), 1)
        fd = cfg["ambience"]["fade"]
        src = f"[{amb_idx}:a]"
        for j, ci in enumerate(amb_copies[1:], 1):
            fc.append(f"{src}[{ci}:a]acrossfade=d={xf:.2f}[aj{j}]")
            src = f"[aj{j}]"
        fc.append(f"{src}atrim=0:{total:.3f},asetpts=N/SR/TB,aresample=48000,"
                  f"aformat=channel_layouts=stereo,loudnorm=I=-16:TP=-2,aresample=48000,"
                  f"volume={amb_db}dB,afade=t=in:d={fd},afade=t=out:st={total - fd - 0.5:.3f}:d={fd}[amb]")
        fc.append("[rec][amb]amix=inputs=2:normalize=0:duration=first[mix]")
        a_out = "[mix]"
    tl = cfg["ambience"]["target_lufs"]
    fc.append(f"{a_out}loudnorm=I={tl}:TP=-1.5:LRA=11,aresample=48000,"
              f"atrim=0:{total:.3f}[aout]")

    run_ffmpeg([*args, "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]",
                "-t", f"{total:.3f}", "-r", str(FPS),
                "-c:v", "libx264", "-preset", v["preset"], "-crf", str(v["crf"]),
                "-maxrate", v["maxrate"], "-bufsize", v["bufsize"],
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", v["audio_bitrate"],
                "-movflags", "+faststart", str(out_mp4)], "compose")
    return {"duration": total, "luma": round(luma, 3), "color": color, "background": style,
            "ambience_db": amb_db, "ambience_offset": amb_offset}
