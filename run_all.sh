#!/usr/bin/env bash
# Full verification run: executes every phase gate in order, one by one.
# Usage:
#   ./run_all.sh            # template mode (offline, fast)
#   ./run_all.sh --llm      # use local Ollama models
#   ./run_all.sh --docker   # run sandbox tests in Docker
#   ./run_all.sh --quick    # skip the slow --eval suite
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
echo "mode:  $([ "$USE_LLM" -eq 1 ] && echo 'local Ollama' || echo 'template (offline)')"
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

if [ "$USE_LLM" -eq 1 ]; then
  export SKIP_OLLAMA=0
  echo "SKIP_OLLAMA=0 (Ollama will be tried first)"
else
  export SKIP_OLLAMA=1
  echo "SKIP_OLLAMA=1 (forced template mode)"
fi

echo ""
echo "python: $($PY --version 2>&1)"
echo "ollama: $(command -v ollama >/dev/null && ollama --version 2>&1 | head -1 || echo 'not installed')"
echo "ollama reachable: $(curl -s -m 3 http://localhost:11434/api/version >/dev/null 2>&1 && echo yes || echo no)"

DOCKER_FLAG=""
[ "$USE_DOCKER" -eq 1 ] && DOCKER_FLAG="--docker"

step "Gate 0: unit + integration tests" $PY -m pytest -q

step "Phase 1: graph traversal (stub)" $PY cli.py --stub

step "Phase 2/3: single team run (template)" \
  $PY cli.py --run "task manager" --out outputs/verify_demo $DOCKER_FLAG

if [ "$USE_LLM" -eq 1 ]; then
  step "LLM connectivity" $PY backend/evals/check_llm.py
  step "Phase 2/3: single team run (local Ollama, slow)" \
    $PY cli.py --run "task manager" --out outputs/verify_llm --live $DOCKER_FLAG
fi

step "Phase 0: single-agent baseline" $PY backend/evals/baseline.py

step "Phase 4: reviewer recall on planted bugs" \
  $PY -m pytest -q backend/tests/test_reviewer.py

step "Phase 5: ablation, no tester" \
  $PY cli.py --run "task manager" --out outputs/verify_no_tester --no-tester

step "Phase 5: ablation, no reviewer" \
  $PY cli.py --run "task manager" --out outputs/verify_no_reviewer --no-reviewer

if [ "$QUICK" -eq 0 ]; then
  step "Phase 5: full eval suite" $PY cli.py --eval
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
echo "  outputs/verify_demo/summary.json"
echo "  outputs/baseline/result.json"
[ "$QUICK" -eq 0 ] && echo "  backend/evals/results.json"
echo ""
echo "Review findings (read these, they are not gates):"
for f in outputs/verify_demo/review.json outputs/verify_demo/ruff.txt outputs/verify_demo/bandit.txt; do
  [ -f "$f" ] && echo "  $f"
done

[ "$FAILED" -eq 0 ] || exit 1