#!/bin/sh
# Daily backup helper for /app/data and the configuration (spec 19).
#
# SQLite is backed up through the online backup API, so a running container is
# safe: WAL is checkpointed into the copy instead of being copied mid-write.
#
# Usage: ./scripts/backup.sh [backup_dir]   (default: ./backups)
set -eu

BACKUP_DIR="${1:-./backups}"
DATA_DIR="${DATA_DIR:-./data}"
CONFIG_DIR="${CONFIG_DIR:-./config}"
DB_FILE="${DB_FILE:-$DATA_DIR/app.db}"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP_DIR"

if [ -f "$DB_FILE" ]; then
    python3 - "$DB_FILE" "$BACKUP_DIR/app-$STAMP.db" <<'PY'
import sqlite3
import sys

source, target = sys.argv[1], sys.argv[2]
with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
    src.backup(dst)
print(f"database backed up to {target}")
PY
else
    echo "no database at $DB_FILE, skipping database backup"
fi

if [ -d "$CONFIG_DIR" ]; then
    tar -czf "$BACKUP_DIR/config-$STAMP.tar.gz" "$CONFIG_DIR"
    echo "configuration backed up to $BACKUP_DIR/config-$STAMP.tar.gz"
fi

# Keep the last 14 archives of each kind.
find "$BACKUP_DIR" -name 'app-*.db' -type f | sort | head -n -14 | xargs -r rm -f
find "$BACKUP_DIR" -name 'config-*.tar.gz' -type f | sort | head -n -14 | xargs -r rm -f

echo "backup finished: $BACKUP_DIR"
