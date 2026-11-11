import hashlib
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
BASE_DIR = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    # App
    PROJECT_NAME: str = "SAP Knowledge Assistant"
    API_V1_STR: str = "/api"
    DEBUG: bool = False

    # Database: PostgreSQL + pgvector (Docker) is the only supported database.
    # Required; there is deliberately no default and no fallback database.
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")
    DATABASE_CONNECT_TIMEOUT: int = int(os.getenv("DATABASE_CONNECT_TIMEOUT", "5"))
    DATABASE_POOL_SIZE: int = int(os.getenv("DATABASE_POOL_SIZE", "10"))
    DATABASE_MAX_OVERFLOW: int = int(os.getenv("DATABASE_MAX_OVERFLOW", "20"))
    # How long a request waits for a free pooled connection before failing with a clean 503 (SQLAlchemy's own
    # default is 30 s, long enough for every worker thread to pile up behind a stuck query).
    DATABASE_POOL_TIMEOUT: int = int(os.getenv("DATABASE_POOL_TIMEOUT", "10"))
    # Capacity (services/capacity.py). At most MAX_CONCURRENT_ANSWERS answers run at once (0 = no limit); up to
    # ANSWER_QUEUE_MAX more wait at most ANSWER_QUEUE_WAIT_SECONDS for a slot, the rest get an immediate 503.
    # One user may run MAX_CONCURRENT_ANSWERS_PER_USER at a time (0 = no per-user limit). Each running answer
    # can hold a pooled database connection, so keep MAX_CONCURRENT_ANSWERS + 6 <= pool size + overflow.
    MAX_CONCURRENT_ANSWERS: int = int(os.getenv("MAX_CONCURRENT_ANSWERS", "16"))
    ANSWER_QUEUE_MAX: int = int(os.getenv("ANSWER_QUEUE_MAX", "32"))
    ANSWER_QUEUE_WAIT_SECONDS: float = float(os.getenv("ANSWER_QUEUE_WAIT_SECONDS", "15"))
    MAX_CONCURRENT_ANSWERS_PER_USER: int = int(os.getenv("MAX_CONCURRENT_ANSWERS_PER_USER", "3"))
    # Threads available to synchronous endpoints (the framework default is 40).
    THREADPOOL_SIZE: int = int(os.getenv("THREADPOOL_SIZE", "64"))
    # On shutdown, wait this long for running answers to finish and be saved (keep below the container's
    # stop_grace_period).
    SHUTDOWN_DRAIN_SECONDS: float = float(os.getenv("SHUTDOWN_DRAIN_SECONDS", "25"))
    HEALTH_CHECK_TIMEOUT_SECONDS: float = float(os.getenv("HEALTH_CHECK_TIMEOUT_SECONDS", "3"))
    # Apply Alembic migrations at startup (AUDIT_REPORT F-29). Convenient for development; in production
    # set False and run `alembic -c backend/alembic.ini upgrade head` as a deploy step (startup then
    # refuses to run against an out-of-date schema).
    AUTO_MIGRATE: bool = os.getenv("AUTO_MIGRATE", "True").lower() in ("true", "1")

    # JWT Security. Required: no default, so tokens are never signed with a public value.
    JWT_SECRET: str = os.getenv("JWT_SECRET", "")
    JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")
    JWT_ISSUER: str = os.getenv("JWT_ISSUER", "sap-brim-assistant")
    JWT_AUDIENCE: str = os.getenv("JWT_AUDIENCE", "sap-brim-assistant-api")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    # Refresh tokens (httpOnly cookie, rotated on every use, hashed in the database). A session lasts at
    # most REFRESH_TOKEN_EXPIRE_DAYS; a rotated token presented again after the grace window revokes its
    # whole family (theft detection). The grace window covers two tabs refreshing at the same moment.
    REFRESH_TOKEN_EXPIRE_DAYS: int = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))
    REFRESH_TOKEN_REUSE_GRACE_SECONDS: int = int(os.getenv("REFRESH_TOKEN_REUSE_GRACE_SECONDS", "30"))
    REFRESH_COOKIE_NAME: str = os.getenv("REFRESH_COOKIE_NAME", "sap_brim_refresh")
    ALLOWED_ORIGINS: str = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")

    # Access control (AUDIT_REPORT F-01). Individual self-registration is closed by default; opening it
    # requires an explicit deployment setting. Organisations use Super-Admin activation codes.
    # Token wallet (services/token_wallet.py): each individual account gets this many tokens, refilled to it
    # every TOKEN_REFILL_DAYS days.
    TOKEN_ALLOWANCE: int = int(os.getenv("TOKEN_ALLOWANCE", "100"))
    TOKEN_REFILL_DAYS: int = int(os.getenv("TOKEN_REFILL_DAYS", "3"))
    REGISTRATION_ENABLED: bool = os.getenv("REGISTRATION_ENABLED", "False").lower() in ("true", "1")
    DEFAULT_ACCESS_GROUP: str = os.getenv("DEFAULT_ACCESS_GROUP", "all")

    # Rate limits and login lockout (AUDIT_REPORT F-06). REDIS_URL shares counters across workers;
    # without it limits are per-process. TRUSTED_PROXIES: comma-separated IPs of reverse proxies whose
    # X-Forwarded-For / X-Forwarded-Proto headers are believed (empty: none, use the TCP peer).
    RATE_LIMIT_ENABLED: bool = os.getenv("RATE_LIMIT_ENABLED", "True").lower() in ("true", "1")
    REDIS_URL: str = os.getenv("REDIS_URL", "")
    TRUSTED_PROXIES: str = os.getenv("TRUSTED_PROXIES", "")
    RATE_LIMIT_MAX_KEYS: int = int(os.getenv("RATE_LIMIT_MAX_KEYS", "50000"))
    AUTH_RATE_LIMIT_PER_MINUTE: int = int(os.getenv("AUTH_RATE_LIMIT_PER_MINUTE", "20"))
    LOGIN_RATE_LIMIT_PER_MINUTE: int = int(os.getenv("LOGIN_RATE_LIMIT_PER_MINUTE", "10"))
    LOGIN_MAX_FAILURES: int = int(os.getenv("LOGIN_MAX_FAILURES", "5"))
    LOGIN_LOCKOUT_WINDOW_SECONDS: int = int(os.getenv("LOGIN_LOCKOUT_WINDOW_SECONDS", "900"))
    LOGIN_UNLOCK_KEY_TTL_MINUTES: int = int(os.getenv("LOGIN_UNLOCK_KEY_TTL_MINUTES", "15"))
    ANSWER_RATE_LIMIT_PER_MINUTE: int = int(os.getenv("ANSWER_RATE_LIMIT_PER_MINUTE", "12"))
    EXTRACT_RATE_LIMIT_PER_MINUTE: int = int(os.getenv("EXTRACT_RATE_LIMIT_PER_MINUTE", "10"))
    MAX_UPLOAD_BYTES: int = int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
    MAX_TOTAL_UPLOAD_BYTES: int = int(os.getenv("MAX_TOTAL_UPLOAD_BYTES", str(100 * 1024 * 1024)))
    ALLOW_EXTENDED_UPLOAD_TYPES: bool = os.getenv("ALLOW_EXTENDED_UPLOAD_TYPES", "False").lower() in ("true", "1")
    MAX_PDF_PAGES: int = int(os.getenv("MAX_PDF_PAGES", "500"))
    MAX_REQUEST_BODY_BYTES: int = int(os.getenv("MAX_REQUEST_BODY_BYTES", str(12 * 1024 * 1024)))
    ATTACHMENT_RETENTION_DAYS: int = int(os.getenv("ATTACHMENT_RETENTION_DAYS", "365"))

    # Organisation Portal (docs/ORGANISATION_PORTAL.md). Activation codes are single-use and expire after
    # this many hours. Public organisation endpoints have their own per-IP budgets on top of the auth limit.
    ORG_ACTIVATION_CODE_TTL_HOURS: int = int(os.getenv("ORG_ACTIVATION_CODE_TTL_HOURS", "72"))
    ORG_ACTIVATION_RATE_LIMIT_PER_MINUTE: int = int(os.getenv("ORG_ACTIVATION_RATE_LIMIT_PER_MINUTE", "10"))
    ORG_REQUEST_RATE_LIMIT_PER_HOUR: int = int(os.getenv("ORG_REQUEST_RATE_LIMIT_PER_HOUR", "5"))
    ORG_MEMBER_CREATE_RATE_LIMIT_PER_HOUR: int = int(os.getenv("ORG_MEMBER_CREATE_RATE_LIMIT_PER_HOUR", "30"))

    # Admin Key for Developer Ingestion. Empty disables X-Admin-Key access.
    ADMIN_API_KEY: str = os.getenv("ADMIN_API_KEY", "")

    # Super admin created on first start; an existing account's password is never overwritten.
    SUPER_ADMIN_EMAIL: str = os.getenv("SUPER_ADMIN_EMAIL", "")
    SUPER_ADMIN_PASSWORD: str = os.getenv("SUPER_ADMIN_PASSWORD", "")

    # LLM Settings
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "groq")  # preferred approved cloud provider
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "")
    # User-authorized tier order. Set this explicitly to empty to disable cloud text generation.
    LLM_ALLOWED_PROVIDERS: str = os.getenv("LLM_ALLOWED_PROVIDERS", "groq,gemini,openrouter")
    LLM_ALLOW_FREE_TIER: bool = os.getenv("LLM_ALLOW_FREE_TIER", "False").lower() in ("true", "1")
    LLM_GROQ_MODELS: str = os.getenv("LLM_GROQ_MODELS", "qwen/qwen3.8-27b,openai/gpt-oss-120b,openai/gpt-oss-20b,allam-2-7b")
    LLM_GEMINI_MODELS: str = os.getenv("LLM_GEMINI_MODELS", "gemini-2.5-flash,gemini-3.8-flash")
    LLM_OPENROUTER_MODELS: str = os.getenv("LLM_OPENROUTER_MODELS", "liquid/lfm-2.5-2.6b:free,nvidia/nemotron-3.5-lightning:free")
    # Small models that answered most questions in the 2026-10-26 baseline once the larger ones were throttled.
    # They are skipped in the normal Groq -> Gemini -> OpenRouter pass and tried only when every other model
    # failed (still before the web-search fallback). Empty: every model is in the normal pass.
    LLM_LAST_RESORT_MODELS: str = os.getenv("LLM_LAST_RESORT_MODELS", "allam-2-7b,liquid/lfm-2.5-2.6b:free")
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    # Groq rejects a request whose prompt plus reserved output exceeds this. The free tier allows 8000 per model
    # (x-ratelimit-limit-tokens, measured 2026-11-04); a lower limit reported in a 413 error is learned per model.
    GROQ_TOKENS_PER_MINUTE: int = int(os.getenv("GROQ_TOKENS_PER_MINUTE", "8000"))
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
    # Up to three extra keys per provider, used in this order only when the key before them is rejected or
    # out of quota (see services/provider_keys.py). Leave a name empty to leave that slot unused; a key
    # added to .env takes effect on the next backend restart.
    GROQ_API_KEY_FALLBACK_1: str = os.getenv("GROQ_API_KEY_FALLBACK_1", "")
    GROQ_API_KEY_FALLBACK_2: str = os.getenv("GROQ_API_KEY_FALLBACK_2", "")
    GROQ_API_KEY_FALLBACK_3: str = os.getenv("GROQ_API_KEY_FALLBACK_3", "")
    GEMINI_API_KEY_FALLBACK_1: str = os.getenv("GEMINI_API_KEY_FALLBACK_1", "")
    GEMINI_API_KEY_FALLBACK_2: str = os.getenv("GEMINI_API_KEY_FALLBACK_2", "")
    GEMINI_API_KEY_FALLBACK_3: str = os.getenv("GEMINI_API_KEY_FALLBACK_3", "")
    OPENROUTER_API_KEY_FALLBACK_1: str = os.getenv("OPENROUTER_API_KEY_FALLBACK_1", "")
    OPENROUTER_API_KEY_FALLBACK_2: str = os.getenv("OPENROUTER_API_KEY_FALLBACK_2", "")
    OPENROUTER_API_KEY_FALLBACK_3: str = os.getenv("OPENROUTER_API_KEY_FALLBACK_3", "")
    # OpenAI-compatible endpoint used for OpenRouter. Only tests and the E2E stack point it elsewhere
    # (a local stub provider), so browser tests never make chargeable calls.
    OPENROUTER_BASE_URL: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    # Whole-request budget for answer generation (all providers, retries, repair and web fallback).
    # When it runs out the user gets the "unavailable" answer instead of a request that never ends.
    LLM_REQUEST_DEADLINE_SECONDS: float = float(os.getenv("LLM_REQUEST_DEADLINE_SECONDS", "120"))
    # Size caps for what is sent to providers (characters). A longer question is rejected with 422;
    # attachments and recalled history are trimmed to fit.
    MAX_MESSAGE_CHARS: int = int(os.getenv("MAX_MESSAGE_CHARS", "12000"))
    MAX_ATTACHMENT_PROMPT_CHARS: int = int(os.getenv("MAX_ATTACHMENT_PROMPT_CHARS", "24000"))
    MAX_PROMPT_CHARS: int = int(os.getenv("MAX_PROMPT_CHARS", "60000"))
    ALLOWED_LINK_HOSTS: str = os.getenv("ALLOWED_LINK_HOSTS", "help.sap.com,learning.sap.com,community.sap.com,support.sap.com")

    # Vision models that read chat image attachments (comma-separated, tried in order).
    # OCR-only reading is used when vision is disabled or every model fails.
    VISION_ENABLED: bool = os.getenv("VISION_ENABLED", "True").lower() in ("true", "1")
    # Screenshots can contain visible personal data and cannot be safely regex-redacted.
    VISION_EXTERNAL_ENABLED: bool = os.getenv("VISION_EXTERNAL_ENABLED", "False").lower() in ("true", "1")
    VISION_GROQ_MODELS: str = os.getenv("VISION_GROQ_MODELS", "qwen/qwen3.8-27b")
    VISION_OPENROUTER_MODELS: str = os.getenv(
        "VISION_OPENROUTER_MODELS", "nvidia/nemotron-3.5-super-vl:free,google/gemma-4-31b-it:free"
    )
    VISION_TIMEOUT_SEC: float = float(os.getenv("VISION_TIMEOUT_SEC", "45"))

    # Embedding & RAG parameters
    EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    EMBEDDING_VERSION: str = os.getenv("EMBEDDING_VERSION", "1")
    EMBEDDING_DIM: int = 384
    TOP_K: int = int(os.getenv("TOP_K", "50"))
    RERANK_TOP_K: int = int(os.getenv("RERANK_TOP_K", "4"))
    # Build the in-memory chunk index at startup and shortly after documents change, in a background
    # thread, instead of inside the next user request (about 10 s for 31k chunks).
    RETRIEVAL_CACHE_WARMUP: bool = os.getenv("RETRIEVAL_CACHE_WARMUP", "True").lower() in ("true", "1")
    SIMILARITY_THRESHOLD: float = float(os.getenv("SIMILARITY_THRESHOLD", "0.60"))
    GROUNDING_THRESHOLD: float = float(os.getenv("GROUNDING_THRESHOLD", "0.65"))

    # Evidence Confidence & Grounding Verification Settings (v2)
    INTERNAL_EVIDENCE_THRESHOLD: float = float(os.getenv("INTERNAL_EVIDENCE_THRESHOLD", "0.70"))
    # Search GitHub and approved SAP web sources whenever the best document match
    # falls below this calibrated relevance score.
    DOCUMENT_MATCH_THRESHOLD: float = float(os.getenv("DOCUMENT_MATCH_THRESHOLD", "0.80"))
    AUTO_MULTI_SOURCE_FALLBACK: bool = os.getenv("AUTO_MULTI_SOURCE_FALLBACK", "True").lower() in ("true", "1")
    ANSWER_GROUNDING_THRESHOLD: float = float(os.getenv("ANSWER_GROUNDING_THRESHOLD", "0.80"))
    MAX_GROUNDING_REPAIR_ATTEMPTS: int = int(os.getenv("MAX_GROUNDING_REPAIR_ATTEMPTS", "1"))
    FOLLOWUP_QUESTION_COUNT: int = int(os.getenv("FOLLOWUP_QUESTION_COUNT", "3"))
    TAVILY_MAX_RESULTS: int = int(os.getenv("TAVILY_MAX_RESULTS", "5"))
    TAVILY_MAX_IMAGES: int = int(os.getenv("TAVILY_MAX_IMAGES", "4"))

    # Answer Style Router: picks the answer layout (direct answer, explanation, comparison, steps,
    # troubleshooting, document research) before generation. Grounding rules are unaffected.
    ANSWER_STYLE_ROUTER_ENABLED: bool = os.getenv("ANSWER_STYLE_ROUTER_ENABLED", "True").lower() in ("true", "1")
    # One small LLM call, only for questions the rules cannot place.
    ANSWER_STYLE_LLM_CLASSIFIER_ENABLED: bool = os.getenv("ANSWER_STYLE_LLM_CLASSIFIER_ENABLED", "True").lower() in ("true", "1")
    ANSWER_STYLE_CLASSIFIER_MODEL: str = os.getenv("ANSWER_STYLE_CLASSIFIER_MODEL", "openai/gpt-oss-20b")
    ANSWER_STYLE_CLASSIFIER_TIMEOUT: float = float(os.getenv("ANSWER_STYLE_CLASSIFIER_TIMEOUT", "4.0"))
    # Documents the assistant creates on request (Word, PDF, Excel, PowerPoint, CSV, text, Markdown, HTML).
    # Files are stored for their owner only and deleted after the TTL; point the directory at a volume to keep
    # them across container restarts.
    DOCUMENT_GENERATION_ENABLED: bool = os.getenv("DOCUMENT_GENERATION_ENABLED", "True").lower() in ("true", "1")
    GENERATED_FILES_DIR: Path = Path(os.getenv("GENERATED_FILES_DIR", "") or (Path(tempfile.gettempdir()) / "clyptus_generated_files"))
    GENERATED_FILE_TTL_DAYS: int = int(os.getenv("GENERATED_FILE_TTL_DAYS", "14"))
    # Reference mode: retrieved passages are reference notes, not ground truth. The model answers as a senior SAP
    # architect, uses them for context, corrects them when they are wrong and fills gaps from its own knowledge;
    # claim verification is kept as an admin-only diagnostic and no longer deletes, repairs or flags the answer.
    # False restores strict evidence-only answering (verify -> repair -> guard -> notice).
    REFERENCE_MODE_ENABLED: bool = os.getenv("REFERENCE_MODE_ENABLED", "True").lower() in ("true", "1")
    # The first LLM call of every question: intent (SAP BRIM / follow-up / general / clarification) and
    # spelling correction by meaning, not keywords. Off or unavailable -> the older rule-based routing.
    INTENT_ANALYZER_ENABLED: bool = os.getenv("INTENT_ANALYZER_ENABLED", "True").lower() in ("true", "1")
    INTENT_ANALYZER_MODEL: str = os.getenv("INTENT_ANALYZER_MODEL", "") or os.getenv("ANSWER_STYLE_CLASSIFIER_MODEL", "openai/gpt-oss-20b")
    INTENT_ANALYZER_TIMEOUT: float = float(os.getenv("INTENT_ANALYZER_TIMEOUT", "6.0"))
    # SAP-only scope: a question the intent analysis judges to be unrelated to SAP is declined with a short
    # message (greetings and thanks are still answered). False lets the assistant answer any topic.
    SAP_ONLY_SCOPE: bool = os.getenv("SAP_ONLY_SCOPE", "True").lower() in ("true", "1")
    ANSWER_STYLE_DEFAULT: str = os.getenv("ANSWER_STYLE_DEFAULT", "technical_explanation")
    ANSWER_STYLE_DEFAULT_DETAIL: str = os.getenv("ANSWER_STYLE_DEFAULT_DETAIL", "balanced")
    ANSWER_STYLE_MAX_SECTIONS: int = int(os.getenv("ANSWER_STYLE_MAX_SECTIONS", "4"))

    # DECISION (HUMAN_ACTIONS.md): answer "simple" SAP questions from model memory without retrieval.
    # Off by default: every SAP question goes through retrieval and claim verification. When on, those
    # answers are reported as grounding_score=None / verification_status="unverified".
    SIMPLE_SAP_FAST_PATH_ENABLED: bool = os.getenv("SIMPLE_SAP_FAST_PATH_ENABLED", "False").lower() in ("true", "1")

    # Claim-level technical validation (new; additive to the existing grounding gate)
    TECHNICAL_CLAIM_VALIDATION_ENABLED: bool = os.getenv("TECHNICAL_CLAIM_VALIDATION_ENABLED", "True").lower() in ("true", "1")
    # Inline [E#] evidence markers next to claims (stripped for users who cannot see sources)
    INLINE_EVIDENCE_MARKERS_ENABLED: bool = os.getenv("INLINE_EVIDENCE_MARKERS_ENABLED", "True").lower() in ("true", "1")
    # Restriction/block scope classification and per-scope retrieval expansion
    RESTRICTION_SCOPE_ENABLED: bool = os.getenv("RESTRICTION_SCOPE_ENABLED", "True").lower() in ("true", "1")
    RESTRICTION_SCOPE_MAX_EXPANSIONS: int = int(os.getenv("RESTRICTION_SCOPE_MAX_EXPANSIONS", "5"))
    RESTRICTION_SCOPE_PASSAGES_PER_CATEGORY: int = int(os.getenv("RESTRICTION_SCOPE_PASSAGES_PER_CATEGORY", "1"))
    # Web evidence: extract claim-relevant passages from page content instead of snippets only
    WEB_RAW_CONTENT_ENABLED: bool = os.getenv("WEB_RAW_CONTENT_ENABLED", "True").lower() in ("true", "1")
    WEB_PASSAGE_MAX_CHARS: int = int(os.getenv("WEB_PASSAGE_MAX_CHARS", "1800"))
    # Confidence components (observable; overall score never overrides a failed critical claim)
    CONFIDENCE_WEIGHT_RETRIEVAL: float = float(os.getenv("CONFIDENCE_WEIGHT_RETRIEVAL", "0.15"))
    CONFIDENCE_WEIGHT_COVERAGE: float = float(os.getenv("CONFIDENCE_WEIGHT_COVERAGE", "0.20"))
    CONFIDENCE_WEIGHT_CLAIMS: float = float(os.getenv("CONFIDENCE_WEIGHT_CLAIMS", "0.35"))
    CONFIDENCE_WEIGHT_AUTHORITY: float = float(os.getenv("CONFIDENCE_WEIGHT_AUTHORITY", "0.10"))
    CONFIDENCE_WEIGHT_TECHNICAL: float = float(os.getenv("CONFIDENCE_WEIGHT_TECHNICAL", "0.20"))
    CRITICAL_FAILURE_CONFIDENCE_CAP: float = float(os.getenv("CRITICAL_FAILURE_CONFIDENCE_CAP", "0.49"))

    # Generation Contract & Context Packing Flags (Grounded RAG)
    GROUNDED_CONTRACT_ENABLED: bool = os.getenv("GROUNDED_CONTRACT_ENABLED", "True").lower() in ("true", "1")
    GROUNDED_GENERATOR_MODEL: str = os.getenv("GROUNDED_GENERATOR_MODEL", "qwen/qwen3.8-27b")
    GROUNDED_TEMPERATURE: float = float(os.getenv("GROUNDED_TEMPERATURE", "0.15"))
    ANSWER_TEMPERATURE: float = float(os.getenv("ANSWER_TEMPERATURE", "0.10"))
    CONTEXT_PACKING_TOKEN_LIMIT: int = int(os.getenv("CONTEXT_PACKING_TOKEN_LIMIT", "3000"))
    CONTEXT_BASE_TOKEN_BUDGET: int = int(os.getenv("CONTEXT_BASE_TOKEN_BUDGET", "1500"))
    CONTEXT_PER_MODULE_TOKEN_BUDGET: int = int(os.getenv("CONTEXT_PER_MODULE_TOKEN_BUDGET", "600"))
    RETRIEVAL_CALIBRATED_THRESHOLD: float = float(os.getenv("RETRIEVAL_CALIBRATED_THRESHOLD", "0.6761"))
    DEDUPE_SIMILARITY_THRESHOLD: float = float(os.getenv("DEDUPE_SIMILARITY_THRESHOLD", "0.85"))
    PROMPT_PATH_GROUNDED_ANSWER: Path = (BASE_DIR / "app" / "prompts" / "grounded_answer.txt")

    # Tier Ranking Weights (relevance * tier_weight)
    TIER_WEIGHT_OFFICIAL_SAP: float = float(os.getenv("TIER_WEIGHT_OFFICIAL_SAP", "1.0"))
    TIER_WEIGHT_BOOK: float = float(os.getenv("TIER_WEIGHT_BOOK", "0.85"))
    TIER_WEIGHT_OFFICIAL_WEB: float = float(os.getenv("TIER_WEIGHT_OFFICIAL_WEB", "0.80"))
    TIER_WEIGHT_FORUM_WEB: float = float(os.getenv("TIER_WEIGHT_FORUM_WEB", "0.50"))
    TIER_WEIGHT_SELF_AUTHORED: float = float(os.getenv("TIER_WEIGHT_SELF_AUTHORED", "0.60"))
    ENABLE_SENTENCE_COMPRESSION: bool = os.getenv("ENABLE_SENTENCE_COMPRESSION", "False").lower() in ("true", "1")

    # Phase 1 & Advanced Architecture Feature Flags (Default OFF)
    SHADOW_MODE_ENABLED: bool = os.getenv("SHADOW_MODE_ENABLED", "False").lower() in ("true", "1")
    EXCLUDE_OFF_DOMAIN_DOCS: bool = os.getenv("EXCLUDE_OFF_DOMAIN_DOCS", "False").lower() in ("true", "1")
    INCLUDE_SELF_AUTHORED_DOCS: bool = os.getenv("INCLUDE_SELF_AUTHORED_DOCS", "False").lower() in ("true", "1")
    USE_CORPUS_METADATA_V2: bool = os.getenv("USE_CORPUS_METADATA_V2", "False").lower() in ("true", "1")

    # GitHub Repository Retrieval & Multi-Source Comparison Feature Flags (Strictly Default FALSE)
    REPO_SOURCE_ENABLED: bool = os.getenv("REPO_SOURCE_ENABLED", "False").lower() in ("true", "1")
    MULTI_SOURCE_COMPARE_ENABLED: bool = os.getenv("MULTI_SOURCE_COMPARE_ENABLED", "False").lower() in ("true", "1")
    TIER_WEIGHT_GITHUB_REPO: float = float(os.getenv("TIER_WEIGHT_GITHUB_REPO", "0.80"))
    REPO_GATE_CALIBRATED_THRESHOLD: float = float(os.getenv("REPO_GATE_CALIBRATED_THRESHOLD", "0.65"))
    REPO_MIN_CHUNKS_THRESHOLD: int = int(os.getenv("REPO_MIN_CHUNKS_THRESHOLD", "10"))
    REPO_SNAPSHOTS_DIR: Path = BASE_DIR / "data" / "github_snapshots"

    # Extended Official SAP Web & Community Sources Feature Flags (Strictly Default FALSE)
    SAP_WEB_SOURCES_ENABLED: bool = os.getenv("SAP_WEB_SOURCES_ENABLED", "False").lower() in ("true", "1")
    TIER_WEIGHT_OFFICIAL_DEV_DOCS: float = float(os.getenv("TIER_WEIGHT_OFFICIAL_DEV_DOCS", "0.85"))
    TIER_WEIGHT_OFFICIAL_HELP_DOCS: float = float(os.getenv("TIER_WEIGHT_OFFICIAL_HELP_DOCS", "0.85"))
    TIER_WEIGHT_SAP_COMMUNITY: float = float(os.getenv("TIER_WEIGHT_SAP_COMMUNITY", "0.50"))
    WEB_DOC_SNAPSHOTS_DIR: Path = BASE_DIR / "data" / "web_doc_snapshots"

    # Rate Limiting & Backoff Settings
    RATE_LIMIT_MAX_RETRIES: int = int(os.getenv("RATE_LIMIT_MAX_RETRIES", "3"))
    RATE_LIMIT_BASE_BACKOFF_SEC: float = float(os.getenv("RATE_LIMIT_BASE_BACKOFF_SEC", "2.0"))

    # Tavily web fallback
    WEB_FALLBACK_ENABLED: bool = os.getenv("WEB_FALLBACK_ENABLED", "True").lower() in ("true", "1")
    WEB_SEARCH_API_KEY: str = os.getenv("WEB_SEARCH_API_KEY", "")
    # Per-call timeout for the web-search provider (Tavily). It runs inside user requests.
    WEB_SEARCH_TIMEOUT_SECONDS: float = float(os.getenv("WEB_SEARCH_TIMEOUT_SECONDS", "15"))

    # Canonical knowledge-base source directory (at repo root if present).
    # Keep exactly one source location so ingestion results can be reconciled with
    # the on-disk inventory and an operator never has to guess which folder is live.
    KNOWLEDGE_BASE_DIR: Path = (ROOT_DIR / "knowledge_base") if (ROOT_DIR / "knowledge_base").exists() else (BASE_DIR / "knowledge_base")
    DOCUMENTS_DIR: Path = (ROOT_DIR / "knowledge_base" / "documents") if (ROOT_DIR / "knowledge_base" / "documents").exists() else (BASE_DIR / "knowledge_base" / "documents")

    # One settings file for the whole project: the repository-root .env, which Docker Compose
    # also reads for docker-compose.yml. Template: .env.example.
    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        extra="allow"
    )

settings = Settings()

# SHA-256 of credentials that were committed to this repository (so anyone can read them). Only the hashes
# are kept here: the startup check refuses these values without publishing them a second time.
PUBLISHED_SECRET_HASHES = frozenset({
    "1259039c4b8b4ddd7d8acb33545d265964ff84ab57a8bf691e46dab0c7eefc18",
    "e28e503476b9a58e4293d119a4a3b4b9f55253eaae96e109d7917b05d56632d3",
    "9d360e6668c53d7abdeabb39e0bd2c9a926fba8d8ec2480460d561db35730f60",
})


def is_published_secret(value: str) -> bool:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest() in PUBLISHED_SECRET_HASHES


MIN_SECRET_LENGTH = 32
# Tokens are signed with the shared JWT_SECRET, so only HMAC algorithms make sense; never "none".
ALLOWED_JWT_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})
MIN_ADMIN_PASSWORD_LENGTH = 12
# Placeholder passwords in .env.example. Production refuses to start with them.
TEMPLATE_PLACEHOLDER_MARKERS = ("change-me", "change_me", "changeme")


def _is_template_placeholder(value: str) -> bool:
    lowered = (value or "").lower()
    return any(marker in lowered for marker in TEMPLATE_PLACEHOLDER_MARKERS)


def production_origin_errors(s: Settings = settings) -> list:
    """Production serves browsers from explicit public HTTPS origins only."""
    origins = [origin.strip() for origin in s.ALLOWED_ORIGINS.split(",") if origin.strip()]
    if not origins or "*" in s.ALLOWED_ORIGINS or any(
        "localhost" in origin or "127.0.0.1" in origin for origin in origins
    ):
        return ["Production requires explicit public ALLOWED_ORIGINS without wildcards or localhost entries."]
    return []


def security_settings_errors(s: Settings = settings) -> list:
    """Problems that make the configured secrets unsafe; the app refuses to start on any."""
    errors = []
    if s.JWT_ALGORITHM not in ALLOWED_JWT_ALGORITHMS:
        errors.append(f"JWT_ALGORITHM must be one of {', '.join(sorted(ALLOWED_JWT_ALGORITHMS))} (a shared-secret HMAC algorithm).")
    if not s.JWT_ISSUER.strip() or not s.JWT_AUDIENCE.strip():
        errors.append("JWT_ISSUER and JWT_AUDIENCE must not be empty.")
    if len(s.JWT_SECRET) < MIN_SECRET_LENGTH or is_published_secret(s.JWT_SECRET):
        errors.append(f"JWT_SECRET must be a private random value of at least {MIN_SECRET_LENGTH} characters.")
    if s.ADMIN_API_KEY and (len(s.ADMIN_API_KEY) < MIN_SECRET_LENGTH or is_published_secret(s.ADMIN_API_KEY)):
        errors.append(f"ADMIN_API_KEY must be empty or a private random value of at least {MIN_SECRET_LENGTH} characters.")
    if bool(s.SUPER_ADMIN_EMAIL.strip()) != bool(s.SUPER_ADMIN_PASSWORD):
        errors.append("Set both SUPER_ADMIN_EMAIL and SUPER_ADMIN_PASSWORD, or neither.")
    elif s.SUPER_ADMIN_PASSWORD and (
        len(s.SUPER_ADMIN_PASSWORD) < MIN_ADMIN_PASSWORD_LENGTH or is_published_secret(s.SUPER_ADMIN_PASSWORD)
    ):
        errors.append(f"SUPER_ADMIN_PASSWORD must be a private password of at least {MIN_ADMIN_PASSWORD_LENGTH} characters.")
    if s.ENVIRONMENT.lower() == "production":
        errors.extend(production_origin_errors(s))
        if _is_template_placeholder(urlparse(s.DATABASE_URL).password or ""):
            errors.append("DATABASE_URL still uses the template database password; set a private one (production).")
        if _is_template_placeholder(s.SUPER_ADMIN_PASSWORD):
            errors.append("SUPER_ADMIN_PASSWORD still uses a template value; set a private one (production).")
    return errors

