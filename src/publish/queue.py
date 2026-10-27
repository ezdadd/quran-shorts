"""Publish queue for videos that need a human approval (lectures, locally-aligned recordings).

They are made on the local machine, but publishing runs on GitHub Actions — so approving one
hosts the MP4 on Cloudinary right away and commits its record to queue/<id>.json. The daily run
takes `daily.queue_per_day` items from the queue (oldest first).

    python -m src.publish.queue add <id>       # (panel "approve" does this)
    python -m src.publish.queue remove <id>    # (panel "reject" does this, if not yet published)
    python -m src.publish.queue list
"""
from __future__ import annotations

import argparse
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..util import ROOT, read_json, setup_logging, load_config, write_json
from . import hosting

log = logging.getLogger("quran-shorts.queue")
QUEUE = ROOT / "queue"
OUTPUT = ROOT / "output"


def _find(video_id: str) -> Path | None:
    hits = [p for p in OUTPUT.glob(f"*/{video_id}.json") if p.parent.name != "visual_test"]
    return hits[0] if hits else None


def items() -> list[tuple[Path, dict]]:
    QUEUE.mkdir(exist_ok=True)
    out = [(p, read_json(p, {})) for p in QUEUE.glob("*.json")]
    out.sort(key=lambda x: x[1].get("queued_at", ""))
    return out


def _git(*args: str) -> bool:
    p = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if p.returncode != 0:
        log.warning("git %s: %s", " ".join(args), (p.stderr or p.stdout).strip()[:300])
    return p.returncode == 0


def sync(message: str, paths: list[Path]) -> None:
    """Commit queue changes and push, so the next daily run sees them (no-op without a remote)."""
    if not _git("remote", "get-url", "origin"):
        log.info("no git remote yet — queue change saved locally only")
        return
    rel = [str(p.relative_to(ROOT)) for p in paths]
    _git("add", "--all", "--", *rel)
    _git("commit", "-m", message, "--", *rel)
    if _git("pull", "--rebase", "--autostash") and _git("push"):
        log.info("queue pushed to GitHub")
    else:
        log.warning("could not push the queue — run `git pull --rebase && git push` later")


def add(video_id: str) -> Path:
    src = _find(video_id)
    if not src:
        raise SystemExit(f"no video {video_id}")
    r = read_json(src, {})
    if not r.get("approved") or r.get("rejected"):
        raise SystemExit(f"{video_id} is not approved")
    mp4 = src.with_suffix(".mp4")
    if not (r.get("hosting") or {}).get("url") or (r.get("hosting") or {}).get("deleted"):
        r["hosting"] = hosting.upload(mp4, r["id"]) | {"deleted": False}
        write_json(src, r)
    q = QUEUE / f"{video_id}.json"
    QUEUE.mkdir(exist_ok=True)
    rec = {**r, "queued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "local_record": str(src.relative_to(ROOT)).replace("\\", "/")}
    write_json(q, rec)
    log.info("queued %s (%s)", video_id, rec["hosting"]["url"])
    sync(f"queue: add {video_id}", [q])
    return q


def remove(video_id: str) -> None:
    q = QUEUE / f"{video_id}.json"
    r = read_json(q, None)
    if r is None:
        log.info("%s is not in the queue", video_id)
        return
    if r.get("published"):
        raise SystemExit(f"{video_id} was already scheduled on {list(r['published'])} — not removed")
    h = r.get("hosting") or {}
    if h.get("public_id") and not h.get("deleted"):
        try:
            hosting.delete(h["public_id"])
        except Exception as e:
            log.warning("could not delete hosted file: %s", e)
    q.unlink()
    log.info("removed %s from the queue", video_id)
    sync(f"queue: remove {video_id}", [q])


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("add", "remove"):
        sub.add_parser(c).add_argument("id")
    sub.add_parser("list")
    a = ap.parse_args(argv)
    setup_logging()
    load_config()
    if a.cmd == "add":
        add(a.id)
    elif a.cmd == "remove":
        remove(a.id)
    else:
        for p, r in items():
            pub = r.get("published") or {}
            print(f"{r['id']}  queued {r.get('queued_at')}  scheduled on: {', '.join(pub) or '—'}")


if __name__ == "__main__":
    main()
