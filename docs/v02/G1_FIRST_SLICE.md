# G1 first slice: current implementation state

Handover summary and remaining actions: [`G1_HANDOVER_2026-11-26.md`](G1_HANDOVER_2026-11-26.md).

Working baseline: `main` at `58511df37d1836ccb804f92ad012f8401495c02b` (the commit named in the architecture, `bdd8d3d2d06e2914293727992c67daf04c5d3f11`, is its ancestor).

## Implemented in the working tree

- Separate synthetic REST source: bearer authentication, bounded page size, cursor pagination, start-inclusive/end-exclusive UTC history filtering.
- Separate connector: bounded history pages, cursor-loop detection, bounded retries for upstream/Core transient failures, stable event IDs, and translation through existing ingestion batch contract 1.0.
- Canonical vocabulary migration for explicit `door_opened`, `offline_start`, and `offline_end`; `access_granted` stays a distinct stored event and is not mapped to physical opening, and silence is not mapped to offline.
- Typed Core analyses for building-local after-hours door events and explicit outage session counts, with event evidence references and stable tie ordering.
- Direct chat routing for those two intents when the user gives two ISO dates. The response states that upstream completeness is unknown and limits its claims to stored Core records.
- Existing chat widget renders the returned structured result as a text-only table alongside the answer and event evidence.
- Focused positive/negative unit tests added for reference REST auth/pagination, canonical mapping, source fixture expectations, timezone evidence, and outage sessionization.

## Verification status (2026-11-24)

This is an initial G1 implementation slice, not an accepted milestone. Verification used a dedicated disposable Docker network and database; the shared `anzma-core-*` stack was not used. All disposable G1 containers and the network were removed after the run.

- Connector end-to-end: 8 fixture events fetched; 8 accepted by Core; 0 rejected. A second delivery produced 8 duplicates, 0 new records, and 0 rejected records.
- Chat acceptance: 3/3 passed, including required authentication, after-hours-door evidence parity, and offline-ranking parity. Door query returned the expected two fixture events (`evt-door-001`, `evt-door-003`); offline ranking returned the expected two mapped devices with one incident each. Responses disclose upstream completeness as unknown.
- Focused unit suite: 11/11 passed, including pagination/auth, mapping, invalid timestamps and event types, fixture expectations, partial Core rejection, timezone evidence, outage sessionization, and date boundaries.
- `git diff --check` and `node --check app/dashboard/static/chat-widget.js` passed.
- Browser visual check was attempted after the temporary API container stopped responding; `127.0.0.1:18480` refused the connection. UI visual behavior is therefore not verified.

This is not yet an accepted milestone. The fixture and connector are synthetic, external IDs must be registered/mapped before ingestion, and migration 014 must be applied. The result envelope has no retained result snapshot yet. At the time of the initial run, source synchronization/checkpoint handling was absent; see the 26 November update below for the local connector checkpoint/outbox and its limitations. Core still does not persist end-to-end coverage or connector health. The recognized chat intent currently requires two explicit `YYYY-MM-DD` dates and does not yet parse building names or natural-language date ranges. Changes remain uncommitted and analytics/event semantics require Ahmed's review.

## G1 engineering update (26 November 2026)

The adapter now rejects pages that omit or mis-type the `complete` field. The command-line runner processes one page at a time and stores a SQLite checkpoint plus the exact pending Core batch in a local outbox. It advances the source cursor only after Core confirms full acceptance. If Core accepted a batch but its response was lost, restart resends the same batch ID and payload; Core's existing idempotency behavior can return the recorded result. A local file lock prevents simultaneous runs against the same state file. Completed source/building/time ranges are skipped on rerun; incomplete ranges restart from the beginning. Use a persistent, access-controlled volume for container deployments. Vendor cursor lifetime across restart must be qualified separately.

The focused reference REST/analytics unit module passes 19/19, including lost-response replay, completed-range skipping, incomplete-coverage handling, and concurrent-run rejection. This local connector state is not a Core checkpoint service: Core still does not retain/expose end-to-end sync completeness, and analytics coverage therefore remains `UNKNOWN`. The connector is synthetic; no real vendor or source credentials were qualified. This is still an unaccepted technical slice, not completion of the CEO guided-connection gate.

The 26 November disposable-stack recovery run passed against the real Core HTTP API and a separate temporary PostgreSQL database: eight fixture events were accepted, the test simulated loss of Core's successful response, the connector replayed the exact saved batch, the database retained one batch/eight unique events, and the completed range was skipped on a subsequent run. The disposable project, volume, and image were removed; the shared `anzma-core-*` containers remained running. The test is synthetic reference evidence only.

A second isolated full-path run on 26 November registered a disposable source, mapped four synthetic external IDs, started the reference HTTP API as a separate process, and ran the connector through Core ingestion: **8 received, 8 accepted, 0 rejected**. The guided `/setup` connection check passed against that source. The G1 chat/API acceptance suite then passed **3/3**: operator authentication, after-hours doors matching cited event evidence, and offline incident ranking matching cited start events. This demonstrates the synthetic end-to-end reference path; it is not real-vendor or independent QA evidence.

## G1 setup preview (26 November 2026)

An initial `/setup` page and authenticated `POST /api/v1/connectors/test-connection` route now provide the first guided step for the synthetic reference connector. A disposable runtime smoke check passed: the page rendered behind authentication; an approved reference origin connected and returned `history.events`; an invalid source credential returned a sanitized failure; and an unapproved origin was rejected before outbound access. The check used a temporary API container and reference source on an isolated network with no database connection. Both containers, the network, the temporary volume and image were removed; the shared `anzma-core-*` services remained running.

The route only connects to an exact operator-allowlisted origin; HTTP requires a separate allowlist. It rejects redirects, caps the request body at 8 KiB, uses a secret-typed credential, does not persist the entered credential, and returns sanitized connection status, latency, and manifest capabilities. Focused runtime checks confirmed that invalid credentials fail safely, unapproved origins are rejected, and an overlong credential is rejected without being echoed. The page explicitly labels this as an engineering reference, not a certified support tier. This is a verified connection-test slice, **not an accepted G1 gate**: it does not create a source, save settings, discover/map assets, or begin ingestion. Scoped setup roles, secure storage for saved configuration, and a real vendor remain unresolved.

## Semantics in this slice

- Door time windows use each building's configured IANA timezone; dates are inclusive and the after-hours comparison is inclusive at the configured local clock (default 20:00).
- Offline incidents begin with an explicit `offline_start`, close with `offline_end`, and repeated starts while active do not add counts. A start before the requested range initializes the active state; a missing recovery remains open. Each counted start is evidence.
- An empty query result means no matching stored Core records; it does not prove that the upstream system had no matching events.
