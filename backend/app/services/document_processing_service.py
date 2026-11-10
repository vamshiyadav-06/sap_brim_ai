import io
import os
import re
import json
import uuid
import logging
import threading
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Set

from sqlalchemy.orm import Session
from sqlalchemy import or_, desc

from backend.app.config import settings
from backend.app.security.log_safety import privacy_fingerprint
from backend.app.database import SessionLocal
from backend.app.models.document import Document
from backend.app.models.chunk import DocumentChunk
from backend.app.services.document_service import document_service
from backend.app.services.embedding_service import embedding_service
from backend.app.services.retrieval_service import retrieval_service
from backend.app.services.document_extractor import extract_document

logger = logging.getLogger("document_processing")

SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff",
    ".txt", ".md", ".markdown", ".csv", ".log", ".tsv"
}
EXTENDED_UPLOAD_EXTENSIONS = {".json", ".xml", ".sql", ".yaml", ".yml"}


def allowed_upload_extensions() -> Set[str]:
    if settings.ALLOW_EXTENDED_UPLOAD_TYPES:
        return SUPPORTED_EXTENSIONS | EXTENDED_UPLOAD_EXTENSIONS
    return SUPPORTED_EXTENSIONS


def validate_upload_signature(filename: str, prefix: bytes) -> None:
    """Reject common extension/content mismatches before a document is registered."""
    suffix = Path(filename).suffix.lower()
    signatures = {
        ".jpg": prefix.startswith(b"\xff\xd8\xff"),
        ".jpeg": prefix.startswith(b"\xff\xd8\xff"),
        ".png": prefix.startswith(b"\x89PNG\r\n\x1a\n"),
        ".webp": prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP",
        ".bmp": prefix.startswith(b"BM"),
        ".tiff": prefix.startswith((b"II*\x00", b"MM\x00*")),
        ".docx": prefix.startswith(b"PK\x03\x04"),
    }
    if suffix == ".pdf":
        valid = b"%PDF-" in prefix[:1024]
    elif suffix in signatures:
        valid = signatures[suffix]
    else:
        try:
            prefix.decode("utf-8-sig")
            valid = b"\x00" not in prefix
        except UnicodeDecodeError:
            valid = False
    if not valid:
        raise ValueError("File content does not match the selected document format.")


class DocumentProcessingService:
    def __init__(self):
        self._lock = threading.Lock()
        self._is_processing = False
        self._listeners: Dict[asyncio.Queue, asyncio.AbstractEventLoop] = {}

        # Live Progress State
        self._progress_state: Dict[str, Any] = {
            "is_processing": False,
            "total_selected": 0,
            "completed_count": 0,
            "processing_count": 0,
            "pending_count": 0,
            "failed_count": 0,
            "total_chunks_generated": 0,
            "total_embeddings_stored": 0,
            "overall_progress_pct": 0,
            "current_document_id": None,
            "current_document_name": None,
            "current_stage": "Idle",
            "verification_status": "Pending",
            "summary": None,
            "per_document_status": {},
            "recent_logs": [],
        }

    def subscribe(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> asyncio.Queue:
        loop = loop or asyncio.get_running_loop()
        # Progress is a latest-state snapshot, not a lossless event log. A bounded queue
        # prevents slow/disconnected SSE clients from accumulating every intermediate update.
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        with self._lock:
            self._listeners[q] = loop
        return q

    def unsubscribe(self, q: asyncio.Queue):
        with self._lock:
            self._listeners.pop(q, None)

    @staticmethod
    def _deliver_latest(q: asyncio.Queue, payload: Dict[str, Any]) -> None:
        """Runs on the subscriber's event loop and coalesces stale progress snapshots."""
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            # Another loop callback won the race; its snapshot is at least as recent.
            pass

    def _broadcast(self, event_data: Dict[str, Any]):
        with self._lock:
            # Add timestamp to event
            event_data["timestamp"] = datetime.now(timezone.utc).isoformat()
            self._progress_state.update(event_data)
            payload = dict(self._progress_state)
            listeners = list(self._listeners.items())

        for q, loop in listeners:
            try:
                # asyncio.Queue is not thread-safe. The pipeline worker must schedule
                # delivery onto the loop that owns each subscriber queue.
                loop.call_soon_threadsafe(self._deliver_latest, q, payload)
            except RuntimeError:
                # The client loop has closed; remove that stale listener.
                self.unsubscribe(q)

    def get_progress_status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._progress_state)

    def _log_event(self, message: str):
        logger.info(message)
        with self._lock:
            logs = self._progress_state.get("recent_logs", [])
            logs.append(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {message}")
            if len(logs) > 50:
                logs.pop(0)
            self._progress_state["recent_logs"] = logs

    def sync_disk_documents_with_db(self, db: Session) -> int:
        """
        Discovers files in settings.DOCUMENTS_DIR. If any are not yet in documents table,
        registers them with status='pending'.
        """
        source_dir = settings.DOCUMENTS_DIR
        if not source_dir.exists():
            try:
                source_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.warning(f"Could not create DOCUMENTS_DIR {source_dir}: {e}")
                return 0

        synced_count = 0
        existing_filenames = {d.filename for d in db.query(Document.filename).all()}

        for path in source_dir.rglob("*"):
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix not in allowed_upload_extensions():
                continue

            filename = path.name
            if filename not in existing_filenames:
                try:
                    file_size = path.stat().st_size
                    file_hash = document_service.compute_file_hash(path)
                    doc_info = document_service.get_document_info(filename)
                    doc_type = suffix.lstrip(".")

                    new_doc = Document(
                        id=str(uuid.uuid4()),
                        filename=filename,
                        title=doc_info["title"],
                        file_hash=file_hash,
                        file_type=doc_type,
                        file_size_bytes=file_size,
                        version="1.0",
                        page_count=0,
                        status="pending",
                        stage="Discovered on disk (Pending processing)",
                        vector_indexed=False,
                    )
                    db.add(new_doc)
                    existing_filenames.add(filename)
                    synced_count += 1
                except Exception as e:
                    logger.error("Could not register discovered document_ref=%s (%s)", privacy_fingerprint(filename), type(e).__name__)

        if synced_count > 0:
            db.commit()
            logger.info(f"Synchronized {synced_count} new files from knowledge base disk directory.")

        return synced_count

    def get_documents(
        self,
        db: Session,
        status_filter: Optional[str] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 50
    ) -> Dict[str, Any]:
        """Returns paginated documents with status filters, search, and aggregate metrics."""
        self.sync_disk_documents_with_db(db)

        query = db.query(Document)

        if status_filter and status_filter.lower() != "all":
            query = query.filter(Document.status == status_filter.lower())

        if search:
            s = f"%{search.strip().lower()}%"
            query = query.filter(or_(Document.filename.ilike(s), Document.title.ilike(s)))

        total_matching = query.count()
        documents = (
            query.order_by(desc(Document.created_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

        # Preload chunk counts for each document in query
        doc_ids = [d.id for d in documents]
        chunk_count_map = {}
        if doc_ids:
            chunk_counts = (
                db.query(DocumentChunk.document_id, DocumentChunk.id)
                .filter(DocumentChunk.document_id.in_(doc_ids))
                .all()
            )
            for d_id, _ in chunk_counts:
                chunk_count_map[d_id] = chunk_count_map.get(d_id, 0) + 1

        docs_data = []
        for doc in documents:
            c_count = chunk_count_map.get(doc.id, 0)
            docs_data.append({
                "id": str(doc.id),
                "filename": doc.filename,
                "title": doc.title,
                "file_type": doc.file_type or Path(doc.filename).suffix.lstrip(".").lower() or "pdf",
                "file_size_bytes": doc.file_size_bytes or 0,
                "file_size_formatted": self._format_size(doc.file_size_bytes or 0),
                "page_count": doc.page_count,
                "chunk_count": c_count,
                "status": doc.status or "completed",
                "stage": doc.stage or ("Indexed" if c_count > 0 else "Pending"),
                "error_message": doc.error_message,
                "vector_indexed": bool(doc.vector_indexed and c_count > 0),
                "allowed_groups": list(doc.allowed_groups or []),
                "source_tier": doc.source_tier,
                "created_at": doc.created_at.isoformat() if doc.created_at else None,
                "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
            })

        # Global aggregate stats
        all_docs = db.query(Document).all()
        total_chunks = db.query(DocumentChunk).count()

        counts = {
            "all": len(all_docs),
            "pending": sum(1 for d in all_docs if (d.status or "").lower() == "pending"),
            "processing": sum(1 for d in all_docs if (d.status or "").lower() == "processing"),
            "completed": sum(1 for d in all_docs if (d.status or "").lower() == "completed"),
            "failed": sum(1 for d in all_docs if (d.status or "").lower() == "failed"),
            "total_chunks": total_chunks,
        }

        return {
            "documents": docs_data,
            "total": total_matching,
            "page": page,
            "page_size": page_size,
            "counts": counts,
        }

    def _format_size(self, size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes / (1024 * 1024):.2f} MB"

    def register_uploaded_file(
        self,
        filename: str,
        file_bytes: Any,
        db: Session
    ) -> Dict[str, Any]:
        """
        Saves uploaded file into settings.DOCUMENTS_DIR safely and registers Document record.
        Prevents unintentional overwrites by generating a safe unique name if a different file
        with the same name already exists.
        """
        suffix = Path(filename).suffix.lower()
        allowed_extensions = allowed_upload_extensions()
        if suffix not in allowed_extensions:
            raise ValueError(
                f"Unsupported file format '{suffix}'. Supported formats are: {', '.join(sorted(allowed_extensions))}"
            )

        source = io.BytesIO(file_bytes) if isinstance(file_bytes, bytes) else file_bytes
        source.seek(0)
        prefix = source.read(2048)
        source.seek(0)
        validate_upload_signature(filename, prefix)

        import hashlib
        hasher = hashlib.sha256()
        file_size = 0
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            file_size += len(block)
            if file_size > settings.MAX_UPLOAD_BYTES:
                raise ValueError("Uploaded file exceeds the configured per-file size limit.")
            hasher.update(block)
        if file_size == 0:
            raise ValueError("Uploaded file is empty (0 bytes).")
        file_hash = hasher.hexdigest()
        source.seek(0)

        source_dir = settings.DOCUMENTS_DIR
        source_dir.mkdir(parents=True, exist_ok=True)

        # Clean base name
        stem = Path(filename).stem
        sanitized_stem = re.sub(r"[^\w\s\-\.]", "_", stem).strip()
        if not sanitized_stem:
            sanitized_stem = "uploaded_doc"

        target_filename = f"{sanitized_stem}{suffix}"
        target_path = source_dir / target_filename

        # If a file already exists:
        existing_doc = db.query(Document).filter(Document.filename == target_filename).first()
        if existing_doc or target_path.exists():
            # If the file hash matches, we can keep the file and reset status to pending for reprocessing
            if existing_doc and existing_doc.file_hash == file_hash:
                target_filename = existing_doc.filename
            else:
                # Different file with same name: create safe unique name
                unique_suffix = uuid.uuid4().hex[:6]
                target_filename = f"{sanitized_stem}_{unique_suffix}{suffix}"
                target_path = source_dir / target_filename
                existing_doc = None

        # Write to disk
        with open(target_path, "wb") as f:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                f.write(block)
        source.seek(0)

        doc_info = document_service.get_document_info(target_filename)
        doc_type = suffix.lstrip(".")

        if existing_doc:
            existing_doc.file_hash = file_hash
            existing_doc.file_size_bytes = file_size
            existing_doc.status = "pending"
            existing_doc.stage = "Ready for processing"
            existing_doc.vector_indexed = False
            existing_doc.error_message = None
            existing_doc.updated_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(existing_doc)
            doc_record = existing_doc
        else:
            doc_record = Document(
                id=str(uuid.uuid4()),
                filename=target_filename,
                title=doc_info["title"],
                file_hash=file_hash,
                file_type=doc_type,
                file_size_bytes=file_size,
                version="1.0",
                page_count=0,
                status="pending",
                stage="Ready for processing",
                vector_indexed=False,
            )
            db.add(doc_record)
            db.commit()
            db.refresh(doc_record)

        self._log_event(f"Registered uploaded document_ref={privacy_fingerprint(target_filename)} (ID: {doc_record.id}, Size: {self._format_size(file_size)})")

        return {
            "id": str(doc_record.id),
            "filename": doc_record.filename,
            "title": doc_record.title,
            "file_type": doc_record.file_type,
            "file_size_bytes": doc_record.file_size_bytes,
            "status": doc_record.status,
            "stage": doc_record.stage,
        }

    def start_processing(
        self,
        document_ids: Optional[List[str]] = None,
        force: bool = False
    ) -> Dict[str, Any]:
        """
        Triggers background ingestion pipeline for selected or all pending/failed documents.
        """
        with self._lock:
            if self._is_processing:
                return {
                    "status": "already_running",
                    "message": "A document processing job is already in progress.",
                    # We already hold _lock here. Re-entering get_progress_status() would
                    # deadlock because _lock is intentionally a non-reentrant Lock.
                    "progress": dict(self._progress_state),
                }
            self._is_processing = True

        # Launch worker in background thread
        thread = threading.Thread(
            target=self._process_worker,
            args=(document_ids, force),
            daemon=True,
            name="DocumentProcessingWorker"
        )
        try:
            thread.start()
        except Exception:
            with self._lock:
                self._is_processing = False
                self._progress_state["is_processing"] = False
            raise

        return {
            "status": "started",
            "message": "Document processing pipeline started successfully.",
        }

    def _process_worker(self, document_ids: Optional[List[str]], force: bool):
        db = SessionLocal()
        try:
            self._execute_pipeline(db, document_ids, force)
        except Exception as e:
            logger.error(f"Fatal error in document processing worker: {e}", exc_info=True)
            self._log_event(f"Fatal processing failure ({type(e).__name__})")
            self._broadcast({
                "is_processing": False,
                "current_stage": "DOCUMENT_PROCESSING_FAILED",
                "verification_status": "Failed",
            })
        finally:
            with self._lock:
                self._is_processing = False
                self._progress_state["is_processing"] = False
            db.close()

    def _execute_pipeline(
        self,
        db: Session,
        document_ids: Optional[List[str]],
        force: bool
    ):
        self.sync_disk_documents_with_db(db)

        # Select target documents
        query = db.query(Document)
        if document_ids:
            query = query.filter(Document.id.in_(document_ids))
        elif not force:
            query = query.filter(Document.status.in_(["pending", "failed", "processing"]))

        target_docs = query.all()
        total_docs = len(target_docs)

        if total_docs == 0:
            self._log_event("No pending or failed documents found for processing.")
            self._broadcast({
                "is_processing": False,
                "total_selected": 0,
                "overall_progress_pct": 100,
                "current_stage": "No pending documents to process.",
                "verification_status": "Successful",
                "summary": {
                    "documents_processed": 0,
                    "chunks_generated": 0,
                    "embeddings_stored": 0,
                    "failed_documents": 0,
                    "vector_database_verification": "Successful",
                }
            })
            return

        self._log_event(f"Starting processing for {total_docs} document(s)...")

        # Initialize progress state
        completed_count = 0
        failed_count = 0
        total_chunks = 0
        total_embeddings = 0

        self._broadcast({
            "is_processing": True,
            "total_selected": total_docs,
            "completed_count": 0,
            "processing_count": 0,
            "pending_count": total_docs,
            "failed_count": 0,
            "total_chunks_generated": 0,
            "total_embeddings_stored": 0,
            "overall_progress_pct": 0,
            "current_stage": "Initializing document pipeline...",
            "verification_status": "Pending",
            "summary": None,
        })

        BATCH_EMBED_SIZE = 64

        for idx, doc in enumerate(target_docs, start=1):
            doc_id = str(doc.id)
            filename = doc.filename
            file_path = settings.DOCUMENTS_DIR / filename

            file_ref = privacy_fingerprint(filename)
            self._log_event(f"[{idx}/{total_docs}] Starting processing document_ref={file_ref}")

            # Update state to Processing
            doc.status = "processing"
            doc.stage = "Extracting document content..."
            doc.error_message = None
            db.commit()

            self._broadcast({
                "current_document_id": doc_id,
                "current_document_name": filename,
                "current_stage": "Extracting document content...",
                "processing_count": 1,
                "pending_count": total_docs - (completed_count + failed_count + 1),
                "overall_progress_pct": int(((completed_count + failed_count) / total_docs) * 100),
            })

            try:
                # Step 1: Validate file existence and readability
                if not file_path.exists() or file_path.stat().st_size == 0:
                    raise ValueError(f"File '{filename}' does not exist on disk or is 0 bytes.")

                # Step 2 & 3: Extract text, pages, and run OCR on images
                with open(file_path, "rb") as f:
                    file_bytes = f.read()

                extracted = extract_document(filename, file_bytes)
                extracted_pages = extracted.get("pages", [])
                page_count = extracted.get("page_count", len(extracted_pages) or 1)
                full_text = extracted.get("text", "")

                if not full_text or len(full_text.strip()) < 10:
                    raise ValueError("No readable text could be extracted from this document.")

                doc.page_count = page_count
                doc.file_size_bytes = len(file_bytes)
                doc.stage = "Generating chunks..."
                db.commit()

                self._broadcast({
                    "current_stage": "Generating chunks...",
                })

                # Step 4: Split into structure-aware chunks
                all_chunks = []
                if extracted_pages and len(extracted_pages) > 0:
                    for p in extracted_pages:
                        p_num = p.get("page", 1)
                        p_text = p.get("text", "")
                        if not p_text or len(p_text.strip()) < 10:
                            continue
                        chunks = document_service.chunk_page_text(
                            text=p_text,
                            page_number=p_num,
                            document_name=filename,
                            active_section=f"Page {p_num}",
                        )
                        all_chunks.extend(chunks)
                else:
                    # Single text segment
                    all_chunks = document_service.chunk_page_text(
                        text=full_text,
                        page_number=1,
                        document_name=filename,
                        active_section="Overview",
                    )

                if not all_chunks:
                    raise ValueError("Extracted text was insufficient to generate document chunks.")

                num_chunks = len(all_chunks)
                total_chunks += num_chunks
                self._log_event(f"document_ref={file_ref}: Generated {num_chunks} chunks. Creating embeddings...")

                doc.stage = "Creating embeddings..."
                db.commit()

                self._broadcast({
                    "current_stage": "Creating embeddings...",
                    "total_chunks_generated": total_chunks,
                })

                # Step 5: Generate embeddings in batches
                # Purge any existing chunks for idempotency / clean retry
                db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).delete()
                db.commit()

                doc.stage = "Storing vectors..."
                db.commit()

                self._broadcast({
                    "current_stage": "Storing vectors...",
                })

                inserted_chunks = 0
                for i in range(0, num_chunks, BATCH_EMBED_SIZE):
                    batch = all_chunks[i : i + BATCH_EMBED_SIZE]
                    texts_to_embed = [c["chunk_text"] for c in batch]
                    embeddings = embedding_service.embed_batch(texts_to_embed)

                    for j, chunk_data in enumerate(batch):
                        meta = dict(chunk_data.get("metadata", {}))
                        meta["document_id"] = doc.id
                        meta["filename"] = filename
                        meta["file_type"] = doc.file_type

                        chunk_record = DocumentChunk(
                            id=str(uuid.uuid4()),
                            document_id=doc.id,
                            document_name=filename,
                            chunk_index=i + j,
                            page_number=chunk_data.get("page_number", 1),
                            section=chunk_data.get("section", "Section"),
                            chunk_text=chunk_data["chunk_text"],
                            metadata_json=json.dumps(meta),
                            embedding=embeddings[j],
                            embedding_model=embedding_service.embedding_model,
                            embedding_version=embedding_service.embedding_version,
                        )
                        db.add(chunk_record)
                        inserted_chunks += 1

                    db.commit()

                total_embeddings += inserted_chunks

                # Step 6 & 7: Verify vector storage
                doc.stage = "Verifying vector storage..."
                db.commit()

                self._broadcast({
                    "current_stage": "Verifying vector storage...",
                    "total_embeddings_stored": total_embeddings,
                })

                verified_chunk_count = (
                    db.query(DocumentChunk)
                    .filter(DocumentChunk.document_id == doc.id)
                    .count()
                )
                if verified_chunk_count != num_chunks:
                    raise ValueError(
                        f"Vector database verification failed: expected {num_chunks} chunks, found {verified_chunk_count}."
                    )

                # Step 8: Complete successfully
                doc.status = "completed"
                doc.stage = "Indexed"
                doc.vector_indexed = True
                doc.error_message = None
                doc.updated_at = datetime.now(timezone.utc)
                db.commit()

                completed_count += 1
                self._log_event(f"document_ref={file_ref} processed successfully ({num_chunks} chunks, {inserted_chunks} vectors verified).")

                self._broadcast({
                    "current_stage": "Processing completed successfully!",
                    "completed_count": completed_count,
                    "processing_count": 0,
                    "overall_progress_pct": int(((completed_count + failed_count) / total_docs) * 100),
                })

            except Exception as doc_err:
                logger.error("Error processing document_ref=%s (%s)", file_ref, type(doc_err).__name__, exc_info=True)
                db.rollback()

                # Clean up any partial chunks so no orphaned vectors remain
                try:
                    db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).delete()
                    db.commit()
                except Exception:
                    pass

                doc.status = "failed"
                doc.stage = "Failed"
                doc.error_message = "DOCUMENT_PROCESSING_FAILED"
                doc.vector_indexed = False
                doc.updated_at = datetime.now(timezone.utc)
                db.commit()

                failed_count += 1
                self._log_event(f"document_ref={file_ref} failed: {type(doc_err).__name__}")

                self._broadcast({
                    "failed_count": failed_count,
                    "processing_count": 0,
                    "current_stage": "DOCUMENT_PROCESSING_FAILED",
                    "overall_progress_pct": int(((completed_count + failed_count) / total_docs) * 100),
                })

        # Invalidate retrieval cache so newly indexed vectors are live
        try:
            retrieval_service.invalidate_cache()
            self._log_event("Retrieval service vector cache invalidated. All new documents are live in RAG queries.")
        except Exception as e:
            logger.warning(f"Could not invalidate retrieval cache: {e}")

        # Final Summary
        summary = {
            "documents_processed": total_docs,
            "completed_documents": completed_count,
            "chunks_generated": total_chunks,
            "embeddings_stored": total_embeddings,
            "failed_documents": failed_count,
            "vector_database_verification": "Successful" if failed_count == 0 else "Completed with errors",
        }

        self._broadcast({
            "is_processing": False,
            "overall_progress_pct": 100,
            "completed_count": completed_count,
            "failed_count": failed_count,
            "processing_count": 0,
            "pending_count": 0,
            "current_document_id": None,
            "current_document_name": None,
            "current_stage": "Document Processing Completed Successfully!" if failed_count == 0 else "Processing Completed with some issues.",
            "verification_status": "Successful" if failed_count == 0 else "Partial",
            "summary": summary,
        })

        self._log_event(f"Pipeline finished: {summary}")

    def delete_document(self, document_id: str, db: Session) -> Dict[str, Any]:
        """
        Permanently deletes a document, purges all associated chunks and vectors,
        removes the file from disk, and invalidates retrieval memory caches.
        """
        doc = db.query(Document).filter(Document.id == document_id).first()
        if not doc:
            raise ValueError(f"Document with ID '{document_id}' not found.")

        filename = doc.filename
        file_path = settings.DOCUMENTS_DIR / filename

        # 1. Database transaction: delete chunks and document
        chunk_count = db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).count()
        db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).delete()
        db.delete(doc)
        db.commit()

        # 2. Verify zero chunks remain for this document ID
        remaining_chunks = db.query(DocumentChunk).filter(DocumentChunk.document_id == document_id).count()
        if remaining_chunks > 0:
            logger.error(f"Transaction warning: {remaining_chunks} chunks remained for deleted doc ID '{document_id}'")

        # 3. Remove physical file from disk
        file_deleted_from_disk = False
        try:
            if file_path.exists() and file_path.is_file():
                file_path.unlink()
                file_deleted_from_disk = True
        except Exception as e:
            logger.warning("Could not remove physical document_ref=%s (%s)", privacy_fingerprint(filename), type(e).__name__)

        # 4. Invalidate retrieval cache immediately so RAG chatbot never retrieves this document
        try:
            retrieval_service.invalidate_cache()
            logger.info("Retrieval cache invalidated following document deletion (document_ref=%s)", privacy_fingerprint(filename))
        except Exception as e:
            logger.warning(f"Could not invalidate retrieval cache: {e}")

        self._log_event(f"Permanently deleted document_ref={privacy_fingerprint(filename)} (purged {chunk_count} vectors).")

        return {
            "success": True,
            "deleted_document_id": document_id,
            "deleted_filename": filename,
            "deleted_chunks": chunk_count,
            "file_deleted_from_disk": file_deleted_from_disk,
            "vector_database_status": "Synchronized (All vectors purged)",
        }


document_processing_service = DocumentProcessingService()
