# PostgreSQL backup creation

`scripts/backup_db.sh` writes a custom-format `pg_dump` archive plus SHA-256 and row-count sidecars. The output directory is protected with mode `700`; backup files use mode `600`. The script verifies each new archive with `pg_restore --list` before publishing it and prunes only old, checksum-valid backups with all expected sidecars.

**Linux retention correction:** versions before commit `18ac4d0` used BSD `stat -f` before GNU `stat -c`. On GNU/Linux, that produced filesystem-status text which could break the retention arithmetic; retention on Linux hosts was therefore broken. The corrected script tries GNU first, accepts only numeric mtimes, then tries BSD, and skips a file with a warning if neither result is valid. Invalid mtimes never trigger deletion.

**One backup has passed restore verification:** `anzma_20261119_145637.dump`; see [`RESTORE_RUNBOOK.md`](RESTORE_RUNBOOK.md) for the recorded scratch run. Other backups remain UNVERIFIED until individually restored and checked. Archive listing and a matching checksum alone do not prove a restore works.

## Usage and settings

Run from any directory with the repository's Compose `.env` available at the repository root, or export the standard PostgreSQL variables in the shell:

```sh
scripts/backup_db.sh
scripts/backup_db.sh --dry-run
```

Default mode runs `pg_dump`, `pg_restore --list`, and the row-count query inside the configured Compose `db` service. `ANZMA_BACKUP_MODE=host` uses local `pg_dump`, `pg_restore`, and `psql` with the standard `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, and `PGPASSWORD` environment. The script does not source `.env`; Compose loads its normal project `.env`, while host mode uses variables exported by the caller.

| Variable | Default | Purpose |
|---|---|---|
| `ANZMA_BACKUP_MODE` | `compose` | `compose` or `host`. |
| `ANZMA_COMPOSE_FILE` | `deploy/compose/docker-compose.yml` | Compose file, relative to the repository root unless absolute. |
| `ANZMA_COMPOSE_PROJECT` | Compose default | Optional project passed with `-p`; set this to target the intended deployment. |
| `ANZMA_BACKUP_DB_SERVICE` | `db` | Compose database service name; override only when the selected Compose file uses a different service name. |
| `ANZMA_BACKUP_DIR` | `$HOME/anzma_backups` | Output directory; must be outside this repository. |
| `ANZMA_BACKUP_KEEP_DAYS` | `14` | Remove eligible backups older than this many days. |
| `ANZMA_BACKUP_MIN_KEEP` | `3` | Preserve at least this many newest valid backup sets. |

Each backup set is `anzma_<UTC YYYYmmdd_HHMMSS>.dump` with `.sha256` and `.rowcounts.txt` sidecars. Row counts cover telemetry, events, alerts, incidents, assets, buildings, locations, data sources, source credentials, external asset mappings, ingestion batches, ingestion rejections, and equipment state records. Retention never removes unrelated files. `--dry-run` prints the planned output and eligible deletions without creating or deleting anything.

## Scheduling examples

The examples below are **NOT TESTED in this repository**. Run them as an OS account that can access Docker and the intended Compose project, or the host PostgreSQL credentials. Protect the scheduler's environment file and logs; never put `PGPASSWORD` directly in a command line.

Example crontab entry (adjust the repository path and project):

```cron
15 2 * * * cd /opt/anzma && ANZMA_COMPOSE_PROJECT=anzma-insight scripts/backup_db.sh >> /var/log/anzma-backup.jsonl 2>&1
```

Example systemd service and timer pair:

```ini
# /etc/systemd/system/anzma-backup.service
[Unit]
Description=Create Anzma PostgreSQL backup

[Service]
Type=oneshot
User=anzma
WorkingDirectory=/opt/anzma
EnvironmentFile=/etc/anzma/backup.env
ExecStart=/opt/anzma/scripts/backup_db.sh
```

```ini
# /etc/systemd/system/anzma-backup.timer
[Unit]
Description=Daily Anzma PostgreSQL backup

[Timer]
OnCalendar=*-*-* 02:15:00
Persistent=true

[Install]
WantedBy=timers.target
```

## Checking and collecting results

- Check the last cron run in the configured log (`tail -n 1 /var/log/anzma-backup.jsonl`) and inspect the backup directory for its dump and both sidecars.
- For systemd, use `systemctl list-timers anzma-backup.timer` and `systemctl status anzma-backup.service`; retrieve the last JSON output with `journalctl -u anzma-backup.service -n 1 --no-pager`.
- Collect the final stdout JSON line with the scheduler's normal log collector. Successful runs report `BACKUP_OK`; failures report `BACKUP_FAILED`; each pruned set reports `RETENTION_DELETED`. The JSON contains the filename, size, elapsed seconds, and UTC timestamp, never credentials.

The restore process and its last verified scratch run are recorded in [`RESTORE_RUNBOOK.md`](RESTORE_RUNBOOK.md). A successful check of one archive does not verify other backups.
