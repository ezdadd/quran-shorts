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
