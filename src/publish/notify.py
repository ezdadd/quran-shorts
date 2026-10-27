"""Failure / warning notifications: Telegram bot and/or Discord webhook (both free, optional)."""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)


def send(text: str) -> None:
    text = f"⚠️ quran-shorts\n{text}"[:3900]
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not ((tok and chat) or hook):
        log.warning("no notifier configured; message was: %s", text)
        return
    try:
        if tok and chat:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          data={"chat_id": chat, "text": text}, timeout=20)
        if hook:
            requests.post(hook, json={"content": text[:1900]}, timeout=20)
    except requests.RequestException as e:
        log.error("notification failed: %s", e)
