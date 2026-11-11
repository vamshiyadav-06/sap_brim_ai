# Clyptus.AI: SAP BRIM Knowledge Assistant

[![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688.svg?style=flat&logo=fastapi)](https://fastapi.tiangolo.com/)
[![React 19](https://img.shields.io/badge/Frontend-React%2019-61DAFB.svg?style=flat&logo=react)](https://react.dev/)
[![TypeScript](https://img.shields.io/badge/Language-TypeScript-3178C6.svg?style=flat&logo=typescript)](https://www.typescriptlang.org/)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB.svg?style=flat&logo=python)](https://www.python.org/)
[![PostgreSQL + pgvector](https://img.shields.io/badge/Database-PostgreSQL%20%2B%20pgvector-336791.svg?style=flat&logo=postgresql)](https://github.com/pgvector/pgvector)
![License: Proprietary](https://img.shields.io/badge/License-Proprietary-red.svg)

Clyptus.AI is a retrieval-augmented generation (RAG) assistant for **SAP Billing and Revenue Innovation Management (BRIM)** and related SAP S/4HANA topics: Subscription Order Management (SOM), Convergent Charging (CC), Convergent Invoicing (CI) and Contract Accounting (FI-CA). It answers questions from an indexed library of SAP documentation. Each answer is checked against the retrieved evidence before it is shown, and the assistant declines when it cannot back an answer up.

The application has three parts: a FastAPI backend, a React single-page frontend, and PostgreSQL with pgvector. It serves three audiences: individual consultants, organisations with their own members and shared knowledge hub, and a super administrator who manages the knowledge base and accounts.

> **Setting up the project?** Follow **[SETUP.md](SETUP.md)**: prerequisites, `.env` configuration, Docker, migrations, startup, tests, deployment and troubleshooting.

---

## Contents

- [Key features](#key-features)
- [Technology stack](#technology-stack)
- [Architecture](#architecture)
- [Project structure](#project-structure)
- [How answers are produced](#how-answers-are-produced)
- [LLM providers and fallback](#llm-providers-and-fallback)
- [Embeddings and document ingestion](#embeddings-and-document-ingestion)
- [Database](#database)
- [Docker support](#docker-support)
- [Installation and running locally](#installation-and-running-locally)
- [Configuration](#configuration)
- [API](#api)
- [Testing](#testing)
- [Deployment](#deployment)
- [Security](#security)
- [Known limitations](#known-limitations)
- [Notes for developers and contributors](#notes-for-developers-and-contributors)
- [License](#license)

---

## Key features

**Chat**
- **Two answer modes.** *Sprint* answers directly from the language model, quickly and without document retrieval. *Scholar* runs the full retrieval and verification pipeline. New chats start in Sprint; users switch per message.
- **Grounded, verified answers (Scholar).** Every answer is checked claim by claim against the retrieved evidence, including SAP identifiers such as transaction codes and tables. Unsupported content is repaired or removed. If no verified answer is possible, the assistant says so instead of guessing.
- **SAP-only scope.** Questions outside SAP are politely refused; greetings and small talk are answered briefly.
- **Answer layouts that fit the question:** direct answer, explanation, comparison, step-by-step, troubleshooting, document research, solution design or production incident.
- **Attachments.** PDF, DOCX, TXT and Markdown files and images (OCR) can be attached to a question and are used as evidence.
- **Conversation memory.** The assistant recalls earlier questions in the chat and across the user's chats ("what did I ask first?"), and keeps a rolling summary of each chat.
- **Streaming responses** over Server-Sent Events, with stop, edit-and-regenerate, thumbs up/down feedback, and public read-only share links for a question and its answer.

**Knowledge and administration**
- **Admin control center** (`/admin`): document upload and processing with live progress, a retrieval sandbox with per-chunk scores, system and security status, and individual-user and organisation management. Document access groups, source tiers and the security audit trail are available through the admin API.
- **SAP web fallback.** When internal documents are weak, Tavily search is used, restricted to SAP domains (help.sap.com, learning.sap.com, community.sap.com and others).
- **Optional sources:** GitHub repository code and multi-source comparison, both off by default.

**Organisations**
- Organisation workspaces with request, approval and activation-code onboarding, owner/admin/member roles, and member management (admins set each member's starting password; members choose their own at first sign-in).
- A **shared knowledge hub**: members publish helpful answers (category, likes, comments) for colleagues.

**Accounts and privacy**
- Account data export (JSON) and self-service account deletion.
- Regular users never see internal citations, scores or retrieval details; administrators see the full diagnostics.

---

## Technology stack

| Layer | Technology |
| --- | --- |
| Backend | Python 3.12, FastAPI, Uvicorn, Pydantic v2 / pydantic-settings |
| Database | PostgreSQL 16 + pgvector (Docker image `pgvector/pgvector:pg16`), SQLAlchemy 2, Alembic, psycopg2 |
| Auth and security | PyJWT (HMAC-signed access tokens), bcrypt password hashing, per-IP and per-account rate limiting (optional Redis via `limits`) |
| LLM providers | Groq (`groq` SDK), Google Gemini (`google-generativeai`), OpenRouter (`openai` SDK with the OpenRouter endpoint) |
| Embeddings | `sentence-transformers/all-MiniLM-L6-v2`, 384 dimensions (sentence-transformers / PyTorch; FastEmbed is used instead if installed) |
| Web search | Tavily (`tavily-python`) |
| Document processing | PyMuPDF, pypdf, python-docx, RapidOCR (ONNX Runtime), Pillow |
| Frontend | React 19, TypeScript 6, Vite 8, Tailwind CSS 4, React Router 7, react-markdown + remark-gfm, Framer Motion, Lucide icons |
| Testing and quality | pytest (backend), Vitest (frontend), oxlint, Ruff via pre-commit |
| Containers | Docker Compose (PostgreSQL only) |

---

## Architecture

```text
 Browser
   │  React SPA (Vite dev server :5173 / static build)
   │  JSON + Server-Sent Events, Bearer token
   ▼
 FastAPI backend (:8000)
   ├── security middleware ─ CORS, CSP and security headers, body-size limits,
   │                         rate limits, HTTPS enforcement in production
   ├── API routers ───────── /api/auth  /api/chats  /api/chats/{id}/messages  /api/rag
   │                         /api/documents  /api/admin  /api/rag-inspect  /api/org  /api/shared
   ├── RAG pipeline ──────── routing → retrieval → evidence selection → generation → verification
   │       │                       │                    │                   │
   │       │                       │                    │                   └─► Groq / Gemini / OpenRouter
   │       │                       │                    └─► Tavily (SAP domains only)
   │       │                       └─► in-memory hybrid index (dense + BM25 + identifiers)
   │       └─► local embedding model (all-MiniLM-L6-v2)
   └── SQLAlchemy ────────────► PostgreSQL 16 + pgvector (Docker, 127.0.0.1:5434)
                                 users, chats, messages, documents, chunks + vectors,
                                 organisations, audit events
```

- The backend is stateless apart from the database, an in-memory retrieval index (rebuilt from PostgreSQL) and per-process caches.
- PostgreSQL is the only supported database; there is no fallback store.
- Settings come from one file, the repository-root `.env`, which Docker Compose also reads.

---

## Project structure

```text
SAP_BRIM_Ai/
├── README.md                 # This document
├── SETUP.md                  # Local installation, configuration, troubleshooting
├── DEPLOYMENT.md             # Server deployment with Docker and HTTPS
├── docker.md                 # Database access, backups, SQL examples
├── docker-compose.yml        # Development: PostgreSQL + pgvector only
├── docker-compose.prod.yml   # Production: postgres + backend + web (Caddy, HTTPS)
├── docker/postgres/init/     # First-start script: sap_brim_ai role/database, vector extension
├── .env.example              # Production settings template for docker-compose.prod.yml (copy to .env; local dev: see SETUP.md §4)
├── .python-version           # Python 3.12
├── pytest.ini                # Pytest configuration ("live" marker)
├── .pre-commit-config.yaml   # Ruff checks for backend/
├── AGENTS.md                 # Working rules for contributors and coding agents
├── HUMAN_ACTIONS.md          # Owner decisions and pre-deployment actions
├── AUDIT_FIX_LOG.md          # Security/quality audit remediation status
├── CHANGELOG_AUDIT.md        # API and configuration changes from the audit
│
├── backend/
│   ├── requirements.txt      # Python dependencies
│   ├── Dockerfile            # Production backend image (context: repository root)
│   ├── alembic.ini           # Alembic configuration (URL comes from .env)
│   ├── migrations/           # Alembic environment and revisions 0001–0016
│   ├── app/
│   │   ├── main.py           # FastAPI app, middleware, router mounting, /health
│   │   ├── config.py         # All settings (pydantic-settings) and startup security checks
│   │   ├── database.py       # Engine, sessions, migrations at startup, super-admin seeding
│   │   ├── api/              # Routers: auth, chats, messages, rag, documents, admin,
│   │   │                     #   admin_organisations, organisations, rag_inspect, shared
│   │   ├── models/           # SQLAlchemy models
│   │   ├── schemas/          # Pydantic request/response models
│   │   ├── security/         # JWT auth, access groups, rate limiting, uploads, org access
│   │   ├── services/         # RAG, LLM, retrieval, grounding, embeddings, documents, memory
│   │   ├── retrieval/        # Multi-source comparison, GitHub repository index
│   │   ├── ingestion/        # Knowledge-base ingestion (ingest.py)
│   │   ├── corpus/           # Document tier definitions and identifier index
│   │   └── prompts/          # Prompt templates
│   ├── scripts/              # Admin password reset, attachment retention, ingestion tools
│   ├── evaluation/           # Evaluation datasets and runners
│   └── tests/                # pytest suite
│
├── frontend/
│   ├── package.json          # Scripts: dev, build, test, lint, preview
│   ├── Dockerfile, Caddyfile # Production web image: built app + HTTPS + /api proxy
│   ├── .env.example          # Optional frontend overrides (VITE_API_BASE_URL)
│   ├── vite.config.ts        # Dev server (5173, proxies /api to :8000), build-time Content-Security-Policy
│   └── src/
│       ├── App.tsx           # Routes
│       ├── pages/            # Landing, login, chat, admin, organisation pages
│       ├── components/       # Chat, admin, organisation and shared UI components
│       ├── layouts/          # App shell and route guards
│       ├── context/          # Authentication state
│       └── services/api.ts   # REST and SSE client
│
├── knowledge_base/documents/ # Source documents for ingestion (PDF)
└── docs/
    ├── ORGANISATION_PORTAL.md     # Organisation portal design and API contract
    └── engineering-memory/        # Project context, implementation map, known issues, handover
```

---

## How answers are produced

### Sprint mode

Sprint sends the question to the configured LLM without retrieval or embeddings. Each turn is decided in this order:

1. **Memory questions** ("what was my first question?") are answered word for word from the database. Other recall questions use keyword search over the user's history.
2. **SAP questions**, including follow-ups to earlier SAP questions in the chat, are answered with the Sprint system prompt. The prompt includes the chat summary, the recent history and the current attachments.
3. **Small talk** (greetings, thanks, "who are you?") gets a short reply.
4. Anything else is refused as out of scope.

Sprint answers stream token by token from the provider and are marked *unverified*.

### Scholar mode: the RAG pipeline

Implemented in `backend/app/services/rag_service.py` (`process_query` and `stream_query` share one pipeline):

1. **Routing and memory.** Small talk and memory-lookup questions are handled first. Recall questions search the user's earlier messages; that search combines keywords with stored message embeddings.
2. **Follow-up rewriting.** A short follow-up such as "and for FI-CA?" is rewritten into a standalone question. This uses a small Groq call, with a rule-based fallback.
3. **SAP scope check.** A rule-based classifier (modules, transaction codes, tables, keywords) refuses non-SAP questions. The check is skipped when a document is attached.
4. **Hybrid retrieval.** The question is split into its parts, and each part is searched in an in-memory index of all document chunks with three channels:
   - dense cosine similarity on the embeddings,
   - BM25 keyword scoring,
   - exact SAP identifier matching.

   The channels are combined with reciprocal rank fusion, near-duplicates are removed, and the user's document access groups are applied. A heuristic reranker then boosts heading, phrase and transaction-code matches.
5. **Evidence assessment.** A confidence score combines relevance, term coverage, identifier matches and domain fit.
6. **Web fallback.** If the best document match is below `DOCUMENT_MATCH_THRESHOLD` (0.80) or confidence is low, Tavily searches SAP domains only. Relevant paragraphs are taken from the pages, and every result is labelled by authority (SAP Help, SAP Notes, SAP Learning, community).
7. **Evidence selection.** The strongest set is chosen: knowledge base only, web only, or combined. Attached documents are put first. Each passage gets an evidence ID `[E1]`, `[E2]`, ….
8. **Answer layout.** Rules choose the layout, and a small LLM classifier handles questions the rules cannot place.
9. **Generation.** The selected LLM writes the answer from the numbered evidence. Retrieved text, history and attachments are wrapped as untrusted content.
10. **Verification gate** (`grounding_service.verify_answer`), a deterministic check with no LLM judge. The answer is split into claims, and each claim is checked for:
    - term overlap with its evidence;
    - SAP identifiers that are invented or missing from the evidence;
    - citations that do not support the claim;
    - contradictions and release mismatches (ECC vs S/4HANA).

    The answer passes only if its score is at least `ANSWER_GROUNDING_THRESHOLD` (0.80) and there are no fabricated identifiers or critical failures.
11. **Repair and guard.** A failing answer gets one LLM repair attempt. Any remaining unsupported sentences are removed and listed under "Not confirmed by the available SAP documentation".
12. **Web retry, then a labelled answer.** If the answer still fails and web search has not run yet, the pipeline regenerates once with web evidence. If no candidate passes, the better-supported one is published anyway, never as verified:
    - sentences with contradicted, unsafe or unconfirmed SAP identifiers are removed first;
    - a note says that parts could not be confirmed;
    - up to three SAP web sources are linked.
13. **Final fallbacks.** If none of the three LLM tiers can generate an answer, approved SAP web search is the last resort. The user gets a list of the relevant SAP pages, each a clickable link with the sentences that address the question, marked as not verified. If web search also fails or is disabled, the user gets *"All response-generation methods are temporarily unavailable…"*.

```text
Question → LLM tier 1 (Groq) → tier 2 (Gemini) → tier 3 (OpenRouter) → claim verification
         ↘ all three tiers fail → approved SAP web search → answer with source links
                                ↘ web search fails → "All response-generation methods are temporarily unavailable"
```

The final answer is then streamed to the browser in small chunks, followed by deterministic follow-up questions.

**What users see.** Regular users get the answer text without evidence markers, citations, scores or pipeline details. Administrators also see citations, web sources, grounding scores, the model and provider used, and the verification audit.

---

## LLM providers and fallback

Implemented in `backend/app/services/llm_service.py`.

| Provider | SDK | Key | Default models (`config.py`, overridable) |
| --- | --- | --- | --- |
| Groq | `groq` | `GROQ_API_KEY` | `LLM_GROQ_MODELS` |
| Google Gemini | `google-generativeai` | `GEMINI_API_KEY` | `LLM_GEMINI_MODELS` |
| OpenRouter | `openai` (OpenRouter base URL) | `OPENROUTER_API_KEY` (falls back to `LLM_API_KEY`) | `LLM_OPENROUTER_MODELS` (free-tier models) |

**Fallback order**
- **Providers.** `LLM_PROVIDER` is tried first, then Groq → Gemini → OpenRouter. Only providers listed in `LLM_ALLOWED_PROVIDERS` (default `groq,gemini,openrouter`) are used. An empty list disables all cloud generation, and every answer then reports "unavailable". `LLM_PROVIDER=local` means "no preferred provider".
- **Models.** Within a provider, the models are tried in list order, with `LLM_MODEL` first for the preferred provider. A provider without an API key is skipped. `LLM_ALLOW_FREE_TIER=False` removes `:free` models.
- **Cooldowns.** A model that returns 404/decommissioned is skipped for 1 hour. A rejected API key (401, or an invalid-key/permission error) skips every model of that provider for 1 hour; a request-specific 403 such as a moderation flag does not. A rate limit (429) is skipped for the provider's `retry-after` (else 30 s), and a timeout for 60 s. Cooldowns are per process.
- **Logging.** The backend log names the tier, provider and model that produced each answer ("Answer generated by LLM tier 2 (gemini, model …)"). It also records each tier that failed, with a category such as `auth_error`, `rate_limited` or `timeout`. Exception text and keys are not logged.
- **Limits.** Groq output size is budgeted from `GROQ_TOKENS_PER_MINUTE`. Gemini and OpenRouter answers cut off by length get one continuation call. Calls time out after 45 s (60 s when streaming).

**Data protection and audit**
- **Redaction.** E-mail addresses, IBANs, phone numbers and 10-digit account numbers are redacted from prompts before they leave the server.
- **Audit.** Each external call is recorded in `audit_events` with provider, model and token estimates; the prompt text itself is not stored.
- **Images.** Chat images go to external vision models only when `VISION_EXTERNAL_ENABLED=True` (default off); otherwise local OCR is used.

---

## Embeddings and document ingestion

**Embeddings**
- **Model.** `all-MiniLM-L6-v2` (384 dimensions, L2-normalised), computed locally. It is downloaded from Hugging Face on first use.
- **Provenance.** Every chunk and message vector records its model and version. Vectors from a different model are ignored by dense search until the documents are re-ingested; keyword and identifier search still cover them.

**Getting documents into the knowledge base**
- **Command line:** `python backend/app/ingestion/ingest.py` indexes `.pdf`, `.txt` and `.md` files under `knowledge_base/documents/`, skipping unchanged documents.
- **Admin portal:** upload in **Document Manager & Ingestion**, then process. The same pipeline is exposed as `POST /api/documents/upload` and `POST /api/documents/process-all`.
- **Processing.** Text is extracted with PyMuPDF (pypdf fallback), python-docx or OCR. It is chunked per page (about 1,200 characters with 200 overlap), embedded in batches and stored. Limits: 50 MB per file, 100 MB per upload request, 500 PDF pages.

**Source tiers and access groups**
- Each document has an authority tier: `official_sap`, `sap_partner_or_book`, `official_sap_domain`, `forum`, `self_authored`, `github_repo` or `unknown`. The tier weights evidence ranking and confidence.
- New documents default to `self_authored`, which retrieval skips unless `INCLUDE_SELF_AUTHORED_DOCS=True`. Set tiers with `PATCH /api/documents/{id}/tier` (see [SETUP.md §9.3](SETUP.md#93-make-documents-retrievable-source-tiers)).
- Each document also has access groups (default `all`). A regular user only retrieves from documents that share one of the user's groups; administrators retrieve from everything.

---

## Database

PostgreSQL 16 with the pgvector extension, managed by Alembic.

| Table | Purpose |
| --- | --- |
| `users` | Accounts: admin flag, role, access groups, token version (revocation), account type (individual/organisation), organisation role, active and forced-password-change flags |
| `chats` | Conversations with a rolling summary |
| `messages` | Questions and answers, source type, grounding score, feedback, optional `vector(384)` embedding with model/version |
| `citations`, `web_sources`, `web_images` | Evidence and web results attached to an answer |
| `uploaded_attachments` | Extracted text of chat attachments (purged after `ATTACHMENT_RETENTION_DAYS`) |
| `documents` | Knowledge-base files: hash, processing status, source tier, access groups |
| `document_chunks` | Chunk text, page, section, metadata, `vector(384)` embedding with model/version |
| `shared_prompts` | Public share links (revocable) |
| `audit_events` | Security and LLM-egress audit trail |
| `organizations`, `org_activation_codes`, `org_shared_answers`, `org_answer_likes`, `org_answer_comments` | Organisation portal |
| `github_repo_chunks`, `web_doc_chunks` | Optional GitHub and SAP web-document indexes |

**Migrations** (`backend/migrations/versions`):

| Revision | Change |
| --- | --- |
| `0001_baseline` | Baseline schema, `vector` extension, HNSW and full-text indexes |
| `0002_access_groups` | User and document access groups |
| `0003_audit_events` | Audit trail |
| `0004_document_source_tier` | Document authority tiers |
| `0005_embedding_provenance` | Embedding model/version on chunks and messages |
| `0006_organisations` | Organisation portal tables and user columns |
| `0007_message_llm_tracking` | Provider/model/regeneration columns on messages |
| `0008_message_token_usage` | Per-answer token usage and baseline |
| `0009_projects` | Server-side projects and `chats.project_id` |
| `0010_refresh_tokens` | Rotating refresh-token families |
| `0011_token_wallet` | Free token wallet (`users.token_balance_milli`) |
| `0012_token_refill` | Wallet refill date (`users.token_refill_at`) |
| `0013_user_memories` | `user_memories` table (things a user asked the assistant to remember) |
| `0014_user_language` | Answer language (`users.preferred_language`) |
| `0015_question_language` | Question language (`users.question_language`) |
| `0016_auth_security_controls` | Persisted registration switch and login-lockout/unlock-key columns |
| `0017_token_allowance_100` | Free token allowance 1,000 → 100: new column default, balances above 100 capped |

- **Downgrades.** Revisions `0016` down to `0002` downgrade one step at a time (verified on a scratch database; `0016` drops only the registration switch and lockout state, not user rows). `0001_baseline` refuses to downgrade by design: restore from a backup instead.
- **Startup.** With `AUTO_MIGRATE=True` (default) the backend migrates at startup. With `False` it refuses to start on an outdated schema; run `python -m alembic -c backend/alembic.ini upgrade head` instead. Migrations take an advisory lock, so only one worker migrates at a time.
- **Indexes vs search.** Vectors are stored in pgvector columns with HNSW indexes. Similarity search itself runs in the backend's in-memory index, which is built from these rows at startup and after document changes.

---

## Docker support

There are two Compose files:

| File | Purpose | Runs |
| --- | --- | --- |
| `docker-compose.yml` | local development | PostgreSQL only; backend and frontend run on the host |
| `docker-compose.prod.yml` | server deployment ([DEPLOYMENT.md](DEPLOYMENT.md)) | `postgres`, `backend` (`backend/Dockerfile`), `web` (`frontend/Dockerfile`: Caddy with the React app, automatic HTTPS, `/api` proxy) |

For development, `docker-compose.yml` runs **PostgreSQL 16 + pgvector only**:

| | |
| --- | --- |
| Service / container | `postgres` / `sap_assistant_postgres` |
| Image | `pgvector/pgvector:pg16` |
| Port | `127.0.0.1:${POSTGRES_HOST_PORT:-5434}` → `5432` (localhost only) |
| Volume | `sap_brim_pgdata` |
| First start | `docker/postgres/init/10-create-sap-brim-ai.sh` creates the `sap_brim_ai` role/database and the `vector` extension |

```sh
docker compose up -d postgres     # start
docker compose ps                 # status and health
docker compose stop postgres      # stop, data is kept
```

In development the backend and frontend run on the host. Never use `docker compose down -v`: it deletes the database volume. Backups and database access are described in [docker.md](docker.md).

---

## Installation and running locally

Requirements: Git, Docker, Python 3.12, Node.js 22.12+, and at least one LLM API key (Groq, Gemini or OpenRouter). The full walkthrough is in **[SETUP.md](SETUP.md)**; the short version, from the repository root:

```sh
# 1. Settings: create .env from the development example in SETUP.md section 4.4
#    (.env.example is the production template, not the local-dev one)

# 2. Database
docker compose up -d postgres

# 3. Backend (Python 3.12 virtual environment)
python3.12 -m venv .venv                  # Windows: py -3.12 -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\Activate.ps1
python -m pip install -r backend/requirements.txt
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --reload

# 4. Knowledge base (second terminal, venv active; first time only)
python backend/app/ingestion/ingest.py

# 5. Frontend (third terminal)
cd frontend
npm ci
npm run dev
```

| URL | What |
| --- | --- |
| `http://localhost:5173` | Web application |
| `http://localhost:5173/admin/login` | Super admin sign-in |
| `http://127.0.0.1:8000/health` | Backend health check |
| `http://127.0.0.1:8000/docs` | Interactive API docs (not in production) |

---

## Configuration

All settings live in the repository-root **`.env`** (template: [`.env.example`](.env.example)). Defaults are defined in `backend/app/config.py`; real environment variables override `.env`.

| Group | Settings | Notes |
| --- | --- | --- |
| Database (Compose) | `POSTGRES_HOST_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `SAP_BRIM_DB_PASSWORD` | Passwords required; applied when the volume is first created |
| Database (backend) | `DATABASE_URL`, `DATABASE_POOL_SIZE`, `DATABASE_MAX_OVERFLOW`, `DATABASE_CONNECT_TIMEOUT`, `AUTO_MIGRATE` | `DATABASE_URL` required; password and port must match the Compose values |
| Security | `JWT_SECRET`, `JWT_ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `ENVIRONMENT`, `ALLOWED_ORIGINS`, `TRUSTED_PROXIES`, `ADMIN_API_KEY` | `JWT_SECRET` required (32+ characters); HS256/384/512 only |
| Accounts | `SUPER_ADMIN_EMAIL`, `SUPER_ADMIN_PASSWORD`, `REGISTRATION_ENABLED`, `DEFAULT_ACCESS_GROUP` | Individuals sign up directly (no code, 8+ character password); `REGISTRATION_ENABLED=False` closes sign-up. Organisations use activation codes |
| Rate limits | `RATE_LIMIT_ENABLED`, `REDIS_URL`, `AUTH_RATE_LIMIT_PER_MINUTE`, `LOGIN_RATE_LIMIT_PER_MINUTE`, `LOGIN_MAX_FAILURES`, `ANSWER_RATE_LIMIT_PER_MINUTE`, … | Per process unless `REDIS_URL` is set |
| LLM | `LLM_PROVIDER`, `LLM_MODEL`, `LLM_ALLOWED_PROVIDERS`, `LLM_ALLOW_FREE_TIER`, `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `LLM_*_MODELS`, `GROQ_TOKENS_PER_MINUTE`, `ANSWER_TEMPERATURE` | See [LLM providers](#llm-providers-and-fallback) |
| Retrieval | `EMBEDDING_MODEL`, `TOP_K`, `RERANK_TOP_K`, `RETRIEVAL_CACHE_WARMUP`, `INCLUDE_SELF_AUTHORED_DOCS`, `INTERNAL_EVIDENCE_THRESHOLD`, `DOCUMENT_MATCH_THRESHOLD` | Do not change `EMBEDDING_MODEL` after indexing |
| Verification | `ANSWER_GROUNDING_THRESHOLD`, `MAX_GROUNDING_REPAIR_ATTEMPTS`, `TECHNICAL_CLAIM_VALIDATION_ENABLED`, `SIMPLE_SAP_FAST_PATH_ENABLED` | Fast path (unverified answers) is off by default |
| Web search | `WEB_FALLBACK_ENABLED`, `WEB_SEARCH_API_KEY`, `AUTO_MULTI_SOURCE_FALLBACK`, `TAVILY_MAX_RESULTS`, `TAVILY_MAX_IMAGES`, `ALLOWED_LINK_HOSTS` | Tavily key optional |
| Optional sources | `REPO_SOURCE_ENABLED`, `MULTI_SOURCE_COMPARE_ENABLED`, `SAP_WEB_SOURCES_ENABLED` | All off by default |
| Answer layout | `ANSWER_STYLE_ROUTER_ENABLED`, `ANSWER_STYLE_LLM_CLASSIFIER_ENABLED`, `ANSWER_STYLE_CLASSIFIER_MODEL`, … | |
| Uploads | `MAX_UPLOAD_BYTES`, `MAX_TOTAL_UPLOAD_BYTES`, `MAX_PDF_PAGES`, `ALLOW_EXTENDED_UPLOAD_TYPES`, `MAX_REQUEST_BODY_BYTES`, `ATTACHMENT_RETENTION_DAYS` | |
| Vision | `VISION_ENABLED`, `VISION_EXTERNAL_ENABLED`, `VISION_*_MODELS` | External vision off by default |
| Organisations | `ORG_ACTIVATION_CODE_TTL_HOURS`, `ORG_*_RATE_LIMIT_*` | |

The backend refuses to start with an unsafe configuration: a short or published `JWT_SECRET`/`ADMIN_API_KEY`, or an incomplete or weak super-admin setting. In production it also refuses wildcard or localhost CORS origins.

---

## API

Base URL: `http://127.0.0.1:8000`. Every application route is under `/api`. Interactive documentation is at `/docs` (Swagger), `/redoc` and `/openapi.json`; all three are disabled when `ENVIRONMENT=production`.

### Authentication

Sign in to obtain an access token and send it as `Authorization: Bearer <token>`. Tokens expire after `ACCESS_TOKEN_EXPIRE_MINUTES` (15). There is no refresh endpoint; sign in again. Signing out revokes **all** tokens of that account.

```sh
# Sign in (JSON body)
curl -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com", "password": "your-password"}'
# → {"access_token": "...", "token_type": "bearer", "user": {...}}

# Create a chat
curl -X POST http://127.0.0.1:8000/api/chats \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"title": "FI-CA questions"}'

# Ask a question (mode: "scholar" = verified RAG, "sprint" = direct)
curl -X POST http://127.0.0.1:8000/api/chats/$CHAT_ID/messages \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"content": "How does Convergent Invoicing create invoicing documents?", "mode": "scholar"}'

# Same request as a Server-Sent Events stream
curl -N -X POST http://127.0.0.1:8000/api/chats/$CHAT_ID/messages/stream \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"content": "Explain contract accounts in FI-CA", "mode": "sprint"}'
```

Stream events are `data: {json}` frames of type `stream`, `user_message`, `status`, `token`, `done` and `error`.

### Endpoints

Access: **Public**; **User**, meaning any signed-in active account; **Admin**, the super admin; **Admin/Key**, an admin token or the `X-Admin-Key` header; **Org**, an organisation member; **Org admin**, an organisation owner or admin.

| Area | Method and path | Access | Purpose |
| --- | --- | --- | --- |
| Service | `GET /` · `GET /health` | Public | Service info · liveness and database check |
| Auth | `POST /api/auth/register` | Public | Individual sign-up, no code needed (closed if `REGISTRATION_ENABLED=False`). Returns the new account (201) without a session; the user then signs in with `/api/auth/login` |
| | `POST /api/auth/login` · `POST /api/auth/admin-login` | Public | Sign in as an individual user · as super admin |
| | `GET /api/auth/me` · `POST /api/auth/change-password` · `POST /api/auth/logout` | User | Profile · change password · revoke all sessions |
| | `GET /api/auth/me/export` · `DELETE /api/auth/me` | User | Export account data · delete account |
| Chats | `GET/POST /api/chats` · `GET/PATCH/DELETE /api/chats/{chat_id}` | User | List/create · read/rename/delete own chats |
| | `POST /api/chats/extract-document` | User | Extract text from an attachment (PDF, DOCX, TXT, MD, images; 10 MB) |
| Messages | `GET /api/chats/{chat_id}/messages` | User | Message history |
| | `POST /api/chats/{chat_id}/messages` (`?stream=true` for SSE) · `POST …/messages/stream` | User | Ask a question |
| | `POST …/messages/{message_id}/edit` | User | Edit a question and regenerate (SSE) |
| | `POST` / `DELETE …/messages/{message_id}/share` | User | Create/revoke a public share link |
| | `PUT …/messages/{message_id}/feedback` · `POST …/messages/save-partial` | User | Thumbs up/down · keep a stopped answer |
| RAG | `POST /api/rag/query` · `POST /api/rag/query/stream` | User | Single question without the chat UI |
| Sharing | `GET /api/shared/prompts/{token}` | Public | Read a shared question and answer |
| Documents | `GET /api/documents` · `GET /api/documents/{id}` | Admin/Key | List · details with sample chunks |
| | `POST /api/documents/upload` · `POST /api/documents/process-all` · `POST /api/documents/{id}/process` | Admin/Key | Upload · process all pending · process one |
| | `GET /api/documents/processing-status` · `GET /api/documents/processing-stream` | Admin/Key | Progress snapshot · progress stream (SSE) |
| | `PATCH /api/documents/{id}/access` · `PATCH /api/documents/{id}/tier` · `DELETE /api/documents/{id}` | Admin/Key | Access groups · source tier · delete |
| Admin | `POST /api/admin/documents/ingest` | Admin/Key | Index the whole `knowledge_base/documents` folder |
| | `GET /api/admin/users` · `PATCH /api/admin/users/{id}/groups` · `PATCH /api/admin/users/{id}/status` | Admin | Users · access groups · activate/deactivate |
| | `GET /api/admin/individual-users` · `GET /api/admin/audit-events` · `GET /api/admin/system-health` | Admin | Usage · audit trail · corpus and model status |
| | `GET/POST /api/admin/organisations` · `GET /api/admin/organisations/{id}` | Admin | List/create · details |
| | `POST …/organisations/{id}/approve` · `…/reject` · `…/activation-codes` · `PATCH …/status` | Admin | Onboarding and lifecycle |
| Inspection | `GET /api/rag-inspect/system-config` · `GET /api/rag-inspect/documents` · `POST /api/rag-inspect/similarity-search` | Admin | Retrieval settings · index coverage · retrieval sandbox |
| Organisation (public) | `POST /api/org/requests` · `POST /api/org/activation/verify` · `POST /api/org/activation/complete` · `POST /api/org/auth/login` | Public | Request a workspace · activate · sign in |
| Organisation | `GET /api/org/me` · `GET /api/org/knowledge/categories` · `GET/POST /api/org/knowledge` · `GET /api/org/knowledge/shared-message-ids` | Org | Workspace info · knowledge hub |
| | `GET/PATCH/DELETE /api/org/knowledge/{id}` · `POST/DELETE …/{id}/like` · `POST …/{id}/comments` · `DELETE …/comments/{comment_id}` | Org | Shared answers, likes, comments |
| | `GET/POST /api/org/admin/members` · `PATCH /api/org/admin/members/{id}` · `POST …/members/{id}/reset-password` | Org admin | Member management |
| | `GET /api/org/admin/activity` · `PATCH /api/org/admin/organisation` | Org admin | Usage · organisation details |

Rate limits apply per user (or per IP for anonymous calls): 20/min for authentication endpoints, 12/min for answer endpoints, 10/min for document extraction. Exceeding them returns `429`.

---

## Testing

```sh
# Backend: offline suite (tests marked "live" call real LLM/web APIs)
python -m pytest backend/tests -m "not live"

# Frontend
cd frontend
npm test           # Vitest
npm run lint       # oxlint
npm run build      # type check + production build
```

Backend tests run against a separate database whose name must end in `_test` (`sap_brim_ai_test` by default); `backend/tests/conftest.py` enforces this. Creating it is described in [SETUP.md §13](SETUP.md#13-run-the-tests).

---

## Deployment

**Step-by-step guide: [DEPLOYMENT.md](DEPLOYMENT.md).** On a Linux server with Docker and a domain name:

```sh
git clone https://github.com/vamshiyadav-06/SAP_chatbot.git && cd SAP_chatbot
cp .env.example .env      # set the domain, passwords, JWT_SECRET, admin account and API keys
docker compose up -d --build         # .env selects docker-compose.prod.yml
```

| Piece | How it is deployed |
| --- | --- |
| Web | `frontend/Dockerfile`: builds the React app and serves it with Caddy, which obtains the HTTPS certificate for `APP_DOMAIN` and forwards `/api` and `/health` to the backend. The browser calls the same-origin `/api`, so no API address is configured. |
| Backend | `backend/Dockerfile`: Python 3.12, CPU-only PyTorch, embedding and OCR models baked in, non-root user. Runs `alembic upgrade head`, then one uvicorn worker. Not published; trusts Caddy's forwarded headers on the private Docker network. |
| Database | `pgvector/pgvector:pg16` with the `pgdata` volume. Not published. |

`ENVIRONMENT=production` disables the API docs, requires HTTPS (except `/health`), adds HSTS, and refuses to start with an empty or short `JWT_SECRET`, a `change-me` database or admin password, or localhost/wildcard `ALLOWED_ORIGINS`. Templates: [`.env.example`](.env.example) (server), [`frontend/.env.example`](frontend/.env.example) (optional frontend overrides). Hosting without Docker is described in [SETUP.md §15](SETUP.md#15-deploying-to-a-server).

Owner actions to complete before going live, such as secret rotation, knowledge-base licensing and provider approval, are listed in [HUMAN_ACTIONS.md](HUMAN_ACTIONS.md).

---

## Security

- **Startup checks.** The backend refuses unsafe secrets and, in production, unsafe CORS settings.
- **Tokens.** Access tokens are short-lived (15 min) JWTs with issuer, audience and token-version claims. Sign-out, password changes and deactivation revoke existing tokens.
- **Login protection.** Constant-time comparisons, per-IP rate limits and per-account lockout.
- **Isolation.** Users only reach their own chats. Organisations only reach their own workspace. Document access groups limit retrieval.
- **Response headers.** Strict Content-Security-Policy and security headers on API responses; a build-time CSP for the SPA.
- **Requests and uploads.** Request bodies, uploads and PDFs are size-limited, and upload signatures are checked.
- **Prompt safety.** Retrieved content, history and attachments are treated as untrusted. Personal data is redacted from LLM prompts, and every external LLM call is audited.
- **Error responses.** They do not expose stack traces, document-processing internals or retrieval metadata to regular users.

Report security issues privately to the repository owners rather than in public issues.

---

## Known limitations

- **No CI pipeline.** Tests and image builds are run by hand.
- **One backend process.** The production image runs one worker; rate limits, login lockout and Stop for answers that keep generating are per process (see [DEPLOYMENT.md](DEPLOYMENT.md#capacity)).
- **Fresh installs need tiers.** New documents are `self_authored` and excluded from retrieval until their tier is set or `INCLUDE_SELF_AUTHORED_DOCS=True`. The admin UI has no tier control yet.
- **In-memory retrieval.** Similarity search runs over an in-memory index of all chunks, so memory use grows with the corpus.
- **Scholar streaming.** Scholar answers are generated in full and verified before streaming, so the first token arrives later than in Sprint.
- **Per-process state.** Rate limits and model cooldowns are per process unless `REDIS_URL` is set (cooldowns are always per process).
- **Sign-in.** There are no refresh tokens or multi-factor authentication; users sign in again after 15 minutes.
- **Knowledge-base licensing.** Some bundled PDFs need a licensing decision; see [HUMAN_ACTIONS.md](HUMAN_ACTIONS.md).

---

## Notes for developers and contributors

- **Read first:** [AGENTS.md](AGENTS.md), then `docs/engineering-memory/NEXT_SESSION.md`, `PROJECT_CONTEXT.md` and `IMPLEMENTATION_MAP.md`. Run `git status` before editing; others' uncommitted work may be present.
- **Never commit** `.env`, API keys, database dumps, virtual environments or build output. `.gitignore` covers these; check `git status` before every commit.
- **Keep the architecture:** FastAPI routers → services → SQLAlchemy models; React pages → `services/api.ts`. Avoid unrelated refactors.
- **Schema changes** need a new Alembic revision in `backend/migrations/versions`. Never edit an applied revision.
- **New settings** go in `backend/app/config.py` with a safe default. Add them to `.env.example` when users are expected to change them; `backend/tests/test_single_env_file.py` checks that every template key is really used.
- **Every behaviour change** needs focused regression tests. Run the relevant backend tests and `npm test` / `npm run build` before pushing.
- **Linting:** `pip install pre-commit && pre-commit install` enables the Ruff check for `backend/`.
- **Do not run destructive commands** such as `docker compose down -v`, dropping databases or deleting volumes without a verified backup.
- **Record your work** in `docs/engineering-memory/` (changelog, known issues, next-session handover) after meaningful changes.

---

## License

Proprietary and confidential. All rights reserved by **Clyptus-Soft**. Bundled third-party material keeps its own terms: the SAP "72" fonts in `frontend/src/assets/fonts/` are under the Apache License 2.0 (see the `LICENSE.txt` there). Licensing of the knowledge-base documents is tracked in [HUMAN_ACTIONS.md](HUMAN_ACTIONS.md).
