#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
cd "$REPO_ROOT"

MODE=${ANZMA_BACKUP_MODE:-compose}
COMPOSE_FILE=${ANZMA_COMPOSE_FILE:-deploy/compose/docker-compose.yml}
COMPOSE_PROJECT=${ANZMA_COMPOSE_PROJECT:-}
DB_SERVICE=${ANZMA_BACKUP_DB_SERVICE:-db}
BACKUP_DIR=${ANZMA_BACKUP_DIR:-${HOME:?HOME must be set}/anzma_backups}
KEEP_DAYS=${ANZMA_BACKUP_KEEP_DAYS:-14}
MIN_KEEP=${ANZMA_BACKUP_MIN_KEEP:-3}
START_EPOCH=$(date -u +%s)
UTC_NOW=$(date -u +%Y%m%d_%H%M%S)
BASE="anzma_${UTC_NOW}.dump"
FINAL_PATH="${BACKUP_DIR%/}/$BASE"
PARTIAL_PATH="${FINAL_PATH}.partial"
SHA_PARTIAL="${FINAL_PATH}.sha256.partial"
ROWS_PARTIAL="${FINAL_PATH}.rowcounts.txt.partial"
FAILED=0
FAILURE_CODE=1
TEMP_LOG=""
SIZE_BYTES=0
OUTPUT_STARTED=0

json_summary() {
    local event=$1 file=$2 size=$3 finish duration
    finish=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    duration=$(($(date -u +%s) - START_EPOCH))
    printf '{"event":"%s","file":"%s","size_bytes":%s,"duration_s":%s,"timestamp":"%s"}\n' \
        "$event" "$file" "$size" "$duration" "$finish"
}

on_exit() {
    local status=$?
    if [[ -n "$TEMP_LOG" ]]; then rm -f "$TEMP_LOG"; fi
    if (( FAILED )); then
        if (( OUTPUT_STARTED )); then
            rm -f "$PARTIAL_PATH" "$SHA_PARTIAL" "$ROWS_PARTIAL"
            rm -f "$FINAL_PATH" "${FINAL_PATH}.sha256" "${FINAL_PATH}.rowcounts.txt"
        fi
        json_summary BACKUP_FAILED "$BASE" 0
        exit "$FAILURE_CODE"
    fi
    if (( status != 0 )); then
        if (( OUTPUT_STARTED )); then rm -f "$PARTIAL_PATH" "$SHA_PARTIAL" "$ROWS_PARTIAL"; fi
        json_summary BACKUP_FAILED "$BASE" 0
        exit 1
    fi
}
trap on_exit EXIT

config_error() {
    printf '%s\n' "$1" >&2
    FAILED=1
    FAILURE_CODE=2
    exit 2
}

dump_error() {
    FAILED=1
    FAILURE_CODE=1
    exit 1
}

if [[ ${1:-} == --dry-run ]]; then
    DRY_RUN=1
    shift
else
    DRY_RUN=0
fi
[[ $# -eq 0 ]] || config_error 'Usage: scripts/backup_db.sh [--dry-run]'
[[ "$MODE" == compose || "$MODE" == host ]] || config_error 'ANZMA_BACKUP_MODE must be compose or host.'
[[ "$DB_SERVICE" =~ ^[A-Za-z0-9_-]+$ ]] || config_error 'ANZMA_BACKUP_DB_SERVICE must be a Compose service name.'
[[ "$KEEP_DAYS" =~ ^[0-9]+$ ]] || config_error 'ANZMA_BACKUP_KEEP_DAYS must be a non-negative integer.'
[[ "$MIN_KEEP" =~ ^[0-9]+$ ]] || config_error 'ANZMA_BACKUP_MIN_KEEP must be a non-negative integer.'
[[ -n "$BACKUP_DIR" ]] || config_error 'ANZMA_BACKUP_DIR must not be empty.'

if [[ "$MODE" == compose ]]; then
    [[ -f "$COMPOSE_FILE" ]] || config_error 'ANZMA_COMPOSE_FILE does not name a file.'
    command -v docker >/dev/null 2>&1 || config_error 'docker is required in compose mode.'
else
    [[ -n ${PGUSER:-} && -n ${PGDATABASE:-} ]] || config_error 'PGUSER and PGDATABASE are required in host mode.'
    command -v pg_dump >/dev/null 2>&1 && command -v pg_restore >/dev/null 2>&1 \
        && command -v psql >/dev/null 2>&1 || config_error 'pg_dump, pg_restore, and psql are required in host mode.'
fi

if [[ "$BACKUP_DIR" != /* ]]; then BACKUP_DIR="$REPO_ROOT/$BACKUP_DIR"; fi
command -v python3 >/dev/null 2>&1 || config_error 'python3 is required to normalize the backup directory.'
BACKUP_DIR=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$BACKUP_DIR") \
    || config_error 'Could not normalize ANZMA_BACKUP_DIR.'
case "$BACKUP_DIR/" in
    "$REPO_ROOT/"|"$REPO_ROOT/"*) config_error 'Backup directory must be outside the repository.' ;;
esac

if [[ "$DRY_RUN" -eq 0 ]]; then
    mkdir -p "$BACKUP_DIR" || dump_error
    chmod 700 "$BACKUP_DIR" || dump_error
fi
if [[ -d "$BACKUP_DIR" ]]; then
    BACKUP_DIR=$(cd -- "$BACKUP_DIR" && pwd -P)
fi
FINAL_PATH="${BACKUP_DIR%/}/$BASE"
PARTIAL_PATH="${FINAL_PATH}.partial"
SHA_PARTIAL="${FINAL_PATH}.sha256.partial"
ROWS_PARTIAL="${FINAL_PATH}.rowcounts.txt.partial"
[[ ! -e "$FINAL_PATH" && ! -e "${FINAL_PATH}.sha256" && ! -e "${FINAL_PATH}.rowcounts.txt" ]] \
    || config_error 'A backup with this timestamp already exists.'

if [[ "$DRY_RUN" -eq 1 ]]; then
    printf 'Would create %s and evaluate retention in %s.\n' "$FINAL_PATH" "$BACKUP_DIR"
fi

compose() {
    local args=(-f "$COMPOSE_FILE")
    [[ -z "$COMPOSE_PROJECT" ]] || args+=(-p "$COMPOSE_PROJECT")
    docker compose "${args[@]}" "$@"
}

run_pg_restore_list() {
    if [[ "$MODE" == compose ]]; then
        local container_path="/tmp/anzma-backup-${$}-${UTC_NOW}.dump"
        compose exec -T "$DB_SERVICE" sh -c 'umask 077; cat > "$1"' sh "$container_path" \
            < "$1" 2>>"$TEMP_LOG" || return 1
        if ! compose exec -T "$DB_SERVICE" pg_restore --list "$container_path" \
            >/dev/null 2>>"$TEMP_LOG"; then
            compose exec -T "$DB_SERVICE" rm -f "$container_path" >/dev/null 2>&1 || true
            return 1
        fi
        compose exec -T "$DB_SERVICE" rm -f "$container_path" >/dev/null 2>&1 || true
    else
        pg_restore --list "$1" >/dev/null 2>>"$TEMP_LOG"
    fi
}

checksum_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        return 1
    fi
}

valid_backup() {
    local path=$1 sidecar="${1}.sha256" expected actual
    [[ -s "$path" && -s "$sidecar" && -s "${path}.rowcounts.txt" ]] || return 1
    expected=$(awk 'NR==1 {print $1}' "$sidecar")
    actual=$(checksum_file "$path") || return 1
    [[ "$expected" == "$actual" ]]
}

file_mtime() {
    local path=$1 value
    if value=$(stat -c %Y -- "$path" 2>/dev/null) && [[ "$value" =~ ^[0-9]+$ ]]; then
        printf '%s\n' "$value"
        return 0
    fi
    if value=$(stat -f %m "$path" 2>/dev/null) && [[ "$value" =~ ^[0-9]+$ ]]; then
        printf '%s\n' "$value"
        return 0
    fi
    return 1
}

retain_backups() {
    local min_keep=$1 keep_days=$2 current=$3 path base count=0 mtime cutoff now size
    local -a paths=()
    shopt -s nullglob
    for path in "$BACKUP_DIR"/anzma_????????_??????.dump; do
        base=${path##*/}
        [[ "$base" =~ ^anzma_[0-9]{8}_[0-9]{6}\.dump$ ]] || continue
        valid_backup "$path" || continue
        paths+=("$path")
    done
    shopt -u nullglob
    # Names contain UTC timestamps, so descending lexical order is newest first.
    now=$(date -u +%s)
    cutoff=$((now - keep_days * 86400))
    while IFS= read -r path; do
        count=$((count + 1))
        [[ "$path" != "$current" ]] || continue
        if ! mtime=$(file_mtime "$path"); then
            printf 'WARNING: could not read numeric mtime; skipping retention for %s.\n' "$path" >&2
            continue
        fi
        if (( count > min_keep && mtime < cutoff )); then
            if [[ "$DRY_RUN" -eq 1 ]]; then
                printf 'Would delete %s and its sidecars.\n' "$path"
            else
                size=$(wc -c < "$path" | tr -d ' ')
                rm -f "$path" "${path}.sha256" "${path}.rowcounts.txt"
                printf '{"event":"RETENTION_DELETED","file":"%s","size_bytes":%s,"duration_s":%s,"timestamp":"%s"}\n' \
                    "${path##*/}" "$size" "$(($(date -u +%s) - START_EPOCH))" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
            fi
        fi
    done < <(if [[ -n ${paths[*]:-} ]]; then printf '%s\n' "${paths[@]}" | sort -r; fi)
}

if [[ "$DRY_RUN" -eq 1 ]]; then
    retain_backups "$MIN_KEEP" "$KEEP_DAYS" ""
    exit 0
fi

TEMP_LOG=$(mktemp "${TMPDIR:-/tmp}/anzma-backup.XXXXXX") || dump_error
chmod 600 "$TEMP_LOG"
OUTPUT_STARTED=1
if [[ "$MODE" == compose ]]; then
    compose exec -T "$DB_SERVICE" sh -c 'pg_dump -U "${PGUSER:-${POSTGRES_USER:-postgres}}" -d "${PGDATABASE:-${POSTGRES_DB:-anzma}}" -Fc' \
        > "$PARTIAL_PATH" 2>"$TEMP_LOG" || dump_error
else
    pg_dump -U "$PGUSER" -d "$PGDATABASE" -Fc > "$PARTIAL_PATH" 2>"$TEMP_LOG" || dump_error
fi
[[ -s "$PARTIAL_PATH" ]] || dump_error
run_pg_restore_list "$PARTIAL_PATH" || dump_error

SQL="SELECT 'telemetry_records=' || count(*) FROM telemetry_records;
SELECT 'event_records=' || count(*) FROM event_records;
SELECT 'alerts=' || count(*) FROM alerts;
SELECT 'incidents=' || count(*) FROM incidents;
SELECT 'assets=' || count(*) FROM assets;
SELECT 'buildings=' || count(*) FROM buildings;
SELECT 'locations=' || count(*) FROM locations;
SELECT 'data_sources=' || count(*) FROM data_sources;
SELECT 'source_credentials=' || count(*) FROM source_credentials;
SELECT 'asset_external_ids=' || count(*) FROM asset_external_ids;
SELECT 'ingestion_batches=' || count(*) FROM ingestion_batches;
SELECT 'ingestion_rejections=' || count(*) FROM ingestion_rejections;
SELECT 'equipment_state_records=' || count(*) FROM equipment_state_records;"
if [[ "$MODE" == compose ]]; then
    compose exec -T "$DB_SERVICE" sh -c 'psql -X -A -t -v ON_ERROR_STOP=1 -U "${PGUSER:-${POSTGRES_USER:-postgres}}" -d "${PGDATABASE:-${POSTGRES_DB:-anzma}}" -c "$1"' \
        sh "$SQL" > "$ROWS_PARTIAL" 2>"$TEMP_LOG" || dump_error
else
    psql -X -A -t -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$PGDATABASE" -c "$SQL" \
        > "$ROWS_PARTIAL" 2>"$TEMP_LOG" || dump_error
fi
[[ -s "$ROWS_PARTIAL" ]] || dump_error

HASH=$(checksum_file "$PARTIAL_PATH") || dump_error
printf '%s  %s\n' "$HASH" "$BASE" > "$SHA_PARTIAL"
chmod 600 "$PARTIAL_PATH" "$SHA_PARTIAL" "$ROWS_PARTIAL" || dump_error
mv "$SHA_PARTIAL" "${FINAL_PATH}.sha256" || dump_error
mv "$ROWS_PARTIAL" "${FINAL_PATH}.rowcounts.txt" || dump_error
mv "$PARTIAL_PATH" "$FINAL_PATH" || dump_error
SIZE_BYTES=$(wc -c < "$FINAL_PATH" | tr -d ' ')
retain_backups "$MIN_KEEP" "$KEEP_DAYS" "$FINAL_PATH"
json_summary BACKUP_OK "$BASE" "$SIZE_BYTES"
