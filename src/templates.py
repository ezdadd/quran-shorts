"""Random visual template per video, never repeating the same combination on one day."""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass


@dataclass
class Template:
    font: str
    position: str        # center | lower | upper
    background: str      # shadow | band | frame | ribbon | blur | dim
    animation: str       # whole | line | word
    grade: str           # none | warm | cool | teal_orange | golden | moody | film | dusk | bw
    color: str           # white | gold | cream (may be adjusted after brightness check)
    ken_burns: bool      # kept for old records; camera decides the motion
    vignette: bool
    show_title: bool
    show_reciter: bool
    camera: str = "zoom_in"          # zoom_in | zoom_out | pan | still
    grain: bool = False
    bloom: bool = False
    letterbox: bool = False
    entrance: str = "fade"           # fade | rise
    label_font: str = "Amiri-Regular.ttf"

    def signature(self) -> str:
        return "|".join([self.font, self.position, self.background, self.animation, self.grade])

    def to_dict(self) -> dict:
        return asdict(self)


def _pick(rng: random.Random, weights: dict) -> str:
    keys = [k for k in weights if float(weights[k]) > 0]
    return rng.choices(keys, weights=[float(weights[k]) for k in keys])[0]


def _look(t: dict, rng: random.Random) -> dict:
    """Grade, camera move and effects — shared by Quran videos and lecture/link clips."""
    camera = _pick(rng, t.get("camera") or {"zoom_in": 1})
    return dict(grade=_pick(rng, t["grade"]), camera=camera, ken_burns=camera != "still",
                vignette=rng.random() < t.get("vignette", 0),
                grain=rng.random() < t.get("grain", 0), bloom=rng.random() < t.get("bloom", 0),
                letterbox=rng.random() < t.get("letterbox", 0),
                entrance=_pick(rng, t.get("entrance") or {"fade": 1}),
                label_font=_pick(rng, t.get("label_fonts") or {"Amiri-Regular.ttf": 1}))


def choose(cfg: dict, rng: random.Random, used_today: set[str], word_timing: bool) -> Template:
    t = cfg["templates"]
    for _ in range(200):
        anim_w = dict(t["animation"])
        if not word_timing:
            anim_w.pop("word", None)
        tpl = Template(
            font=_pick(rng, {k: v["weight"] for k, v in t["fonts"].items()}),
            position=_pick(rng, t["position"]),
            background=_pick(rng, t["background"]),
            animation=_pick(rng, anim_w),
            color=rng.choice(list(t["colors"])),
            show_title=rng.random() < t["show_title"],
            show_reciter=rng.random() < t["show_reciter"],
            **_look(t, rng),
        )
        if tpl.signature() not in used_today:
            return tpl
    raise RuntimeError("could not find an unused template combination for today")


def choose_clip(cfg: dict, rng: random.Random) -> Template:
    """Look for a lecture / link clip: random subtitle font + the same cinematic options."""
    t = cfg["templates"]
    subs = t.get("sub_fonts") or {"noto_naskh": {"weight": 1}}
    return Template(font=_pick(rng, {k: v.get("weight", 1) for k, v in subs.items()}), position="lower",
                    background=_pick(rng, {k: v for k, v in t["background"].items()
                                           if k in ("band", "frame", "ribbon")}),
                    animation="word", color="white", show_title=True, show_reciter=True, **_look(t, rng))


def readable_color(tpl: Template, luma: float) -> tuple[str, str]:
    """Pick (text colour, background style) that stays readable on footage of given mean luma
    (0..1, measured in the text zone). Bright footage forces a darker backing."""
    background = tpl.background
    color = tpl.color
    if luma >= 0.35:
        if background in ("shadow", "dim"):
            background = "band"
        if luma >= 0.50 and color == "cream":
            color = "white"
    elif luma >= 0.20 and background == "shadow":
        background = "dim"
    # word/line modes show part of the text dimmed — never on a bare shadow
    if tpl.animation in ("word", "line") and background == "shadow":
        background = "dim"
    return color, background
