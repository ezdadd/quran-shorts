"""Turn a short-lived Facebook user token into a long-lived Page token and save it to .env.

    python -m tools.fb_token                       # reads FB_SHORT_TOKEN from .env
    python -m tools.fb_token <SHORT_LIVED_USER_TOKEN>

Needs FB_APP_ID and FB_APP_SECRET in .env. Steps (Meta docs, long-lived tokens):
  1. GET /oauth/access_token?grant_type=fb_exchange_token&client_id&client_secret&fb_exchange_token
     -> long-lived user token (~60 days)
  2. GET /me/accounts with that token -> each Page's access_token (a Page token obtained from a
     long-lived user token does not expire)
"""
from __future__ import annotations

import os
import re
import sys

import requests

from src.util import ROOT, load_config


def set_env(key: str, value: str) -> None:
    p = ROOT / ".env"
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    if re.search(rf"^{key}=.*$", text, flags=re.M):
        text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{key}={value}\n"
    p.write_text(text, encoding="utf-8")


def main():
    cfg = load_config()
    short = sys.argv[1] if len(sys.argv) == 2 else os.environ.get("FB_SHORT_TOKEN", "").strip()
    if not short:
        print(__doc__)
        sys.exit(1)
    ver = cfg["publishing"].get("facebook_api_version", "v26.0")
    app_id, secret = os.environ.get("FB_APP_ID"), os.environ.get("FB_APP_SECRET")
    if not (app_id and secret):
        sys.exit("put FB_APP_ID and FB_APP_SECRET in .env first")
    g = f"https://graph.facebook.com/{ver}"
    r = requests.get(f"{g}/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": secret,
        "fb_exchange_token": short}, timeout=30).json()
    if "access_token" not in r:
        sys.exit(f"exchange failed: {r.get('error', r)}")
    pages = requests.get(f"{g}/me/accounts", params={"access_token": r["access_token"]},
                         timeout=30).json().get("data", [])
    if not pages:
        sys.exit("no Pages found — did you grant pages_show_list and pick the Page?")
    for i, pg in enumerate(pages):
        print(f"[{i}] {pg['name']} ({pg['id']})")
    same = [i for i, pg in enumerate(pages) if pg["id"] == os.environ.get("FB_PAGE_ID")]
    k = same[0] if same else 0 if len(pages) == 1 else int(input("which page? "))
    set_env("FB_PAGE_ID", pages[k]["id"])
    set_env("FB_PAGE_TOKEN", pages[k]["access_token"])
    print(f"saved FB_PAGE_ID and FB_PAGE_TOKEN for “{pages[k]['name']}” in .env")


if __name__ == "__main__":
    main()
