#!/bin/sh
# Modmail Dokploy port helper: MongoDB 27048 (internal + host), Logviewer 38492.
# Internal services use docker DNS on the same port inside the container.

set -eu

MONGO_HOST_PORT="${MONGO_HOST_PORT:-27048}"
MONGO_PORT="${MONGO_PORT:-27048}"
LOGVIEWER_HOST_PORT="${LOGVIEWER_HOST_PORT:-38492}"
LOGVIEWER_CONTAINER_PORT=8000
MONGO_VOLUME_NAME="${MONGO_VOLUME_NAME:-modmail-mongodb-data}"
MONGO_DATA_BIND="${MONGO_DATA_BIND:-/var/lib/modmail/mongodb}"
MONGO_UID=999
MONGO_GID=999
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"

export MONGO_HOST_PORT MONGO_PORT LOGVIEWER_HOST_PORT MONGO_VOLUME_NAME

usage() {
  cat <<EOF
Usage: $(basename "$0") <command>

Commands:
  up            Start stack with configured host ports
  down          Stop stack
  status        Show container status and mapped ports
  mongo-uri     Print external MongoDB connection URI for this host
  setup-volume  Create external Docker volume (run once on server before first deploy)
  setup-bind    Create bind-mount dir with mongodb ownership (999:999, optional alternative)

Volume:
  MONGO_VOLUME_NAME=${MONGO_VOLUME_NAME}  (external named volume, persists across redeploys)

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

cmd_setup_volume() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "error: docker is not installed" >&2
    exit 1
  fi
  if docker volume inspect "${MONGO_VOLUME_NAME}" >/dev/null 2>&1; then
    echo "Volume ${MONGO_VOLUME_NAME} already exists"
  else
    docker volume create "${MONGO_VOLUME_NAME}"
    echo "Created external volume: ${MONGO_VOLUME_NAME}"
  fi
  mountpoint=$(docker volume inspect "${MONGO_VOLUME_NAME}" --format '{{.Mountpoint}}')
  echo "Host path: ${mountpoint}"
  echo "First start: MongoDB (uid ${MONGO_UID}) will create files inside this volume automatically."
}

cmd_setup_bind() {
  echo "Optional bind-mount path (if you switch compose to bind instead of named volume):"
  echo "  ${MONGO_DATA_BIND}"
  mkdir -p "${MONGO_DATA_BIND}" 2>/dev/null || sudo mkdir -p "${MONGO_DATA_BIND}"
  if command -v sudo >/dev/null 2>&1; then
    sudo chown -R "${MONGO_UID}:${MONGO_GID}" "${MONGO_DATA_BIND}"
  else
    chown -R "${MONGO_UID}:${MONGO_GID}" "${MONGO_DATA_BIND}"
  fi
  echo "Directory ready with owner ${MONGO_UID}:${MONGO_GID} (mongodb user in mongo:7 image)"
}

case "${1:-}" in
  up) cmd_up ;;
  down) cmd_down ;;
  status) cmd_status ;;
  mongo-uri) cmd_mongo_uri ;;
  setup-volume) cmd_setup_volume ;;
  setup-bind) cmd_setup_bind ;;
  -h|--help|help|"") usage ;;
  *)
    echo "error: unknown command: $1" >&2
    usage
    exit 1
    ;;
esac
