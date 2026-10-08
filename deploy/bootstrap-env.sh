#!/usr/bin/env bash
# Create or complete the deployment .env with working auth credentials.
#
#   ./deploy/bootstrap-env.sh                 # uses REGISTRY=local/mat (local run)
#   REGISTRY=ghcr.io/you/repo ./deploy/bootstrap-env.sh
#   REGISTRY=ghcr.io/you/repo SITE_ADDRESS=:443 ./deploy/bootstrap-env.sh
#   ./deploy/bootstrap-env.sh --reset-password 'new-password'
#   ./deploy/bootstrap-env.sh --reset-password        # generates one
#
# Idempotent: existing values are preserved, missing ones are generated. The
# generated password is printed once and never stored in plain text.
set -euo pipefail

RESET_PASSWORD=""
while [ $# -gt 0 ]; do
  case "$1" in
    --reset-password)
      RESET_PASSWORD="${2:-}"
      shift 2 || shift
      ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."
ROOT="$PWD"
ENV_FILE="$ROOT/.env"

REGISTRY="${REGISTRY:-local/mat}"
TAG="${TAG:-latest}"
SITE_ADDRESS="${SITE_ADDRESS:-:80}"
PY="${PY:-$ROOT/.venv/bin/python}"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

# Read a single key from .env without sourcing the file.
env_get() {
  [ -f "$ENV_FILE" ] || return 0
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -1
}

ensure_python() {
  [ -x "$PY" ] && return 0
  if command -v python3 >/dev/null 2>&1; then
    PY="python3"
    warn "using system python3 (no .venv); create one with: python3 -m venv .venv"
    return 0
  fi
  die "no python found; install python3 or create .venv"
}

ensure_deps() {
  ensure_python
  if ! "$PY" -c "import pydantic_settings, itsdangerous" >/dev/null 2>&1; then
    warn "installing backend dependencies into the venv"
    [ -x "$ROOT/.venv/bin/python" ] || die "no .venv; run: python3 -m venv .venv"
    "$ROOT/.venv/bin/python" -m pip install -q -r "$ROOT/backend/requirements.txt"
  fi
}

log "env file: $ENV_FILE"
if [ -f "$ENV_FILE" ]; then
  log "existing values will be preserved"
else
  log "creating a new .env from .env.example"
  [ -f "$ROOT/.env.example" ] || die ".env.example missing"
  cp "$ROOT/.env.example" "$ENV_FILE"
fi

set_key() {
  # set_key KEY VALUE — replace the key if present, append otherwise.
  local key="$1" value="$2"
  if grep -q "^${key}=" "$ENV_FILE"; then
    # Use a temp file: sed -i with an arbitrary value can contain slashes.
    local tmp
    tmp=$(mktemp)
    awk -v k="$key" -v v="$value" '
      BEGIN { FS = OFS = "=" }
      $1 == k && !done { print k "=" v; done = 1; next }
      { print }
    ' "$ENV_FILE" > "$tmp"
    mv "$tmp" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

ensure_deps

PASSWORD=""
FORCE_RESET=0
[ -n "$RESET_PASSWORD" ] && FORCE_RESET=1

if [ "$FORCE_RESET" -eq 1 ]; then
  log "resetting the password"
  PASSWORD="${RESET_PASSWORD:-$(ensure_python; "$PY" -c 'import secrets; print(secrets.token_urlsafe(18))')}"
  HASH=$("$PY" "$ROOT/backend/scripts/gen_auth.py" "$PASSWORD" | sed -n 's/^AUTH_PASSWORD_HASH=//p')
  set_key AUTH_PASSWORD_HASH "$HASH"
elif [ -z "$(env_get AUTH_PASSWORD_HASH)" ]; then
  log "generating a password and scrypt hash"
  GENERATED=$("$PY" "$ROOT/backend/scripts/gen_auth.py" | sed -n 's/^# generated password (save it now, it is not stored): //p')
  PASSWORD="$GENERATED"
  HASH=$("$PY" "$ROOT/backend/scripts/gen_auth.py" "$PASSWORD" | sed -n 's/^AUTH_PASSWORD_HASH=//p')
  set_key AUTH_PASSWORD_HASH "$HASH"
else
  log "AUTH_PASSWORD_HASH already set, keeping it"
  HASH="$(env_get AUTH_PASSWORD_HASH)"
fi

[ -n "$(env_get AUTH_SECRET)" ] || set_key AUTH_SECRET "$("$PY" -c 'import secrets; print(secrets.token_urlsafe(48))')"
[ -n "$(env_get AUTH_API_KEY)" ] || set_key AUTH_API_KEY "$("$PY" -c 'import sys; sys.path.insert(0, "backend"); from app.auth import new_api_key; print(new_api_key())')"

set_key AUTH_USERNAME "$(env_get AUTH_USERNAME)"
[ -n "$(env_get AUTH_USERNAME)" ] || set_key AUTH_USERNAME admin
set_key AUTH_COOKIE_SECURE 0
set_key AUTH_SESSION_HOURS 12
set_key AUTH_CSRF 1

# Deployment coordinates.
set_key REGISTRY "$REGISTRY"
set_key TAG "$TAG"
set_key SITE_ADDRESS "$SITE_ADDRESS"

# Default to template mode so a fresh deploy is coherent; see docs/DEPLOY.md §7.
[ -n "$(env_get SKIP_OLLAMA)" ] || set_key SKIP_OLLAMA 1

chmod 600 "$ENV_FILE"
log "wrote $ENV_FILE"

cat <<EOF

Next:
  docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d --build
  open http://localhost$( [ "$SITE_ADDRESS" = ":80" ] && echo "" || echo ":443" )

EOF

if [ -n "$PASSWORD" ]; then
  cat <<EOF
  Sign in with:
    username: $(env_get AUTH_USERNAME)
    password: $PASSWORD

  This password is NOT stored anywhere. It exists only as a hash in .env.
  Save it now, or re-run:  python backend/scripts/gen_auth.py 'new-password'

EOF
else
  cat <<EOF
  Keeping your existing password. If you have forgotten it, reset with:
    python backend/scripts/gen_auth.py 'new-password'   # then update .env

EOF
fi