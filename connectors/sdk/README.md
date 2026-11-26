# Connector SDK contracts

`contracts.py` defines the versioned Python interface used by connector processes. A connector declares the capabilities it really implements in its manifest and implements only the matching capability protocols. Core can therefore distinguish an unsupported feature from a supported query that returned no records.

The lifecycle contract covers connection tests and health. Optional protocols cover asset/point discovery, event history, telemetry history, current state, and event subscriptions. Pages carry separate continuation and source-coverage signals: `next_cursor` indicates another page, while `complete=False` means the source reported incomplete coverage. Callers must preserve any incomplete-coverage signal across the whole paginated read. Event and telemetry timestamps use UTC.

Connectors translate vendor data to these common records and submit accepted records through the existing Core ingestion API. They do not access Core tables. Credentials belong in runtime secret/configuration handling, never in the manifest. The synthetic REST connector is the first contract example; it does not establish compatibility with any real vendor.
