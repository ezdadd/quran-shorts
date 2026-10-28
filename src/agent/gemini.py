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


def pick_model(preferred: str) -> str:
    """The configured model, or — if Google renamed/retired it — the newest Flash model available."""
    r = requests.get(f"{BASE}/models", headers={"x-goog-api-key": _key()}, params={"pageSize": 200}, timeout=30)
    r.raise_for_status()
    names = [m["name"].split("/", 1)[1] for m in r.json().get("models", [])
             if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    if preferred in names:
        return preferred
    flash = sorted((n for n in names if "flash" in n and "lite" not in n and "image" not in n
                    and "tts" not in n and "live" not in n), reverse=True)
    if not flash:
        raise GeminiError(f"مفيش موديل Gemini Flash متاح (المتاح: {names[:8]})")
    log.info("model %s not available, using %s", preferred, flash[0])
    return flash[0]


def _post(model: str, body: dict) -> dict:
    for attempt in range(4):
        r = requests.post(f"{BASE}/models/{model}:generateContent", json=body, timeout=180,
                          headers={"x-goog-api-key": _key(), "Content-Type": "application/json"})
        if r.status_code == 429 or r.status_code >= 500:     # free-tier rate limit / busy: wait
            wait = 20 * (attempt + 1)
            for d in (r.json().get("error", {}).get("details") or []) if r.headers.get(
                    "content-type", "").startswith("application/json") else []:
                if d.get("@type", "").endswith("RetryInfo"):
                    wait = max(wait, int(float(str(d.get("retryDelay", "0s")).rstrip("s")) + 1))
            log.warning("gemini %s, retrying in %ss", r.status_code, wait)
            time.sleep(min(wait, 90))
            continue
        if r.status_code == 404:
            raise FileNotFoundError(model)
        if not r.ok:
            raise GeminiError(f"Gemini {r.status_code}: {r.text[:300]}")
        return r.json()
    raise GeminiError("Gemini مشغول (rate limit) — هيتجرب تاني بعدين")


def run(system: str, user: str, tools: list[dict], result_schema: dict, call_tool, model: str,
        max_turns: int = 16) -> dict:
    """Agent loop. call_tool(name, args) -> str. Returns submit_result's arguments."""
    decls = [{"name": t["name"], "description": t["description"], "parameters": _schema(t["input_schema"])}
             for t in tools]
    decls.append({"name": "submit_result",
                  "description": "Call this once at the end with your final answer (only the lines you change).",
                  "parameters": _schema(result_schema)})
    contents = [{"role": "user", "parts": [{"text": user}]}]
    body = {"systemInstruction": {"parts": [{"text": system + "\n\nWhen you are done, call submit_result."}]},
            "tools": [{"functionDeclarations": decls}],
            "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
            "generationConfig": {"temperature": 0.2}}
    try:
        model = pick_model(model)
    except requests.RequestException as e:
        raise GeminiError(f"مش قادر أوصل لـ Gemini: {e}") from e
    for turn in range(max_turns):
        try:
            resp = _post(model, {**body, "contents": contents})
        except FileNotFoundError:
            raise GeminiError(f"الموديل {model} مش موجود")
        cands = resp.get("candidates") or []
        if not cands:
            raise GeminiError(f"مفيش رد: {resp.get('promptFeedback')}")
        cand = cands[0]
        usage = resp.get("usageMetadata") or {}
        log.info("gemini turn %d (%s): finish=%s in=%s out=%s", turn + 1, model, cand.get("finishReason"),
                 usage.get("promptTokenCount"), usage.get("candidatesTokenCount"))
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
