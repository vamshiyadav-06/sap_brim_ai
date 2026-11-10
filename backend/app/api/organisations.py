"""Organisation Portal API (docs/ORGANISATION_PORTAL.md): public request/activation/sign-in, the shared
knowledge hub for members and the organisation administrator endpoints.

Tenant isolation: the organisation is always the signed-in user's own (security/org_access.py); IDs of
another organisation's answers, comments or members return 404. Activation codes and temporary passwords
are never logged or written to the audit log.
"""

import logging
import uuid
from datetime import timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.api.auth import (
    INDIVIDUAL_ACCOUNT_AT_ORGANISATION_LOGIN,
    _guard_login,
    _record_login_failure,
    _verify_and_consume_unlock_key,
    ADMIN_UNLOCK_REQUIRED,
    INVALID_LOGIN,
    deny_sign_in,
    issue_login_token,
    require_usable_account,
)
from backend.app.config import settings
from backend.app.database import get_db
from backend.app.models.chat import Chat
from backend.app.models.message import Message
from backend.app.models.organization import (
    ACCOUNT_ORGANISATION,
    ORG_ROLE_ADMIN,
    ORG_ROLE_OWNER,
    ORG_STATUS_ACTIVE,
    ORG_STATUS_PENDING,
    ORG_STATUS_REQUESTED,
    OrgActivationCode,
    OrgAnswerComment,
    OrgAnswerLike,
    OrgSharedAnswer,
    Organization,
)
from backend.app.models.user import User
from backend.app.schemas.auth import Token, UserLogin
from backend.app.schemas.organisation import (
    ActivationCodeIn,
    ActivationCompleteIn,
    CategoryKey,
    CategoryUpdateIn,
    CommentIn,
    MemberCreateIn,
    MemberPasswordIn,
    MemberUpdateIn,
    OrganisationUpdateIn,
    OrgRequestIn,
    ShareAnswerIn,
)
from backend.app.security.auth import authenticate_user, hash_password
from backend.app.security.log_safety import privacy_fingerprint
from backend.app.security.org_access import (
    get_current_org_admin,
    get_current_org_member,
    is_org_manager,
    is_org_owner,
)
from backend.app.security.ratelimit import client_ip, rate_limiter
from backend.app.services import audit_log
from backend.app.services.evidence_markers import strip_markers
from backend.app.services.org_categoriser import CATEGORIES, categorise, category_label
from backend.app.services.org_credentials import (
    hash_activation_code,
    normalise_activation_code,
)
from backend.app.services.organisations import (
    escape_like,
    find_organisation_by_name,
    iso,
    member_count,
    unique_slug,
    utcnow,
)

router = APIRouter(prefix="/org", tags=["Organisation Portal"])
logger = logging.getLogger(__name__)

ACTIVATION_CODE_INVALID = "This activation code is not valid. Ask your Clyptus administrator for a new code."
TOO_MANY_ATTEMPTS = "Too many attempts. Try again later."
ANSWER_NOT_FOUND = "Shared answer not found"
COMMENT_NOT_FOUND = "Comment not found"
MEMBER_NOT_FOUND = "Member not found"
ANSWER_PREVIEW_CHARS = 280


# --------------------------------------------------------------------------------------------------
# Public endpoints
# --------------------------------------------------------------------------------------------------

def _limit(request: Request, scope: str, limit: int, window: int) -> None:
    if not rate_limiter.allow(scope, client_ip(request), limit, window):
        logger.warning("security_event rate_limit scope=%s", scope)
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=TOO_MANY_ATTEMPTS)


@router.post("/requests", status_code=status.HTTP_202_ACCEPTED)
def request_organisation(body: OrgRequestIn, request: Request, db: Session = Depends(get_db)):
    """Ask for an organisation workspace. The response is identical whether or not the name is taken."""
    _limit(request, "org-request", settings.ORG_REQUEST_RATE_LIMIT_PER_HOUR, 3600)
    duplicate = find_organisation_by_name(db, body.organisation_name) is not None
    if not duplicate:
        organisation = Organization(
            name=body.organisation_name,
            slug=unique_slug(db, body.organisation_name),
            contact_name=body.contact_name,
            contact_email=body.contact_email,
            request_message=(body.message or "").strip() or None,
            status=ORG_STATUS_REQUESTED,
        )
        db.add(organisation)
        try:
            db.commit()
        except IntegrityError:  # a concurrent request with the same name or slug
            db.rollback()
            duplicate = True
        else:
            audit_log.record("org_request", request=request, target=organisation.id)
    if duplicate:
        audit_log.record("org_request", "failure", request=request, details={"reason": "duplicate_name"})
    return {"status": "received"}


def _usable_code(code: Optional[OrgActivationCode]) -> Optional[str]:
    """None when the code can be used, otherwise the (internal, audited) reason."""
    if code is None:
        return "unknown_code"
    if code.revoked_at is not None:
        return "revoked"
    if code.used_at is not None:
        return "used"
    if code.expires_at <= utcnow():
        return "expired"
    return None


def _activation_failed(request: Request, reason: str, code_id: Optional[str] = None,
                       action: str = "org_activation_verify"):
    """One generic message for every failure; the reason goes to the audit log only."""
    audit_log.record(action, "failure", request=request, target=code_id, details={"reason": reason})
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ACTIVATION_CODE_INVALID)


@router.post("/activation/verify")
def verify_activation_code(body: ActivationCodeIn, request: Request, db: Session = Depends(get_db)):
    _limit(request, "org-activation", settings.ORG_ACTIVATION_RATE_LIMIT_PER_MINUTE, 60)
    normalised = normalise_activation_code(body.code)
    if normalised is None:
        raise _activation_failed(request, "malformed")
    code = db.query(OrgActivationCode).filter(OrgActivationCode.code_hash == hash_activation_code(normalised)).first()
    reason = _usable_code(code)
    organisation = db.get(Organization, code.organization_id) if code is not None else None
    if reason is None and (organisation is None or organisation.status != ORG_STATUS_PENDING):
        reason = "organisation_not_pending"
    if reason:
        raise _activation_failed(request, reason, code.id if code is not None else None)
    return {
        "organisation_name": organisation.name,
        "admin_email": code.admin_email,
        "admin_name": code.admin_name,
        "expires_at": iso(code.expires_at),
    }


@router.post("/activation/complete", response_model=Token, status_code=status.HTTP_201_CREATED)
def complete_activation(body: ActivationCompleteIn, request: Request, response: Response,
                        db: Session = Depends(get_db)):
    """Create the owner account and activate the organisation. The code row (and the organisation) are
    locked so two concurrent requests cannot both use one code."""
    _limit(request, "org-activation", settings.ORG_ACTIVATION_RATE_LIMIT_PER_MINUTE, 60)
    normalised = normalise_activation_code(body.code)
    if normalised is None:
        raise _activation_failed(request, "malformed", action="org_activation")
    code = (db.query(OrgActivationCode)
            .filter(OrgActivationCode.code_hash == hash_activation_code(normalised))
            .with_for_update()
            .first())
    reason = _usable_code(code)
    organisation = None
    if reason is None:
        organisation = (db.query(Organization).filter(Organization.id == code.organization_id)
                        .with_for_update().first())
        if organisation is None or organisation.status != ORG_STATUS_PENDING:
            reason = "organisation_not_pending"
        elif db.query(User.id).filter(User.email == code.admin_email.lower()).first() is not None:
            reason = "email_in_use"
    if reason:
        code_id = code.id if code is not None else None
        db.rollback()
        raise _activation_failed(request, reason, code_id, action="org_activation")

    now = utcnow()
    owner = User(
        id=str(uuid.uuid4()),
        email=code.admin_email.lower(),
        name=body.name,
        password_hash=hash_password(body.password),
        is_admin=False,
        role="consultant",
        groups=[settings.DEFAULT_ACCESS_GROUP],
        account_type=ACCOUNT_ORGANISATION,
        organization_id=organisation.id,
        org_role=ORG_ROLE_OWNER,
        is_active=True,
        must_change_password=False,
        created_at=now,
    )
    db.add(owner)
    db.flush()
    code.used_at = now
    code.used_by = owner.id
    organisation.status = ORG_STATUS_ACTIVE
    organisation.activated_at = now
    # Any other outstanding code for this organisation is now pointless.
    (db.query(OrgActivationCode)
     .filter(OrgActivationCode.organization_id == organisation.id, OrgActivationCode.id != code.id,
             OrgActivationCode.used_at.is_(None), OrgActivationCode.revoked_at.is_(None))
     .update({OrgActivationCode.revoked_at: now}, synchronize_session=False))
    try:
        db.commit()
    except IntegrityError:  # the e-mail address was taken concurrently
        db.rollback()
        raise _activation_failed(request, "email_in_use", None, action="org_activation")
    audit_log.record("org_activation", request=request, principal=owner, target=organisation.id)
    return issue_login_token(request, "org_login", owner, db, response)


@router.post("/auth/login", response_model=Token)
def organisation_login(login_data: UserLogin, request: Request, response: Response,
                       db: Session = Depends(get_db)):
    """Sign-in for organisation accounts, with the same rate limit, lockout and timing rules as /auth/login."""
    locked_user = _guard_login(request, login_data.email, db, login_data.unlock_key)
    user = (
        _verify_and_consume_unlock_key(db, locked_user, login_data.password, login_data.unlock_key or "")
        if locked_user is not None
        else authenticate_user(db, login_data.email, login_data.password)
    )
    if not user:
        locked = _record_login_failure(db, login_data.email)
        logger.warning("security_event login_failed flow=organisation")
        audit_log.record("org_login", "failure", request=request,
                         target=privacy_fingerprint(login_data.email.lower()))
        raise HTTPException(
            status_code=423 if locked else status.HTTP_401_UNAUTHORIZED,
            detail=ADMIN_UNLOCK_REQUIRED if locked else INVALID_LOGIN,
            headers={"WWW-Authenticate": "Bearer"},
        )
    if user.account_type != ACCOUNT_ORGANISATION:
        deny_sign_in(request, "org_login", user, "individual_account", INDIVIDUAL_ACCOUNT_AT_ORGANISATION_LOGIN)
    require_usable_account(request, "org_login", user)
    return issue_login_token(request, "org_login", user, db, response)


# --------------------------------------------------------------------------------------------------
# Members: organisation overview and the shared knowledge hub
# --------------------------------------------------------------------------------------------------

def _organisation_of(db: Session, member: User) -> Organization:
    organisation = db.get(Organization, member.organization_id)
    if organisation is None:  # get_current_user already requires an active organisation
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Organisation not available.")
    return organisation


def _organisation_out(db: Session, organisation: Organization) -> dict:
    return {
        "id": organisation.id,
        "name": organisation.name,
        "status": organisation.status,
        "member_count": member_count(db, organisation.id),
        "created_at": iso(organisation.created_at),
        "contact_email": organisation.contact_email,
    }


@router.get("/me")
def organisation_me(member: User = Depends(get_current_org_member), db: Session = Depends(get_db)):
    return {
        "organisation": _organisation_out(db, _organisation_of(db, member)),
        "role": member.org_role,
        "can_manage": is_org_manager(member),
    }


@router.get("/knowledge/categories")
def knowledge_categories(member: User = Depends(get_current_org_member), db: Session = Depends(get_db)):
    counts = dict(db.query(OrgSharedAnswer.category, func.count(OrgSharedAnswer.id))
                  .filter(OrgSharedAnswer.organization_id == member.organization_id)
                  .group_by(OrgSharedAnswer.category).all())
    return [{"key": key, "label": label, "count": counts.get(key, 0)} for key, label in CATEGORIES]


def _answer_stats(db: Session, answer_ids: List[str], user_id: str):
    if not answer_ids:
        return {}, {}, set()
    likes = dict(db.query(OrgAnswerLike.shared_answer_id, func.count(OrgAnswerLike.id))
                 .filter(OrgAnswerLike.shared_answer_id.in_(answer_ids))
                 .group_by(OrgAnswerLike.shared_answer_id).all())
    comments = dict(db.query(OrgAnswerComment.shared_answer_id, func.count(OrgAnswerComment.id))
                    .filter(OrgAnswerComment.shared_answer_id.in_(answer_ids))
                    .group_by(OrgAnswerComment.shared_answer_id).all())
    mine = {row[0] for row in db.query(OrgAnswerLike.shared_answer_id)
            .filter(OrgAnswerLike.shared_answer_id.in_(answer_ids), OrgAnswerLike.user_id == user_id).all()}
    return likes, comments, mine


def _summary(answer: OrgSharedAnswer, likes: Dict[str, int], comments: Dict[str, int], mine: set) -> dict:
    return {
        "id": answer.id,
        "question": answer.question,
        "answer_preview": answer.answer[:ANSWER_PREVIEW_CHARS],
        "category": answer.category,
        "category_label": category_label(answer.category),
        "shared_by_name": answer.shared_by_name,
        "created_at": iso(answer.created_at),
        "like_count": likes.get(answer.id, 0),
        "comment_count": comments.get(answer.id, 0),
        "liked_by_me": answer.id in mine,
    }


def _can_edit(member: User, answer: OrgSharedAnswer) -> bool:
    return answer.shared_by == member.id or is_org_manager(member)


def _comment_out(member: User, comment: OrgAnswerComment) -> dict:
    return {
        "id": comment.id,
        "author_name": comment.author_name,
        "body": comment.body,
        "created_at": iso(comment.created_at),
        "can_delete": comment.user_id == member.id or is_org_manager(member),
    }


def _detail(db: Session, member: User, answer: OrgSharedAnswer) -> dict:
    likes, comments, mine = _answer_stats(db, [answer.id], member.id)
    out = _summary(answer, likes, comments, mine)
    likers = (db.query(User.name, OrgAnswerLike.created_at)
              .join(User, User.id == OrgAnswerLike.user_id)
              .filter(OrgAnswerLike.shared_answer_id == answer.id)
              .order_by(OrgAnswerLike.created_at.asc()).all())
    comment_rows = (db.query(OrgAnswerComment).filter(OrgAnswerComment.shared_answer_id == answer.id)
                    .order_by(OrgAnswerComment.created_at.asc()).all())
    out.update({
        "answer": answer.answer,
        "likers": [{"name": name, "liked_at": iso(liked_at)} for name, liked_at in likers],
        "comments": [_comment_out(member, comment) for comment in comment_rows],
        "can_edit": _can_edit(member, answer),
    })
    return out


def _answer_in_org(db: Session, member: User, answer_id: str) -> OrgSharedAnswer:
    answer = (db.query(OrgSharedAnswer)
              .filter(OrgSharedAnswer.id == answer_id, OrgSharedAnswer.organization_id == member.organization_id)
              .first())
    if answer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ANSWER_NOT_FOUND)
    return answer


@router.get("/knowledge")
def list_knowledge(
    category: Optional[CategoryKey] = Query(None),
    q: Optional[str] = Query(None, max_length=200),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    member: User = Depends(get_current_org_member),
    db: Session = Depends(get_db),
):
    query = db.query(OrgSharedAnswer).filter(OrgSharedAnswer.organization_id == member.organization_id)
    if category:
        query = query.filter(OrgSharedAnswer.category == category)
    if q and q.strip():
        pattern = f"%{escape_like(q.strip())}%"
        query = query.filter(or_(OrgSharedAnswer.question.ilike(pattern, escape="\\"),
                                 OrgSharedAnswer.answer.ilike(pattern, escape="\\")))
    total = query.count()
    rows = query.order_by(OrgSharedAnswer.created_at.desc(), OrgSharedAnswer.id.desc()).offset(offset).limit(limit).all()
    likes, comments, mine = _answer_stats(db, [row.id for row in rows], member.id)
    return {"total": total, "items": [_summary(row, likes, comments, mine) for row in rows]}


@router.get("/knowledge/shared-message-ids")
def shared_message_ids(member: User = Depends(get_current_org_member), db: Session = Depends(get_db)):
    """The caller's own chat messages that are already in the hub (to mark them "Shared" in the chat)."""
    rows = (db.query(OrgSharedAnswer.message_id)
            .join(Message, Message.id == OrgSharedAnswer.message_id)
            .join(Chat, Chat.id == Message.chat_id)
            .filter(OrgSharedAnswer.organization_id == member.organization_id, Chat.user_id == member.id)
            .all())
    return {"message_ids": [row[0] for row in rows]}


@router.post("/knowledge", status_code=status.HTTP_201_CREATED)
def share_answer(body: ShareAnswerIn, request: Request, member: User = Depends(get_current_org_member),
                 db: Session = Depends(get_db)):
    """Share one of the caller's own assistant answers with the organisation (one click; no like needed)."""
    answer_message = (db.query(Message)
                      .join(Chat, Chat.id == Message.chat_id)
                      .filter(Message.id == body.message_id, Chat.user_id == member.id, Message.role == "assistant")
                      .first())
    if answer_message is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Only your own assistant answers can be shared.")
    existing = (db.query(OrgSharedAnswer)
                .filter(OrgSharedAnswer.organization_id == member.organization_id,
                        OrgSharedAnswer.message_id == answer_message.id).first())
    if existing is not None:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT,
                            content={"detail": "This answer is already shared.", "id": existing.id})

    question_message = (db.query(Message)
                        .filter(Message.chat_id == answer_message.chat_id, Message.role == "user",
                                Message.created_at <= answer_message.created_at)
                        .order_by(Message.created_at.desc()).first())
    question = (question_message.content if question_message is not None else "").strip()
    answer_text = strip_markers(answer_message.content).strip()
    now = utcnow()
    shared = OrgSharedAnswer(
        id=str(uuid.uuid4()),
        organization_id=member.organization_id,
        message_id=answer_message.id,
        question=question,
        answer=answer_text,
        category=categorise(question, answer_text),
        shared_by=member.id,
        shared_by_name=member.name,
        created_at=now,
        updated_at=now,
    )
    db.add(shared)
    try:
        db.flush()  # the like below references this row; the unit of work does not order by bare FKs
        # Sharing counts as liking the answer in the hub.
        db.add(OrgAnswerLike(shared_answer_id=shared.id, user_id=member.id, created_at=now))
        db.commit()
    except IntegrityError:  # shared concurrently
        db.rollback()
        existing = (db.query(OrgSharedAnswer)
                    .filter(OrgSharedAnswer.organization_id == member.organization_id,
                            OrgSharedAnswer.message_id == answer_message.id).first())
        return JSONResponse(status_code=status.HTTP_409_CONFLICT,
                            content={"detail": "This answer is already shared.",
                                     "id": existing.id if existing is not None else None})
    audit_log.record("org_knowledge_share", request=request, principal=member, target=shared.id,
                     details={"category": shared.category})
    return _detail(db, member, shared)


@router.get("/knowledge/{answer_id}")
def get_knowledge(answer_id: str, member: User = Depends(get_current_org_member), db: Session = Depends(get_db)):
    return _detail(db, member, _answer_in_org(db, member, answer_id))


@router.patch("/knowledge/{answer_id}")
def update_knowledge(answer_id: str, body: CategoryUpdateIn, request: Request,
                     member: User = Depends(get_current_org_member), db: Session = Depends(get_db)):
    answer = _answer_in_org(db, member, answer_id)
    if not _can_edit(member, answer):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Only the person who shared this answer or an organisation administrator can change it.")
    previous = answer.category
    answer.category = body.category
    answer.updated_at = utcnow()
    db.commit()
    audit_log.record("org_knowledge_update", request=request, principal=member, target=answer.id,
                     details={"before": previous, "after": body.category})
    return _detail(db, member, answer)


@router.delete("/knowledge/{answer_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_knowledge(answer_id: str, request: Request, member: User = Depends(get_current_org_member),
                     db: Session = Depends(get_db)):
    answer = _answer_in_org(db, member, answer_id)
    if not _can_edit(member, answer):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Only the person who shared this answer or an organisation administrator can remove it.")
    db.delete(answer)
    db.commit()
    audit_log.record("org_knowledge_delete", request=request, principal=member, target=answer_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _like_state(db: Session, answer_id: str, user_id: str) -> dict:
    count = db.query(func.count(OrgAnswerLike.id)).filter(OrgAnswerLike.shared_answer_id == answer_id).scalar() or 0
    liked = db.query(OrgAnswerLike.id).filter(OrgAnswerLike.shared_answer_id == answer_id,
                                              OrgAnswerLike.user_id == user_id).first() is not None
    return {"liked": liked, "like_count": count}


@router.post("/knowledge/{answer_id}/like")
def like_knowledge(answer_id: str, member: User = Depends(get_current_org_member), db: Session = Depends(get_db)):
    answer = _answer_in_org(db, member, answer_id)
    exists = db.query(OrgAnswerLike.id).filter(OrgAnswerLike.shared_answer_id == answer.id,
                                               OrgAnswerLike.user_id == member.id).first()
    if exists is None:
        db.add(OrgAnswerLike(shared_answer_id=answer.id, user_id=member.id, created_at=utcnow()))
        try:
            db.commit()
        except IntegrityError:  # liked concurrently: still liked
            db.rollback()
    return _like_state(db, answer.id, member.id)


@router.delete("/knowledge/{answer_id}/like")
def unlike_knowledge(answer_id: str, member: User = Depends(get_current_org_member), db: Session = Depends(get_db)):
    answer = _answer_in_org(db, member, answer_id)
    (db.query(OrgAnswerLike)
     .filter(OrgAnswerLike.shared_answer_id == answer.id, OrgAnswerLike.user_id == member.id)
     .delete(synchronize_session=False))
    db.commit()
    return _like_state(db, answer.id, member.id)


@router.post("/knowledge/{answer_id}/comments", status_code=status.HTTP_201_CREATED)
def add_comment(answer_id: str, body: CommentIn, request: Request,
                member: User = Depends(get_current_org_member), db: Session = Depends(get_db)):
    answer = _answer_in_org(db, member, answer_id)
    comment = OrgAnswerComment(shared_answer_id=answer.id, user_id=member.id, author_name=member.name,
                               body=body.body, created_at=utcnow())
    db.add(comment)
    db.commit()
    db.refresh(comment)
    audit_log.record("org_comment_create", request=request, principal=member, target=comment.id)
    return _comment_out(member, comment)


@router.delete("/knowledge/{answer_id}/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_comment(answer_id: str, comment_id: str, request: Request,
                   member: User = Depends(get_current_org_member), db: Session = Depends(get_db)):
    answer = _answer_in_org(db, member, answer_id)
    comment = (db.query(OrgAnswerComment)
               .filter(OrgAnswerComment.id == comment_id, OrgAnswerComment.shared_answer_id == answer.id).first())
    if comment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=COMMENT_NOT_FOUND)
    if comment.user_id != member.id and not is_org_manager(member):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Only the author or an organisation administrator can delete this comment.")
    db.delete(comment)
    db.commit()
    audit_log.record("org_comment_delete", request=request, principal=member, target=comment_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------------------------------
# Organisation administrators (org_role owner or admin)
# --------------------------------------------------------------------------------------------------

def _member_out(user: User) -> dict:
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "org_role": user.org_role,
        "is_active": user.is_active,
        "must_change_password": user.must_change_password,
        "created_at": iso(user.created_at),
        "last_login_at": iso(user.last_login_at),
    }


def _member_in_org(db: Session, admin: User, member_id: str) -> User:
    member = db.query(User).filter(User.id == member_id, User.organization_id == admin.organization_id,
                                   User.account_type == ACCOUNT_ORGANISATION).first()
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MEMBER_NOT_FOUND)
    return member


def _check_can_manage(admin: User, target: User) -> None:
    """Nobody changes themselves or the owner here; only the owner manages other administrators."""
    if target.id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot change your own account here.")
    if target.org_role == ORG_ROLE_OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="The organisation owner cannot be changed.")
    if target.org_role == ORG_ROLE_ADMIN and not is_org_owner(admin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Only the organisation owner can change an administrator.")


@router.get("/admin/members")
def list_members(admin: User = Depends(get_current_org_admin), db: Session = Depends(get_db)):
    members = (db.query(User).filter(User.organization_id == admin.organization_id,
                                     User.account_type == ACCOUNT_ORGANISATION)
               .order_by(User.created_at.asc()).all())
    return [_member_out(member) for member in members]


@router.post("/admin/members", status_code=status.HTTP_201_CREATED)
def create_member(body: MemberCreateIn, request: Request, admin: User = Depends(get_current_org_admin),
                  db: Session = Depends(get_db)):
    """Create a member with the e-mail and password the admin chose. The password is never returned or logged;
    the member must choose their own at the first sign-in."""
    if not rate_limiter.allow("org-member-create", admin.id, settings.ORG_MEMBER_CREATE_RATE_LIMIT_PER_HOUR, 3600):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=TOO_MANY_ATTEMPTS)
    if body.org_role == ORG_ROLE_ADMIN and not is_org_owner(admin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Only the organisation owner can grant the admin role.")
    if db.query(User.id).filter(User.email == body.email).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="An account with this e-mail address already exists.")
    member = User(
        email=body.email,
        name=body.name,
        password_hash=hash_password(body.password),
        is_admin=False,
        role="consultant",
        groups=[settings.DEFAULT_ACCESS_GROUP],
        account_type=ACCOUNT_ORGANISATION,
        organization_id=admin.organization_id,
        org_role=body.org_role,
        is_active=True,
        must_change_password=True,
        created_at=utcnow(),
    )
    db.add(member)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="An account with this e-mail address already exists.")
    db.refresh(member)
    audit_log.record("org_member_create", request=request, principal=admin, target=member.id,
                     details={"org_role": member.org_role})
    return {"member": _member_out(member)}


@router.patch("/admin/members/{member_id}")
def update_member(member_id: str, body: MemberUpdateIn, request: Request,
                  admin: User = Depends(get_current_org_admin), db: Session = Depends(get_db)):
    member = _member_in_org(db, admin, member_id)
    _check_can_manage(admin, member)
    if body.org_role == ORG_ROLE_ADMIN and member.org_role != ORG_ROLE_ADMIN and not is_org_owner(admin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Only the organisation owner can grant the admin role.")
    changes = {}
    revoke = False
    if body.org_role is not None and body.org_role != member.org_role:
        changes["org_role"] = {"before": member.org_role, "after": body.org_role}
        member.org_role = body.org_role
        revoke = True
    if body.is_active is not None and body.is_active != member.is_active:
        changes["is_active"] = {"before": member.is_active, "after": body.is_active}
        member.is_active = body.is_active
        revoke = revoke or not body.is_active
    if revoke:
        member.token_version += 1
    db.commit()
    db.refresh(member)
    if changes:
        audit_log.record("org_member_update", request=request, principal=admin, target=member.id, details=changes)
    return _member_out(member)


@router.post("/admin/members/{member_id}/reset-password")
def reset_member_password(member_id: str, body: MemberPasswordIn, request: Request,
                          admin: User = Depends(get_current_org_admin), db: Session = Depends(get_db)):
    """Replace the member's password with one the admin chose (never returned or logged), require a change at
    the next sign-in and revoke the member's sessions."""
    member = _member_in_org(db, admin, member_id)
    _check_can_manage(admin, member)
    member.password_hash = hash_password(body.password)
    member.must_change_password = True
    member.token_version += 1
    db.commit()
    rate_limiter.clear_login_failures(member.email)
    audit_log.record("org_member_password_reset", request=request, principal=admin, target=member.id)
    db.refresh(member)
    return _member_out(member)


@router.get("/admin/activity")
def organisation_activity(admin: User = Depends(get_current_org_admin), db: Session = Depends(get_db)):
    """Usage counts per member. Counts only: no chat content."""
    org_id = admin.organization_id
    members = (db.query(User).filter(User.organization_id == org_id, User.account_type == ACCOUNT_ORGANISATION)
               .order_by(User.created_at.asc()).all())
    ids = [member.id for member in members]
    chats, questions, shared, comments, likes = {}, {}, {}, {}, {}
    if ids:
        chats = dict(db.query(Chat.user_id, func.count(Chat.id)).filter(Chat.user_id.in_(ids))
                     .group_by(Chat.user_id).all())
        questions = dict(db.query(Chat.user_id, func.count(Message.id))
                         .join(Message, Message.chat_id == Chat.id)
                         .filter(Chat.user_id.in_(ids), Message.role == "user")
                         .group_by(Chat.user_id).all())
        shared = dict(db.query(OrgSharedAnswer.shared_by, func.count(OrgSharedAnswer.id))
                      .filter(OrgSharedAnswer.organization_id == org_id, OrgSharedAnswer.shared_by.in_(ids))
                      .group_by(OrgSharedAnswer.shared_by).all())
        comments = dict(db.query(OrgAnswerComment.user_id, func.count(OrgAnswerComment.id))
                        .join(OrgSharedAnswer, OrgSharedAnswer.id == OrgAnswerComment.shared_answer_id)
                        .filter(OrgSharedAnswer.organization_id == org_id, OrgAnswerComment.user_id.in_(ids))
                        .group_by(OrgAnswerComment.user_id).all())
        likes = dict(db.query(OrgAnswerLike.user_id, func.count(OrgAnswerLike.id))
                     .join(OrgSharedAnswer, OrgSharedAnswer.id == OrgAnswerLike.shared_answer_id)
                     .filter(OrgSharedAnswer.organization_id == org_id, OrgAnswerLike.user_id.in_(ids))
                     .group_by(OrgAnswerLike.user_id).all())
    rows = [{
        "id": member.id,
        "name": member.name,
        "chats": chats.get(member.id, 0),
        "questions": questions.get(member.id, 0),
        "shared_answers": shared.get(member.id, 0),
        "comments": comments.get(member.id, 0),
        "likes": likes.get(member.id, 0),
        "last_login_at": iso(member.last_login_at),
    } for member in members]
    hub_answers = db.query(func.count(OrgSharedAnswer.id)).filter(OrgSharedAnswer.organization_id == org_id).scalar()
    hub_comments = (db.query(func.count(OrgAnswerComment.id))
                    .join(OrgSharedAnswer, OrgSharedAnswer.id == OrgAnswerComment.shared_answer_id)
                    .filter(OrgSharedAnswer.organization_id == org_id).scalar())
    hub_likes = (db.query(func.count(OrgAnswerLike.id))
                 .join(OrgSharedAnswer, OrgSharedAnswer.id == OrgAnswerLike.shared_answer_id)
                 .filter(OrgSharedAnswer.organization_id == org_id).scalar())
    since = utcnow() - timedelta(days=30)
    active_30d = sum(1 for member in members if member.last_login_at and member.last_login_at >= since)
    return {
        "members": rows,
        "totals": {
            "members": len(members),
            "active_members": sum(1 for member in members if member.is_active),
            "members_signed_in_30d": active_30d,
            "chats": sum(row["chats"] for row in rows),
            "questions": sum(row["questions"] for row in rows),
            "shared_answers": hub_answers or 0,
            "comments": hub_comments or 0,
            "likes": hub_likes or 0,
        },
    }


@router.patch("/admin/organisation")
def update_organisation(body: OrganisationUpdateIn, request: Request, admin: User = Depends(get_current_org_admin),
                        db: Session = Depends(get_db)):
    organisation = _organisation_of(db, admin)
    changes = {}
    if body.name is not None and body.name != organisation.name:
        if find_organisation_by_name(db, body.name, exclude_id=organisation.id) is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="An organisation with this name already exists.")
        changes["name"] = True
        organisation.name = body.name
    if body.contact_email is not None and body.contact_email != organisation.contact_email:
        changes["contact_email"] = True
        organisation.contact_email = body.contact_email
    if changes:
        organisation.updated_at = utcnow()
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="An organisation with this name already exists.")
        audit_log.record("org_update", request=request, principal=admin, target=organisation.id,
                         details={"changed": sorted(changes)})
    return _organisation_out(db, organisation)
