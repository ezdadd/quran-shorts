"""Subtitle-correction agent for lecture / link clips (free Gemini by default, Claude optional;
two tools + what it learned from your own corrections — see memory.py).

When you haven't touched a clip in Telegram for `agent.delay_hours`, the poller starts this on
GitHub Actions. Claude reads the numbered subtitle lines (Whisper's transcript, with the words it
was unsure of), can *listen again* to any line (Whisper re-run on that stretch with a context
prompt / slowed down) and *search the Quran*, then returns only the lines that need fixing.

Hard limits enforced in code, whatever the model says:
  * verified Quran segments are never shown as editable lines;
  * a line the agent marks as Quran or hadith is never changed;
  * an edit that rewrites most of a line (not a correction any more) is dropped;
  * auto-approval only when confidence >= agent.auto_approve_min and no Quran/hadith line is
    uncertain — otherwise the clip waits for you with the reason.
"""
from __future__ import annotations

import difflib
import json
import logging
import os
from pathlib import Path

from .memory import norm      # light key: spelling fixes (ابن القائم -> ابن القيم) count as changes
from ..review.edit import speech_segments

log = logging.getLogger("quran-shorts.agent")

SYSTEM = """You proofread the on-screen subtitles of short Islamic videos for the Arabic channel "ازْدَدْ".
Each clip is a sheikh speaking — usually Egyptian colloquial mixed with Modern Standard Arabic (fusha).
The numbered lines came from Whisper speech recognition and may contain mishearings.

Goal: every line shows exactly what the sheikh said, spelled correctly.

Rules
1. Keep the sheikh's own words and register. Colloquial stays colloquial (عشان، دلوقتي، إزاي، مش، بتاع،
   اللي، كده); fusha stays fusha. Never translate between them, paraphrase, summarize, add, drop or
   reorder words, and never "improve" his style or grammar.
2. Fix only: misheard words, wrong word boundaries, spelling (hamza, ة/ه, ى/ي, ال), repeated-word
   glitches, and Whisper hallucinations that were never said (channel slogans like "اشتركوا في القناة",
   "ترجمة نانسي قنقر", subtitle credits, music markers) — delete those lines.
3. No tashkeel and no punctuation; plain words like the existing lines.
4. The Quran and hadith are off limits. A line that quotes the Quran or the words of a hadith (the
   Prophet's ﷺ words, usually after "قال رسول الله" / "قال النبي ﷺ") must not be edited at all: list it
   under `locked` with its kind, and set `uncertain` to true when its recognized words look garbled or
   you are not sure they match the real text. Use quran_search when a line might be a Quran quote.
   Segments shown as [🔒 ...] are verified Quran text already shown on screen; they are not lines.
5. When a line has low-confidence words or does not make sense, call relisten on it before deciding.
   Give a short Arabic context_prompt (the previous line plus the topic); use slow=true for fast speech.
   Do not relisten lines that are already clear.
6. If you still cannot tell what was said, leave the line unchanged and lower your confidence.
7. confidence: "high" = every line now matches the audio; "medium" = small doubts in minor words;
   "low" = parts are unintelligible or a meaning could be wrong.

Return only the lines you change (replace or delete). Write `reason` and `summary_ar` in short
Egyptian Arabic."""

TOOLS = [
    {
        "name": "relisten",
        "description": "Run speech recognition again on one numbered line's audio, with a wider beam, "
                       "your context prompt, and optionally slowed to 0.85x. Returns the new text and a "
                       "confidence (0-1) per word. Use it for lines with unclear or suspicious words.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "line": {"type": "integer", "description": "Line number as shown"},
                "context_prompt": {"type": "string",
                                   "description": "Arabic text that primes recognition: previous line + topic"},
                "slow": {"type": "boolean", "description": "Slow the audio to 0.85x before listening"},
            },
            "required": ["line", "context_prompt", "slow"],
            "additionalProperties": False,
        },
    },
    {
        "name": "quran_search",
        "description": "Search the whole Quran for a phrase (spelling-insensitive). Returns matching ayah "
                       "references with their verified text, or nothing if the phrase is not Quran.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "The Arabic phrase as recognized"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
]

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "line": {"type": "integer"},
                "action": {"type": "string", "enum": ["replace", "delete"]},
                "text": {"type": "string", "description": "The corrected line (empty for delete)"},
                "reason": {"type": "string"},
            },
            "required": ["line", "action", "text", "reason"],
            "additionalProperties": False,
        }},
        "locked": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "line": {"type": "integer"},
                "kind": {"type": "string", "enum": ["quran", "hadith"]},
                "uncertain": {"type": "boolean"},
            },
            "required": ["line", "kind", "uncertain"],
            "additionalProperties": False,
        }},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "summary_ar": {"type": "string"},
    },
    "required": ["edits", "locked", "confidence", "summary_ar"],
    "additionalProperties": False,
}

LEVEL = {"low": 0, "medium": 1, "high": 2}


class AgentError(Exception):
    pass


def lines_prompt(r: dict) -> str:
    """The transcript as the agent sees it: numbered speech lines (same numbers as the Telegram
    "📝" message) with timings and unsure words, and locked Quran segments in between."""
    out, n = [], 0
    for s in r.get("segments") or []:
        if s.get("type") == "quran":
            out.append(f"[🔒 {s.get('ref', '')} آية موثقة: {s.get('text', '')}]")
            continue
        if s.get("type") != "speech":
            continue
        n += 1
        unsure = [w["w"] for w in s.get("words") or [] if w.get("p") is not None and w["p"] < 0.6]
        txt = s.get("text") or " ".join(w["w"] for w in s.get("words") or [])
        out.append(f"{n}) [{s['start']:.1f}-{s['end']:.1f}s] {txt}"
                   + (f"   ⟵ كلمات مش أكيدة: {'، '.join(unsure)}" if unsure else ""))
    return "\n".join(out)


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


class Corrector:
    def __init__(self, cfg, r: dict, audio: Path):
        self.cfg, self.r, self.audio = cfg, r, audio
        self.a = cfg.get("agent") or {}
        self.segs = speech_segments(r)
        self.calls = {"relisten": 0, "quran_search": 0}
        self._heard: dict[tuple, str] = {}

    # ---------------------------------------------------------------- tools
    def relisten(self, line: int, context_prompt: str, slow: bool) -> str:
        if not 1 <= line <= len(self.segs):
            return f"Error: no line {line} (lines are 1-{len(self.segs)})"
        key = (line, context_prompt, slow)
        if key in self._heard:          # same request again (e.g. after switching model): no re-run
            return self._heard[key]
        if len(self._heard) >= int(self.a.get("max_relisten", 12)):
            return "Error: relisten limit reached — decide with what you have"
        from .. import asr
        s = self.segs[line - 1]
        res = asr.transcribe_slice(self.cfg, self.audio, s["start"], s["end"], context_prompt, slow)
        self._heard[key] = json.dumps(res, ensure_ascii=False)
        return self._heard[key]

    def quran_search(self, text: str) -> str:
        from ..lectures import find_quotes, quote_text
        words = [{"w": w} for w in text.split()]
        hits = find_quotes(self.cfg, words, min_words=3)
        if not hits:
            return "No Quran match."
        return json.dumps([{"ref": quote_text(self.cfg, q)[2], "verified_text": quote_text(self.cfg, q)[0]}
                           for q in hits], ensure_ascii=False)

    def _run_tool(self, name: str, args: dict) -> str:
        self.calls[name] = self.calls.get(name, 0) + 1
        try:
            if name == "relisten":
                return self.relisten(int(args["line"]), str(args.get("context_prompt", "")),
                                     bool(args.get("slow", False)))
            if name == "quran_search":
                return self.quran_search(str(args["text"]))
            return f"Error: unknown tool {name}"
        except Exception as e:          # a tool failure goes back to the model, not up the stack
            log.warning("tool %s failed: %s", name, e)
            return f"Error: {e}"

    # ---------------------------------------------------------------- loop
    def user_prompt(self) -> str:
        from . import memory
        title = self.r.get("title") or ""
        who = self.r.get("sheikh_name") or "شيخ"
        ex = memory.examples(int(self.a.get("examples", 20)))
        refs = (f"\n\nتصحيحات عملها صاحب القناة بنفسه قبل كدا (اتعلم منها أسلوبه والأسماء الصح):\n{ex}"
                if ex else "")
        return (f"المتكلم: {who}\nالعنوان: {title or '—'}{refs}\n\nسطور الترجمة:\n{lines_prompt(self.r)}\n\n"
                "صحح السطور حسب القواعد ورجّع النتيجة.")

    def ask(self) -> dict:
        if self.a.get("provider", "gemini") == "gemini":
            from . import gemini
            try:
                return gemini.run(SYSTEM, self.user_prompt(), TOOLS, RESULT_SCHEMA, self._run_tool,
                                  list(self.a.get("gemini_models") or ["gemini-3.5-flash"]),
                                  int(self.a.get("max_turns", 16)), int(self.a.get("gemini_timeout", 90)),
                                  str(self.a.get("thinking", "low")), int(self.a.get("paid_daily_max", 150)))
            except gemini.GeminiError as e:
                raise AgentError(str(e)) from e
        return self._ask_claude()

    def _ask_claude(self) -> dict:
        import anthropic
        client = anthropic.Anthropic()
        messages = [{"role": "user", "content": self.user_prompt()}]
        tools = TOOLS          # non-streaming requests, so no eager_input_streaming
        for turn in range(int(self.a.get("max_turns", 16))):
            resp = client.beta.messages.create(
                model=self.a.get("model", "claude-opus-5-5"),
                max_tokens=16000,
                system=SYSTEM,
                tools=tools,
                messages=messages,
                output_config={"effort": self.a.get("effort", "high"),
                               "format": {"type": "json_schema", "schema": RESULT_SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            log.info("agent turn %d: stop=%s in=%s out=%s", turn + 1, resp.stop_reason,
                     resp.usage.input_tokens, resp.usage.output_tokens)
            if resp.stop_reason == "refusal":
                raise AgentError("الموديل رفض يكمل الطلب ده")
            if resp.stop_reason == "max_tokens":
                raise AgentError("الرد اتقطع (max_tokens)")
            if resp.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": resp.content})
                results = [{"type": "tool_result", "tool_use_id": b.id, "content": self._run_tool(b.name, b.input)}
                           for b in resp.content if b.type == "tool_use"]
                messages.append({"role": "user", "content": results})
                continue
            text = next((b.text for b in resp.content if b.type == "text"), "")
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                raise AgentError(f"رد مش مفهوم: {e}") from e
        raise AgentError("الوكيل خد لفات كتير من غير ما يخلص")

    # ---------------------------------------------------------------- policy
    def near_ayah(self) -> set[int]:
        """Speech line numbers directly before / after a verified Quran segment."""
        out, n, prev_quran = set(), 0, False
        for s in self.r.get("segments") or []:
            if s.get("type") == "quran":
                if n:
                    out.add(n)
                prev_quran = True
            elif s.get("type") == "speech":
                n += 1
                if prev_quran:
                    out.add(n)
                prev_quran = False
        return out

    def decide(self, out: dict) -> dict:
        """Turn the model's answer into edits we are willing to apply + approve / hold."""
        locked = {int(x["line"]): x for x in out.get("locked") or []}
        near = self.near_ayah()
        edits: dict[str, str | None] = {}
        changes, dropped = [], []
        touched_near = False
        for e in out.get("edits") or []:
            k = int(e["line"])
            if not 1 <= k <= len(self.segs):
                continue
            old = self.segs[k - 1].get("text") or " ".join(w["w"] for w in self.segs[k - 1]["words"])
            if k in locked:
                dropped.append(f"{k}: سطر {('آية' if locked[k]['kind'] == 'quran' else 'حديث')} — ممنوع يتعدل")
                continue
            if k in near:               # right next to a verified ayah: often the ayah itself, misheard
                dropped.append(f"{k}: جنب آية — ممكن يكون جزء منها، ممنوع يتعدل")
                touched_near = True
                continue
            if e["action"] == "delete":
                edits[str(k)] = None
                changes.append({"line": k, "old": old, "new": None, "reason": e.get("reason", "")})
                continue
            new = " ".join(e["text"].split())
            if not new or norm(new) == norm(old):
                continue
            unsure = any(w.get("p") is not None and w["p"] < 0.6 for w in self.segs[k - 1].get("words") or [])
            if _similar(old, new) < float(self.a.get("min_similarity", 0.5)) and not unsure:
                dropped.append(f"{k}: التعديل غيّر السطر كله — اتساب زي ما هو")
                continue
            edits[str(k)] = new
            changes.append({"line": k, "old": old, "new": new, "reason": e.get("reason", "")})
        conf = out.get("confidence", "low")
        need = self.a.get("auto_approve_min", "medium")
        risky = [x for x in locked.values() if x.get("uncertain")]
        if risky:
            decision, why = "hold", "فيه " + ("آية" if risky[0]["kind"] == "quran" else "حديث") + \
                " الكلام فيه مش واضح — لازم تراجعه بنفسك"
        elif touched_near:
            decision, why = "hold", "سطر جنب آية شكله غلط (ممكن يكون الآية نفسها متسمعة غلط) — لازم تراجعه بنفسك"
        elif LEVEL.get(conf, 0) < LEVEL.get(need, 1):
            decision, why = "hold", "الوكيل مش واثق كفاية من التصحيح"
        else:
            decision, why = "approve", ""
        return {"edits": edits, "changes": changes, "dropped": dropped, "locked": list(locked.values()),
                "confidence": conf, "summary": out.get("summary_ar", ""), "decision": decision, "why": why,
                "tool_calls": dict(self.calls)}


def correct(cfg, r: dict, audio: Path) -> dict:
    if (cfg.get("agent") or {}).get("provider", "gemini") == "gemini":
        if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
            raise AgentError("GEMINI_API_KEY مش متضاف")
    elif not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        raise AgentError("ANTHROPIC_API_KEY مش متضاف")
    c = Corrector(cfg, r, audio)
    return c.decide(c.ask())
