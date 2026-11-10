import logging
from pathlib import Path
from typing import Generator
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, OperationalError
from sqlalchemy.orm import Session, sessionmaker, declarative_base
from backend.app.config import settings

logger = logging.getLogger(__name__)

Base = declarative_base()

NUL = "\x00"
NUL_REPLACEMENT = "�"


class DatabaseConnectionError(RuntimeError):
    """PostgreSQL is unreachable or misconfigured. There is no fallback database."""


def describe_target(url: str) -> str:
    """host:port/database without credentials, safe for logs and error messages."""
    try:
        u = make_url(url)
        return f"{u.host}:{u.port or 5432}/{u.database}"
    except ArgumentError:
        return "<unparseable DATABASE_URL>"


def create_db_engine(url: str = None):
    """Creates the PostgreSQL engine. Rejects any non-PostgreSQL URL."""
    url = url or settings.DATABASE_URL
    if not url:
        raise DatabaseConnectionError(
            "DATABASE_URL is not set. Configure it in the repository-root .env, e.g. "
            "postgresql://sap_brim_ai:<password>@127.0.0.1:5434/sap_brim_ai"
        )
    try:
        parsed = make_url(url)
    except ArgumentError as exc:
        raise DatabaseConnectionError(f"DATABASE_URL is not a valid database URL: {exc}") from exc
    if parsed.get_backend_name() != "postgresql":
        raise DatabaseConnectionError(
            f"DATABASE_URL must point to PostgreSQL + pgvector (got '{parsed.get_backend_name()}'). "
            "PostgreSQL is the only supported database."
        )
    if parsed.drivername == "postgresql":
        # SQLAlchemy 2.1 defaults bare postgresql:// to psycopg 3; this project ships psycopg2.
        parsed = parsed.set(drivername="postgresql+psycopg2")
    return create_engine(
        parsed,
        pool_pre_ping=True,
        pool_size=settings.DATABASE_POOL_SIZE,
        max_overflow=settings.DATABASE_MAX_OVERFLOW,
        pool_timeout=settings.DATABASE_POOL_TIMEOUT,
        pool_recycle=1800,
        # SQL errors and engine logs never carry bound values (password hashes, e-mails, chat text).
        hide_parameters=True,
        connect_args={
            "connect_timeout": settings.DATABASE_CONNECT_TIMEOUT,
            # Naive DateTime columns hold UTC, matching the values written by the app.
            "options": "-c timezone=UTC",
        },
    )


engine = create_db_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@event.listens_for(Session, "before_flush")
def _replace_nul_characters(session, flush_context, instances):
    """PostgreSQL text cannot store NUL (0x00); PDF extraction emits it for unmapped icon glyphs.

    Replaced with U+FFFD, the same rule applied to the three chunks migrated from SQLite.
    Only already-loaded values are inspected, so no lazy loads happen during flush.
    """
    for obj in list(session.new) + list(session.dirty):
        state = inspect(obj)
        for attr in state.mapper.column_attrs:
            value = state.dict.get(attr.key)
            if isinstance(value, str) and NUL in value:
                setattr(obj, attr.key, value.replace(NUL, NUL_REPLACEMENT))
                logger.info("Replaced %d NUL character(s) in %s.%s before writing to PostgreSQL",
                            value.count(NUL), state.mapper.class_.__name__, attr.key)


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_database_connection() -> None:
    """Raises DatabaseConnectionError with an actionable message if PostgreSQL is unreachable."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        raise DatabaseConnectionError(
            f"Cannot connect to PostgreSQL at {describe_target(str(engine.url))}. "
            "Start it with `docker compose up -d postgres` and check DATABASE_URL in the repository-root .env. "
            f"Driver error: {str(exc.orig).strip()}"
        ) from exc


def create_search_indexes(conn) -> None:
    """pgvector HNSW (cosine) and GIN full-text indexes on document_chunks."""
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding_hnsw "
        "ON document_chunks USING hnsw (embedding vector_cosine_ops)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_document_chunks_fts "
        "ON document_chunks USING gin (to_tsvector('english', chunk_text))"
    ))


def ensure_chat_memory_columns(conn, chat_columns=None) -> None:
    """Apply the existing additive runtime migration convention for chat memory."""
    if chat_columns is None:
        chat_columns = {c["name"] for c in inspect(conn).get_columns("chats")}
    if "summary" not in chat_columns:
        conn.execute(text("ALTER TABLE chats ADD COLUMN summary TEXT"))
    if "summary_through_created_at" not in chat_columns:
        conn.execute(text("ALTER TABLE chats ADD COLUMN summary_through_created_at TIMESTAMP"))
    if "summary_through_message_id" not in chat_columns:
        conn.execute(text("ALTER TABLE chats ADD COLUMN summary_through_message_id VARCHAR(36)"))


class SchemaOutOfDateError(RuntimeError):
    """The database is behind the application's migrations and AUTO_MIGRATE is off."""


def _alembic_config():
    from alembic.config import Config
    backend_dir = Path(__file__).resolve().parent.parent
    cfg = Config(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "migrations"))
    return cfg


def run_migrations(connection=None) -> None:
    """Apply every pending Alembic migration (AUDIT_REPORT F-29). Safe to run from several workers:
    migrations take a PostgreSQL advisory lock and are idempotent."""
    from alembic import command
    cfg = _alembic_config()
    if connection is not None:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
        return
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


def schema_revision_status():
    """(current revision, head revision) of the connected database."""
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    return current, head


def init_db():
    from backend.app.models import user, chat, message, document, chunk, citation, web_source, web_image, uploaded_attachment, repo_chunk, web_doc_chunk, shared_prompt, audit_event, organization  # noqa: F401
    check_database_connection()

    if settings.AUTO_MIGRATE:
        run_migrations()
    else:
        current, head = schema_revision_status()
        if current != head:
            raise SchemaOutOfDateError(
                f"Database schema revision is {current or 'none'} but the application needs {head}. "
                "Run: py -3.12 -m alembic -c backend/alembic.ini upgrade head"
            )
    logger.info("Database schema is at the latest migration on %s.", describe_target(str(engine.url)))

    seed_super_admin()


def seed_super_admin():
    """Create the super admin named in .env. An existing account keeps its password."""
    # Login looks accounts up by the lower-cased address, so a mixed-case one could never sign in.
    admin_email = settings.SUPER_ADMIN_EMAIL.strip().lower()
    if not admin_email:
        logger.info("SUPER_ADMIN_EMAIL not set; no super admin account seeded.")
        return
    try:
        from backend.app.models.user import User
        from backend.app.security.auth import hash_password
        with SessionLocal() as db:
            admin_user = db.query(User).filter(User.email == admin_email).first()
            if not admin_user:
                db.add(User(
                    email=admin_email,
                    name="Super Admin",
                    password_hash=hash_password(settings.SUPER_ADMIN_PASSWORD),
                    is_admin=True,
                    role="admin",
                ))
                db.commit()
                logger.info("Super admin account created.")
            else:
                logger.warning("Configured super admin email already exists; operator must provision admin rights explicitly.")
    except Exception as e:
        logger.warning(f"Could not seed super admin user: {e}")
