import json
import inspect
import logging
import re
import time
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, load_only

logger = logging.getLogger(__name__)

from backend.app.database import get_db, SessionLocal
from backend.app.models.user import User
from backend.app.models.chat import Chat
from backend.app.models.message import Message
from backend.app.models.citation import Citation
from backend.app.models.web_source import WebSource
from backend.app.models.web_image import WebImage
from backend.app.models.uploaded_attachment import UploadedAttachment
from backend.app.models.shared_prompt import SharedPrompt
from backend.app.schemas.message import MessageCreate, MessageFeedback, MessageResponse, SavePartialRequest, stored_token_usage
from backend.app.security.auth import get_current_user
from backend.app.security.ratelimit import rate_limiter
from backend.app.security.stream_tokens import STREAM_TOKEN_TTL_SECONDS, issue_stream_token, verify_stream_token
from backend.app.services.rag_service import rag_service
from backend.app.services.llm_service import (
    SPRINT_CASUAL_SYSTEM_PROMPT, UNAVAILABLE_RESPONSE, generation_context, llm_service, with_generation_context,
)
from backend.app.services.query_router import QueryRoute, query_router
from backend.app.services.intent_analyzer import intent_analyzer
from backend.app.services import document_generator
from backend.app.services.retrieval_service import retrieval_service
from backend.app.services.reranking_service import reranking_service
from backend.app.services.grounding_service import grounding_service
from backend.app.services.web_search_service import web_search_service
from backend.app.services.company_info import COMPANY_ANSWER, is_company_question
from backend.app.services.sap_classifier import NON_SAP_REFUSAL_RESPONSE, sap_classifier
from backend.app.services.evidence_markers import strip_markers, StreamingMarkerStripper
from backend.app.services.conversation_memory_service import MemoryIntent, conversation_memory_service
from backend.app.services.public_payload import public_done_event
from backend.app.services import audit_log, languages, token_meter, token_wallet, user_memory
from backend.app.services.detached_stream import detached, stream_cancellations
from backend.app.services.capacity import answer_capacity
from backend.app.config import settings

# "[Attached: a.pdf (2 pages)]\n\n" header that prefixes prompts sent with files.
ATTACHED_PREFIX = re.compile(r"^\[Attached:.*?\]\n\n", re.DOTALL)


def visible_content(content: str, is_admin: bool) -> str:
    """Inline evidence markers point at sources; users who cannot see sources get clean text."""
    return content if is_admin else strip_markers(content or "")

def _fit(model, field: str, value):
    """Clip display metadata to its VARCHAR length; PostgreSQL rejects over-long values."""
    if value is None:
        return None
    value = str(value)
    limit = getattr(model.__table__.c[field].type, "length", None)
    return value if not limit or len(value) <= limit else value[: limit - 1] + "…"


def _url_fits(model, field: str, url) -> bool:
    """A clipped URL is a broken link, so over-long URLs are skipped instead."""
    limit = getattr(model.__table__.c[field].type, "length", None)
    return bool(url) and (not limit or len(str(url)) <= limit)


def source_rows(message_id: str, citations, web_sources, images) -> list:
    """Citation, web-source and web-image rows for an assistant message, sized to their columns."""
    rows = []
    for c in citations or []:
        document = str(c.get("document", ""))
        rows.append(Citation(
            message_id=message_id,
            source_type="knowledge_base",
            document_id=_fit(Citation, "document_id", "doc-" + document),
            document_name=_fit(Citation, "document_name", document),
            chunk_id="chunk-ref",
            page_number=c.get("page"),
            section=_fit(Citation, "section", c.get("section")),
            similarity_score=c.get("score"),
            citation_text=c.get("snippet"),
        ))
    for w in web_sources or []:
        if not _url_fits(WebSource, "url", w.get("url")):
            logger.warning("Skipping web source with over-long URL (%d chars)", len(str(w.get("url") or "")))
            continue
        rows.append(WebSource(
            message_id=message_id,
            url=w.get("url"),
            title=_fit(WebSource, "title", w.get("title")),
            domain=_fit(WebSource, "domain", w.get("domain")),
            snippet=w.get("snippet"),
        ))
    for image in images or []:
        if not _url_fits(WebImage, "url", image.get("url")):
            logger.warning("Skipping web image with over-long URL (%d chars)", len(str(image.get("url") or "")))
            continue
        source_url = image.get("source_url")
        rows.append(WebImage(
            message_id=message_id,
            url=image.get("url"),
            description=image.get("description"),
            alt_text=_fit(WebImage, "alt_text", image.get("alt_text")),
            source_url=source_url if _url_fits(WebImage, "source_url", source_url) else None,
            source_title=_fit(WebImage, "source_title", image.get("source_title")),
        ))
    return rows


router = APIRouter(prefix="/chats/{chat_id}/messages", tags=["Messages"])

def verify_chat_ownership(chat_id: str, user_id: str, db: Session) -> Chat:
    chat = db.query(Chat).filter(Chat.id == chat_id, Chat.user_id == user_id).first()
    if not chat:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")
    return chat


def persist_uploaded_attachments(message: Message, attachments: List[dict]) -> None:
    """Keep extracted text scoped to the user message that uploaded it."""
    message.uploaded_attachments = [
        UploadedAttachment(
            name=attachment["name"],
            file_type=attachment.get("file_type") or "txt",
            page_count=attachment.get("page_count"),
            extracted_text=attachment["text"],
        )
        for attachment in attachments
        if attachment.get("text", "").strip()
    ]


def latest_chat_attachments(
    db: Session, chat_id: str, current_message_id: str, current_attachments: List[dict]
) -> List[dict]:
    """Reuse the most recently attached files for follow-up questions in this chat."""
    if current_attachments:
        return current_attachments
    prior_message = (
        db.query(Message)
        .join(UploadedAttachment, UploadedAttachment.message_id == Message.id)
        .filter(
            Message.chat_id == chat_id,
            Message.role == "user",
            Message.id != current_message_id,
        )
        .order_by(Message.created_at.desc())
        .first()
    )
    if not prior_message:
        return []
    return [
        {
            "name": item.name,
            "file_type": item.file_type,
            "page_count": item.page_count,
            "text": item.extracted_text,
        }
        for item in prior_message.uploaded_attachments
    ]

def _saved_memory_text(db: Session, user_id: str, query: str) -> str:
    """The user's saved memories for this question; a memory failure must never lose the answer."""
    try:
        return user_memory.prompt_text(db, user_id, query)
    except Exception as exc:
        logger.warning("Saved memory unavailable (%s)", type(exc).__name__)
        return ""


OUT_OF_TOKENS = "You have used all your tokens."


def _require_tokens(db: Session, user: User) -> None:
    token_wallet.refill_if_due(db, user)
    if not token_wallet.has_balance(user):
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=OUT_OF_TOKENS)


def _charge_tokens(db: Session, user: User, token_usage: Dict[str, Any]) -> Optional[float]:
    """Charge the answer to the user's free tokens; the new balance, or None for unmetered accounts."""
    token_usage["cost"] = token_wallet.cost_tokens(token_usage["used"])
    if not token_wallet.is_metered(user):
        return None
    try:
        return round(token_wallet.charge(db, user.id, token_wallet.cost_milli(token_usage["used"])) / token_wallet.MILLI, 2)
    except Exception as exc:  # an accounting failure must not lose the answer
        logger.warning("Token charge failed (%s)", type(exc).__name__)
        try:
            db.rollback()
        except Exception:
            pass
        return None


@router.get("", response_model=List[MessageResponse])
def get_messages(
    chat_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    verify_chat_ownership(chat_id, current_user.id, db)
    messages = (
        db.query(Message)
        .options(load_only(
            Message.id, Message.chat_id, Message.role, Message.content, Message.source_type,
            Message.grounding_score, Message.created_at, Message.feedback,
            Message.llm_provider, Message.llm_model, Message.regeneration_count,
            Message.tokens_used, Message.tokens_baseline,
        ))
        .filter(Message.chat_id == chat_id)
        .order_by(Message.created_at.asc())
        .all()
    )

    is_admin = bool(current_user.is_admin)
    result = []
    for msg in messages:
        c_list = [
            {
                "id": c.id,
                "document": c.document_name,
                "page": c.page_number,
                "section": c.section,
                "score": c.similarity_score,
                "snippet": c.citation_text
            }
            for c in msg.citations
        ] if is_admin else []

        w_list = [
            {
                "id": w.id,
                "title": w.title,
                "url": w.url,
                "domain": w.domain,
                "snippet": w.snippet
            }
            for w in msg.web_sources
        ] if is_admin else []

        image_list = [
            {
                "url": image.url,
                "description": image.description,
                "alt_text": image.alt_text,
                "source_url": image.source_url if is_admin else None,
                "source_title": image.source_title if is_admin else None,
                "relevance_score": None,
            }
            for image in msg.images
        ]
        result.append(
            MessageResponse(
                id=msg.id,
                chat_id=msg.chat_id,
                role=msg.role,
                content=visible_content(msg.content, is_admin),
                source_type=msg.source_type if is_admin else None,
                grounding_score=msg.grounding_score if is_admin else None,
                verification_status=("unavailable" if msg.source_type == "unavailable" else None),
                created_at=msg.created_at,
                feedback=msg.feedback,
                llm_provider=msg.llm_provider if is_admin else None,
                llm_model=msg.llm_model if is_admin else None,
                regeneration_count=msg.regeneration_count or 0,
                token_usage=stored_token_usage(msg),
                citations=c_list,
                web_sources=w_list,
                images=image_list,
                attachments=[
                    {
                        "name": attachment.name,
                        "file_type": attachment.file_type,
                        "page_count": attachment.page_count,
                    }
                    for attachment in msg.uploaded_attachments
                ] or None,
            )
        )
    return result

def _accepted_optional_kwargs(method, **optional) -> Dict[str, Any]:
    """The optional keyword arguments that `method` (or the test double replacing it) accepts."""
    target = getattr(method, "side_effect", None) or method
    try:
        parameters = inspect.signature(target).parameters
    except Exception:
        return optional
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return optional
    return {name: value for name, value in optional.items() if name in parameters}


def _invoke_rag_process(db: Session, query: str, chat_history, user, attachments, chat_id: Optional[str] = None, regeneration_index: Optional[int] = None):
    kwargs = {"chat_history": chat_history, "user": user, "attachments": attachments}
    kwargs.update(_accepted_optional_kwargs(
        rag_service.process_query, chat_id=chat_id, regeneration_index=regeneration_index
    ))
    try:
        return rag_service.process_query(db, query, **kwargs)
    except TypeError as te:
        if "chat_id" in str(te) and "chat_id" in kwargs:
            kwargs.pop("chat_id", None)
            return rag_service.process_query(db, query, **kwargs)
        raise


def _invoke_rag_stream(db: Session, query: str, chat_history, user, attachments, chat_id: Optional[str] = None, regeneration_index: Optional[int] = None):
    kwargs = {"chat_history": chat_history, "user": user, "attachments": attachments}
    kwargs.update(_accepted_optional_kwargs(
        rag_service.stream_query, chat_id=chat_id, regeneration_index=regeneration_index
    ))
    try:
        return rag_service.stream_query(db, query, **kwargs)
    except TypeError as te:
        if "chat_id" in str(te) and "chat_id" in kwargs:
            kwargs.pop("chat_id", None)
            return rag_service.stream_query(db, query, **kwargs)
        raise


def _plain_chat_history(db: Session, chat_id: str, exclude_message_id: Optional[str] = None):
    """Read conversational text only; Sprint must not touch message embeddings or retrieval services."""
    query = db.query(Message.id, Message.role, Message.content).filter(Message.chat_id == chat_id)
    if exclude_message_id:
        query = query.filter(Message.id != exclude_message_id)
    rows = query.order_by(Message.created_at.desc(), Message.id.desc()).limit(20).all()
    return [{"id": msg_id, "role": role, "content": content} for msg_id, role, content in reversed(rows)]


def _conversation_chars(db: Session, chat_id: str, exclude_message_id: Optional[str] = None) -> int:
    """Length of the whole conversation, which a generic chatbot resends with every question."""
    query = db.query(func.coalesce(func.sum(func.length(Message.content)), 0)).filter(Message.chat_id == chat_id)
    if exclude_message_id:
        query = query.filter(Message.id != exclude_message_id)
    return int(query.scalar() or 0)


def _history_chars(history) -> int:
    return sum(len(item.get("content") or "") for item in history or [])


def _usage_event(tokens: int) -> str:
    return f"data: {json.dumps({'type': 'usage', 'tokens': tokens})}\n\n"


# Columns the regeneration bookkeeping reads, so Sprint never loads stored message embeddings.
_ANSWER_REGENERATION_COLUMNS = load_only(
    Message.id, Message.created_at, Message.feedback, Message.regeneration_count,
    Message.llm_provider, Message.llm_model,
)


def _next_regeneration_index(previous_answer: Optional[Message]) -> int:
    """Regenerations of one question count up from the stored answer being replaced, so the count survives
    restarts and is the same in every worker."""
    return (previous_answer.regeneration_count or 0) + 1 if previous_answer is not None else 1


def _resend_regeneration(db: Session, chat_id: str, user_content: str, requested: bool):
    """(regeneration index, previous answer) for a newly sent prompt; index 0 means a new question.
    Sending the latest question again, or `is_regeneration`, regenerates its answer."""
    previous_question = (
        db.query(Message)
        .options(load_only(Message.id, Message.content, Message.created_at))
        .filter(Message.chat_id == chat_id, Message.role == "user")
        .order_by(Message.created_at.desc())
        .first()
    )
    text = user_content.strip()
    repeated = previous_question is not None and text in {
        (previous_question.content or "").strip(), ATTACHED_PREFIX.sub("", previous_question.content or "").strip(),
    }
    if not (requested or repeated):
        return 0, None
    answers = db.query(Message).options(_ANSWER_REGENERATION_COLUMNS).filter(
        Message.chat_id == chat_id, Message.role == "assistant"
    )
    if previous_question is not None:
        answers = answers.filter(Message.created_at > previous_question.created_at)
    previous_answer = answers.order_by(Message.created_at.desc()).first()
    return _next_regeneration_index(previous_answer), previous_answer


def _log_prompt(action: str, chat_id: str, user: User, mode: str, prompt_chars: int, regeneration_index: int,
                previous_answer: Optional[Message] = None) -> None:
    # The user id, not the email: the privacy log filter would redact an email anyway.
    if regeneration_index:
        logger.info(
            "[REGENERATION #%d] %s | Chat: %s... | User: %s | Mode: %s | Previous answer: %s (%s)",
            regeneration_index, action, chat_id[:8], user.id, mode,
            getattr(previous_answer, "llm_provider", None) or "unknown",
            getattr(previous_answer, "llm_model", None) or "unknown",
        )
    else:
        logger.info(
            "[CHAT] %s | Chat: %s... | User: %s | Mode: %s | Prompt length: %d chars",
            action, chat_id[:8], user.id, mode, prompt_chars,
        )


def _answering_llm(result: Dict[str, Any], llm_info: Dict[str, Any]):
    """(provider, model) that wrote an answer: the pipeline's own record first, then the provider call that
    replied. None when no LLM answered (memory lookups, refusals, extractive fallbacks) - never a guess."""
    return (
        result.get("provider_used") or llm_info.get("provider"),
        result.get("model_used") or llm_info.get("model"),
    )


# Questions about the conversation itself; they bypass the SAP-only scope check.
_SPRINT_RECALL_INTENTS = (MemoryIntent.HISTORY_RECALL, MemoryIntent.CROSS_CONVERSATION_RECALL, MemoryIntent.MIXED)


def _sprint_memory(
    db: Session, chat_id: str, user_id: str, query: str, chat_history, user_message_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Conversation memory for Sprint, without embeddings: exact first/last-question lookups, the rolling
    summary of turns older than the 20-message window, and keyword recall from this chat or the user's
    other chats. A set "answer" is final and is returned without calling the LLM.
    """
    saved = user_memory.handle_command(db, user_id, chat_id, query, chat_history or [])
    if saved is not None:  # "save it" / "remember that ...": stored, confirmed without calling the model
        return {"intent": "MEMORY_COMMAND", "answer": saved, "summary": None, "recalled": []}
    intent = conversation_memory_service.classify_intent(query, chat_history=chat_history)
    memory: Dict[str, Any] = {"intent": intent, "answer": None, "summary": None, "recalled": []}
    if intent in (MemoryIntent.FIRST_MESSAGE_LOOKUP, MemoryIntent.LAST_MESSAGE_LOOKUP):
        memory["answer"] = conversation_memory_service.exact_lookup_answer(
            db, chat_id, intent, exclude_message_id=user_message_id
        )
        return memory

    memory["summary"] = conversation_memory_service.get_chat_summary(db, chat_id)
    if intent in _SPRINT_RECALL_INTENTS:
        cross = intent == MemoryIntent.CROSS_CONVERSATION_RECALL
        memory["recalled"] = conversation_memory_service.search_history(
            db, query, user_id=user_id, current_chat_id=chat_id,
            exclude_message_ids=[m.get("id") for m in (chat_history or [])] + [user_message_id],
            cross_conversation=cross, top_k=5, use_embeddings=False,
        )
        if not memory["recalled"] and cross:
            memory["answer"] = (
                "I searched your other conversations, but could not find any earlier discussion matching your query."
            )
        elif not memory["recalled"] and intent == MemoryIntent.HISTORY_RECALL and not chat_history and not memory["summary"]:
            memory["answer"] = (
                "I reviewed our conversation history, but could not find any previous mention of that in our messages."
            )
    return memory


def _sprint_allowed(query: str, chat_history, attachments) -> bool:
    attachment_text = "\n".join(str(item.get("text") or "") for item in (attachments or []))
    allowed, _ = sap_classifier.classify(f"{query}\n{attachment_text}")
    if not allowed and chat_history:
        prior_user_questions = [
            item.get("content", "") for item in chat_history if item.get("role") == "user"
        ][-3:]
        allowed, _ = sap_classifier.classify("\n".join([*prior_user_questions, query]))
    return allowed


def _sprint_analysis(query: str, chat_history, attachments, memory: Optional[Dict[str, Any]]):
    """The LLM intent analysis (intent + spelling correction) of a Sprint question, or None when it does not
    apply (attachments, memory answers and recall) or is unavailable; the rule-based routing then decides."""
    memory = memory or {}
    if attachments or memory.get("answer") or memory.get("intent") in _SPRINT_RECALL_INTENTS:
        return None
    return intent_analyzer.analyze(query, chat_history)


def _sprint_formats(query: str, analysis, attachments, memory: Optional[Dict[str, Any]]) -> list:
    """The files a Sprint question asks to be created (none for attachments, memory answers and recall)."""
    memory = memory or {}
    if attachments or memory.get("answer") or memory.get("intent") in _SPRINT_RECALL_INTENTS:
        return []
    return document_generator.requested_formats(analysis, query)


def _sprint_document(content: str, formats: list, owner_id, request: str) -> str:
    """The chat message for a document request (links to the created files); the content itself if that fails."""
    try:
        return document_generator.create_documents(owner_id, content, formats, request_title=request)[0]
    except Exception as exc:
        logger.warning("Document creation failed (%s)", type(exc).__name__)
        return content


def _sprint_route(query: str, chat_history, attachments, memory: Dict[str, Any], analysis=None) -> str:
    """
    The Sprint path for this turn; the value is also the stored source_type. Memory answers win. With an
    LLM intent analysis the meaning decides: clarifying question, small talk, SAP answer or a general answer
    (nothing is refused for lacking SAP keywords). Without one, SAP questions (including SAP follow-ups such
    as "ok, what about dunning?") win, and only a turn that would otherwise be refused is checked for small
    talk, with the same router as Scholar's casual path.
    """
    if memory.get("answer"):
        return "conversation_memory"
    if analysis is not None and not attachments and memory.get("intent") not in _SPRINT_RECALL_INTENTS:
        if is_company_question(query) or is_company_question(analysis.corrected_question):
            return "company"  # about Clyptus itself: a fixed answer, not a model guess
        if analysis.needs_clarification:
            return "clarification"
        if analysis.small_talk:
            return "conversational"
        if analysis.about_sap:
            return "sprint"
        return "refusal" if settings.SAP_ONLY_SCOPE else "general"  # not about SAP: declined unless the scope is open
    if memory.get("intent") in _SPRINT_RECALL_INTENTS or _sprint_allowed(query, chat_history, attachments):
        return "sprint"
    if not attachments and is_company_question(query):
        return "company"  # about Clyptus itself: a fixed answer instead of the SAP-only refusal
    if not attachments and query_router.route(query, chat_history=chat_history).route == QueryRoute.EMOTIONAL_CASUAL:
        return "conversational"
    return "refusal"


def _sprint_result(query: str, chat_history, attachments, memory: Optional[Dict[str, Any]] = None, owner_id=None) -> Dict[str, Any]:
    memory = memory or {}
    analysis = _sprint_analysis(query, chat_history, attachments, memory)
    route = _sprint_route(query, chat_history, attachments, memory, analysis=analysis)
    formats = _sprint_formats(query, analysis, attachments, memory) if route in ("sprint", "general") else []
    if analysis is not None:
        query = analysis.corrected_question  # the corrected question is what the model answers
    ask = document_generator.query_with_instructions(query, formats) if formats else query
    if route == "clarification":
        return {
            "answer": analysis.clarification_question, "source_type": "conversational",
            "verification_status": "unverified", "grounding_score": None,
        }
    if route == "general":
        answer = llm_service.generate_general_reply(
            ask, chat_history=chat_history, regeneration_index=memory.get("regeneration_index")
        )
        if formats and answer != UNAVAILABLE_RESPONSE:
            answer = _sprint_document(answer, formats, owner_id, query)
        return {
            "answer": answer, "source_type": "unavailable" if answer == UNAVAILABLE_RESPONSE else "direct_llm",
            "verification_status": "unavailable" if answer == UNAVAILABLE_RESPONSE else "unverified",
            "grounding_score": None,
        }
    if route == "conversation_memory":
        return {
            "answer": memory["answer"], "source_type": "conversation_memory",
            "verification_status": "unverified", "grounding_score": None,
        }
    if route == "company":
        return {
            "answer": COMPANY_ANSWER, "source_type": "conversational",
            "verification_status": "unverified", "grounding_score": None,
        }
    if route == "refusal":
        return {
            "answer": NON_SAP_REFUSAL_RESPONSE, "source_type": "refusal",
            "verification_status": "unverified", "grounding_score": None,
        }
    if route == "conversational":
        answer = llm_service.generate_emotional_casual_reply(
            query, chat_history=chat_history, system_prompt=SPRINT_CASUAL_SYSTEM_PROMPT
        )
        return {
            "answer": answer, "source_type": "conversational",
            "verification_status": "unverified", "grounding_score": None,
        }

    answer = llm_service.generate_sprint_reply(
        ask, chat_history=chat_history, attachments=attachments,
        summary=memory.get("summary"), recalled_messages=memory.get("recalled"),
        regeneration_index=memory.get("regeneration_index"),
    )
    if formats and answer != UNAVAILABLE_RESPONSE:
        answer = _sprint_document(answer, formats, owner_id, query)
    return {
        "answer": answer,
        "source_type": "unavailable" if answer == UNAVAILABLE_RESPONSE else "sprint",
        "verification_status": "unavailable" if answer == UNAVAILABLE_RESPONSE else "unverified",
        "grounding_score": None,
    }


@router.post("")
@answer_capacity
def send_message(
    chat_id: str,
    msg_in: MessageCreate,
    stream: bool = Query(False, description="Enable Server-Sent Events (SSE) streaming"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    chat = verify_chat_ownership(chat_id, current_user.id, db)
    _require_tokens(db, current_user)

    attachments_data = [att.model_dump() for att in (msg_in.attachments or [])]
    user_content = msg_in.content.strip()
    if attachments_data and not user_content.startswith("[Attached"):
        doc_summary = ", ".join([f"{a.get('name', 'Document')} ({a.get('page_count', 1)} pages)" for a in attachments_data])
        user_content = f"[Attached: {doc_summary}]\n\n{user_content}"

    regeneration_index, previous_answer = _resend_regeneration(db, chat_id, user_content, bool(msg_in.is_regeneration))
    is_regen = regeneration_index > 0
    _log_prompt("New question received", chat_id, current_user, msg_in.mode, len(user_content),
                regeneration_index, previous_answer)

    # 1. Save user message
    user_msg = Message(
        chat_id=chat_id,
        role="user",
        content=user_content,
        embedding=None if msg_in.mode == "sprint" else conversation_memory_service.embed_message_content(user_content)
    )
    persist_uploaded_attachments(user_msg, attachments_data)
    db.add(user_msg)

    # Auto-generate chat title from first question if default
    if chat.title == "New SAP Chat" or not chat.title:
        words = msg_in.content.strip().split()
        chat.title = " ".join(words[:6]) + ("..." if len(words) > 6 else "")

    chat.updated_at = datetime.now(timezone.utc)
    db.commit()
    if msg_in.mode == "sprint":
        db.refresh(user_msg, attribute_names=["id", "created_at"])
    else:
        db.refresh(user_msg)

    # Fetch recent conversation history (max 20 messages, excluding the new user_msg)
    sprint_memory = None
    if msg_in.mode == "sprint":
        chat_history = _plain_chat_history(db, chat_id, exclude_message_id=user_msg.id)
        request_attachments = attachments_data
        sprint_memory = _sprint_memory(
            db, chat_id, current_user.id, msg_in.content.strip(), chat_history, user_message_id=user_msg.id
        )
    else:
        chat_history = conversation_memory_service.get_recent_messages(
            db, chat_id, limit=20, exclude_message_id=user_msg.id
        )
        request_attachments = latest_chat_attachments(db, chat_id, user_msg.id, attachments_data)

    # 2. Check SSE streaming flow
    if stream:
        if msg_in.mode == "sprint":
            return handle_sprint_streaming_response(
                chat_id, msg_in.content.strip(), chat_history=chat_history,
                current_user=current_user, attachments=request_attachments, user_message_id=user_msg.id,
                memory=sprint_memory, conversation_chars=_conversation_chars(db, chat_id, user_msg.id),
                is_regeneration=is_regen,
                regeneration_index=regeneration_index,
            )
        return handle_streaming_response(
            chat_id, msg_in.content.strip(), chat_history=chat_history, current_user=current_user,
            attachments=request_attachments, user_message_id=user_msg.id,
            conversation_chars=_conversation_chars(db, chat_id, user_msg.id),
            is_regeneration=is_regen,
            regeneration_index=regeneration_index,
        )

    # 3. Sprint makes a direct configured-LLM call; Scholar retains the existing RAG pipeline.
    llm_info: Dict[str, Any] = {}
    _gen_start_time = time.perf_counter()
    with generation_context(regeneration_index, llm_info):
        if msg_in.mode == "sprint":
            rag_result = _sprint_result(msg_in.content.strip(), chat_history, request_attachments, memory=sprint_memory, owner_id=current_user.id)
            rag_result.update({"citations": [], "web_sources": [], "images": [], "follow_up_questions": []})
        else:
            rag_result = _invoke_rag_process(
                db, msg_in.content.strip(),
                chat_history=chat_history,
                user=current_user,
                attachments=request_attachments,
                chat_id=chat_id,
                regeneration_index=regeneration_index,
            )

    used_provider, used_model = _answering_llm(rag_result, llm_info)
    _total_ms = round((time.perf_counter() - _gen_start_time) * 1000, 1)
    status_label = f"completed (regeneration #{regeneration_index})" if is_regen else "completed"
    logger.info(
        f"[CHAT] Answer generation {status_label} in {_total_ms}ms | Provider: {used_provider} | Model: {used_model} | Status: completed"
    )

    # 4. Save assistant message
    assistant_msg = Message(
        chat_id=chat_id,
        role="assistant",
        content=rag_result["answer"],
        source_type=rag_result["source_type"],
        grounding_score=rag_result["grounding_score"],
        llm_provider=used_provider,
        llm_model=used_model,
        regeneration_count=regeneration_index if is_regen else 0,
        embedding=None if msg_in.mode == "sprint" else conversation_memory_service.embed_message_content(rag_result["answer"])
    )
    db.add(assistant_msg)
    db.commit()
    if msg_in.mode == "sprint":
        db.refresh(assistant_msg, attribute_names=["id", "created_at"])
    else:
        db.refresh(assistant_msg)

    # 5. Persist citations, web sources and web images
    rows = source_rows(
        assistant_msg.id, rag_result.get("citations", []), rag_result.get("web_sources", []), rag_result.get("images")
    )
    if rows:
        db.add_all(rows)
        db.commit()

    # 6. Incrementally maintain conversation summary (both modes; it reads text only, no embeddings)
    try:
        conversation_memory_service.update_rolling_summary(
            db, chat_id, user_content, rag_result["answer"]
        )
    except Exception as sum_err:
        logger.warning(f"Failed to update rolling summary for chat {chat_id}: {sum_err}")

    if msg_in.mode == "sprint":
        db.refresh(assistant_msg, attribute_names=["id", "created_at"])
    else:
        db.refresh(assistant_msg)

    is_admin = bool(current_user.is_admin)

    return MessageResponse(
        id=assistant_msg.id,
        chat_id=assistant_msg.chat_id,
        role=assistant_msg.role,
        content=visible_content(assistant_msg.content, is_admin),
        source_type=assistant_msg.source_type if is_admin else None,
        grounding_score=assistant_msg.grounding_score if is_admin else None,
        is_in_rag_pipeline=rag_result.get("is_in_rag_pipeline") if is_admin else None,
        kb_score=rag_result.get("kb_score") if is_admin else None,
        web_score=rag_result.get("web_score") if is_admin else None,
        winning_score=rag_result.get("winning_score") if is_admin else None,
        verification_status=(rag_result.get("verification_status") if is_admin or rag_result.get("verification_status") == "unavailable" else None),
        internal_evidence_confidence=rag_result.get("internal_evidence_confidence") if is_admin else None,
        selected_evidence_quality=rag_result.get("selected_evidence_quality") if is_admin else None,
        external_search_used=rag_result.get("external_search_used", False) if is_admin else False,
        created_at=assistant_msg.created_at,
        llm_provider=assistant_msg.llm_provider if is_admin else None,
        llm_model=assistant_msg.llm_model if is_admin else None,
        regeneration_count=assistant_msg.regeneration_count or 0,
        citations=[
            {
                "id": c.id,
                "document": c.document_name,
                "page": c.page_number,
                "section": c.section,
                "score": c.similarity_score,
                "snippet": c.citation_text
            }
            for c in assistant_msg.citations
        ] if is_admin else [],
        web_sources=[
            {
                "id": w.id,
                "title": w.title,
                "url": w.url,
                "domain": w.domain,
                "snippet": w.snippet
            }
            for w in assistant_msg.web_sources
        ] if is_admin else [],
        images=[
            {
                "url": image.url,
                "description": image.description,
                "alt_text": image.alt_text,
                "source_url": image.source_url if is_admin else None,
                "source_title": image.source_title if is_admin else None,
                "relevance_score": None,
            }
            for image in assistant_msg.images
        ],
        follow_up_questions=rag_result.get("follow_up_questions", [])
    )


@router.post("/stream")
@answer_capacity
def send_message_stream(
    chat_id: str,
    msg_in: MessageCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Direct POST /chats/{chat_id}/messages/stream endpoint for SSE streaming."""
    chat = verify_chat_ownership(chat_id, current_user.id, db)
    _require_tokens(db, current_user)

    attachments_data = [att.model_dump() for att in (msg_in.attachments or [])]
    user_content = msg_in.content.strip()
    if attachments_data and not user_content.startswith("[Attached"):
        doc_summary = ", ".join([f"{a.get('name', 'Document')} ({a.get('page_count', 1)} pages)" for a in attachments_data])
        user_content = f"[Attached: {doc_summary}]\n\n{user_content}"

    regeneration_index, previous_answer = _resend_regeneration(db, chat_id, user_content, bool(msg_in.is_regeneration))
    is_regen = regeneration_index > 0
    _log_prompt("New question received", chat_id, current_user, msg_in.mode, len(user_content),
                regeneration_index, previous_answer)

    # 1. Save user message
    user_msg = Message(
        chat_id=chat_id,
        role="user",
        content=user_content,
        embedding=None if msg_in.mode == "sprint" else conversation_memory_service.embed_message_content(user_content)
    )
    persist_uploaded_attachments(user_msg, attachments_data)
    db.add(user_msg)

    if chat.title == "New SAP Chat" or not chat.title:
        words = msg_in.content.strip().split()
        chat.title = " ".join(words[:6]) + ("..." if len(words) > 6 else "")

    chat.updated_at = datetime.now(timezone.utc)
    db.commit()
    if msg_in.mode == "sprint":
        db.refresh(user_msg, attribute_names=["id", "created_at"])
    else:
        db.refresh(user_msg)

    if msg_in.mode == "sprint":
        chat_history = _plain_chat_history(db, chat_id, exclude_message_id=user_msg.id)
        request_attachments = attachments_data
        return handle_sprint_streaming_response(
            chat_id, msg_in.content.strip(), chat_history=chat_history,
            current_user=current_user, attachments=request_attachments, user_message_id=user_msg.id,
            memory=_sprint_memory(
                db, chat_id, current_user.id, msg_in.content.strip(), chat_history, user_message_id=user_msg.id
            ),
            conversation_chars=_conversation_chars(db, chat_id, user_msg.id),
            is_regeneration=is_regen,
            regeneration_index=regeneration_index,
        )

    chat_history = conversation_memory_service.get_recent_messages(
        db, chat_id, limit=20, exclude_message_id=user_msg.id
    )
    rag_attachments = latest_chat_attachments(db, chat_id, user_msg.id, attachments_data)
    return handle_streaming_response(
        chat_id, msg_in.content.strip(), chat_history=chat_history, current_user=current_user,
        attachments=rag_attachments, user_message_id=user_msg.id,
        conversation_chars=_conversation_chars(db, chat_id, user_msg.id),
        is_regeneration=is_regen,
        regeneration_index=regeneration_index,
    )


@router.post("/{message_id}/edit")
@answer_capacity
def edit_message_stream(
    chat_id: str,
    message_id: str,
    msg_in: MessageCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Replace a user prompt, drop every later message, and stream a fresh answer (SSE)."""
    chat = verify_chat_ownership(chat_id, current_user.id, db)
    _require_tokens(db, current_user)
    user_msg = (
        db.query(Message)
        .options(*(
            [load_only(Message.id, Message.chat_id, Message.role, Message.content, Message.created_at)]
            if msg_in.mode == "sprint" else []
        ))
        .filter(Message.id == message_id, Message.chat_id == chat_id, Message.role == "user")
        .first()
    )
    if not user_msg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")

    new_text = msg_in.content.strip()
    if not new_text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Prompt cannot be empty")

    # The same question again (the Regenerate button), or a rewrite of a disliked answer's question,
    # regenerates: the next answer comes from a different provider/model.
    clean_original = ATTACHED_PREFIX.sub("", user_msg.content or "").strip()
    is_same_question = new_text in {clean_original, (user_msg.content or "").strip()}
    later_assistant = (
        db.query(Message)
        .options(_ANSWER_REGENERATION_COLUMNS)
        .filter(Message.chat_id == chat_id, Message.created_at > user_msg.created_at, Message.role == "assistant")
        .order_by(Message.created_at.asc())
        .first()
    )
    user_disliked = later_assistant is not None and later_assistant.feedback == "down"
    is_regen = bool(msg_in.is_regeneration or is_same_question or user_disliked)
    regeneration_index = _next_regeneration_index(later_assistant) if is_regen else 0
    _log_prompt("Question edited", chat_id, current_user, msg_in.mode, len(new_text),
                regeneration_index, later_assistant)

    # The old answer and any follow-up turns were built on the old prompt, so they go.
    for later in (
        db.query(Message)
        .options(*([load_only(Message.id)] if msg_in.mode == "sprint" else []))
        .filter(Message.chat_id == chat_id, Message.created_at > user_msg.created_at)
        .all()
    ):
        db.delete(later)

    # Files attached to the original prompt stay attached to the edited one.
    own_attachments = [
        {
            "name": item.name,
            "file_type": item.file_type,
            "page_count": item.page_count,
            "text": item.extracted_text,
        }
        for item in user_msg.uploaded_attachments
    ]
    if own_attachments:
        doc_summary = ", ".join(f"{a['name']} ({a['page_count'] or 1} pages)" for a in own_attachments)
        user_msg.content = f"[Attached: {doc_summary}]\n\n{new_text}"
    else:
        user_msg.content = new_text

    user_msg.embedding = None if msg_in.mode == "sprint" else conversation_memory_service.embed_message_content(user_msg.content)
    chat.updated_at = datetime.now(timezone.utc)
    db.commit()

    if msg_in.mode == "sprint":
        chat_history = _plain_chat_history(db, chat_id, exclude_message_id=user_msg.id)
        return handle_sprint_streaming_response(
            chat_id, new_text, chat_history=chat_history,
            current_user=current_user, attachments=own_attachments, user_message_id=user_msg.id,
            memory=_sprint_memory(db, chat_id, current_user.id, new_text, chat_history, user_message_id=user_msg.id),
            conversation_chars=_conversation_chars(db, chat_id, user_msg.id),
            is_regeneration=is_regen,
            regeneration_index=regeneration_index,
        )

    chat_history = conversation_memory_service.get_recent_messages(
        db, chat_id, limit=20, exclude_message_id=user_msg.id
    )
    rag_attachments = latest_chat_attachments(db, chat_id, user_msg.id, own_attachments)
    return handle_streaming_response(
        chat_id, new_text, chat_history=chat_history, current_user=current_user,
        attachments=rag_attachments, user_message_id=user_msg.id,
        conversation_chars=_conversation_chars(db, chat_id, user_msg.id),
        is_regeneration=is_regen,
        regeneration_index=regeneration_index,
    )


@router.post("/{message_id}/share")
def share_prompt(
    chat_id: str,
    message_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create (or refresh) the public read-only link for one of the user's prompts or answers."""
    verify_chat_ownership(chat_id, current_user.id, db)
    msg = (
        db.query(Message)
        .filter(Message.id == message_id, Message.chat_id == chat_id, Message.role.in_(("user", "assistant")))
        .first()
    )
    if not msg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")

    answer = None
    question_msg = msg
    if msg.role == "assistant" and (msg.source_type or "").startswith("interrupted"):
        # Stopped answers are partial and their text came from the client; never publish them (F-37).
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Interrupted answers cannot be shared")
    if msg.role == "assistant":
        # An answer is shared together with the question it answers.
        question_msg = (
            db.query(Message)
            .filter(Message.chat_id == chat_id, Message.role == "user", Message.created_at < msg.created_at)
            .order_by(Message.created_at.desc())
            .first()
        )
        if not question_msg:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
        # The public page has no sources, so evidence markers are always stripped.
        answer = strip_markers(msg.content or "").strip()

    # Attachment names and extracted file text stay private.
    content = ATTACHED_PREFIX.sub("", question_msg.content, count=1).strip()
    # One atomic upsert: two concurrent shares of the same message get the same link
    # instead of the second failing on the unique message_id.
    token = db.execute(
        pg_insert(SharedPrompt)
        .values(message_id=msg.id, content=content, answer=answer)
        .on_conflict_do_update(
            index_elements=[SharedPrompt.message_id],
            set_={"content": content, "answer": answer, "revoked": False},
        )
        .returning(SharedPrompt.token)
    ).scalar_one()
    db.commit()
    audit_log.record("share_create", request=request, principal=current_user, target=msg.id,
                     details={"includes_answer": answer is not None})
    return {"token": token}


@router.delete("/{message_id}/share", status_code=status.HTTP_204_NO_CONTENT)
def revoke_shared_prompt(
    chat_id: str,
    message_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    verify_chat_ownership(chat_id, current_user.id, db)
    shared = db.query(SharedPrompt).join(Message, SharedPrompt.message_id == Message.id).filter(
        SharedPrompt.message_id == message_id, Message.chat_id == chat_id
    ).first()
    if not shared:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shared prompt not found")
    shared.revoked = True
    db.commit()
    audit_log.record("share_revoke", request=request, principal=current_user, target=message_id)


@router.put("/{message_id}/feedback")
def set_message_feedback(
    chat_id: str,
    message_id: str,
    feedback_in: MessageFeedback,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Record a thumbs up/down on one of the user's answers; a null value clears it."""
    verify_chat_ownership(chat_id, current_user.id, db)
    answer = (
        db.query(Message)
        .filter(Message.id == message_id, Message.chat_id == chat_id, Message.role == "assistant")
        .first()
    )
    if not answer:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    answer.feedback = feedback_in.value
    db.commit()
    return {"message_id": answer.id, "feedback": answer.feedback}


@router.post("/save-partial")
def save_partial_message(
    chat_id: str,
    msg_in: SavePartialRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Saves what the user saw of an answer they stopped. Requires the stream id the server issued to this
    user for this chat, and accepts it once, so arbitrary "assistant" text cannot be stored (F-37)."""
    chat = verify_chat_ownership(chat_id, current_user.id, db)
    nonce = verify_stream_token(msg_in.stream_id, current_user.id, chat_id)
    if nonce is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unknown or expired stream")
    # The user pressed Stop: the answer would otherwise keep generating in the background and be saved.
    stream_cancellations.cancel(msg_in.stream_id)
    if not rate_limiter.backend.hit(f"partial-saved:{nonce}", 1, STREAM_TOKEN_TTL_SECONDS):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This stream was already saved")
    partial_msg = Message(
        chat_id=chat_id,
        role="assistant",
        content=msg_in.content.strip(),
        source_type="interrupted",
        grounding_score=0.0
    )
    db.add(partial_msg)
    chat.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(partial_msg)
    return {"status": "saved", "message_id": partial_msg.id}


def handle_streaming_response(
    chat_id: str,
    query: str,
    db: Session = None,
    chat_history: List[Dict[str, Any]] = None,
    current_user: User = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
    user_message_id: Optional[str] = None,
    conversation_chars: Optional[int] = None,
    is_regeneration: bool = False,
    regeneration_index: int = 0,
):
    """
    Generates Server-Sent Events (SSE) stream using the complete verified RAG pipeline.
    Progressively yields structured status stages, LLM token stream, and done event with citations/grounding.
    Persists the completed assistant message in the database once generation concludes.
    `usage` events carry the running token estimate; the done event carries `token_usage`.
    """
    regeneration = regeneration_index if is_regeneration else 0
    # Scholar chats too: "save it" / "remember that ..." is stored and confirmed without the RAG pipeline.
    if user_memory.parse_command(query) is not None:
        with SessionLocal() as command_db:
            history = chat_history if chat_history is not None else conversation_memory_service.get_recent_messages(
                command_db, chat_id, limit=20, exclude_message_id=user_message_id)
            reply = user_memory.handle_command(command_db, current_user.id, chat_id, query, history or [])
        if reply is not None:
            return handle_sprint_streaming_response(
                chat_id, query, chat_history=chat_history, current_user=current_user, attachments=attachments,
                user_message_id=user_message_id,
                memory={"intent": "MEMORY_COMMAND", "answer": reply, "summary": None, "recalled": []},
                conversation_chars=conversation_chars, is_regeneration=is_regeneration,
                regeneration_index=regeneration_index,
            )
    # Stream id: the only credential save-partial accepts for text shown in this stream (F-37); also how Stop
    # cancels the answer, which otherwise finishes and is saved even if the browser disconnects.
    stream_id = issue_stream_token(current_user.id, chat_id)

    def event_stream():
        _stream_start = time.perf_counter()
        save_db = SessionLocal()
        accumulated_text = ""
        done_payload = None
        marker_stripper = None if current_user.is_admin else StreamingMarkerStripper()
        meter = token_meter.TokenMeter()
        reported_tokens = 0
        llm_info: Dict[str, Any] = {}

        yield f"data: {json.dumps({'type': 'stream', 'stream_id': stream_id})}\n\n"

        # Lets the client swap its optimistic prompt for the stored one (needed to edit it later).
        if user_message_id:
            yield f"data: {json.dumps({'type': 'user_message', 'message_id': user_message_id})}\n\n"

        hist = chat_history
        if hist is None:
            hist = conversation_memory_service.get_recent_messages(
                save_db, chat_id, limit=20, exclude_message_id=user_message_id
            )

        try:
            memory_text = _saved_memory_text(save_db, current_user.id, query)
            # Another language: ask the pipeline in English (SAP-topic check and retrieval work on English),
            # verify the English draft, then translate the finished answer for the user.
            language = languages.normalize(getattr(current_user, "preferred_language", None))
            translate_answer = not languages.is_english(language)
            rag_query = languages.question_to_english(query, languages.question_language_of(current_user))
            rag_events = user_memory.scoped(with_generation_context(_invoke_rag_stream(
                save_db, rag_query, chat_history=hist, user=current_user, attachments=attachments, chat_id=chat_id,
                regeneration_index=regeneration,
            ), regeneration, llm_info), memory_text)
            for event in token_meter.metered(rag_events, meter):
                if meter.used_tokens != reported_tokens:
                    reported_tokens = meter.used_tokens
                    yield _usage_event(reported_tokens)
                event_type = event.get("type")

                if event_type == "status":
                    if not current_user.is_admin:
                        event["message"] = "Thinking..." if event.get("stage") != "generating" else "Generating response..."

                elif event_type == "token":
                    accumulated_text += event.get("content", "")
                    if translate_answer:
                        continue  # the verified English draft is translated and shown once it is complete
                    if marker_stripper is not None:
                        event["content"] = marker_stripper.feed(event.get("content", ""))
                        if not event["content"]:
                            continue

                elif event_type == "done":
                    if translate_answer:
                        translated = languages.answer_to_language(event.get("answer") or accumulated_text, language)
                        event["answer"] = translated
                        accumulated_text = translated
                        for start in range(0, len(translated), 48):
                            piece = translated[start:start + 48]
                            shown = marker_stripper.feed(piece) if marker_stripper is not None else piece
                            if shown:
                                yield f"data: {json.dumps({'type': 'token', 'content': shown})}\n\n"
                    if marker_stripper is not None:
                        leftover = marker_stripper.flush()
                        if leftover:
                            yield f"data: {json.dumps({'type': 'token', 'content': leftover})}\n\n"
                    done_payload = event
                    full_answer = event.get("answer", accumulated_text)
                    source_type = event.get("source_type", "knowledge_base")
                    grounding = event.get("grounding_score", 0.0)
                    citations = event.get("citations", [])
                    web_sources = event.get("web_sources", [])
                    images = event.get("images", [])

                    # Anti-duplicate persistence check:
                    # Check if an interrupted or duplicate message was already saved for this turn
                    last_msg = (
                        save_db.query(Message)
                        .filter(Message.chat_id == chat_id)
                        .order_by(Message.created_at.desc())
                        .first()
                    )
                    used_provider, used_model = _answering_llm(event, llm_info)
                    # A declined (out-of-scope) question costs nothing and shows no savings figures.
                    token_usage = None if source_type == "refusal" else meter.summary(
                        question=query, answer=full_answer,
                        conversation_chars=_history_chars(hist) if conversation_chars is None else conversation_chars,
                    )
                    if last_msg and last_msg.role == "assistant" and last_msg.source_type == "interrupted":
                        # Upgrade the interrupted message to the final verified answer
                        assistant_msg = last_msg
                        assistant_msg.content = full_answer
                        assistant_msg.source_type = source_type
                        assistant_msg.grounding_score = grounding
                        assistant_msg.llm_provider = used_provider
                        assistant_msg.llm_model = used_model
                        assistant_msg.regeneration_count = regeneration
                        assistant_msg.tokens_used = token_usage["used"] if token_usage else None
                        assistant_msg.tokens_baseline = token_usage["baseline"] if token_usage else None
                        assistant_msg.embedding = conversation_memory_service.embed_message_content(full_answer)
                        save_db.commit()
                    else:
                        assistant_msg = Message(
                            chat_id=chat_id,
                            role="assistant",
                            content=full_answer,
                            source_type=source_type,
                            grounding_score=grounding,
                            llm_provider=used_provider,
                            llm_model=used_model,
                            regeneration_count=regeneration,
                            tokens_used=token_usage["used"] if token_usage else None,
                            tokens_baseline=token_usage["baseline"] if token_usage else None,
                            embedding=conversation_memory_service.embed_message_content(full_answer)
                        )
                        save_db.add(assistant_msg)
                        save_db.commit()
                        save_db.refresh(assistant_msg)

                    rows = source_rows(assistant_msg.id, citations, web_sources, images)
                    if rows:
                        save_db.add_all(rows)
                        save_db.commit()

                    # Incrementally update conversation summary
                    try:
                        conversation_memory_service.update_rolling_summary(
                            save_db, chat_id, query, full_answer
                        )
                    except Exception as sum_err:
                        logger.warning(f"Failed to update rolling summary for chat {chat_id}: {sum_err}")

                    _total_ms = round((time.perf_counter() - _stream_start) * 1000, 1)
                    status_label = f"completed (regeneration #{regeneration})" if regeneration else "completed"
                    logger.info(
                        f"[CHAT] Answer generation {status_label} in {_total_ms}ms | Provider: {used_provider} | Model: {used_model} | Grounding: {round(grounding, 3) if grounding else 'N/A'} | Status: completed"
                    )

                    event["message_id"] = assistant_msg.id
                    if token_usage is not None:
                        event["token_usage"] = token_usage
                        balance = _charge_tokens(save_db, current_user, token_usage)
                        if balance is not None:
                            event["token_balance"] = balance
                    event["llm_provider"] = used_provider
                    event["llm_model"] = used_model
                    event["regeneration_count"] = regeneration

                    if not current_user.is_admin:
                        # Allowlist: diagnostics, model names, scores and audits stay admin-only.
                        event = public_done_event(event)

                # SSE Event Formats:
                # 1. Main JSON payload: data: {"type": ...}\n\n
                yield f"data: {json.dumps(event)}\n\n"


        except Exception as e:
            logger.error(f"Streaming generation error: {e}", exc_info=True)
            err_event = {
                "type": "error",
                "message": "Generation interrupted. Please try again."
            }
            yield f"data: {json.dumps(err_event)}\n\n"

        finally:
            # A stream that ends before 'done' never finished claim verification, so its text is
            # not stored as an answer (AUDIT_REPORT F-03). The user can simply ask again.
            if not done_payload and accumulated_text.strip():
                logger.info("Stream for chat %s ended before verification; partial text not persisted", chat_id)
            save_db.close()

    return StreamingResponse(
        detached(event_stream(), stream_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


def handle_sprint_streaming_response(
    chat_id: str,
    query: str,
    chat_history: Optional[List[Dict[str, Any]]] = None,
    current_user: User = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
    user_message_id: Optional[str] = None,
    memory: Optional[Dict[str, Any]] = None,
    conversation_chars: Optional[int] = None,
    is_regeneration: bool = False,
    regeneration_index: int = 0,
):
    """Direct LLM stream and persistence path. It intentionally never calls RAG or embedding services.
    `memory` comes from _sprint_memory: a final memory answer, or summary/recalled context for the LLM."""
    memory = memory or {}
    regeneration = regeneration_index if is_regeneration else 0
    stream_id = issue_stream_token(current_user.id, chat_id)  # see handle_streaming_response

    def event_stream():
        _sprint_start = time.perf_counter()
        save_db = SessionLocal()
        accumulated_text = ""
        marker_stripper = None if current_user.is_admin else StreamingMarkerStripper()
        done_payload = False
        meter = token_meter.TokenMeter()
        llm_info: Dict[str, Any] = {}

        yield f"data: {json.dumps({'type': 'stream', 'stream_id': stream_id})}\n\n"
        if user_message_id:
            yield f"data: {json.dumps({'type': 'user_message', 'message_id': user_message_id})}\n\n"

        hist = chat_history if chat_history is not None else _plain_chat_history(
            save_db, chat_id, exclude_message_id=user_message_id
        )
        try:
            yield f"data: {json.dumps({'type': 'status', 'stage': 'generating', 'message': 'Generating response...'})}\n\n"
            memory_text = "" if memory.get("answer") else _saved_memory_text(save_db, current_user.id, query)
            language = languages.normalize(getattr(current_user, "preferred_language", None))
            # The SAP-topic check works on English; the model still gets the question as the user wrote it.
            scope_query = (languages.question_to_english(query, languages.question_language_of(current_user))
                           if not memory.get("answer") else query)
            # The LLM reads the intent first (on the question as the user wrote it, in any language).
            analysis = _sprint_analysis(query, hist, attachments, memory)
            route = _sprint_route(scope_query, hist, attachments, memory, analysis=analysis)
            if route == "refusal" and user_memory.matches_query(memory_text, query):
                route = "sprint"  # a question about what the user saved is not off-topic
            answer_query = analysis.corrected_question if analysis is not None else query
            doc_formats = _sprint_formats(query, analysis, attachments, memory) if route in ("sprint", "general") else []
            if doc_formats:  # the content goes into the file, not the chat
                answer_query = document_generator.query_with_instructions(answer_query, doc_formats)
                yield f"data: {json.dumps({'type': 'status', 'stage': 'generating', 'message': 'Creating your document...'})}\n\n"
            if route in ("sprint", "conversational", "general"):
                if route == "sprint":
                    chunks = llm_service.stream_sprint_reply(
                        answer_query, chat_history=hist, attachments=attachments,
                        summary=memory.get("summary"), recalled_messages=memory.get("recalled"),
                        regeneration_index=regeneration,
                    )
                elif route == "general":
                    chunks = llm_service.stream_general_reply(
                        answer_query, chat_history=hist, regeneration_index=regeneration,
                    )
                else:
                    chunks = llm_service.stream_emotional_casual_reply(
                        answer_query, chat_history=hist, system_prompt=SPRINT_CASUAL_SYSTEM_PROMPT,
                        regeneration_index=regeneration,
                    )
                reported_tokens = 0
                for chunk in token_meter.metered(
                        languages.scoped(
                            user_memory.scoped(with_generation_context(chunks, regeneration, llm_info), memory_text),
                            language), meter):
                    if meter.used_tokens != reported_tokens:
                        reported_tokens = meter.used_tokens
                        yield _usage_event(reported_tokens)
                    accumulated_text += chunk
                    if doc_formats:
                        continue
                    visible = marker_stripper.feed(chunk) if marker_stripper is not None else chunk
                    if visible:
                        yield f"data: {json.dumps({'type': 'token', 'content': visible})}\n\n"
                if doc_formats and strip_markers(accumulated_text).strip() not in ("", UNAVAILABLE_RESPONSE):
                    accumulated_text = _sprint_document(accumulated_text, doc_formats, current_user.id, query)
                    yield f"data: {json.dumps({'type': 'token', 'content': accumulated_text})}\n\n"
                elif doc_formats:
                    yield f"data: {json.dumps({'type': 'token', 'content': strip_markers(accumulated_text)})}\n\n"
                answer = strip_markers(accumulated_text).strip()
                source_type = "unavailable" if answer == UNAVAILABLE_RESPONSE else (
                    "direct_llm" if route == "general" else route)
                verification_status = "unavailable" if source_type == "unavailable" else "unverified"
            else:
                # A memory lookup answered from the database, a clarifying question, or the SAP-scope refusal.
                if route == "conversation_memory":
                    answer = memory["answer"]
                elif route == "company":
                    answer = COMPANY_ANSWER
                elif route == "clarification":
                    answer = analysis.clarification_question
                else:
                    answer = NON_SAP_REFUSAL_RESPONSE
                source_type = "conversational" if route in ("company", "clarification") else route
                verification_status = "unverified"
                accumulated_text = answer
                visible = marker_stripper.feed(answer) if marker_stripper is not None else answer
                if visible:
                    yield f"data: {json.dumps({'type': 'token', 'content': visible})}\n\n"

            if marker_stripper is not None:
                leftover = marker_stripper.flush()
                if leftover:
                    yield f"data: {json.dumps({'type': 'token', 'content': leftover})}\n\n"

            # Memory answers and refusals call no LLM, so they record no provider or model.
            used_provider, used_model = llm_info.get("provider"), llm_info.get("model")
            # A declined (out-of-scope) question costs nothing and shows no savings figures.
            token_usage = None if source_type == "refusal" else meter.summary(
                question=query, answer=answer,
                conversation_chars=_history_chars(hist) if conversation_chars is None else conversation_chars,
            )

            assistant_msg = Message(
                chat_id=chat_id,
                role="assistant",
                content=answer,
                source_type=source_type,
                grounding_score=None,
                embedding=None,
                llm_provider=used_provider,
                llm_model=used_model,
                regeneration_count=regeneration,
                tokens_used=token_usage["used"] if token_usage else None,
                tokens_baseline=token_usage["baseline"] if token_usage else None,
            )
            save_db.add(assistant_msg)
            save_db.commit()
            save_db.refresh(assistant_msg, attribute_names=["id", "created_at"])

            try:
                conversation_memory_service.update_rolling_summary(save_db, chat_id, query, answer)
            except Exception as sum_err:
                logger.warning("Failed to update rolling summary for chat %s (%s)", chat_id, type(sum_err).__name__)

            _sprint_ms = round((time.perf_counter() - _sprint_start) * 1000, 1)
            status_label = f"completed (regeneration #{regeneration})" if regeneration else "completed"
            logger.info(
                f"[CHAT] Sprint answer generation {status_label} in {_sprint_ms}ms | Provider: {used_provider} | Model: {used_model} | Status: completed"
            )

            done_payload = True
            event = {
                "type": "done",
                "answer": answer,
                "message_id": assistant_msg.id,
                "source_type": source_type,
                "grounding_score": None,
                "llm_provider": used_provider,
                "llm_model": used_model,
                "regeneration_count": regeneration,
                "citations": [],
                "web_sources": [],
                "images": [],
                "verification_status": verification_status,
                "is_in_rag_pipeline": False,
                "external_search_used": False,
                "follow_up_questions": [],
            }
            if token_usage is not None:
                event["token_usage"] = token_usage
                balance = _charge_tokens(save_db, current_user, token_usage)
                if balance is not None:
                    event["token_balance"] = balance
            if not current_user.is_admin:
                event = public_done_event(event)
            yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            logger.error("Sprint streaming generation failed (%s)", type(exc).__name__, exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'message': 'Generation interrupted. Please try again.'})}\n\n"
        finally:
            if not done_payload and accumulated_text.strip():
                logger.info("Sprint stream for chat %s ended before completion; partial text was not persisted", chat_id)
            save_db.close()

    return StreamingResponse(
        detached(event_stream(), stream_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
