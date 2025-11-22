#!/bin/sh
set -e

DB_HOST="${DB_HOST:-tidb}"
DB_PORT="${DB_PORT:-4000}"
DB_ROOT_USER="${DB_ROOT_USER:-root}"
DB_ROOT_PASSWORD="${DB_ROOT_PASSWORD:-}"

EXPECTED_TABLES=3
RETRIES=5
SLEEP_SECONDS=3

echo "Waiting for TiDB at ${DB_HOST}:${DB_PORT}..."

# Wait for TiDB to accept connections
until mysql -h"${DB_HOST}" -P"${DB_PORT}" -u"${DB_ROOT_USER}" ${DB_ROOT_PASSWORD:+-p"${DB_ROOT_PASSWORD}"} -e "SELECT 1;" >/dev/null 2>&1; do
  echo "TiDB is not ready yet, sleeping..."
  sleep "${SLEEP_SECONDS}"
done

echo "TiDB is up. Applying schema and user creation..."

i=1
while [ "$i" -le "$RETRIES" ]; do
  echo "Attempt ${i}/${RETRIES}: applying schema.sql..."
  # Apply schema (idempotent: uses IF NOT EXISTS)
  mysql -h"${DB_HOST}" -P"${DB_PORT}" -u"${DB_ROOT_USER}" ${DB_ROOT_PASSWORD:+-p"${DB_ROOT_PASSWORD}"} < /schema.sql || true

  echo "Checking that required tables exist..."
  TABLE_COUNT=$(mysql -h"${DB_HOST}" -P"${DB_PORT}" -u"${DB_ROOT_USER}" ${DB_ROOT_PASSWORD:+-p"${DB_ROOT_PASSWORD}"} \
    -N -s -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'shop' AND table_name IN ('customers','suppliers','products');" 2>/dev/null || echo 0)

  echo "Found ${TABLE_COUNT}/${EXPECTED_TABLES} required tables."

  if [ "$TABLE_COUNT" -eq "$EXPECTED_TABLES" ]; then
    echo "All required tables created successfully."
    echo "Database initialization completed successfully."
    exit 0
  fi

  i=$((i + 1))
  if [ "$i" -le "$RETRIES" ]; then
    echo "Not all tables exist yet, retrying after ${SLEEP_SECONDS} seconds..."
    sleep "${SLEEP_SECONDS}"
  fi
done

echo "ERROR: Failed to verify that all required tables exist after ${RETRIES} attempts."
exit 1


