"""Minimal Google Gemini REST client with function calling (no SDK needed)."""
from __future__ import annotations

import time

import httpx

from . import config

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiUnavailable(RuntimeError):
    pass


def generate(contents: list[dict], system: str, tools: list[dict], temperature: float = 0.2) -> tuple[dict, str]:
    """Call the first model that answers. Returns (candidate content, model id)."""
    key = config.gemini_key()
    if not key:
        raise GeminiUnavailable("no GEMINI_API_KEY")
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
        "tools": [{"functionDeclarations": tools}],
        "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
        "generationConfig": {"temperature": temperature},
    }
    last = ""
    for model in config.gemini_models():
        for attempt in range(2):
            try:
                r = httpx.post(API.format(model=model), json=body, headers={"x-goog-api-key": key}, timeout=60)
            except httpx.HTTPError as e:
                last = f"{model}: {e}"
                break
            if r.status_code in (429, 500, 503) and attempt == 0:
                time.sleep(1.5)
                continue
            if r.status_code != 200:
                last = f"{model}: HTTP {r.status_code}" + (" rate limit" if r.status_code == 429 else " overloaded" if r.status_code == 503 else "")
                break
            d = r.json()
            cands = d.get("candidates") or []
            if not cands or "content" not in cands[0]:
                last = f"{model}: empty candidate {str(d)[:200]}"
                break
            content = cands[0]["content"]
            content.setdefault("role", "model")
            content.setdefault("parts", [])
            return content, model
    raise GeminiUnavailable(last or "no model answered")


def function_calls(content: dict) -> list[dict]:
    return [p["functionCall"] for p in content.get("parts", []) if "functionCall" in p]


def text_of(content: dict) -> str:
    return "".join(p.get("text", "") for p in content.get("parts", []) if not p.get("thought")).strip()


def function_response(call: dict, result: dict) -> dict:
    fr = {"name": call["name"], "response": {"result": result}}
    if call.get("id"):
        fr["id"] = call["id"]
    return {"functionResponse": fr}
