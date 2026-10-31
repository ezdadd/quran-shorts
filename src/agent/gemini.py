"""Free brain for the correction agent: Google Gemini API (free tier) over plain REST.

Function calling drives the loop: the model may call relisten / quran_search any number of times
and must finish by calling submit_result with the structured answer (mode ANY forces a function
call on every turn, so the answer always arrives as validated JSON arguments, never loose text).
"""
from __future__ import annotations

import json
import logging
import os
import time

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


def _key() -> str:
    k = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not k:
        raise GeminiError("GEMINI_API_KEY مش متضاف")
    return k


class Busy(Exception):
    """This model is overloaded / rate-limited / too slow right now — try the next one."""


def available() -> set[str]:
    r = requests.get(f"{BASE}/models", headers={"x-goog-api-key": _key()}, params={"pageSize": 200}, timeout=30)
    r.raise_for_status()
    return {m["name"].split("/", 1)[1] for m in r.json().get("models", [])
            if "generateContent" in (m.get("supportedGenerationMethods") or [])}


def chain(models: list[str]) -> list[str]:
    """The configured models that exist for this key, in order; if none do (Google renamed them),
    the newest non-lite Flash models."""
    names = available()
    out = [m for m in models if m in names]
    if not out:
        out = sorted((n for n in names if "flash" in n and not any(x in n for x in ("lite", "image", "tts", "live",
                                                                                   "omni", "preview"))),
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
    for attempt in range(2):
        try:
            r = requests.post(f"{BASE}/models/{model}:generateContent", json=body, timeout=timeout,
                              headers={"x-goog-api-key": _key(), "Content-Type": "application/json"})
        except requests.Timeout:
            raise Busy(f"{model}: no answer in {timeout}s")
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


def run(system: str, user: str, tools: list[dict], result_schema: dict, call_tool, models: list[str],
        max_turns: int = 16, timeout: int = 90, thinking: str = "low") -> dict:
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
