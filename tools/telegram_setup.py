"""One-time Telegram setup: finds your chat id from the /start you sent the bot, saves it to .env,
and sends a test message.

    python -m tools.telegram_setup
"""
from __future__ import annotations

import os
import sys

from src.review import telegram
from src.util import load_config
from tools.fb_token import set_env


def main():
    load_config()
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        sys.exit("put TELEGRAM_BOT_TOKEN in .env first (from @BotFather)")
    me = telegram.api("getMe")
    chats = {}
    for u in telegram.api("getUpdates", timeout=0):
        m = u.get("message") or {}
        if m.get("chat", {}).get("type") == "private":
            chats[m["chat"]["id"]] = m["chat"].get("first_name", "")
    if not chats:
        sys.exit(f"open https://t.me/{me['username']} , press Start (or send /start), then run this again")
    chat_id, name = list(chats.items())[-1]
    set_env("TELEGRAM_CHAT_ID", str(chat_id))
    os.environ["TELEGRAM_CHAT_ID"] = str(chat_id)
    telegram.api("sendMessage", chat_id=chat_id,
                 text="✅ بوت المراجعة شغال. هيوصلك كل فيديو جديد هنا بزرار موافقة/رفض.\n/status لحالة الفيديوهات.")
    print(f"bot @{me['username']} linked to {name} ({chat_id}); TELEGRAM_CHAT_ID saved to .env")


if __name__ == "__main__":
    main()
