"""Public video hosting for Buffer (it has no upload endpoint — it fetches the file from a URL
when the post goes out). Cloudinary free tier, Upload API with HTTP Basic auth.

Docs (checked 2026-10): POST https://api.cloudinary.com/v1_1/<cloud>/video/upload and
.../video/destroy; Basic auth with API key + secret is supported for server-side calls.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)


class HostingError(Exception):
    pass


def _creds():
    # the console's "API environment variable" (cloudinary://KEY:SECRET@CLOUD) wins if present
    url = os.environ.get("CLOUDINARY_URL", "")
    if url.startswith("cloudinary://") and "@" in url and ":" in url:
        cred, cloud = url[len("cloudinary://"):].rsplit("@", 1)
        key, secret = cred.split(":", 1)
        return cloud.strip(), (key.strip(), secret.strip())
    cloud = os.environ.get("CLOUDINARY_CLOUD_NAME")
    key = os.environ.get("CLOUDINARY_API_KEY")
    secret = os.environ.get("CLOUDINARY_API_SECRET")
    if not (cloud and key and secret):
        raise HostingError("CLOUDINARY_CLOUD_NAME / CLOUDINARY_API_KEY / CLOUDINARY_API_SECRET missing in .env")
    return cloud, (key, secret)


def configured() -> bool:
    try:
        _creds()
        return True
    except HostingError:
        return False


MAX_BYTES = 95 * 1000 * 1000      # free plan: 100 MB per video (bigger ones get a 413)


def fit(path: Path) -> Path:
    """The file itself, or (a long shared clip at full quality can pass 100 MB) a copy re-encoded
    at the bit rate that fits, picture size and sound unchanged."""
    if path.stat().st_size <= MAX_BYTES:
        return path
    from ..util import media_duration, run_ffmpeg
    out = path.with_name(path.stem + "_host.mp4")
    kbps = int(MAX_BYTES * 8 / 1000 / max(1.0, media_duration(path)) * 0.9) - 192
    run_ffmpeg(["-y", "-i", str(path), "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{kbps}k",
                "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)], "fit for hosting")
    log.info("%s is %.0f MB: hosting a %d kb/s copy (%.0f MB)", path.name, path.stat().st_size / 1e6, kbps,
             out.stat().st_size / 1e6)
    return out


def upload(path: Path, public_id: str) -> dict:
    """Upload an MP4; returns {"url", "public_id", "bytes"}. Re-uploading the same id overwrites.
    On GitHub (publishing.hosting: github) when set up, else Cloudinary."""
    if _github():
        try:
            return _gh_upload(path, public_id)
        except Exception as e:                   # Cloudinary stays the fallback
            log.warning("GitHub hosting failed (%s) — Cloudinary instead", e)
    return _upload(fit(path), public_id)


# ---------------------------------------------------------------- GitHub release hosting
# Free with no bandwidth cap: each video is an asset of the "media" release of a public repo, and
# the Cloudflare Worker serves it as /v/<id>.mp4 with a video content type (worker/bot.js).
# Cloudinary's free plan has a monthly cap, and Buffer fetches every video 3 times.
GH_API = "https://api.github.com"
GH_TAG = "media"


def _github() -> dict | None:
    from ..util import load_config
    repo = (load_config().get("publishing") or {}).get("media_repo")
    tok = os.environ.get("MEDIA_TOKEN") or os.environ.get("GH_TOKEN")
    worker = (os.environ.get("WORKER_URL") or "").rstrip("/")
    if (load_config().get("publishing") or {}).get("hosting") != "github" or not (repo and tok and worker):
        return None
    return {"repo": repo, "worker": worker,
            "h": {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}}


def github_ready() -> bool:
    return _github() is not None


def on_cloudinary(h: dict | None) -> bool:
    h = h or {}
    return bool(h.get("url")) and not h.get("deleted") and "res.cloudinary.com" in h["url"]


def _gh_release(g: dict) -> dict:
    r = requests.get(f"{GH_API}/repos/{g['repo']}/releases/tags/{GH_TAG}", headers=g["h"], timeout=30)
    if r.status_code == 404:
        new = {"tag_name": GH_TAG, "name": "videos waiting to be posted",
               "body": "Each video is removed once it has been posted."}
        r = requests.post(f"{GH_API}/repos/{g['repo']}/releases", headers=g["h"], timeout=30, json=new)
        if r.status_code == 422 and "empty" in r.text.lower():     # a release needs one commit first
            import base64
            text = "Videos of the ازدد channel waiting to be posted; each is removed once it is out.\n"
            requests.put(f"{GH_API}/repos/{g['repo']}/contents/README.md", headers=g["h"], timeout=30,
                         json={"message": "README", "content": base64.b64encode(text.encode()).decode()}
                         ).raise_for_status()
            r = requests.post(f"{GH_API}/repos/{g['repo']}/releases", headers=g["h"], timeout=30, json=new)
    r.raise_for_status()
    return r.json()


def _gh_asset_ids(g: dict, rel: dict, stem: str) -> list[int]:
    out, page = [], 1
    while True:
        r = requests.get(f"{GH_API}/repos/{g['repo']}/releases/{rel['id']}/assets", headers=g["h"],
                         params={"per_page": 100, "page": page}, timeout=30)
        r.raise_for_status()
        out += [a["id"] for a in r.json() if a["name"] == f"{stem}.mp4" or a["name"].startswith(f"{stem}.v")]
        if len(r.json()) < 100:
            return out
        page += 1


@retry(wait=wait_exponential(min=3, max=40), stop=stop_after_attempt(3), reraise=True)
def _gh_upload(path: Path, public_id: str) -> dict:
    g = _github()
    rel = _gh_release(g)
    stem = public_id.replace("/", "_")
    # a new name on every upload: a re-made video (edited, re-rendered) never comes back as the old
    # one from a cache on the way
    name = f"{stem}.v{int(time.time())}.mp4"
    for aid in _gh_asset_ids(g, rel, stem):          # same id again: replace it
        requests.delete(f"{GH_API}/repos/{g['repo']}/releases/assets/{aid}", headers=g["h"], timeout=30)
    up = rel["upload_url"].split("{")[0]
    with open(path, "rb") as f:
        r = requests.post(up, params={"name": name}, data=f, timeout=900,
                          headers={**g["h"], "Content-Type": "video/mp4",
                                   "Content-Length": str(path.stat().st_size)})
    if r.status_code >= 400:
        raise HostingError(f"GitHub upload {r.status_code}: {r.text[:300]}")
    a = r.json()
    url = f"{g['worker']}/v/{name}"
    log.info("hosted %s -> %s (%.1f MB, GitHub)", path.name, url, a["size"] / 1e6)
    return {"url": url, "public_id": f"gh:{a['id']}", "bytes": a["size"]}


@retry(wait=wait_exponential(min=3, max=40), stop=stop_after_attempt(4), reraise=True)
def _upload(path: Path, public_id: str) -> dict:
    cloud, auth = _creds()
    with open(path, "rb") as f:
        r = requests.post(f"https://api.cloudinary.com/v1_1/{cloud}/video/upload", auth=auth,
                          data={"public_id": public_id, "folder": "quran-shorts", "overwrite": "true"},
                          files={"file": (path.name, f, "video/mp4")}, timeout=600)
    if r.status_code >= 400:
        raise HostingError(f"Cloudinary upload {r.status_code}: {r.text[:300]}")
    d = r.json()
    log.info("hosted %s -> %s (%.1f MB)", path.name, d["secure_url"], d.get("bytes", 0) / 1e6)
    return {"url": d["secure_url"], "public_id": d["public_id"], "bytes": d.get("bytes")}


def url_is_public(url: str) -> bool:
    try:
        r = requests.head(url, timeout=30, allow_redirects=True)
        return r.status_code == 200 and r.headers.get("content-type", "").startswith("video/")
    except requests.RequestException:
        return False


@retry(wait=wait_exponential(min=3, max=40), stop=stop_after_attempt(4), reraise=True)
def delete(public_id: str) -> bool:
    if public_id.startswith("gh:"):
        from ..util import load_config
        repo = (load_config().get("publishing") or {}).get("media_repo")
        tok = os.environ.get("MEDIA_TOKEN") or os.environ.get("GH_TOKEN")
        r = requests.delete(f"{GH_API}/repos/{repo}/releases/assets/{public_id[3:]}", timeout=30,
                            headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
        if r.status_code not in (204, 404):
            raise HostingError(f"GitHub delete {r.status_code}: {r.text[:200]}")
        return True
    cloud, auth = _creds()
    r = requests.post(f"https://api.cloudinary.com/v1_1/{cloud}/video/destroy", auth=auth,
                      data={"public_id": public_id, "invalidate": "true"}, timeout=60)
    if r.status_code >= 400:
        raise HostingError(f"Cloudinary destroy {r.status_code}: {r.text[:300]}")
    return r.json().get("result") in ("ok", "not found")
