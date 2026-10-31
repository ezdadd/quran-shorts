"""Add virtual phone scenes to data/screens.json (src/screens.py: render_virtual).

    python -m tools.virtual_phones --n 12        # recipes + a local render of each to review by eye

Backgrounds come from our own footage search (Pixabay) and pass the same modesty check as the
footage behind Quran videos; categories without people (rooms, nature, sky, mosques...). Every new
scene is rendered here so it can be looked at before it is used — remove a bad one from the file.
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import footage, screens  # noqa: E402
from src.util import cache_dir, download, load_config, read_json, setup_logging, write_json  # noqa: E402

log = logging.getLogger("quran-shorts.virtual_phones")
# no green-heavy backgrounds (forest, fields): greenery would be mistaken for the green screen
CATEGORIES = ["home", "sky", "mountain", "islamic", "rain", "ocean", "desert", "snow", "space"]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=12)
    a = ap.parse_args(argv)
    setup_logging()
    cfg = load_config()
    lib = read_json(screens.LIB, {"scenes": {}, "rejected": {}})
    have = {s["virtual"]["bg_id"] for s in lib["scenes"].values() if s.get("virtual")}
    rng = random.Random()
    added = []
    cats = CATEGORIES[:]
    rng.shuffle(cats)
    for cat in cats * 3:
        if len(added) >= a.n:
            break
        q = rng.choice(cfg["footage"]["categories"][cat])
        for c in footage._pixabay(cfg, q, rng)[:6]:
            if c["id"] in have or footage._blocked(c["text"], cfg["footage"]["blocked_words"]):
                continue
            p = download(c["url"], cache_dir(cfg, "screens", "bg") / f"{c['id'].replace(':', '_')}.mp4")
            if not footage._modest(cfg, c["id"], p, cat):
                continue
            sid = f"v-{c['id'].split(':')[1]}"
            spec = {"bg": c["url"], "bg_id": c["id"], "style": rng.choice(list(screens.PHONE_COLORS)),
                    "x": round(rng.uniform(0.38, 0.62), 3), "tilt": round(rng.choice([-1, 1]) * rng.uniform(0.01, 0.05), 3),
                    "blur": rng.choice([6, 10, 14])}
            out = cache_dir(cfg, "screens") / f"{sid}.mp4"
            try:
                screens.render_virtual(cfg, spec, out)
                info = screens.survey(out)
            except screens.ScreenError as e:     # e.g. green in the background: try another clip
                log.info("skip %s: %s", c["id"], e)
                out.unlink(missing_ok=True)
                have.add(c["id"])
                continue
            lib["scenes"][sid] = {"virtual": spec, "tags": f"virtual phone over {cat}: {q}", "kind": "phone",
                                  "portrait": bool(info["portrait"]), "aspect": round(info["aspect"], 3),
                                  "box": [round(x, 1) for x in info["box"]], "size": list(info["size"]),
                                  "user": c["credit"], "page": c["page"], "dur": screens.VSEC}
            have.add(c["id"])
            added.append(sid)
            log.info("✓ %s (%s, %s) over %s", sid, spec["style"], cat, q)
            break
    write_json(screens.LIB, lib)
    log.info("added %d virtual phone scenes: %s", len(added), " ".join(added))


if __name__ == "__main__":
    main()
