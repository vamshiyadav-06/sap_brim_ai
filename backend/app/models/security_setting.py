from sqlalchemy import Boolean, Column, Integer, text

from backend.app.database import Base


class SecuritySetting(Base):
    """Database-backed global controls that administrators may change at runtime."""

    __tablename__ = "security_settings"

    id = Column(Integer, primary_key=True, default=1)
    registration_enabled = Column(Boolean, nullable=False, default=False, server_default=text("false"))
