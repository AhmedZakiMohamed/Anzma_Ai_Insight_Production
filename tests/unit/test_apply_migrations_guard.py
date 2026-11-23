import unittest

from scripts.apply_migrations import _validate_target


class ApplyMigrationsGuardTests(unittest.TestCase):
    def _validate(self, host, **overrides):
        options = {
            "allow_host_target": None,
            "yes": False,
            "dry_run": False,
            "inside_container": False,
        }
        options.update(overrides)
        return _validate_target(host, "anzma", **options)

    def test_local_aliases_refused_without_matching_override(self):
        for host in ("localhost", "127.0.0.1", "[::1]", "host.docker.internal"):
            with self.subTest(host=host), self.assertRaisesRegex(ValueError, "Refusing local host target"):
                self._validate(host, yes=True)

    def test_local_target_requires_exact_database_override(self):
        with self.assertRaisesRegex(ValueError, "exactly match PGDATABASE"):
            self._validate("localhost", allow_host_target="other", yes=True)
        with self.assertRaisesRegex(ValueError, "Non-container.*requires --yes"):
            self._validate("localhost", allow_host_target="anzma")
        self.assertIsNone(self._validate("localhost", allow_host_target="anzma", yes=True))

    def test_container_target_keeps_existing_invocation_compatible(self):
        self.assertIsNone(self._validate("db", inside_container=True))

    def test_host_execution_requires_confirmation_except_dry_run(self):
        with self.assertRaisesRegex(ValueError, "requires --yes"):
            self._validate("db")
        self.assertIsNone(self._validate("db", dry_run=True))
        self.assertIsNone(self._validate("db", yes=True))


if __name__ == "__main__":
    unittest.main()
