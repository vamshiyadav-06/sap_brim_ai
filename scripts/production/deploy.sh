#!/usr/bin/env bash
# Deploy (or update) the production stack. Nothing is replaced until every check has passed.
#   scripts/production/deploy.sh
# Order: validate .env -> build images -> start database+Redis -> preflight inside the new image (with a
# database connection) -> verified backup of the running installation -> start the new version -> health check.
# It never runs `down -v` and never deletes a volume.
set -euo pipefail
cd "$(dirname "$0")/../.."

[[ -f .env ]] || { echo "Missing .env; start with: cp .env.example .env" >&2; exit 1; }
if [[ "$(stat -c %a .env 2>/dev/null || echo 600)" != "600" ]]; then
  echo "WARN: .env is readable by others; run: chmod 600 .env" >&2
fi
python3 scripts/production/validate_env.py .env

COMPOSE=(docker compose -f docker-compose.prod.yml)
export IMAGE_TAG="prod-$(git rev-parse --short=12 HEAD)"
mkdir -p .deploy
previous=''
[[ -f .deploy/production-image-tag ]] && previous="$(cat .deploy/production-image-tag)"

"${COMPOSE[@]}" config --quiet
"${COMPOSE[@]}" build

# Database and Redis are only started (never recreated with different data); they hold state.
"${COMPOSE[@]}" up -d postgres redis
for _ in $(seq 1 30); do
  [[ "$("${COMPOSE[@]}" ps --format '{{.Health}}' postgres redis | grep -c healthy)" == "2" ]] && break
  sleep 2
done

"${COMPOSE[@]}" run --rm --no-deps backend python -m backend.app.preflight --expect-production --check-db

if "${COMPOSE[@]}" ps --status running --services | grep -qx backend; then
  echo "Backing up the running installation before replacing it..."
  scripts/production/backup.sh || { code=$?; [[ $code -eq 2 ]] || exit "$code"; echo "WARN: backup upload failed (kept locally)." >&2; }
else
  echo "No running backend: first deployment, backup skipped."
fi

"${COMPOSE[@]}" up -d --remove-orphans

for _ in $(seq 1 60); do
  if "${COMPOSE[@]}" exec -T backend python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)" >/dev/null 2>&1; then
    printf '%s\n' "$IMAGE_TAG" > .deploy/production-image-tag
    chmod 600 .deploy/production-image-tag
    echo "Deployed $IMAGE_TAG: backend healthy. Check the site over HTTPS (certificate issuance can take a minute)."
    [[ -n "$previous" ]] && echo "Roll back with: IMAGE_TAG=$previous docker compose -f docker-compose.prod.yml up -d"
    exit 0
  fi
  sleep 5
done

echo "Health check failed; the stack was left running for inspection." >&2
"${COMPOSE[@]}" ps >&2
"${COMPOSE[@]}" logs --tail=100 backend >&2
[[ -n "$previous" ]] && echo "To restore the previous image: IMAGE_TAG=$previous docker compose -f docker-compose.prod.yml up -d" >&2
exit 1
