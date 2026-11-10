from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.security.auth import get_current_user
from backend.app.schemas.message import MAX_ATTACHMENTS, DocumentAttachment, MessageCreate
from backend.app.services.document_extractor import extract_document
from backend.app.services.llm_service import LLMService
from backend.app.services.rag_service import RAGService


def _guest_headers(client):
    """Use a test-only authenticated principal; public guest auth is disabled."""
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id="test-user", email="test@example.com", name="Test", role="consultant",
        is_admin=False, token_version=0,
    )
    return {}


def _png_bytes():
    import io

    output = io.BytesIO()
    Image.new("RGB", (80, 40), "white").save(output, format="PNG")
    return output.getvalue()


def test_png_upload_ocr_text_becomes_llm_evidence():
    fake_output = SimpleNamespace(
        boxes=np.asarray([[[5, 5], [70, 5], [70, 20], [5, 20]]], dtype=float),
        txts=("Invoice total: 125.00",),
    )
    with patch("backend.app.services.document_extractor._get_ocr_engine", return_value=lambda _image: fake_output):
        extracted = extract_document("invoice.png", _png_bytes())

    attachment = DocumentAttachment(
        name=extracted["filename"],
        file_type=extracted["file_type"],
        size_bytes=extracted["size_bytes"],
        page_count=extracted["page_count"],
        text=extracted["text"],
    )
    passages = RAGService._attachment_passages("What is the invoice total?", [attachment.model_dump()])
    llm_context = LLMService._format_evidence_context(passages)

    assert extracted["file_type"] == "png"
    assert "Invoice total: 125.00" in extracted["text"]
    assert "125.00" in llm_context
    assert "Attached Document: invoice.png" in llm_context


def test_png_upload_endpoint_returns_ocr_content():
    fake_output = SimpleNamespace(
        boxes=np.asarray([[[5, 5], [70, 5], [70, 20], [5, 20]]], dtype=float),
        txts=("Invoice total: 125.00",),
    )
    client = TestClient(app)
    with patch("backend.app.services.document_extractor._get_ocr_engine", return_value=lambda _image: fake_output), \
         patch("backend.app.services.image_vision_service._call_vision_models", return_value=(None, None)):
        response = client.post(
            "/api/chats/extract-document",
            headers=_guest_headers(client),
            files={"file": ("invoice.png", _png_bytes(), "image/png")},
        )

    assert response.status_code == 200, response.text
    assert response.json()["file_type"] == "png"
    assert response.json()["analysis_method"] == "ocr"
    assert "Invoice total: 125.00" in response.json()["text"]


VISION_REPLY = """## Image Summary
Screenshot of an SAP error dialog in transaction FKKBIX_M.

## Visible Text
Billable item class 0SAP is not defined (message FKKBIX 012)

## Detected Question or Problem
Error FKKBIX 012: billable item class 0SAP is not defined.

DETECTED_QUESTION: How do I resolve SAP message FKKBIX 012 "billable item class 0SAP is not defined"?"""


def test_pasted_image_endpoint_returns_vision_analysis_and_detected_question():
    fake_output = SimpleNamespace(
        boxes=np.asarray([[[5, 5], [70, 5], [70, 20], [5, 20]]], dtype=float),
        txts=("FKKBIX 012",),
    )
    client = TestClient(app)
    with patch("backend.app.services.document_extractor._get_ocr_engine", return_value=lambda _image: fake_output), \
         patch("backend.app.services.image_vision_service._call_vision_models", return_value=(VISION_REPLY, "test-vision")):
        response = client.post(
            "/api/chats/extract-document",
            headers=_guest_headers(client),
            files={"file": ("pasted-image-1.png", _png_bytes(), "image/png")},
        )

    body = response.json()
    assert response.status_code == 200, response.text
    assert body["analysis_method"] == "vision+ocr"
    assert body["detected_question"].startswith("How do I resolve SAP message FKKBIX 012")
    assert "Screenshot of an SAP error dialog" in body["text"]
    assert "OCR Cross-check" in body["text"]
    assert "DETECTED_QUESTION" not in body["text"]


def test_image_without_text_is_still_read_by_vision_and_none_question_is_dropped():
    from backend.app.services.image_vision_service import analyze_chat_image

    reply = "## Image Summary\nA flow diagram of order-to-cash.\n\nDETECTED_QUESTION: NONE"
    with patch("backend.app.services.document_extractor._get_ocr_engine", return_value=lambda _image: None), \
         patch("backend.app.services.image_vision_service._call_vision_models", return_value=(reply, "test-vision")):
        result = analyze_chat_image("diagram.png", _png_bytes())

    assert result["analysis_method"] == "vision"
    assert result["detected_question"] is None
    assert "order-to-cash" in result["text"]


def test_image_fails_cleanly_when_neither_vision_nor_ocr_can_read_it():
    from backend.app.services.image_vision_service import analyze_chat_image

    with patch("backend.app.services.document_extractor._get_ocr_engine", return_value=lambda _image: None), \
         patch("backend.app.services.image_vision_service._call_vision_models", return_value=(None, None)):
        with pytest.raises(ValueError, match="Could not read image"):
            analyze_chat_image("blank.png", _png_bytes())
    with pytest.raises(ValueError, match="not a readable image"):
        analyze_chat_image("fake.jpg", b"not an image")


def test_image_without_readable_text_and_invalid_png_fail_cleanly():
    with patch("backend.app.services.document_extractor._get_ocr_engine", return_value=lambda _image: None):
        with pytest.raises(ValueError, match="No readable text"):
            extract_document("empty.png", _png_bytes())
    with pytest.raises(ValueError, match="valid PNG"):
        extract_document("fake.png", b"not an image")


def test_attachment_payload_limits_and_supported_format_validation():
    with pytest.raises(ValueError):
        DocumentAttachment(name="empty.txt", text="")
    with pytest.raises(ValueError, match="Unsupported file format"):
        extract_document("legacy.doc", b"not supported")
    with pytest.raises(ValueError):
        MessageCreate(content="Question", attachments=[
            DocumentAttachment(name=f"{index}.txt", text="text") for index in range(MAX_ATTACHMENTS + 1)
        ])

    extracted = extract_document("large.txt", ("SAP BRIM details. " * 10000).encode())
    assert extracted["char_count"] <= 150000
    assert len(extracted["text"]) <= 150000


def test_long_file_selects_question_relevant_text_instead_of_only_the_start():
    early = "Unrelated introductory notes about the project. " * 80
    target = "Order process status: the FAILED invoice transfer is waiting for reconciliation."
    passages = RAGService._attachment_passages(
        "Why did the invoice transfer fail?",
        [{"name": "operations.txt", "page_count": 1, "text": early + target}],
    )
    context = LLMService._format_evidence_context(passages)

    assert "FAILED invoice transfer" in context
