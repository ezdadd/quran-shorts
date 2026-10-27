"""Render sample ayat (dense tashkeel + pause marks) in every font for manual inspection.
Long ayat are split into screens exactly as in the videos; one PNG per screen.

    python -m tools.visual_test
"""
from __future__ import annotations

from PIL import Image

from src import quran
from src.render import AYAH_END, CENTER_Y, MAX_TEXT_H, _paginate
from src.textrender import TextRenderer
from src.util import ROOT, load_config, setup_logging

SAMPLES = [(2, 255), (24, 35), (55, 13), (3, 190), (20, 46)]


def main():
    setup_logging()
    cfg = load_config()
    t = cfg["templates"]
    out = ROOT / "output" / "visual_test"
    out.mkdir(parents=True, exist_ok=True)
    reciter = cfg["reciters"]["husary"]
    ayat = [quran.load_ayah(cfg, reciter, s, a) for s, a in SAMPLES]
    bg = Image.linear_gradient("L").resize((1080, 1920)).convert("RGB")
    bg = Image.merge("RGB", [c.point(lambda v: 60 + v * 0.5) for c in bg.split()])
    with TextRenderer(cfg) as tr:
        for fid, f in t["fonts"].items():
            tr.use_fonts(f["file"])
            kw = dict(style="band", color="#FFFFFF", accent="#F2D27A", size=f["size"],
                      center_y=CENTER_Y["center"], attach_marks=f.get("attach_marks", False),
                      weight=f.get("css_weight", 400), min_size=t["min_size"])
            for a in ayat:
                pages = _paginate(tr, a, kw, t["max_lines"]["center"])
                for pi, (i0, i1) in enumerate(pages):
                    last = pi == len(pages) - 1
                    info = tr.layout_ayah(a.words[i0:i1],
                                          AYAH_END + quran.to_arabic_digits(a.ayah) if last else None,
                                          max_h=MAX_TEXT_H["center"], open_bracket=pi == 0,
                                          close_bracket=last, **kw)
                    layer = tr.state(info, word=min(2, i1 - i0 - 1))
                    img = bg.copy()
                    img.paste(layer.image, (0, 0), layer.image)
                    p = out / f"{fid}_{a.surah}_{a.ayah}_p{pi + 1}.png"
                    img.save(p)
                    print(p.name, "size", info["size"], "lines", info["lines"])
        # labels (reciter + channel tag)
        tr.use_fonts(t["fonts"]["amiri"]["file"])
        b = cfg["branding"]
        lab = tr.labels([{"text": "القارئ محمود خليل الحصري", "y": b["y"] - 75, "size": 38},
                         {"text": b["handle"], "y": b["y"], "size": b["size"], "pill": True}])
        img = bg.copy()
        img.paste(lab, (0, 0), lab)
        img.save(out / "labels.png")


if __name__ == "__main__":
    main()
