import json
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.config import settings
from backend.app.models.audit_event import AuditEvent
from backend.app.models.chunk import DocumentChunk
from backend.app.models.document import Document
from backend.app.models.security_setting import SecuritySetting
from backend.app.models.user import User
from backend.app.schemas.auth import UserResponse
from backend.app.security.access import normalize_groups
from backend.app.security.auth import get_current_admin_user, verify_admin_key_or_user
from backend.app.ingestion.ingest import run_ingestion
from backend.app.services import audit_log

router = APIRouter(prefix="/admin", tags=["Admin / Developer Ingestion"])


class GroupsUpdate(BaseModel):
    groups: List[str] = Field(..., min_length=1, max_length=50)


class RegistrationControlUpdate(BaseModel):
    registration_enabled: bool


@router.get("/security-controls")
def get_security_controls(admin: User = Depends(get_current_admin_user), db: Session = Depends(get_db)):
    """Current authentication controls; registration is persisted so it takes effect immediately."""
    saved = db.query(SecuritySetting).filter(SecuritySetting.id == 1).first()
    return {
        "registration_enabled": bool(saved.registration_enabled) if saved is not None else bool(settings.REGISTRATION_ENABLED),
        "login_max_failures": settings.LOGIN_MAX_FAILURES,
        "unlock_key_ttl_minutes": settings.LOGIN_UNLOCK_KEY_TTL_MINUTES,
    }


@router.put("/security-controls/registration")
def update_registration_control(
    update: RegistrationControlUpdate,
    request: Request,
    admin: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    saved = db.query(SecuritySetting).filter(SecuritySetting.id == 1).first()
    if saved is None:
        saved = SecuritySetting(id=1, registration_enabled=update.registration_enabled)
        db.add(saved)
    else:
        saved.registration_enabled = update.registration_enabled
    db.commit()
    audit_log.record(
        "registration_setting_change",
        request=request,
        principal=admin,
        details={"enabled": update.registration_enabled},
    )
    return {"registration_enabled": update.registration_enabled}


@router.get("/login-lockouts")
def list_login_lockouts(
    admin: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(User)
        .filter(User.login_locked_at.isnot(None))
        .order_by(User.login_locked_at.desc())
        .limit(200)
        .all()
    )
    return {
        "users": [
            {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "account_type": user.account_type,
                "locked_at": user.login_locked_at.isoformat() if user.login_locked_at else None,
                "key_expires_at": user.login_unlock_key_expires_at.isoformat() if user.login_unlock_key_expires_at else None,
            }
            for user in rows
        ]
    }


@router.post("/users/{user_id}/login-unlock-key")
def issue_login_unlock_key(
    user_id: str,
    request: Request,
    admin: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).with_for_update().first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    if user.login_locked_at is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This account is not locked.")

    key = secrets.token_urlsafe(24)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=max(1, settings.LOGIN_UNLOCK_KEY_TTL_MINUTES))
    user.login_unlock_key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    user.login_unlock_key_expires_at = expires_at
    db.commit()
    audit_log.record(
        "login_unlock_key_issue",
        request=request,
        principal=admin,
        target=user.id,
        details={"expires_at": expires_at.isoformat()},
    )
    return {"user_id": user.id, "email": user.email, "key": key, "expires_at": expires_at.isoformat()}


@router.post("/documents/ingest")
def trigger_ingestion(
    request: Request,
    principal=Depends(verify_admin_key_or_user),
    db: Session = Depends(get_db)
):
    """
    Developer-only ingestion endpoint.
    Scans backend/knowledge_base/documents/ for PDFs, extracts, chunks, embeds, and stores them.
    """
    audit_log.record("ingestion_trigger", request=request, principal=principal)
    results = run_ingestion(db)
    return {
        "status": "success",
        "message": "Ingestion process completed successfully.",
        "details": results
    }


@router.get("/users", response_model=List[UserResponse])
def list_users(
    search: Optional[str] = Query(None, max_length=255),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    admin: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """Accounts with their access groups (AUDIT_REPORT F-01)."""
    query = db.query(User)
    if search:
        query = query.filter(User.email.ilike(f"%{search.strip()}%"))
    users = query.order_by(User.created_at.desc()).offset(offset).limit(limit).all()
    return [UserResponse.model_validate(u) for u in users]


@router.patch("/users/{user_id}/groups", response_model=UserResponse)
def set_user_groups(
    user_id: str,
    update: GroupsUpdate,
    request: Request,
    admin: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """Replace a user's access groups; retrieval then only uses documents sharing one of them."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    groups = normalize_groups(update.groups)
    if not groups:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="At least one group is required")
    previous = list(user.groups or [])
    user.groups = groups
    db.commit()
    audit_log.record("user_groups_change", request=request, principal=admin, target=user_id,
                     details={"before": previous, "after": groups})
    db.refresh(user)
    return UserResponse.model_validate(user)


@router.get("/audit-events")
def list_audit_events(
    action: Optional[str] = Query(None, max_length=64),
    actor_id: Optional[str] = Query(None, max_length=36),
    outcome: Optional[str] = Query(None, max_length=20),
    since: Optional[datetime] = Query(None, description="ISO timestamp (UTC)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    admin: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """Security audit trail, newest first (AUDIT_REPORT F-25). Admin JWT only, not the X-Admin-Key."""
    query = db.query(AuditEvent)
    if action:
        query = query.filter(AuditEvent.action == action)
    if actor_id:
        query = query.filter(AuditEvent.actor_id == actor_id)
    if outcome:
        query = query.filter(AuditEvent.outcome == outcome)
    if since:
        query = query.filter(AuditEvent.created_at >= since.replace(tzinfo=None))
    total = query.count()
    rows = query.order_by(AuditEvent.created_at.desc()).offset(offset).limit(limit).all()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "events": [
            {
                "id": e.id,
                "created_at": e.created_at.isoformat(),
                "actor_id": e.actor_id,
                "actor_type": e.actor_type,
                "action": e.action,
                "target": e.target,
                "ip": e.ip,
                "user_agent": e.user_agent,
                "outcome": e.outcome,
                "details": json.loads(e.details) if e.details else None,
            }
            for e in rows
        ],
    }


@router.get("/system-health")
def system_health(admin: User = Depends(get_current_admin_user), db: Session = Depends(get_db)):
    """Corpus counts and model settings that /health no longer exposes publicly (AUDIT_REPORT F-42)."""
    return {
        "status": "healthy",
        "service": settings.PROJECT_NAME,
        "version": "1.0.0",
        "environment": settings.ENVIRONMENT,
        "database": {"engine": "postgresql+pgvector", "status": "connected"},
        "knowledge_base": {
            "documents_count": db.query(Document).count(),
            "chunks_count": db.query(DocumentChunk).count(),
            "model": settings.EMBEDDING_MODEL,
        },
    }
