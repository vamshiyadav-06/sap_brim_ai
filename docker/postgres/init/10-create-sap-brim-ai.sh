#!/bin/sh
# Runs once, when the PostgreSQL volume is first initialised (docker-entrypoint-initdb.d).
# Creates the application's dedicated role and database with the pgvector extension.
# Tables and indexes are created by the backend's Alembic migrations when its container starts.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
     -v app_password="$SAP_BRIM_DB_PASSWORD" <<'SQL'
CREATE ROLE sap_brim_ai LOGIN PASSWORD :'app_password';
CREATE DATABASE sap_brim_ai OWNER sap_brim_ai ENCODING 'UTF8' TEMPLATE template0;
\connect sap_brim_ai
CREATE EXTENSION IF NOT EXISTS vector;
SQL
