"""Deploy the instant Telegram bot (worker/bot.js) to Cloudflare Workers (free plan) and point
Telegram at it.

    python tools/deploy_worker.py            # deploy / update + set the webhook
    python tools/deploy_worker.py --off      # back to plain polling (removes the webhook)

Needs in .env: CLOUDFLARE_API_TOKEN (template "Edit Cloudflare Workers"), CLOUDFLARE_ACCOUNT_ID,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GH_TOKEN. Creates WORKER_KEY / WORKER_TG_SECRET in .env and
WORKER_URL / WORKER_KEY in the GitHub repo secrets. Prints no secret values.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.fb_token import set_env  # noqa: E402  (writes KEY=value into .env)
from src.util import load_config  # noqa: E402

NAME = "ezdadd-bot"
KV_TITLE = "ezdadd-bot-updates"
REPO = "ezdadd/quran-shorts"
CF = "https://api.cloudflare.com/client/v4"
GH = os.environ.get("GH_CLI", r"C:\Program Files\GitHub CLI\gh.exe")


def cf(method: str, path: str, **kw) -> dict:
    r = requests.request(method, f"{CF}{path}", timeout=60,
                         headers={"Authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}"}, **kw)
    body = r.json()
    if not body.get("success"):
        raise SystemExit(f"Cloudflare {method} {path.split('?')[0]}: {body.get('errors')}")
    return body["result"]


def tg(method: str, **params) -> dict:
    r = requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}", json=params,
                      timeout=30).json()
    if not r.get("ok"):
        raise SystemExit(f"Telegram {method}: {r.get('description')}")
    return r["result"]


def gh_secret(name: str, value: str) -> None:
    subprocess.run([GH, "secret", "set", name, "-R", REPO], input=value, text=True, check=True,
                   capture_output=True, env={**os.environ, "GH_TOKEN": os.environ["GH_TOKEN"]})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--off", action="store_true")
    a = ap.parse_args()
    load_config()                       # loads .env into the environment
    if a.off:
        tg("deleteWebhook")
        gh_secret("WORKER_URL", "")
        print("webhook removed — the GitHub poller reads Telegram directly again")
        return
    for k in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GH_TOKEN"):
        if not os.environ.get(k):
            raise SystemExit(f"{k} missing in .env")
    acc = os.environ["CLOUDFLARE_ACCOUNT_ID"].strip()
    key = os.environ.get("WORKER_KEY") or secrets.token_urlsafe(32)
    tg_secret = os.environ.get("WORKER_TG_SECRET") or secrets.token_urlsafe(32)
    set_env("WORKER_KEY", key)
    set_env("WORKER_TG_SECRET", tg_secret)

    # 1) KV namespace for the waiting updates
    spaces = cf("GET", f"/accounts/{acc}/storage/kv/namespaces?per_page=100")
    ns = next((s["id"] for s in spaces if s["title"] == KV_TITLE), None) or \
        cf("POST", f"/accounts/{acc}/storage/kv/namespaces", json={"title": KV_TITLE})["id"]
    print("KV namespace ready")

    # 2) the script (ES module) with its bindings and secrets
    meta = {"main_module": "bot.js", "compatibility_date": "2026-10-01",
            "bindings": [{"type": "kv_namespace", "name": "UPDATES", "namespace_id": ns},
                         {"type": "plain_text", "name": "REPO", "text": REPO},
                         {"type": "plain_text", "name": "CHAT_ID", "text": os.environ["TELEGRAM_CHAT_ID"].strip()},
                         {"type": "secret_text", "name": "TG_TOKEN", "text": os.environ["TELEGRAM_BOT_TOKEN"].strip()},
                         {"type": "secret_text", "name": "TG_SECRET", "text": tg_secret},
                         {"type": "secret_text", "name": "WORKER_KEY", "text": key},
                         {"type": "secret_text", "name": "GH_TOKEN", "text": os.environ["GH_TOKEN"].strip()}]}
    code = (ROOT / "worker" / "bot.js").read_bytes()
    cf("PUT", f"/accounts/{acc}/workers/scripts/{NAME}",
       files={"metadata": (None, json.dumps(meta), "application/json"),
              "bot.js": ("bot.js", code, "application/javascript+module")})
    print("worker uploaded")
    # timekeeper: wake the GitHub poller every 10 min (GitHub's own cron fires ~hourly)
    cf("PUT", f"/accounts/{acc}/workers/scripts/{NAME}/schedules", json=[{"cron": "*/10 * * * *"}])
    print("cron trigger: every 10 min")

    # 3) public URL on workers.dev
    try:
        sub = cf("GET", f"/accounts/{acc}/workers/subdomain")["subdomain"]
    except SystemExit:
        sub = cf("PUT", f"/accounts/{acc}/workers/subdomain", json={"subdomain": "ezdadd"})["subdomain"]
    cf("POST", f"/accounts/{acc}/workers/scripts/{NAME}/subdomain", json={"enabled": True})
    url = f"https://{NAME}.{sub}.workers.dev"
    set_env("WORKER_URL", url)
    print("worker URL:", url)

    # 4) Telegram -> worker, GitHub poller -> worker
    tg("setWebhook", url=f"{url}/tg", secret_token=tg_secret,
       allowed_updates=["message", "callback_query"])
    gh_secret("WORKER_URL", url)
    gh_secret("WORKER_KEY", key)
    info = tg("getWebhookInfo")
    print("webhook set:", info.get("url") == f"{url}/tg", "| pending updates:", info.get("pending_update_count"))


if __name__ == "__main__":
    main()
