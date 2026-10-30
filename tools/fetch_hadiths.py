"""Snapshot of authentic hadiths from HadeethEnc (hadeethenc.com — the Encyclopedia of Translated
Prophetic Hadiths) into data/hadiths.json, so making a video never depends on the site being up.

    python -m tools.fetch_hadiths            # (re)build the snapshot; resumes where it stopped

Only grades صحيح / حسن are kept, and only hadiths short enough to read in one Short. The text,
narration intro, collector (attribution) and grade are stored exactly as the source gives them.
"""
from __future__ import annotations

import json
import re
import time

import requests

from src.util import ROOT

B = "https://hadeethenc.com/api/v1"
OUT = ROOT / "data" / "hadiths.json"
# topic shown in the bot -> HadeethEnc category ids
TOPICS = {
    "الرقائق والمواعظ": [314, 316, 317, 318, 319, 322, 324],
    "التوبة والرجاء": [321],
    "الأخلاق": [282, 283, 287],
    "فضائل الأعمال والذكر": [277, 278, 280, 300],
    "الأدعية المأثورة": [313],
    "بر الوالدين": [273],
}
MAX_WORDS = 60          # main text (after the intro): readable in < 1 minute on screen
GRADES = ("صحيح", "حسن")

s = requests.Session()
s.headers["User-Agent"] = "Mozilla/5.0 (ezdadd hadith shorts)"


def get(path: str) -> dict | list:
    for i in range(6):                  # the site drops fast / repeated connections: go slowly
        try:
            r = s.get(f"{B}/{path}", timeout=40)
            r.raise_for_status()
            time.sleep(1.2)
            return r.json()
        except (requests.RequestException, ValueError):
            time.sleep(5 * (i + 1))
    raise SystemExit(f"failed: {path}")


def main() -> None:
    data = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {"hadiths": {}}
    have = data["hadiths"]
    for topic, cats in TOPICS.items():
        for cat in cats:
            page = 1
            while True:
                lst = get(f"hadeeths/list/?language=ar&category_id={cat}&page={page}&per_page=50")
                for h in lst["data"]:
                    if h["id"] in have:
                        have[h["id"]].setdefault("topics", [])
                        if topic not in have[h["id"]]["topics"]:
                            have[h["id"]]["topics"].append(topic)
                        continue
                    one = get(f"hadeeths/one/?language=ar&id={h['id']}")
                    grade, text, intro = one.get("grade", ""), one.get("hadeeth", ""), one.get("hadeeth_intro", "")
                    body = text[len(intro):].strip() if intro and text.startswith(intro) else text
                    words = len(re.sub(r"[«»]", "", body).split())
                    keep = any(g in grade for g in GRADES) and 4 <= words <= MAX_WORDS
                    have[h["id"]] = {"id": h["id"], "keep": keep, "topics": [topic], "title": one.get("title", ""),
                                     "intro": intro, "text": body, "full": text,
                                     "attribution": one.get("attribution", ""), "grade": grade, "words": words,
                                     "source": f"https://hadeethenc.com/ar/browse/hadith/{h['id']}"}
                OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                print(topic, cat, "page", page, "->", sum(1 for x in have.values() if x["keep"]), "kept so far",
                      flush=True)
                if page >= int(lst["meta"]["last_page"]):
                    break
                page += 1
    data["_note"] = ("HadeethEnc snapshot (hadeethenc.com): only صحيح/حسن, short enough for one Short. "
                     "Rebuild: python -m tools.fetch_hadiths")
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
