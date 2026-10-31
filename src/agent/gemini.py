"""Brain for the correction agent: Google Gemini API over plain REST.

Two keys: GEMINI_API_KEY (free tier) and, optionally, GEMINI_API_KEY_PAID (a project with billing).
A model listed as "name@paid" in agent.gemini_models uses the paid key; when that key is missing,
busy, out of quota or past agent.paid_daily_max requests today, the chain simply moves on to the
next model — the free ones — automatically.

Function calling drives the loop: the model may call relisten / quran_search any number of times
and must finish by calling submit_result with the structured answer (mode ANY forces a function
call on every turn, so the answer always arrives as validated JSON arguments, never loose text).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import requests

log = logging.getLogger("quran-shorts.agent.gemini")
BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiError(Exception):
    pass


def _schema(s: dict) -> dict:
    """JSON-Schema (as used for the Claude tools) -> Gemini's OpenAPI subset."""
    out: dict = {}
    for k, v in s.items():
        if k == "type":
            out["type"] = v.upper()
        elif k == "properties":
            out["properties"] = {n: _schema(p) for n, p in v.items()}
        elif k == "items":
            out["items"] = _schema(v)
        elif k in ("required", "enum", "description"):
            out[k] = v
    return out


def _key(paid: bool = False) -> str:
    k = os.environ.get("GEMINI_API_KEY_PAID") if paid else \
        (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    if not k:
        raise GeminiError("GEMINI_API_KEY_PAID مش متضاف" if paid else "GEMINI_API_KEY مش متضاف")
    return k


def _split(entry: str) -> tuple[str, bool]:
    """"gemini-3.1-pro-preview@paid" -> ("gemini-3.1-pro-preview", True)"""
    name, _, tag = entry.partition("@")
    return name, tag == "paid"


# today's paid requests, kept with the bot's records so every job counts towards the same cap
USAGE = Path(__file__).resolve().parents[2] / "data" / "gemini_usage.json"
PAID_DAILY_MAX = 150


def _usage() -> dict:
    try:
        return json.loads(USAGE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_usage(u: dict) -> None:
    USAGE.parent.mkdir(parents=True, exist_ok=True)
    USAGE.write_text(json.dumps(u), encoding="utf-8")


def _paid_used() -> int:
    u = _usage()
    return int(u.get("paid", 0)) if u.get("date") == time.strftime("%Y-%m-%d", time.gmtime()) else 0


def _count_paid() -> None:
    u = _usage()
    u.update(paid=_paid_used() + 1, date=time.strftime("%Y-%m-%d", time.gmtime()))
    _save_usage(u)


def exhausted() -> set[str]:
    """Models whose free DAILY quota is used up (Google said so) — not tried again until it resets."""
    now = time.time()
    return {m for m, until in (_usage().get("exhausted") or {}).items() if until > now}


def _mark_exhausted(model: str, r: requests.Response) -> bool:
    """A 429 for the per-day quota (not the per-minute one): remember until Google's retry time."""
    try:
        details = r.json().get("error", {}).get("details", [])
    except ValueError:
        return False
    if not any("PerDay" in (v.get("quotaId") or "") for d in details for v in d.get("violations", [])):
        return False
    delay = next((d["retryDelay"] for d in details if "retryDelay" in d), "3600s")
    u = _usage()
    u.setdefault("exhausted", {})[model] = time.time() + float(str(delay).rstrip("s") or 3600)
    _save_usage(u)
    log.info("%s: free daily quota used up — skipped until it resets", model)
    return True


class Busy(Exception):
    """This model is overloaded / rate-limited / too slow right now — try the next one."""


def available(paid: bool = False) -> set[str]:
    r = requests.get(f"{BASE}/models", headers={"x-goog-api-key": _key(paid)}, params={"pageSize": 200}, timeout=30)
    r.raise_for_status()
    return {m["name"].split("/", 1)[1] for m in r.json().get("models", [])
            if "generateContent" in (m.get("supportedGenerationMethods") or [])}


def chain(models: list[str]) -> list[str]:
    """The configured models that exist for this key, in order; if none do (Google renamed them),
    the newest non-lite Flash models."""
    names = available()
    paid_names: set[str] = set()
    if os.environ.get("GEMINI_API_KEY_PAID") and any(_split(m)[1] for m in models):
        if _paid_used() >= PAID_DAILY_MAX:
            log.info("paid Gemini: today's cap of %d requests reached — free models only", PAID_DAILY_MAX)
        else:
            try:
                paid_names = available(paid=True)
            except requests.RequestException as e:
                log.warning("paid Gemini key unusable (%s) — free models only", e)
    used_up = exhausted()
    out = [m for m in models if m not in used_up and (_split(m)[0] in paid_names if _split(m)[1] else m in names)]
    if not out and any(m in used_up for m in models):
        raise GeminiError("كل موديلات Gemini مشغولة دلوقتي (الحصة المجانية اليومية خلصت) — هيتجرب تاني بعدين")
    if not out:
        out = sorted((n for n in names if "flash" in n and n not in used_up and
                      not any(x in n for x in ("lite", "image", "tts", "live", "omni", "preview"))),
                     reverse=True)[:3]
        log.info("configured models unavailable, using %s", out)
    if not out:
        raise GeminiError("مفيش موديل Gemini Flash متاح للمفتاح ده")
    return out


def _thinking(model: str, level: str = "low") -> dict:
    # agent.thinking in config.yaml (low / medium / high); medium checks hard lines more carefully
    budget = {"low": 1024, "medium": 4096, "high": 8192}.get(level, 1024)
    return {"thinkingBudget": budget} if model.startswith("gemini-2.") else {"thinkingLevel": level}


def _post(model: str, body: dict, timeout: int) -> dict:
    name, paid = _split(model)
    for attempt in range(2):
        if paid:
            if _paid_used() >= PAID_DAILY_MAX:
                raise Busy(f"{model}: today's paid cap reached")
            _count_paid()
        try:
            r = requests.post(f"{BASE}/models/{name}:generateContent", json=body, timeout=timeout,
                              headers={"x-goog-api-key": _key(paid), "Content-Type": "application/json"})
        except requests.Timeout:
            raise Busy(f"{model}: no answer in {timeout}s")
        if r.status_code == 429 and _mark_exhausted(model, r):
            raise Busy(f"{model}: daily quota used up")      # no point waiting 10 s and asking again
        if r.status_code == 429 or r.status_code >= 500:     # free-tier rate limit / overloaded
            if attempt == 0:
                log.warning("gemini %s on %s, retrying once in 10s", r.status_code, model)
                time.sleep(10)
                continue
            raise Busy(f"{model}: HTTP {r.status_code}")
        if r.status_code == 404:
            raise Busy(f"{model}: not available")
        if not r.ok:
            raise GeminiError(f"Gemini {r.status_code}: {r.text[:300]}")
        return r.json()
    raise Busy(model)


def _loop(model: str, body: dict, user: str, call_tool, max_turns: int, timeout: int,
          thinking: str = "low") -> dict:
    body = {**body, "generationConfig": {**body["generationConfig"], "thinkingConfig": _thinking(model, thinking)}}
    contents = [{"role": "user", "parts": [{"text": user}]}]
    for turn in range(max_turns):
        t0 = time.time()
        resp = _post(model, {**body, "contents": contents}, timeout)
        cands = resp.get("candidates") or []
        if not cands:
            raise GeminiError(f"مفيش رد: {resp.get('promptFeedback')}")
        cand = cands[0]
        usage = resp.get("usageMetadata") or {}
        log.info("gemini turn %d (%s, %.0fs): finish=%s in=%s out=%s", turn + 1, model, time.time() - t0,
                 cand.get("finishReason"), usage.get("promptTokenCount"), usage.get("candidatesTokenCount"))
        content = cand.get("content") or {}
        calls = [p["functionCall"] for p in content.get("parts") or [] if p.get("functionCall")]
        if not calls:
            if cand.get("finishReason") not in (None, "STOP"):
                raise GeminiError(f"الرد وقف: {cand.get('finishReason')}")
            text = "".join(p.get("text", "") for p in content.get("parts") or [])
            try:                       # rare: answered in text instead of calling submit_result
                return json.loads(text.strip().removeprefix("```json").removesuffix("```"))
            except json.JSONDecodeError:
                contents += [content or {"role": "model", "parts": [{"text": text or "..."}]},
                             {"role": "user", "parts": [{"text": "Call submit_result with your final answer."}]}]
                continue
        final = next((c for c in calls if c["name"] == "submit_result"), None)
        if final:
            return final.get("args") or {}
        contents.append(content)       # keeps thought signatures, as the API requires
        parts = []
        for c in calls:
            fr = {"name": c["name"], "response": {"result": call_tool(c["name"], c.get("args") or {})}}
            if c.get("id"):
                fr["id"] = c["id"]
            parts.append({"functionResponse": fr})
        contents.append({"role": "user", "parts": parts})
    raise GeminiError("الوكيل خد لفات كتير من غير ما يخلص")


def ask_json(system: str, user: str, result_schema: dict, models: list[str], timeout: int = 90,
             thinking: str = "low", paid_daily_max: int | None = None) -> dict:
    """ONE request: the answer as JSON matching result_schema (no tools, no back-and-forth).
    A busy / out-of-quota model hands over to the next one in the chain."""
    global PAID_DAILY_MAX
    if paid_daily_max is not None:
        PAID_DAILY_MAX = paid_daily_max
    try:
        order = chain(models)
    except requests.RequestException as e:
        raise GeminiError(f"مش قادر أوصل لـ Gemini: {e}") from e
    busy = []
    for model in order:
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json",
                                     "responseSchema": _schema(result_schema),
                                     "thinkingConfig": _thinking(_split(model)[0], thinking)}}
        t0 = time.time()
        try:
            resp = _post(model, body, timeout)
        except Busy as e:
            log.warning("switching model: %s", e)
            busy.append(str(e))
            continue
        try:
            text = "".join(p.get("text", "") for p in resp["candidates"][0]["content"]["parts"])
            out = json.loads(text)
        except (KeyError, IndexError, ValueError) as e:
            raise GeminiError(f"{model}: رد مش مفهوم ({e})") from e
        u = resp.get("usageMetadata") or {}
        log.info("gemini one-shot (%s, %.0fs): in=%s out=%s", model, time.time() - t0,
                 u.get("promptTokenCount"), u.get("candidatesTokenCount"))
        return out
    raise GeminiError("كل موديلات Gemini مشغولة دلوقتي (" + "؛ ".join(busy) + ") — هيتجرب تاني بعدين")


def run(system: str, user: str, tools: list[dict], result_schema: dict, call_tool, models: list[str],
        max_turns: int = 16, timeout: int = 90, thinking: str = "low", paid_daily_max: int | None = None) -> dict:
    """Agent loop over a chain of models: a busy / slow / retired model hands over to the next one
    (from the start of the conversation — thought signatures belong to one model).
    call_tool(name, args) -> str. Returns submit_result's arguments."""
    decls = [{"name": t["name"], "description": t["description"], "parameters": _schema(t["input_schema"])}
             for t in tools]
    decls.append({"name": "submit_result",
                  "description": "Call this once at the end with your final answer (only the lines you change).",
                  "parameters": _schema(result_schema)})
    body = {"systemInstruction": {"parts": [{"text": system + "\n\nWhen you are done, call submit_result."}]},
            "tools": [{"functionDeclarations": decls}],
            "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
            "generationConfig": {"temperature": 0.2}}
    global PAID_DAILY_MAX
    if paid_daily_max is not None:
        PAID_DAILY_MAX = paid_daily_max
    try:
        order = chain(models)
    except requests.RequestException as e:
        raise GeminiError(f"مش قادر أوصل لـ Gemini: {e}") from e
    busy = []
    for model in order:
        try:
            return _loop(model, body, user, call_tool, max_turns, timeout, thinking)
        except Busy as e:
            log.warning("switching model: %s", e)
            busy.append(str(e))
    raise GeminiError("كل موديلات Gemini مشغولة دلوقتي (" + "؛ ".join(busy) + ") — هيتجرب تاني بعدين")
