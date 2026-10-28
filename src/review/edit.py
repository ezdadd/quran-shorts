"""Editing a clip's on-screen subtitles from Telegram (light module — the poller imports it).

The bot sends the subtitles as numbered lines; you reply with corrections:
    3: اللي كان بيجاوبك ده هو عبدالله بن مسعود
    5: حذف
Quran segments are never listed or editable: they always show the verified text.
"""
from __future__ import annotations

import re

HEADER = "📝 نص الترجمة"
LINE_RE = re.compile(r"^\s*(\d{1,3})\s*[:：\-–.)]\s*(.*)$")
DELETE = {"حذف", "امسح", "شيل", "-", "delete"}


def speech_segments(r: dict) -> list[dict]:
    return [s for s in r.get("segments") or [] if s.get("type") == "speech"]


def numbered_text(r: dict) -> str:
    lines = [f"{HEADER} — 🆔 {r['id']}",
             "رد على الرسالة دي بالتصحيح: رقم السطر ثم النص الصح (أو «حذف»). الآيات بتظهر بالنص الموثّق ومش بتتعدل.", ""]
    for i, s in enumerate(speech_segments(r), 1):
        lines.append(f"{i}) {s.get('text') or ' '.join(w['w'] for w in s.get('words') or [])}")
    return "\n".join(lines)[:4000]


def parse_edits(text: str) -> dict[str, str | None]:
    """{"3": "new text", "5": None (delete)} — keys are strings so they survive JSON."""
    out: dict[str, str | None] = {}
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        body = m.group(2).strip()
        out[str(int(m.group(1)))] = None if body in DELETE or not body else body
    return out


def apply_edits(r: dict, edits: dict[str, str | None]) -> int:
    """Rewrite speech segments in place, re-timing the new words over the old line's time span
    (same word count keeps the original timing). Returns how many lines changed."""
    segs = speech_segments(r)
    changed = 0
    for key, new in edits.items():
        k = int(key) - 1
        if not 0 <= k < len(segs):
            continue
        s = segs[k]
        if new is None:
            s["words"], s["text"] = [], ""
            changed += 1
            continue
        old = s.get("words") or []
        ws = new.split()
        a = old[0]["s"] if old else s["start"]
        b = old[-1]["e"] if old else s["end"]
        if len(ws) == len(old):
            s["words"] = [{"w": w, "s": o["s"], "e": o["e"]} for w, o in zip(ws, old)]
        else:
            total = sum(len(w) for w in ws) or 1
            t, out = a, []
            for w in ws:
                d = (b - a) * len(w) / total
                out.append({"w": w, "s": round(t, 3), "e": round(t + d, 3)})
                t += d
            s["words"] = out
        s["text"] = new
        changed += 1
    # deleted lines leave no empty subtitle
    r["segments"] = [s for s in r["segments"] if s.get("type") != "speech" or s.get("words")]
    return changed
