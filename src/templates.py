"""Random visual template per video, never repeating the same combination on one day."""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass


@dataclass
class Template:
    font: str
    position: str        # center | lower | upper
    background: str      # shadow | band | blur | dim
    animation: str       # whole | line | word
    grade: str           # none | warm | cool
    color: str           # white | gold | cream (may be adjusted after brightness check)
    ken_burns: bool
    vignette: bool
    show_title: bool
    show_reciter: bool

    def signature(self) -> str:
        return "|".join([self.font, self.position, self.background, self.animation, self.grade])

    def to_dict(self) -> dict:
        return asdict(self)


def _pick(rng: random.Random, weights: dict) -> str:
    keys = list(weights)
    return rng.choices(keys, weights=[float(weights[k]) for k in keys])[0]


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
            grade=_pick(rng, t["grade"]),
            color=rng.choice(list(t["colors"])),
            ken_burns=rng.random() < t["ken_burns"],
            vignette=rng.random() < t["vignette"],
            show_title=rng.random() < t["show_title"],
            show_reciter=rng.random() < t["show_reciter"],
        )
        if tpl.signature() not in used_today:
            return tpl
    raise RuntimeError("could not find an unused template combination for today")


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
