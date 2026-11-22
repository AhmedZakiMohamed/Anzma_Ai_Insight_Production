# Change coordination

This ledger records reviewed ownership-sensitive edits. Amr's AI provider, grounding, validation, chat, security, deployment, and `DAY3_*` report files remain owned by Amr unless a change is explicitly reviewed and approved. Changes to provider or grounding interfaces, authentication behavior, environment variables, or Compose defaults require a design note and human approval before implementation.

| File | What changed | Why | Commit |
|---|---|---|---|
| `scripts/backup_db.sh` | Made retention mtime lookup GNU-first, BSD-fallback, numeric-only; invalid values warn and skip. | Fix Linux retention failure without deleting files on invalid metadata. | `18ac4d0` |
| `tests/unit/test_backup_retention.py` | Added regression coverage for malformed mtime output and no deletion. | Prove malformed metadata is safely skipped. | `18ac4d0` |
| `docs/BACKUP.md` | Documented the pre-fix Linux retention defect and corrected behavior. | Warn operators about earlier script versions. | This documentation commit |
