"""Fast checks of the pieces that broke (or could break) the pipeline. Run: python -m pytest -q
No network, no FFmpeg, no browser: everything external is replaced by small fakes."""
from __future__ import annotations

import ast
import json
import pkgutil
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


# ---------------------------------------------------------------- code health
def _py_files():
    return [p for p in SRC.rglob("*.py")] + list((ROOT / "tools").glob("*.py"))


def test_every_file_parses():
    for p in _py_files():
        ast.parse(p.read_text(encoding="utf-8"), filename=str(p))


LOCAL_ONLY = {"src.panel"}      # the PC control panel (needs Flask) — never runs on GitHub


def test_every_module_imports():
    """Catches a missing import / typo at module level before a job hits it."""
    import importlib
    import src
    for m in pkgutil.walk_packages(src.__path__, "src."):
        if m.name not in LOCAL_ONLY:
            importlib.import_module(m.name)


def test_no_local_import_shadows_a_module_level_import():
    """The bug that broke every clip job: `import json` inside main() made `json` a local name
    for the whole function. Any function importing a name that is also imported at module level
    and used in that function is flagged."""
    bad = []
    for p in _py_files():
        tree = ast.parse(p.read_text(encoding="utf-8"))
        top = {a.asname or a.name.split(".")[0] for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
               for a in n.names}
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            local = {a.asname or a.name.split(".")[0] for n in ast.walk(fn) if isinstance(n, ast.Import)
                     for a in n.names}
            if local & top:
                bad.append(f"{p.name}:{fn.name} re-imports {sorted(local & top)}")
    assert not bad, bad


def test_workflows_and_config_are_valid_yaml():
    import yaml
    for p in list((ROOT / ".github" / "workflows").glob("*.yml")) + [ROOT / "config.yaml"]:
        yaml.safe_load(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- Telegram message parsing
def test_link_messages_sound_or_clip():
    from src.clip_request import parse_request, sound_name
    U = "https://www.tiktok.com/@x/video/123"
    assert sound_name(U) is None                                   # bare link -> clip
    assert sound_name(f"{U} مطر هادي") == "مطر هادي"                 # link + name -> sound
    assert sound_name(f"صوت: نشيد {U}") == "نشيد"
    assert sound_name(f"{U} 1:20-2:05") is None
    assert sound_name(f"{U}\nالشيخ محمد") is None
    assert sound_name(f"{U}\nعنوان: الرزق") is None
    assert sound_name(f"{U} 3 مقاطع") is None
    assert sound_name(f"مصدر: {U} الشيخ فلان") is None              # a source, not a sound
    r = parse_request(f"{U} 1:20-2:05\nالشيخ محمد\nعنوان: الرزق")
    assert (r["start"], r["end"], r["sheikh"], r["title"]) == (80, 125, "الشيخ محمد", "الرزق")


def test_subtitle_edits():
    from src.review.edit import apply_edits, parse_edits
    assert parse_edits("3: النص الصح\n5: حذف") == {"3": "النص الصح", "5": None}
    r = {"segments": [{"type": "speech", "start": 0, "end": 2, "text": "a b",
                       "words": [{"w": "a", "s": 0, "e": 1}, {"w": "b", "s": 1, "e": 2}]},
                      {"type": "speech", "start": 2, "end": 3, "text": "c", "words": [{"w": "c", "s": 2, "e": 3}]}]}
    assert apply_edits(r, {"1": "x y", "2": None}) == 2
    assert [s["text"] for s in r["segments"]] == ["x y"]
    assert r["segments"][0]["words"][0]["s"] == 0                  # same word count keeps timing


def test_tags_and_youtube_limit(monkeypatch):
    from src.review import store
    monkeypatch.setattr(store, "load_config", lambda: {"captions": {"fixed_hashtags": [f"#t{i}" for i in 20 * [0]] and
                                                                      [f"#t{i}" for i in range(20)]}})
    r = {"id": "q-1", "platforms": {"youtube": {"caption": "عنوان\n\n#a #b", "hashtags": ["#a", "#b"]},
                                    "tiktok": {"caption": "عنوان\n\n#a #b", "hashtags": ["#a", "#b"]}}}
    yt = store.final_caption(r, "youtube", {})
    assert len(store.TAG.findall(yt)) == 15 and "#shorts" in yt     # YouTube ignores all above 15
    assert len(store.TAG.findall(store.final_caption(r, "tiktok", {}))) == 22


def test_approval_rules():
    from src.review.store import is_approved, is_rejected
    r = {"id": "c-1", "auto": {"status": "approved"}}
    assert is_approved(r, {})                                       # agent approval counts...
    assert not is_approved(r, {"c-1": {"status": "waiting"}})       # ...until you press ↩️
    assert is_rejected(r, {"c-1": {"status": "rejected"}})


# ---------------------------------------------------------------- scheduling + tuning
def _cfg():
    import yaml
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def test_schedule_keeps_gap_from_taken_posts():
    from src import scheduler
    cfg = _cfg()
    tz = ZoneInfo(cfg["timezone"])
    d = date(2030, 1, 1)
    taken = [datetime(2030, 1, 1, 21, 10, tzinfo=tz), datetime(2030, 1, 1, 14, 20, tzinfo=tz)]
    t = scheduler.base_times(cfg, random.Random(1), 2, d, now=datetime(2030, 1, 1, 6, 0, tzinfo=tz), taken=taken)
    assert len(t) == 2 and min(abs((a - b).total_seconds()) for a in t for b in taken) >= 20 * 60


def test_tuning_multiplies_but_keeps_disabled_reciters(tmp_path, monkeypatch):
    from src import util
    cfg = _cfg()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "tuning.json").write_text(json.dumps(
        {"theme_weights": {"دعاء": 2}, "reciter_weights": {"minshawi_1967": 2},
         "window_weights": {"20:30-22:30": 1.5}}), encoding="utf-8")
    monkeypatch.setattr(util, "ROOT", tmp_path)
    before = cfg["quran"]["theme_weights"]["دعاء"]
    util._apply_tuning(cfg)
    assert cfg["quran"]["theme_weights"]["دعاء"] == before * 2
    assert cfg["reciters"]["minshawi_1967"]["weight"] == 0
    assert any(w.startswith("20:30-22:30 x") and w != "20:30-22:30 x4" for w in cfg["publishing"]["windows"])


# ---------------------------------------------------------------- agent policy
def test_agent_never_edits_lines_next_to_an_ayah():
    from src.agent.corrector import Corrector
    r = {"segments": [
        {"type": "speech", "start": 0, "end": 1, "text": "ابن القائم قال", "words": [{"w": "x", "s": 0, "e": 1}]},
        {"type": "speech", "start": 1, "end": 2, "text": "قال لان تراني", "words": [{"w": "y", "s": 1, "e": 2}]},
        {"type": "quran", "start": 2, "end": 3, "text": "…", "ref": "7:143"}]}
    c = Corrector({"agent": {}}, r, Path("x"))
    res = c.decide({"edits": [{"line": 1, "action": "replace", "text": "ابن القيم قال", "reason": ""},
                              {"line": 2, "action": "replace", "text": "قال لن تراني", "reason": ""}],
                    "locked": [], "confidence": "high", "summary_ar": ""})
    assert res["edits"] == {"1": "ابن القيم قال"}                   # spelling fix kept
    assert res["decision"] == "hold"                                # line beside the ayah -> you decide


def test_agent_drops_rewrites_and_holds_uncertain_hadith():
    from src.agent.corrector import Corrector
    r = {"segments": [{"type": "speech", "start": 0, "end": 1, "text": "كلام الشيخ هنا",
                       "words": [{"w": "x", "s": 0, "e": 1, "p": 0.9}]}]}
    c = Corrector({"agent": {}}, r, Path("x"))
    res = c.decide({"edits": [{"line": 1, "action": "replace", "text": "جملة مختلفة تماما عن الأصل", "reason": ""}],
                    "locked": [{"line": 1, "kind": "hadith", "uncertain": True}], "confidence": "high",
                    "summary_ar": ""})
    assert res["edits"] == {} and res["decision"] == "hold"


def test_memory_learns_spelling_fixes(tmp_path, monkeypatch):
    from src.agent import memory
    monkeypatch.setattr(memory, "DIR", tmp_path)
    memory.record({"id": "a"}, [{"line": 1, "old": "قال ابن القائم", "new": "قال ابن القيم"}])
    memory.record({"id": "b"}, [{"line": 1, "old": "يقول ابن القائم", "new": "يقول ابن القيم"}])
    words = [{"w": w, "s": i, "e": i + 1, "p": 0.5} for i, w in enumerate("وابن القائم".split())]
    assert memory.apply_rules(words, memory.rules(2)) == 1 and words[1]["w"] == "القيم"


# ---------------------------------------------------------------- content sources
def test_sources_random_no_repeats(tmp_path, monkeypatch):
    from src import sources as S
    monkeypatch.setattr(S, "LIST", tmp_path / "l.json")
    monkeypatch.setattr(S, "SEEN", tmp_path / "s.json")
    S.add("https://www.tiktok.com/@a")
    with pytest.raises(S.SourceError):
        S.add("https://www.youtube.com/@x")
    vids = [{"id": f"tiktok:{i}", "url": f"u{i}", "title": "", "duration": 900 if i == 0 else 40} for i in range(6)]
    monkeypatch.setattr(S, "latest", lambda s, n=40: vids)
    got = [S.run(_cfg(), random.Random(d))[0] for d in range(7)]
    urls = [x["url"] for day in got for x in day]
    assert len(urls) == len(set(urls)) == 5 and "u0" not in urls  # no repeats, long one skipped


def test_hadith_intro_split_and_composite_filter():
    from src.hadith import load, split_intro
    intro, body = split_intro({"intro": "", "text": "عن أبي هريرة قال: «الكلمة الطيبة صدقة»."})
    assert intro == "عن أبي هريرة قال:" and body.startswith("«")
    assert all("الحديث الثاني" not in (h.get("attribution") or "") for h in load())
    assert all(any(g in h["grade"] for g in ("صحيح", "حسن")) for h in load())


def test_quran_check_catches_broken_videos(monkeypatch):
    from src.publish import hosting
    from src.review import telegram
    monkeypatch.setattr(hosting, "url_is_public", lambda u: True)
    cfg = _cfg()
    ok = {"ayat": [{"text": "x"}], "duration": 30, "hosting": {"url": "u"},
          "platforms": {"youtube": {"caption": "c"}, "tiktok": {"caption": "c"}}}
    assert telegram.quran_check(cfg, ok) is None
    assert telegram.quran_check(cfg, ok | {"duration": 300})
    assert telegram.quran_check(cfg, ok | {"ayat": [{"text": ""}]})
    h = ok | {"kind": "hadith", "hadith": {"text": "x", "attribution": "رواه مسلم", "grade": "ضعيف"}}
    assert telegram.quran_check(cfg, h)                             # weak hadith never passes


def test_kids_story_rules(monkeypatch):
    """The model's story never carries the hadith or forbidden characters; only short kid topics are picked."""
    from src import hadith, kids
    h = next(x for x in hadith.load() if x["words"] <= 20 and "الأخلاق" in (x.get("topics") or []))
    story = {"suitable": True, "title": "t", "moral": "درس", "closing_image": "c",
             "scenes": [{"narration": "ذهب عمر إلى الحديقة", "image": "a"}, {"narration": "ساعد عمر جاره", "image": "b"}]}
    monkeypatch.setattr(kids, "_gemini", lambda *a, **k: json.loads(json.dumps(story)))
    assert len(kids.write_story(h, random.Random(1))["scenes"]) == 2
    for bad in ({"suitable": False}, {"scenes": [{"narration": "قالت أمه له", "image": "a"}] * 2},
                {"scenes": [{"narration": "قال النبي", "image": "a"}] * 2}):
        monkeypatch.setattr(kids, "_gemini", lambda *a, b=bad, **k: story | b)
        with pytest.raises(kids.KidsError):
            kids.write_story(h, random.Random(1))
    for _ in range(20):
        p = kids.pick({}, random.Random())
        assert p["words"] <= 20 and set(p["topics"]) & set(kids.KID_TOPICS)
    assert "no " not in kids.STYLE.lower()          # Flux draws whatever it is told not to


def test_dataset_labels_and_final_text():
    """Training data: your corrections win over the raw transcript; untouched clips aren't kept."""
    from src import dataset
    r = {"id": "c-1", "segments": [
        {"type": "speech", "start": 0.0, "end": 2.0, "text": "النص المصحح", "words": [{"w": "النص"}, {"w": "الغلط"}]},
        {"type": "quran", "start": 2.0, "end": 4.0, "words": ["قُلْ", "هُوَ", "اللَّهُ", "أَحَدٌ"]},
        {"type": "speech", "start": 4.0, "end": 4.0, "words": [{"w": "فاضي"}]}]}
    ls = dataset.lines(r)
    assert [x["text"] for x in ls] == ["النص المصحح", "قُلْ هُوَ اللَّهُ أَحَدٌ"]
    assert dataset.label(r, {}) is None
    assert dataset.label(r | {"edited_at": "x"}, {}) == "human"
    assert dataset.label(r | {"auto": {"status": "approved"}}, {}) == "agent"


def test_screen_scene_choice_matches_the_clip(monkeypatch):
    from src import screens
    lib = {"p1": {"portrait": True, "kind": "phone"}, "p2": {"portrait": True, "kind": "phone"},
           "l1": {"portrait": False, "kind": "laptop"}}
    monkeypatch.setattr(screens, "library", lambda: lib)
    for i in range(40):
        look = screens.choose({}, random.Random(i), clip_portrait=False)
        assert look["scene"] == "l1" and look["frame"] in ("close", "tall", "wide")   # never a tiny phone
        look = screens.choose({}, random.Random(i), clip_portrait=True, avoid={"p1"})
        assert look["scene"] != "p1"
    assert screens.safe(look)["camera"] == "still"


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="needs FFmpeg")
def test_screen_composite_leaves_no_green(tmp_path, monkeypatch):
    """A clip warped onto a green screen seen in perspective: no green left, the clip shows there."""
    import cv2
    import numpy as np
    from src import screens
    from src.util import load_config, run_ffmpeg
    quad = np.int32([[700, 300], [1250, 330], [1230, 700], [690, 680]])
    vw = cv2.VideoWriter(str(tmp_path / "scene.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), 30, (1920, 1080))
    for _ in range(90):
        f = np.full((1080, 1920, 3), 110, np.uint8)
        cv2.fillConvexPoly(f, quad, (40, 230, 40))
        vw.write(f)
    vw.release()
    clip = tmp_path / "clip.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=red:s=640x360:d=3", "-f", "lavfi", "-i", "sine=f=440:d=3",
                "-c:v", "libx264", "-c:a", "aac", "-shortest", str(clip)], "test clip")
    monkeypatch.setattr(screens, "scene_file", lambda cfg, sid: tmp_path / "scene.mp4")
    look = {"scene": "t", "frame": "close", "camera": "push", "grade": "warm", "vignette": True, "grain": False,
            "round": True, "backdrop": "blur", "title": "plain", "crop": [0, 0, 1, 1]}   # no face to zoom on
    with pytest.raises(screens.NoPicture):          # a clip with no one on screen goes on our footage
        screens.focus(clip, 0, 3, 1.7)
    out = tmp_path / "out.mp4"
    geo = screens.compose(load_config(), look, clip, 0.5, 2.0, out, tmp_path)
    assert geo["top"] == screens.TOP and geo["sub_y"] > geo["bottom"]
    cap = cv2.VideoCapture(str(out))
    cap.set(cv2.CAP_PROP_POS_FRAMES, 30)
    ok, fr = cap.read()
    assert ok and fr.shape[:2] == (1920, 1080)
    hsv = cv2.cvtColor(fr, cv2.COLOR_BGR2HSV)
    assert cv2.countNonZero(cv2.inRange(hsv, (40, 120, 80), (85, 255, 255))) < 200          # green gone
    red = cv2.inRange(hsv, (0, 120, 80), (10, 255, 255)) | cv2.inRange(hsv, (170, 120, 80), (180, 255, 255))
    assert cv2.countNonZero(red) > 50000                                                       # clip on screen


def test_clip_job_parses_its_request(monkeypatch):
    """Regression for the UnboundLocalError: main() must get as far as making the clip."""
    from src import clips
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    called = {}
    monkeypatch.setattr(clips, "make_clips", lambda cfg, req: called.setdefault("req", req) and [])
    clips.main(["--req", json.dumps({"url": "https://x", "id": "t"})])
    assert called["req"]["url"] == "https://x"


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="needs FFmpeg")
def test_quality_check_flags_broken_videos(tmp_path):
    from src import qc
    from src.util import run_ffmpeg
    bad = tmp_path / "bad.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "color=black:s=1080x1920:d=6", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-t", "6", "-shortest", "-c:v", "libx264", "-c:a", "aac", str(bad)], "test")
    probs = qc.check(bad)
    assert any("black" in p for p in probs) and any("silent" in p for p in probs)
    assert qc.check(bad, allow_silent=True) == [p for p in probs if "silent" not in p]


# ---------------------------------------------------------------- regressions from the 2026-10-31 incidents
@pytest.mark.skipif(any(__import__("shutil").which(t) is None for t in ("bash", "git", "rsync")),
                    reason="needs bash, git and rsync (runs on GitHub)")
def test_parallel_jobs_never_lose_their_records(tmp_path):
    """Two jobs save at once on a runner with NO git identity (exactly what lost 6 clips): both
    records must land, a file both changed keeps the later job's version."""
    import os
    import shutil
    import subprocess
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR", "GIT_COMMITTER"))}
    env.update(HOME=str(tmp_path / "home"), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               STATE_TOKEN="x", GITHUB_RUN_ID="1")
    bare = tmp_path / "state.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True, env=env)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True, env=env)
    (seed / "queue").mkdir()
    (seed / "queue" / "old.json").write_text("{}")
    g = ["git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(g + ["add", "-A"], check=True, env=env)
    subprocess.run(g + ["commit", "-q", "-m", "seed"], check=True, env=env)
    subprocess.run(g + ["push", "-q", "origin", "HEAD:main"], check=True, env=env)
    env["STATE_URL"] = bare.as_uri()

    def job(name: str) -> Path:
        d = tmp_path / name
        (d / "tools").mkdir(parents=True)
        shutil.copy(ROOT / "tools" / "state.sh", d / "tools" / "state.sh")
        r = subprocess.run(["bash", "tools/state.sh", "pull"], cwd=d, env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return d

    a, b = job("a"), job("b")                       # both start from the same records
    for d, n in ((a, "a"), (b, "b")):
        (d / "queue" / f"{n}.json").write_text("{}")
        (d / "data").mkdir(exist_ok=True)
        (d / "data" / "reviews.json").write_text(json.dumps({"by": n}))
    for d in (a, b):
        r = subprocess.run(["bash", "tools/state.sh", "save", "test"], cwd=d, env=env, capture_output=True, text=True)
        assert r.returncode == 0 and "saved" in r.stdout, r.stdout + r.stderr
    check = tmp_path / "check"
    subprocess.run(["git", "clone", "-q", str(bare), str(check)], check=True, env=env)
    assert {p.name for p in (check / "queue").glob("*.json")} == {"old.json", "a.json", "b.json"}
    assert json.loads((check / "data" / "reviews.json").read_text()) == {"by": "b"}


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="needs FFmpeg")
def test_review_copy_fits_the_telegram_limit(tmp_path, monkeypatch):
    """A 3-minute clip was too big for the bot (50 MB): a lighter copy is sent instead."""
    from src.review import telegram
    from src.util import run_ffmpeg
    v = tmp_path / "big.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=s=540x960:d=4", "-f", "lavfi", "-i", "sine=d=4", "-c:v", "libx264",
                "-crf", "4", "-c:a", "aac", "-shortest", str(v)], "test")
    assert telegram.review_copy(v) == v                       # small enough: sent as it is
    monkeypatch.setattr(telegram, "TG_MAX", v.stat().st_size // 3)
    out = telegram.review_copy(v)
    assert out != v and out.stat().st_size <= telegram.TG_MAX


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="needs FFmpeg")
def test_long_shared_clip_length_rules(tmp_path):
    """A shared clip may be up to 3 min; everything else stays a <= 61 s Short."""
    from src import qc
    from src.util import run_ffmpeg
    v = tmp_path / "long.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "color=gray:s=1080x1920:r=2:d=70", "-f", "lavfi", "-i", "sine=d=70",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(v)], "test")
    assert any("Shorts-sized" in p for p in qc.check(v))
    assert not any("Shorts-sized" in p for p in qc.check(v, max_len=181))


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="needs FFmpeg")
def test_video_probe_reads_codecs_opencv_cannot(tmp_path):
    """Facebook served VP9/AV1 and the clip was treated as sound-only: probing must use FFprobe."""
    import subprocess
    from src.clips import video_size
    from src.util import FFMPEG
    v = tmp_path / "vp9.webm"
    r = subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x568:d=1", "-c:v", "libvpx-vp9",
                        "-b:v", "200k", str(v)], capture_output=True)
    if r.returncode:
        pytest.skip("this FFmpeg has no VP9 encoder")
    assert video_size(v) == (320, 568)
    a = tmp_path / "a.m4a"
    subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "sine=d=1", str(a)], check=True)
    assert video_size(a) is None


def test_paid_gemini_first_then_free_automatically(tmp_path, monkeypatch):
    """Pro on the paid key first; no paid key, a busy / out-of-quota paid model, or today's paid cap
    reached -> the free models carry on by themselves."""
    from src.agent import gemini
    monkeypatch.setattr(gemini, "USAGE", tmp_path / "usage.json")
    monkeypatch.setattr(gemini, "available", lambda paid=False: {"pro", "flash"})
    models = ["pro@paid", "flash", "flash@paid"]
    monkeypatch.delenv("GEMINI_API_KEY_PAID", raising=False)
    assert gemini.chain(models) == ["flash"]                    # no paid key: free only
    monkeypatch.setenv("GEMINI_API_KEY_PAID", "k")
    assert gemini.chain(models) == ["pro@paid", "flash", "flash@paid"]
    tried = []

    def loop(model, *a, **k):
        tried.append(model)
        if model.endswith("@paid"):
            raise gemini.Busy(f"{model}: HTTP 429")             # paid quota / rate limit hit
        return {"ok": model}

    monkeypatch.setattr(gemini, "_loop", loop)
    out = gemini.run("s", "u", [], {"type": "object", "properties": {}}, None, models, paid_daily_max=5)
    assert out == {"ok": "flash"} and tried == ["pro@paid", "flash"]
    for _ in range(5):
        gemini._count_paid()
    assert gemini.chain(models) == ["flash"]                    # today's paid cap reached


def test_review_is_one_request_and_skips_used_up_models(tmp_path, monkeypatch):
    """Free tier = ~20 requests a day per model: a review costs ONE request (the second listening is
    done locally), and a model whose daily quota is gone isn't asked again until it resets."""
    import time as _t
    from src.agent import corrector, gemini
    monkeypatch.setattr(gemini, "USAGE", tmp_path / "usage.json")
    monkeypatch.setattr(gemini, "available", lambda paid=False: {"m1", "m2"})
    r = {"id": "c-1", "title": "", "sheikh_name": "", "segments": [
        {"type": "speech", "start": 0.0, "end": 2.0, "words": [{"w": "قال", "p": 0.9}, {"w": "الشيح", "p": 0.3}]}]}
    c = corrector.Corrector({"agent": {"gemini_models": ["m1", "m2"], "mode": "single"}}, r, tmp_path / "a.wav")
    monkeypatch.setattr(c, "relisten", lambda k, ctx, slow: json.dumps({"text": "قال الشيخ", "words": []}))
    monkeypatch.setattr(c, "quran_search", lambda text: "No Quran match.")
    calls = []

    def post(model, body, timeout):
        calls.append(model)
        assert "قال الشيخ" in body["contents"][0]["parts"][0]["text"]     # the second hearing is in the prompt
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(
            {"edits": [], "locked": [], "confidence": "high", "summary_ar": "تمام"})}]}}]}

    monkeypatch.setattr(gemini, "_post", post)
    assert c.ask()["confidence"] == "high" and calls == ["m1"]          # one request, no tool turns
    gemini._save_usage({"exhausted": {"m1": _t.time() + 3600}})
    assert gemini.chain(["m1", "m2"]) == ["m2"]                         # used-up model skipped
    gemini._save_usage({"exhausted": {"m1": _t.time() + 3600, "m2": _t.time() + 3600}})
    with pytest.raises(gemini.GeminiError, match="مشغولة"):              # all used up: retry tomorrow path
        gemini.chain(["m1", "m2"])


def test_report_and_tuning_compare_clip_looks(tmp_path, monkeypatch):
    """Link clips are compared by their look (device, framing, camera, title style); a look that
    does better gets picked more often — among clips only, never mixed with Quran videos."""
    from src import screens, stats
    monkeypatch.setattr(stats, "TUNING", tmp_path / "tuning.json")
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="minutes")
    data = {}
    for i in range(12):
        dev = "phone" if i % 2 else "tv"
        data[f"c-{i}"] = {"meta": {"kind": "clip", "style": "screen", "device": dev, "frame": "close",
                                   "camera": "push", "title_style": "plain", "length": "< 1 د", "posted": old},
                          "platforms": {"tiktok": {"views": 900 if dev == "phone" else 100}}}
    data["q-1"] = {"meta": {"kind": "quran", "posted": old}, "platforms": {"tiktok": {"views": 50}}}
    monkeypatch.setattr(stats, "read_json", lambda p, d: data if p == stats.PATH else d)
    text = stats.report({"reciters": {}}, 7)
    assert "🎬" in text and "موبايل" in text and "تلفزيون" in text
    stats.tune({}, data)
    t = json.loads((tmp_path / "tuning.json").read_text(encoding="utf-8"))
    assert t["device_weights"]["phone"] > 1 > t["device_weights"]["tv"]
    lib = {"p": {"portrait": False, "kind": "phone"}, "t": {"portrait": False, "kind": "tv"}}
    monkeypatch.setattr(screens, "library", lambda: lib)
    cfg = {"screens": {"tuning": {"device_weights": t["device_weights"]}}}
    picks = [screens.choose(cfg, random.Random(i), clip_portrait=False)["scene"] for i in range(400)]
    assert picks.count("p") > picks.count("t")      # the better device is chosen more often
