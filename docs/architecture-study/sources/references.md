# Sources and evidence register

**Research snapshot:** 25 November 2026. URLs and prices can change; re-check them before an architecture or procurement decision. Access dates below are 25 November 2026 unless stated otherwise. Several vendor pages are rolling documentation and do not show a stable publication/update date; where a date is unavailable, this register does not infer one.

## Repository evidence inspected

These are primary sources for the current implementation assessment. Repository paths refer to the checkout at the time of this study; uncommitted changes were present, so this is not a release certification.

| Source | What it supports |
|---|---|
| [`README.md`](../../../README.md) | Product overview, documented demo hardware/model claim; not a measured minimum. |
| [`docs/ARCHITECTURE.md`](../../ARCHITECTURE.md), [`docs/CORE_DATA_ARCHITECTURE.md`](../../CORE_DATA_ARCHITECTURE.md) | Core layers, data responsibilities and known limitations. |
| [`docs/CONNECTOR_ARCHITECTURE.md`](../../CONNECTOR_ARCHITECTURE.md), [`docs/INGESTION_CONTRACT.md`](../../INGESTION_CONTRACT.md) | Simulator separation, HTTP ingestion contract and source-scoped mapping. |
| [`docs/DEPLOYMENT_STANDARD.md`](../../DEPLOYMENT_STANDARD.md), [`deploy/compose/docker-compose.yml`](../../../deploy/compose/docker-compose.yml), [`deploy/compose/Dockerfile`](../../../deploy/compose/Dockerfile) | Compose services, ports, Python image and Uvicorn startup. |
| [`requirements.txt`](../../../requirements.txt) | Python dependency versions. |
| [`app/domain/ai/model_provider.py`](../../../app/domain/ai/model_provider.py), [`app/adapters/model_providers/ollama_provider.py`](../../../app/adapters/model_providers/ollama_provider.py) | Provider protocol and implemented Ollama adapter. |
| [`app/api/chat_lite.py`](../../../app/api/chat_lite.py), [`app/services/pipeline.py`](../../../app/services/pipeline.py), [`app/domain/evidence/grounding.py`](../../../app/domain/evidence/grounding.py) | Chat, deterministic tools, explanation pipeline and grounding boundary. |
| [`app/security.py`](../../../app/security.py), [`app/services/rate_limit.py`](../../../app/services/rate_limit.py) | REST authentication boundary and per-process ingestion rate-limit implementation. |
| [`scripts/backup_db.sh`](../../../scripts/backup_db.sh), [`docs/BACKUP.md`](../../BACKUP.md), [`scripts/freshness_job.py`](../../../scripts/freshness_job.py) | Backup process/runbook and optional freshness evaluator entry point. |
| User-supplied `Anzma_AI_Insight_Self_Service_Blueprint_4_Visuals (Promise).docx` and four concept images | Target experience/design references only. The blueprint labels its illustrations as concepts, not implemented UI screenshots. |

## Official model and runtime sources

| Source | Publisher | Use in study |
|---|---|---|
| [Qwen2.5 7B Instruct on Ollama](https://ollama.com/library/qwen2.5%3A7b-instruct) | Ollama / model registry | Exact local tag, downloadable artifact size, quantization and context listing. The installed host models were separately observed in the development environment; this registry page does not prove that installation. |
| [Qwen2.5 3B on Ollama](https://ollama.com/library/qwen2.5%3A3b) | Ollama / model registry | Pilot artifact size, parameter count, Q4_K_M quantization, listed context and Qwen license. Registry claims are not ANZMA performance results. |
| [Qwen2.5 model card](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct) | Alibaba Qwen / Hugging Face | Parameters, model family language claims, license and model-card limitations. |
| [Qwen3 8B on Ollama](https://ollama.com/library/qwen3%3A8b) and [Qwen3 family page](https://ollama.com/library/qwen3) | Ollama / Alibaba Qwen | Candidate tag’s model file/context listing and family language/tool-capability claims. Candidate only; not installed or benchmarked for this project. |
| [Qwen3 8B model card](https://huggingface.co/Qwen/Qwen3-8B) | Alibaba Qwen / Hugging Face | Parameters, license and base model details; check derivative-specific terms. |
| [Llama 3.2 on Ollama](https://ollama.com/library/llama3.2) and [Llama 3.2 model card](https://huggingface.co/meta-llama/Llama-3.2-3B-Instruct) | Ollama / Meta | 3B artifact listing, context, Meta language list and community license. |
| [Llama 3.3 tags on Ollama](https://ollama.com/library/llama3.3/tags) and [Llama 3.3 model card](https://huggingface.co/meta-llama/Llama-3.3-70B-Instruct) | Ollama / Meta | 70B artifact/context listing, supported languages and community license. |
| [OpenAI model catalog](https://developers.openai.com/api/docs/models) and [API pricing](https://developers.openai.com/api/docs/pricing) | OpenAI | GPT-6 Luna / GPT-6.1 Terra identifiers, current context/tool claims and token rates. The price worksheet uses uncached standard short-context text rates at the research snapshot. |
| [Gemini Developer API pricing](https://ai.google.dev/gemini-api/docs/pricing) and [Gemini API model list](https://ai.google.dev/gemini-api/docs/models) | Google | Gemini 3.5 Flash-Lite identifier and paid-tier input/output rates. Verify current context/tool-call requirements on the selected API surface before design approval. |
| [Claude Sonnet 5.5](https://www.anthropic.com/claude-sonnet-5-5) and [Claude API tool use](https://docs.anthropic.com/en/docs/build-with-claude/tool-use) | Anthropic | Exact model identifier, current announced token pricing and tool-use capability. Performance statements on vendor pages are vendor benchmarks, not ANZMA results. |
| [Ollama documentation](https://docs.ollama.com/) | Ollama | Local inference runtime operations and API. Ollama is not a model. |
| [vLLM installation and supported hardware](https://docs.vllm.ai/en/stable/getting_started/installation/) and [vLLM CPU backend](https://docs.vllm.ai/en/latest/getting_started/installation/cpu/) | vLLM project | Runtime platform support and CPU caveats. vLLM is not a model. |
| [NVIDIA GeForce RTX 4090 specifications](https://www.nvidia.com/en-us/geforce/graphics-cards/40-series/rtx-4090/) | NVIDIA | Published 24 GB GDDR6X capacity used only as a reference point, not a server recommendation. |

## Official infrastructure and database sources

| Source | Publisher | Use in study |
|---|---|---|
| [Compose networking](https://docs.docker.com/compose/how-tos/networking/) | Docker | Service-name DNS and project-network scope; not proof of cross-host routing. |
| [Docker Engine installation on Ubuntu](https://docs.docker.com/engine/install/ubuntu/) | Docker | Illustrative operator installation sequence. Commands are marked NOT TESTED in this repository. |
| [PostgreSQL 16 backup and restore](https://www.postgresql.org/docs/16/backup.html) | PostgreSQL Global Development Group | Logical dump vs physical backup considerations. |
| [PostgreSQL 16 continuous archiving and PITR](https://www.postgresql.org/docs/16/continuous-archiving.html) | PostgreSQL Global Development Group | WAL archiving and point-in-time recovery requirements. |
| [`pg_dump` reference](https://www.postgresql.org/docs/16/app-pgdump.html) | PostgreSQL Global Development Group | Logical dump behavior and options. |

## Evidence classification

- **Repository fact:** verified by source files or recorded read-only environment observation. Does not establish a customer production configuration.
- **Official specification:** vendor/model/runtime documentation; may change and is not an ANZMA test.
- **Vendor-published benchmark:** attributable to the vendor and must be read with its test setup; not transferable as an ANZMA latency/accuracy claim.
- **Engineering estimate:** sizing, workload profiles and architecture recommendations in the study; must be replaced by customer measurements and a representative benchmark.
- **Unknown:** customer site counts, network, legal terms, data residency, procurement price, service objectives, and acceptance thresholds remain open.
