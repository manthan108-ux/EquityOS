#!/usr/bin/env bash
# Restore a backup into a database. It NEVER overwrites the live database unless you pass --replace.
#   ./scripts/restore.sh backups/equityos_YYYYMMDD_HHMMSS.dump                 -> scratch db <name>_restore
#   ./scripts/restore.sh backups/file.dump --replace                           -> replaces the live db (stop the backend and worker first)
set -euo pipefail
cd "$(dirname "$0")/.."
file="${1:?usage: restore.sh <dump-file> [--replace]}"
user="${POSTGRES_USER:-equityos}"
live="${POSTGRES_DB:-equityos}"
target="${live}_restore"
if [ "${2:-}" = "--replace" ]; then target="$live"; fi
psql() { docker compose exec -T db psql -U "$user" -d postgres -v ON_ERROR_STOP=1 "$@"; }
psql -c "DROP DATABASE IF EXISTS \"$target\" WITH (FORCE)"
psql -c "CREATE DATABASE \"$target\""
docker compose exec -T db pg_restore -U "$user" -d "$target" --no-owner --exit-on-error < "$file"
echo "restored $file into database: $target"
