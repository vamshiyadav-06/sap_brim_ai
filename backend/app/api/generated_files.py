"""Download of documents the assistant created for the signed-in user (see services/document_generator.py)."""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from backend.app.models.user import User
from backend.app.security.auth import get_current_user
from backend.app.services.document_generator import load_file

router = APIRouter(prefix="/files", tags=["Files"])


@router.get("/{file_id}")
def download_generated_file(file_id: str, current_user: User = Depends(get_current_user)):
    """The file only for the user it was created for; expired, unknown and other users' files all look like 404."""
    found = load_file(current_user.id, file_id)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This file is no longer available. Ask for it again to get a new copy.")
    path, meta = found
    return FileResponse(
        path, media_type=meta["media_type"], filename=meta["filename"],
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
    )
