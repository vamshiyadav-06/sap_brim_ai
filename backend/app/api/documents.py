import json
import logging
import asyncio
from typing import List, Optional, Dict, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status, UploadFile, File, Query, Header, Body
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.database import get_db
from backend.app.models.document import Document
from backend.app.models.chunk import DocumentChunk
from backend.app.models.user import User
from backend.app.security.access import normalize_groups
from backend.app.security.auth import oauth2_scheme, get_optional_current_user, admin_key_matches
from backend.app.services.retrieval_service import retrieval_service
from backend.app.services.document_processing_service import document_processing_service
from backend.app.services.document_processing_service import validate_upload_signature
from backend.app.security.uploads import UploadLimitError, spool_limited_upload
from backend.app.services import audit_log

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["Document Management"])


def authenticate_document_operator(
    request: Request,
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """
    Strictly enforces administrator privileges.
    Allows operations with valid X-Admin-Key header or authenticated admin JWT token.
    """
    if admin_key_matches(x_admin_key):
        audit_log.record("admin_key_used", request=request, actor_type="admin_key", target=request.url.path)
        return "admin_key"
    user = get_optional_current_user(token=token, db=db)
    if user and user.is_admin:
        return user

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Unauthorized: Document management is strictly restricted to authorized Super Administrators.",
    )


class ProcessRequest(BaseModel):
    force: bool = False
    document_ids: Optional[List[str]] = None


class AccessUpdate(BaseModel):
    allowed_groups: List[str] = Field(..., min_length=1, max_length=50)


class SourceTierUpdate(BaseModel):
    source_tier: Literal[
        "official_sap", "sap_partner_or_book", "official_sap_domain", "forum",
        "self_authored", "github_repo", "unknown",
    ]


@router.get("", summary="List knowledge base documents with statuses, filters, and chunk metrics")
def get_documents(
    status: Optional[str] = Query(None, description="Filter by status: all, pending, processing, completed, failed"),
    search: Optional[str] = Query(None, description="Search by filename or title"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    auth: Any = Depends(authenticate_document_operator),
):
    return document_processing_service.get_documents(
        db=db,
        status_filter=status,
        search=search,
        page=page,
        page_size=page_size,
    )


@router.get("/processing-status", summary="Get real-time live processing statistics and current document progress")
def get_processing_status(
    auth: Any = Depends(authenticate_document_operator),
):
    return document_processing_service.get_progress_status()


@router.get("/processing-stream", summary="Server-Sent Events (SSE) live stream for processing progress")
async def stream_processing_progress(
    request: Request,
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    header_token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
):
    effective_token = header_token
    is_authorized = False
    if admin_key_matches(x_admin_key):
        is_authorized = True
        audit_log.record("admin_key_used", request=request, actor_type="admin_key", target=request.url.path)
    elif effective_token:
        user = get_optional_current_user(token=effective_token, db=db)
        if user and user.is_admin:
            is_authorized = True

    if not is_authorized:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Unauthorized: Stream access is restricted to authorized administrators.",
        )

    async def event_generator():
        # The service uses a threading.Lock shared with the processing worker. Keep all
        # lock-taking calls off the event loop, including disconnect cleanup.
        loop = asyncio.get_running_loop()
        q = await asyncio.to_thread(document_processing_service.subscribe, loop)
        try:
            # Send immediate state on connect
            init_state = await asyncio.to_thread(document_processing_service.get_progress_status)
            yield f"data: {json.dumps(init_state)}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=12.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # Keepalive ping
                    yield ": ping\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            await asyncio.to_thread(document_processing_service.unsubscribe, q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )



@router.post("/upload", summary="Upload multiple documents through file picker", status_code=status.HTTP_201_CREATED)
async def upload_documents(
    request: Request,
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
    auth: Any = Depends(authenticate_document_operator),
):
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")

    registered = []
    errors = []
    total_uploaded_bytes = 0

    for file in files:
        spool = None
        try:
            spool, file_size, signature_prefix = await spool_limited_upload(
                file,
                max_file_bytes=settings.MAX_UPLOAD_BYTES,
                remaining_total_bytes=max(0, settings.MAX_TOTAL_UPLOAD_BYTES - total_uploaded_bytes),
            )
            total_uploaded_bytes += file_size
            validate_upload_signature(file.filename or "", signature_prefix)

            doc_info = document_processing_service.register_uploaded_file(
                filename=file.filename,
                file_bytes=spool,
                db=db,
            )
            registered.append(doc_info)
            audit_log.record("document_upload", request=request, principal=auth, target=doc_info.get("id"),
                             details={"filename": doc_info.get("filename"), "size_bytes": file_size})
        except (UploadLimitError, ValueError) as e:
            logger.warning("Document upload rejected (%s)", type(e).__name__)
            errors.append({"filename": file.filename, "error": "UPLOAD_REJECTED"})
            audit_log.record("document_upload", "failure", request=request, principal=auth,
                             details={"reason": type(e).__name__})
        except Exception as e:
            logger.error("Document upload failed (%s)", type(e).__name__, exc_info=True)
            errors.append({"filename": file.filename, "error": "UPLOAD_FAILED"})
            audit_log.record("document_upload", "failure", request=request, principal=auth,
                             details={"reason": type(e).__name__})
        finally:
            if spool is not None:
                spool.close()
            await file.close()

    return {
        "status": "success" if registered else "error",
        "registered_count": len(registered),
        "documents": registered,
        "errors": errors,
    }


@router.post("/process-all", summary="Process all pending and failed documents in one operation")
def process_all_documents(
    request: Request,
    req: Optional[ProcessRequest] = Body(default=None),
    auth: Any = Depends(authenticate_document_operator),
):
    force = req.force if req else False
    doc_ids = req.document_ids if req else None
    result = document_processing_service.start_processing(document_ids=doc_ids, force=force)
    audit_log.record("document_process", request=request, principal=auth, target="all",
                     details={"force": force, "document_ids": doc_ids, "status": result.get("status")})
    return result


@router.post("/{document_id}/process", summary="Process or retry an individual document")
def process_individual_document(
    document_id: str,
    request: Request,
    req: Optional[ProcessRequest] = Body(default=None),
    db: Session = Depends(get_db),
    auth: Any = Depends(authenticate_document_operator),
):
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document '{document_id}' not found.")

    force = req.force if req else True
    result = document_processing_service.start_processing(document_ids=[document_id], force=force)
    audit_log.record("document_process", request=request, principal=auth, target=document_id,
                     details={"force": force, "status": result.get("status")})
    return result


@router.get("/{document_id}", summary="Get detailed document metadata and sample chunk inspect")
def get_document_details(
    document_id: str,
    db: Session = Depends(get_db),
    auth: Any = Depends(authenticate_document_operator),
):
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")

    chunks = (
        db.query(DocumentChunk)
        .filter(DocumentChunk.document_id == doc.id)
        .order_by(DocumentChunk.chunk_index)
        .limit(20)
        .all()
    )
    total_chunks = db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).count()

    chunks_data = []
    for c in chunks:
        chunks_data.append({
            "id": str(c.id),
            "chunk_index": c.chunk_index,
            "page_number": c.page_number,
            "section": c.section,
            "text_preview": c.chunk_text[:200] + ("..." if len(c.chunk_text) > 200 else ""),
            "has_embedding": c.embedding is not None,
        })

    return {
        "id": str(doc.id),
        "filename": doc.filename,
        "title": doc.title,
        "file_type": doc.file_type,
        "file_size_bytes": doc.file_size_bytes,
        "file_hash": doc.file_hash,
        "page_count": doc.page_count,
        "chunk_count": total_chunks,
        "status": doc.status,
        "stage": doc.stage,
        "error_message": doc.error_message,
        "vector_indexed": doc.vector_indexed,
        "allowed_groups": list(doc.allowed_groups or []),
        "source_tier": doc.source_tier,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
        "sample_chunks": chunks_data,
    }


@router.patch("/{document_id}/access", summary="Set which access groups may retrieve this document")
def set_document_access(
    document_id: str,
    update: AccessUpdate,
    request: Request,
    db: Session = Depends(get_db),
    auth: Any = Depends(authenticate_document_operator),
):
    """Regular users retrieve a document only if they share one of its groups (AUDIT_REPORT F-01)."""
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    groups = normalize_groups(update.allowed_groups)
    if not groups:
        raise HTTPException(status_code=422, detail="At least one group is required.")
    previous = list(doc.allowed_groups or [])
    doc.allowed_groups = groups
    db.commit()
    audit_log.record("document_access_change", request=request, principal=auth, target=document_id,
                     details={"before": previous, "after": groups})
    # Group changes do not change the chunk count, so the retrieval cache must be dropped explicitly.
    retrieval_service.invalidate_cache()
    return {"id": str(doc.id), "allowed_groups": list(doc.allowed_groups)}


@router.patch("/{document_id}/tier", summary="Classify a document's source authority tier")
def set_document_source_tier(
    document_id: str,
    update: SourceTierUpdate,
    request: Request,
    db: Session = Depends(get_db),
    auth: Any = Depends(authenticate_document_operator),
):
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    previous = doc.source_tier
    doc.source_tier = update.source_tier
    db.commit()
    audit_log.record(
        "document_source_tier_change", request=request, principal=auth, target=document_id,
        details={"before": previous, "after": update.source_tier},
    )
    retrieval_service.invalidate_cache()
    return {"id": str(doc.id), "source_tier": doc.source_tier}


@router.delete("/{document_id}", summary="Delete document from knowledge base, purge vectors and disk file")
def delete_document(
    document_id: str,
    request: Request,
    db: Session = Depends(get_db),
    auth: Any = Depends(authenticate_document_operator),
):
    try:
        result = document_processing_service.delete_document(document_id=document_id, db=db)
        audit_log.record("document_delete", request=request, principal=auth, target=document_id,
                         details={"filename": result.get("deleted_filename"), "chunks": result.get("deleted_chunks")})
        return result
    except ValueError:
        raise HTTPException(status_code=404, detail="DOCUMENT_NOT_FOUND")
    except Exception as e:
        logger.error("Failed to delete document id=%s (%s)", document_id, type(e).__name__, exc_info=True)
        raise HTTPException(status_code=500, detail="DOCUMENT_DELETE_FAILED")
