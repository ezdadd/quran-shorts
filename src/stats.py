"""Performance: collect views per post, report weekly in Telegram, and gently tune what we pick.

    python -m src.stats --collect          # daily: numbers from Buffer + Facebook -> data/stats.json
    python -m src.stats --report [--days 7] [--tune]

Numbers: Buffer gives views / reactions / comments / shares / watch time for YouTube, TikTok and
Instagram. Facebook gives Reels plays only with the read_insights permission (else 'views', which
under-counts Reels). Each post is refreshed while it is younger than COLLECT_DAYS.

Tuning (data/tuning.json, applied on top of config.yaml by util.load_config — config.yaml itself is
never changed): a theme / reciter / footage category / publishing window whose median views beat
the overall median gets chosen a little more often, and the reverse. Only groups with at least
MIN_SAMPLES videos count, the factor is the square root of the ratio, and it stays in [0.5, 2].
"""
from __future__ import annotations

import argparse
import logging
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .util import ROOT, load_config, read_json, setup_logging, write_json

log = logging.getLogger("quran-shorts.stats")
PATH = ROOT / "data" / "stats.json"
TUNING = ROOT / "data" / "tuning.json"
COLLECT_DAYS = 14
MIN_SAMPLES = 5
PLATFORM_AR = {"youtube": "يوتيوب", "tiktok": "تيك توك", "instagram": "إنستجرام", "facebook": "فيسبوك"}
KIND_AR = {"quran": "📖 قرآن", "hadith": "📜 حديث", "clip": "🎙 مقاطع"}


# ---------------------------------------------------------------- records
def records() -> list[dict]:
    out = []
    for p in sorted((ROOT / "output").glob("*/q-*.json")) + sorted((ROOT / "queue").glob("*.json")):
        r = read_json(p, {})
        if r.get("published"):
            out.append(r)
    return out


def meta(r: dict, cfg) -> dict:
    """What we chose for this video — the things the report compares."""
    tz = ZoneInfo(cfg["timezone"])
    dues = [datetime.fromisoformat(i["due"]) for i in r["published"].values() if i.get("due")]
    first = min(dues).astimezone(tz) if dues else None
    kind = "hadith" if r.get("kind") == "hadith" else "clip" if r.get("kind") == "clip" or r.get("type") == "lecture" \
        else "quran"
    cat = r.get("category") or ((r.get("footage") or [{}])[0].get("category") if isinstance(
        (r.get("footage") or [None])[0], dict) else None)
    return {"kind": kind, "theme": (r.get("reference") or {}).get("theme") or r.get("topic"),
            "reciter": (r.get("reciter") or {}).get("id"), "category": cat,
            "hour": first.hour if first else None, "weekday": first.weekday() if first else None,
            "posted": first.isoformat(timespec="minutes") if first else None,
            "window": _window_of(cfg, first) if first else None, "date": r.get("date")}


def _window_of(cfg, t: datetime) -> str | None:
    p = cfg["publishing"]
    specs = p.get("friday_windows") if t.weekday() == 4 and p.get("friday_windows") else p["windows"]
    for w in specs:
        a, b = w.partition(" x")[0].strip().split("-")
        if a <= t.strftime("%H:%M") <= b:
            return w.partition(" x")[0].strip()
    return None


# ---------------------------------------------------------------- collecting
def _buffer_metrics(post_id: str) -> dict | None:
    from .publish import buffer
    q = """query($id: PostId!) { post(input: {id: $id}) { status metrics { type value } } }"""
    d = buffer.gql(q, {"id": post_id})["post"]
    if d["status"] != "sent":
        return None
    return {m["type"]: m["value"] for m in d.get("metrics") or []}


def _facebook_metrics(cfg, video_id: str) -> dict | None:
    v, tok = cfg["publishing"]["facebook_api_version"], os.environ.get("FB_PAGE_TOKEN")
    if not tok:
        return None
    a = requests.get(f"https://graph.facebook.com/{v}/{video_id}", timeout=30,
                     params={"fields": "published,views", "access_token": tok}).json()
    if not a.get("published"):
        return None
    out = {"views": a.get("views") or 0}
    ins = requests.get(f"https://graph.facebook.com/{v}/{video_id}/video_insights", timeout=30,
                       params={"access_token": tok}).json()
    for x in ins.get("data") or []:             # needs read_insights; Reels plays are the real count
        val = (x.get("values") or [{}])[0].get("value")
        if x.get("name") == "blue_reels_play_count" and isinstance(val, (int, float)):
            out["views"] = val
        elif x.get("name") == "post_video_avg_time_watched" and isinstance(val, (int, float)):
            out["averageTimeWatched"] = round(val / 1000, 2)
    out["_insights"] = bool(ins.get("data"))
    return out


def collect(cfg) -> int:
    data = read_json(PATH, {})
    now = datetime.now(timezone.utc)
    n = 0
    for r in records():
        e = data.setdefault(r["id"], {})
        e["meta"] = meta(r, cfg)
        for pl, info in r["published"].items():
            due = datetime.fromisoformat(info["due"]) if info.get("due") else None
            if not due or due > now or now - due > timedelta(days=COLLECT_DAYS):
                continue
            try:
                m = _facebook_metrics(cfg, info["video_id"]) if pl == "facebook" and info.get("video_id") else \
                    _buffer_metrics(info["post_id"]) if info.get("post_id") else None
            except Exception as ex:
                log.warning("%s %s: %s", r["id"], pl, ex)
                continue
            if m is not None:
                e.setdefault("platforms", {})[pl] = m | {"at": now.isoformat(timespec="minutes")}
                n += 1
    write_json(PATH, data)
    log.info("collected %d post stats", n)
    return n


# ---------------------------------------------------------------- report + tuning
def _views(e: dict) -> float:
    return sum(float(m.get("views") or 0) for m in (e.get("platforms") or {}).values())


def _recent(data: dict, days: int) -> dict:
    cut = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return {k: e for k, e in data.items() if e.get("platforms") and (e["meta"].get("posted") or "") >= cut[:16]}


MATURE_HOURS = 48      # a post a few hours old still shows ~0 views: don't let it drag a group down


def _mature(sel: dict) -> dict:
    cut = (datetime.now(timezone.utc) - timedelta(hours=MATURE_HOURS)).isoformat()[:16]
    return {k: e for k, e in sel.items()
            if (datetime.fromisoformat(e["meta"]["posted"]).astimezone(timezone.utc).isoformat()[:16]
                if e["meta"].get("posted") else "9") <= cut}


def groups(sel: dict, key: str) -> list[tuple[str, float, int]]:
    g: dict[str, list[float]] = defaultdict(list)
    for e in _mature(sel).values():
        k = e["meta"].get(key)
        if k is not None:
            g[str(k)].append(_views(e))
    return sorted(((k, statistics.median(v), len(v)) for k, v in g.items()), key=lambda x: -x[1])


def tune(cfg, data: dict) -> list[str]:
    """Update data/tuning.json from the last 28 days. Returns human-readable changes."""
    sel = _recent(data, 28)
    if len(_mature(sel)) < MIN_SAMPLES * 2:
        return []
    overall = statistics.median(_views(e) for e in _mature(sel).values()) or 1.0
    t = {"updated": datetime.now(timezone.utc).isoformat(timespec="minutes")}
    changes = []
    for key, name in (("theme", "theme_weights"), ("reciter", "reciter_weights"), ("category", "category_weights"),
                      ("window", "window_weights")):
        mult = {}
        for k, med, cnt in groups(sel, key):
            if cnt < MIN_SAMPLES:
                continue
            f = round(min(2.0, max(0.5, (med / overall) ** 0.5)), 2) if med > 0 else 0.5
            mult[k] = f
            if abs(f - 1) >= 0.15:
                changes.append(f"{'⬆️' if f > 1 else '⬇️'} {k}: ×{f}")
        t[name] = mult
    write_json(TUNING, t)
    return changes


def report(cfg, days: int = 7, do_tune: bool = False) -> str:
    data = read_json(PATH, {})
    sel = _recent(data, days)
    if not sel:
        return "📊 لسه مفيش أرقام كفاية للتقرير (بيتجمع كل يوم)."
    total = sum(_views(e) for e in sel.values())
    per_pl: dict[str, float] = defaultdict(float)
    for e in sel.values():
        for pl, m in (e.get("platforms") or {}).items():
            per_pl[pl] += float(m.get("views") or 0)
    lines = [f"📊 تقرير آخر {days} أيام — {len(sel)} فيديو، {int(total):,} مشاهدة"]
    lines.append("المنصات: " + " · ".join(f"{PLATFORM_AR.get(p, p)} {int(v):,}" for p, v in
                                         sorted(per_pl.items(), key=lambda x: -x[1])))
    if "facebook" in per_pl and not any((e.get("platforms") or {}).get("facebook", {}).get("_insights")
                                        for e in sel.values()):
        lines.append("ℹ️ أرقام فيسبوك ناقصة (محتاج صلاحية read_insights عشان مشاهدات الريلز الحقيقية).")
    top = sorted(sel.items(), key=lambda x: -_views(x[1]))[:3]
    lines.append("\n🏆 الأعلى:")
    for k, e in top:
        m = e["meta"]
        lines.append(f"• {k} ({KIND_AR.get(m['kind'], m['kind'])}{' · ' + m['theme'] if m.get('theme') else ''}) — "
                     f"{int(_views(e)):,}")
    lines.append(f"\n(المقارنات من الفيديوهات اللي عدى عليها {MATURE_HOURS} ساعة بس)")
    for key, title in (("kind", "النوع"), ("window", "وقت النشر"), ("theme", "الموضوع"), ("reciter", "القارئ"),
                       ("category", "المشاهد")):
        gs = [g for g in groups(sel, key) if g[2] >= 2][:4]
        if gs:
            lines.append(f"\n{title} (متوسط المشاهدات):")
            names = {r: v.get("name_ar", r) + (" (مجوّد)" if "mujawwad" in r else "")
                     for r, v in (cfg.get("reciters") or {}).items()}
            label = (lambda k: KIND_AR.get(k, k)) if key == "kind" else \
                (lambda k: names.get(k, k)) if key == "reciter" else (lambda k: k)
            lines += [f"  {label(k)}: {int(med):,} ({n} فيديو)" for k, med, n in gs]
    if do_tune:
        ch = tune(cfg, data)
        lines.append("\n🎯 ضبط تلقائي للاختيارات:" + ("\n" + "\n".join(ch) if ch else
                                                   " مفيش تغيير (محتاج 5 فيديوهات على الأقل لكل مجموعة)."))
    return "\n".join(lines)[:4000]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--tune", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args(argv)
    setup_logging()
    cfg = load_config()
    if a.collect:
        collect(cfg)
    if a.report:
        text = report(cfg, a.days, a.tune)
        print(text)
        if os.environ.get("TELEGRAM_BOT_TOKEN"):
            from .review import telegram
            telegram.api("sendMessage", chat_id=telegram.owner(), text=text)


if __name__ == "__main__":
    main()
