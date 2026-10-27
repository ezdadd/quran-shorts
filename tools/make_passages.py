"""Build data/passages.json (references only — the text is always fetched from the API).

    python -m tools.make_passages          # write the file
    python -m tools.make_passages --check  # also verify every ayah against both text APIs
"""
from __future__ import annotations

import sys

from src import quran
from src.util import load_config, path, setup_logging, write_json

T = {
    "c": "تأمل في الخلق",
    "t": "تسبيح وتعظيم",
    "p": "طمأنينة وصبر",
    "m": "رحمة ومغفرة",
    "d": "دعاء",
}

# (surah, from, to, theme)
RAW = """
3 190 191 c | 6 95 96 c | 6 99 99 c | 10 5 6 c | 13 3 3 c | 13 12 13 c | 14 32 33 c
16 10 11 c | 16 12 13 c | 16 14 14 c | 16 65 65 c | 16 68 69 c | 16 79 79 c | 21 30 31 c
21 32 33 c | 22 63 64 c | 24 41 41 c | 24 43 44 c | 25 45 46 c | 25 48 49 c | 25 53 53 c
27 61 62 c | 30 21 21 c | 30 22 22 c | 30 24 24 c | 35 27 27 c | 36 38 40 c | 39 21 21 c
41 39 39 c | 45 3 4 c | 50 6 8 c | 50 9 11 c | 51 47 49 c | 55 5 9 c | 55 10 13 c
55 19 23 c | 55 26 27 c | 56 68 70 c | 67 3 4 c | 67 19 19 c | 71 15 16 c | 71 19 20 c
78 6 10 c | 78 12 16 c | 79 27 30 c | 79 31 33 c | 80 24 27 c | 88 17 20 c | 6 59 59 c
31 27 27 c | 18 109 109 c
57 2 3 t | 59 22 23 t | 59 24 24 t | 17 44 44 t | 30 17 18 t | 33 41 42 t | 33 43 43 t
87 2 5 t | 20 130 130 t | 15 97 99 t | 50 39 40 t | 52 48 49 t | 3 26 27 t | 6 102 103 t
40 64 65 t | 25 58 58 t | 37 180 182 t | 21 87 88 t
13 28 28 p | 2 152 152 p | 2 186 186 p | 94 5 8 p | 93 3 5 p | 93 6 8 p | 65 3 3 p
9 129 129 p | 3 139 139 p | 3 173 174 p | 48 4 4 p | 12 87 87 p | 20 45 46 p | 26 61 62 p
29 69 69 p | 40 44 44 p | 41 30 31 p | 10 62 64 p | 89 27 30 p | 16 97 97 p | 57 22 23 p
64 11 11 p | 2 155 157 p | 2 45 46 p | 47 7 7 p | 3 160 160 p | 17 82 82 p | 10 57 57 p
39 53 53 m | 7 56 56 m | 15 49 50 m | 4 110 110 m | 3 135 135 m | 25 70 70 m | 42 25 25 m
11 90 90 m | 7 23 23 m | 21 83 84 m | 2 218 218 m | 4 27 28 m | 85 14 16 m | 12 64 64 m
2 201 201 d | 2 127 128 d | 3 8 9 d | 3 193 194 d | 14 40 41 d | 17 80 80 d | 18 10 10 d
20 25 28 d | 23 118 118 d | 25 74 74 d | 26 83 85 d | 27 19 19 d | 28 24 24 d | 59 10 10 d
60 5 5 d | 71 28 28 d | 3 38 38 d | 21 89 90 d | 10 85 86 d | 12 101 101 d | 2 250 250 d
3 53 53 d | 17 111 111 d | 3 147 147 d | 7 126 126 d | 66 8 8 d
"""


def parse() -> list[dict]:
    out = []
    for chunk in RAW.replace("\n", "|").split("|"):
        chunk = chunk.strip()
        if not chunk:
            continue
        s, a, b, t = chunk.split()
        out.append({"surah": int(s), "from": int(a), "to": int(b), "theme": T[t]})
    return out


def main():
    setup_logging()
    cfg = load_config()
    items = parse()
    keys = [(p["surah"], p["from"], p["to"]) for p in items]
    assert len(keys) == len(set(keys)), "duplicate passage"
    check = "--check" in sys.argv
    ok, bad = [], []
    if check:
        chaps = quran.chapters(cfg)
        reciter = cfg["reciters"]["husary"]
        for p in items:
            try:
                if p["to"] > chaps[p["surah"]]["verses_count"] or p["to"] - p["from"] > 4:
                    raise ValueError("range out of surah or > 5 ayat")
                for n in range(p["from"], p["to"] + 1):
                    v = quran._quran_com_verse(cfg, p["surah"], n, reciter["quran_com_id"])
                    words = [w["text_uthmani"] for w in v["words"] if w["char_type_name"] == "word"]
                    other = quran._alquran_cloud_text(cfg, p["surah"], n)
                    if not (quran.skeleton(" ".join(words)) == quran.skeleton(v["text_uthmani"])
                            == quran.skeleton(other)):
                        raise quran.TextMismatch(f"{p['surah']}:{n} letters differ")
                ok.append(p)
            except Exception as e:
                bad.append((p, str(e)))
        for p, e in bad:
            print("BAD", p, e)
        print(f"checked {len(items)}: {len(ok)} ok, {len(bad)} bad")
    write_json(path(cfg, "passages"), {
        "_note": "References only. Text is fetched from Quran.com and cross-checked with alquran.cloud. "
                 "Review and edit freely; passages must not start at ayah 1 (basmala ambiguity).",
        "passages": items,
    })
    print(f"wrote {len(items)} passages")


if __name__ == "__main__":
    main()
