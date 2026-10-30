"""Keeps an eye on everything that runs without you.

    python -m src.health --watchdog     # hourly: any GitHub job that failed in the last hour -> Telegram
    python -m src.health --check        # daily: Facebook token, Buffer channels, Cloudinary space, Gemini

Jobs that already send their own alert (daily videos, link clips, tests) are left out of the watchdog,
so you never get the same failure twice. The daily check only writes when something needs you, and
says how to fix it.
"""
from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timedelta, timezone

import requests

from .util import load_config, setup_logging

log = logging.getLogger("quran-shorts.health")
SELF_ALERTING = {"daily-shorts", "clip-from-link", "tests"}
NAMES_AR = {"telegram-review": "البوت", "cleanup-hosting": "تنظيف Cloudinary", "performance-stats": "الإحصائيات",
            "clip-sources": "المصادر التلقائية"}


def _send(text: str) -> None:
    print(text)
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        from .review import telegram
        telegram.api("sendMessage", chat_id=telegram.owner(), text=text)


def watchdog(window_minutes: int = 65) -> list[str]:
    repo, tok = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not repo or not tok:
        return []
    since = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get(f"https://api.github.com/repos/{repo}/actions/runs", timeout=30,
                     headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"},
                     params={"status": "failure", "created": f">={since}", "per_page": 50})
    r.raise_for_status()
    out = []
    for run in r.json().get("workflow_runs", []):
        if run["name"] in SELF_ALERTING:
            continue
        out.append(f"• {NAMES_AR.get(run['name'], run['name'])}: {run['html_url']}")
    if out:
        _send("🚨 شغلانة وقفت في آخر ساعة (هتتجرب تاني لوحدها في معادها الجاي):\n" + "\n".join(out[:8]))
    return out


def check(cfg) -> list[str]:
    probs: list[str] = []
    # Facebook page token
    if os.environ.get("FB_PAGE_TOKEN"):
        from .publish import facebook
        try:
            st = facebook.token_status(cfg)
            need = {"pages_show_list", "pages_read_engagement", "pages_manage_posts"}
            if not st["valid"]:
                probs.append("فيسبوك: توكن الصفحة بطل يشتغل — جدده (FB_SHORT_TOKEN في .env وقولّي).")
            elif st.get("scopes") and not need <= set(st["scopes"]):
                probs.append(f"فيسبوك: صلاحيات ناقصة {sorted(need - set(st['scopes']))}.")
            elif st.get("scopes") and "read_insights" not in st["scopes"]:
                probs.append("فيسبوك: صلاحية read_insights راحت — أرقام الريلز هتبقى ناقصة.")
        except Exception as e:
            probs.append(f"فيسبوك: التوكن مش شغال ({str(e)[:90]}) — جدده.")
    # Buffer channels
    if os.environ.get("BUFFER_API_KEY"):
        from .publish import buffer
        try:
            ch = buffer.channels()
            missing = [s for s in ("youtube", "tiktok", "instagram") if s in cfg["publishing"]["platforms"]
                       and s not in ch]
            if missing:
                probs.append(f"Buffer: القنوات دي مش متوصلة: {', '.join(missing)} — افتح Buffer ووصّلها تاني.")
        except Exception as e:
            probs.append(f"Buffer: مش قادر أوصل ({str(e)[:90]}).")
    # Cloudinary storage
    try:
        from .publish import hosting
        cloud, auth = hosting._creds()
        u = requests.get(f"https://api.cloudinary.com/v1_1/{cloud}/usage", auth=auth, timeout=30).json()
        pct = float((u.get("credits") or {}).get("used_percent") or 0)
        if pct >= 80:
            probs.append(f"Cloudinary: المساحة/الرصيد المجاني اتملى {pct:.0f}% — ممكن الرفع يقف قريب.")
    except Exception as e:
        log.warning("cloudinary usage: %s", e)
    # Gemini key (agent, narration, footage check)
    if os.environ.get("GEMINI_API_KEY"):
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models", timeout=30,
                         headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]})
        if r.status_code in (400, 401, 403):
            probs.append("Gemini: المفتاح مش شغال — المصحح وقراءة الأحاديث وفحص المشاهد هيقفوا. اعمل مفتاح جديد.")
    if probs:
        _send("🩺 فحص يومي — محتاج منك:\n" + "\n".join(f"• {p}" for p in probs))
    else:
        log.info("health check: all good")
    return probs


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--watchdog", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    setup_logging()
    cfg = load_config()
    if a.watchdog:
        watchdog()
    if a.check:
        check(cfg)


if __name__ == "__main__":
    main()
