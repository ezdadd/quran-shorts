"""Parse a Telegram message into a clip request (no heavy imports: used by the poller)."""
from __future__ import annotations

import re

URL_RE = re.compile(r"https?://\S+")
TIME_RE = re.compile(r"(?<![\w/])((?:\d{1,2}:)?\d{1,2}:\d{2}|\d{1,5})\s*[-–—]\s*((?:\d{1,2}:)?\d{1,2}:\d{2}|\d{1,5})(?![\w/])")


def _secs(t: str) -> float:
    parts = [float(x) for x in t.split(":")]
    s = 0.0
    for x in parts:
        s = s * 60 + x
    return s


def parse_request(text: str) -> dict:
    """Pull url / start-end / sheikh / title / count out of a free-form Telegram message.
        https://... 1:20-2:05
        الشيخ محمد متولي الشعراوي
        عنوان: الرزق
        3 مقاطع"""
    req: dict = {}
    m = URL_RE.search(text)
    if m:
        req["url"] = m.group(0).rstrip(").,،")
    rest = URL_RE.sub(" ", text)
    t = TIME_RE.search(rest)
    if t:
        a, b = _secs(t.group(1)), _secs(t.group(2))
        if b > a:
            req["start"], req["end"] = a, b
        rest = rest.replace(t.group(0), " ")
    for line in rest.splitlines():
        line = line.strip()
        if re.match(r"^(الشيخ|شيخ|القارئ|قارئ)\b", line):
            req["sheikh"] = re.sub(r"^(شيخ|قارئ)\s*[:：]\s*", "", line).strip()
        elif re.match(r"^(عنوان|العنوان)\s*[:：]", line):
            req["title"] = re.sub(r"^(عنوان|العنوان)\s*[:：]\s*", "", line).strip()
        n = re.search(r"(\d)\s*(مقاطع|مقطع|clips?)", line)
        if n:
            req["n"] = max(1, min(5, int(n.group(1))))
    return req


def sound_name(text: str) -> str | None:
    """A link (or a sent video) with a plain name next to it = save it as a sound, not a clip.
    "صوت: <name>" always counts; otherwise the leftover text must not hold any clip instruction
    (time range, الشيخ, عنوان:, N مقاطع) and must contain letters."""
    m = re.search(r"(?m)^\s*(?:صوت|sound)\s*[:：]\s*(.+?)\s*$", text or "")
    if m:
        return URL_RE.sub(" ", m.group(1)).strip(" -–—:،,")[:40] or None
    rest = URL_RE.sub(" ", text or "").strip()
    if not rest or not re.search(r"[^\W\d_]", rest):
        return None                         # the link alone -> a clip, as before
    req = parse_request(text)
    if any(k in req for k in ("start", "sheikh", "title", "n")):
        return None                         # clip instructions -> a clip
    return re.sub(r"\s+", " ", rest).strip(" -–—:،,")[:40] or None
