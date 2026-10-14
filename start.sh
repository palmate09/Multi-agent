#!/usr/bin/env bash
#
# One command to bring the whole platform up, and to take it down again.
#
#   ./start.sh              build if needed, start, wait until healthy, print URLs
#   ./start.sh --rebuild    force a rebuild of every image first
#   ./start.sh --db         also start Postgres (revocable sessions, durable limits)
#   ./start.sh --prod       the production topology behind Caddy, with Postgres
#   ./start.sh down         stop everything and remove the containers
#   ./start.sh logs         tail the logs of every service
#   ./start.sh status       what is running, and whether it is healthy
#   ./start.sh check        preflight only: report problems, change nothing
#
# What this does that a bare `docker compose up` does not, and why:
#
#   * Detects the Docker socket's group and exports DOCKER_GID. The sandbox
#     sidecar runs as uid 10001 and the socket is root:<gid> mode 660, so
#     without the supplementary group it is refused a socket it was explicitly
#     given -- `engine: false`, and every sandbox run fails. Compose cannot
#     discover this itself.
#   * Builds the sandbox image if it is missing, so the first run does not fail
#     inside a container and report it as a sandbox error.
#   * Waits for health and actually probes the services over HTTP. A container
#     that is "Up" is not a service that answers, and the gap between those two
#     is where a broken stack looks fine.
#   * Fails loudly on the specific known failure modes instead of leaving a
#     half-up stack that only misbehaves when you click something.

set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# --------------------------------------------------------------------- output ----
if [[ -t 1 ]]; then
  B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'
else
  B=''; G=''; Y=''; R=''; D=''; N=''
fi
step() { printf '%s==>%s %s\n' "$B" "$N" "$*"; }
ok()   { printf '%s  ok%s  %s\n' "$G" "$N" "$*"; }
warn() { printf '%s warn%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '%s fail%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

# --------------------------------------------------------------------- options ----
REBUILD=0
WITH_DB=0
PROD=0
CMD=up
for arg in "$@"; do
  case "$arg" in
    --rebuild) REBUILD=1 ;;
    --db|--database) WITH_DB=1 ;;
    --prod) PROD=1 ;;
    up|down|logs|status|check) CMD="$arg" ;;
    -h|--help) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option '$arg' (try --help)" ;;
  esac
done

if [[ $PROD -eq 1 ]]; then
  COMPOSE=(docker compose --env-file .env -f deploy/docker-compose.prod.yml)
  PROFILES=()
  [[ $WITH_DB -eq 1 ]] || PROFILES+=(--profile db)
else
  COMPOSE=(docker compose)
  PROFILES=()
  if [[ $WITH_DB -eq 1 ]]; then
    PROFILES+=(--profile db)
    # The dev stack's postgres has no host port, so the DSN has to use the
    # service name. Without this the container starts, the backend never learns
    # it exists, and everything silently stays in-memory -- which looks exactly
    # like --db not having worked.
    export DEV_DATABASE_URL="${DEV_DATABASE_URL:-postgresql+psycopg://mat:mat@postgres:5432/mat}"
  fi
fi

compose() { "${COMPOSE[@]}" "${PROFILES[@]}" "$@"; }

# ------------------------------------------------------------------ preflight ----
check_prereqs() {
  command -v docker >/dev/null 2>&1 || die "docker is not on PATH"
  docker info >/dev/null 2>&1 || die "the Docker daemon is not reachable (try: sudo systemctl start docker)"
  [[ -S /var/run/docker.sock ]] || die "/var/run/docker.sock is missing; Docker is not running normally here"
}

discover_docker_gid() {
  # The sidecar runs as uid 10001; docker.sock is root:<gid> 660. Without a
  # supplementary group matching <gid> it cannot open the socket it was given.
  local gid
  gid="$(stat -c '%g' /var/run/docker.sock 2>/dev/null || stat -f '%g' /var/run/docker.sock 2>/dev/null || echo 1001)"
  export DOCKER_GID="$gid"
  ok "docker socket group = $DOCKER_GID (passed to the sandbox sidecar)"
}

ensure_env_file() {
  [[ -f .env ]] && return 0
  [[ -f .env.example ]] || die "neither .env nor .env.example exists"
  cp .env.example .env
  warn "created .env from .env.example -- add an LLM key before starting a run"
  warn "  (GROQ_API_KEY or GEMINI_API_KEY; see README.md)"
}

# --------------------------------------------------------- sandbox image check ----
ensure_sandbox_image() {
  # The sidecar builds on demand, but a first-run build inside the request
  # surfaces as a confusing "sandbox image unavailable" on the first run rather
  # than at setup. Build it up front so that failure happens here, loudly.
  local tag
  tag="$(python3 - <<'PY' 2>/dev/null || echo "agent-team-sandbox:local"
import hashlib, pathlib
ctx = pathlib.Path("backend/sandbox")
h = hashlib.sha256()
for p in sorted(ctx.glob("*")):
    if p.is_file() and p.name in {"Dockerfile", "requirements.txt"}:
        h.update(p.name.encode()); h.update(p.read_bytes())
print(f"agent-team-sandbox:local-{h.hexdigest()[:12]}")
PY
)"
  if docker image inspect "$tag" >/dev/null 2>&1; then
    ok "sandbox image $tag"
    return 0
  fi
  warn "building the sandbox image (one-off, a few minutes)..."
  docker build -q -t "$tag" backend/sandbox >/dev/null || die "sandbox image build failed"
  ok "sandbox image built"
}

# ------------------------------------------------------------------- wait/health ----
wait_for() {
  local url="$1" name="$2" tries="${3:-60}" i
  for ((i = 1; i <= tries; i++)); do
    if curl -fsS -o /dev/null --max-time 4 "$url" 2>/dev/null; then
      ok "$name is answering"
      return 0
    fi
    sleep 2
  done
  return 1
}

report_health() {
  local bad=0
  while read -r svc state _; do
    case "$state" in
      *healthy*|*Up*) ok "$svc: $state" ;;
      *) warn "$svc: $state"; bad=1 ;;
    esac
  done < <(compose ps --format '{{.Service}} {{.State}} {{.Status}}' 2>/dev/null || true)
  return $bad
}

# ------------------------------------------------------------------- subcommands ----
do_check() {
  step "Checking prerequisites"
  check_prereqs
  ok "docker is running"
  discover_docker_gid
  ensure_env_file
  ensure_sandbox_image
  step "Checked. Nothing was changed."
}

do_up() {
  step "Starting the platform"
  check_prereqs
  discover_docker_gid
  ensure_env_file
  ensure_sandbox_image

  local build=()
  [[ $REBUILD -eq 1 ]] && build=(--build)
  step "Bringing up services (this builds on first run)"
  compose up -d "${build[@]}"

  if [[ $PROD -eq 1 ]]; then
    wait_for "http://localhost/health" "backend" || warn "backend did not answer /health yet"
    ok "open http://localhost  (sign in with the password bootstrap-env.sh printed)"
  else
    wait_for "http://localhost:8000/health" "backend" || warn "backend did not answer :8000/health yet"
    wait_for "http://localhost:5173/"        "frontend" || warn "frontend did not answer :5173 yet"
    ok "open http://localhost:5173"
  fi

  # The one check worth doing every time: is the sandbox actually able to start
  # a container? A sidecar that is "healthy" but cannot reach the daemon makes
  # every run fall back to an unisolated subprocess, silently.
  step "Verifying the sandbox can start a container"
  if compose exec -T sandbox python - <<'PY' 2>/dev/null
import httpx, sys
try:
    b = httpx.get("http://127.0.0.1:8001/health", timeout=15).json()
except Exception as e:
    sys.exit(1)
sys.exit(0 if b.get("ok") and b.get("engine") else 1)
PY
  then
    ok "sandbox sidecar has a working Docker engine"
  else
    warn "the sidecar is up but reports no engine. Check: docker compose logs sandbox"
    warn "  a common cause is DOCKER_GID; it is currently ${DOCKER_GID:-unset}"
  fi

  step "Ready"
  compose ps --format 'table {{.Service}}\t{{.Status}}' 2>/dev/null || true
}

do_down()   { step "Stopping"; compose down --remove-orphans; ok "stopped"; }
do_logs()   { compose logs -f --tail 100; }
do_status() { compose ps; }

case "$CMD" in
  up)     do_up ;;
  down)   do_down ;;
  logs)   do_logs ;;
  status) do_status ;;
  check)  do_check ;;
esac
