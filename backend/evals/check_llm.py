"""Report which LLM backends are configured and whether one actually answers.

Run this after changing keys or providers. It is the fastest way to tell the
failure modes apart, which need opposite fixes:

  * key missing/empty      -> the provider declines immediately
  * HTTP 429               -> quota exhausted; resets daily
  * HTTP 404 on a model id -> that id is retired; update the model list
  * connection refused     -> nothing is listening (Ollama not started)
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

from agents.llm import generate, role_model, role_num_predict


def probe_ollama() -> tuple[bool, list[str]]:
    url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    try:
        with urllib.request.urlopen(f"{url}/api/tags", timeout=3) as r:
            data = json.loads(r.read().decode())
        return True, sorted(m.get("name", "") for m in data.get("models", []))
    except Exception:
        return False, []


def main() -> int:
    skip = os.getenv("SKIP_OLLAMA", "") == "1"
    prefer_local = os.getenv("PREFER_LOCAL_ONLY", "") == "1"
    print(
        f"SKIP_OLLAMA={os.getenv('SKIP_OLLAMA')}  PREFER_LOCAL_ONLY={os.getenv('PREFER_LOCAL_ONLY')}"
    )

    reachable, models = probe_ollama()
    print(f"ollama reachable: {reachable}")
    for m in models:
        print(f"  pulled: {m}")
    print("role models:")
    for role in ("pm", "designer", "developer", "tester", "reviewer"):
        model = role_model(role)
        present = any(m == model or m.split(":")[0] == model.split(":")[0] for m in models)
        flag = "ok" if present else "MISSING"
        print(f"  {role:10s} {model:32s} num_predict={role_num_predict(role):5d}  [{flag}]")

    from agents.llm import cloud_order

    provider = os.getenv("LLM_PROVIDER", "auto").strip().lower()
    print(f"LLM_PROVIDER={provider}")
    hosted = [
        n
        for n in (
            "GEMINI_API_KEY",
            "GROQ_API_KEY",
            "OPENROUTER_API_KEY",
            "GITHUB_TOKEN",
            "HF_TOKEN",
            "OPENAI_API_KEY",
        )
        if os.getenv(n)
    ]
    print(f"hosted keys present: {hosted or 'none'}")
    if prefer_local:
        print("  (PREFER_LOCAL_ONLY=1, so hosted keys will not be used)")
    wanted = [n for _, n in cloud_order()]
    print(f"tiers to try: {wanted or '(none - local only)'}")
    if hosted and provider not in ("local", "auto"):
        needed = {
            "gemini": "GEMINI_API_KEY",
            "groq/free": "GROQ_API_KEY",
            "openrouter/free": "OPENROUTER_API_KEY",
            "github-models": "GITHUB_TOKEN",
            "huggingface": "HF_TOKEN",
        }.get(wanted[0] if wanted else "")
        if needed and needed not in hosted:
            print(f"  WARNING: LLM_PROVIDER={provider} needs {needed}, which is not set")

    if skip and provider == "local":
        print(
            "\nSKIP_OLLAMA=1 together with LLM_PROVIDER=local: no backend at all. "
            "Runs will be 'blocked'. Either set SKIP_OLLAMA=0 or pick a hosted provider."
        )
        return 1
    if skip:
        print("\nSKIP_OLLAMA=1: local models disabled; generation goes to the hosted tier.")
    if not reachable and provider == "local":
        print("\nno Ollama server. Start it with `ollama serve`, or set SKIP_OLLAMA=0.")
        return 1

    # Measure local throughput when local is actually in play.
    url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    if provider != "local" or skip:
        _smoke()
        return 0
    for role in ("developer", "designer"):
        model = role_model(role)
        body = json.dumps(
            {
                "model": model,
                "prompt": "Write a Python function that reverses a string. Code only.",
                "stream": False,
                "options": {"num_predict": 120, "temperature": 0.1},
            }
        ).encode()
        t0 = time.time()
        try:
            with urllib.request.urlopen(
                urllib.request.Request(
                    f"{url}/api/generate", data=body, headers={"Content-Type": "application/json"}
                ),
                timeout=300,
            ) as r:
                d = json.loads(r.read().decode())
        except Exception as exc:
            print(f"{role:10s} {model:32s} FAILED: {type(exc).__name__}: {exc}")
            continue
        elapsed = time.time() - t0
        n = d.get("eval_count", 0)
        secs = max(d.get("eval_duration", 1) / 1e9, 1e-6)
        print(f"{role:10s} {model:32s} {n / secs:5.2f} tok/s  ({elapsed:.0f}s for {n} tokens)")

    _smoke()
    return 0


def _smoke() -> int:
    """One real call through the configured routing, with the error surfaced."""
    text, meta = generate("Reply with exactly: hello-ok", "You are a test.", role="pm")
    print(f"\nsmoke call: model={meta['model']} ok={meta['ok']} latency={meta['latency']}s")
    if not text:
        for e in meta.get("errors") or []:
            print(f"  - {e}")
        return 1
    print(f"response: {text.strip()[:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
