"""Facebook Page Reels via the Meta Graph API (direct upload, scheduled publishing).

Docs (checked 2026-10): Reels Publishing API —
  1. POST graph.facebook.com/<ver>/<page-id>/video_reels  upload_phase=start      -> video_id
  2. POST rupload.facebook.com/video-upload/<ver>/<video-id>  (Authorization: OAuth <token>,
     offset: 0, file_size: <bytes>, binary body)
  3. POST /<page-id>/video_reels  upload_phase=finish, video_state=SCHEDULED,
     scheduled_publish_time=<unix>, description, title
Needs a Page access token with pages_show_list, pages_read_engagement, pages_manage_posts.
Limit: 30 API-published reels per 24 h. Specs: 9:16, 3–90 s, H.264, AAC 48 kHz.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)


class FacebookError(Exception):
    pass


def configured() -> bool:
    return bool(os.environ.get("FB_PAGE_ID") and os.environ.get("FB_PAGE_TOKEN"))


def _ver(cfg) -> str:
    return cfg["publishing"].get("facebook_api_version", "v26.0")


def _check(r: requests.Response, what: str) -> dict:
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400 or "error" in body:
        err = body.get("error", {})
        raise FacebookError(f"{what}: {err.get('message') or r.text[:300]} (code {err.get('code')})")
    return body


def token_status(cfg) -> dict:
    """{"valid": bool, "expires_at": unix|0 (0 = never), "page": name} — uses debug_token when the
    app id/secret are available, otherwise just checks the token can read the page."""
    page, token = os.environ.get("FB_PAGE_ID"), os.environ.get("FB_PAGE_TOKEN")
    g = f"https://graph.facebook.com/{_ver(cfg)}"
    info = _check(requests.get(f"{g}/{page}", params={"fields": "name", "access_token": token}, timeout=30),
                  "page lookup")
    out = {"valid": True, "expires_at": None, "page": info.get("name")}
    app_id, app_secret = os.environ.get("FB_APP_ID"), os.environ.get("FB_APP_SECRET")
    if app_id and app_secret:
        d = _check(requests.get(f"{g}/debug_token", params={"input_token": token,
                                                              "access_token": f"{app_id}|{app_secret}"},
                                timeout=30), "debug_token")["data"]
        out["type"] = d.get("type")            # must be PAGE for publishing
        out["valid"] = bool(d.get("is_valid")) and d.get("type") == "PAGE"
        out["expires_at"] = d.get("expires_at", 0)
        out["scopes"] = d.get("scopes", [])
    return out


REEL_MAX = 90.0          # Reels API limit; a longer shared clip goes out as a regular Page video


@retry(wait=wait_exponential(min=5, max=60), stop=stop_after_attempt(3), reraise=True)
def schedule_video(cfg, path: Path, description: str, due: datetime, title: str = "") -> dict:
    """POST /<page-id>/videos (multipart), published=false + scheduled_publish_time."""
    page, token = os.environ["FB_PAGE_ID"], os.environ["FB_PAGE_TOKEN"]
    data = {"access_token": token, "description": description, "published": "false",
            "scheduled_publish_time": str(int(due.timestamp()))}
    if title:
        data["title"] = title[:100]
    with open(path, "rb") as f:
        res = _check(requests.post(f"https://graph-video.facebook.com/{_ver(cfg)}/{page}/videos", data=data,
                                   files={"source": (path.name, f, "video/mp4")}, timeout=900), "video upload")
    log.info("facebook video %s scheduled for %s (longer than a Reel)", res.get("id"), due.isoformat())
    return {"video_id": res.get("id"), "post_id": None, "scheduled_unix": int(due.timestamp()),
            "uploaded_at": int(time.time()), "as": "video"}


@retry(wait=wait_exponential(min=5, max=60), stop=stop_after_attempt(3), reraise=True)
def schedule_reel(cfg, path: Path, description: str, due: datetime, title: str = "") -> dict:
    from ..util import media_duration
    if media_duration(path) > REEL_MAX:
        return schedule_video(cfg, path, description, due, title)
    page, token = os.environ["FB_PAGE_ID"], os.environ["FB_PAGE_TOKEN"]
    g = f"https://graph.facebook.com/{_ver(cfg)}"
    start = _check(requests.post(f"{g}/{page}/video_reels",
                                 data={"upload_phase": "start", "access_token": token}, timeout=60),
                   "reels start")
    vid = start["video_id"]
    size = path.stat().st_size
    with open(path, "rb") as f:
        _check(requests.post(f"https://rupload.facebook.com/video-upload/{_ver(cfg)}/{vid}",
                             headers={"Authorization": f"OAuth {token}", "offset": "0",
                                      "file_size": str(size)}, data=f, timeout=900), "reels upload")
    params = {"upload_phase": "finish", "video_id": vid, "video_state": "SCHEDULED",
              "scheduled_publish_time": str(int(due.timestamp())), "description": description,
              "access_token": token}
    if title:
        params["title"] = title[:100]
    fin = _check(requests.post(f"{g}/{page}/video_reels", data=params, timeout=120), "reels finish")
    if not fin.get("success", True):
        raise FacebookError(f"reels finish: {fin}")
    log.info("facebook reel %s scheduled for %s", vid, due.isoformat())
    return {"video_id": vid, "post_id": fin.get("post_id"), "scheduled_unix": int(due.timestamp()),
            "uploaded_at": int(time.time())}
