#!/usr/bin/env bash
# Build production backend/web images using the isolated local E2E configuration; start no containers.
set -euo pipefail
cd "$(dirname "$0")/../.."
[[ -f .env.e2e ]] || { echo "Missing .env.e2e; create it first (local fake-provider values; see docs/STAGING_DEPLOYMENT.md)." >&2; exit 1; }
docker compose --env-file .env.e2e -f docker-compose.prod.yml -f docker-compose.e2e.yml config --quiet
docker compose --env-file .env.e2e -f docker-compose.prod.yml -f docker-compose.e2e.yml build backend web
echo "Backend and web image builds completed; no containers or database volumes were started."
