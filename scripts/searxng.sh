#!/usr/bin/env bash
# Manage the local SearXNG search backend for Sakura Fare.
#
#   scripts/searxng.sh up      # pull + start (idempotent)
#   scripts/searxng.sh down    # stop + remove
#   scripts/searxng.sh logs    # follow logs
#   scripts/searxng.sh status  # health probe
#
# Bound to 127.0.0.1 only — not reachable from the network. The app talks to
# it via SAKURA_SEARXNG_URL (default http://localhost:8888).
set -euo pipefail

NAME="sakura-searxng"
PORT="${SAKURA_SEARXNG_PORT:-8888}"
IMAGE="searxng/searxng:latest"
CONFIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/searxng"

case "${1:-up}" in
  up)
    if docker ps --filter "name=^${NAME}$" --format '{{.Names}}' | grep -q .; then
      echo "SearXNG already running on :${PORT}"
      exit 0
    fi
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    echo "Pulling $IMAGE (first run only)…"
    docker pull "$IMAGE" >/dev/null
    # --restart no: it does NOT come back on reboot; it only runs when the app
    # (or you) starts it. Use `down` to stop it.
    docker run -d --name "$NAME" \
      -p "127.0.0.1:${PORT}:8080" \
      -v "${CONFIG_DIR}:/etc/searxng:rw" \
      -e "SEARXNG_BASE_URL=http://localhost:${PORT}/" \
      --restart no \
      "$IMAGE" >/dev/null
    echo "Started $NAME on http://localhost:${PORT}"
    ;;
  down)
    docker rm -f "$NAME" >/dev/null 2>&1 && echo "Stopped $NAME" || echo "$NAME not running"
    ;;
  logs)
    docker logs -f "$NAME"
    ;;
  status)
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${PORT}/search?q=test&format=json" || true)
    [ "$code" = "200" ] && echo "SearXNG healthy (JSON API on :${PORT})" || echo "SearXNG not responding (HTTP $code)"
    ;;
  *)
    echo "usage: $0 {up|down|logs|status}" >&2
    exit 2
    ;;
esac
