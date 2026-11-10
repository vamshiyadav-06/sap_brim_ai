"""Documents on request: the model writes Markdown, the file (Word, PDF, Excel, PowerPoint, CSV, text, Markdown,
HTML) is created locally and stored for its owner, and the chat shows a short message with a download link.
Files are read back with the same extractors users' uploads go through (offline, fake models)."""

import io
import json
import re
import sys
import time
import zipfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app.api import messages as messages_api
from backend.app.config import settings
from backend.app.main import app
from backend.app.security.auth import get_current_user
from backend.app.services import document_generator as dg
from backend.app.services import intent_analyzer as ia
from backend.app.services.conversation_memory_service import conversation_memory_service
from backend.app.services.document_extractor import extract_document
from backend.app.services.grounding_service import grounding_service
from backend.app.services.llm_service import UNAVAILABLE_RESPONSE, llm_service
from backend.app.services.rag_service import rag_service
from backend.app.services.web_search_service import web_search_service

from backend.tests.test_chat_modes import FakeSession, _sse_events

MD = """# SAP BRIM Billing Overview

An intro with **bold**, *italic*, `code` and a [link](https://help.sap.com) -> done [E1].

## Key components

- Subscription Order Management
- Convergent Charging
  - Rating
- Convergent Invoicing

1. First step
2. Second step

## Comparison

| Component | Role | Release |
|---|---|---|
| SOM | Orders | 2023 |
| CC | Rating | 2022 |
| CI | Invoicing | 1,250 |

```
SELECT * FROM dfkkop;
```
"""


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "GENERATED_FILES_DIR", tmp_path / "generated")
    monkeypatch.setattr(settings, "GENERATED_FILE_TTL_DAYS", 14)
    monkeypatch.setattr(settings, "DOCUMENT_GENERATION_ENABLED", True)
    monkeypatch.setattr(dg, "_last_purge", 0.0)


def _forbid(*_a, **_k):
    raise AssertionError("must not be called on this path")


# ---- is a file being asked for? ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Create a PDF about SAP BRIM billing", ["pdf"]),
    ("make me an excel sheet of the billing steps", ["xlsx"]),
    ("Generate a PowerPoint presentation on convergent invoicing", ["pptx"]),
    ("write a word document explaining dunning", ["docx"]),
    ("Prepare a report document on FI-CA", ["docx"]),
    ("export this as a CSV", ["csv"]),
    ("give me a pdf and a word file of the summary", ["pdf", "docx"]),
    ("What is a PDF?", []),
    ("How do I export a PDF from SAP?", []),
    ("Explain SAP BRIM billing", []),
])
def test_rule_based_fallback_detects_requests_not_questions(text, expected):
    assert sorted(dg.detect_formats(text)) == sorted(expected)


def test_formats_are_normalised_and_limited():
    assert dg.normalize_formats(["Word", "EXCEL", "powerpoint", "pdf", "bogus", "word"]) == ["docx", "xlsx", "pptx"]
    assert dg.normalize_formats("pdf") == []
    assert dg.requested_formats(SimpleNamespace(output_formats=["pdf", "bad"]), "x") == ["pdf"]


def test_analyzer_reads_output_formats_and_small_talk_never_creates_files():
    raw = json.dumps({"category": "sap_brim", "corrected_question": "Create a PDF about billing.", "about_sap": True,
                      "output_formats": ["PDF", "word"]})
    assert ia.parse_analysis(raw, "create a pdf about biling").output_formats == ["pdf", "docx"]
    chat = json.dumps({"category": "general", "small_talk": True, "output_formats": ["pdf"]})
    assert ia.parse_analysis(chat, "thanks").output_formats == []
    assert ia.parse_analysis(json.dumps({"category": "general"}), "hi there").output_formats == []


# ---- the files ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", list(dg.FORMATS))
def test_every_format_is_a_valid_file_with_the_content(fmt):
    data = dg.render(MD, fmt, "SAP BRIM Billing Overview")
    text = extract_document(f"t.{fmt}", data)["text"]
    assert "Subscription Order Management" in text and "Convergent Invoicing" in text or fmt in ("xlsx", "csv")
    assert "[E1]" not in text  # evidence markers are never part of a document
    if fmt in ("docx", "pdf", "pptx", "txt", "md", "html"):
        assert "sap brim billing overview" in text.lower() and "SELECT * FROM dfkkop" in text
    if fmt in ("xlsx", "csv", "docx", "pdf", "pptx", "txt", "html"):
        assert "Rating" in text and "1,250" in text.replace("1250", "1,250")


def test_pdf_is_a_real_multi_section_document():
    import fitz
    document = fitz.open("pdf", dg.render(MD, "pdf", "T"))
    assert len(document) >= 1 and "Comparison" in document[0].get_text()
    long = "# Big\n\n" + "\n\n".join(f"Paragraph {i} " + "text " * 60 for i in range(80))
    assert len(fitz.open("pdf", dg.render(long, "pdf", "Big"))) > 2


def test_pptx_has_a_title_slide_a_slide_per_section_and_splits_long_lists():
    from pptx import Presentation
    deck = Presentation(io.BytesIO(dg.render(MD, "pptx", "T")))
    titles = [s.shapes.title.text_frame.text for s in deck.slides]
    assert titles[0] == "SAP BRIM Billing Overview" and "Key components" in titles and "Comparison" in titles
    bullets = "\n".join(f"- point {i}" for i in range(20))
    long = Presentation(io.BytesIO(dg.render(f"# Deck\n\n## Many\n\n{bullets}\n", "pptx", "Deck")))
    assert [s.shapes.title.text_frame.text for s in long.slides][1:] == ["Many", "Many (cont.)", "Many (cont.)"]


def test_xlsx_has_a_sheet_per_table_with_numbers_and_no_formulas():
    md = "# Data\n\n## Sales\n\n| Item | Qty |\n|---|---|\n| =SUM(A1) | 7 |\n| @cmd | 007 |\n| ok | 12.5 |\n\n## Costs\n\n| A | B |\n|---|---|\n| 1 | 2 |\n"
    data = dg.render(md, "xlsx", "Data")
    archive = zipfile.ZipFile(io.BytesIO(data))
    workbook = archive.read("xl/workbook.xml").decode()
    assert 'name="Sales"' in workbook and 'name="Costs"' in workbook
    sheet = archive.read("xl/worksheets/sheet1.xml").decode()
    assert "<f>" not in sheet  # typed text never becomes a formula
    assert re.search(r"<c r=\"B2\"[^>]*><v>7</v>", sheet) and not re.search(r"<c r=\"B3\"[^>]*t=\"n\"", sheet)  # 007 stays text
    assert 'name="Content"' in zipfile.ZipFile(io.BytesIO(dg.render("# T\n\nJust text", "xlsx", "T"))).read("xl/workbook.xml").decode()


def test_csv_is_excel_friendly_and_formula_safe():
    csv_text = dg.render("| a | b |\n|---|---|\n| =1+1 | -5 |\n| x,y | \"q\" |\n", "csv", "T").decode("utf-8")
    assert csv_text.startswith("﻿") and "'=1+1" in csv_text and ",-5" in csv_text and '"x,y"' in csv_text


def test_docx_keeps_structure_and_real_hyperlinks():
    from docx import Document
    document = Document(io.BytesIO(dg.render(MD, "docx", "T")))
    assert [p.text for p in document.paragraphs if p.style.name.startswith("Heading")][:2] == ["SAP BRIM Billing Overview", "Key components"]
    assert len(document.tables) == 1 and document.tables[0].cell(0, 0).text == "Component"
    assert any("help.sap.com" in rel.target_ref for rel in document.part.rels.values())


def test_script_links_are_not_turned_into_links():
    data = dg.render("# T\n\n[click](javascript:alert(1)) and <script>x</script>", "docx", "T")
    from docx import Document
    assert not any("javascript" in rel.target_ref for rel in Document(io.BytesIO(data)).part.rels.values())
    assert "<script>" not in dg.render("# T\n\n<script>alert(1)</script>", "html", "T").decode()


# ---- storage, ownership, expiry --------------------------------------------------------------------------

def test_created_files_are_linked_and_belong_to_their_owner():
    message, files = dg.create_documents("u1", MD, ["pdf", "docx"], request_title="billing")
    assert [f.fmt for f in files] == ["pdf", "docx"] and message.startswith("I've created **SAP BRIM Billing Overview**")
    for f in files:
        assert f"[{f.filename}]({dg.LINK_PREFIX}{f.id})" in message
    assert "Key components" in message and "Subscription Order Management" not in message  # content is in the file
    path, meta = dg.load_file("u1", files[0].id)
    assert path.read_bytes()[:4] == b"%PDF" and meta["filename"] == "SAP_BRIM_Billing_Overview.pdf"
    assert dg.load_file("u2", files[0].id) is None and dg.load_file("u1", "../../etc/passwd") is None


def test_expired_files_are_unavailable_and_purged(monkeypatch):
    _, files = dg.create_documents("u1", MD, ["txt"])
    meta_path = settings.GENERATED_FILES_DIR / f"{files[0].id}.json"
    meta = json.loads(meta_path.read_text())
    meta["created"] = time.time() - 15 * 86400
    meta_path.write_text(json.dumps(meta))
    assert dg.load_file("u1", files[0].id) is None
    import os
    old = time.time() - 15 * 86400
    for item in settings.GENERATED_FILES_DIR.iterdir():
        os.utime(item, (old, old))
    assert dg.purge_expired(force=True) == 2 and list(settings.GENERATED_FILES_DIR.iterdir()) == []


def test_if_no_file_can_be_created_the_content_is_not_lost(monkeypatch):
    monkeypatch.setattr(dg, "render", _forbid)
    message, files = dg.create_documents("u1", "# Title\n\nBody text", ["pdf"])
    assert files == [] and "Body text" in message and "couldn't create the file" in message


# ---- download endpoint --------------------------------------------------------------------------------------

def test_download_endpoint_serves_only_the_owners_file():
    _, files = dg.create_documents("u1", MD, ["xlsx"])
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="u1", is_admin=False)
    client = TestClient(app)
    ok = client.get(f"/api/files/{files[0].id}")
    assert ok.status_code == 200 and ok.content[:2] == b"PK"
    assert "attachment" in ok.headers["content-disposition"] and ok.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/files/" + "0" * 32).status_code == 404
    assert client.get("/api/files/not-an-id").status_code == 404
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="u2", is_admin=True)
    assert client.get(f"/api/files/{files[0].id}").status_code == 404  # not even an admin


def test_download_requires_login():
    app.dependency_overrides.clear()
    assert TestClient(app).get("/api/files/" + "0" * 32).status_code in (401, 403)


# ---- Scholar --------------------------------------------------------------------------------------------------

def _model(monkeypatch, **fields):
    base = {"category": "sap_brim", "about_sap": True, "small_talk": False, "needs_clarification": False,
            "clarification_question": "", "output_formats": []}
    base.update(fields)

    def fake(prompt):
        question = prompt.split("<<<\n", 1)[1].split("\n>>>", 1)[0]
        data = dict(base)
        data.setdefault("corrected_question", question)
        data.setdefault("standalone_question", data["corrected_question"])
        return json.dumps(data)

    monkeypatch.setattr(llm_service, "analyze_question_intent", fake)


def test_scholar_general_document_request_returns_a_link_not_the_text(monkeypatch):
    _model(monkeypatch, category="general", about_sap=False, output_formats=["pdf"],
           corrected_question="Create a PDF about volcanoes.")
    seen = {}

    def reply(q, chat_history=None, regeneration_index=None):
        seen["q"] = q
        return "# Volcanoes\n\n## Types\n\n- Shield\n- Cone\n"

    monkeypatch.setattr(llm_service, "generate_general_reply", reply)
    result = rag_service.process_query(None, "make a pdf about volcanos", chat_history=[], user=SimpleNamespace(id="u1"))
    assert "DOCUMENT REQUEST" in seen["q"] and "Create a PDF about volcanoes." in seen["q"]
    assert re.search(r"\[Volcanoes\.pdf\]\(/generated-files/[0-9a-f]{32}\)", result["answer"])
    assert "Shield" not in result["answer"] and result["route"] == "GENERAL"
    file_id = re.search(r"/generated-files/([0-9a-f]{32})", result["answer"]).group(1)
    assert dg.load_file("u1", file_id) is not None


def test_scholar_streaming_hides_the_content_and_sends_only_the_message(monkeypatch):
    _model(monkeypatch, category="general", about_sap=False, output_formats=["docx"])
    monkeypatch.setattr(llm_service, "stream_general_reply",
                        lambda q, chat_history=None, regeneration_index=None: iter(["# Plan\n\n", "## Step\n\n", "Do it."]))
    events = list(rag_service.stream_query(None, "write a word doc about planning", chat_history=[], user=SimpleNamespace(id="u1")))
    streamed = "".join(e["content"] for e in events if e["type"] == "token")
    assert "[Plan.docx](/generated-files/" in streamed and "Do it." not in streamed
    assert events[-1]["type"] == "done" and events[-1]["answer"] == streamed


def test_scholar_sap_document_request_uses_retrieval_and_writes_the_document(monkeypatch):
    _model(monkeypatch, category="sap_brim", about_sap=True, output_formats=["xlsx"],
           corrected_question="Create an Excel sheet of billing steps.")
    evidence = [{"evidence_id": "E1", "document_name": "BRIM", "page_number": 1, "section": "S",
                 "chunk_text": "Billing runs invoice billable items.", "source_type": "knowledge_base"}]
    monkeypatch.setattr(rag_service, "_retrieve_aspect_evidence", lambda *a, **k: (list(evidence), 0.9, []))
    monkeypatch.setattr(rag_service, "_should_trigger_web_fallback", lambda *a, **k: False)
    monkeypatch.setattr(rag_service, "_should_auto_compare", lambda *a, **k: False)
    monkeypatch.setattr(settings, "MULTI_SOURCE_COMPARE_ENABLED", False)
    monkeypatch.setattr(settings, "REPO_SOURCE_ENABLED", False)
    monkeypatch.setattr(web_search_service, "search_sap_authoritative", lambda *a, **k: [])
    monkeypatch.setattr(grounding_service, "verify_answer", lambda *a, **k: {"grounding_score": 0.9, "verification_status": "verified", "passes_grounding_gate": True})
    prompts = {}

    def generate(query, passages, source_type="knowledge_base", guidance=None, **k):
        prompts["guidance"] = guidance
        return "# Billing steps\n\n| Step | Action |\n|---|---|\n| 1 | Rate [E1] |\n| 2 | Invoice |\n"

    monkeypatch.setattr(llm_service, "generate_answer", generate)
    result = rag_service.process_query(None, "make an excel of billing steps", chat_history=[], user=SimpleNamespace(id="u7"))
    assert "DOCUMENT REQUEST" in prompts["guidance"] and "Markdown tables" in prompts["guidance"]
    file_id = re.search(r"/generated-files/([0-9a-f]{32})", result["answer"]).group(1)
    path, meta = dg.load_file("u7", file_id)
    assert meta["filename"] == "Billing_steps.xlsx" and "Invoice" in extract_document("x.xlsx", path.read_bytes())["text"]


def test_without_a_model_the_rule_based_detection_still_creates_documents(monkeypatch):
    monkeypatch.setattr(llm_service, "generate_answer", lambda *a, **k: "# Dunning in FI-CA\n\nDunning notifies customers.")
    monkeypatch.setattr(rag_service, "_retrieve_aspect_evidence", lambda *a, **k: ([], 0.0, []))
    monkeypatch.setattr(rag_service, "_should_trigger_web_fallback", lambda *a, **k: False)
    monkeypatch.setattr(rag_service, "_should_auto_compare", lambda *a, **k: False)
    monkeypatch.setattr(settings, "MULTI_SOURCE_COMPARE_ENABLED", False)
    monkeypatch.setattr(settings, "REPO_SOURCE_ENABLED", False)
    monkeypatch.setattr(web_search_service, "search_sap_authoritative", lambda *a, **k: [])
    result = rag_service.process_query(None, "create a word document about dunning in SAP FI-CA", chat_history=[], user=SimpleNamespace(id="u1"))
    assert "/generated-files/" in result["answer"] and "Dunning notifies" not in result["answer"]


def test_a_normal_question_is_unaffected(monkeypatch):
    _model(monkeypatch, category="general", about_sap=False)
    monkeypatch.setattr(llm_service, "generate_general_reply", lambda q, chat_history=None, regeneration_index=None: "Paris." if "DOCUMENT" not in q else "BAD")
    result = rag_service.process_query(None, "capital of france", chat_history=[], user=SimpleNamespace(id="u1"))
    assert result["answer"] == "Paris." and not list(settings.GENERATED_FILES_DIR.glob("*"))


def test_document_generation_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "DOCUMENT_GENERATION_ENABLED", False)
    assert dg.requested_formats(SimpleNamespace(output_formats=["pdf"]), "make a pdf") == []


# ---- Sprint ------------------------------------------------------------------------------------------------------

def test_sprint_document_request_creates_the_file(monkeypatch):
    _model(monkeypatch, category="sap_brim", about_sap=True, output_formats=["pptx"],
           corrected_question="Create a PowerPoint about CC.")
    asked = {}

    def reply(q, **k):
        asked["q"] = q
        return "# CC deck\n\n## Rating\n\n- rates events\n"

    monkeypatch.setattr(llm_service, "generate_sprint_reply", reply)
    result = messages_api._sprint_result("make a ppt about CC", [], [], owner_id="u5")
    assert "DOCUMENT REQUEST" in asked["q"] and result["source_type"] == "sprint"
    assert "[CC_deck.pptx](/generated-files/" in result["answer"] and "rates events" not in result["answer"]


def test_sprint_stream_sends_only_the_message(monkeypatch):
    _model(monkeypatch, category="general", about_sap=False, output_formats=["pdf"])
    monkeypatch.setattr(llm_service, "stream_general_reply",
                        lambda q, chat_history=None, regeneration_index=None: iter(["# Trip\n\n", "Secret body."]))
    monkeypatch.setattr(conversation_memory_service, "update_rolling_summary", lambda *_a, **_k: None)
    session = FakeSession()
    monkeypatch.setattr(messages_api, "SessionLocal", lambda: session)
    monkeypatch.setattr(messages_api, "issue_stream_token", lambda *_a: "signed")
    response = messages_api.handle_sprint_streaming_response(
        "chat-id", "make a pdf trip plan", chat_history=[],
        current_user=SimpleNamespace(id="u9", is_admin=True), attachments=[], user_message_id="m1",
    )
    events = _sse_events(response)
    tokens = "".join(e["content"] for e in events if e["type"] == "token")
    done = next(e for e in events if e["type"] == "done")
    assert "[Trip.pdf](/generated-files/" in tokens and "Secret body" not in tokens
    assert done["answer"] == tokens and done["source_type"] == "direct_llm"


def test_sprint_failure_to_generate_content_creates_no_file(monkeypatch):
    _model(monkeypatch, category="general", about_sap=False, output_formats=["pdf"])
    monkeypatch.setattr(llm_service, "generate_general_reply", lambda q, chat_history=None, regeneration_index=None: UNAVAILABLE_RESPONSE)
    result = messages_api._sprint_result("make a pdf about x", [], [], owner_id="u1")
    assert result["answer"] == UNAVAILABLE_RESPONSE and not list(settings.GENERATED_FILES_DIR.glob("*"))
