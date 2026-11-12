"""Public video hosting for Buffer (it has no upload endpoint — it fetches the file from a URL
when the post goes out). Cloudinary free tier, Upload API with HTTP Basic auth.

Docs (checked 2026-10): POST https://api.cloudinary.com/v1_1/<cloud>/video/upload and
.../video/destroy; Basic auth with API key + secret is supported for server-side calls.
"""
from __future__ import annotations

import logging
import os
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
    """Upload an MP4; returns {"url", "public_id", "bytes"}. Re-uploading the same id overwrites."""
    return _upload(fit(path), public_id)


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
    cloud, auth = _creds()
    r = requests.post(f"https://api.cloudinary.com/v1_1/{cloud}/video/destroy", auth=auth,
                      data={"public_id": public_id, "invalidate": "true"}, timeout=60)
    if r.status_code >= 400:
        raise HostingError(f"Cloudinary destroy {r.status_code}: {r.text[:300]}")
    return r.json().get("result") in ("ok", "not found")
