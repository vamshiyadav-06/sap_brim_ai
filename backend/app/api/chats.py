import logging
from typing import List, Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status, Query, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import func
from starlette.concurrency import run_in_threadpool

from backend.app.database import get_db
from backend.app.models.user import User
from backend.app.models.chat import Chat
from backend.app.models.message import Message
from backend.app.api.projects import own_project
from backend.app.schemas.chat import ChatCreate, ChatUpdate, ChatResponse, ChatListItem
from backend.app.schemas.message import DocumentExtractResponse, UrlExtractRequest
from backend.app.services.document_extractor import extract_document
from backend.app.services.url_reader import UrlReadError, read_url
from backend.app.services.image_vision_service import analyze_chat_image, is_chat_image
from backend.app.security.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chats", tags=["Chats"])

@router.post("", response_model=ChatResponse, status_code=status.HTTP_201_CREATED)
def create_chat(
    chat_in: ChatCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if chat_in.project_id:
        own_project(db, current_user, chat_in.project_id)  # 404 for another account's project
    chat = Chat(
        user_id=current_user.id,
        title=chat_in.title or "New SAP Chat",
        project_id=chat_in.project_id or None,
    )
    db.add(chat)
    db.commit()
    db.refresh(chat)
    return ChatResponse.model_validate(chat)

@router.get("", response_model=List[ChatListItem])
def list_chats(
    search: Optional[str] = Query(None, description="Search chats by title"),
    project_id: Optional[str] = Query(None, max_length=36, description="Only chats in this project"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    query = (
        db.query(
            Chat.id,
            Chat.user_id,
            Chat.title,
            Chat.project_id,
            Chat.created_at,
            Chat.updated_at,
            func.count(Message.id).label("message_count")
        )
        .outerjoin(Message, Message.chat_id == Chat.id)
        .filter(Chat.user_id == current_user.id)
    )

    if search:
        query = query.filter(Chat.title.ilike(f"%{search}%"))
    if project_id:
        query = query.filter(Chat.project_id == project_id)

    results = (
        query.group_by(Chat.id)
        .order_by(Chat.updated_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return [
        ChatListItem(
            id=r.id,
            user_id=r.user_id,
            title=r.title,
            project_id=r.project_id,
            created_at=r.created_at,
            updated_at=r.updated_at,
            message_count=r.message_count
        )
        for r in results
    ]

@router.get("/{chat_id}", response_model=ChatResponse)
def get_chat(
    chat_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # Strictly query by both chat_id and authenticated user_id to prevent enumeration or cross-user leaks
    chat = db.query(Chat).filter(
        Chat.id == chat_id,
        Chat.user_id == current_user.id
    ).first()

    if not chat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat not found"
        )

    return ChatResponse.model_validate(chat)

@router.patch("/{chat_id}", response_model=ChatResponse)
def update_chat(
    chat_id: str,
    chat_in: ChatUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    chat = db.query(Chat).filter(
        Chat.id == chat_id,
        Chat.user_id == current_user.id
    ).first()

    if not chat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat not found"
        )

    chat.title = chat_in.title.strip()
    chat.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(chat)
    return ChatResponse.model_validate(chat)

@router.delete("/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chat(
    chat_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    chat = db.query(Chat).filter(
        Chat.id == chat_id,
        Chat.user_id == current_user.id
    ).first()

    if not chat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat not found"
        )

    db.delete(chat)
    db.commit()
    return None

@router.post("/extract-document", response_model=DocumentExtractResponse)
async def extract_chat_document(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """
    Extracts text and page metadata from any readable upload: PDF, Word, PowerPoint, Excel, OpenDocument, HTML,
    RTF, ZIP archives, images, and text/data/source-code files (unreadable formats are refused with a reason).
    Images are read by a vision model (with OCR cross-check/fallback) so screenshots of errors
    and questions can be answered. Enforces a 25MB limit and returns structured content.
    """
    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No file uploaded")

    max_bytes = 10 * 1024 * 1024
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        await file.close()
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File size exceeds the 10MB limit."
        )
    await file.close()

    try:
        extractor = analyze_chat_image if is_chat_image(file.filename) else extract_document
        extracted = await run_in_threadpool(extractor, file.filename, content)
        if not extracted.get("text", "").strip():
            raise ValueError(f"No readable text could be extracted from '{file.filename}'.")
        return DocumentExtractResponse(**extracted)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error processing document {file.filename}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to extract document contents. Please try another supported file."
        )


@router.post("/extract-url", response_model=DocumentExtractResponse)
async def extract_chat_url(
    body: UrlExtractRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Reads a web page or online file the user linked (public http/https addresses only; see url_reader) and
    returns its text like an uploaded document, so the question can be answered from it.
    """
    try:
        extracted = await run_in_threadpool(read_url, body.url)
        return DocumentExtractResponse(**extracted)
    except UrlReadError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error("Error reading a linked page (%s)", type(e).__name__, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to read that link. Please try another one.",
        )
