#!/bin/bash
# Root-only logical backup of GhostNote, not WhoUpdate's separate database.
set -euo pipefail
umask 077

readonly backup_dir=/srv/maksonchik/backups/ghost-postgresql-daily
readonly container=who-update-postgresql
readonly database=ghostnote

if [[ ${EUID} -ne 0 ]]; then
    echo 'GhostNote PostgreSQL backup must run as root' >&2
    exit 1
fi
if [[ -L "$backup_dir" ]]; then
    echo 'Refusing a symlink as the backup directory' >&2
    exit 1
fi
/usr/bin/install -d -o root -g root -m 0700 "$backup_dir"
if [[ $(/usr/bin/stat -c '%u:%a' "$backup_dir") != '0:700' ]]; then
    echo 'Backup directory must be owned by root with mode 0700' >&2
    exit 1
fi

# A manual run and timer run must never create overlapping dumps.
exec 9>"$backup_dir/.backup.lock"
if ! /usr/bin/flock -n 9; then
    echo 'GhostNote PostgreSQL backup is already running'
    exit 0
fi

temporary_dump=''
temporary_checksum=''
cleanup() {
    # Only unlink this invocation's exact, root-private mktemp files. Completed
    # backups are never deleted, including when a later invocation fails.
    if [[ -n "$temporary_dump" && "$temporary_dump" == "$backup_dir"/.ghostnote-*.partial && -f "$temporary_dump" ]]; then
        /bin/rm -f -- "$temporary_dump"
    fi
    if [[ -n "$temporary_checksum" && "$temporary_checksum" == "$backup_dir"/.ghostnote-*.sha256.partial && -f "$temporary_checksum" ]]; then
        /bin/rm -f -- "$temporary_checksum"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

timestamp=$(/bin/date -u +%Y%m%dT%H%M%SZ)
temporary_dump=$(/bin/mktemp "$backup_dir/.ghostnote-${timestamp}.XXXXXX.partial")
dump_basename=${temporary_dump##*/}
dump_basename=${dump_basename#.}
dump_basename=${dump_basename%.partial}.dump
final_dump="$backup_dir/$dump_basename"
if [[ -e "$final_dump" || -e "$final_dump.sha256" ]]; then
    echo 'Refusing to overwrite an existing backup' >&2
    exit 1
fi

# Password is inherited and used inside the existing container only; it never
# appears in the host command line, logs, or backup metadata. --dbname is
# explicit because POSTGRES_DB names WhoUpdate, not GhostNote.
/usr/bin/docker exec "$container" sh -c '
    export PGPASSWORD="${POSTGRES_PASSWORD:?Missing PostgreSQL password}"
    export PGAPPNAME=ghostnote_daily_backup
    exec pg_dump --host=127.0.0.1 --username="${POSTGRES_USER:?Missing PostgreSQL user}" \
        --dbname=ghostnote --format=custom --compress=6 --no-owner --no-acl
' >"$temporary_dump"
test -s "$temporary_dump"

# Listing the TOC alone does not read compressed table data. Rendering the
# entire archive to /dev/null validates every data section without restoring
# anything or exposing records on stdout.
/usr/bin/docker exec -i "$container" pg_restore --exit-on-error --file=/dev/null <"$temporary_dump"

checksum=$(/usr/bin/sha256sum -- "$temporary_dump")
checksum=${checksum%% *}
temporary_checksum=$(/bin/mktemp "$backup_dir/.ghostnote-${timestamp}.XXXXXX.sha256.partial")
printf '%s  %s\n' "$checksum" "$dump_basename" >"$temporary_checksum"
/bin/sync -f "$temporary_dump"
/bin/sync -f "$temporary_checksum"

# Each published file appears atomically only after archive verification. The
# SHA256 manifest is published last and can be used as the completion marker.
/bin/mv -T -- "$temporary_dump" "$final_dump"
temporary_dump=''
/bin/mv -T -- "$temporary_checksum" "$final_dump.sha256"
temporary_checksum=''
/bin/sync -f "$backup_dir"
printf 'GhostNote PostgreSQL backup verified: database=%s file=%s bytes=%s\n' \
    "$database" "$dump_basename" "$(/usr/bin/stat -c '%s' "$final_dump")"
