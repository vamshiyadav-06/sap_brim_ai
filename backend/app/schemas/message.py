from datetime import datetime
from typing import List, Literal, Optional
from pydantic import BaseModel, Field, ConfigDict, field_validator
from backend.app.schemas.citation import CitationResponse, WebSourceResponse, WebImageResponse
from backend.app.config import settings


# Files (also a folder's files and linked pages) attached to one message; their text must fit the request body.
MAX_ATTACHMENTS = 20
MAX_ATTACHMENT_TOTAL_CHARS = 1_500_000


class DocumentAttachment(BaseModel):
    name: str = Field(..., min_length=1, max_length=500)
    file_type: Optional[str] = Field(default="pdf", max_length=32)
    size_bytes: Optional[int] = 0
    page_count: Optional[int] = None
    text: str = Field(..., min_length=1, max_length=150000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.replace("\\", "/").split("/")[-1].strip()
        if not value:
            raise ValueError("Attachment filename cannot be empty.")
        return value


class UrlExtractRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2000)


class DocumentExtractResponse(BaseModel):
    filename: str
    file_type: str
    size_bytes: int
    page_count: int
    char_count: int
    preview: str
    text: str
    # Images only: the question/error found in the image, and how it was read
    # ("vision", "vision+ocr" or "ocr").
    detected_question: Optional[str] = None
    analysis_method: Optional[str] = None


class UploadedAttachmentInfo(BaseModel):
    name: str
    file_type: str
    page_count: Optional[int] = None
    size_bytes: Optional[int] = None


class MessageCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=200000)
    attachments: Optional[List[DocumentAttachment]] = Field(default=None, max_length=MAX_ATTACHMENTS)
    # Keep legacy API callers on Scholar; the chat UI explicitly sends its Sprint default.
    mode: Literal["sprint", "scholar"] = "scholar"
    is_regeneration: Optional[bool] = False

    @field_validator("attachments")
    @classmethod
    def enforce_total_attachment_size(cls, value):
        if value and sum(len(item.text) for item in value) > MAX_ATTACHMENT_TOTAL_CHARS:
            raise ValueError("The attached files are too large together. Attach fewer or smaller files.")
        return value

    @field_validator("content")
    @classmethod
    def enforce_configured_message_limit(cls, value: str) -> str:
        limit = int(getattr(settings, "MAX_MESSAGE_CHARS", 12000))
        if len(value) > limit:
            raise ValueError(f"Message is too long. The maximum length is {limit} characters.")
        return value


class SavePartialRequest(BaseModel):
    """Text the user saw before stopping a stream, with the stream id the server issued for it (F-37)."""
    content: str = Field(..., min_length=1, max_length=200000)
    stream_id: str = Field(..., min_length=20, max_length=600)


class MessageFeedback(BaseModel):
    # None clears the rating.
    value: Optional[Literal["up", "down"]] = None


class TokenUsage(BaseModel):
    used: int
    baseline: int
    saved: int
    # Free tokens this answer cost the user (0 to 1), see services/token_wallet.py.
    cost: float = 0.0


class TokenSavingsSummary(BaseModel):
    """Sum over the account's answers that have stored figures."""
    answers: int
    used: int
    baseline: int
    saved: int


def stored_token_usage(message) -> Optional[TokenUsage]:
    """The figures stored with an answer; None when they are unknown (older or non-streamed answers)."""
    used, baseline = getattr(message, "tokens_used", None), getattr(message, "tokens_baseline", None)
    if used is None or baseline is None:
        return None
    from backend.app.services import token_wallet  # local: the wallet imports the models
    return TokenUsage(used=used, baseline=baseline, saved=max(0, baseline - used), cost=token_wallet.cost_tokens(used))


class MessageResponse(BaseModel):
    id: str
    chat_id: str
    role: str
    content: str
    source_type: Optional[str] = None
    grounding_score: Optional[float] = None
    is_in_rag_pipeline: Optional[bool] = None
    kb_score: Optional[float] = None
    web_score: Optional[float] = None
    winning_score: Optional[float] = None
    verification_status: Optional[str] = "verified"
    internal_evidence_confidence: Optional[float] = None
    selected_evidence_quality: Optional[float] = None
    external_search_used: Optional[bool] = False
    created_at: datetime
    citations: List[CitationResponse] = []
    web_sources: List[WebSourceResponse] = []
    images: List[WebImageResponse] = []
    follow_up_questions: List[str] = []
    attachments: Optional[List[UploadedAttachmentInfo]] = None
    feedback: Optional[Literal["up", "down"]] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    regeneration_count: Optional[int] = 0
    token_usage: Optional[TokenUsage] = None

    model_config = ConfigDict(from_attributes=True)
