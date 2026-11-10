import logging
import uuid
import hashlib
import hmac
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from backend.app.config import settings
from backend.app.database import get_db
from backend.app.models.user import User
from backend.app.models.chat import Chat
from backend.app.models.message import Message
from backend.app.models.shared_prompt import SharedPrompt
from backend.app.schemas.auth import ChangePasswordRequest, UserRegister, UserLogin, Token, UserResponse
from backend.app.schemas.message import TokenSavingsSummary
from backend.app.security.auth import (
    REFRESH_REQUEST_HEADER,
    account_is_usable,
    authenticate_user,
    clear_refresh_cookie,
    create_access_token,
    find_refresh_token,
    get_current_user,
    get_current_user_allow_password_change,
    hash_password,
    oauth2_scheme,
    revoke_refresh_family,
    revoke_user_refresh_tokens,
    rotate_refresh_token,
    set_refresh_cookie,
    start_refresh_session,
    user_for_access_token,
    verify_password,
)
from backend.app.security.ratelimit import client_ip, rate_limiter
from backend.app.services import audit_log, languages, token_wallet
from backend.app.services.security_controls import registration_enabled
from backend.app.services.evidence_markers import strip_markers

router = APIRouter(prefix="/auth", tags=["Authentication"])
logger = logging.getLogger(__name__)

TOO_MANY_ATTEMPTS = "Too many login attempts. Try again later."
ADMIN_UNLOCK_REQUIRED = "This account is locked after repeated failed sign-in attempts. Ask an administrator for a one-time unlock key."
INVALID_LOGIN = "Invalid email or password."
# Shown only after a correct password (docs/ORGANISATION_PORTAL.md, "Authentication and account state").
ORGANISATION_ACCOUNT_AT_INDIVIDUAL_LOGIN = "This is an organisation account. Sign in through the Organisation Portal."
INDIVIDUAL_ACCOUNT_AT_ORGANISATION_LOGIN = "Sign in with your individual account at /login."
ACCOUNT_NOT_ACTIVE = "This account is not active. Contact your administrator."
CURRENT_PASSWORD_INCORRECT = "The current password is incorrect."
PASSWORD_UNCHANGED = "The new password must be different from the current password."
REGISTRATION_FAILED = "Registration could not be completed."
# One message for every refresh failure; the reason goes to the log/audit only.
SESSION_ENDED = "Your session has ended. Please sign in again."
REFRESH_REJECTED = "Refresh request rejected."


def _guard_login(request: Request, email: str, db: Session, unlock_key: Optional[str] = None) -> Optional[User]:
    """Apply the per-IP limit and return a persistently locked account only for key verification."""
    if not rate_limiter.allow("login-ip", client_ip(request), settings.LOGIN_RATE_LIMIT_PER_MINUTE, 60):
        logger.warning("[AUTH] Login throttled: IP rate limit exceeded for client %s", client_ip(request))
        audit_log.record("login", "denied", request=request, target=email, details={"reason": "ip_rate_limit"})
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=TOO_MANY_ATTEMPTS)
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if user is not None and user.login_locked_at is not None:
        if not unlock_key:
            audit_log.record("login", "denied", request=request, principal=user, details={"reason": "admin_unlock_required"})
            raise HTTPException(status_code=423, detail=ADMIN_UNLOCK_REQUIRED)
        return user
    if rate_limiter.account_locked(email):
        logger.warning("[AUTH] Login throttled: account locked for email %s", email)
        audit_log.record("login", "denied", request=request, target=email, details={"reason": "account_lockout"})
        detail = INVALID_LOGIN if unlock_key else ADMIN_UNLOCK_REQUIRED
        raise HTTPException(status_code=423 if not unlock_key else status.HTTP_401_UNAUTHORIZED, detail=detail)
    return None


def _record_login_failure(db: Session, email: str) -> bool:
    """Persist failures for known accounts so a lock survives restarts and worker changes."""
    normalized = email.strip().lower()
    user = db.query(User).filter(User.email == normalized).with_for_update().first()
    if user is not None and user.login_locked_at is None:
        user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
        if user.failed_login_attempts >= max(1, settings.LOGIN_MAX_FAILURES):
            user.login_locked_at = datetime.now(timezone.utc)
            user.login_unlock_key_hash = None
            user.login_unlock_key_expires_at = None
            audit_log.record("login_account_locked", "denied", target=user.id, details={"reason": "failed_password_limit"})
        db.commit()
    rate_limiter.record_login_failure(normalized)
    return bool((user is not None and user.login_locked_at is not None) or rate_limiter.account_locked(normalized))


def _verify_and_consume_unlock_key(
    db: Session,
    locked_user: User,
    password: str,
    unlock_key: str,
    *,
    require_admin: bool = False,
) -> Optional[User]:
    """Consume a valid, current key under a row lock, once, for the intended account only."""
    user = db.query(User).filter(User.id == locked_user.id).with_for_update().first()
    if user is None or user.login_locked_at is None:
        return None
    stored_hash = user.login_unlock_key_hash or ""
    provided_hash = hashlib.sha256(unlock_key.encode("utf-8")).hexdigest()
    expiry = user.login_unlock_key_expires_at
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    valid = bool(
        stored_hash
        and expiry
        and expiry > datetime.now(timezone.utc)
        and hmac.compare_digest(stored_hash, provided_hash)
        and verify_password(password, user.password_hash)
        and account_is_usable(user)
        and (not require_admin or user.is_admin)
    )
    if not valid:
        return None
    user.failed_login_attempts = 0
    user.login_locked_at = None
    user.login_unlock_key_hash = None
    user.login_unlock_key_expires_at = None
    db.commit()
    rate_limiter.clear_login_failures(user.email)
    return user


def deny_sign_in(request: Request, action: str, user: User, reason: str, detail: str) -> None:
    """The password was correct but this account may not sign in here. Not a failed guess, so the
    lockout counter is cleared rather than increased."""
    rate_limiter.clear_login_failures(user.email)
    user.failed_login_attempts = 0
    user.login_locked_at = None
    user.login_unlock_key_hash = None
    user.login_unlock_key_expires_at = None
    logger.warning("security_event login_denied reason=%s", reason)
    audit_log.record(action, "denied", request=request, principal=user, details={"reason": reason})
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def require_usable_account(request: Request, action: str, user: User) -> None:
    if not account_is_usable(user):
        deny_sign_in(request, action, user, "account_not_active", ACCOUNT_NOT_ACTIVE)


def session_token(user: User) -> Token:
    """The JSON every sign-in, refresh and password change returns (the refresh token is only a cookie)."""
    access_token = create_access_token(data={"sub": user.id, "ver": user.token_version})
    return Token(access_token=access_token, token_type="bearer", user=UserResponse.model_validate(user))


def issue_login_token(request: Request, action: str, user: User, db: Session, response: Response) -> Token:
    """Successful sign-in: clear the lockout counter, record last_login_at, audit, start a refresh-token
    family (HttpOnly cookie on `response`) and issue an access token."""
    rate_limiter.clear_login_failures(user.email)
    user.failed_login_attempts = 0
    user.login_locked_at = None
    user.login_unlock_key_hash = None
    user.login_unlock_key_expires_at = None
    token_wallet.refill_if_due(db, user)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()  # expired attributes reload on access below
    start_refresh_session(db, user, response)
    audit_log.record(action, request=request, principal=user)
    logger.info("[AUTH] User authenticated successfully: %s (role=%s)", user.email, "admin" if user.is_admin else "user")
    return session_token(user)


def _email_registered(db: Session, email: str) -> bool:
    return db.query(User.id).filter(User.email == email).first() is not None


def _registration_rejected(request: Request) -> HTTPException:
    logger.warning("security_event registration_rejected reason=duplicate")
    audit_log.record("register", "failure", request=request, details={"reason": "duplicate"})
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=REGISTRATION_FAILED)


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(user_in: UserRegister, request: Request, db: Session = Depends(get_db)):
    # Individuals sign up directly, with no code; organisations use activation codes (AUDIT_REPORT F-01).
    # The only gate is the operator's emergency switch.
    if not registration_enabled(db):
        audit_log.record("register", "denied", request=request, details={"reason": "registration_disabled"})
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Sign-up is temporarily unavailable. Please try again later.")

    email = user_in.email.lower()
    already_registered = _email_registered(db, email)
    # Hash in both branches so a duplicate email is not revealed by a faster response (F-39).
    password_hash = hash_password(user_in.password)
    if already_registered:
        raise _registration_rejected(request)

    new_user = User(
        email=email,
        name=user_in.name.strip(),
        password_hash=password_hash,
        is_admin=False,
        role="consultant",
        groups=[settings.DEFAULT_ACCESS_GROUP],
        account_type="individual",
    )
    db.add(new_user)
    try:
        db.commit()
    except IntegrityError:  # the same e-mail address was registered concurrently
        db.rollback()
        raise _registration_rejected(request)
    db.refresh(new_user)
    audit_log.record("register", request=request, principal=new_user, target=new_user.id)

    # Sign-up only creates the account; the user signs in afterwards (owner decision 2026-11-04),
    # so the session starts at /login with its rate limit, lockout and audit trail.
    return UserResponse.model_validate(new_user)


@router.get("/registration-status")
def get_registration_status(db: Session = Depends(get_db)):
    """Public UI hint; `/register` independently enforces the same server-side setting."""
    return {"registration_enabled": registration_enabled(db)}

@router.post("/login", response_model=Token)
def login(login_data: UserLogin, request: Request, response: Response, db: Session = Depends(get_db)):
    locked_user = _guard_login(request, login_data.email, db, login_data.unlock_key)
    if locked_user is not None:
        unlocked = _verify_and_consume_unlock_key(db, locked_user, login_data.password, login_data.unlock_key or "")
        if unlocked is None:
            audit_log.record("login", "failure", request=request, principal=locked_user, details={"reason": "invalid_unlock_key_or_password"})
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_LOGIN)
        user = unlocked
    else:
        user = authenticate_user(db, login_data.email, login_data.password)
    if not user:
        locked = _record_login_failure(db, login_data.email)
        logger.warning("[AUTH] Login failed: invalid credentials")
        audit_log.record("login", "failure", request=request, target=login_data.email.lower())
        if locked:
            raise HTTPException(status_code=423, detail=ADMIN_UNLOCK_REQUIRED)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if getattr(user, "account_type", None) == "organisation":
        deny_sign_in(request, "login", user, "organisation_account", ORGANISATION_ACCOUNT_AT_INDIVIDUAL_LOGIN)
    require_usable_account(request, "login", user)
    return issue_login_token(request, "login", user, db, response)

@router.post("/admin-login", response_model=Token)
def admin_login(login_data: UserLogin, request: Request, response: Response, db: Session = Depends(get_db)):
    locked_user = _guard_login(request, login_data.email, db, login_data.unlock_key)
    if locked_user is not None:
        user = _verify_and_consume_unlock_key(
            db, locked_user, login_data.password, login_data.unlock_key or "", require_admin=True
        )
        if user is None:
            audit_log.record("admin_login", "failure", request=request, target=login_data.email.lower(),
                             details={"reason": "invalid_unlock_key_or_password"})
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_LOGIN)
    else:
        user = authenticate_user(db, login_data.email, login_data.password)
    if not user:
        locked = _record_login_failure(db, login_data.email)
        logger.warning("[AUTH] Admin login failed: invalid credentials or unauthorized user")
        audit_log.record("admin_login", "failure", request=request, target=login_data.email.lower())
        if locked:
            raise HTTPException(status_code=423, detail=ADMIN_UNLOCK_REQUIRED)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_admin:
        locked = _record_login_failure(db, login_data.email)
        logger.warning("[AUTH] Admin login failed: invalid credentials or unauthorized user")
        audit_log.record("admin_login", "denied", request=request, principal=user, details={"reason": "not_admin"})
        if locked:
            raise HTTPException(status_code=423, detail=ADMIN_UNLOCK_REQUIRED)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    require_usable_account(request, "admin_login", user)
    return issue_login_token(request, "admin_login", user, db, response)


class LanguageIn(BaseModel):
    language: str


@router.patch("/me/language", response_model=UserResponse)
def set_language(body: LanguageIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Choose the language answers are written in (and questions may be asked in, besides English)."""
    if body.language.strip().lower() not in languages.LANGUAGES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unsupported language.")
    current_user.preferred_language = languages.normalize(body.language)
    db.commit()
    db.refresh(current_user)
    return UserResponse.model_validate(current_user)


class QuestionLanguageIn(BaseModel):
    language: Optional[str] = None  # empty/null = same as the answer language


@router.patch("/me/question-language", response_model=UserResponse)
def set_question_language(body: QuestionLanguageIn, current_user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    """Choose the language questions are written in; empty means the same as the answer language."""
    code = (body.language or "").strip().lower()
    if code and code not in languages.LANGUAGES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unsupported language.")
    current_user.question_language = code or None
    db.commit()
    db.refresh(current_user)
    return UserResponse.model_validate(current_user)


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user_allow_password_change), db: Session = Depends(get_db)):
    token_wallet.refill_if_due(db, current_user)
    return UserResponse.model_validate(current_user)


def _session_ended(request: Request, reason: str) -> JSONResponse:
    """Every refresh failure: one generic 401 that also clears the cookie."""
    logger.warning("security_event refresh_rejected reason=%s", reason)
    if reason in ("reuse_detected", "session_revoked"):
        audit_log.record("token_refresh", "denied", request=request, details={"reason": reason})
    response = JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": SESSION_ENDED},
                            headers={"WWW-Authenticate": "Bearer"})
    clear_refresh_cookie(response)
    return response


@router.post("/refresh", response_model=Token,
             summary="Exchange the refresh-token cookie for a new access token (rotates the cookie)")
def refresh_session(request: Request, response: Response, db: Session = Depends(get_db)):
    """Reads the token from the HttpOnly cookie only, never from the body, query or headers. The
    X-Refresh-Request header is required as a CSRF defence (with SameSite=Strict on the cookie)."""
    if request.headers.get(REFRESH_REQUEST_HEADER) != "1":
        logger.warning("security_event refresh_rejected reason=missing_csrf_header")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=REFRESH_REJECTED)
    result = rotate_refresh_token(db, request.cookies.get(settings.REFRESH_COOKIE_NAME))
    if result.user is None:
        return _session_ended(request, result.reason or "unknown")
    set_refresh_cookie(response, result.raw, result.expires_at)
    return session_token(result.user)


@router.get("/me/token-savings", response_model=TokenSavingsSummary,
            summary="Estimated tokens saved across the authenticated user's answers")
def my_token_savings(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Sums the figures stored with each answer in the user's own chats. Answers without stored figures
    (older or non-streamed ones) are left out rather than estimated; an answer that saved nothing counts 0."""
    answers, used, baseline, saved = (
        db.query(
            func.count(Message.id),
            func.coalesce(func.sum(Message.tokens_used), 0),
            func.coalesce(func.sum(Message.tokens_baseline), 0),
            func.coalesce(func.sum(func.greatest(Message.tokens_baseline - Message.tokens_used, 0)), 0),
        )
        .join(Chat, Chat.id == Message.chat_id)
        .filter(
            Chat.user_id == current_user.id,
            Message.role == "assistant",
            Message.tokens_used.isnot(None),
            Message.tokens_baseline.isnot(None),
        )
        .one()
    )
    return TokenSavingsSummary(answers=answers, used=used, baseline=baseline, saved=saved)


@router.post("/change-password", response_model=Token)
def change_password(
    change: ChangePasswordRequest,
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user_allow_password_change),
    db: Session = Depends(get_db),
):
    """Set a new password, clear a pending forced change and revoke every existing token (access and
    refresh); the response is a new session with a new refresh cookie."""
    if not rate_limiter.allow("change-password", current_user.id, settings.LOGIN_RATE_LIMIT_PER_MINUTE, 60):
        logger.warning("security_event password_change_throttled")
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=TOO_MANY_ATTEMPTS)
    if not verify_password(change.current_password, current_user.password_hash):
        audit_log.record("password_change", "failure", request=request, principal=current_user,
                         details={"reason": "current_password"})
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=CURRENT_PASSWORD_INCORRECT)
    if verify_password(change.new_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=PASSWORD_UNCHANGED)
    current_user.password_hash = hash_password(change.new_password)
    current_user.must_change_password = False
    current_user.token_version += 1
    revoke_user_refresh_tokens(db, current_user.id)
    db.commit()
    db.refresh(current_user)
    start_refresh_session(db, current_user, response)
    audit_log.record("password_change", request=request, principal=current_user, target=current_user.id)
    return session_token(current_user)


@router.get("/me/export", summary="Export the authenticated user's account and conversation data")
def export_my_account(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Regular users get what the chat history shows them (api/messages.py get_messages): no knowledge-base
    # citations, web sources, image source pages or inline [E#] evidence markers.
    is_admin = bool(current_user.is_admin)
    chats = db.query(Chat).filter(Chat.user_id == current_user.id).order_by(Chat.created_at).all()
    message_ids = [message.id for chat in chats for message in chat.messages]
    shared_by_message = {}
    if message_ids:
        for shared in db.query(SharedPrompt).filter(SharedPrompt.message_id.in_(message_ids)).all():
            shared_by_message[shared.message_id] = {
                "created_at": shared.created_at,
                "revoked": shared.revoked,
                "content": shared.content,
                "answer": shared.answer if is_admin or shared.answer is None else strip_markers(shared.answer),
            }

    chat_exports = []
    for chat in chats:
        message_exports = []
        for message in chat.messages:
            message_exports.append({
                "id": message.id,
                "role": message.role,
                "content": message.content if is_admin else strip_markers(message.content or ""),
                "created_at": message.created_at,
                "feedback": message.feedback,
                "citations": [
                    {
                        "source_type": citation.source_type,
                        "document_name": citation.document_name,
                        "page_number": citation.page_number,
                        "section": citation.section,
                        "citation_text": citation.citation_text,
                    }
                    for citation in message.citations
                ] if is_admin else [],
                "web_sources": [
                    {"url": source.url, "title": source.title, "domain": source.domain, "snippet": source.snippet}
                    for source in message.web_sources
                ] if is_admin else [],
                "images": [
                    {
                        "url": image.url,
                        "description": image.description,
                        "alt_text": image.alt_text,
                        "source_url": image.source_url if is_admin else None,
                        "source_title": image.source_title if is_admin else None,
                    }
                    for image in message.images
                ],
                "attachments": [
                    {
                        "name": attachment.name,
                        "file_type": attachment.file_type,
                        "page_count": attachment.page_count,
                        "extracted_text": attachment.extracted_text,
                    }
                    for attachment in message.uploaded_attachments
                ],
                "shared_link": shared_by_message.get(message.id),
            })
        chat_exports.append({
            "id": chat.id,
            "title": chat.title,
            "created_at": chat.created_at,
            "updated_at": chat.updated_at,
            "summary": chat.summary,
            "messages": message_exports,
        })

    payload = {
        "export_version": 1,
        "exported_at": datetime.now(timezone.utc),
        "account": {
            "id": current_user.id,
            "email": current_user.email,
            "name": current_user.name,
            "created_at": current_user.created_at,
        },
        "chats": chat_exports,
    }
    audit_log.record("account_export", request=request, principal=current_user, target=current_user.id)
    return JSONResponse(
        content=jsonable_encoder(payload),
        headers={"Content-Disposition": f'attachment; filename="account-export-{uuid.uuid4().hex[:12]}.json"'},
    )


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT, summary="Delete the authenticated user's account and data")
def delete_my_account(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.is_admin:
        remaining_admins = db.query(User).filter(
            User.is_admin.is_(True), User.id != current_user.id
        ).count()
        if remaining_admins == 0:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="The final administrator account cannot be deleted.")
    # Deleting the owner would leave the organisation without anyone able to manage it.
    if getattr(current_user, "org_role", None) == "owner":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="The organisation owner account cannot be deleted. Ask Clyptus to "
                                   "deactivate the organisation instead.")

    account_id = current_user.id
    email = current_user.email
    actor_type = "admin" if current_user.is_admin else "user"
    db.delete(current_user)
    db.commit()
    rate_limiter.clear_login_failures(email)
    audit_log.record(
        "account_delete", request=request, actor_id=account_id, actor_type=actor_type, target=account_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, token: Optional[str] = Depends(oauth2_scheme),
           db: Session = Depends(get_db)):
    """End the account's sessions: every access token (token_version) and every refresh token, and clear
    the cookie. The access token may already have expired (it lives 15 minutes), so a valid refresh cookie
    identifies the account too; a stale cookie still has its own family revoked. 401 with neither."""
    user = user_for_access_token(token, db) if token else None
    row = find_refresh_token(db, request.cookies.get(settings.REFRESH_COOKIE_NAME))
    if user is None and row is not None and row.revoked_at is None:
        owner = db.query(User).filter(User.id == row.user_id).first()
        if owner is not None and owner.token_version == row.token_version:
            user = owner
    if user is None and row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials",
                            headers={"WWW-Authenticate": "Bearer"})
    if user is not None:
        user.token_version += 1
        revoke_user_refresh_tokens(db, user.id)
    else:
        revoke_refresh_family(db, row.family_id)
    db.commit()
    clear_refresh_cookie(response)
    if user is not None:
        audit_log.record("logout", request=request, principal=user)
    else:
        audit_log.record("logout", request=request, actor_id=row.user_id, actor_type="user",
                         details={"reason": "stale_session"})
