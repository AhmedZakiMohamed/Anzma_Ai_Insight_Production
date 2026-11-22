import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "backup_db.sh"


class BackupRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.out = self.root / "backups"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self._tool("pg_dump", '#!/bin/sh\n[ "${FAKE_DUMP_FAIL:-0}" = 1 ] && exit 9\nprintf FAKE-ARCHIVE\n')
        self._tool("pg_restore", '#!/bin/sh\n[ "$1" = --list ] || exit 2\nif [ "$2" = - ]; then data=$(cat); else data=$(cat "$2"); fi\n[ "$data" = FAKE-ARCHIVE ]\n')
        self._tool("psql", '#!/bin/sh\ncat <<EOF\ntelemetry_records=1\nevent_records=2\nalerts=3\nincidents=4\nassets=5\nbuildings=6\nlocations=7\ndata_sources=8\nsource_credentials=9\nasset_external_ids=10\ningestion_batches=11\ningestion_rejections=12\nequipment_state_records=13\nEOF\n')
        self.env = os.environ.copy()
        self.env.update({
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "ANZMA_BACKUP_MODE": "host",
            "ANZMA_BACKUP_DIR": str(self.out),
            "ANZMA_BACKUP_KEEP_DAYS": "14",
            "ANZMA_BACKUP_MIN_KEEP": "2",
            "PGHOST": "127.0.0.1",
            "PGDATABASE": "fake",
            "PGUSER": "fake",
        })

    def tearDown(self):
        self.temp.cleanup()

    def _tool(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)

    def _seed(self, stamp, age_days):
        path = self.out / f"anzma_{stamp}.dump"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = b"FAKE-ARCHIVE"
        path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        Path(f"{path}.sha256").write_text(f"{digest}  {path.name}\n")
        Path(f"{path}.rowcounts.txt").write_text("telemetry_records=1\n")
        old = time.time() - age_days * 86400
        for sidecar in (path, Path(f"{path}.sha256"), Path(f"{path}.rowcounts.txt")):
            os.utime(sidecar, (old, old))
        return path

    def _run(self, *args, env=None):
        return subprocess.run(
            [str(SCRIPT), *args], cwd=ROOT, env=env or self.env,
            capture_output=True, text=True, check=False,
        )

    def test_retention_minimum_unrelated_and_dry_run(self):
        old = self._seed("20261101_000000", 40)
        middle = self._seed("20261102_000000", 30)
        recent_old = self._seed("20261103_000000", 20)
        recent = self._seed("20261104_000000", 10)
        unrelated = self.out / "keep-me.txt"
        unrelated.write_text("unrelated")

        before = sorted(p.name for p in self.out.iterdir())
        dry = self._run("--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), before)
        self.assertIn("Would delete", dry.stdout)

        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(old.exists())
        self.assertFalse(middle.exists())
        self.assertFalse(recent_old.exists())
        self.assertTrue(recent.exists())
        self.assertEqual(unrelated.read_text(), "unrelated")
        dumps = sorted(self.out.glob("anzma_????????_??????.dump"))
        self.assertGreaterEqual(len(dumps), 2)
        created = next(p for p in dumps if p not in {recent_old, recent})
        self.assertEqual(created.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o700)
        self.assertTrue(Path(f"{created}.rowcounts.txt").read_text().strip())
        self.assertEqual(
            hashlib.sha256(created.read_bytes()).hexdigest(),
            Path(f"{created}.sha256").read_text().split()[0],
        )
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])["event"], "BACKUP_OK")

    def test_failed_dump_leaves_no_valid_backup_and_deletes_nothing(self):
        old = self._seed("20261001_000000", 45)
        existing = sorted(p.name for p in self.out.iterdir())
        env = self.env.copy()
        env["FAKE_DUMP_FAIL"] = "1"
        result = self._run(env=env)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), existing)
        self.assertTrue(old.exists())
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])["event"], "BACKUP_FAILED")

    def test_invalid_mtime_is_skipped_without_deleting_backups(self):
        backups = [
            self._seed("20261001_000000", 45),
            self._seed("20261002_000000", 35),
            self._seed("20261003_000000", 25),
        ]
        self._tool(
            "stat",
            '#!/bin/sh\ncase "$1" in\n'
            '  -c) printf "not-a-number\\n"; exit 0;;\n'
            '  -f) printf "File: filesystem status\\n"; exit 1;;\n'
            '  *) exit 1;;\nesac\n',
        )

        result = self._run("--dry-run")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("WARNING: could not read numeric mtime; skipping retention", result.stderr)
        self.assertNotIn("Would delete", result.stdout)
        self.assertTrue(all(path.exists() for path in backups))


if __name__ == "__main__":
    unittest.main()
