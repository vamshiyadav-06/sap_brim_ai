from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, EmailStr, Field, ConfigDict, field_validator

class UserRegister(BaseModel):
    email: EmailStr
    name: str = Field(..., min_length=2, max_length=100)
    # Individual accounts: 8 characters minimum (owner decision 2026-11-04). Organisation accounts and
    # password changes keep 12 (schemas/organisation.py, ChangePasswordRequest).
    password: str = Field(..., min_length=8, max_length=72)

    @field_validator("password")
    @classmethod
    def bcrypt_byte_limit(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password must not exceed 72 UTF-8 bytes")
        return value

class UserLogin(BaseModel):
    email: EmailStr
    password: str
    unlock_key: Optional[str] = Field(None, min_length=8, max_length=128)

def check_password_bytes(value: str) -> str:
    """bcrypt only reads the first 72 bytes; longer passwords are rejected instead of truncated."""
    if len(value.encode("utf-8")) > 72:
        raise ValueError("Password must not exceed 72 UTF-8 bytes")
    return value


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=200)
    new_password: str = Field(..., min_length=12, max_length=72)

    @field_validator("new_password")
    @classmethod
    def bcrypt_byte_limit(cls, value: str) -> str:
        return check_password_bytes(value)


class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    is_admin: bool
    role: str = "consultant"
    groups: List[str] = []
    created_at: datetime
    # Organisation Portal (docs/ORGANISATION_PORTAL.md).
    account_type: str = "individual"
    organization_id: Optional[str] = None
    organization_name: Optional[str] = None
    org_role: Optional[str] = None
    is_active: bool = True
    must_change_password: bool = False
    # Free tokens left (individual accounts); see services/token_wallet.py.
    token_balance: float = 0.0
    token_refill_at: Optional[datetime] = None
    preferred_language: str = "en"
    question_language: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

    # A user object built in memory has no column defaults until it is flushed.
    @field_validator("account_type", mode="before")
    @classmethod
    def _default_account_type(cls, value):
        return value or "individual"

    @field_validator("is_active", mode="before")
    @classmethod
    def _default_is_active(cls, value):
        return True if value is None else value

    @field_validator("must_change_password", mode="before")
    @classmethod
    def _default_must_change_password(cls, value):
        return False if value is None else value

class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse
