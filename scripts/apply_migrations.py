"""Apply unapplied additive database migrations (currently 006+).

Usage: python scripts/apply_migrations.py
Reads PostgreSQL connection settings from PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.adapters.database import db_adapter as da


MIGRATIONS = ROOT / "db" / "migrations"
LOCK_KEY = 718230641
LOCAL_TARGETS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}


def _inside_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _validate_target(host: str, dbname: str, *, allow_host_target: str | None,
                     yes: bool, dry_run: bool, inside_container: bool) -> None:
    normalized = host.strip().lower().strip("[]").rstrip(".")
    if allow_host_target is not None and allow_host_target != dbname:
        raise ValueError("--allow-host-target must exactly match PGDATABASE.")
    if normalized in LOCAL_TARGETS and allow_host_target != dbname:
        raise ValueError(
            f"Refusing local host target {host!r}; pass --allow-host-target {dbname} "
            "only after verifying the intended database."
        )
    if not inside_container and not dry_run and not yes:
        raise ValueError("Non-container migration execution requires --yes.")


def _pending_migrations(cur) -> tuple[int, list[Path]]:
    cur.execute("SELECT to_regclass('public.schema_migrations') AS migration_table")
    exists = cur.fetchone()["migration_table"] is not None
    ledger_rows = 0
    applied: set[str] = set()
    if exists:
        cur.execute("SELECT count(*) AS n FROM public.schema_migrations")
        ledger_rows = cur.fetchone()["n"]
        cur.execute("SELECT version FROM public.schema_migrations")
        applied = {row["version"] for row in cur.fetchall()}
    pending = [path for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"))
               if int(path.name[:3]) >= 6 and path.stem not in applied]
    return ledger_rows, pending

def _register_seed_building_codes(cur) -> int:
    """Give the two repository seed buildings stable codes before migration 013.

    These names are the explicit rows in db/init/anzma_schema_and_seed.sql. Only
    NULL/blank codes are filled; operator-provided values are never overwritten.
    Other buildings remain subject to migration 013's actionable preflight.
    """
    cur.execute(
        """UPDATE buildings
              SET building_code = CASE name
                  WHEN 'Main Tower' THEN 'MAIN-TOWER'
                  WHEN 'Data Center Annex' THEN 'DATA-CENTER-ANNEX'
              END
            WHERE name IN ('Main Tower', 'Data Center Annex')
              AND (building_code IS NULL OR btrim(building_code) = '')"""
    )
    return cur.rowcount


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-host-target", metavar="DBNAME",
                        help="explicitly permit a localhost-like target only when PGDATABASE matches")
    parser.add_argument("--yes", action="store_true",
                        help="confirm migrations when the runner is executed outside a container")
    parser.add_argument("--dry-run", action="store_true",
                        help="list pending migrations without applying changes")
    args = parser.parse_args()
    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    dbname = os.getenv("PGDATABASE", "anzma")
    try:
        _validate_target(host, dbname, allow_host_target=args.allow_host_target,
                         yes=args.yes, dry_run=args.dry_run, inside_container=_inside_container())
    except ValueError as exc:
        parser.error(str(exc))

    print(f"Migration target: host={host} port={port} database={dbname}")
    conn = da.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", ""),
    )
    try:
        with conn.cursor() as cur:
            if not args.dry_run:
                cur.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
            ledger_rows, pending = _pending_migrations(cur)
            print(f"schema_migrations rows: {ledger_rows}")
            if args.dry_run:
                print("Pending migrations:")
                for path in pending:
                    print(f"  {path.stem}")
                if not pending:
                    print("  none")
                return 0
            if not pending and ledger_rows == 0:
                raise RuntimeError("No applicable migration created schema_migrations")
            for path in pending:
                version = path.stem
                if ledger_rows == 0 and version == pending[0].stem:
                    print("No schema_migrations ledger yet; applying additive migration 006 to create it.")
                if version == "013_building_code_required":
                    registered = _register_seed_building_codes(cur)
                    if registered:
                        print(f"REGISTERED {registered} seeded building code(s)")
                cur.execute(path.read_text(encoding="utf-8"))
                conn.commit()
                ledger_rows += 1
                print(f"APPLIED {version}")
            if ledger_rows == 0:
                raise RuntimeError("No applicable migration created schema_migrations")
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
