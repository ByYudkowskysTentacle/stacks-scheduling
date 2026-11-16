#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Encrypted database backup for a Stacks install (docs/install.md, "Backups").
#
#   deploy/backup.sh /path/to/backups
#
# Writes stacks-YYYYmmdd-HHMMSS.dump.age: a PostgreSQL custom-format dump, encrypted with age
# to the public key in deploy/backup-recipient.txt (keep the private key OFF this server).
# Then copy the backup directory off the server (rclone, restic, rsync, or your campus
# storage), and keep at least 30 days. Run it from the install directory, for example daily
# from cron:
#   15 2 * * * cd /opt/stacks && deploy/backup.sh /var/backups/stacks >> /var/log/stacks-backup.log 2>&1
set -eu

DEST=${1:?usage: deploy/backup.sh /path/to/backups}
RECIPIENT_FILE=${RECIPIENT_FILE:-deploy/backup-recipient.txt}
KEEP_DAYS=${KEEP_DAYS:-30}

if ! command -v age >/dev/null 2>&1; then
  echo "backup: install age (apt install age) to encrypt backups" >&2
  exit 1
fi
if [ ! -s "$RECIPIENT_FILE" ]; then
  echo "backup: put the age public key (age1...) in $RECIPIENT_FILE" >&2
  exit 1
fi

mkdir -p "$DEST"
STAMP=$(date -u +%Y%m%d-%H%M%S)
OUT="$DEST/stacks-$STAMP.dump.age"
TMP="$OUT.partial"
STATUS="$TMP.status"
trap 'rm -f "$TMP" "$STATUS"' EXIT

# The dump never touches disk unencrypted. Credentials stay inside the db container.
# POSIX sh has no pipefail, so the dump's exit status goes through a file: otherwise a failed
# dump (database down, say) is encrypted as an empty stream and reported as written.
{
  rc=0
  docker compose exec -T db sh -c 'pg_dump --format=custom --no-owner -U "$POSTGRES_USER" "$POSTGRES_DB"' || rc=$?
  echo "$rc" > "$STATUS"
} | age --encrypt --recipients-file "$RECIPIENT_FILE" > "$TMP"
rc=$(cat "$STATUS" 2>/dev/null || echo unknown)
if [ "$rc" != 0 ]; then
  echo "backup: database dump failed (exit status $rc); no backup written" >&2
  exit 1
fi
mv "$TMP" "$OUT"
echo "backup: wrote $OUT ($(wc -c < "$OUT") bytes)"

find "$DEST" -name 'stacks-*.dump.age' -mtime +"$KEEP_DAYS" -delete
