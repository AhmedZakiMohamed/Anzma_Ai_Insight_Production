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
    parser.parse_args()
    conn = da.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "anzma"),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", ""),
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
            cur.execute("SELECT to_regclass('public.schema_migrations') AS migration_table")
            exists = cur.fetchone()["migration_table"] is not None
            for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
                if int(path.name[:3]) < 6:
                    continue
                version = path.stem
                if exists:
                    cur.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (version,))
                    if cur.fetchone():
                        print(f"SKIP {version} (already recorded)")
                        continue
                else:
                    print("No schema_migrations ledger yet; applying additive migration 006 to create it.")
                if version == "013_building_code_required":
                    registered = _register_seed_building_codes(cur)
                    if registered:
                        print(f"REGISTERED {registered} seeded building code(s)")
                cur.execute(path.read_text(encoding="utf-8"))
                conn.commit()
                exists = True
                print(f"APPLIED {version}")
            if not exists:
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
