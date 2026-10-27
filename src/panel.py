"""Local control panel: watch every generated video, approve/reject, edit lecture subtitles,
re-render, and start generation jobs — all from the browser.

    python -m src.panel          # then open http://127.0.0.1:8765

Binds to 127.0.0.1 only (not reachable from other machines).
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

from .util import ROOT, read_json, write_json

OUTPUT = ROOT / "output"
STATIC = Path(__file__).with_name("panel_static")
app = Flask(__name__, static_folder=None)


# ---------------------------------------------------------------- items
def _records() -> dict[str, Path]:
    """id -> JSON path for every generated video (daily folders + lecture review folder)."""
    out = {}
    for p in OUTPUT.glob("*/*.json"):
        if p.parent.name in ("visual_test",) or p.name.endswith(".info.json"):
            continue
        out[p.stem] = p
    return out


def _status(r: dict) -> str:
    if r.get("rejected"):
        return "rejected"
    if r.get("approved") or (r.get("type") == "quran" and not r.get("review_required")):
        return "approved"
    return "pending"


def _summary(p: Path) -> dict | None:
    r = read_json(p, None)
    if not r or "id" not in r:
        return None
    mp4 = p.with_suffix(".mp4")
    if r.get("type") == "lecture":
        title = r.get("title") or r["id"]
        sub = r.get("sheikh_name", "")
        dur = r["clip"]["end"] - r["clip"]["start"]
        date = time.strftime("%Y-%m-%d", time.localtime(p.stat().st_mtime))
    else:
        ref = r["reference"]
        a, b = ref["from"], ref["to"]
        title = f"سورة {ref['surah_name']} {a}" + (f"–{b}" if b != a else "")
        sub = r["reciter"]["name"]
        dur = r.get("duration", 0)
        date = r.get("date", p.parent.name)
    return {"id": r["id"], "type": r.get("type", "quran"), "title": title, "subtitle": sub,
            "date": date, "duration": round(dur, 1), "status": _status(r),
            "review_required": bool(r.get("review_required") or r.get("type") == "lecture"),
            "flags": len(r.get("flags", [])), "has_video": mp4.exists(),
            "video": f"/media/{mp4.relative_to(OUTPUT).as_posix()}" if mp4.exists() else None,
            "mtime": mp4.stat().st_mtime if mp4.exists() else p.stat().st_mtime}


def _get(item_id: str) -> tuple[Path, dict]:
    p = _records().get(item_id)
    if not p:
        abort(404)
    return p, read_json(p, {})


@app.get("/api/items")
def items():
    out = [s for p in _records().values() if (s := _summary(p))]
    out.sort(key=lambda s: -s["mtime"])
    return jsonify(out)


@app.get("/api/item/<item_id>")
def item(item_id):
    p, r = _get(item_id)
    return jsonify({"record": r, "summary": _summary(p)})


@app.post("/api/item/<item_id>/<action>")
def item_action(item_id, action):
    p, r = _get(item_id)
    if action == "approve":
        r["approved"], r["rejected"] = True, False
    elif action == "reject":
        r["approved"], r["rejected"] = False, True
    elif action == "reset":
        r["approved"], r["rejected"] = False, False
    elif action == "save":
        body = request.get_json(force=True)
        if r.get("type") != "lecture":
            abort(400, "only lecture clips are editable")
        if "title" in body:
            r["title"] = body["title"].strip()
        for edit in body.get("segments", []):
            seg = r["segments"][int(edit["index"])]
            if seg["type"] == "speech":
                seg["text"] = " ".join(edit["text"].split())
        r["approved"] = False      # edited content must be re-checked
        r["edited"] = True
    elif action == "render":
        if r.get("type") != "lecture":
            abort(400)
        return jsonify(start_job(f"إعادة رندر {item_id}",
                                 [sys.executable, "-m", "src.lectures", "render", item_id]))
    else:
        abort(404)
    write_json(p, r)
    # items that need human review go through the publish queue (hosted + pushed to GitHub)
    needs_queue = r.get("type") == "lecture" or r.get("review_required")
    queued = (ROOT / "queue" / f"{item_id}.json").exists()
    job = None
    if needs_queue and action == "approve":
        job = start_job(f"إضافة {item_id} لطابور النشر",
                        [sys.executable, "-m", "src.publish.queue", "add", item_id])
    elif queued and action in ("reject", "reset", "save"):
        job = start_job(f"شيل {item_id} من طابور النشر",
                        [sys.executable, "-m", "src.publish.queue", "remove", item_id])
    return jsonify({"ok": True, "summary": _summary(p), "job": job})


@app.get("/media/<path:rel>")
def media(rel):
    f = (OUTPUT / rel).resolve()
    if OUTPUT.resolve() not in f.parents or not f.is_file():
        abort(404)
    return send_file(f, conditional=True)   # supports range requests for seeking


# ---------------------------------------------------------------- background jobs
_job = {"name": None, "running": False, "log": deque(maxlen=400), "started": None, "code": None}
_lock = threading.Lock()


def start_job(name: str, cmd: list[str]) -> dict:
    with _lock:
        if _job["running"]:
            return {"ok": False, "error": f"في شغلانة شغالة دلوقتي: {_job['name']}"}
        _job.update(name=name, running=True, started=time.time(), code=None)
        _job["log"].clear()

    def run():
        env = {**__import__("os").environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", env=env)
        for line in proc.stdout:
            line = line.rstrip()
            if line and "symlink" not in line and "warnings.warn" not in line and "httpx" not in line:
                _job["log"].append(line)
        _job["code"] = proc.wait()
        _job["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


@app.get("/api/job")
def job():
    return jsonify({"name": _job["name"], "running": _job["running"], "code": _job["code"],
                    "elapsed": round(time.time() - _job["started"]) if _job["started"] else 0,
                    "log": list(_job["log"])[-60:]})


@app.post("/api/job/generate")
def job_generate():
    b = request.get_json(force=True)
    cmd = [sys.executable, "-m", "src.main", "--generate", str(int(b.get("count", 1)))]
    if b.get("dry_run", True):
        cmd.append("--dry-run")
    elif not b.get("publish"):
        cmd.append("--no-publish")     # publishing only when explicitly ticked
    for k in ("reciter", "category", "passage"):
        if b.get(k):
            cmd += [f"--{k}", str(b[k])]
    return jsonify(start_job(f"توليد {b.get('count', 1)} فيديو قرآني", cmd))


@app.post("/api/job/lectures")
def job_lectures():
    b = request.get_json(force=True)
    cmd = [sys.executable, "-m", "src.lectures", "prepare", "--per-file", str(int(b.get("per_file", 5)))]
    if b.get("sheikh"):
        cmd += ["--sheikh", b["sheikh"]]
    if b.get("force"):
        cmd.append("--force")
    return jsonify(start_job("تجهيز مقاطع الدروس", cmd))


@app.post("/api/job/fetch")
def job_fetch():
    b = request.get_json(force=True)
    urls = [u for u in b.get("urls", "").split() if u.startswith("http")]
    if not urls or b.get("sheikh") not in ("samir_mostafa", "amgad_samir"):
        abort(400)
    return jsonify(start_job("تحميل درس", [sys.executable, "-m", "tools.fetch_lecture", b["sheikh"], *urls]))


@app.post("/api/job/publish")
def job_publish():
    b = request.get_json(force=True)
    cmd = [sys.executable, "-m", "src.publish.runner"]
    if b.get("dry_run", True):
        cmd.append("--dry-run")
    if b.get("cleanup"):
        cmd = [sys.executable, "-m", "src.publish.runner", "--cleanup"]
    return jsonify(start_job("النشر والجدولة" if not b.get("cleanup") else "تحديث حالة المنشورات", cmd))


@app.post("/api/job/check")
def job_check():
    return jsonify(start_job("فحص مفاتيح النشر", [sys.executable, "-m", "src.publish.check"]))


@app.get("/api/config")
def config():
    from .util import load_config
    cfg = load_config()
    return jsonify({"reciters": {k: v["name_ar"] + (f" · {v['label_ar']}" if v.get("label_ar") else "")
                                 for k, v in cfg["reciters"].items()},
                    "categories": list(cfg["footage"]["categories"])})


# ---------------------------------------------------------------- static UI
@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/fonts/<name>")
def fonts(name):
    return send_from_directory(ROOT / "assets" / "fonts", name)


def main():
    port = 8765
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
