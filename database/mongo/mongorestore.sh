#!/bin/sh
# Optional MongoDB restore on first init; skips gracefully when no archive is present.
set -eu

MONGO_HOST="${MONGO_HOST:-127.0.0.1}"
MONGO_PORT="${MONGO_PORT:-27048}"
MONGO_DATABASE="${MONGO_DATABASE:-modmail_bot}"
ARCHIVE="${MONGO_RESTORE_ARCHIVE:-/docker-entrypoint-initdb.d/dumps/modmail_bot.archive.gz}"

if [ ! -f "$ARCHIVE" ]; then
  echo "No restore archive at ${ARCHIVE}, skipping mongorestore"
  exit 0
fi

sleep 2

echo "Restoring MongoDB database ${MONGO_DATABASE} from ${ARCHIVE}..."
mongorestore \
  --uri="mongodb://${MONGO_HOST}:${MONGO_PORT}/" \
  --archive="${ARCHIVE}" \
  --gzip \
  --nsInclude="${MONGO_DATABASE}.*" \
  --drop

echo "MongoDB restore completed"
