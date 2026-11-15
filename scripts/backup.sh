#!/usr/bin/env bash
# Back up the EquityOS database (custom-format pg_dump, compressed) from the Docker Compose db service.
#   ./scripts/backup.sh [output-dir]          default: ./backups
# Documents, the HTTP cache and the manual-ingestion inbox live in ./data on your disk (not in the
# database): back that folder up too (e.g. copy it, or `tar czf data.tgz data`).
set -euo pipefail
cd "$(dirname "$0")/.."
out="${1:-backups}"
mkdir -p "$out"
user="${POSTGRES_USER:-equityos}"
db="${POSTGRES_DB:-equityos}"
file="$out/${db}_$(date +%Y%m%d_%H%M%S).dump"
docker compose exec -T db pg_dump -U "$user" -d "$db" -Fc --no-owner > "$file"
# a dump that cannot be listed is not a backup
docker compose exec -T db pg_restore --list < "$file" > /dev/null
echo "backup written: $file ($(du -h "$file" | cut -f1))"
