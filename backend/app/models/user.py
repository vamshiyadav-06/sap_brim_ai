import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Boolean, DateTime, Integer, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import relationship
from backend.app.config import settings
from backend.app.database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String(255), unique=True, index=True, nullable=False)
    name = Column(String(255), nullable=False)
    password_hash = Column(String(255), nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    role = Column(String(50), default="consultant", nullable=False)
    token_version = Column(Integer, default=0, nullable=False)
    # Access groups (AUDIT_REPORT F-01): retrieval only uses documents sharing one of these groups.
    groups = Column(ARRAY(String(100)), nullable=False, default=lambda: ["all"], server_default=text("'{all}'"))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    # Organisation Portal (revision 0006_organisations, docs/ORGANISATION_PORTAL.md).
    # account_type: individual | organisation. org_role: owner | admin | member (organisation accounts only).
    account_type = Column(String(20), nullable=False, default="individual", server_default="individual")
    # The foreign key to organizations.id (ON DELETE SET NULL) is created by migration 0006 only: revision
    # 0001 builds this table from the current model on an empty database, before organizations exists.
    organization_id = Column(String(36), nullable=True, index=True)
    org_role = Column(String(20), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    must_change_password = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    last_login_at = Column(DateTime, nullable=True)
    failed_login_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    login_locked_at = Column(DateTime, nullable=True)
    login_unlock_key_hash = Column(String(64), nullable=True)
    login_unlock_key_expires_at = Column(DateTime, nullable=True)
    # Free token wallet in thousandths of a token (services/token_wallet.py); every account starts with 100.
    token_balance_milli = Column(Integer, nullable=False, default=lambda: settings.TOKEN_ALLOWANCE * 1000,
                                 server_default=text("100000"))
    # Language answers are written in (Settings); services/languages.py.
    preferred_language = Column(String(8), nullable=False, default="en", server_default=text("'en'"))
    # Language questions are written in (Settings); NULL = same as the answer language.
    question_language = Column(String(8), nullable=True)
    # When the wallet is next refilled to the allowance; NULL until the account is first touched.
    token_refill_at = Column(DateTime, nullable=True)

    chats = relationship("Chat", back_populates="user", cascade="all, delete-orphan")
    organization = relationship(
        "Organization",
        primaryjoin="foreign(User.organization_id) == Organization.id",
        viewonly=True,
        lazy="select",
    )

    @property
    def token_balance(self):
        return round((self.token_balance_milli or 0) / 1000, 2)

    @property
    def organization_name(self):
        return self.organization.name if self.organization_id and self.organization is not None else None
