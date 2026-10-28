"""Daily publishing: pick the day's videos, give each a random time, host once, schedule on every
platform, record everything (no double posting), and clean hosted files up after publishing.

    python -m src.publish.runner --dry-run            # show today's schedule, call nothing
    python -m src.publish.runner                      # schedule for real
    python -m src.publish.runner --cleanup            # status check + delete published files
"""
from __future__ import annotations

import argparse
import logging
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import scheduler
from ..util import ROOT, load_config, path, read_json, setup_logging, write_json
from . import buffer, facebook, hosting, notify, queue
from ..review import store as review

log = logging.getLogger("quran-shorts.publish")
OUTPUT = ROOT / "output"


def _ok_to_publish(r: dict, reviews: dict | None = None) -> bool:
    reviews = review.load() if reviews is None else reviews
    if review.is_rejected(r, reviews):
        return False
    cfg = load_config()
    if cfg["publishing"].get("require_approval", True) or r.get("type") == "lecture"             or r.get("review_required"):
        return review.is_approved(r, reviews)
    return True


def todays_items(cfg, day: date) -> list[tuple[Path, dict]]:
    items: list[tuple[Path, dict]] = []
    for p in sorted((OUTPUT / day.isoformat()).glob("q-*.json")):
        r = read_json(p, {})
        if _ok_to_publish(r):
            items.append((p, r))
    # approved items that needed a human review (lectures, aligned recordings) come from the
    # committed queue — they were made locally and are already hosted
    n_q = cfg["daily"].get("queue_per_day", 1)
    platforms = cfg["publishing"]["platforms"]
    reviews = review.load()
    pending: list[tuple[Path, dict]] = []
    blocked: set[str] = set()       # a series whose next part is still waiting for review
    allq = queue.items()
    first_q = {}
    for p, r in allq:
        sid = (r.get("series") or {}).get("id")
        if sid:
            first_q.setdefault(sid, r.get("queued_at", ""))
    allq.sort(key=lambda x: (first_q.get((x[1].get("series") or {}).get("id"), x[1].get("queued_at", "")),
                             (x[1].get("series") or {}).get("part", 0)))
    for p, r in allq:
        sid = (r.get("series") or {}).get("id")
        done = not any(pl not in (r.get("published") or {}) for pl in platforms)
        if review.is_rejected(r, reviews) or done:
            continue
        if sid and sid in blocked:
            continue
        if _ok_to_publish(r, reviews):
            pending.append((p, r))
        elif sid:
            blocked.add(sid)        # parts go out strictly in order
    items += pending[:n_q]
    return items


def video_file(rec_path: Path, r: dict) -> Path:
    """The MP4 next to the record, or (queue items on the CI runner) a copy fetched from hosting."""
    mp4 = rec_path.with_suffix(".mp4")
    local = ROOT / r.get("local_record", "")
    if not mp4.exists() and r.get("local_record") and local.with_suffix(".mp4").exists():
        mp4 = local.with_suffix(".mp4")
    if not mp4.exists():
        url = (r.get("hosting") or {}).get("url")
        if not url:
            raise FileNotFoundError(f"{r['id']}: no local MP4 and no hosted copy")
        from ..util import cache_dir, download
        mp4 = download(url, cache_dir(load_config(), "queue") / f"{r['id']}.mp4")
    return mp4


def _texts(r: dict, platform: str) -> tuple[str, str]:
    title = r["platforms"].get("youtube", {}).get("title", "")
    return review.final_caption(r, platform, review.load()), title


def publish_day(cfg, day: date, dry_run: bool, rng: random.Random) -> dict:
    tz = ZoneInfo(cfg["timezone"])
    platforms = cfg["publishing"]["platforms"]
    errors: list[str] = []
    chans: dict = {}
    fb_ok = False
    if not dry_run:
        if buffer.configured():
            try:
                chans = buffer.channels()
            except Exception as e:
                errors.append(f"Buffer: {e}")
        if facebook.configured():
            try:
                st = facebook.token_status(cfg)
                need = {"pages_show_list", "pages_read_engagement", "pages_manage_posts"}
                fb_ok = st["valid"] and (not st.get("scopes") or need <= set(st["scopes"]))
                exp = st.get("expires_at")
                if exp and exp - datetime.now().timestamp() < 7 * 86400:
                    notify.send(f"توكن صفحة فيسبوك هينتهي {datetime.fromtimestamp(exp):%Y-%m-%d} — جدده")
            except Exception as e:
                errors.append(f"Facebook token: {e}")
        for pl in platforms:
            if pl == "facebook" and not fb_ok:
                log.info("facebook: not configured / token invalid — skipped")
            elif pl != "facebook" and pl not in chans:
                log.info(f"{pl}: no connected Buffer channel — skipped")
        # only platforms that can actually take a post right now; the rest are retried on the
        # next day's videos, never back-filled onto old ones
        platforms = [pl for pl in platforms if (fb_ok if pl == "facebook" else pl in chans)]

    items = todays_items(cfg, day)
    # only videos with at least one available platform still unscheduled
    items = [(p, r) for p, r in items if any(pl not in (r.get("published") or {}) for pl in platforms)]
    if not items:
        log.info("nothing to publish for %s", day)
        if errors:
            notify.send("مشاكل في النشر:\n" + "\n".join(errors))
        return {"scheduled": 0, "errors": errors}
    rng.shuffle(items)
    # parts of a series keep their order among themselves (times are handed out ascending)
    sidx: dict[str, list[int]] = {}
    for i, (_, r) in enumerate(items):
        if r.get("series"):
            sidx.setdefault(r["series"]["id"], []).append(i)
    for pos in sidx.values():
        ordered = sorted((items[i] for i in pos), key=lambda x: x[1]["series"]["part"])
        for i, it in zip(pos, ordered):
            items[i] = it
    series_off: dict[str, dict] = {}
    series_base: dict[str, datetime] = {}
    try:
        times = scheduler.base_times(cfg, rng, len(items), day)
    except ValueError as e:          # approved too late in the day — no window left
        log.info("%s", e)
        notify.send(f"اتأخر الوقت: {len(items)} فيديو متوافق عليهم النهارده مش هيلحقوا يتنشروا ({e})")
        return {"scheduled": 0, "errors": errors}

    hist_p = path(cfg, "history")
    history = read_json(hist_p, {})
    scheduled = 0
    for (rec_path, r), base in zip(items, times):
        mp4 = rec_path.with_suffix(".mp4")
        if not dry_run:
            try:
                mp4 = video_file(rec_path, r)
            except Exception as e:
                errors.append(str(e))
                continue
        sid = (r.get("series") or {}).get("id")
        if sid and sid in series_base:      # next part follows the previous one closely
            base = series_base[sid] + timedelta(minutes=rng.randint(25, 45), seconds=rng.randint(0, 59))
        if sid:
            series_base[sid] = base
        when = scheduler.platform_times(cfg, rng, base, platforms)
        if sid:     # same per-platform offsets for every part -> order holds on each platform
            off = series_off.setdefault(sid, {pl: when[pl] - base for pl in when})
            when = {pl: base + off[pl] for pl in when}
        pub = r.setdefault("published", {})
        line = f"{r['id']}: " + ", ".join(f"{pl} {when[pl].astimezone(tz):%H:%M}" for pl in platforms
                                         if pl not in pub)
        if dry_run:
            log.info("[dry-run] %s", line)
            continue
        log.info("scheduling %s", line)
        need_host = any(pl != "facebook" and pl in chans and pl not in pub for pl in platforms)
        if need_host and not (r.get("hosting") or {}).get("url"):
            try:
                r["hosting"] = hosting.upload(mp4, r["id"]) | {"deleted": False}
                write_json(rec_path, r)
            except Exception as e:
                errors.append(f"{r['id']} hosting: {e}")
        for pl in platforms:
            if pl in pub:
                continue
            caption, title = _texts(r, pl)
            try:
                if pl == "facebook":
                    if not fb_ok:
                        continue
                    res = facebook.schedule_reel(cfg, mp4, caption, when[pl], title=title)
                    pub[pl] = {"status": "scheduled", "due": when[pl].isoformat(), **res}
                else:
                    if pl not in chans or not (r.get("hosting") or {}).get("url"):
                        continue
                    post = buffer.schedule_video(chans[pl], r["hosting"]["url"], caption, when[pl],
                                                 title=title,
                                                 youtube_category=cfg["publishing"]["youtube_category"])
                    pub[pl] = {"status": post["status"], "due": when[pl].isoformat(), "post_id": post["id"]}
                scheduled += 1
            except Exception as e:
                errors.append(f"{r['id']} {pl}: {e}")
            write_json(rec_path, r)     # save after every platform so a crash never re-posts
        history.setdefault("published", {})[r["id"]] = pub
        write_json(hist_p, history)
    if errors:
        notify.send("مشاكل في النشر:\n" + "\n".join(errors))
        for e in errors:
            log.error(e)
    return {"scheduled": scheduled, "errors": errors}


def publish_one(cfg, video_id: str, platforms: list[str], due: datetime) -> dict:
    """Schedule one video on chosen platforms at one time (tests / manual publishing).
    Respects the same records, so a platform already scheduled for this video is skipped."""
    recs = [p for p in list(OUTPUT.glob(f"*/{video_id}.json")) + list(queue.QUEUE.glob(f"{video_id}.json"))
            if p.parent.name != "visual_test"]
    recs.sort(key=lambda p: p.parent != queue.QUEUE)   # prefer the queue copy when both exist
    if not recs:
        raise SystemExit(f"no video {video_id}")
    rec_path = recs[0]
    r = read_json(rec_path, {})
    if not _ok_to_publish(r):
        raise SystemExit(f"{video_id} is not approved for publishing")
    mp4 = video_file(rec_path, r)
    pub = r.setdefault("published", {})
    out = {}
    chans = buffer.channels() if any(pl != "facebook" for pl in platforms) else {}
    if any(pl != "facebook" and pl not in pub for pl in platforms) and not (r.get("hosting") or {}).get("url"):
        r["hosting"] = hosting.upload(mp4, r["id"]) | {"deleted": False}
        write_json(rec_path, r)
    for pl in platforms:
        if pl in pub:
            out[pl] = "already scheduled — skipped"
            continue
        caption, title = _texts(r, pl)
        if pl == "facebook":
            res = facebook.schedule_reel(cfg, mp4, caption, due, title=title)
            pub[pl] = {"status": "scheduled", "due": due.isoformat(), **res}
        else:
            if pl not in chans:
                out[pl] = "no Buffer channel"
                continue
            post = buffer.schedule_video(chans[pl], r["hosting"]["url"], caption, due, title=title,
                                         youtube_category=cfg["publishing"]["youtube_category"])
            pub[pl] = {"status": post["status"], "due": due.isoformat(), "post_id": post["id"]}
        out[pl] = pub[pl]
        write_json(rec_path, r)
    hist_p = path(cfg, "history")
    history = read_json(hist_p, {})
    history.setdefault("published", {})[r["id"]] = pub
    write_json(hist_p, history)
    return out


def status(cfg, video_id: str) -> None:
    for p in list(OUTPUT.glob(f"*/{video_id}.json")) + list(queue.QUEUE.glob(f"{video_id}.json")):
        r = read_json(p, {})
        for pl, info in (r.get("published") or {}).items():
            if info.get("post_id") and pl != "facebook":
                st = buffer.post_status(info["post_id"])
                info.update(status=st["status"], link=st.get("externalLink"))
                err = (st.get("error") or {}).get("message")
                print(f"{pl}: {st['status']}  due {st['dueAt']}  sent {st.get('sentAt')}  {st.get('externalLink') or ''} {err or ''}")
            else:
                print(f"{pl}: {info.get('status')} due {info.get('due')}")
        write_json(p, r)


def cleanup(cfg) -> None:
    """Refresh Buffer post statuses; delete hosted files once every Buffer post is done (or its
    due time is long past); alert on posts that failed."""
    hours = cfg["publishing"]["delete_hosted_after_hours"]
    now = datetime.now(ZoneInfo(cfg["timezone"]))
    problems = []
    reviews = review.load()
    for p in list(OUTPUT.glob("*/q-*.json")) + list(queue.QUEUE.glob("*.json")):
        r = read_json(p, {})
        pub = r.get("published") or {}
        h0 = r.get("hosting") or {}
        stale = not pub and r.get("date", "9999") < (now - timedelta(days=2)).date().isoformat()
        if h0.get("public_id") and not h0.get("deleted") and (review.is_rejected(r, reviews) or stale):
            try:                                  # rejected / never approved: free the space
                h0["deleted"] = hosting.delete(h0["public_id"])
                write_json(p, r)
                log.info("deleted hosted file of %s (%s)", r["id"], "rejected" if not stale else "expired")
            except Exception as e:
                log.warning("delete %s: %s", r["id"], e)
            continue
        changed = False
        for pl, info in pub.items():
            if pl == "facebook" or info.get("status") in ("sent", "error") or not info.get("post_id"):
                continue
            if datetime.fromisoformat(info["due"]) > now:
                continue
            try:
                st = buffer.post_status(info["post_id"])
            except Exception as e:
                log.warning("status check %s %s: %s", r["id"], pl, e)
                continue
            info["status"] = st["status"]
            info["link"] = st.get("externalLink")
            if st["status"] == "error":
                problems.append(f"{r['id']} {pl}: {(st.get('error') or {}).get('message')}")
            changed = True
        h = r.get("hosting") or {}
        if h.get("public_id") and not h.get("deleted"):
            buf = [i for pl, i in pub.items() if pl != "facebook"]
            done = buf and all(i.get("status") in ("sent", "error") or
                               datetime.fromisoformat(i["due"]) + timedelta(hours=hours * 4) < now
                               for i in buf)
            last_due = max((datetime.fromisoformat(i["due"]) for i in buf), default=None)
            if done and last_due and last_due + timedelta(hours=hours) < now:
                try:
                    h["deleted"] = hosting.delete(h["public_id"])
                    changed = True
                    log.info("deleted hosted file for %s", r["id"])
                except Exception as e:
                    log.warning("delete %s: %s", r["id"], e)
        if changed:
            write_json(p, r)
    if problems:
        notify.send("منشورات فشلت:\n" + "\n".join(problems))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cleanup", action="store_true")
    ap.add_argument("--date", type=date.fromisoformat)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--only", help="publish just this video id (e.g. q-20261028-02)")
    ap.add_argument("--platforms", default="", help="with --only: comma list, default all")
    ap.add_argument("--at", help="with --only: Cairo time HH:MM (today) or minutes from now like +30")
    ap.add_argument("--status", help="show live status for a video id")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    setup_logging(a.verbose)
    cfg = load_config()
    if a.status:
        status(cfg, a.status)
        return
    if a.only:
        tz = ZoneInfo(cfg["timezone"])
        now = datetime.now(tz)
        if not a.at or a.at.startswith("+"):
            due = now + timedelta(minutes=int((a.at or "+30")[1:]))
        else:
            h, m = map(int, a.at.split(":"))
            due = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if due < now + timedelta(minutes=cfg["publishing"]["min_lead_minutes"] - 15):
            raise SystemExit("publish time must be at least ~10 minutes ahead")
        pls = [x for x in a.platforms.split(",") if x] or cfg["publishing"]["platforms"]
        log.info("publishing %s on %s at %s", a.only, pls, due.strftime("%Y-%m-%d %H:%M"))
        for k, v in publish_one(cfg, a.only, pls, due).items():
            log.info("%s: %s", k, v)
        return
    if a.cleanup:
        cleanup(cfg)
        return
    day = a.date or datetime.now(ZoneInfo(cfg["timezone"])).date()
    res = publish_day(cfg, day, a.dry_run, random.Random(a.seed))
    log.info("done: %s", res)


if __name__ == "__main__":
    main()
