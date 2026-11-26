"""Did what should have happened actually happen? (the daily health check's second half)

Every job can finish "successfully" while nothing comes out of it: in Nov 2026 the Quran passages
ran out (6 days without a Quran video), Facebook-only items blocked the queue (10 days without a
clip), long clips were killed mid-render (19 lost) — all silent. So once a day this compares what
was made and posted yesterday with what was expected, and lists what is stuck:

  * Quran videos made for yesterday vs daily.quran_count
  * posts per platform yesterday (Facebook + each Buffer channel; "manual" = waiting for you)
  * approved videos that still haven't gone out after STUCK_HOURS
  * Buffer's spare requests, hosted videos piling up on GitHub (cleanup not running)
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .util import ROOT, read_json

log = logging.getLogger("quran-shorts.outcomes")
STUCK_HOURS = 48
MAX_HOSTED = 80          # videos on GitHub at once; more = they aren't being cleaned up
PL_AR = {"facebook": "فيسبوك", "youtube": "يوتيوب", "tiktok": "تيك توك", "instagram": "إنستجرام"}


def _records(root: Path) -> list[tuple[Path, dict]]:
    """Each video once: its queue copy (where publishing writes) over its older output/review copy."""
    seen, out = set(), []
    for p in list((root / "queue").glob("*.json")) + list((root / "output").glob("*/*.json")):
        r = read_json(p, {})
        if r.get("id") and r["id"] not in seen and p.parent.name != "visual_test":
            seen.add(r["id"])
            out.append((p, r))
    return out


def posted_on(records: list[tuple[Path, dict]], day: date, tz) -> dict[str, Counter]:
    """platform -> Counter(status) of the posts whose time fell on `day` (local)."""
    out = {}
    for _, r in records:
        for pl, i in (r.get("published") or {}).items():
            if i.get("due") and datetime.fromisoformat(i["due"]).astimezone(tz).date() == day:
                out.setdefault(pl, Counter())[i.get("status") or "?"] += 1
    return out


def stuck(records: list[tuple[Path, dict]], reviews: dict, now: datetime) -> list[str]:
    """Approved more than STUCK_HOURS ago (or its own day is that long past) and still not out.
    Not counted: a series part whose earlier part still waits for you (parts go out in order), and
    a video never sent to you for review (old local tests)."""
    from .review import store
    ok = {r["id"] for _, r in records if r.get("id") and (r.get("published") or store.is_approved(r, reviews))}
    out = []
    for _, r in records:
        if r.get("published") or not r.get("id") or store.is_rejected(r, reviews) or not store.is_approved(r, reviews):
            continue
        if not (r.get("telegram") or {}).get("message_id"):
            continue
        s = r.get("series") or {}
        if s.get("part", 1) > 1 and any(f"{r['id'].rsplit('-p', 1)[0]}-p{k:02d}" not in ok for k in range(1, s["part"])):
            continue
        at = (store.get(r["id"], reviews).get("at") or (r.get("auto") or {}).get("at")
              or (r.get("date") and f"{r['date']}T23:59:00+00:00"))
        if at and now - datetime.fromisoformat(at) > timedelta(hours=STUCK_HOURS):
            out.append(r["id"])
    return sorted(set(out))


def check(cfg, today: date | None = None, root: Path = ROOT) -> list[str]:
    tz = ZoneInfo(cfg["timezone"])
    now = datetime.now(tz)
    day = (today or now.date()) - timedelta(days=1)
    probs: list[str] = []
    recs = _records(root)
    # 1) Quran videos made
    want = int(cfg["daily"].get("quran_count", 0))
    made = len(list((root / "output" / day.isoformat()).glob("q-*.json")))
    if made < want:
        probs.append(f"قرآن: اتعمل {made} من {want} فيديو امبارح ({day}) — شوف لوج daily-shorts.")
    # 2) posts per platform
    posts = posted_on(recs, day, tz)
    p = cfg["publishing"]
    for pl in p["platforms"]:
        c = posts.get(pl, Counter())
        real = sum(v for k, v in c.items() if k != "manual")
        if c.get("manual"):
            probs.append(f"{PL_AR.get(pl, pl)}: {c['manual']} فيديو امبارح مستنيين تنشرهم بإيدك (لينكات التنزيل).")
        elif real == 0:
            probs.append(f"{PL_AR.get(pl, pl)}: ولا بوست امبارح ({day}).")
    if posts.get("facebook") and sum(posts["facebook"].values()) < made // 2:
        probs.append(f"فيسبوك: {sum(posts['facebook'].values())} بوست بس امبارح من {made} فيديو قرآن اتعملوا.")
    # 3) approved but not out
    from .review import store
    late = stuck(recs, store.load() if root == ROOT else read_json(root / "data" / "reviews.json", {}),
                 now.astimezone(timezone.utc))
    if late:
        probs.append(f"{len(late)} فيديو متوافق عليهم من أكتر من {STUCK_HOURS // 24} يوم ولسه متنشروش: "
                     + ", ".join(late[:6]) + (" …" if len(late) > 6 else ""))
    # 4) limits / cleanup
    if os.environ.get("BUFFER_API_KEY"):
        from .publish import buffer
        if not buffer.blocked_until() and not buffer.spare(1):
            probs.append("Buffer: الطلبات الفاضلة في الشهر قربت تخلص — الإحصائيات هتقف والجدولة ليها الأولوية.")
    repo, tok = p.get("media_repo"), os.environ.get("MEDIA_TOKEN")
    if p.get("hosting") == "github" and repo and tok:
        try:
            rel = requests.get(f"https://api.github.com/repos/{repo}/releases/tags/media", timeout=30,
                               headers={"Authorization": f"Bearer {tok}"})
            if rel.ok:
                n = rel.json().get("assets") and len(rel.json()["assets"]) or 0
                if n > MAX_HOSTED:
                    probs.append(f"GitHub: {n} فيديو متخزنين في {repo} — التنظيف مش شغال (cleanup-hosting).")
        except requests.RequestException as e:
            log.warning("media repo: %s", e)
    return probs
