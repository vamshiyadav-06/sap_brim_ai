# Deployment Guide

How to run Clyptus.AI (SAP BRIM Knowledge Assistant) on a Linux server with Docker, with HTTPS on your own domain.

- Local development (database in Docker, backend and frontend on your machine): **[SETUP.md](SETUP.md)**.
- Database access, backups and SQL examples: **[docker.md](docker.md)**.
- Owner decisions to settle before going live (secret rotation, knowledge-base licensing, approved LLM providers): **[HUMAN_ACTIONS.md](HUMAN_ACTIONS.md)**.

## Contents

1. [What gets deployed](#1-what-gets-deployed)
2. [What you need](#2-what-you-need)
3. [Prepare the server](#3-prepare-the-server)
4. [Get the code](#4-get-the-code)
5. [Create the `.env` file](#5-create-the-env-file)
6. [Start the stack](#6-start-the-stack)
7. [Load the knowledge base](#7-load-the-knowledge-base)
8. [Sign in and check the deployment](#8-sign-in-and-check-the-deployment)
9. [Day-to-day operations](#9-day-to-day-operations)
10. [Troubleshooting](#10-troubleshooting)
11. [Other ways to host it](#11-other-ways-to-host-it)
12. [Go-live checklist](#12-go-live-checklist)

---

## 1. What gets deployed

`docker-compose.prod.yml` runs three containers on one server:

```text
 Browser ──HTTPS──▶ web (Caddy, ports 80/443) ──▶ backend (FastAPI, :8000) ──▶ postgres (PostgreSQL 16 + pgvector)
                     ├─ serves the React app              ├─ runs DB migrations on start      └─ volume: pgdata
                     ├─ /api/* and /health → backend       └─ reads ./knowledge_base
                     └─ gets the HTTPS certificate (Let's Encrypt)
```

| Container | Built from | Published | Data kept in |
| --- | --- | --- | --- |
| `web` | `frontend/Dockerfile` + `frontend/Caddyfile` | ports 80 and 443 | volumes `caddy_data`, `caddy_config` (certificates) |
| `backend` | `backend/Dockerfile` | no (only reachable through `web`) | bind mount `./knowledge_base` (source documents, admin uploads) |
| `postgres` | `pgvector/pgvector:pg16` | no | volume `pgdata` (users, chats, documents, embeddings) |

The browser only ever talks to `https://<your domain>`: the app is served at `/` and the API at `/api`, so no CORS or API address has to be configured.

## 2. What you need

| Item | Minimum | Notes |
| --- | --- | --- |
| Linux server (VM) | 2 vCPU, **4 GB RAM**, 30 GB disk | 8 GB RAM recommended. Ubuntu 22.04/24.04 is assumed below. The backend loads PyTorch and an embedding model (about 1.5 GB RAM). |
| Domain name | one host name, e.g. `assistant.example.com` | A DNS **A** record (and **AAAA** if the server has IPv6) pointing at the server's public IP. |
| Open ports | 22 (SSH), **80** and **443** (TCP), 443/UDP optional | Port 80 is needed for the Let's Encrypt certificate. |
| LLM API key | at least one | Groq is the default provider: <https://console.groq.com/keys>. Gemini and OpenRouter are fallbacks. |
| Tavily API key | optional | Web search fallback and SAP images: <https://app.tavily.com>. |

## 3. Prepare the server

SSH into the server, then:

```sh
# Docker Engine + Compose plugin (official convenience script)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"      # run docker without sudo; log out and back in afterwards

# Check (log in again first)
docker version
docker compose version               # must be v2.x

# Firewall (if ufw is enabled)
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443
```

On a cloud provider, also open ports 80 and 443 in the instance's security group / network firewall.

Check DNS before continuing; it must print the server's public IP:

```sh
getent hosts assistant.example.com
```

## 4. Get the code

```sh
sudo mkdir -p /opt/clyptus && sudo chown "$USER": /opt/clyptus
git clone https://github.com/vamshiyadav-06/SAP_chatbot.git /opt/clyptus/app
cd /opt/clyptus/app
```

All later commands run in this folder.

The backend container runs as user id `1000` and saves admin-uploaded documents into `./knowledge_base`. Make sure that user id can write there:

```sh
id -u                                   # if this prints 1000, nothing to do
sudo chown -R 1000:1000 knowledge_base  # otherwise (or set APP_UID in .env to your id)
```

## 5. Create the `.env` file

```sh
cp .env.example .env
chmod 600 .env
```

Generate one random value for **each** secret below (run the command once per value):

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Open `.env` (`nano .env`) and set:

| Setting | What to put there |
| --- | --- |
| `APP_DOMAIN` | your host name without `https://`, e.g. `assistant.example.com` |
| `ALLOWED_ORIGINS` | the same name with `https://`, e.g. `https://assistant.example.com` |
| `POSTGRES_PASSWORD` | a new random value (database superuser) |
| `SAP_BRIM_DB_PASSWORD` | a new random value (application database user) |
| `DATABASE_URL` | replace `change-me-app-password` with the **same** value as `SAP_BRIM_DB_PASSWORD`; keep `@postgres:5432` |
| `JWT_SECRET` | a new random value (signs login tokens; at least 32 characters) |
| `REDIS_PASSWORD` | a new random value, e.g. `openssl rand -hex 32` (protects the private Redis service that shares rate limits and login lockout between workers; `docker compose` refuses to start without it) |
| `SUPER_ADMIN_EMAIL`, `SUPER_ADMIN_PASSWORD` | your admin login (password 12-72 characters) |
| `GROQ_API_KEY` (and/or `GEMINI_API_KEY`, `OPENROUTER_API_KEY`) | at least one LLM key |
| `GROQ_API_KEY_FALLBACK_1` to `_3` (same for `GEMINI_` and `OPENROUTER_`) | optional extra keys for the worst case, see [Fallback API keys](#fallback-api-keys) |
| `WEB_SEARCH_API_KEY` | Tavily key, optional |
| `APP_UID` | only if `id -u` was not `1000` |
| `REGISTRATION_ENABLED` | `True` shows the Register button (open sign-up, no e-mail verification: anyone can use your LLM quota); `False` hides it and you create users with `backend/scripts/create_user.py`. It is built into the web image, so rebuild after changing it. |

Everything else can stay as it is. Notes:

- `.env` sets `COMPOSE_FILE=docker-compose.prod.yml`, so the plain `docker compose ...` commands in this guide use the production stack. Run them from the repository folder.
- The database passwords are applied **only when the database volume is created** (first start). Changing them later in `.env` does not change the database; see [Rotate secrets](#rotate-secrets).
- The backend refuses to start in production with an empty or short `JWT_SECRET`, a `change-me` database or admin password, or a `localhost`/wildcard `ALLOWED_ORIGINS`. The reason is printed in `docker compose logs backend`.
- `EMBEDDING_MODEL` must stay equal to the value used where the knowledge base was embedded (the template value matches the development template).

## 6. Start the stack

**If you will restore a backup in step 7 (option A), do that first and start the full stack afterwards.**

```sh
scripts/production/deploy.sh
```

The script refuses to continue unless `.env` is safe: it rejects template placeholders (including the database superuser password, which nothing else checks), a missing `ENVIRONMENT=production`, a mismatch between `ALLOWED_ORIGINS` and `APP_DOMAIN`, reused or short secrets, a `DATABASE_URL` password that differs from `SAP_BRIM_DB_PASSWORD`, and a missing LLM key. It then builds the images, runs the backend preflight (`python -m backend.app.preflight --expect-production --check-db`) inside the new image, takes a verified backup if an installation is already running, starts the new version and waits for the health check. It never deletes a volume. Run `python3 scripts/production/validate_env.py` on its own at any time to check `.env`.

(`docker compose up -d --build` still works, but skips all those checks; the compose file itself defaults the backend to `ENVIRONMENT=production` and closed sign-up.)

The first build downloads Python packages, PyTorch (CPU), the embedding model and the OCR models, and builds the React app. It takes about 10-20 minutes; later builds reuse the cache.

Check that everything runs:

```sh
docker compose ps                       # postgres "healthy", backend "healthy" (after up to 2 minutes), web "running"
docker compose logs -f backend          # wait for "SAP BRIM AI backend ready to accept requests."  (Ctrl+C to stop following)
curl https://assistant.example.com/health
# {"status":"healthy","database":{"status":"connected"}}
```

What happens on first start:

1. `postgres` creates the `sap_brim_ai` role and database with the `vector` extension (`docker/postgres/init`).
2. `backend` applies all database migrations (`alembic upgrade head`), creates the super admin from `.env`, then starts the API.
3. `web` obtains the HTTPS certificate for `APP_DOMAIN` (watch `docker compose logs web` if the site does not open).

## 7. Load the knowledge base

A new database contains no documents, so Scholar mode has nothing to retrieve until you do **one** of the following.

### Option A (recommended): restore a backup of an existing installation

This copies everything: documents, embeddings, source tiers, users and chats, so nothing has to be re-embedded.

**On the existing machine** (for example the development PC, with its database container running), make a backup as the application role, which exists on every installation:

```sh
docker exec sap_assistant_postgres pg_dump -U sap_brim_ai -d sap_brim_ai -Fc -f /tmp/sap_brim_ai.dump
docker cp sap_assistant_postgres:/tmp/sap_brim_ai.dump ./sap_brim_ai.dump
scp ./sap_brim_ai.dump <user>@<server>:/opt/clyptus/
```

Also copy documents that were uploaded through the admin portal and are not in Git: copy the existing `knowledge_base/documents/` folder into `/opt/clyptus/app/knowledge_base/documents/` on the server (for example with `scp -r` or `rsync`).

**On the server**, before the first full start (fresh `pgdata` volume):

```sh
docker compose up -d postgres                                   # database only; wait until "healthy" in: docker compose ps
docker compose cp /opt/clyptus/sap_brim_ai.dump postgres:/tmp/sap_brim_ai.dump
docker compose exec postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d sap_brim_ai --no-owner --role=sap_brim_ai --no-comments --exit-on-error /tmp/sap_brim_ai.dump'
docker compose exec postgres rm /tmp/sap_brim_ai.dump
docker compose up -d --build                                    # backend migrates the restored data to the current version
```

`pg_restore` prints nothing when it succeeds. It must run against the empty database created on first start; if it reports that objects already exist, the database is not empty: stop and check before going further. Users and admins from the backup can sign in with their existing passwords; `SUPER_ADMIN_EMAIL` only creates an admin if that e-mail does not exist yet.

### Option B: start empty and ingest the documents

1. Index every document in `knowledge_base/documents/` (PDF, TXT, MD); this is CPU-heavy and takes a while:

   ```sh
   docker compose exec backend python backend/app/ingestion/ingest.py
   ```

   Or upload files in the admin portal (**Document Manager & Ingestion**) and click **Process All Documents**.

2. Set each document's **source tier**. New documents start as `self_authored`, which retrieval skips (`INCLUDE_SELF_AUTHORED_DOCS=False`), so Scholar finds nothing until tiers are set. The admin UI has no tier control yet; use the API with an admin token:

   ```sh
   DOMAIN=https://assistant.example.com
   TOKEN=$(curl -s "$DOMAIN/api/auth/admin-login" -H 'Content-Type: application/json' \
     -d '{"email":"admin@your-company.com","password":"<admin password>"}' \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

   # list documents (id, filename, tier)
   curl -s "$DOMAIN/api/documents?page_size=200" -H "Authorization: Bearer $TOKEN"

   # classify one document
   curl -s -X PATCH "$DOMAIN/api/documents/<document_id>/tier" -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"source_tier":"official_sap"}'
   ```

   Tiers: `official_sap`, `sap_partner_or_book`, `official_sap_domain`, `forum`, `self_authored`, `github_repo`, `unknown`. `HUMAN_ACTIONS.md` lists the project-generated documents that should stay `self_authored`.

## 8. Sign in and check the deployment

| Who | URL |
| --- | --- |
| Super admin | `https://<domain>/admin/login` |
| Individual users (sign up themselves) | `https://<domain>/login` |
| Organisation members | `https://<domain>/organisation/login` |

Smoke test after every deployment:

- [ ] `https://<domain>/health` returns `"healthy"`, and `http://<domain>` redirects to HTTPS.
- [ ] Admin login works, and the admin **Document Manager** lists the expected documents as processed.
- [ ] A **Sprint** question gets an answer that streams in word by word (if it arrives all at once at the end, see Troubleshooting).
- [ ] A **Scholar** question about a document in the knowledge base gets an answer.
- [ ] Sign-up, sign-in and sign-out work for a normal user.

The interactive API documentation (`/docs`) is switched off in production and is not forwarded by the web container.

## 9. Day-to-day operations

All commands run in `/opt/clyptus/app`.

### Status and logs

```sh
docker compose ps
docker compose logs -f backend          # or: web, postgres
docker compose logs --since 1h backend
```

Container logs are rotated automatically (5 files of 10 MB per container).

### Update to a new version

```sh
git pull
scripts/production/deploy.sh            # validates .env, preflights, backs up, then replaces the running version
docker compose ps                       # backend becomes "healthy" again after the migrations ran
```

The script records the deployed image tag in `.deploy/production-image-tag` and prints the one-line rollback command (`IMAGE_TAG=<previous> docker compose up -d`) at the end.

The backend applies new database migrations when it starts. If the backend does not come back, check `docker compose logs backend` before doing anything else.

### Roll back

If a new version misbehaves, go back to the previous commit. Migrations are additive, so older code runs on the newer schema; if a migration itself failed, restore the backup you took before updating.

```sh
git log --oneline -5                      # note the last good commit
git checkout <good-commit>
docker compose up -d --build
docker compose ps
```

Do not run `docker compose down -v` (it deletes the database volume).

### Backups

Back up the database daily and keep copies **off the server**. `scripts/production/backup.sh` writes a dump, proves it can be read back (`pg_restore --list`), archives `knowledge_base/documents/`, optionally uploads both, and prunes files older than 14 days. Files are `0600` in a `0700` directory.

```sh
scripts/production/backup.sh                                   # one backup now; the file must not be empty
BACKUP_UPLOAD_CMD='rclone copy "$1" remote:clyptus-backups'   scripts/production/install-backup-cron.sh                    # daily at 02:00, log in .deploy/backup.log
```

`BACKUP_UPLOAD_CMD` is run once per new file with the file as `$1` (any command: `rclone`, `rsync`, `aws s3 cp`). Without it the backup stays on the server and the script warns. Settings: `BACKUP_DIR` (default `/opt/clyptus/backups`), `BACKUP_KEEP_DAYS`.

**Prove the restore.** After the first backup and then monthly, run `scripts/production/restore-check.sh`: it loads the newest dump into a scratch database next to the live one, counts tables and users, and drops only that scratch database.

Also back up `.env` (it holds the only copy of your secrets) and `knowledge_base/documents/` (admin uploads). The backups contain user data and chat history: store them encrypted and with restricted access.

**Restore** a backup: follow [Option A](#option-a-recommended-restore-a-backup-of-an-existing-installation) on a fresh database. To restore over a running installation, restore into a new database next to it first and check it, as described in [docker.md §8](docker.md#8-backup-and-restore). Never delete the `pgdata` volume (`docker compose down -v`) without a verified backup.

### Rotate secrets

**Keys that were committed to Git (`685bb31`, Clyptus-Soft history) must be rotated.** That commit put the Groq, Google Gemini and OpenRouter API keys into `.env.example`. Create a **new** key at each provider's console, put it in the server `.env`, then delete (revoke) the old key there. Never reuse a value from Git, chat or a template. The history was not rewritten; removing it from Clyptus-Soft needs a force-push that only the repository owner should decide.

- **`JWT_SECRET`:** change it in `.env`, then `docker compose up -d backend`. Every user is signed out.
- **LLM / Tavily keys:** change them in `.env`, then `docker compose up -d backend`.
- **Database application password:** the database keeps its own copy, so change both:

  ```sh
  NEW='<new random value>'
  docker compose exec postgres sh -c "psql -U \"\$POSTGRES_USER\" -d postgres -c \"ALTER ROLE sap_brim_ai PASSWORD '$NEW'\""
  # then set SAP_BRIM_DB_PASSWORD and the password inside DATABASE_URL in .env to the same value, and:
  docker compose up -d backend
  ```

- **Admin password:** `docker compose exec backend python backend/scripts/set_admin_password.py admin@your-company.com` (prompts for the new password).

### Restart and stop

```sh
docker compose restart backend          # e.g. after editing .env: use "docker compose up -d backend" instead, which reloads .env
docker compose stop                     # stop everything; data is kept
docker compose up -d                    # start again
```

### Capacity

The backend runs **one** worker process on purpose: rate limits, login lockout and the Stop button for answers that keep generating after the browser leaves are tracked inside that process (`KNOWN_ISSUES.md` STREAM-001). For more capacity, give the server more CPU and RAM. Running several workers or replicas first needs `REDIS_URL` (shared rate limits) and the STREAM-001 fix.

**Many users at once.** The number of answers that run at the same time is bounded, so one burst of questions cannot take the service down:

| Setting (`.env`) | Default | Meaning |
| --- | --- | --- |
| `MAX_CONCURRENT_ANSWERS` | 16 | answers running at once (0 = no limit) |
| `ANSWER_QUEUE_MAX` / `ANSWER_QUEUE_WAIT_SECONDS` | 32 / 15 | how many more may wait for a slot, and for how long; everyone beyond that gets an immediate "busy, try again" (HTTP 503 with `Retry-After`) |
| `MAX_CONCURRENT_ANSWERS_PER_USER` | 3 | one user cannot take every slot (HTTP 429) |
| `DATABASE_POOL_SIZE` / `DATABASE_MAX_OVERFLOW` / `DATABASE_POOL_TIMEOUT` | 10 / 20 / 10 | database connections, and how long a request waits for one before a clean 503 |
| `THREADPOOL_SIZE` | 64 | threads for synchronous endpoints (framework default is 40) |
| `SHUTDOWN_DRAIN_SECONDS` | 25 | on stop or redeploy, running answers get this long to finish and be saved (`stop_grace_period` is 30 s) |
| `WEB_SEARCH_TIMEOUT_SECONDS` | 15 | per call to the web-search provider |

A slot lasts as long as the answer, including an answer that keeps generating after the browser left. Keep `MAX_CONCURRENT_ANSWERS + 6 <= DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW`: each running answer can use a database connection, and sign-in and health checks need some left (the backend logs a `[CAPACITY]` warning at start if this is violated). `/health` has its own executor and a 3-second timeout, so a busy service still reports healthy and a stuck one reports unhealthy quickly instead of hanging. The limits are **per backend process**; with the single worker above they are the limits of the service.

These numbers are conservative defaults, not measured capacity. The provider quotas (Groq's free tier allows about 8,000 tokens a minute) usually limit throughput before the server does. Measure on staging with `scripts/staging/test-load.py` and the approved providers, then adjust.

### Monitoring and Redis

Point an uptime monitor at `https://<domain>/health` and alert on anything but HTTP 200. The endpoint returns 503 when the database or the Redis rate-limit store is unreachable.

If Redis stops after the backend started, rate limits and the login lockout keep working **per process** (the single backend worker) instead of failing every request; the backend logs `Shared rate-limit storage failed ... using per-process limits` once a minute and switches back by itself when Redis returns. Redis restarts on its own (`restart: unless-stopped`); `/health` stays 503 until it is back, which is what the alert is for.

### Fallback API keys

Each provider can have up to three extra keys for the worst case: `GROQ_API_KEY_FALLBACK_1` to `_3`, `GEMINI_API_KEY_FALLBACK_1` to `_3`, `OPENROUTER_API_KEY_FALLBACK_1` to `_3`. The names are in `.env.example`; leave a value empty to leave that slot unused. Put a key in, run `docker compose up -d backend`, and it is used automatically; nothing else needs changing.

Order: the normal key first, then fallback 1, 2, 3. The next key is tried **only** when the key before it is rejected (401/403), out of credit (402) or out of quota (429). A problem that is not the key's fault (an empty reply, a too-long prompt) does not use up the other keys. A key that failed is skipped for a while (a rejected key for an hour, a rate limit for the provider's retry time) while the next key answers. Only when every key of a provider fails does the request move on to the next provider (Groq, then Gemini, then OpenRouter, then web search). A streamed answer never changes key after text was sent. Use a separate key for each slot, ideally from a separate account, since keys of one account share one quota. Key values are never logged. The OpenRouter tier also needs paid models in `LLM_OPENROUTER_MODELS` or `LLM_ALLOW_FREE_TIER=True`.

## 10. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `docker compose` complains `Set POSTGRES_PASSWORD in .env` / `Set APP_DOMAIN in .env` | `.env` is missing or the value is empty. Run commands from the repository folder; check `.env`. |
| `docker compose ps` lists only a `postgres` service named `sap_assistant_postgres` | `.env` does not set `COMPOSE_FILE=docker-compose.prod.yml`, so the development file was used. Add the line, or use `docker compose -f docker-compose.prod.yml ...`. |
| backend keeps restarting, log says `INSECURE CONFIGURATION` | Fix the setting named in the message (`JWT_SECRET`, a `change-me` password, `SUPER_ADMIN_*`), then `docker compose up -d backend`. |
| backend log: `Production requires explicit public ALLOWED_ORIGINS` | Set `ALLOWED_ORIGINS=https://<your domain>` (no localhost, no `*`). |
| backend log: `password authentication failed for user "sap_brim_ai"` | `DATABASE_URL` password differs from the one the database was created with. Use the original value, or change it in the database ([Rotate secrets](#rotate-secrets)). |
| The site does not open / certificate errors | `docker compose logs web`: DNS must point at this server and ports 80 and 443 must be reachable from the internet. Let's Encrypt limits retries; fix DNS/firewall first, then `docker compose restart web`. |
| `502 Bad Gateway` on `/api/...` | The backend is still starting (model loading, migrations) or has stopped: `docker compose ps`, `docker compose logs backend`. |
| API requests answer `{"detail":"HTTPS is required."}` | A request reached the backend without passing through `web`, or `TRUSTED_PROXIES` was overridden. Use the domain over HTTPS; do not publish the backend port. |
| Every answer says the assistant is unavailable | No working LLM key: check `GROQ_API_KEY` / `GEMINI_API_KEY` / `OPENROUTER_API_KEY` and the backend log (`missing_api_key`, `auth_error`, `rate_limited`). |
| Scholar finds nothing although documents are processed | Source tiers are still `self_authored` ([Option B step 2](#option-b-start-empty-and-ingest-the-documents)), or `EMBEDDING_MODEL` differs from the one the vectors were made with (re-ingest or restore the matching value, then rebuild). |
| Admin upload fails with a permission error | `./knowledge_base` is not writable by `APP_UID` (default 1000): `sudo chown -R 1000:1000 knowledge_base`. |
| Answers appear only at the end instead of streaming | Something between the browser and the server buffers responses (a CDN or corporate proxy). Disable buffering for `/api/`; Caddy itself flushes immediately. |
| The build fails while installing packages | Retry `docker compose build --no-cache backend`; check free disk space (`df -h`, at least 10 GB free for the build). |

## 11. Other ways to host it

- **Behind an existing reverse proxy or load balancer that terminates HTTPS:** you can still use the images. Run `web` on an internal port (`HTTP_PORT=8080`, `APP_DOMAIN=:80`, and drop the 443 mappings), point your proxy at it, and make sure your proxy sets `X-Forwarded-Proto: https`. Caddy forwards that header only from proxies it trusts: add a [`trusted_proxies`](https://caddyserver.com/docs/caddyfile/options#trusted-proxies) global option with your proxy's address to `frontend/Caddyfile`, otherwise the backend answers `HTTPS is required`.
- **Frontend on a static host (CDN), API elsewhere:** build the frontend with `VITE_API_BASE_URL=https://api.example.com/api` (see `frontend/.env.example`), serve `frontend/dist/` with an `index.html` fallback for unknown paths, and add the frontend's origin to the backend's `ALLOWED_ORIGINS`.
- **Without Docker:** follow [SETUP.md §15](SETUP.md#15-deploying-to-a-server) (Python 3.12 virtual environment, `uvicorn` behind your own HTTPS proxy, PostgreSQL 16 with pgvector).

## 12. Go-live checklist

- [ ] All secrets in `.env` are new random values; no value from a template, chat or old commit is reused. Keys that were ever committed to Git are revoked (`HUMAN_ACTIONS.md`).
- [ ] `ENVIRONMENT=production`, `ALLOWED_ORIGINS=https://<domain>`, `APP_DOMAIN=<domain>`.
- [ ] `.env` is `chmod 600` and backed up somewhere safe.
- [ ] The knowledge base is loaded and source tiers are set (Scholar answers from documents).
- [ ] `scripts/production/deploy.sh` passed (env check, preflight).
- [ ] A daily database backup runs with `BACKUP_UPLOAD_CMD` set (copies off the server), and `scripts/production/restore-check.sh` has succeeded once.
- [ ] An uptime monitor alerts on `/health`.
- [ ] `.env` is not inside a cloud-synced folder (OneDrive, Dropbox, iCloud) and no `.env*.bak` copies exist.
- [ ] Ports 80 and 443 are open; nothing else is published (PostgreSQL and the backend are internal).
- [ ] The smoke test in [section 8](#8-sign-in-and-check-the-deployment) passes.
- [ ] Owner decisions in `HUMAN_ACTIONS.md` (knowledge-base licensing, approved LLM providers) are settled.
