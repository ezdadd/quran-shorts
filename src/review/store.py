"""Review decisions made in Telegram: data/reviews.json  {video_id: {status, tags, at, ...}}.

Only the Telegram poller writes this file; the daily run only reads it — so the two GitHub
workflows never edit the same file and their commits never conflict.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from ..util import ROOT, read_json, write_json

PATH = ROOT / "data" / "reviews.json"
CREDIT_LINE = re.compile(r"^\s*Video by .+ on (Pixabay|Pexels)\s*$", re.M)
TAG = re.compile(r"[#＃][\w؀-ۿ]+")


def load() -> dict:
    return read_json(PATH, {})


def save(d: dict) -> None:
    write_json(PATH, d)


def get(video_id: str, reviews: dict | None = None) -> dict:
    return (reviews if reviews is not None else load()).get(video_id, {})


def set_status(reviews: dict, video_id: str, status: str, by: str = "") -> None:
    e = reviews.setdefault(video_id, {})
    e.update(status=status, at=datetime.now(timezone.utc).isoformat(timespec="seconds"), by=by)


def edit_tags(current: list[str], text: str) -> list[str]:
    """'#a #b' replaces the list; any '+x' / '-x' (sign before or after, with or without '#')
    switches to add/remove mode, where plain tags are added too."""
    ops = []
    for w in text.replace("،", " ").replace(",", " ").split():
        sign = "+" if ("+" in w[:2] or w.endswith("+")) else "-" if (w.startswith("-") or w.endswith("-")) else ""
        tag = w.strip("+-#＃ ‎‏")
        if tag:
            ops.append((sign, "#" + tag))
    if not any(sign for sign, _ in ops):
        return [t for _, t in ops]
    out = list(current)
    for sign, t in ops:
        if sign == "-":
            out = [x for x in out if x != t]
        elif t not in out:
            out.append(t)
    return out


def is_rejected(r: dict, reviews: dict) -> bool:
    return bool(r.get("rejected")) or get(r["id"], reviews).get("status") == "rejected"


def is_approved(r: dict, reviews: dict) -> bool:
    """Your ✅, or the correction agent's approval when you haven't decided anything yourself
    (any status of yours — including ↩️ "waiting" — overrides the agent)."""
    st = get(r["id"], reviews).get("status")
    return bool(r.get("approved")) or st == "approved" or \
        (st is None and (r.get("auto") or {}).get("status") == "approved")


def final_caption(r: dict, platform: str, reviews: dict) -> str:
    """Stored caption, minus any footage credit line, with the reviewer's hashtags if edited."""
    p = r.get("platforms", {}).get(platform) or r["platforms"]["tiktok"]
    cap = CREDIT_LINE.sub("", p["caption"]).rstrip()
    tags = get(r["id"], reviews).get("tags")
    if tags is not None:
        old = " ".join(p.get("hashtags") or [])
        new = list(tags)
        if platform == "youtube" and "#shorts" not in new:
            new.append("#shorts")
        cap = cap.replace(old, " ".join(new)) if old and old in cap else \
            TAG.sub("", cap).rstrip() + "\n\n" + " ".join(new)
    return re.sub(r"\n{3,}", "\n\n", cap).strip()
