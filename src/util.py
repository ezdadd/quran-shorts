"""Shared helpers: config, logging, HTTP with retry, FFmpeg discovery, JSON state."""
from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("quran-shorts")

# Keep every downloaded runtime asset inside the project folder (not %LOCALAPPDATA% / ~/.cache),
# so the panel, scheduled runs and manual runs all see the same files.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".browsers"))
os.environ.setdefault("HF_HOME", str(ROOT / ".cache" / "hf"))


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def load_config() -> dict:
    load_dotenv(ROOT / ".env")
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    _apply_tuning(cfg)
    return cfg


def _apply_tuning(cfg: dict) -> None:
    """Multipliers learned from views (src/stats.py -> data/tuning.json) on top of config.yaml's
    weights. config.yaml stays exactly as you wrote it; delete data/tuning.json to undo."""
    p = ROOT / "data" / "tuning.json"
    if not p.exists():
        return
    try:
        t = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return
    tw = (cfg.get("quran") or {}).get("theme_weights") or {}
    for k, m in (t.get("theme_weights") or {}).items():
        if k in tw:
            tw[k] = float(tw[k]) * float(m)
    cw = (cfg.get("footage") or {}).get("category_weights") or {}
    for k, m in (t.get("category_weights") or {}).items():
        if k in cw:
            cw[k] = float(cw[k]) * float(m)
    for k, m in (t.get("reciter_weights") or {}).items():
        r = (cfg.get("reciters") or {}).get(k)
        if r and float(r.get("weight", 1)) > 0:        # a disabled reciter stays disabled
            r["weight"] = float(r.get("weight", 1)) * float(m)
    ww = t.get("window_weights") or {}
    for key in ("windows", "friday_windows"):
        out = []
        for w in (cfg.get("publishing") or {}).get(key) or []:
            span, _, wt = w.partition(" x")
            m = float(ww.get(span.strip(), 1))
            out.append(f"{span.strip()} x{float(wt or 1) * m:.2f}" if m != 1 else w)
        if out:
            cfg["publishing"][key] = out


def path(cfg: dict, key: str) -> Path:
    p = ROOT / cfg["paths"][key]
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir(cfg: dict, *sub: str) -> Path:
    p = ROOT / cfg["paths"]["cache"] / Path(*sub)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------- HTTP
_session = requests.Session()
_session.headers["User-Agent"] = "quran-shorts/1.0"


class RetryableHTTP(Exception):
    pass


@retry(
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout, RetryableHTTP)),
    wait=wait_exponential(multiplier=1.5, min=2, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def http_get(url: str, **kw) -> requests.Response:
    kw.setdefault("timeout", 30)
    r = _session.get(url, **kw)
    if r.status_code == 429 or r.status_code >= 500:
        raise RetryableHTTP(f"{r.status_code} for {url}")
    r.raise_for_status()
    return r


def http_json(url: str, **kw):
    return http_get(url, **kw).json()


def download(url: str, dest: Path, **kw) -> Path:
    """Download to dest (skips if already cached). Writes atomically."""
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    r = http_get(url, stream=True, timeout=120, **kw)
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(1 << 16):
            f.write(chunk)
    os.replace(tmp, dest)
    return dest


# ---------------------------------------------------------------- FFmpeg
def _find_tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    # winget installs land here but PATH needs a new shell
    pattern = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages",
                           "*FFmpeg*", "*", "bin", f"{name}.exe")
    hits = glob.glob(pattern)
    if hits:
        return hits[0]
    # not fatal at import: the Telegram poller runs without FFmpeg; a render fails clearly instead
    logging.getLogger(__name__).debug("%s not found; install FFmpeg and put it on PATH", name)
    return name


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")


def run_ffmpeg(args: list[str], what: str = "ffmpeg") -> None:
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args]
    log.debug("%s: %s", what, " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"{what} failed:\n{p.stderr[-3000:]}")


def media_duration(p: Path) -> float:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


# ---------------------------------------------------------------- JSON state
def read_json(p: Path, default):
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return default


def write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
