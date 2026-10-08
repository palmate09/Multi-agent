"""Shared pytest configuration.

Tests must be deterministic and fast, so local/cloud LLM backends are disabled
unless a test explicitly opts in. Without this, `generate()` would try Ollama
first with a 60s timeout per call and the suite would take many minutes
(or hang) on any machine where Ollama happens to be running.
"""

import os

os.environ.setdefault("SKIP_OLLAMA", "1")
os.environ.setdefault("AGENT_TEAM_TESTING", "1")
# A developer's .env must never influence a test run: a stray AUTH_PASSWORD_HASH
# in the working tree would silently enable auth (or break it) for every test.
# python-dotenv honours this flag to skip loading entirely.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
