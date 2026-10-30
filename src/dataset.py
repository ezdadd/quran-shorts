"""Training data for our own speech model (a Whisper fine-tune, later on a free GPU).

Every published clip / lecture is kept, just before its hosted copy is deleted: the audio (16 kHz
mono Opus, ~135 KB a clip) and each subtitle line with its time and FINAL text — after your edits
and the corrector's. Quran lines are kept with their verified text. Each clip is labelled:
  human — you approved or corrected it yourself (the reliable part, trained on first)
  agent — the corrector approved it while you didn't act
Stored in the private repo <owner>/quran-shorts-dataset (tools/dataset.sh checks it out sparsely
into _dataset/: only index.json, so the hourly job doesn't download the audio).
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone

from .util import ROOT, cache_dir, download, read_json, run_ffmpeg, write_json

log = logging.getLogger("quran-shorts.dataset")
DIR = ROOT / "_dataset"


def label(r: dict, reviews: dict) -> str | None:
    from .review import store
    e = store.get(r["id"], reviews)
    if r.get("edited_at") or (e.get("status") == "approved" and e.get("by") != "auto"):
        return "human"
    if e.get("status") == "approved" or (r.get("auto") or {}).get("status") == "approved":
        return "agent"
    return None


def lines(r: dict) -> list[dict]:
    out = []
    for s in r.get("segments") or []:
        words = s.get("words") or []
        if s.get("type") == "speech":
            text = (s.get("text") or " ".join(w["w"] for w in words)).strip()
        elif s.get("type") == "quran":
            text = " ".join(words) if words and isinstance(words[0], str) else ""
        else:
            continue
        if text and s.get("end", 0) > s.get("start", 0):
            out.append({"start": round(s["start"], 2), "end": round(s["end"], 2), "text": text,
                        "type": s["type"]})
    return out


def collect(cfg, r: dict, reviews: dict) -> bool:
    """Keep this clip's audio + final text. False when it doesn't qualify or is already kept."""
    if not DIR.exists() or r.get("type") != "lecture":
        return False
    idx_p = DIR / "index.json"
    idx = read_json(idx_p, {})
    if r["id"] in idx:
        return False
    lab, ls = label(r, reviews), lines(r)
    url = (r.get("hosting") or {}).get("url")
    if not lab or not ls or not url:
        return False
    work = cache_dir(cfg, "dataset", r["id"])
    mp4 = download(url, work / "v.mp4")
    out = DIR / "clips" / f"{r['id']}.opus"
    out.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-i", str(mp4), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k", str(out)],
               "dataset audio")
    meta = {"id": r["id"], "label": lab, "sheikh": r.get("sheikh_name") or "", "duration": r.get("duration"),
            "source": (r.get("source") or {}).get("url"), "lines": ls,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    write_json(DIR / "clips" / f"{r['id']}.json", meta)
    idx[r["id"]] = {"label": lab, "seconds": round(sum(x["end"] - x["start"] for x in ls), 1)}
    write_json(idx_p, idx)
    shutil.rmtree(work, ignore_errors=True)
    log.info("dataset: kept %s (%s, %d lines)", r["id"], lab, len(ls))
    return True


def summary() -> str:
    idx = read_json(DIR / "index.json", {})
    hrs = {k: sum(v["seconds"] for v in idx.values() if v["label"] == k) / 3600 for k in ("human", "agent")}
    return f"{len(idx)} clips — human {hrs['human']:.2f} h, agent {hrs['agent']:.2f} h"
