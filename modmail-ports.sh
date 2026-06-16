#!/bin/sh
# Modmail Dokploy port helper: MongoDB 27048 (internal + host), Logviewer 5943.
# Internal services use docker DNS on the same port inside the container.

set -eu

MONGO_HOST_PORT="${MONGO_HOST_PORT:-27048}"
MONGO_PORT="${MONGO_PORT:-27048}"
LOGVIEWER_HOST_PORT="${LOGVIEWER_HOST_PORT:-5943}"
LOGVIEWER_CONTAINER_PORT=8000
MONGO_DATA_PATH="${MONGO_DATA_PATH:-/mnt/modmail/mongodb}"
MONGO_UID=999
MONGO_GID=999
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"

export MONGO_HOST_PORT MONGO_PORT LOGVIEWER_HOST_PORT MONGO_DATA_PATH

usage() {
  cat <<EOF
Usage: $(basename "$0") <command>

Commands:
  up            Start stack with configured host ports
  down          Stop stack
  status        Show container status and mapped ports
  mongo-uri     Print external MongoDB connection URI for this host
  setup-bind    Create host data dir with mongodb ownership (999:999, run once on server)

Data bind mount:
  MONGO_DATA_PATH=${MONGO_DATA_PATH}  (host folder -> /data/db in container)

MongoDB runs as UID ${MONGO_UID} inside the image — do NOT use user: 1000:1000 on mongo.

Ports (override via env):
  MONGO_PORT=${MONGO_PORT}
  MONGO_HOST_PORT=${MONGO_HOST_PORT}
  LOGVIEWER_HOST_PORT=${LOGVIEWER_HOST_PORT}

Internal (bot/logviewer -> mongo): mongodb://mongo:${MONGO_PORT}
External MongoDB: mongodb://<host>:${MONGO_HOST_PORT}
Logviewer UI:     http://<host>:${LOGVIEWER_HOST_PORT}
EOF
}

require_compose() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
  else
    echo "error: docker compose is not installed" >&2
    exit 1
  fi
}

cmd_up() {
  require_compose
  $COMPOSE -f "$COMPOSE_FILE" up -d
  cmd_status
}

cmd_down() {
  require_compose
  $COMPOSE -f "$COMPOSE_FILE" down
}

cmd_status() {
  require_compose
  echo "=== Modmail port mapping ==="
  echo "Mongo host port:     ${MONGO_HOST_PORT} -> container ${MONGO_PORT}"
  echo "Logviewer host port: ${LOGVIEWER_HOST_PORT} -> container ${LOGVIEWER_CONTAINER_PORT}"
  echo
  $COMPOSE -f "$COMPOSE_FILE" ps
  echo
  if docker ps --format '{{.Names}}' | grep -q mongo; then
    docker ps --filter "name=mongo" --format 'table {{.Names}}\t{{.Ports}}' 2>/dev/null || true
  fi
}

cmd_mongo_uri() {
  host="${MONGO_EXTERNAL_HOST:-127.0.0.1}"
  echo "mongodb://${host}:${MONGO_HOST_PORT}"
}

cmd_setup_bind() {
  target="${1:-$MONGO_DATA_PATH}"
  echo "Preparing MongoDB bind mount directory:"
  echo "  ${target}"
  mkdir -p "${target}" 2>/dev/null || sudo mkdir -p "${target}"
  if command -v sudo >/dev/null 2>&1; then
    sudo chown -R "${MONGO_UID}:${MONGO_GID}" "${target}"
  else
    chown -R "${MONGO_UID}:${MONGO_GID}" "${target}"
  fi
  echo "Ready: ${target} owned by ${MONGO_UID}:${MONGO_GID}"
  echo "Compose maps: ${target} -> /data/db"
}

case "${1:-}" in
  up) cmd_up ;;
  down) cmd_down ;;
  status) cmd_status ;;
  mongo-uri) cmd_mongo_uri ;;
  setup-bind) cmd_setup_bind "$2" ;;
  -h|--help|help|"") usage ;;
  *)
    echo "error: unknown command: $1" >&2
    usage
    exit 1
    ;;
esac
