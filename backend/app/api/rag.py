import json
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.orm import Session
from datetime import datetime, timezone

from backend.app.database import get_db
from backend.app.schemas.rag import RAGQueryRequest, RAGQueryResponse
from backend.app.services.rag_service import rag_service
from backend.app.services.conversation_memory_service import conversation_memory_service
from backend.app.models.user import User
from backend.app.models.chat import Chat
from backend.app.models.message import Message
from backend.app.security.auth import get_current_user
from backend.app.services.evidence_markers import StreamingMarkerStripper, strip_markers
from backend.app.services.public_payload import public_done_event
from backend.app.services.llm_service import RequestDeadline, current_deadline, deadline_scope
from backend.app.config import settings
from backend.app.services.capacity import answer_capacity


router = APIRouter(prefix="/rag", tags=["RAG"])


def _public_result(result: dict, is_admin: bool) -> dict:
    """Strip evidence, scoring and comparison internals from regular-user payloads."""
    if is_admin:
        return result
    safe = dict(result)
    for key in ("citations", "web_sources", "source_scores", "comparison_audit", "kb_score", "web_score",
                "winning_score", "internal_evidence_confidence", "selected_evidence_quality",
                "selected_source", "selection_summary"):
        safe.pop(key, None)
    safe["answer"] = strip_markers(safe.get("answer", ""))
    return safe


@router.post("/query", response_model=RAGQueryResponse)
@answer_capacity
def query_rag(
    request: RAGQueryRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # 1. Verify that the chat belongs to the logged-in user
    chat = None

    if request.chat_id:
        chat = db.query(Chat).filter(
            Chat.id == request.chat_id,
            Chat.user_id == current_user.id
        ).first()

        if not chat:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Chat not found"
            )

    # 2. Save the user's message
    user_message = None
    if chat:
        user_message = Message(
            chat_id=chat.id,
            role="user",
            content=request.query,
            embedding=conversation_memory_service.embed_message_content(request.query)
        )

        db.add(user_message)
        db.commit()
        db.refresh(user_message)

    # 3. Run RAG with chat history (max 20 messages) if available
    chat_history = []
    if chat and user_message:
        chat_history = conversation_memory_service.get_recent_messages(
            db, chat.id, limit=20, exclude_message_id=user_message.id
        )

    try:
        if current_deadline() is None:
            seconds = max(1.0, float(getattr(settings, "LLM_REQUEST_DEADLINE_SECONDS", 120)))
            with deadline_scope(RequestDeadline(seconds=seconds)):
                result = rag_service.process_query(
                    db,
                    request.query,
                    chat_history=chat_history,
                    user=current_user,
                    chat_id=chat.id if chat else None
                )
        else:
            result = rag_service.process_query(
                db,
                request.query,
                chat_history=chat_history,
                user=current_user,
                chat_id=chat.id if chat else None
            )

    except Exception:
        db.rollback()

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while processing the SAP query."
        )

    # 4. Save assistant response
    assistant_message_id = None

    if chat:
        assistant_message = Message(
            chat_id=chat.id,
            role="assistant",
            content=result["answer"],
            source_type=result["source_type"],
            grounding_score=result["grounding_score"],
            embedding=conversation_memory_service.embed_message_content(result["answer"])
        )

        db.add(assistant_message)

        chat.updated_at = datetime.now(timezone.utc)

        db.commit()
        db.refresh(assistant_message)

        try:
            conversation_memory_service.update_rolling_summary(
                db, chat.id, request.query, result["answer"]
            )
        except Exception:
            pass

        assistant_message_id = assistant_message.id

    # 5. Return response
    is_admin = bool(getattr(current_user, "is_admin", False))
    if not is_admin:
        return JSONResponse(content={
            "answer": _public_result(result, False)["answer"],
            "source_type": result["source_type"],
            "grounding_score": result["grounding_score"],
            "verification_status": result.get("verification_status"),
            "external_search_used": result.get("external_search_used", False),
            "follow_up_questions": result.get("follow_up_questions", []),
            "chat_id": chat.id if chat else None,
            "message_id": assistant_message_id,
        })
    result = _public_result(result, True)
    return RAGQueryResponse(
        answer=result["answer"],
        source_type=result["source_type"],
        grounding_score=result["grounding_score"],
        is_in_rag_pipeline=result.get("is_in_rag_pipeline"),
        kb_score=result.get("kb_score"),
        web_score=result.get("web_score"),
        winning_score=result.get("winning_score"),
        source_scores=result.get("source_scores", {}),
        comparison_audit=result.get("comparison_audit"),
        selected_source=result.get("selected_source"),
        verification_status=result.get("verification_status", "verified"),
        internal_evidence_confidence=result.get("internal_evidence_confidence"),
        selected_evidence_quality=result.get("selected_evidence_quality"),
        external_search_used=result.get("external_search_used", False),
        citations=result.get("citations", []),
        web_sources=result.get("web_sources", []),
        images=result.get("images", []),
        follow_up_questions=result.get("follow_up_questions", []),
        selection_summary=result.get("selection_summary"),
        chat_id=chat.id if chat else None,
        message_id=assistant_message_id
    )


@router.post("/query/stream")
@answer_capacity
def query_rag_stream(
    request: RAGQueryRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    chat = None
    if request.chat_id:
        chat = db.query(Chat).filter(
            Chat.id == request.chat_id,
            Chat.user_id == current_user.id
        ).first()
        if not chat:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Chat not found"
            )

    user_message = None
    if chat:
        user_message = Message(
            chat_id=chat.id,
            role="user",
            content=request.query,
            embedding=conversation_memory_service.embed_message_content(request.query)
        )
        db.add(user_message)
        db.commit()
        db.refresh(user_message)

    chat_history = []
    if chat and user_message:
        chat_history = conversation_memory_service.get_recent_messages(
            db, chat.id, limit=20, exclude_message_id=user_message.id
        )

    def event_stream():
        accumulated_text = ""
        # Markers can be split across tokens; the shared stripper holds a possible marker back
        # until it is complete, so none ever reaches a regular user (AUDIT_REPORT F-38).
        stripper = None if getattr(current_user, "is_admin", False) else StreamingMarkerStripper()
        for event in rag_service.stream_query(
            db, request.query, chat_history=chat_history, user=current_user, chat_id=chat.id if chat else None
        ):
            if event.get("type") == "token":
                token_text = event.get("content", "")
                accumulated_text += token_text
                if stripper is not None:
                    event["content"] = stripper.feed(token_text)
                    if not event["content"]:
                        continue
            elif event.get("type") == "done" and stripper is not None:
                leftover = stripper.flush()
                if leftover:
                    yield f"data: {json.dumps({'type': 'token', 'content': leftover})}\n\n"
            if event.get("type") == "done" and chat:
                full_answer = event.get("answer", accumulated_text)
                assistant_message = Message(
                    chat_id=chat.id,
                    role="assistant",
                    content=full_answer,
                    source_type=event.get("source_type", "knowledge_base"),
                    grounding_score=event.get("grounding_score", 0.0),
                    embedding=conversation_memory_service.embed_message_content(full_answer)
                )
                db.add(assistant_message)
                chat.updated_at = datetime.now(timezone.utc)
                db.commit()
                db.refresh(assistant_message)
                try:
                    conversation_memory_service.update_rolling_summary(
                        db, chat.id, request.query, full_answer
                    )
                except Exception:
                    pass
                event["message_id"] = assistant_message.id
            if event.get("type") == "done" and not getattr(current_user, "is_admin", False):
                # Allowlist: diagnostics, model names, scores and audits stay admin-only.
                event = public_done_event(event)

            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )
