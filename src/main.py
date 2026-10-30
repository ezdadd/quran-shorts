"""Daily entry point.

    python -m src.main --generate 3 --dry-run
"""
from __future__ import annotations

import argparse
import logging
import random
import shutil
from datetime import date, timedelta

from . import ambience, footage, qc, quran, templates
from .align import AlignError
from .publish import meta
from .render import TooLong, compose
from .textrender import TextRenderer
from .util import ROOT, load_config, path, read_json, setup_logging, write_json

log = logging.getLogger("quran-shorts.main")
MAX_FAILURES = 4        # replacement attempts per run before giving up (and alerting)


def load_passages(cfg) -> list[dict]:
    items = read_json(path(cfg, "passages"), {"passages": []})["passages"]
    out = []
    for p in items:
        if p["from"] == 1 and p["surah"] != 1:
            # ayah 1 audio may or may not include the basmala depending on the reciter;
            # the text never does, so these are skipped to keep text == recitation
            log.warning("skipping %s:%s — passages must not start at ayah 1", p["surah"], p["from"])
            continue
        out.append(p)
    return out


def pick_reciter(cfg, rng: random.Random) -> str:
    ids = [r for r in cfg["reciters"] if r not in cfg.get("excluded_reciters", [])]
    return rng.choices(ids, weights=[cfg["reciters"][r].get("weight", 1) for r in ids])[0]


def generate_quran(cfg, n: int, run_date: date, rng: random.Random, history: dict,
                   dry_run: bool, force_category: str | None = None,
                   force_reciter: str | None = None, force_passage: str | None = None,
                   theme: str | None = None) -> list[dict]:
    out_dir = ROOT / cfg["paths"]["output"] / run_date.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    cutoff = (run_date - timedelta(days=cfg["quran"]["passage_cooldown_days"])).isoformat()
    recent = {k for k, d in history.setdefault("passages_used", {}).items() if d >= cutoff}
    passages = [p for p in load_passages(cfg)
                if f"{p['surah']}:{p['from']}-{p['to']}" not in recent]
    # weighted random order (themes in quran.theme_weights come up more often); list is popped
    # from the end, so the most favoured keys sort last
    tw = cfg["quran"].get("theme_weights") or {}
    passages.sort(key=lambda p: rng.random() ** (1.0 / max(0.01, float(tw.get(p.get("theme"), 1)))))
    if theme:                   # chosen from the /generate menu in Telegram
        passages = [p for p in passages if p.get("theme") == theme]
        if not passages:
            log.error("no unused passages left for theme %s", theme)
    if force_passage:
        s, rngs = force_passage.split(":")
        a, _, b = rngs.partition("-")
        passages = [{"surah": int(s), "from": int(a), "to": int(b or a), "theme": None}]
    used_tpl = set(history.setdefault("templates_by_day", {}).get(run_date.isoformat(), []))
    used_cats: set[str] = set()
    results = []
    failures: list[str] = []

    with TextRenderer(cfg) as tr:
        while len(results) < n and passages:
            p = passages.pop()
            key = f"{p['surah']}:{p['from']}-{p['to']}"
            reciter = force_reciter or pick_reciter(cfg, rng)
            rcfg = cfg["reciters"][reciter]
            if rcfg.get("source") == "mp3quran_aligned" and not force_passage and                     quran.chapters(cfg)[p["surah"]]["verses_count"] > rcfg.get("align_max_ayat", 130):
                continue   # whole-surah recognition of very long surahs takes hours on CPU
            try:
                rec = quran.load_recitation(cfg, reciter, p["surah"], p["from"], p["to"])
            except AlignError as e:
                log.warning("skip %s (%s): %s", key, reciter, e)
                continue
            except quran.TextMismatch as e:
                log.error("REJECTED %s: %s", key, e)
                history.setdefault("rejected", {})[key] = str(e)
                continue
            except Exception as e:      # audio / API hiccup: take another passage instead of stopping
                failures.append(f"{key} ({reciter}): {type(e).__name__}: {str(e)[:120]}")
                log.error("skip %s (%s) after error: %s", key, reciter, e)
                if len(failures) >= MAX_FAILURES:
                    break
                continue
            approx = cfg["video"]["lead_in"] + rec.duration + cfg["video"]["tail"]
            if approx > cfg["video"]["max_duration"] - 0.3:
                log.info("skip %s (%s): %.1fs too long", key, reciter, approx)
                continue

            idx = len(results) + 1
            while (out_dir / f"q-{run_date:%Y%m%d}-{idx:02d}.json").exists():
                idx += 1   # never overwrite an earlier video from the same day
            vid = f"q-{run_date:%Y%m%d}-{idx:02d}"
            tpl = templates.choose(cfg, rng, used_tpl, rec.has_word_times)
            if failures:                # after a crash: no camera move / extra effects this time
                tpl = templates.safe(tpl)
            category = force_category or footage.pick_category(cfg, rng, avoid=used_cats)
            clips = footage.get_clips(cfg, category, approx, history, rng)
            amb = ambience.pick(cfg, category, rng)
            log.info("[%s] %s %s | %s | footage=%s (%d clip) | template=%s",
                     vid, key, reciter, rec.surah_name, category, len(clips), tpl.signature())
            work = ROOT / cfg["paths"]["cache"] / "work" / vid
            mp4 = out_dir / f"{vid}.mp4"
            try:
                info = compose(cfg, rng, tr, rec, clips, amb, tpl, mp4, work)
                qc.ensure(mp4, info["duration"])   # broken render -> the replacement path below
            except TooLong as e:
                log.info("skip: %s", e)
                continue
            except Exception as e:      # broken render: drop it and make another one right away
                mp4.unlink(missing_ok=True)
                first = (str(e).strip().splitlines() or [""])[0][:120]
                failures.append(f"{vid} {key}: {type(e).__name__} {first}")
                log.error("[%s] render failed, making another video instead: %s", vid, e)
                if len(failures) >= MAX_FAILURES:
                    break
                continue
            finally:
                shutil.rmtree(work, ignore_errors=True)

            credits = sorted({c.credit for c in clips if c.credit})
            record = {
                "id": vid,
                "type": "quran",
                "date": run_date.isoformat(),
                "file": mp4.name,
                "duration": info["duration"],
                "reference": {"surah": rec.surah, "surah_name": rec.surah_name,
                              "from": rec.ayat[0].ayah, "to": rec.ayat[-1].ayah, "theme": p.get("theme")},
                "ayat": [{"key": a.key, "text": a.text, "duration": round(a.duration, 3),
                          "word_timing": bool(a.word_times)} for a in rec.ayat],
                "text_sources": ["api.quran.com v4 (text_uthmani, words)",
                                 "api.alquran.cloud quran-uthmani (letter cross-check)"],
                "reciter": {"id": reciter, "name": rec.reciter_name,
                            "audio": (rcfg["mp3quran_server"] + " (aligned locally)") if rcfg.get("source") == "mp3quran_aligned" else f"everyayah.com/{rcfg['everyayah_folder']}"},
                "footage": [{"id": c.id, "category": c.category, "credit": c.credit, "url": c.page_url}
                            for c in clips],
                "ambience": amb.name if amb else None,
                "template": tpl.to_dict() | {"applied_color": info["color"],
                                             "applied_background": info["background"],
                                             "text_zone_luma": info["luma"],
                                             "ambience_db": info["ambience_db"],
                                             "ambience_offset": info["ambience_offset"]},
                "platforms": meta.quran_meta(cfg, rng, rec, credits),
                "published": {},
                # recordings aligned locally (no published timings) must be approved by a human
                "review_required": bool(rcfg.get("requires_review")),
                "approved": not rcfg.get("requires_review") and not cfg["publishing"].get("require_approval", True),
            }
            write_json(out_dir / f"{vid}.json", record)
            results.append(record)
            used_tpl.add(tpl.signature())
            used_cats.add(category)
            history["passages_used"][key] = run_date.isoformat()
            for c in clips:
                history.setdefault("footage_used", {})[c.id] = run_date.isoformat()
            history["templates_by_day"][run_date.isoformat()] = sorted(used_tpl)
            log.info("[%s] done: %s (%.1fs)", vid, mp4, info["duration"])
    if failures:
        from .publish import notify
        notify.send(f"🔧 {len(failures)} فيديو حصل فيهم عطل أثناء التجهيز واتلغوا، واتعمل بدالهم "
                    f"({len(results)}/{n} جاهزين):\n" + "\n".join(failures[:5]))
    if len(results) < n:
        log.error("only %d/%d videos generated (passage pool exhausted or %d failures)", len(results), n,
                  len(failures))
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate (and later publish) daily Quran shorts")
    ap.add_argument("--generate", type=int, help="number of Quran videos (default: config daily.quran_count)")
    ap.add_argument("--dry-run", action="store_true", help="generate only; never publish or save history")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--category", help="force one footage category (testing)")
    ap.add_argument("--reciter", help="force one reciter id from config (testing)")
    ap.add_argument("--passage", help="force one passage, e.g. 93:3-5 (testing)")
    ap.add_argument("--theme", help="only passages of this theme (quran.theme_weights key)")
    ap.add_argument("--no-publish", action="store_true", help="generate only, schedule nothing")
    ap.add_argument("--date", type=date.fromisoformat, default=None, help="default: today in Cairo")
    ap.add_argument("--top-up", action="store_true",
                    help="only generate what is missing for the day (safe to re-run)")
    ap.add_argument("--max-new", type=int, help="with --top-up: make at most this many in this run")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    cfg = load_config()
    rng = random.Random(args.seed)
    hist_path = path(cfg, "history")
    history = read_json(hist_path, {})
    if args.date is None:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        args.date = datetime.now(ZoneInfo(cfg["timezone"])).date()
    n = args.generate if args.generate is not None else cfg["daily"]["quran_count"]
    if args.top_up:
        from .review import store as review
        revs = review.load()
        have = sum(1 for p in (ROOT / cfg["paths"]["output"] / args.date.isoformat()).glob("q-*.json")
                   if not review.is_rejected(read_json(p, {}), revs))
        n = max(0, n - have)
        if args.max_new is not None:    # spread over the day: a few per run (every 3 hours)
            n = min(n, args.max_new)
        log.info("top-up %s: %d usable already, generating %d", args.date, have, n)

    results = generate_quran(cfg, n, args.date, rng, history, args.dry_run, args.category,
                             args.reciter, args.passage, args.theme or None)
    if args.passage and not results and not args.dry_run:
        from .publish import notify     # a passage you picked in /generate could not be made
        notify.send(f"⚠️ الآيات {args.passage} متعملتش: غالبًا أطول من دقيقة بالتلاوة دي. "
                    "اختار آيات أقل أو شيخ تاني من /generate.")
    if args.dry_run:
        log.info("dry-run: %d videos in output/%s — nothing published, history not saved",
                 len(results), args.date.isoformat())
        if not args.no_publish:
            from .publish.runner import publish_day
            publish_day(cfg, args.date, True, rng)      # show the schedule it would use
        return
    write_json(hist_path, history)
    if args.no_publish:
        log.info("--no-publish: videos generated and history saved")
        return
    from .publish import runner
    runner.cleanup(cfg)
    res = runner.publish_day(cfg, args.date, False, rng)
    log.info("published: %s", res)


if __name__ == "__main__":
    main()
