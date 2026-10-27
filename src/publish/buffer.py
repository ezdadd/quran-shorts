"""Buffer GraphQL API — YouTube Shorts, TikTok and Instagram Reels (free plan: 3 channels).

Docs (checked 2026-10, developers.buffer.com): POST https://api.buffer.com with
`Authorization: Bearer <BUFFER_API_KEY>`; createPost(input: CreatePostInput) with
mode=customScheduled + dueAt (UTC ISO 8601); video via assets[{video:{url}}] — a public URL that
must stay reachable until the post publishes; Post.status in draft|error|needs_approval|
scheduled|sending|sent. Free plan: 10 scheduled posts per channel at a time, 1,000 requests per
channel per 30 days.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)
API = "https://api.buffer.com"
SERVICES = ("youtube", "tiktok", "instagram")


class BufferError(Exception):
    pass


class _Retryable(Exception):
    pass


def configured() -> bool:
    return bool(os.environ.get("BUFFER_API_KEY"))


@retry(retry=retry_if_exception_type((_Retryable, requests.ConnectionError, requests.Timeout)),
       wait=wait_exponential(min=3, max=60), stop=stop_after_attempt(5), reraise=True)
def gql(query: str, variables: dict | None = None) -> dict:
    key = os.environ.get("BUFFER_API_KEY")
    if not key:
        raise BufferError("BUFFER_API_KEY missing in .env")
    r = requests.post(API, json={"query": query, "variables": variables or {}},
                      headers={"Authorization": f"Bearer {key}"}, timeout=60)
    if r.status_code == 429 or r.status_code >= 500:
        raise _Retryable(f"Buffer {r.status_code}")
    if r.status_code >= 400:
        raise BufferError(f"Buffer {r.status_code}: {r.text[:300]}")
    body = r.json()
    if body.get("errors"):
        raise BufferError("; ".join(e.get("message", "?") for e in body["errors"]))
    return body["data"]


def channels() -> dict[str, dict]:
    """service -> channel (first connected, unlocked channel of each service we use)."""
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
        raise BufferError(f"{service}: {res.get('message', 'createPost failed')}")
    log.info("buffer %s scheduled %s at %s", service, res["post"]["id"], res["post"]["dueAt"])
    return res["post"]


def post_status(post_id: str) -> dict:
    return gql("""query($id: PostId!) { post(input: {id: $id}) {
                   id status dueAt sentAt externalLink error { message } } }""", {"id": post_id})["post"]
