"""'Screen' videos: a shared clip playing inside a device in one of our scenes — a laptop on a desk,
an iMac, a TV in a living room, a phone on a table — the "organic" look that does well online.

Scenes are free green-screen stock videos (Pixabay). For every frame the green screen is found
(OpenCV), the clip is warped into its four corners in perspective, and the green is keyed out with
soft, de-spilled edges, so a hand or reflection in front of the screen stays in front.

Each clip gets its own look (saved in the record, so an edit re-renders it the same way): framing
(the full 9:16 phone frame, or a square / tall / wide close-up over a blurred or dark backdrop),
camera (still, slow push-in, pull-out, drift), colour grade, vignette, grain, rounded corners and
the title style. Scenes live in data/screens.json (built and modesty-checked by tools/build_screens).
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import cv2
import numpy as np

from .util import FFMPEG, ROOT, cache_dir, download, media_duration, read_json, run_ffmpeg

log = logging.getLogger("quran-shorts.screens")
W, H, FPS = 1080, 1920, 30
WORK_W = 1920                       # scenes are analysed at this width (4K sources are scaled down)
GROW = 1.10                         # the clip reaches a bit under the bezel; the key decides what shows
LOOP_MAX = 8.0                      # seconds of the scene used, played forward then backward


class ScreenError(Exception):
    pass


class NoPicture(ScreenError):
    """The shared video has nothing to show on a screen (only text on a plain background...)."""


# ---------------------------------------------------------------- smart zoom on the shared clip
FACE_MODEL = ROOT / "assets" / "models" / "face_detection_yunet_2023mar.onnx"
GRID = 12                           # px per heat-map cell, on frames scaled to 360 px wide


def focus(clip: Path, start: float, total: float, aspect: float) -> list[float]:
    """The part of the shared video to show (x, y, w, h as fractions), shaped like the device's
    screen: keeps the speaker's face, cuts black bars and as much as it can of what was laid over
    the video — burned-in captions, page logos, usernames — without zooming in too far."""
    cap = cv2.VideoCapture(str(clip))
    fw, fh = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    det = cv2.FaceDetectorYN.create(str(FACE_MODEL), "", (320, 320), 0.75) if FACE_MODEL.exists() else None
    sw = 360
    sh = max(GRID, round(fh * sw / fw))
    texty, edges_all, lum, faces, n = None, None, 0.0, [], 0
    for t in np.linspace(start + 0.3, start + max(0.5, total - 0.3), 14):
        cap.set(cv2.CAP_PROP_POS_MSEC, float(t) * 1000)
        ok, f = cap.read()
        if not ok:
            continue
        n += 1
        s = cv2.resize(f, (sw, sh), interpolation=cv2.INTER_AREA)
        if det is not None:
            k = 640 / max(fw, fh)
            small = cv2.resize(f, (int(fw * k), int(fh * k)))
            det.setInputSize((small.shape[1], small.shape[0]))
            _, found = det.detect(small)
            if found is not None and len(found):
                x, y, w, h = (found[0][:4] / k).tolist()
                faces.append((x, y, w, h))
        hsv = cv2.cvtColor(s, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(s, cv2.COLOR_BGR2GRAY)
        e = cv2.Canny(gray, 80, 180)
        # caption / username text: sharp strokes of white or yellow
        bright = ((hsv[..., 2] > 185) & (hsv[..., 1] < 70)) | \
                 ((hsv[..., 0] >= 18) & (hsv[..., 0] <= 35) & (hsv[..., 1] > 110) & (hsv[..., 2] > 170))
        txt = (cv2.dilate(e, np.ones((3, 3), np.uint8)) > 0) & bright
        cells = cv2.resize(txt.astype(np.float32), (sw // GRID, sh // GRID), interpolation=cv2.INTER_AREA)
        texty = (cells > 0.07).astype(np.float32) + (texty if texty is not None else 0)
        edges_all = (e > 0) if edges_all is None else (edges_all & (cv2.dilate(e, np.ones((3, 3), np.uint8)) > 0))
        lum = lum + gray.astype(np.float32)
    cap.release()
    if n < 4:
        raise NoPicture("could not read the video")
    gw, gh = sw // GRID, sh // GRID
    heat = texty / n
    # logos: sharp edges that never move, near the borders (the middle may be a still background)
    static = cv2.resize(edges_all.astype(np.float32), (gw, gh), interpolation=cv2.INTER_AREA) > 0.05
    border = np.ones((gh, gw), bool)
    border[int(gh * .2):int(gh * .8), int(gw * .2):int(gw * .8)] = False
    heat = np.maximum(heat, 0.8 * (static & border))
    # black bars: rows / columns that stay dark on average
    lum /= n
    # (a bar is evenly dark; dark clothes or a dark room still have brighter spots)
    rows = np.nonzero(np.percentile(lum, 95, axis=1) > 30)[0]
    cols = np.nonzero(np.percentile(lum, 95, axis=0) > 30)[0]
    if not len(rows) or not len(cols):
        raise NoPicture("black video")
    cx0, cx1, cy0, cy1 = cols[0] / sw, (cols[-1] + 1) / sw, rows[0] / sh, (rows[-1] + 1) / sh
    if len(faces) < max(2, n // 4):         # no speaker on screen (text on a plain background...)
        raise NoPicture("no one on screen")
    x, y, w, h = np.median(np.array(faces), axis=0) / np.array([fw, fh, fw, fh])
    face = [x - w * .1, y - h * .25, x + w * 1.1, y + h * 1.35]     # face + beard: never cut
    g = [int((x - w * .3) * gw), int((y - h * .5) * gh), int(np.ceil((x + w * 1.3) * gw)),
         int(np.ceil((y + h * 1.8) * gh))]                         # no "overlay" around the speaker
    heat[max(0, g[1]):g[3], max(0, g[0]):g[2]] = 0
    total_heat = float(heat.sum())
    # the biggest rectangle of the screen's shape inside the lit area, then smaller ones
    a = aspect * fh / fw                        # aspect in fraction units
    mw, mh = cx1 - cx0, cy1 - cy0
    mw, mh = (mh * a, mh) if mw / mh > a else (mw, mw / a)
    best, best_cost = None, 1e9
    for s in (1.0, 0.9, 0.8, 0.7, 0.6, 0.52):
        w, h = mw * s, mh * s
        if h * fh < 300 and s < 1:              # too few pixels left: stop zooming
            break
        for x in np.linspace(cx0, cx1 - w, 9):
            for y in np.linspace(cy0, cy1 - h, 9):
                if not (x <= max(face[0], cx0) + 1e-3 and y <= max(face[1], cy0) + 1e-3 and
                                 x + w >= min(face[2], cx1) - 1e-3 and y + h >= min(face[3], cy1) - 1e-3):
                    continue
                inside = heat[int(y * gh):int(np.ceil((y + h) * gh)), int(x * gw):int(np.ceil((x + w) * gw))].sum()
                cost = (inside / total_heat if total_heat > 0.5 else 0) + 0.55 * (1 - s)
                # keep the speaker near the middle, the face in the upper part
                cost += 0.15 * abs((face[0] + face[2]) / 2 - (x + w / 2)) / w
                cost += 0.1 * abs((face[1] + face[3]) / 2 - (y + h * 0.42)) / h
                if cost < best_cost - 1e-6:
                    best, best_cost = [float(x), float(y), float(w), float(h)], cost
    if best is None or best[2] * fw < 260 or best[3] * fh < 260:
        # the speaker doesn't fit the screen's shape (a tall video on a TV), or too few pixels would be
        # left: the whole picture without its black bars, fitted on the screen over a blurred copy
        best = [float(cx0), float(cy0), float(cx1 - cx0), float(cy1 - cy0)]
    log.info("smart zoom: %.0f%% of the picture, overlay cut %.0f%%", best[2] * best[3] * 100,
             100 * (1 - heat[int(best[1] * gh):int(np.ceil((best[1] + best[3]) * gh)),
                             int(best[0] * gw):int(np.ceil((best[0] + best[2]) * gw))].sum() / total_heat)
             if total_heat > 0.5 else 0)
    return [round(v, 4) for v in best]


# ---------------------------------------------------------------- finding the screen
def green_mask(bgr: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    m = cv2.inRange(hsv, (35, 70, 50), (88, 255, 255))
    return cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))


def _order(pts: np.ndarray) -> np.ndarray:
    """top-left, top-right, bottom-right, bottom-left"""
    s, d = pts.sum(1), np.diff(pts, axis=1).ravel()
    return np.float32([pts[s.argmin()], pts[d.argmin()], pts[s.argmax()], pts[d.argmax()]])


def find_quad(bgr: np.ndarray, mask: np.ndarray | None = None) -> np.ndarray | None:
    m = green_mask(bgr) if mask is None else mask
    cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return None
    c = max(cs, key=cv2.contourArea)
    area = cv2.contourArea(c)
    if area < 0.01 * m.size:
        return None
    hull = cv2.convexHull(c)
    for eps in (0.015, 0.025, 0.04, 0.06):
        p = cv2.approxPolyDP(hull, eps * cv2.arcLength(hull, True), True)
        if len(p) == 4:
            return _order(p.reshape(4, 2).astype(np.float32))
    r = cv2.minAreaRect(hull)        # a finger cuts a corner: fall back to the rotated box
    return _order(cv2.boxPoints(r).astype(np.float32))


def survey(scene: Path) -> dict:
    """Where the screen is over the scene (sampled), and whether it is a usable one."""
    cap = cv2.VideoCapture(str(scene))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    quads, size, stray = [], None, 0.0
    for i in np.linspace(0, n - 1, 10).astype(int):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, f = cap.read()
        if not ok:
            continue
        f = cv2.resize(f, (WORK_W, round(f.shape[0] * WORK_W / f.shape[1] / 2) * 2))
        size = f.shape[1], f.shape[0]
        m = green_mask(f)
        q = find_quad(f, m)
        if q is not None:
            quads.append(q)
            hole = np.zeros(m.shape, np.uint8)
            cv2.fillConvexPoly(hole, (q.mean(0) + (q - q.mean(0)) * 1.15).astype(np.int32), 255)
            # a second screen = a big green patch elsewhere (plants or green light on a desk are small)
            cs, _ = cv2.findContours(cv2.bitwise_and(m, cv2.bitwise_not(hole)), cv2.RETR_EXTERNAL,
                                     cv2.CHAIN_APPROX_SIMPLE)
            stray += max((cv2.contourArea(c) for c in cs), default=0) / m.size
    cap.release()
    if len(quads) < 6 or size is None:
        raise ScreenError(f"no steady green screen in {scene.name}")
    if stray / len(quads) > 0.012:
        raise ScreenError(f"more than one green area in {scene.name}")
    allp = np.concatenate(quads)
    x0, y0 = allp.min(0)
    x1, y1 = allp.max(0)
    if x0 < 8 or y0 < 8 or x1 > size[0] - 8 or y1 > size[1] - 8:
        raise ScreenError(f"the screen leaves the picture in {scene.name}")
    q = np.mean(quads, axis=0)
    qw = (np.linalg.norm(q[1] - q[0]) + np.linalg.norm(q[2] - q[3])) / 2
    qh = (np.linalg.norm(q[3] - q[0]) + np.linalg.norm(q[2] - q[1])) / 2
    return {"size": size, "box": [float(x0), float(y0), float(x1), float(y1)], "portrait": qh > qw,
            "aspect": float(qw / qh), "moving": float(np.ptp(allp[:, 0]) > 0)}

# ---------------------------------------------------------------- scenes + looks
LIB = ROOT / "data" / "screens.json"
# picture shape (width / height) and where it sits; the space under it holds the subtitles
FRAMES = {"full": None, "close": 1.0, "tall": 0.9, "wide": 4 / 3}
TOP = 210                                # picture top (title above it)
DEFAULT_LOOKS = {
    "frame_landscape": {"close": 4, "tall": 3, "wide": 2},
    "frame_portrait": {"full": 3, "tall": 2},
    "camera": {"push": 4, "still": 2, "pull": 2, "drift": 2},
    "grade": {"none": 3, "warm": 2, "golden": 2, "film": 2, "cool": 1, "moody": 1, "teal_orange": 1},
    "vignette": 0.5, "grain": 0.35, "round": 0.6,
    "backdrop": {"blur": 4, "dark": 2},
    "title": {"plain": 3, "box_white": 2, "box_black": 2, "box_gold": 1},
}


def _w(rng, d: dict):
    ks = [k for k, v in d.items() if v > 0]
    return rng.choices(ks, [d[k] for k in ks])[0]


def library() -> dict:
    return read_json(LIB, {"scenes": {}}).get("scenes", {})


def choose(cfg, rng, clip_portrait: bool, avoid: set[str] = frozenset()) -> dict:
    """A scene + look for one clip. A vertical clip goes on a phone most of the time; a landscape
    clip never does (it would be tiny)."""
    lib = {k: v for k, v in library().items() if k not in avoid}
    if not lib:
        raise ScreenError("no scenes in data/screens.json (run tools.build_screens)")
    sc = (cfg.get("screens") or {})
    phones = [k for k, v in lib.items() if v["portrait"]]
    wide = [k for k, v in lib.items() if not v["portrait"]]
    use_phone = phones and (not wide or (clip_portrait and rng.random() < float(sc.get("phone_share", 0.7))))
    tun = sc.get("tuning") or {}             # learned from views (src/stats.py), 1 = no change
    pool = phones if use_phone else wide
    dw = tun.get("device_weights") or {}
    sid = rng.choices(pool, [float(dw.get(lib[k].get("kind", ""), 1)) for k in pool])[0]
    looks = {**DEFAULT_LOOKS, **(sc.get("looks") or {})}
    for dim, key in (("camera", "camera_weights"), ("title", "title_weights")):
        if tun.get(key):
            looks[dim] = {k: v * float(tun[key].get(k, 1)) for k, v in looks[dim].items()}
    for dim in ("frame_portrait", "frame_landscape"):
        if tun.get("frame_weights"):
            looks[dim] = {k: v * float(tun["frame_weights"].get(k, 1)) for k, v in looks[dim].items()}
    portrait = lib[sid]["portrait"]
    return {"scene": sid, "kind": lib[sid].get("kind", ""),
            "frame": _w(rng, looks["frame_portrait" if portrait else "frame_landscape"]),
            "camera": _w(rng, looks["camera"]), "grade": _w(rng, looks["grade"]),
            "vignette": rng.random() < looks["vignette"], "grain": rng.random() < looks["grain"],
            "round": rng.random() < looks["round"], "backdrop": _w(rng, looks["backdrop"]),
            "title": _w(rng, looks["title"])}


def safe(look: dict) -> dict:
    """After a failed render: plainest settings (another scene is picked by the caller)."""
    return {**look, "camera": "still", "grain": False, "grade": "none"}


def scene_file(cfg, sid: str) -> Path:
    s = library().get(sid)
    if not s:
        raise ScreenError(f"scene {sid} is not in the library")
    out = cache_dir(cfg, "screens") / f"{sid}.mp4"
    if s.get("virtual"):                # our own phone over modesty-checked footage: built from its recipe
        return out if out.exists() and out.stat().st_size else render_virtual(cfg, s["virtual"], out)
    return download(s["url"], out)


# ---------------------------------------------------------------- virtual phone scenes
# Pixabay has few usable phone green screens, and vertical clips go on a phone most of the time, so
# we also draw one: a phone (bezel, Dynamic Island, side buttons, soft shadow, a little tilt that
# drifts gently) floating over a background clip from our footage library — already checked for
# modesty — blurred like a shallow depth of field. Its screen is green, so compose() treats it like
# any other scene. Only the recipe is stored (data/screens.json); the video is rebuilt when needed.
PHONE_COLORS = {"black": (24, 24, 26), "silver": (198, 200, 204), "gold": (150, 178, 205),
                "blue": (92, 60, 32), "green": (60, 78, 52)}      # BGR
VW, VH, VSEC = 1920, 1080, 8.0


def _phone_sprite(color: str) -> np.ndarray:
    """BGRA phone, upright, 400x840 plus margin for the shadow."""
    pw, ph, pad = 400, 840, 60
    img = np.zeros((ph + 2 * pad, pw + 2 * pad, 4), np.uint8)
    body = PHONE_COLORS.get(color, PHONE_COLORS["black"])

    def rrect(a, x0, y0, x1, y1, r, col):
        cv2.rectangle(a, (x0 + r, y0), (x1 - r, y1), col, -1, cv2.LINE_AA)
        cv2.rectangle(a, (x0, y0 + r), (x1, y1 - r), col, -1, cv2.LINE_AA)
        for cx, cy in ((x0 + r, y0 + r), (x1 - r, y0 + r), (x0 + r, y1 - r), (x1 - r, y1 - r)):
            cv2.circle(a, (cx, cy), r, col, -1, cv2.LINE_AA)

    x0, y0 = pad, pad
    rrect(img, x0 - 5, y0 + 150, x0 + 4, y0 + 230, 3, (*body, 255))        # side buttons
    rrect(img, x0 + pw - 4, y0 + 190, x0 + pw + 5, y0 + 300, 3, (*body, 255))
    rrect(img, x0, y0, x0 + pw, y0 + ph, 64, (*body, 255))                 # body
    edge = tuple(min(255, c + 45) for c in body)
    rrect(img, x0 + 4, y0 + 4, x0 + pw - 4, y0 + ph - 4, 60, (*edge, 255))   # lit rim
    rrect(img, x0 + 7, y0 + 7, x0 + pw - 7, y0 + ph - 7, 58, (14, 14, 16, 255))  # black bezel
    rrect(img, x0 + 18, y0 + 18, x0 + pw - 18, y0 + ph - 18, 48, (0, 255, 0, 255))  # the screen (keyed)
    rrect(img, x0 + pw // 2 - 52, y0 + 34, x0 + pw // 2 + 52, y0 + 66, 16, (8, 8, 8, 255))  # Dynamic Island
    return img


def render_virtual(cfg, spec: dict, out: Path) -> Path:
    """spec: {bg (footage URL), style, x (0-1 centre), tilt (-0.05..0.05), blur (px)}"""
    work = cache_dir(cfg, "screens", "bg")
    bg = download(spec["bg"], work / f"{spec.get('bg_id', out.stem).replace(':', '_')}.mp4")
    sprite = _phone_sprite(spec.get("style", "black"))
    sh, sw = sprite.shape[:2]
    blur = int(spec.get("blur", 8))
    frames = _reader(["-stream_loop", "-1", "-i", str(bg), "-t", f"{VSEC}", "-vf",
                      f"fps={FPS},scale={VW}:{VH}:force_original_aspect_ratio=increase,crop={VW}:{VH}"
                      + (f",gblur=sigma={blur}" if blur else "") + ",eq=brightness=-0.04"], VW, VH)
    enc = subprocess.Popen([FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{VW}x{VH}",
                            "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
                            "-pix_fmt", "yuv420p", str(out)], stdin=subprocess.PIPE)
    cx, cy, tilt = float(spec.get("x", 0.5)) * VW, VH * 0.5, float(spec.get("tilt", 0.0))
    src = np.float32([[0, 0], [sw, 0], [sw, sh], [0, sh]])
    n = 0
    try:
        for i, f in enumerate(frames):
            t = i / FPS
            dy = 7 * np.sin(2 * np.pi * t / VSEC)                     # a gentle float, loops cleanly
            tl = tilt * (1 + 0.25 * np.sin(2 * np.pi * t / VSEC))
            hw, hh = sw / 2, sh / 2
            dst = np.float32([[cx - hw * (1 - tl), cy - hh + dy], [cx + hw * (1 + tl), cy - hh * (1 - abs(tl)) + dy],
                              [cx + hw * (1 + tl), cy + hh * (1 - abs(tl)) + dy], [cx - hw * (1 - tl), cy + hh + dy]])
            M = cv2.getPerspectiveTransform(src, dst)
            layer = cv2.warpPerspective(sprite, M, (VW, VH), flags=cv2.INTER_LINEAR)
            a = layer[..., 3:].astype(np.float32) / 255
            shadow = cv2.GaussianBlur(np.roll(layer[..., 3], (28, 18), (0, 1)), (0, 0), 22).astype(np.float32) / 255
            base = f.astype(np.float32) * (1 - 0.45 * shadow[..., None])
            enc.stdin.write((base * (1 - a) + layer[..., :3] * a).astype(np.uint8).tobytes())
            n += 1
    finally:
        enc.stdin.close()
    if enc.wait() != 0 or n < FPS:
        raise ScreenError("virtual phone scene failed")
    return out


def layout(info: dict, frame: str) -> tuple[tuple[int, int, int, int], tuple[int, int]]:
    """(crop x, y, w, h in WORK_W coordinates, size of the composited picture)."""
    sw, sh = info["size"]
    x0, y0, x1, y1 = info["box"]
    cx, cy, bw, bh = (x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0
    if frame == "full":                          # phone: the whole 9:16 frame, phone in its upper part
        ch = min(sh, bh / 0.62)
        cw = ch * 9 / 16
        cy = cy + ch * 0.08                      # leaves the lower part for the subtitles
        out = (W, H)
    else:
        r = FRAMES.get(frame) or 1.0
        # the screen fills ~70% of the picture's width (or 80% of its height)
        cw = max(bw / 0.7, bh / 0.8 * r)
        ch = cw / r
        if ch > sh:
            ch, cw = sh, sh * r
        if cw > sw:
            cw, ch = sw, sw / r
        out = (W, int(W / r) // 2 * 2)
    x = int(np.clip(cx - cw / 2, 0, sw - cw))
    y = int(np.clip(cy - ch / 2, 0, sh - ch))
    return (x, y, int(cw) // 2 * 2, int(ch) // 2 * 2), out


def geometry(look: dict, info: dict) -> dict:
    """Where the picture is on the 1080x1920 frame and where subtitles / title / names go."""
    _, (ow, oh) = layout(info, look["frame"])
    if (ow, oh) == (W, H):
        return {"top": 0, "bottom": H, "sub_y": 1500, "title_y": 110}
    bottom = TOP + oh
    return {"top": TOP, "bottom": bottom, "sub_y": min(1530, bottom + 180), "title_y": 90}



# ---------------------------------------------------------------- compositing
def _reader(args: list[str], w: int, h: int):
    p = subprocess.Popen([FFMPEG, "-v", "quiet", *args, "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                         stdout=subprocess.PIPE)       # quiet: closing early (shorter input) is normal
    size = w * h * 3
    while True:
        buf = p.stdout.read(size)
        if len(buf) < size:
            break
        yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    p.stdout.close()
    p.wait()


def _despill(img: np.ndarray, zone: np.ndarray) -> np.ndarray:
    """Green light spilling on the screen's edge / bezel: cap green at max(red, blue) there."""
    f = img.astype(np.int16)
    cap = np.maximum(f[..., 2], f[..., 0])
    f[..., 1] = np.where(zone, np.minimum(f[..., 1], cap), f[..., 1])
    return f.astype(np.uint8)


def _camera(look: dict, ow: int, oh: int, total: float) -> str:
    T = max(total, 1.0)
    z = {"push": f"1+0.09*t/{T:.2f}", "pull": f"1.09-0.09*t/{T:.2f}", "drift": "1.07"}.get(look["camera"])
    if not z:
        return ""
    x = f"(iw-{ow})*t/{T:.2f}" if look["camera"] == "drift" else f"(iw-{ow})/2"
    return (f",scale=w='2*trunc({ow}*({z})/2)':h=-2:eval=frame,crop={ow}:{oh}:'{x}':'(ih-{oh})/2'")


def _round_mask(ow: int, oh: int, path: Path, r: int = 46) -> Path:
    m = np.zeros((oh, ow), np.uint8)
    cv2.rectangle(m, (r, 0), (ow - r, oh), 255, -1)
    cv2.rectangle(m, (0, r), (ow, oh - r), 255, -1)
    for cx, cy in ((r, r), (ow - r, r), (r, oh - r), (ow - r, oh - r)):
        cv2.circle(m, (cx, cy), r, 255, -1, lineType=cv2.LINE_AA)
    cv2.imwrite(str(path), m)
    return path


def compose(cfg, look: dict, clip: Path, start: float, total: float, out: Path, work: Path) -> dict:
    """The clip (its picture and sound, from `start` for `total` s) playing on the scene's screen,
    styled by `look`, as a 1080x1920 MP4 without text. Returns geometry() for placing the text."""
    from .render import GRADES
    scene = scene_file(cfg, look["scene"])
    info = survey(scene)
    (x, y, cw, ch), (ow, oh) = layout(info, look["frame"])
    seg = min(LOOP_MAX, media_duration(scene) - 0.1)
    # 1) the scene, cropped and sized, forward + backward so it loops without a jump
    loop = work / "scene_loop.mp4"
    run_ffmpeg(["-i", str(scene), "-filter_complex",
                f"[0:v]trim=0:{seg:.2f},setpts=PTS-STARTPTS,fps={FPS},scale={WORK_W}:-2,crop={cw}:{ch}:{x}:{y},"
                f"scale={ow}:{oh}:flags=lanczos,split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1:a=0,format=yuv420p[v]",
                "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", str(loop)], "scene loop")
    # 2) the clip on a canvas shaped like the screen (fitted, blurred copy behind it)
    k = ow / cw
    sw_ = int(min(ow * 1.2, (info["box"][2] - info["box"][0]) * k * 1.05)) // 2 * 2
    sh_ = int(sw_ / info["aspect"]) // 2 * 2
    if not look.get("crop"):
        look["crop"] = focus(clip, start, total, sw_ / sh_)
    zx, zy, zw, zh = look["crop"]
    can = (f"[0:v]crop='trunc(iw*{zw}/2)*2':'trunc(ih*{zh}/2)*2':'iw*{zx}':'ih*{zy}',fps={FPS},split[a][b];[a]scale={sw_}:{sh_}:force_original_aspect_ratio=increase,crop={sw_}:{sh_},"
           f"boxblur=20:2,eq=brightness=-0.15[bg];[b]scale={sw_}:{sh_}:force_original_aspect_ratio=decrease[fg];"
           f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
    src = np.float32([[0, 0], [sw_, 0], [sw_, sh_], [0, sh_]])
    scenes = _reader(["-stream_loop", "-1", "-i", str(loop), "-t", f"{total:.3f}"], ow, oh)
    clips = _reader(["-ss", f"{start:.3f}", "-t", f"{total:.3f}", "-i", str(clip), "-filter_complex", can], sw_, sh_)
    # 3) encoder: look (grade, camera, vignette, grain, rounded corners) + backdrop + the clip's sound
    pic = ",".join(GRADES.get(look.get("grade", "none"), [])) or "null"
    pic += _camera(look, ow, oh, total)
    if look.get("vignette"):
        pic += ",vignette=PI/5"
    if look.get("grain"):
        pic += ",noise=alls=7:allf=t"
    ins = ["-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{ow}x{oh}", "-r", str(FPS), "-i", "-",
           "-ss", f"{start:.3f}", "-t", f"{total:.3f}", "-i", str(clip)]
    if (ow, oh) == (W, H):
        fc = f"[0:v]{pic},format=yuv420p[v]"
    else:
        if look.get("round"):
            ins += ["-loop", "1", "-i", str(_round_mask(ow, oh, work / "round.png"))]
            shape = "[p0];[2:v]format=gray[m];[p0]format=rgba[p1];[p1][m]alphamerge[p]"
        else:
            shape = "[p]"
        if look.get("backdrop") == "dark":
            bg = f"color=c=0x0d0f12:s={W}x{H}:r={FPS},format=yuv420p[bg]"
            fc = f"[0:v]{pic},split[pa][pb];[pb]nullsink;{bg};[pa]null{shape}"
        else:
            fc = (f"[0:v]{pic},split[pa][pb];[pb]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                  f"boxblur=32:2,eq=brightness=-0.24:saturation=0.75[bg];[pa]null{shape}")
        fc += f";[bg][p]overlay=0:{TOP}:format=auto:shortest=1,format=yuv420p[v]"
    enc = subprocess.Popen([FFMPEG, "-v", "error", "-y", *ins, "-filter_complex", fc, "-map", "[v]", "-map", "1:a?",
                            "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", str(out)], stdin=subprocess.PIPE)
    # only the part of the picture where the screen ever is gets processed (several times faster)
    bx0, by0, bx1, by1 = info["box"]
    pad = 0.08 * max(bx1 - bx0, by1 - by0)
    rx0, ry0 = int(max(0, (bx0 - pad - x) * k)), int(max(0, (by0 - pad - y) * k))
    rx1, ry1 = int(min(ow, (bx1 + pad - x) * k)), int(min(oh, (by1 + pad - y) * k))
    last, n = None, 0
    kernel = np.ones((3, 3), np.uint8)
    try:
        for frame, cf in zip(scenes, clips):
            frame = frame.copy()
            roi = frame[ry0:ry1, rx0:rx1]
            m = green_mask(roi)
            q = find_quad(roi, m)
            if q is None:
                q = last
            elif last is not None:
                q = np.where(np.abs(q - last) < 1.5, last, 0.5 * q + 0.5 * last)   # steady, no jitter
            if q is not None:
                last = q
                g = q.mean(0) + (q - q.mean(0)) * GROW      # past rounded / curved corners
                M = cv2.getPerspectiveTransform(src, g)
                warped = cv2.warpPerspective(cf, M, (rx1 - rx0, ry1 - ry0), flags=cv2.INTER_LINEAR)
                # every green pixel of the screen itself (its hull), not just of the four-corner shape:
                # rounded phone corners and a curved old TV leave no green rim
                inside = np.zeros(m.shape, np.uint8)
                cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                if cs:
                    cv2.fillConvexPoly(inside, cv2.convexHull(max(cs, key=cv2.contourArea)), 255)
                cv2.fillConvexPoly(inside, g.astype(np.int32), 255)
                key = cv2.bitwise_and(m, cv2.dilate(inside, kernel, iterations=2))
                # soft edge: the green itself fully replaced, its rim blended and de-spilled
                a = cv2.GaussianBlur(key, (5, 5), 0).astype(np.float32)[..., None] / 255.0
                rim = cv2.dilate(key, kernel, iterations=4) > 0
                frame[ry0:ry1, rx0:rx1] = (_despill(roi, rim) * (1 - a) + warped * a).astype(np.uint8)
                n += 1
            enc.stdin.write(frame.tobytes())
    finally:
        enc.stdin.close()
    if enc.wait() != 0 or n < FPS:
        raise ScreenError("compositing failed")
    log.info("screen: %s on scene %s (%s, %s, %s) %d frames", clip.name, look["scene"], look["frame"],
             look["camera"], look["grade"], n)
    return geometry(look, info)
