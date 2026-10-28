"""What the agent learns from YOUR corrections (free, no model training needed).

Every subtitle edit you send in Telegram ("3: النص الصح") is saved as one file in
data/corrections/ (one file per edit, so parallel GitHub jobs never conflict). From them:

  * fix rules   — a word/phrase you corrected the same way >= agent.learn_min_count times is fixed
                  automatically in every new transcript, before any model runs
                  (e.g. «ابن القائم» -> «ابن القيم»);
  * hotwords    — names and terms from your corrections are given to Whisper, so it hears them
                  right in the first place;
  * examples    — your most recent corrections are shown to the correction agent as references.

Later, the same files are the training set for fine-tuning our own Whisper (phase 3).
"""
from __future__ import annotations

import difflib
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

from ..util import ROOT, read_json, write_json

DIR = ROOT / "data" / "corrections"
MAX_PHRASE = 3          # words per side in one learned fix
_MARKS = re.compile("[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")   # tashkeel, tatweel
_PUNCT = re.compile(r"[^\wء-ي٠-٩ٱ-ۓ]")    # keeps letters / digits only


def norm(w: str) -> str:
    """Light key: drops tashkeel / tatweel / punctuation only. Spelling stays significant
    (unlike the Quran matcher's loose key), so hamza and ة/ه fixes are learned too."""
    return _PUNCT.sub("", _MARKS.sub("", w))


def record(r: dict, pairs: list[dict], source: str = "telegram") -> None:
    """pairs: [{"line": n, "old": text, "new": text | None (deleted)}] — only real changes."""
    pairs = [p for p in pairs if p["new"] is None or norm(p["new"]) != norm(p["old"])]
    if not pairs:
        return
    DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    write_json(DIR / f"{r['id']}-{now:%Y%m%dT%H%M%S}.json", {
        "id": r["id"], "at": now.isoformat(timespec="seconds"), "source": source,
        "sheikh": r.get("sheikh_name") or "", "audio": (r.get("hosting") or {}).get("url"),
        "pairs": pairs})


def pairs() -> list[dict]:
    """All corrected line pairs, newest first."""
    out = []
    for p in sorted(DIR.glob("*.json"), reverse=True) if DIR.exists() else []:
        d = read_json(p, {})
        out += [{**x, "sheikh": d.get("sheikh", "")} for x in d.get("pairs") or []]
    return out


def _phrase_changes(old: str, new: str):
    a, b = old.split(), new.split()
    sm = difflib.SequenceMatcher(None, [norm(w) for w in a], [norm(w) for w in b], autojunk=False)
    for op, i0, i1, j0, j1 in sm.get_opcodes():
        if op == "replace" and i1 - i0 <= MAX_PHRASE and j1 - j0 <= MAX_PHRASE:
            yield tuple(a[i0:i1]), tuple(b[j0:j1])


def rules(min_count: int = 2) -> dict[tuple, tuple]:
    """{normalized old phrase: new words} for fixes you made consistently >= min_count times."""
    seen: dict[tuple, Counter] = defaultdict(Counter)
    for p in pairs():
        if p.get("new"):
            for old, new in _phrase_changes(p["old"], p["new"]):
                seen[tuple(norm(w) for w in old)][new] += 1
    out = {}
    for key, c in seen.items():
        (best, n), total = c.most_common(1)[0], sum(c.values())
        if n >= min_count and n / total >= 0.8 and all(key):
            out[key] = best
    return out


def apply_rules(words: list[dict], fixes: dict[tuple, tuple]) -> int:
    """Rewrite a Whisper word list [{w, s, e, p}] in place with the learned fixes (longest match
    first); new words share the old span's time evenly. Returns how many fixes were applied."""
    if not fixes:
        return 0
    longest = max(len(k) for k in fixes)
    i, n = 0, 0
    while i < len(words):
        for size in range(min(longest, len(words) - i), 0, -1):
            key = tuple(norm(w["w"]) for w in words[i:i + size])
            new = fixes.get(key)
            if new is None:
                continue
            s, e = words[i]["s"], words[i + size - 1]["e"]
            step = (e - s) / len(new)
            words[i:i + size] = [{"w": w, "s": round(s + k * step, 3), "e": round(s + (k + 1) * step, 3),
                                  "p": 1.0} for k, w in enumerate(new)]
            i += len(new) - 1
            n += 1
            break
        i += 1
    return n


def hotwords(limit: int = 40) -> str:
    """Words you typed in corrections that Whisper had got wrong (names, terms), most frequent
    first — passed to Whisper as hotwords."""
    c: Counter = Counter()
    for p in pairs():
        if p.get("new"):
            for _, new in _phrase_changes(p["old"], p["new"]):
                c.update(w for w in new if len(norm(w)) >= 3)
    return " ".join(w for w, _ in c.most_common(limit))


def examples(n: int = 20) -> str:
    """Your latest line corrections as few-shot references for the agent."""
    lines = []
    for p in pairs()[:n]:
        lines.append(f"- قبل: {p['old']}\n  بعد: {p['new'] if p.get('new') else '(اتمسح)'}")
    return "\n".join(lines)
