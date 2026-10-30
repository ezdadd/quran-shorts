"""Automatic clip sourcing: pages / channels you follow -> their new videos become clips.

    python -m src.sources --run                 # daily (sources.yml): pick new videos -> clip requests
    python -m src.sources --probe <url>         # test that an account can be listed from here

You add a source in Telegram: "مصدر: <link> الشيخ <name>" (a YouTube channel, or a TikTok account /
any TikTok video of that account). Every day the newest videos we haven't taken yet (at most
per_source per account and sources.max_per_day in total) go through the normal clip pipeline:
audio only on our footage, subtitles, the sheikh's photo if saved, review / correction agent.
Facebook pages can't be listed automatically (Facebook blocks it): send Facebook links by hand.

data/sources.json (written by the bot) = the list; data/sources_seen.json (written by this job) =
video ids already taken / skipped, so the two never edit the same file.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime, timezone

from .util import ROOT, load_config, read_json, setup_logging, write_json

log = logging.getLogger("quran-shorts.sources")
LIST = ROOT / "data" / "sources.json"
SEEN = ROOT / "data" / "sources_seen.json"


class SourceError(Exception):
    pass


def platform(url: str) -> str | None:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "tiktok.com" in u or u.startswith("tiktokuser:"):
        return "tiktok"
    if "facebook.com" in u or "fb.watch" in u:
        return "facebook"
    if "instagram.com" in u:
        return "instagram"
    return None


def listing_url(url: str) -> str:
    """What yt-dlp should list for this source."""
    p = platform(url)
    if p == "youtube":
        m = re.search(r"youtube\.com/(@[\w.\-]+|channel/[\w\-]+|c/[\w\-]+)", url)
        if not m:
            raise SourceError("ابعت لينك القناة نفسها (youtube.com/@الاسم)، مش لينك فيديو")
        return f"https://www.youtube.com/{m.group(1)}/shorts"
    if p == "tiktok":
        return url
    raise SourceError("المصادر التلقائية: يوتيوب وتيك توك بس — فيسبوك وإنستجرام بيمنعوا قراءة الصفحة، ابعت لينكاتهم بإيدك.")


def _ydl(opts: dict):
    import yt_dlp
    return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": False} | opts)


def latest(source: dict, n: int = 12) -> list[dict]:
    """Newest videos of a source: [{id, url, title, duration}] (newest first)."""
    url = source["list_url"]
    if platform(url) == "tiktok" and "/video/" in url:          # a video link: list its account
        with _ydl({}) as y:
            v = y.extract_info(url, download=False)
        url = f"tiktokuser:{v['channel_id']}"
        source["list_url"] = url
    with _ydl({"extract_flat": True, "playlistend": n}) as y:
        info = y.extract_info(url, download=False)
    out = []
    for e in info.get("entries") or []:
        vid = e.get("id")
        if not vid:
            continue
        link = e.get("url") or e.get("webpage_url") or ""
        if platform(url) == "youtube" and not link.startswith("http"):
            link = f"https://www.youtube.com/shorts/{vid}"
        out.append({"id": f"{platform(url)}:{vid}", "url": link, "title": e.get("title") or "",
                    "duration": e.get("duration")})
    return out


def run(cfg) -> list[dict]:
    """Clip requests for today's new videos (and remembers what was taken)."""
    sc = cfg.get("sources") or {}
    sources = read_json(LIST, {})
    seen = read_json(SEEN, {})
    left = int(sc.get("max_per_day", 3))
    lo, hi = int(sc.get("min_seconds", 15)), int(sc.get("max_seconds", 300))
    reqs, problems = [], []
    for key, s in sources.items():
        if left <= 0:
            break
        try:
            vids = latest(s)
        except Exception as e:
            problems.append(f"{s.get('name') or key}: {str(e)[:120]}")
            continue
        taken = seen.setdefault(key, {})
        first_sync = not taken
        fresh = [v for v in vids if v["id"] not in taken]
        if first_sync:                      # only the newest one now; the older ones count as seen
            for v in fresh[1:]:
                taken[v["id"]] = "old"
            fresh = fresh[:1]
        picked = 0
        for v in fresh:
            if picked >= int(s.get("per_day", sc.get("per_source", 1))) or left <= 0:
                break
            d = v.get("duration")
            if d is not None and not lo <= d <= hi:
                taken[v["id"]] = f"skipped ({int(d)}s)"
                continue
            reqs.append({"id": "src" + re.sub(r"\W", "", v["id"])[-14:], "url": v["url"],
                         "sheikh": s.get("sheikh") or "", "source_key": key})
            taken[v["id"]] = datetime.now(timezone.utc).isoformat(timespec="minutes")
            picked += 1
            left -= 1
    write_json(SEEN, seen)
    if problems:
        log.warning("sources with problems: %s", problems)
    return reqs, problems


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--probe")
    a = ap.parse_args(argv)
    setup_logging()
    cfg = load_config()
    if a.probe:
        try:
            vids = latest({"list_url": listing_url(a.probe)}, 5)
            print("OK", len(vids), [(v["id"], v["duration"]) for v in vids])
            from .clips import ClipError, fetch_audio      # can the audio itself be downloaded here?
            from .util import cache_dir
            try:
                p, _ = fetch_audio(cfg, {"url": vids[0]["url"]}, cache_dir(cfg, "probe"))
                print("DOWNLOAD OK", p.name, p.stat().st_size // 1024, "KB")
            except ClipError as e:
                print("DOWNLOAD FAIL", str(e)[:200])
        except Exception as e:
            print("FAIL", str(e)[:300])
        return
    if a.run:
        reqs, problems = run(cfg)
        (ROOT / ".cache").mkdir(exist_ok=True)
        (ROOT / ".cache" / "clip_requests.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in reqs), encoding="utf-8")
        log.info("%d new videos from sources", len(reqs))
        import os
        if os.environ.get("TELEGRAM_BOT_TOKEN") and (reqs or problems):
            from .review import telegram
            text = f"🛰 المصادر: {len(reqs)} فيديو جديد بيتعمل منهم مقاطع — هيوصلوك للمراجعة."
            if problems:
                text += "\n⚠️ مصادر مقدرتش أقراها:\n" + "\n".join(problems[:5])
            telegram.api("sendMessage", chat_id=telegram.owner(), text=text)


if __name__ == "__main__":
    main()
