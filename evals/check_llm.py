"""Check which LLM backends are configured (no model pull)."""
import os, sys
sys.path.insert(0, ".")
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass
from agents.llm import generate

print(f"SKIP_OLLAMA={os.getenv('SKIP_OLLAMA')}")
for name in ["GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "GITHUB_TOKEN", "HF_TOKEN", "OPENAI_API_KEY"]:
    v = os.getenv(name, "")
    print(f"{name}={'set (' + str(len(v)) + ' chars)' if v else 'missing'}")
os.environ["SKIP_OLLAMA"] = os.getenv("SKIP_OLLAMA", "1")  # check hosted path
text, meta = generate("Reply with: hosted-ok", "You are a test.", role="pm", timeout=30)
print(f"result model={meta['model']} empty={not bool(text)}")
if text:
    print("response:", text[:200])
else:
    print("-> no hosted key reachable, template mode (offline E2E still passes). Add one key from .env.example.")
