# Setup Guide

This guide takes a fresh machine to a running copy of the SAP BRIM AI assistant (Clyptus.AI): PostgreSQL in Docker, the FastAPI backend and the React frontend. It then covers tests, deployment and troubleshooting. Commands are given for **Windows PowerShell** and **macOS/Linux (bash/zsh)** where they differ. Run every command from the **repository root** unless a step says otherwise.

For what the application does and how it works, see [README.md](README.md). For database access, backups and SQL examples, see [docker.md](docker.md).

## Contents

1. [What runs where](#1-what-runs-where)
2. [Prerequisites](#2-prerequisites)
3. [Get the code](#3-get-the-code)
4. [Configure the .env file](#4-configure-the-env-file)
5. [Start PostgreSQL with Docker Compose](#5-start-postgresql-with-docker-compose)
6. [Install the backend](#6-install-the-backend)
7. [Database migrations](#7-database-migrations)
8. [Start the backend](#8-start-the-backend)
9. [Load the knowledge base](#9-load-the-knowledge-base)
10. [Start the frontend](#10-start-the-frontend)
11. [Sign in and create users](#11-sign-in-and-create-users)
12. [Everyday start and stop](#12-everyday-start-and-stop)
13. [Run the tests](#13-run-the-tests)
14. [Docker reference](#14-docker-reference)
15. [Deploying to a server](#15-deploying-to-a-server)
16. [Troubleshooting](#16-troubleshooting)

---

## 1. What runs where

| Component | Runs in | Address |
| --- | --- | --- |
| PostgreSQL 16 + pgvector | Docker Compose (`pgvector/pgvector:pg16`) | `127.0.0.1:5434` (host) → `5432` (container) |
| FastAPI backend | Python 3.12 on the host | `http://127.0.0.1:8000` |
| React + Vite frontend | Node.js on the host | `http://localhost:5173` |
| LLM providers (Groq, Google Gemini, OpenRouter) | Hosted APIs | needs at least one API key |
| Tavily web search | Hosted API | optional |

For development, Docker Compose runs **only the database**; the backend and frontend run directly on your machine. To deploy everything in containers on a server (with HTTPS), use **[DEPLOYMENT.md](DEPLOYMENT.md)** instead of this guide.

There is exactly **one settings file**: `.env` in the repository root. Docker Compose reads it for `docker-compose.yml`, and the backend reads it through `backend/app/config.py`. The production template is `.env.example`; for local development use the example in section 4.4.

## 2. Prerequisites

| Software | Version | Notes |
| --- | --- | --- |
| Git | any recent | |
| Docker | Docker Desktop (Windows/macOS) or Docker Engine + Compose v2 plugin (Linux) | `docker compose` (with a space) must work |
| Python | **3.12** | pinned in `.python-version`; other versions are not supported |
| Node.js | **22.12 or newer** (with npm) | Vite needs 20.19+ or 22.12+; the Vitest test runner needs 22.12+ |
| API key | Groq, Gemini or OpenRouter | at least one, for generating answers |
| API key (optional) | Tavily | SAP web search fallback |

Disk and network: the repository includes about 490 MB of knowledge-base PDFs. The Python dependencies include PyTorch, and the first backend start downloads the `all-MiniLM-L6-v2` embedding model from Hugging Face.

Check your tools:

```sh
git --version
docker --version
docker compose version
node --version
npm --version
```

Python 3.12:

```powershell
# Windows
py -3.12 --version
```

```sh
# macOS / Linux
python3.12 --version
```

> **Windows:** if `py -3.12` reports that no runtime is installed although you installed Python 3.12, call it by its full path in step 6, for example `& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"`.

## 3. Get the code

The repository is private; you need access to `Clyptus-Soft/SAP_BRIM_Ai` on GitHub.

```sh
git clone https://github.com/Clyptus-Soft/SAP_BRIM_Ai.git
cd SAP_BRIM_Ai
```

## 4. Configure the .env file

### 4.1 Create it from the template

`.env.example` is the **production** template (for [DEPLOYMENT.md](DEPLOYMENT.md)). For local development create `.env` yourself from the example in [4.4](#44-example-env-placeholders): it sets the development values (`ENVIRONMENT=development`, a `127.0.0.1` `DATABASE_URL`, `POSTGRES_HOST_PORT`, localhost `ALLOWED_ORIGINS`). Use `.env.example` as the reference for every other setting.

```powershell
# Windows
if (-not (Test-Path .env)) { New-Item .env -ItemType File }
notepad .env
```

```sh
# macOS / Linux
touch .env && ${EDITOR:-nano} .env
```

`.env` is ignored by Git. Never commit it, and never paste its contents into issues, chats or logs.

### 4.2 Generate secrets

Run this once **per secret**; each run prints a different random value:

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

These values contain only letters, digits, `-` and `_`, so they can be pasted into `DATABASE_URL` without escaping. If you choose a password with characters such as `@ : / # %`, URL-encode it inside `DATABASE_URL`.

### 4.3 Values you must change

| Variable | What to set | Why |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | a generated secret | PostgreSQL superuser; Compose refuses to start without it |
| `SAP_BRIM_DB_PASSWORD` | a different generated secret | password of the application role `sap_brim_ai`; Compose refuses to start without it |
| `DATABASE_URL` | `postgresql://sap_brim_ai:<SAP_BRIM_DB_PASSWORD>@127.0.0.1:5434/sap_brim_ai` | must contain the **same** password as `SAP_BRIM_DB_PASSWORD` and the same port as `POSTGRES_HOST_PORT` |
| `JWT_SECRET` | a third generated secret (32+ characters) | signs login tokens; the backend refuses to start if it is shorter or a known published value |
| `SUPER_ADMIN_EMAIL`, `SUPER_ADMIN_PASSWORD` | your admin e-mail and a password of 12–72 characters | creates the first administrator on first start; set both or neither |
| `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY` | at least one real key | providers listed in `LLM_ALLOWED_PROVIDERS` are tried in order; a provider without a key is skipped |
| `WEB_SEARCH_API_KEY` | Tavily key (optional) | enables SAP web search when the knowledge base is weak; without it, web search returns nothing |

Optional:

| Variable | Default | When to change |
| --- | --- | --- |
| `ADMIN_API_KEY` | empty (disabled) | enables the `X-Admin-Key` header for scripted ingestion; 32+ random characters |
| `LLM_PROVIDER` / `LLM_MODEL` | `groq` / template value | preferred provider and the model tried first for it |
| `LLM_ALLOWED_PROVIDERS` | `groq,gemini,openrouter` | which providers may receive prompts; empty disables cloud generation |
| `REGISTRATION_ENABLED` | `True` | `False` closes individual sign-up (see [section 11](#11-sign-in-and-create-users)) |
| `INCLUDE_SELF_AUTHORED_DOCS` | `False` | see [section 9.3](#93-make-documents-retrievable-source-tiers) |
| `POSTGRES_HOST_PORT` | `5434` | if port 5434 is taken; change `DATABASE_URL` to match |

Leave everything else at the template values unless you know why you are changing it. Do **not** change `EMBEDDING_MODEL` after documents are indexed: existing vectors record the model that made them and become unusable until the knowledge base is re-ingested.

### 4.4 Example .env (placeholders)

A minimal working file looks like this. Replace every `replace-with-...` and `your-...` value:

```dotenv
# Docker Compose: PostgreSQL + pgvector
POSTGRES_HOST_PORT=5434
POSTGRES_USER=sap_assistant
POSTGRES_PASSWORD=replace-with-generated-secret-1
POSTGRES_DB=sap_assistant
SAP_BRIM_DB_PASSWORD=replace-with-generated-secret-2

# Backend
DATABASE_URL=postgresql://sap_brim_ai:replace-with-generated-secret-2@127.0.0.1:5434/sap_brim_ai
JWT_SECRET=replace-with-generated-secret-3
ENVIRONMENT=development
ALLOWED_ORIGINS=http://localhost:5173,http://127.0.0.1:5173

SUPER_ADMIN_EMAIL=admin@your-company.com
SUPER_ADMIN_PASSWORD=replace-with-a-strong-password

LLM_PROVIDER=groq
LLM_ALLOWED_PROVIDERS=groq,gemini,openrouter
GROQ_API_KEY=your-groq-api-key
GEMINI_API_KEY=
OPENROUTER_API_KEY=

WEB_FALLBACK_ENABLED=True
WEB_SEARCH_API_KEY=your-tavily-api-key
```

Every setting not listed falls back to its default in `backend/app/config.py`. `.env.example` documents the commonly changed ones (it is the production template).

## 5. Start PostgreSQL with Docker Compose

Start Docker Desktop (Windows/macOS) or the Docker service (Linux), then:

```sh
docker compose up -d postgres
docker compose ps
```

Wait until the status shows `healthy`, then confirm the server accepts connections:

```sh
docker exec sap_assistant_postgres pg_isready -U sap_assistant -d sap_assistant
```

What happens on the **first** start (an empty data volume):

- PostgreSQL creates the superuser from `POSTGRES_USER` / `POSTGRES_PASSWORD`.
- `docker/postgres/init/10-create-sap-brim-ai.sh` creates the application role and database `sap_brim_ai` (password from `SAP_BRIM_DB_PASSWORD`) and enables the `vector` extension.
- Data is stored in the named volume `sap_brim_pgdata`.

These scripts run **only** on an empty volume. Changing passwords in `.env` later does not change passwords already stored in PostgreSQL. If you change them, update them inside PostgreSQL as well, or keep the original values.

> **Never run `docker compose down -v`.** It deletes the volume and every database in it.

## 6. Install the backend

Create a virtual environment in the repository root and install the dependencies:

```powershell
# Windows PowerShell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r backend/requirements.txt
```

```sh
# macOS / Linux
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r backend/requirements.txt
```

- If PowerShell refuses to run `Activate.ps1`, allow local scripts once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
- On Linux, `pip` installs the CUDA build of PyTorch by default (several GB). For a CPU-only machine, install the CPU build first: `python -m pip install torch --index-url https://download.pytorch.org/whl/cpu`, then run the requirements install.
- Check the result with `python -m pip check` ("No broken requirements found").

Keep the virtual environment activated for every backend command below.

## 7. Database migrations

The schema is managed by **Alembic** (`backend/migrations/versions`, revisions `0001_baseline` to `0017_token_allowance_100`). Back up the database before upgrading an existing one; `0001_baseline` cannot be downgraded.

- **Development (default):** with `AUTO_MIGRATE=True` the backend applies pending migrations when it starts. Nothing to do.
- **Manual / production:** set `AUTO_MIGRATE=False` and run the migrations yourself before starting the backend:

```sh
python -m alembic -c backend/alembic.ini upgrade head
python -m alembic -c backend/alembic.ini current
```

`current` should print `0017_token_allowance_100 (head)`. With `AUTO_MIGRATE=False`, the backend refuses to start against an out-of-date schema and prints the command to run. Alembic reads `DATABASE_URL` from `.env`; `backend/alembic.ini` contains no credentials.

## 8. Start the backend

```sh
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --reload
```

On startup the backend:

1. refuses to start if `JWT_SECRET`, `ADMIN_API_KEY` or the admin password are unsafe (with `ENVIRONMENT=production` also a `change-me` database or admin password);
2. connects to PostgreSQL (there is no fallback database);
3. applies migrations (`AUTO_MIGRATE=True`);
4. creates the super admin from `SUPER_ADMIN_EMAIL` / `SUPER_ADMIN_PASSWORD` if that e-mail does not exist yet (an existing account is never changed or promoted);
5. builds the in-memory retrieval index in a background thread.

The first start also downloads the embedding model. Once it is cached, setting the environment variable `HF_HUB_OFFLINE=1` skips the Hugging Face online check and saves a few seconds per start.

Check it (in a second terminal):

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

```sh
curl http://127.0.0.1:8000/health
```

Expected: `{"status":"healthy","database":{"status":"connected"}}`. The interactive API documentation is at `http://127.0.0.1:8000/docs` (disabled when `ENVIRONMENT=production`).

## 9. Load the knowledge base

A new database has no indexed documents. The source PDFs are in `knowledge_base/documents/`. If your team gave you a database backup, restore it instead (see the backup and restore sections of [docker.md](docker.md)) and skip to step 10.

### 9.1 Option A: command line (indexes the whole folder)

With the backend virtual environment active and PostgreSQL running:

```sh
python backend/app/ingestion/ingest.py
```

It extracts, chunks and embeds every `.pdf`, `.txt`, `.md` and `.markdown` file under `knowledge_base/documents/`. Documents whose content and embedding model are unchanged are skipped, so re-running is safe. Indexing all bundled PDFs takes a while on a CPU.

### 9.2 Option B: the admin portal

Sign in at `http://localhost:5173/admin/login` and open **Document Manager & Ingestion**. Upload files (they are saved to `knowledge_base/documents/` with status *pending*), then click **Process All Documents**. Files already in the folder are picked up too. Processing runs in the background with live progress.

Admin uploads accept `.pdf .docx .png .jpg .jpeg .webp .bmp .tiff .txt .md .csv .log .tsv` (more with `ALLOW_EXTENDED_UPLOAD_TYPES=True`), up to 50 MB per file and 100 MB per request.

### 9.3 Make documents retrievable (source tiers)

Every document has an authority **source tier**. New documents start as `self_authored`, and retrieval skips `self_authored` documents unless `INCLUDE_SELF_AUTHORED_DOCS=True`. On a fresh database this applies to **every** ingested document, so Scholar mode will find nothing until you do one of the following:

- **Local development:** set `INCLUDE_SELF_AUTHORED_DOCS=True` in `.env` and restart the backend.
- **Proper classification:** set each document's tier through the API (admin token or `X-Admin-Key`). The admin UI has no tier control yet. Allowed tiers: `official_sap`, `sap_partner_or_book`, `official_sap_domain`, `forum`, `self_authored`, `github_repo`, `unknown`.

```sh
curl -X PATCH http://127.0.0.1:8000/api/documents/<document_id>/tier \
  -H "Authorization: Bearer <admin_access_token>" \
  -H "Content-Type: application/json" \
  -d '{"source_tier": "official_sap"}'
```

Document IDs are listed by `GET /api/documents` and in the Document Manager. `HUMAN_ACTIONS.md` lists the documents that are project-generated and should stay `self_authored`.

## 10. Start the frontend

In a new terminal:

```sh
cd frontend
npm ci
npm run dev
```

Open `http://localhost:5173`. `npm ci` installs the exact versions from `package-lock.json`; it is needed only the first time and after dependency changes.

The frontend calls the API on its own origin at `/api` (`frontend/src/services/apiBase.ts`), and the Vite dev server forwards `/api` to the backend at `http://localhost:8000`. The backend must therefore run on port 8000; no CORS setting is involved. If your backend runs elsewhere, set `VITE_DEV_API_PROXY_TARGET` in `frontend/.env` (template: `frontend/.env.example`) and restart `npm run dev`.

## 11. Sign in and create users

| Who | Where | How the account is created |
| --- | --- | --- |
| Super admin | `http://localhost:5173/admin/login` | from `SUPER_ADMIN_EMAIL` / `SUPER_ADMIN_PASSWORD` on first start |
| Individual user | `http://localhost:5173/login` | signs up on the **Sign up** tab (no code needed) |
| Organisation member | `http://localhost:5173/organisation/login` | created by the organisation's admin |

- **Individual sign-up** is open by default (`REGISTRATION_ENABLED=True`): users choose **Sign up** (`/login?mode=register`) with a password of 8–72 characters, then sign in. Set `REGISTRATION_ENABLED=False` and restart the backend to close sign-up.
- **Organisations:** a company requests a workspace at `/organisation`, or the super admin creates one under **Organisations** in the admin dashboard. Approving it issues a single-use activation code (valid 72 hours, shown once). The company's admin activates at `/organisation/activate` and then creates members with a starting password (12+ characters); members choose their own password at first sign-in. Details: [docs/ORGANISATION_PORTAL.md](docs/ORGANISATION_PORTAL.md).
- **Reset an admin password** (does not grant admin rights):

```sh
python backend/scripts/set_admin_password.py admin@your-company.com
```

## 12. Everyday start and stop

Dependencies, `.env` and the database are one-time setup. Daily:

```sh
# terminal 1 (repository root)
docker compose up -d postgres

# terminal 2 (repository root, virtual environment active)
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --reload

# terminal 3
cd frontend
npm run dev
```

Stop the backend and frontend with `Ctrl+C`. Stop the database (data is kept) with `docker compose stop postgres`.

## 13. Run the tests

### Backend

Tests never use the live database. `backend/tests/conftest.py` switches them to `<database>_test` (here `sap_brim_ai_test`, or `TEST_DATABASE_URL` if set) and refuses any database whose name does not end in `_test`. Create the test database once, either as a copy of your indexed database (stop the backend first; the copy needs no other connections to `sap_brim_ai`):

```sh
docker exec sap_assistant_postgres createdb -U sap_assistant -O sap_brim_ai -T sap_brim_ai sap_brim_ai_test
```

or empty:

```sh
docker exec sap_assistant_postgres createdb -U sap_assistant -O sap_brim_ai sap_brim_ai_test
docker exec sap_assistant_postgres psql -U sap_assistant -d sap_brim_ai_test -c "CREATE EXTENSION IF NOT EXISTS vector"
```

Then run the offline suite. Tests marked `live` call real LLM and web APIs and are excluded here:

```sh
python -m pytest backend/tests -m "not live"
```

On a machine with little memory, run one file at a time (`python -m pytest backend/tests/test_security.py`) and set `OPENBLAS_NUM_THREADS=1`. Some retrieval tests expect the real indexed knowledge base, so they fail against an empty test database.

### Frontend

```sh
cd frontend
npm test          # Vitest
npm run lint      # oxlint
npm run build     # TypeScript check + production build into frontend/dist
```

## 14. Docker reference

| Task | Command |
| --- | --- |
| Start | `docker compose up -d postgres` |
| Status / health | `docker compose ps` |
| Logs | `docker logs --tail 80 sap_assistant_postgres` |
| Stop (keeps data) | `docker compose stop postgres` |
| SQL shell (app role) | `docker exec -it sap_assistant_postgres psql -U sap_brim_ai -d sap_brim_ai` |
| Validate `.env` + compose file | `docker compose config --quiet` |

| Setting | Value |
| --- | --- |
| Container | `sap_assistant_postgres` |
| Image | `pgvector/pgvector:pg16` |
| Volume | `sap_brim_pgdata` |
| Host port | `127.0.0.1:${POSTGRES_HOST_PORT}` (default 5434), bound to localhost only |
| Bootstrap role / database | `sap_assistant` / `sap_assistant` (from `POSTGRES_USER` / `POSTGRES_DB`) |
| Application role / database | `sap_brim_ai` / `sap_brim_ai` |

Backups (`pg_dump`), restores into a separate database, GUI clients and example queries are documented in [docker.md](docker.md). Take and verify a backup before recreating containers or touching volumes.

**Backend and frontend in Docker:** `docker-compose.prod.yml` (with `backend/Dockerfile` and `frontend/Dockerfile`) runs the whole application in containers for a server; see [DEPLOYMENT.md](DEPLOYMENT.md). It is a separate Compose project (`sap_brim_prod`) with its own volumes, so it never touches the development database above. Inside that network the database host is `postgres`, port `5432`.

## 15. Deploying to a server

**Recommended: Docker.** [DEPLOYMENT.md](DEPLOYMENT.md) deploys the database, backend and web front door (with automatic HTTPS) on one Linux server with `docker compose up -d --build`, using the template `.env.example`.

**Without Docker**, the code requires the following.

**Backend**

1. Provision PostgreSQL 16 with pgvector (Compose as above, or a managed service with the `vector` extension) and an application role and database.
2. Install Python 3.12 and `backend/requirements.txt` in a virtual environment.
3. In `.env` (or real environment variables, which take precedence over `.env`), set:
   - `ENVIRONMENT=production`: disables `/docs`, `/redoc` and `/openapi.json`, rejects plain-HTTP requests (except `/health`), adds HSTS, and refuses empty, wildcard or localhost `ALLOWED_ORIGINS`;
   - `ALLOWED_ORIGINS` to the public frontend origin(s), for example `https://assistant.example.com`;
   - `AUTO_MIGRATE=False`, and run `python -m alembic -c backend/alembic.ini upgrade head` as a deploy step;
   - `TRUSTED_PROXIES` to the IP (or CIDR range) of your TLS-terminating reverse proxy, so the HTTPS check and client IPs use its `X-Forwarded-*` headers;
   - `REDIS_URL` if you run more than one worker or instance (`pip install "limits[redis]"`); otherwise rate limits are counted per process;
   - new, unique `JWT_SECRET`, `ADMIN_API_KEY` and database passwords. Values committed to this repository in the past are rejected at startup; see `HUMAN_ACTIONS.md`.
4. Start without `--reload` behind the reverse proxy, for example:

```sh
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

The backend handles `X-Forwarded-*` headers itself when `TRUSTED_PROXIES` is set.

**Frontend**

1. By default the app calls `/api` on its own origin. Easiest: serve the frontend and the API under one host name, with your web server forwarding `/api/` and `/health` to the backend (disable response buffering for `/api/`, answers are streamed). `frontend/Caddyfile` is a working example.
2. Only if the API has its own origin, build with `VITE_API_BASE_URL=https://api.example.com/api` (template: `frontend/.env.example`); the build also writes that origin into the Content-Security-Policy `connect-src`, and the frontend's origin must be in the backend's `ALLOWED_ORIGINS`.
3. Build: `cd frontend && npm ci && npm run build`.
4. Serve `frontend/dist/` as static files. Route unknown paths to `index.html` (client-side routing) and send `frame-ancestors 'none'` as a response header.

Also read `HUMAN_ACTIONS.md` before a production deployment: it lists owner decisions (secret rotation, knowledge-base licensing, approved LLM providers) and pre-deploy steps.

## 16. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `docker compose up` says `POSTGRES_PASSWORD` / `SAP_BRIM_DB_PASSWORD` must be set | `.env` is missing from the repository root or the values are empty. Create it as described in [section 4](#4-configure-the-env-file). |
| `docker` commands fail with "cannot connect to the Docker daemon" | Start Docker Desktop / the Docker service and wait until it is ready (`docker info`). |
| Docker Desktop on Windows reports read-only file systems, I/O errors or a `docker_data.vhdx` that cannot detach | Engine storage problem, not a database problem. Save work in WSL, run `wsl --shutdown`, then start Docker Desktop again. Do not factory-reset Docker or delete volumes. |
| Port 5434 already in use | Another PostgreSQL uses it. Set `POSTGRES_HOST_PORT` to a free port and use the same port in `DATABASE_URL`. |
| `DATABASE UNAVAILABLE - backend not started` / `Cannot connect to PostgreSQL` | The container is not running or `DATABASE_URL` is wrong. Check `docker compose ps`, the port and the password. |
| `password authentication failed for user "sap_brim_ai"` | The password in `DATABASE_URL` differs from the one stored when the volume was first created. Use the original `SAP_BRIM_DB_PASSWORD`. |
| `DATABASE_URL is not set` | `.env` is not in the repository root, or `DATABASE_URL` is missing. The backend reads only the root `.env`; an old `backend/.env` is ignored, so move its settings to the root file. |
| `Insecure configuration in .env: ...` | `JWT_SECRET` (or `ADMIN_API_KEY`) is shorter than 32 characters or a published value; or only one of `SUPER_ADMIN_EMAIL` / `SUPER_ADMIN_PASSWORD` is set, or the password is shorter than 12 characters. |
| `Database schema revision is ... but the application needs ...` | `AUTO_MIGRATE=False` and migrations are pending. Run `python -m alembic -c backend/alembic.ini upgrade head`. |
| `ModuleNotFoundError` (for example `fastapi`, `alembic`, `jwt`, `fitz`) or `email-validator is not installed` | Dependencies are missing from the active environment. Activate `.venv` and run `python -m pip install -r backend/requirements.txt`. |
| The first start hangs or fails while loading the embedding model | The model is downloaded from Hugging Face on first use. Allow network access to `huggingface.co` once; afterwards `HF_HUB_OFFLINE=1` works. |
| Scholar says "All response-generation methods are temporarily unavailable…" | None of the three LLM tiers answered, and Tavily web search returned nothing or is off (`WEB_FALLBACK_ENABLED`, `WEB_SEARCH_API_KEY`). Look in the backend log for the per-tier lines "LLM tier N (provider) produced no answer (category=…)". `auth_error` means a rejected key. `rate_limited` means quota. A model 404 means `LLM_MODEL` or an `LLM_*_MODELS` entry names a model your key cannot use. |
| A Scholar answer starts with "The AI answer service is temporarily unavailable…" and lists SAP links | All LLM tiers failed and the answer was assembled from approved SAP web sources (the last fallback). Fix the provider as above. |
| A Scholar answer ends with "Parts of this answer could not be confirmed…" | The answer did not pass claim verification against the knowledge base and web evidence. It is shown with this note instead of being discarded. Usually the knowledge base lacks the topic; check document tiers ([section 9.3](#93-make-documents-retrievable-source-tiers)). |
| Sprint says "I couldn't generate a verified response right now. Please try again later." | No LLM tier answered (Sprint has no web fallback). Check the keys and the per-tier log lines as above. |
| Scholar mode finds nothing although documents are indexed | All documents are still `self_authored` ([section 9.3](#93-make-documents-retrievable-source-tiers)), or ingestion did not run. Admins can check `/api/admin/system-health` and the Document Manager. |
| Questions that are not about SAP are refused | Expected: both chat modes answer SAP topics only (greetings and small talk are allowed). |
| Frontend shows "Cannot reach the server. Check your connection and make sure the backend is running." | Start the backend on port 8000 ([section 8](#8-start-the-backend)); the Vite dev server forwards `/api` there. |
| Browser console shows CORS errors | Only happens when `VITE_API_BASE_URL` points the app at another origin: add the exact frontend origin (scheme, host and port) to `ALLOWED_ORIGINS` and restart the backend. |
| Sign-up says "Self-registration is disabled. Ask an administrator for an account." | `REGISTRATION_ENABLED=False` in `.env`. Set it to `True` and restart the backend, or use the organisation flow ([section 11](#11-sign-in-and-create-users)). |
| `429 Too Many Requests` | Built-in rate limits (sign-in, answers, extraction). Wait a minute; adjust the `*_RATE_LIMIT_PER_MINUTE` settings for testing. |
| You are signed out of every browser after signing out in one | Expected: sign-out revokes all of that account's tokens. Access tokens also expire after 15 minutes. |
| Port 8000 or 5173 already in use | Windows: `Get-Process -Id (Get-NetTCPConnection -LocalPort 8000).OwningProcess`, then stop that process. macOS/Linux: `lsof -i :8000`. |
| `npm ci` or `npm test` fails with an engine or syntax error | Node.js is too old. Install Node.js 22.12 or newer. |
| `py -3.12` not found on Windows | Use the full path to `python.exe` from your Python 3.12 installation (see [section 2](#2-prerequisites)). |
