#!/usr/bin/env bash
# Build and deploy only the isolated staging Compose project. The public bind is never enabled here.
set -euo pipefail
cd "$(dirname "$0")/../.."

[[ -f .env ]] || { echo "Missing .env; start with .env.example and init-env.sh." >&2; exit 1; }
python3 - <<'PY'
from pathlib import Path
import re
values = {}
for line in Path('.env').read_text(encoding='utf-8').splitlines():
    match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$', line)
    if match:
        values[match.group(1)] = match.group(2).strip().strip("'\"")
required = {
    'COMPOSE_PROJECT_NAME': 'sap_brim_staging', 'ENVIRONMENT': 'production',
    'REGISTRATION_ENABLED': 'False', 'ACCESS_MODE': 'basic_auth', 'WEB_BIND': '127.0.0.1',
}
bad = [key for key, expected in required.items() if values.get(key, '').lower() != expected.lower()]
for key in ('APP_DOMAIN', 'ALLOWED_ORIGINS', 'SUPER_ADMIN_EMAIL', 'SUPER_ADMIN_PASSWORD',
            'JWT_SECRET', 'POSTGRES_PASSWORD', 'SAP_BRIM_DB_PASSWORD', 'STAGING_BASIC_AUTH_USER',
            'STAGING_BASIC_AUTH_HASH'):
    if not values.get(key) or 'change-me' in values[key].lower() or 'example.com' in values[key].lower():
        bad.append(key)
provider_keys = [f'{p}_API_KEY' + s for p in ('GROQ', 'GEMINI', 'OPENROUTER')
                 for s in ('', '_FALLBACK_1', '_FALLBACK_2', '_FALLBACK_3')]
if not any(values.get(k) for k in provider_keys):
    bad.append('LLM provider key')
if bad:
    raise SystemExit('Staging configuration is incomplete or unsafe: ' + ', '.join(sorted(set(bad))))
PY

export COMPOSE_PROJECT_NAME=sap_brim_staging
export IMAGE_TAG="staging-$(git rev-parse --short=12 HEAD)"
docker compose -f docker-compose.prod.yml config --quiet
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml run --rm --no-deps \
  -e ACCESS_MODE=basic_auth -e STAGING_BASIC_AUTH_USER=validated \
  -e STAGING_BASIC_AUTH_HASH=validated -e WEB_BIND=127.0.0.1 \
  backend python -m backend.app.preflight --expect-staging

mkdir -p .deploy
previous=''
if [[ -f .deploy/staging-image-tag ]]; then previous=$(cat .deploy/staging-image-tag); fi
IMAGE_TAG="$IMAGE_TAG" docker compose -f docker-compose.prod.yml up -d --remove-orphans

app_domain=$(awk -F= '$1 == "APP_DOMAIN" {print $2}' .env | tr -d "'\"")
health_url="https://${app_domain}:${HTTPS_PORT:-443}/health"
for _ in $(seq 1 60); do
  if curl --silent --show-error --insecure --resolve "${app_domain}:${HTTPS_PORT:-443}:127.0.0.1" --max-time 5 "$health_url" | grep -q '"status":"healthy"'; then
    printf '%s\n' "$IMAGE_TAG" > .deploy/staging-image-tag
    chmod 600 .deploy/staging-image-tag
    echo "Staging is healthy at $health_url; the web port remains bound to 127.0.0.1."
    exit 0
  fi
  sleep 5
done

echo "Staging health check failed; current deployment remains available for inspection." >&2
docker compose -f docker-compose.prod.yml ps >&2
docker compose -f docker-compose.prod.yml logs --tail=100 backend >&2
if [[ -n "$previous" ]]; then
  echo "To restore the previous staging image, run: IMAGE_TAG=$previous docker compose -f docker-compose.prod.yml up -d"
fi
exit 1
