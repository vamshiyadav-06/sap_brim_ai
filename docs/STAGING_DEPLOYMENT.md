# Private Ubuntu staging deployment

This procedure runs the production images in an isolated Compose project and starts with a fresh staging database. It never targets the production project or the live server. Use a staging VM or an approved local Ubuntu VM; do not substitute the production host without separate approval.

## 1. Prepare Ubuntu

Use Ubuntu 22.04/24.04, Docker Engine with the Compose v2 plugin, Python 3, Git, and `curl`. Allow SSH only. Keep ports 80/443 closed to the Internet until access has been reviewed. The Compose project binds them to `127.0.0.1` by default, so connect through an SSH tunnel while testing.

```sh
git clone <repository-url> sap-brim-staging
cd sap-brim-staging
cp .env.example .env
chmod 600 .env
scripts/staging/init-env.sh
```

`init-env.sh` refuses to overwrite `.env`, fills the database and signing secrets with fresh random values, URL-encodes the application DB password, and prints no secret. The generated super-admin password is saved in `.admin-initial-password` with mode `600`; retrieve it locally for the first sign-in and remove that credential file after securely recording the password. Set `APP_DOMAIN` and matching `ALLOWED_ORIGINS`, `SUPER_ADMIN_EMAIL`, and at least one staging-only provider key. Use provider keys with spending limits; do not copy production keys. Create a long, unique staging gate password and its Caddy bcrypt hash using the pinned Caddy CLI:

```sh
docker run --rm -it caddy:2.11.7-alpine caddy hash-password
```

Put the chosen username in `STAGING_BASIC_AUTH_USER` and the returned hash in `STAGING_BASIC_AUTH_HASH`. Treat the gate password as a secret. Confirm `.env` is mode `600`. Keep `COMPOSE_PROJECT_NAME=sap_brim_staging`, `ACCESS_MODE=basic_auth`, `REGISTRATION_ENABLED=False`, and `WEB_BIND=127.0.0.1`.

## 2. Create the fresh staging database and deploy

The first `up` creates `sap_brim_staging_pgdata`, a new project volume with a new empty PostgreSQL database. It does not mount the production volume. The initialization script creates only the staging app role/database; Alembic applies the schema. No production backup or user data is copied.

```sh
scripts/staging/deploy.sh
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail=100 backend
```

The deploy script checks the required staging mode, closed registration, gate credentials, provider key, and local-only bind; validates Compose; builds the images; runs the no-network backend preflight; starts staging; and polls `/health`. Inspect logs before continuing if health fails. The web port remains private. For access from a laptop, create an SSH tunnel to the staging VM and open the staging hostname through the local tunnel (or use a hosts entry for the staging hostname).

Create tester accounts through the hidden password prompt; public registration remains off:

```sh
docker compose -f docker-compose.prod.yml exec backend \
  python backend/scripts/create_user.py tester@example.com "Staging Tester"
```

## 3. Browser, load, and recovery checks

Before deploying, build the application images in the isolated E2E configuration without starting containers or creating a database:

```sh
# create .env.e2e with: COMPOSE_PROJECT_NAME=sap_brim_e2e APP_ENV_FILE=.env.e2e IMAGE_TAG=e2e APP_DOMAIN=localhost ALLOWED_ORIGINS=https://localhost ENVIRONMENT=development HTTP_PORT=18080 HTTPS_PORT=18443 WEB_BIND=127.0.0.1 ACCESS_MODE=open, plus E2E-only secrets
scripts/staging/verify-build.sh
```

Install the browser test dependency and browser once on the operator workstation:

```sh
cd frontend
npm ci
npx playwright install chromium
```

Set `E2E_BASE_URL` to the staging HTTPS URL, `E2E_BASIC_USER` and `E2E_BASIC_PASSWORD` to the staging gate credentials, and (for authenticated API streaming) `E2E_ADMIN_EMAIL` and `E2E_ADMIN_PASSWORD` to the staging admin. Run `npm run test:e2e`. The access test verifies the app can load through the gate and registration returns 403. The streaming test is optional and requires the dedicated E2E fake-provider stack described below; do not run a streaming test against staging with real provider keys unless the owner has separately approved the AI call.

For a bounded authenticated read load, keep a short-lived staging token in a secure environment variable and run `scripts/staging/test-load.py --url https://<stage>/api/chats --requests 100 --concurrency 10`. The script reads `STAGING_TEST_TOKEN`; it rejects a missing value and does not accept tokens as command-line arguments. It calls only the chat-list API and does not generate answers.

Verify database recovery only on the isolated staging database:

```sh
scripts/staging/test-recovery.sh
```

This takes a custom-format dump and restores it into a new timestamped recovery database in the staging PostgreSQL container. It does not drop databases or remove dumps. Review its output and confirm the recovered Alembic revision. Keep the extra recovery DB/dump until the result has been reviewed; cleanup is a separate operator decision.

### Charge-free local E2E stack

For authenticated streaming and cancellation/timeout exercises, use a separate E2E Compose project with a fresh `.env.e2e`: create it as shown above. This uses `COMPOSE_PROJECT_NAME=sap_brim_e2e` and separate database volumes. Run `docker compose --env-file .env.e2e -f docker-compose.prod.yml -f docker-compose.e2e.yml up -d --build`. The overlay routes OpenRouter to the local `e2e/stub_llm/server.py`, blanks real API keys, and disables web fallback. Use E2E-only credentials and never point this project at existing staging or production data. Never run `down -v`; leave test artifacts in place until reviewed. Set `E2E_ALLOW_STUB_STREAM=1` only when running Playwright against this stack; streaming tests are skipped by default to avoid unintended provider calls.

## 4. Public tester access

Keep the default loopback bind while setting up. If remote testers need direct access, first confirm the staging gate is still enabled and registration is closed, then run `scripts/staging/with-public-bind.sh` and allow only the named tester IPs at the Ubuntu/cloud firewall. Basic auth remains in front of the site and public endpoints. Do not make this change to the live Ubuntu server as part of this repository task.

## 5. Updates and rollback

Each deploy uses an image tag derived from the checked-out commit and stores the successful staging tag under ignored `.deploy/`. Back up staging before applying schema/data changes. The current deploy deliberately leaves a failed rollout running for inspection and prints the previous tag; it does not automatically switch versions after a migration may have changed the database. Restore only after reviewing logs and choosing a compatible database state. Never remove Compose volumes as a rollback step.

## 6. Operational boundaries

- Do not run production commands from this guide or reuse production secrets, data, provider keys, or volumes.
- Do not push code, log into the live Ubuntu host, or call a real provider for these checks.
- `REGISTRATION_ENABLED=False` is also enforced by the API; the Caddy gate adds a second access boundary.
- Secrets, tester passwords, access tokens, request bodies, and database contents must not be copied into test output or the readiness audit.
- Docker image build, browser execution, load checks, and recovery verification count as passed only after the commands complete successfully.
