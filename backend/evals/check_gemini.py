"""Fast connectivity check for the Gemini path.

Run this right after pasting a key. It tests each candidate model id in order
and reports which one answers, so a retired id or an exhausted free quota is
visible in a couple of seconds rather than after a six-minute pipeline run.

    python backend/evals/check_gemini.py
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def candidate_ids() -> list[str]:
    pinned = os.getenv("GEMINI_MODEL", "").strip()
    if pinned:
        return [pinned]
    raw = os.getenv("GEMINI_MODELS", "gemini-3-flash-preview,gemini-3.8-flash,gemini-flash-latest")
    return [m.strip() for m in raw.split(",") if m.strip()]


def probe(key: str, model: str, timeout: int = 40) -> tuple[bool, str, float]:
    # Gemini 3.x flash models emit reasoning tokens before the answer, so a tiny
    # cap returns finishReason=MAX_TOKENS with empty content and looks like a
    # failure even though the model is healthy.
    payload = json.dumps(
        {
            "contents": [{"parts": [{"text": "Reply with exactly: ok"}]}],
            "generationConfig": {"maxOutputTokens": 512, "temperature": 0},
        }
    ).encode()
    req = urllib.request.Request(
        f"{BASE}/{model}:generateContent?key={key}",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        return False, f"HTTP {e.code}: {body}", time.time() - t0
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", time.time() - t0
    try:
        text = d["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return False, f"unexpected response: {str(d)[:200]}", time.time() - t0
    return True, text.strip()[:40], time.time() - t0


def main() -> int:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        print("GEMINI_API_KEY is not set.")
        print("Add it to .env, or export it, then run this again.")
        return 1
    print(f"key present ({len(key)} chars, prefix {key[:6]}...)")
    print("candidate model ids:")
    for m in candidate_ids():
        print(f"  - {m}")

    working: list[str] = []
    for model in candidate_ids():
        ok, detail, took = probe(key, model)
        print(f"  {model:34s} {'OK ' if ok else 'FAIL'} {took:5.1f}s  {detail}")
        if ok:
            working.append(model)

    if not working:
        print("\nNo model id answered.")
        print("  - 429 means the free-tier quota is exhausted (resets daily).")
        print("  - 404 means the id is retired; check the current ids at")
        print("    https://aistudio.google.com/app/apikey")
        return 1

    print(f"\nworking ids: {working}")
    first = working[0]
    if first not in candidate_ids():
        pass
    print(f"pin it with:  GEMINI_MODEL={first}")
    if len(working) > 1:
        print(f"or keep the fallback list: GEMINI_MODELS={','.join(working)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
