"""
gemini_pool.py  (put next to main.py in /backend)

Calls Gemini with several API keys: round-robin, a short cooldown for a key that hits 429 / is rejected,
and automatic fall-through to the next key and the next model.

NOTE: Google counts rate limits per PROJECT, not per key. Keys that belong to the same project share one quota.
Env (read at call time, so .env loaded later by main.py still works):
  GEMINI_API_KEYS = key1,key2,key3      (comma / space / newline separated)
  GEMINI_API_KEY  = single key          (still supported)
  GEMINI_MODEL    = optional preferred model name
"""
import asyncio
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_cool: Dict[str, float] = {}      # key -> unix time until which we skip it
_rr = 0                           # round-robin counter
_sem: Optional[asyncio.Semaphore] = None


def load_keys() -> List[str]:
    raw = (os.getenv("GEMINI_API_KEYS", "") + "," + os.getenv("GEMINI_API_KEY", ""))
    return list(dict.fromkeys(k for k in re.split(r"[,\s;]+", raw) if k))


def models() -> List[str]:
    return list(dict.fromkeys(
        m for m in [os.getenv("GEMINI_MODEL", "").strip(), "gemini-3.5-flash", "gemini-2.5-flash"] if m))


def parse_json_loose(txt: str) -> Optional[Any]:
    """Accepts plain JSON, ```json fenced JSON, or JSON with chatter around it."""
    if not txt:
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt.strip(), flags=re.I).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except Exception:
            return None
    return None


def _text_of(data: Dict[str, Any]) -> str:
    cands = data.get("candidates") or []
    if not cands:
        return ""
    parts = (cands[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))


def _sources_of(data: Dict[str, Any]) -> List[Dict[str, str]]:
    cands = data.get("candidates") or []
    gm = (cands[0].get("groundingMetadata") if cands else None) or {}
    out, seen = [], set()
    for ch in gm.get("groundingChunks") or []:
        w = (ch or {}).get("web") or {}
        url = str(w.get("uri") or "")
        if url and url not in seen:
            seen.add(url)
            out.append({"title": str(w.get("title") or "")[:120], "url": url})
    return out[:8]


async def ask(prompt: str, *, grounded: bool = False, timeout: float = 60.0
              ) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, str]], str]:
    """Returns (json_object | None, sources, error_text).
    grounded=True  -> Google Search tool on (JSON mode is not allowed together with it, so JSON is parsed loosely).
    grounded=False -> plain JSON mode."""
    global _rr, _sem
    keys = load_keys()
    if not keys:
        return None, [], "Gemini key not set (GEMINI_API_KEYS)"
    if _sem is None:
        _sem = asyncio.Semaphore(2)          # at most 2 Gemini calls in flight, protects RPM
    n = len(keys)
    start = _rr % n
    _rr += 1
    order = [keys[(start + i) % n] for i in range(n)]
    now = time.time()
    ready = [k for k in order if _cool.get(k, 0) <= now] or order      # all cooling -> try anyway
    last = "no response"
    async with _sem:
        async with httpx.AsyncClient() as client:
            for model in models():
                for key in ready:
                    body: Dict[str, Any] = {"contents": [{"parts": [{"text": prompt}]}],
                                            "generationConfig": {"temperature": 0.2}}
                    if grounded:
                        body["tools"] = [{"google_search": {}}]
                    else:
                        body["generationConfig"]["responseMimeType"] = "application/json"
                    try:
                        r = await client.post(_URL.format(model=model), headers={"x-goog-api-key": key},
                                              json=body, timeout=timeout)
                    except Exception as exc:
                        last = f"{model}: {type(exc).__name__}"
                        continue
                    if r.status_code == 200:
                        try:
                            data = r.json()
                        except Exception:
                            last = f"{model}: bad response body"
                            continue
                        obj = parse_json_loose(_text_of(data))
                        if isinstance(obj, dict):
                            return obj, (_sources_of(data) if grounded else []), ""
                        last = f"{model}: reply was not JSON"
                        continue
                    last = f"{model}: HTTP {r.status_code} {r.text[:100]}"
                    if r.status_code == 429:
                        _cool[key] = time.time() + 60
                        continue
                    if r.status_code in (401, 403) or (r.status_code == 400 and "API key" in r.text):
                        _cool[key] = time.time() + 600
                        continue
                    if r.status_code in (400, 404):      # request / model problem: another key will not help
                        break
                    # 5xx etc: try the next key
    return None, [], last
