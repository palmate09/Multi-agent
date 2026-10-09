#!/usr/bin/env bash
# Stop the stack and free its ports.
#
#   ./deploy/stop.sh          # stop both dev and prod stacks
#   ./deploy/stop.sh --purge  # also delete volumes (loses all run artifacts)
set -euo pipefail

cd "$(dirname "$0")/.."
PURGE=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

# `docker compose down` runs detached by design and has no -d flag.
FLAGS=(--remove-orphans)
[ "$PURGE" -eq 1 ] && FLAGS+=(-v)

if [ -f .env ]; then
  echo "==> stopping production stack"
  docker compose --env-file .env -f deploy/docker-compose.prod.yml down "${FLAGS[@]}" || true
fi

echo "==> stopping development stack"
docker compose down "${FLAGS[@]}" || true

echo "==> stopped"
if [ "$PURGE" -eq 1 ]; then
  echo "    volumes deleted: all run artifacts are gone"
fi