#!/usr/bin/env bash
# Full verification run: executes every gate in order, one by one.
#
#   ./run_all.sh --llm      # required for generation gates (uses $LLM_PROVIDER)
#   ./run_all.sh --llm --docker   # also run the sandbox inside Docker
#   ./run_all.sh --quick --llm    # skip the slow --eval suite
#
# There is no template mode any more: generation needs a real model, so the
# generation gates are skipped (and reported as skipped) without --llm rather
# than silently passing against a built-in fallback.
set -uo pipefail

cd "$(dirname "$0")"
ROOT="$PWD"

USE_LLM=0
USE_DOCKER=0
QUICK=0
for arg in "$@"; do
  case "$arg" in
    --llm) USE_LLM=1 ;;
    --docker) USE_DOCKER=1 ;;
    --quick) QUICK=1 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

PASSED=0
FAILED=0
declare -a RESULTS=()

step() {
  local name="$1"; shift
  echo ""
  echo "=============================================================="
  echo ">> $name"
  echo "=============================================================="
  local start=$SECONDS
  if "$@"; then
    local dur=$((SECONDS - start))
    echo "-- OK: $name (${dur}s)"
    RESULTS+=("PASS  ${dur}s  $name")
    PASSED=$((PASSED + 1))
  else
    local dur=$((SECONDS - start))
    echo "-- FAIL: $name (${dur}s, exit $?)"
    RESULTS+=("FAIL  ${dur}s  $name")
    FAILED=$((FAILED + 1))
  fi
}

echo "=============================================================="
echo " Multi-Agent Software Team - full verification"
echo "=============================================================="
echo "root:  $ROOT"
if [ "$USE_LLM" -eq 1 ]; then
  echo "provider: ${LLM_PROVIDER:-gemini}"
  echo "  (set LLM_PROVIDER=local to use Ollama instead)"
else
  echo "mode:  offline (no generation gates; pass --llm to include them)"
fi
[ "$USE_DOCKER" -eq 1 ] && echo "sandbox: docker"

if [ ! -x .venv/bin/python ]; then
  echo ""
  echo "No .venv found. Creating one..."
  python3 -m venv .venv || { echo "venv creation failed"; exit 1; }
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r backend/requirements.txt || {
    echo "dependency install failed"; exit 1; }
  echo "Dependencies installed."
fi

PY=".venv/bin/python"

# The provider decides where generation goes; --llm only says "generate at all".
# LLM_PROVIDER=gemini means a hosted key, so SKIP_OLLAMA must not be forced.
if [ "$USE_LLM" -eq 1 ]; then
  export LLM_PROVIDER="${LLM_PROVIDER:-gemini}"
  if [ "$LLM_PROVIDER" = "local" ]; then
    export SKIP_OLLAMA=0
    export PREFER_LOCAL_ONLY=1
    export OLLAMA_TIMEOUT="${OLLAMA_TIMEOUT:-600}"
  else
    export SKIP_OLLAMA=1
    export PREFER_LOCAL_ONLY=0
  fi
  echo "provider: $LLM_PROVIDER (skip_ollama=$SKIP_OLLAMA)"
else
  export SKIP_OLLAMA=1
  echo "no generation requested; pass --llm (generation gates report 'blocked')"
fi

echo ""
echo "python: $($PY --version 2>&1)"
echo "ollama: $(command -v ollama >/dev/null && ollama --version 2>&1 | head -1 || echo 'not installed')"
echo "ollama reachable: $(curl -s -m 3 http://localhost:11434/api/version >/dev/null 2>&1 && echo yes || echo no)"

DOCKER_FLAG=""
[ "$USE_DOCKER" -eq 1 ] && DOCKER_FLAG="--docker"

step "Gate 0: unit + integration tests" $PY -m pytest -q

step "Phase 1: graph traversal (stub)" $PY cli.py --stub

step "Phase 4: reviewer recall on planted bugs" \
  $PY -m pytest -q backend/tests/test_reviewer.py

step "Phase 4: domain coverage gate" \
  $PY -m pytest -q backend/tests/test_coverage.py backend/tests/test_introspect.py

skip() {
  RESULTS+=("SKIP  --     $1 (needs --llm)")
}

if [ "$USE_LLM" -eq 1 ]; then
  step "LLM connectivity" $PY backend/evals/check_llm.py

  step "Phase 2/3: novel domain (library)" \
    $PY cli.py --run "Build a REST API for a library that lends books. GET /health, POST /loans to borrow an ISBN, GET /books. 404 on unknown ISBN." \
    --out outputs/verify_library --live $DOCKER_FLAG

  step "Phase 2/3: novel domain (recipes)" \
    $PY cli.py --run "Build a REST API for a recipe collection. GET /health, POST /recipes, GET /recipes. 404 on unknown id." \
    --out outputs/verify_recipes --live $DOCKER_FLAG

  step "Phase 2/3: novel domain (inventory)" \
    $PY cli.py --run "Build a REST API for a warehouse inventory system. GET /health, POST /products, GET /products, PUT /products/{sku}. 404 unknown SKU." \
    --out outputs/verify_inventory --live $DOCKER_FLAG

  step "Phase 0: single-agent baseline" $PY backend/evals/baseline.py

  step "Phase 5: ablation, no tester" \
    $PY cli.py --run "Build a REST API for a recipe collection. GET /health, POST /recipes, GET /recipes." \
    --out outputs/verify_no_tester --no-tester --live

  step "Phase 5: ablation, no reviewer" \
    $PY cli.py --run "Build a REST API for a recipe collection. GET /health, POST /recipes, GET /recipes." \
    --out outputs/verify_no_reviewer --no-reviewer --live

  if [ "$QUICK" -eq 0 ]; then
    step "Phase 5: full eval suite" $PY cli.py --eval
  fi
else
  skip "Phase 2/3: novel-domain generation"
  skip "Phase 0: single-agent baseline"
  skip "Phase 5: ablations"
  skip "Phase 5: full eval suite"
fi

echo ""
echo "=============================================================="
echo " Summary"
echo "=============================================================="
for r in "${RESULTS[@]}"; do echo "  $r"; done
echo ""
echo "  passed: $PASSED   failed: $FAILED"
echo ""
echo "Artifacts:"
for d in outputs/verify_library outputs/verify_recipes outputs/verify_inventory outputs/baseline; do
  [ -f "$d/summary.json" ] && echo "  $d/summary.json"
done
[ "$USE_LLM" -eq 1 ] && [ "$QUICK" -eq 0 ] && echo "  backend/evals/results.json"
echo ""
echo "Review findings (read these, they are not gates):"
for f in outputs/verify_library/review.json outputs/verify_library/ruff.txt outputs/verify_library/bandit.txt; do
  [ -f "$f" ] && echo "  $f"
done

[ "$FAILED" -eq 0 ] || exit 1