# Human actions required (audit remediation)

Items the code changes on branch `audit-fixes` cannot do, or decisions that belong to the owner.
Finding IDs refer to `AUDIT_REPORT.md`. Nothing below has been executed.

## 1. Rotate the committed secrets (F-48) — do first

The three values listed in `backend/app/config.py` `PUBLISHED_SECRETS` were committed and pushed to the
`origin` (github.com/Clyptus-Soft/SAP_BRIM_Ai) and `target` (github.com/vamshiyadav-06/SAP_chatbot) remotes.
`target` is public. The startup denylist prevents reuse in this codebase only; anyone who cloned the repo knows them.

- [ ] Generate a new `JWT_SECRET` (`python -c "import secrets; print(secrets.token_urlsafe(48))"`) in every environment.
      Rotating it signs every user out.
- [ ] Generate a new `ADMIN_API_KEY`, or leave it empty to disable key-based admin access.
- [ ] Change the password of every admin account that ever used the published admin password
      (`py -3.12 backend/scripts/set_admin_password.py`).
- [ ] Change any other account (database role, email, cloud console, personal accounts) that reused any of these values.
- [ ] Check provider dashboards (Groq, Gemini, OpenRouter, Tavily) for unexpected usage, and rotate those API keys
      if `.env` files were ever shared.

## 2. Coordinated git history rewrite and secret scan (F-48, F-01) — commands only, NOT run

Rewriting history changes every commit hash; coordinate with everyone who has a clone, and back up first.

```bash
# 0. Fresh mirror clone; never rewrite your working clone.
git clone --mirror https://github.com/vamshiyadav-06/SAP_chatbot.git SAP_chatbot-mirror.git
cd SAP_chatbot-mirror.git

# 1. Scan the full history of every ref before and after the rewrite.
gitleaks detect --source . --log-opts="--all" --report-path ../gitleaks-before.json

# 2. Replace the leaked literals everywhere in history. replacements.txt contains one line per secret:
#      <literal-secret>==>REDACTED
#    (Write the file locally; do not commit it.)
git filter-repo --replace-text ../replacements.txt

# 3. If licensed PDFs must leave history (see item 3), also:
git filter-repo --invert-paths \
  --path "knowledge_base/documents/BR234_EN_Col22_CO_A4.pdf" \
  --path "knowledge_base/documents/BR234_EN_SOM 2.pdf" \
  --path "knowledge_base/documents/BR245_EN_Col23_CO_A4.pdf" \
  --path "knowledge_base/documents/BRIM_AC240_EN_Col16.pdf" \
  --path "knowledge_base/documents/SAP BRIM Press Book 2nd Edition - Expertsoft.pdf" \
  --path "knowledge_base/documents/E_Book_ABAP_RESTful_Programming_Model_2020_Rel_NW_wwwERPExamsCom.pdf"

gitleaks detect --source . --log-opts="--all" --report-path ../gitleaks-after.json

# 4. Only after review and team agreement:
git push --force --mirror
# Repeat for https://github.com/Clyptus-Soft/SAP_BRIM_Ai. Ask GitHub support to purge cached views and
# dangling objects, and ask every collaborator to re-clone.
```

## 3. Licensing decision for the knowledge base (F-01)

Decide whether the project may store, serve to all registered users, and send to external LLM providers:
SAP Education handbooks (`BR234_*`, `BR245_*`, `BRIM_AC240_*`), `SAP BRIM Press Book 2nd Edition - Expertsoft.pdf`,
and `E_Book_ABAP_RESTful_Programming_Model_2020_Rel_NW_wwwERPExamsCom.pdf`.
If not: remove them from the served corpus (admin portal → delete) and from git history (item 2, step 3).
No file in `knowledge_base/` was deleted by this remediation.

## 4. Approved LLM providers and data-retention terms (F-05)

- Which providers may receive KB excerpts, chat history, uploaded file text and screenshots?
- Are signed zero-retention / no-training terms required (Azure OpenAI, Vertex AI, Anthropic, SAP AI Core)?
- Free-tier models (OpenRouter `:free`, free vision models) must stay disabled unless explicitly approved.
- `LLM_ALLOWED_PROVIDERS` now defaults to empty (cloud inference off); explicitly list approved providers before enabling external text egress. Regex PII redaction applies to text prompts only.
- `VISION_EXTERNAL_ENABLED` defaults to false because screenshots cannot use the text redaction hook. If enabling it, approve sending unredacted image pixels to the configured provider and review its retention terms.

## 5. Product decisions taken with a safe default (flag)

| Decision | Flag (default) | Introduced in |
|---|---|---|
| Answer "simple" SAP questions from model memory without retrieval? Such answers are always labelled unverified. | `SIMPLE_SAP_FAST_PATH_ENABLED` (`false`) | F-04 |
| Let weak document evidence trigger the GitHub/web comparator automatically? Now requires `REPO_SOURCE_ENABLED` or `MULTI_SOURCE_COMPARE_ENABLED`. | `AUTO_MULTI_SOURCE_FALLBACK` (`true`, inert unless a source flag is on) | F-49 |
| An answer stream that ends before verification completes is no longer saved. Users re-ask instead of seeing a partial unverified answer. | — | F-03 |
| Allow public self-registration at all? If yes, require an invite code? **Decided by the owner 2026-11-04:** individuals sign up directly with no code (8+ character password); only organisations use Super-Admin activation codes. `INVITE_CODE` was removed. Open follow-ups: no e-mail verification (KNOWN_ISSUES AUTH-001) and no per-user daily budget (F-21). | `REGISTRATION_ENABLED` (`true`; `false` closes sign-up) | F-01 |
| Access-group model: which groups exist, which users belong to them, which documents are restricted (e.g. the licensed handbooks to a "licensed" group). Everything starts in group `all`. | `DEFAULT_ACCESS_GROUP` (`all`); admin endpoints `PATCH /api/admin/users/{id}/groups`, `PATCH /api/documents/{id}/access` | F-01 |
| Include project-authored/self-authored documents as evidence? Default excludes them before ranking; an admin must classify a document or explicitly opt in. | `INCLUDE_SELF_AUTHORED_DOCS` (`false`); `PATCH /api/documents/{id}/tier` | F-08/F-53
| Attachment text retention period? Default is one year from the parent message; cleanup is dry-run unless `--apply` is supplied. | `ATTACHMENT_RETENTION_DAYS` (`365`; `0` disables) | F-31
| Registration still answers 201 (new) vs 400 (existing email). Hiding that completely needs an e-mail verification flow (always "check your inbox"). | — | F-39 |
| Refresh tokens: not added. With 15-minute access tokens users sign in again after expiry; the frontend now redirects cleanly (F-33). Plan refresh tokens together with item 6. | — | F-33 |
| Keep JWTs in `localStorage` until the BFF/cookie migration (item 6). A strict CSP now limits XSS impact. | — | F-26 |

## 6. SSO and BFF/HttpOnly-cookie migration plan (F-26, F-33, report §8)

1. Pick the identity provider: Microsoft Entra ID (if staff use Microsoft 365) or Keycloak (self-hosted).
2. Add a small backend-for-frontend (BFF) route set in FastAPI (`/bff/login`, `/bff/callback`, `/bff/logout`) using OIDC
   Authorization Code + PKCE. Store the session server-side (Redis) and give the browser only an
   `HttpOnly; Secure; SameSite=Strict` session cookie. Add a CSRF token header for state-changing requests.
3. Issue short-lived access tokens server-side; refresh them inside the BFF with rotating refresh tokens stored
   server-side, revoked on logout and on reuse detection.
4. Map IdP group claims to `users.groups` on each login (F-01 access groups).
5. Remove the `Authorization: Bearer` + `localStorage` path from the SPA; serve the SPA and API from the same
   origin so the cookie is first-party. Remove the password login and `REGISTRATION_ENABLED`, and put MFA in the IdP.

## 7. Before the first start of this branch against the real database

- [ ] Take and verify a backup first:
      `docker exec sap_assistant_postgres pg_dump -U postgres -d sap_brim_ai -Fc -f /tmp/sap_brim_ai.dump` and copy it out.
- On startup Alembic stamps the existing schema (baseline `0001`, no data changes), then adds `users.groups`,
  `documents.allowed_groups` (default `{all}`) and the `audit_events` table (`0002`, `0003`). All are additive.
- Production: set `AUTO_MIGRATE=false` and run `py -3.12 -m alembic -c backend/alembic.ini upgrade head` as a deploy step.
- Existing access tokens lack the new `iss`/`aud` claims (F-40): every user signs in once more.
- Production with several API workers: set `REDIS_URL` and `pip install "limits[redis]"` so rate limits and lockout
  are shared (otherwise they are per process; startup logs a warning). Not verified here: no Redis was available.
- Behind a reverse proxy: set `TRUSTED_PROXIES` to the proxy's IP(s), so client IPs and `https` are taken from its headers.
- A scratch database `sap_brim_ai_empty_test` (pgvector only, no tables) was created on the local Docker
  PostgreSQL for the fresh-build migration test. Remove it if unwanted:
  `docker exec sap_assistant_postgres dropdb -U postgres sap_brim_ai_empty_test`.

## 9. Re-chunk and re-embed the corpus (F-15)

- [ ] F-07 rollout: migration `0005_embedding_provenance` labels existing document/message vectors `legacy-unknown`; dense retrieval excludes vectors whose model provenance cannot be proven, while lexical/identifier search remains available. After verifying a database backup and the configured `EMBEDDING_MODEL`/`EMBEDDING_VERSION`, re-embed the canonical knowledge base with `py -3.12 backend/app/ingestion/ingest.py`. The ingestion script detects incompatible provenance and rebuilds unchanged documents; review its output and verify retrieval before production rollout. No corpus re-embedding has been run by this remediation.
- [ ] After the token-based chunking and model-limit measurement scripts are implemented and reviewed, run the measurement command first, inspect its over-limit percentage, then run the documented re-chunk/re-embed command against the corpus. No corpus re-embedding has been run by this remediation.
- [ ] Confirm a verified database backup exists before the re-embed operation; it replaces stored chunk records and embeddings.

## 10. Own the human-authored golden Q&A set (F-50)

- [ ] Assign an SAP BRIM consultant/SME to author and review `backend/evaluation/golden/template.json` using `backend/evaluation/golden/schema.json`. The template contains placeholders only; no gold answers or passages have been invented.


## 11. Review project-authored generated PDFs (F-08/F-54)

The generator labels were removed and retrieval excludes records classified `self_authored` by default. These existing files were not deleted. Have an SAP BRIM reviewer validate each document before an administrator changes its tier using `PATCH /api/documents/{id}/tier`:

Generated by `backend/generate_brim_master_docs.py`:
- `knowledge_base/documents/SAP_BRIM_FICA_Contract_Accounts_Master_Handbook.pdf`
- `knowledge_base/documents/SAP_BRIM_Convergent_Invoicing_CIT_BIT_Master_Architecture.pdf`
- `knowledge_base/documents/SAP_BRIM_SOM_Subscription_Order_Management_Master_Guide.pdf`
- `knowledge_base/documents/SAP_BRIM_End_to_End_Integration_and_SPRO_Cookbook.pdf`

Generated by `backend/generate_sap_docs.py`:
- `knowledge_base/documents/SAP_MM_Materials_Management_Manual.pdf`
- `knowledge_base/documents/SAP_FI_Financial_Accounting_Guide.pdf`
- `knowledge_base/documents/SAP_CO_Controlling_Configuration_Handbook.pdf`
- `knowledge_base/documents/SAP_SD_Sales_and_Distribution_Handbook.pdf`
- `knowledge_base/documents/SAP_PP_Production_Planning_Reference.pdf`
- `knowledge_base/documents/SAP_HCM_Human_Capital_Management_Guide.pdf`
- `knowledge_base/documents/SAP_S4HANA_Architecture_and_Migration_Guide.pdf`
- `knowledge_base/documents/SAP_Fiori_Design_and_Deployment_Manual.pdf`
- `knowledge_base/documents/SAP_ABAP_Development_and_RAP_Standards.pdf`
- `knowledge_base/documents/SAP_Basis_Administration_and_Security_Handbook.pdf`

F-54 also identifies ten off-domain orphan index records. This remediation did not delete PDF files or purge database records; inventory and approve those records before any cleanup.

## Production go-live actions (deployment audit, 2026-11-11)

Code can't do these; the repo now has scripts and checks for everything else (`DEPLOYMENT.md`, `scripts/production/`).

- [ ] Rotate the Groq, Gemini, OpenRouter and Tavily keys, `JWT_SECRET`, `ADMIN_API_KEY` and admin passwords (item 1 above), then run `python scripts/security/check_rotation.py`.
- [ ] Move the project out of OneDrive (or exclude `.env*` from sync) and remove old copies of `.env` from other places. The two `.env.bak` files were deleted on 2026-11-11.
- [ ] Run the history rewrite in item 2 (the old admin password text is still in earlier commits and on the public `target` remote).
- [ ] On the server set `BACKUP_UPLOAD_CMD` so backups leave the machine, run `scripts/production/install-backup-cron.sh`, and run `scripts/production/restore-check.sh` once.
- [ ] Add an uptime monitor on `https://<domain>/health`.
- [ ] Free disk space (about 15 GB), build once with `scripts/production/deploy.sh`, and watch the first HTTPS certificate issuance (the web container now drops all capabilities except port binding).
- [ ] Push the branch and confirm the new `.github/workflows/ci.yml` goes green.
