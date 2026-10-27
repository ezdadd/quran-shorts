"""Check every publishing credential without posting anything.

    python -m src.publish.check
"""
from __future__ import annotations

import subprocess
from datetime import datetime

from ..util import FFMPEG, ROOT, cache_dir, load_config, setup_logging
from . import buffer, facebook, hosting


def main():
    setup_logging()
    cfg = load_config()
    ok = True
    print("— Buffer")
    if not buffer.configured():
        print("  ✗ BUFFER_API_KEY missing"); ok = False
    else:
        try:
            ch = buffer.channels()
            for s in buffer.SERVICES:
                c = ch.get(s)
                print(f"  {'✓' if c else '✗'} {s}: {c['displayName'] or c['name'] if c else 'not connected'}"
                      + (" (queue paused!)" if c and c["isQueuePaused"] else ""))
                ok &= bool(c)
        except Exception as e:
            print(f"  ✗ {e}"); ok = False

    print("— Cloudinary")
    if not hosting.configured():
        print("  ✗ CLOUDINARY_* missing"); ok = False
    else:
        test = cache_dir(cfg, "check") / "test.mp4"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=360x640:d=2",
                        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "2", "-c:v", "libx264",
                        "-c:a", "aac", "-pix_fmt", "yuv420p", str(test)], check=True)
        try:
            h = hosting.upload(test, "connection-test")
            pub = hosting.url_is_public(h["url"])
            print(f"  ✓ upload works; public URL reachable: {'yes' if pub else 'NO'}")
            hosting.delete(h["public_id"])
            print("  ✓ delete works")
            ok &= pub
        except Exception as e:
            print(f"  ✗ {e}"); ok = False

    print("— Facebook")
    if not facebook.configured():
        print("  ✗ FB_PAGE_ID / FB_PAGE_TOKEN missing"); ok = False
    else:
        try:
            st = facebook.token_status(cfg)
            exp = st.get("expires_at")
            exp_s = "never" if exp == 0 else (datetime.fromtimestamp(exp).strftime("%Y-%m-%d") if exp else "unknown (add FB_APP_ID/SECRET)")
            print(f"  {'✓' if st['valid'] else '✗'} page “{st['page']}”, token type: {st.get('type', '?')}, expires: {exp_s}")
            if st.get("type") and st["type"] != "PAGE":
                print("    → this is a user token, not a Page token: run  python -m tools.fb_token <short token>")
            need = {"pages_show_list", "pages_read_engagement", "pages_manage_posts"}
            if st.get("scopes"):
                missing = need - set(st["scopes"])
                print(f"  {'✓' if not missing else '✗'} permissions" + (f" missing: {', '.join(missing)}" if missing else ""))
                ok &= not missing
            ok &= st["valid"]
        except Exception as e:
            print(f"  ✗ {e}"); ok = False
    print("\nall good ✓" if ok else "\nfix the ✗ items above, then run again")


if __name__ == "__main__":
    main()
