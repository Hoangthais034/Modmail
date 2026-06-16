#!/bin/bash
# Modmail MongoDB entrypoint: start mongod on a custom port and run init scripts on first boot.
set -e

MONGO_PORT="${MONGO_PORT:-27048}"
MONGO_DATABASE="${MONGO_DATABASE:-modmail_bot}"

echo "Starting MongoDB on port ${MONGO_PORT}..."

mongod --bind_ip_all --port "${MONGO_PORT}" --dbpath /data/db --quiet &
MONGO_PID=$!

cleanup() {
  echo "Shutting down MongoDB..."
  kill "${MONGO_PID}" 2>/dev/null || true
  wait "${MONGO_PID}" 2>/dev/null || true
}
trap cleanup SIGTERM SIGINT

echo "Waiting for MongoDB to be ready..."
MAX_RETRIES=60
RETRY_COUNT=0
while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
  if mongosh --port "${MONGO_PORT}" --eval "db.adminCommand('ping')" --quiet > /dev/null 2>&1; then
    echo "MongoDB is ready!"
    break
  fi
  RETRY_COUNT=$((RETRY_COUNT + 1))
  if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
    echo "ERROR: MongoDB failed to start after ${MAX_RETRIES} attempts" >&2
    exit 1
  fi
  sleep 1
done

DB_COUNT=$(mongosh --port "${MONGO_PORT}" --quiet --eval "db.getMongo().getDBNames().filter(db => db !== 'admin' && db !== 'local' && db !== 'config').length" 2>/dev/null || echo "0")

if [ "$DB_COUNT" = "0" ]; then
  echo "No user databases found, running init scripts from /docker-entrypoint-initdb.d/..."
  for f in /docker-entrypoint-initdb.d/*; do
    [ -f "$f" ] || continue
    case "$f" in
      *.sh)
        if [ -x "$f" ]; then
          echo "Running $f"
          MONGO_PORT="${MONGO_PORT}" MONGO_DATABASE="${MONGO_DATABASE}" "$f"
        fi
        ;;
      *.js)
        echo "Running $f"
        mongosh --port "${MONGO_PORT}" "$f"
        ;;
    esac
  done
  echo "Init scripts completed"
else
  echo "User databases already exist (${DB_COUNT} databases), skipping init scripts"
fi

echo "MongoDB is running on port ${MONGO_PORT}. Waiting for connections..."
wait "${MONGO_PID}"
