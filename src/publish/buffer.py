"""Buffer GraphQL API — YouTube Shorts, TikTok and Instagram Reels (free plan: 3 channels).

Docs (checked 2026-10, developers.buffer.com): POST https://api.buffer.com with
`Authorization: Bearer <BUFFER_API_KEY>`; createPost(input: CreatePostInput) with
mode=customScheduled + dueAt (UTC ISO 8601); video via assets[{video:{url}}] — a public URL that
must stay reachable until the post publishes; Post.status in draft|error|needs_approval|
scheduled|sending|sent.

Request limits (read from its `ratelimit` headers, 2026-11): 150 per 15 min, 600 per 24 h and
3,000 per 30 days for the whole key. The 30-day one is the one that bites: every request we make
is counted here (data/buffer_limits.json), checks and stats only use what scheduling posts won't
need (spare()), and once Buffer says "no more" nothing calls it again until its reset time.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..util import ROOT, read_json, write_json

log = logging.getLogger(__name__)
API = "https://api.buffer.com"
SERVICES = ("youtube", "tiktok", "instagram")
LIMITS = ROOT / "data" / "buffer_limits.json"
# requests a day kept for what must happen: ~10 videos x 3 channels scheduled + one status check each
DAILY_ESSENTIAL = 65
CHANNELS_TTL = 24 * 3600


class BufferError(Exception):
    pass


class BufferLimited(BufferError):
    """Buffer's request limit is used up: no call is made until it resets."""


class _Retryable(Exception):
    pass


def configured() -> bool:
    return bool(os.environ.get("BUFFER_API_KEY"))


def _windows(headers) -> dict:
    # one `ratelimit: "30d"; r=<left>; t=<seconds to reset>` per window (requests joins them with ", ")
    now = time.time()
    return {m.group(1): {"left": int(m.group(2)), "reset": int(now + int(m.group(3)))}
            for m in re.finditer(r'"(\w+)";\s*r=(\d+);\s*t=(\d+)', headers.get("ratelimit", ""))}


def blocked_until() -> float:
    """Unix time Buffer accepts requests again (0 = it does now)."""
    t = float(read_json(LIMITS, {}).get("blocked_until") or 0)
    return t if t > time.time() else 0.0


def spare(n: int = 1) -> bool:
    """True if `n` more requests for checks / stats still leave enough for scheduling posts until
    each window resets."""
    if blocked_until():
        return False
    now = time.time()
    w = read_json(LIMITS, {}).get("windows") or {}
    m, d = w.get("30d"), w.get("24h")
    if m and m["reset"] > now and m["left"] - n < DAILY_ESSENTIAL * (m["reset"] - now) / 86400:
        return False
    if d and d["reset"] > now and d["left"] - n < DAILY_ESSENTIAL:
        return False
    return True


def _limited(r) -> BufferLimited:
    try:
        window = (r.json().get("errors") or [{}])[0].get("extensions", {}).get("window", "")
    except ValueError:
        window = ""
    wait = int(r.headers.get("Retry-After") or 0) or (900 if window == "15m" else 86400)
    until = time.time() + wait
    lim = read_json(LIMITS, {})
    first = float(lim.get("blocked_until") or 0) < time.time()
    lim.update(blocked_until=until, blocked_window=window, windows=_windows(r.headers) or lim.get("windows", {}))
    write_json(LIMITS, lim)
    when = datetime.fromtimestamp(until, timezone.utc).astimezone()
    if first and wait > 3600:                     # tell you once, not on every run
        from . import notify
        notify.send(f"Buffer: حصة الطلبات ({window or '?'}) خلصت — يوتيوب/تيك توك/إنستجرام مش هيتنشر عليهم لحد "
                    f"{when:%Y-%m-%d %H:%M} (فيسبوك شغال عادي). البوت مش هيكلم Buffer خالص لحد الوقت ده.")
    return BufferLimited(f"Buffer request limit ({window or '?'}) used up until {when:%Y-%m-%d %H:%M}")


@retry(retry=retry_if_exception_type((_Retryable, requests.ConnectionError, requests.Timeout)),
       wait=wait_exponential(min=3, max=60), stop=stop_after_attempt(4), reraise=True)
def gql(query: str, variables: dict | None = None) -> dict:
    key = os.environ.get("BUFFER_API_KEY")
    if not key:
        raise BufferError("BUFFER_API_KEY missing in .env")
    if t := blocked_until():
        raise BufferLimited(f"Buffer request limit used up until "
                            f"{datetime.fromtimestamp(t, timezone.utc).astimezone():%Y-%m-%d %H:%M}")
    r = requests.post(API, json={"query": query, "variables": variables or {}},
                      headers={"Authorization": f"Bearer {key}"}, timeout=60)
    if w := _windows(r.headers):
        lim = read_json(LIMITS, {})
        lim["windows"] = w
        write_json(LIMITS, lim)
    if r.status_code == 429:
        # a retry is a request too (and counts): only the short 15-minute window is worth waiting out
        if int(r.headers.get("Retry-After") or 0) <= 60:
            raise _Retryable("Buffer 429")
        raise _limited(r)
    if r.status_code >= 500:
        raise _Retryable(f"Buffer {r.status_code}")
    if r.status_code >= 400:
        raise BufferError(f"Buffer {r.status_code}: {r.text[:300]}")
    body = r.json()
    if body.get("errors"):
        if any((e.get("extensions") or {}).get("code") == "RATE_LIMIT_EXCEEDED" for e in body["errors"]):
            raise _limited(r)
        raise BufferError("; ".join(e.get("message", "?") for e in body["errors"]))
    return body["data"]


def channels() -> dict[str, dict]:
    """service -> channel (first connected, unlocked channel of each service we use). Kept a day:
    every publishing run needs it, and it costs two requests."""
    lim = read_json(LIMITS, {})
    c = lim.get("channels") or {}
    if c.get("at", 0) + CHANNELS_TTL > time.time() and c.get("list"):
        return c["list"]
    out = _channels()
    lim = read_json(LIMITS, {})
    lim["channels"] = {"at": int(time.time()), "list": out}
    write_json(LIMITS, lim)
    return out


def forget_channels() -> None:
    """After a post fails because of its channel (disconnected / locked): ask Buffer again next time."""
    lim = read_json(LIMITS, {})
    if lim.pop("channels", None) is not None:
        write_json(LIMITS, lim)


def _channels() -> dict[str, dict]:
    orgs = gql("query { account { organizations { id name } } }")["account"]["organizations"]
    out: dict[str, dict] = {}
    for org in orgs:
        chs = gql("""query($o: OrganizationId!) { channels(input: {organizationId: $o}) {
                       id name displayName service isDisconnected isLocked isQueuePaused } }""",
                  {"o": org["id"]})["channels"]
        for c in chs:
            if c["service"] in SERVICES and c["service"] not in out \
                    and not c["isDisconnected"] and not c["isLocked"]:
                out[c["service"]] = {**c, "organizationId": org["id"]}
    return out


CREATE = """mutation($input: CreatePostInput!) { createPost(input: $input) {
  ... on PostActionSuccess { post { id dueAt status } }
  ... on MutationError { message } } }"""


def schedule_video(channel: dict, video_url: str, text: str, due: datetime, *, title: str = "",
                   youtube_category: str = "27") -> dict:
    service = channel["service"]
    meta: dict = {}
    if service == "youtube":
        meta["youtube"] = {"title": (title or text.split("\n")[0])[:100], "categoryId": youtube_category,
                           "privacy": "public", "madeForKids": False, "notifySubscribers": True,
                           "isAiGenerated": False}
    elif service == "instagram":
        meta["instagram"] = {"type": "reel", "shouldShareToFeed": True, "isAiGenerated": False}
    elif service == "tiktok":
        meta["tiktok"] = {"isAiGenerated": False}
    inp = {
        "channelId": channel["id"],
        "text": text,
        "schedulingType": "automatic",
        "mode": "customScheduled",
        "dueAt": due.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "assets": [{"video": {"url": video_url}}],
        "metadata": meta,
    }
    res = gql(CREATE, {"input": inp})["createPost"]
    if "post" not in res:
        forget_channels()                       # a channel that went away: look them up again next time
        raise BufferError(f"{service}: {res.get('message', 'createPost failed')}")
    log.info("buffer %s scheduled %s at %s", service, res["post"]["id"], res["post"]["dueAt"])
    return res["post"]


def post_status(post_id: str) -> dict:
    return gql("""query($id: PostId!) { post(input: {id: $id}) {
                   id status dueAt sentAt externalLink error { message } } }""", {"id": post_id})["post"]
