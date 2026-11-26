### S0 — Evaluation PC

| Item | Planning setup |
|---|---|
| Purpose | Demo, sales, and internal testing; not a production site |
| Functions | Dashboard, sample data, basic chat; no live vendor integration assumed |
| Containers / services | API + dashboard, PostgreSQL, optional local Ollama; no production connector or monitoring service assumed |
| AI | Small model; one request at a time; Arabic quality and response time untested |
| Device | 8 CPU cores, 32 GB RAM, 512 GB SSD; optional 6–8 GB GPU |
| Data and network | Seeded or simulated data; local access |
| Graphite | Optional for a demo; budget some additional RAM and storage if metrics are retained |
| Pros | Lowest-cost way to show the product; easy to reset and demonstrate |
| Cons | Not production sizing; no live building data, production security, backup or recovery qualification |
| Best fit | Product demonstration and engineering evaluation |

### S1 — On-prem all-in-one

| Item | Planning setup |
|---|---|
| Purpose | First live single-site deployment where the customer allows the Core to reach approved building-system APIs |
| Functions | Core API and dashboard, PostgreSQL, freshness worker, local AI, connector container on the Core |
| Containers / services | Docker Compose; private service network; HTTPS reverse proxy; DB and model endpoints kept private |
| AI | Start with CPU inference and one request at a time, or add a GPU after a representative workload benchmark. The earlier 7–8B Q4 and 2–4 simultaneous responses are targets to validate |
| Device | Planning tier: 16 CPU cores, 64 GB RAM, 2 TB NVMe; optional 16–24 GB GPU. Teammate’s lower-cost pilot target of 4 vCPU / 8 GB / 250–500 GB is explicitly not tested and should not be treated as production sizing |
| Data and network | Live data; approved routes from the Core to building APIs; read-only; no video storage, though CCTV event metadata can still be sensitive |
| Graphite | Add only if the customer needs monitoring; size its RAM and disk separately based on metric volume and retention |
| Pros | One server to install, patch, back up, and support; simplest live topology; data stays on site |
| Cons | One failure point; database, API, and model compete for server resources; some OT teams will not allow Core-to-building-system routes |
| Best fit | Single-site pilot or deployment with an IT-approved network path |

### S2 — On-prem Core plus site gateway

| Item | Planning setup |
|---|---|
| Purpose | Live site where building-system access must stay at the OT boundary |
| Functions | Core API, dashboard, database, and AI stay together on the Core; gateway runs near building systems and sends normalized data to the Core |
| Containers / services | Core: Compose stack and reverse proxy. Gateway: connector service and local disk buffer if designed and implemented |
| AI | Same AI options and Core sizing as S1; gateway does not run the model |
| Device | Core: 16 CPU cores, 64 GB RAM, 2 TB NVMe, optional 16–24 GB GPU. Gateway: 4 CPU cores, 16 GB RAM, 256 GB SSD, two network interfaces |
| Data and network | Read-only source access from gateway; gateway-to-Core traffic over an approved route. Enforce firewall allowlists and prevent the gateway from routing between OT and IT networks |
| Graphite | Prefer centralized monitoring on the Core or an approved monitoring service; avoid adding a separate monitoring server at each site unless required |
| Pros | Only the gateway touches building systems; Core stays in the IT zone; can support a controlled store-and-forward design if buffering and replay are built and tested |
| Cons | Two devices to install, secure, update, monitor, and support; buffering and replay are not verified current features; gateway separation needs careful network configuration |
| Best fit | Sites with strict OT segmentation or a policy that prevents the Core from connecting directly to building systems |

### S3 — Private cloud Core plus site gateway

| Item | Planning setup |
|---|---|
| Purpose | Multi-site deployment where the customer accepts a private-cloud environment and outbound site connectivity |
| Functions | Core API, dashboard, database, and optional local AI run in a customer or provider-managed private VM/tenant; gateway connects each site |
| Containers / services | Cloud-hosted Core stack; site gateway/connectors; VPN or approved outbound connection; private network between Core services |
| AI | Local model in the private environment avoids sending prompts to a model provider. A hosted LLM would be a separate option requiring approval and an implemented provider adapter |
| Device | Core VM: planning target of 16 vCPU, 64 GB RAM, 1–2 TB managed disk plus snapshots. Each site gateway: 4 cores, 16 GB RAM, 256 GB SSD, two network interfaces |
| Data and network | Site data leaves the building for the private environment; confirm region, backup location, access, retention, and customer approval |
| Graphite | Run monitoring centrally if approved; include its storage and retention in the cloud estimate |
| Pros | Central updates and monitoring across sites; no Core server room needed at each customer site; cloud VM can be resized |
| Cons | Monthly hosting cost; depends on network connectivity; region and backup location matter; private cloud does not automatically provide high availability |
| Best fit | Multi-site customers with an approved private-cloud operator and clear data-residency rules |

## Prototype technology already working

| Workflow / component | Current prototype status |
|---|---|
| Source and sample data | The demo seeds synthetic data for 5 systems: HVAC, Network, Access control, CCTV, and Fire alarm; 12 sample assets. No live customer vendor is connected. |
| Ingestion | FastAPI accepts versioned, source-authenticated batches for telemetry, events, and equipment state. It maps each source’s external asset IDs, handles duplicate batches, and records rejected data for review. |
| Database | PostgreSQL 16 stores telemetry and events separately and links them to assets and system catalogues. Persistent Docker volume keeps data across normal restarts; schema changes are managed through SQL migrations. |
| Operational processing | Deterministic rules detect alerts. Related alerts are grouped into incidents; operators can acknowledge or resolve them. Asset health, trends, and evidence come from stored records. |
| Freshness and source status | A separate, optional freshness worker evaluates expected reporting, data gaps, and source liveness. Missing data is not treated as proof that equipment is offline. |
| Dashboard and events | A browser dashboard shows system/asset health, alerts, incidents, and trends. Socket.IO sends new-alert updates to connected dashboards. Operator access currently uses a shared deployment token; named-user roles are not verified. |
| AI service | The ModelProvider boundary currently has an Ollama adapter. The Compose default is Qwen2.5:3b. AI explanations use available evidence and can fall back to deterministic explanations; response quality and performance need benchmarking. |
| Current deployment package | Docker Compose includes PostgreSQL, API/dashboard, Ollama/model setup, and an optional freshness worker. There is no production vendor connector service, Graphite monitoring stack, GraphRAG service, or high-availability deployment in the current package. |

## Scale path from the current prototype

| Scale step | What to add next |
|---|---|
| 1 · Add vendors | Build one read-only adapter per approved vendor/protocol. Each translates vendor data into the existing versioned ingestion contract, maps external assets, and reports sync health. Qualify each real vendor; the simulator is not vendor certification. |
| 2 · Grow the data model | Keep canonical asset identity and source-specific external IDs. Add new metrics/events and mappings through reviewed migrations and catalogue updates. Keep quality, freshness, source coverage, and equipment state distinct. |
| 3 · Scale PostgreSQL | Measure records/day, row/index/WAL growth, query latency, retention, and restore time. Tune indexes and retention first. Consider partitioning, a separate DB host, replicas, or HA only when measured load and recovery targets justify them. |
| 4 · Scale API and workers | Start with the current single API service and optional freshness worker. Before multiple API replicas, add shared rate limiting/session state, coordinated background jobs, and a shared Socket.IO transport; then load-test the complete path. |
| 5 · Upgrade the LLM | Keep deterministic analytics and evidence retrieval as the source of facts. Benchmark stronger bilingual models against fixed Anzma questions, context size, concurrency, latency, and peak RAM/VRAM. Add GPU or a separate inference host only when results require it. |
| 6 · Add GraphRAG if intended | If “stronger Graphite” means GraphRAG, treat it as a future retrieval layer over approved manuals, procedures, and asset/vendor relationships. Return cited, relevant passages to the LLM; do not send the whole database. It adds retrieval/index services and resource use, and retrieved context can increase prompt tokens. |
| 7 · Add Graphite monitoring if intended | If you mean Graphite metrics monitoring, it is a separate observability service, not an LLM feature. It stores host, container, API, database, connector, and model metrics. Size its RAM and disk from metric count, sampling interval, and retention; it does not consume LLM tokens. |
| 8 · Production deployment gates | Add and verify TLS, named-user access/RBAC, secret handling, connector isolation, gateway buffering/replay where needed, backup restore, monitoring, and customer-like install/upgrade before describing a tier as production-ready. |

## Workload assumptions and prototype check

| Input | Planning assumption | Audit against the current prototype |
|---|---|---|
| Standard site | About 3,000 points, 5-minute cadence, 90 days online (~78M telemetry rows). | Arithmetic is correct if 3,000 is total points and every point reports each interval. Confirm point scope and measure actual row/index/WAL growth. |
| Scale unit | 2 buildings, 5 systems, 248 assets. | Five system types match the demo, but current seed is 12 assets; 248 assets is a scale target, not current prototype volume. |
| Users | 10 concurrent users, 5 roles. | Planning assumption. Current operator access uses a shared deployment token; named-user RBAC and five roles are not verified. |
| AI | 7–8B Q4, Arabic and English, 2–4 simultaneous chat responses. | Planning target. Compose defaults to Qwen2.5:3b; bilingual quality, peak RAM/VRAM, concurrency, and latency need a representative benchmark. |
| Services | PostgreSQL 16, API/dashboard, freshness worker, Ollama, connector container. | DB/API/Ollama/model setup are in Compose; freshness is optional. A production connector service is not in Compose; simulator/contract is separate. |
| Data boundary | Read-only; no video footage storage, CCTV events only. | Matches the intended trust boundary. Current data is synthetic; event metadata can still be sensitive and needs customer policy. |

## One-Core RAM estimate

| Component | Planning estimate | Sizing note |
|---|---|---|
| OS + Docker | 4 GB | Baseline allowance; measure on the selected host and container limits. |
| PostgreSQL | 8–16 GB | Depends on active dataset, indexes, cache, queries, WAL, and retention; validate on representative data. |
| API, Socket.IO, freshness, reports | 3–4 GB | Current demo API and optional freshness worker; production reports/traffic are not load-tested. |
| Connector container | 1–2 GB | Allowance for a future connector service; no production connector service is currently packaged. |
| 7–8B model on CPU | 6–10 GB | Planning estimate only; context length, parallel requests, runtime, and prompt size change usage. |
| Subtotal | 22–36 GB | Sum of listed estimates. |
| Subtotal + 30% headroom | 28.6–46.8 GB | Can be rounded to about 32–48 GB as a planning band. |
| Recommended Core tier | 64 GB system RAM | Adds practical margin above the estimate; confirm with peak-load measurements. |

GPU VRAM is separate from system RAM, but GPU inference still uses host RAM for the OS, runtime, API, buffers, and data. Do not assume the model uses zero host memory.

## Deployment specifications by scenario

| Component | S0 · Evaluation | S1 · All-in-one | S2 · Core + gateway | S3 · Private cloud + gateway |
|---|---|---|---|---|
| Core CPU | 8 cores | 16 cores | 16 cores | 16 vCPU (not equivalent to 16 physical cores) |
| Core RAM | 32 GB | 64 GB (32 GB absolute minimum proposal) | 64 GB | 64 GB |
| Core storage | 512 GB SSD | 2 TB NVMe | 2 TB NVMe | 1–2 TB managed disk + snapshots |
| GPU | Optional 6–8 GB | 16–24 GB candidate | 16–24 GB candidate | Cloud GPU ~24 GB class if local model hosting is selected |
| AI model | 3–4B demo model | 7–8B Q4 target | 7–8B Q4 target | 7–8B target; provider/runtime choice remains open |
| Network | Local only | 1 GbE + approved routes to building-system APIs | 1 GbE IT network; gateway touches OT sources | Internet/VPN; outbound site connection |
| Gateway | None | Connector runs on Core | 4 cores, 16 GB RAM, 256 GB SSD, 2 network ports | Same gateway profile as S2 |
| Backup | Not required for throwaway demo | Separate NAS/disk; ~2× DB size is a placeholder | Separate protected target; size from retention/restore need | Cloud snapshots + separate offsite copy |
| Data | Seeded/simulated | Live, read-only | Live, read-only | Live, multi-site; residency approval required |

S1–S3 use the same proposed 64 GB / 2 TB Core tier; S2/S3 add a gateway. All sizing is provisional and subject to benchmark, quote, retention, and recovery targets.

## CPU versus GPU for the local AI Core

| Decision area | CPU-only Core | Core with 16–24 GB GPU |
|---|---|---|
| Chat answer time (7–8B, ~300 words) | About 20–60 s each (UNVERIFIED) | About 3–8 s (UNVERIFIED) |
| 10 users / AI concurrency | Requests queue; waits could grow at busy times. | 2–4 parallel responses is a target to test, not a guarantee. |
| Arabic and English | May require smaller model or accept slower responses; quality needs evaluation. | More room for 7–8B models; Arabic quality still needs evaluation. |
| Memory | Uses system RAM; model/runtime/context compete with Core services. | VRAM holds model workload where supported; host RAM is still needed. Context and concurrent sequences add memory. |
| Cost | Lower initial hardware cost. | Likely the largest single hardware cost; confirm server/card/support compatibility. |
| Proposed use | Demo or low-concurrency pilot, if measured performance is acceptable. | Candidate production tier only if bilingual quality, latency, concurrency, and recovery tests pass. |

Suggested path: S1 = 16 cores / 64 GB / 2 TB; add 16–24 GB GPU only if the benchmark supports the need. S2 = same Core + gateway; S3 = roadmap; S0 = demo. All latency and concurrency values remain unverified.
