"""Checkpoint/outbox recovery against the real Core API in a disposable Compose project."""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from app.adapters.database import db_adapter as da
from connectors.reference_rest import connector, server
from connectors.reference_rest.checkpoint import SyncCheckpointStore


PROJECT = "anzma-g1-e2e-20261126"
SOURCE_CODE = os.getenv("ANZMA_G1_E2E_SOURCE_CODE", "")
SOURCE_KEY = os.getenv("ANZMA_G1_E2E_SOURCE_KEY", "")
ENABLED_PROJECT = os.getenv("ANZMA_G1_E2E_PROJECT", "")


@unittest.skipUnless(SOURCE_CODE and SOURCE_KEY and ENABLED_PROJECT == PROJECT,
                     "requires the named disposable G1 Core project and a test source key")
class ConnectorCheckpointCoreE2E(unittest.TestCase):
    START = "2026-11-01T00:00:00Z"
    END = "2026-11-07T00:00:00Z"

    def test_lost_core_response_replays_same_batch_once_and_completes_checkpoint(self):
        # The source is the separate local HTTP fixture service, not an in-process
        # fixture read. Core ingestion below is the real HTTP API and test DB.
        with patch.dict(os.environ, {"REFERENCE_API_TOKEN": "e2e-reference-token"}):
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(upstream.server_close)
            self.addCleanup(upstream.shutdown)
            self.addCleanup(thread.join, 2)

            adapter = connector.ReferenceRESTConnector(
                f"http://127.0.0.1:{upstream.server_port}", "e2e-reference-token")
            with tempfile.TemporaryDirectory() as directory:
                state_path = Path(directory) / "checkpoint.sqlite3"
                first_store = SyncCheckpointStore(state_path)
                first_batches = []

                def lose_response_after_core_commit(core_url, source_key, batch):
                    result = connector.push_to_core(core_url, source_key, batch)
                    first_batches.append(batch)
                    raise connector.ConnectorError(
                        "test simulated loss of the HTTP response after Core committed")

                with self.assertRaisesRegex(connector.ConnectorError, "simulated loss"):
                    connector.run_checkpointed_sync(
                        adapter, first_store, SOURCE_CODE, "MAIN-TOWER", self.START, self.END,
                        "http://127.0.0.1:8000", SOURCE_KEY, push=lose_response_after_core_commit)

                pending_store = SyncCheckpointStore(state_path)
                pending = pending_store.state(SOURCE_CODE, "MAIN-TOWER")
                self.assertEqual(pending["status"], "RUNNING")
                self.assertIsNone(pending["next_cursor"])
                self.assertEqual(pending["pending_batch"]["batch_id"], first_batches[0]["batch_id"])

                replayed_batches = []

                def capture_replay(core_url, source_key, batch):
                    replayed_batches.append(batch)
                    return connector.push_to_core(core_url, source_key, batch)

                result = connector.run_checkpointed_sync(
                    adapter, SyncCheckpointStore(state_path), SOURCE_CODE, "MAIN-TOWER",
                    self.START, self.END, "http://127.0.0.1:8000", SOURCE_KEY, push=capture_replay)

                self.assertEqual(replayed_batches, first_batches)
                self.assertEqual(result["status"], "COMPLETE")
                self.assertEqual(result["page_count"], 1)
                self.assertEqual(result["core_results"][0]["counts"],
                                 {"received": 8, "accepted": 8, "duplicate": 0, "rejected": 0})

                with da.connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT source_id FROM data_sources WHERE source_code=%s", (SOURCE_CODE,))
                        source_id = cur.fetchone()["source_id"]
                        cur.execute("""SELECT count(*) AS batches, sum(accepted_count) AS accepted
                            FROM ingestion_batches WHERE source_id=%s""", (source_id,))
                        batch_counts = cur.fetchone()
                        cur.execute("""SELECT count(*) AS events, count(DISTINCT source_event_id) AS unique_events
                            FROM event_records WHERE source_id=%s""", (source_id,))
                        event_counts = cur.fetchone()
                self.assertEqual(batch_counts["batches"], 1)
                self.assertEqual(batch_counts["accepted"], 8)
                self.assertEqual(event_counts["events"], 8)
                self.assertEqual(event_counts["unique_events"], 8)

                no_repeat = connector.run_checkpointed_sync(
                    adapter, SyncCheckpointStore(state_path), SOURCE_CODE, "MAIN-TOWER",
                    self.START, self.END, "http://127.0.0.1:8000", SOURCE_KEY,
                    push=lambda *_: self.fail("completed range must not submit a new batch"))
                self.assertTrue(no_repeat["skipped"])


if __name__ == "__main__":
    unittest.main()
