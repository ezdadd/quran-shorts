"""Download a lecture into assets/lectures/<sheikh>/ (you are responsible for permission).

    python -m tools.fetch_lecture samir_mostafa https://www.youtube.com/watch?v=...
    python -m tools.fetch_lecture amgad_samir   https://...

Keeps <=1080p MP4 and writes <file>.info.json (title, channel, url) next to it so every
clip can credit its source.
"""
from __future__ import annotations

import json
import sys

from yt_dlp import YoutubeDL

from src.util import ROOT, load_config

SHEIKHS = {"samir_mostafa": "الشيخ سمير مصطفى", "amgad_samir": "الشيخ أمجد سمير"}


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in SHEIKHS:
        print(__doc__)
        sys.exit(1)
    sheikh, urls = sys.argv[1], sys.argv[2:]
    cfg = load_config()
    out = ROOT / "assets" / "lectures" / sheikh
    out.mkdir(parents=True, exist_ok=True)
    from src.util import FFMPEG
    opts = {
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(out / "%(id)s.%(ext)s"),
        "ffmpeg_location": str(FFMPEG),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    with YoutubeDL(opts) as ydl:
        for url in urls:
            info = ydl.extract_info(url, download=True)
            meta = {k: info.get(k) for k in ("id", "title", "channel", "webpage_url", "duration",
                                               "upload_date", "width", "height")}
            meta["sheikh_id"] = sheikh
            meta["sheikh_name"] = SHEIKHS[sheikh]
            p = out / f"{info['id']}.info.json"
            p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"saved {info['id']}.mp4 — {meta['title']} ({meta['duration']}s)")


if __name__ == "__main__":
    main()
