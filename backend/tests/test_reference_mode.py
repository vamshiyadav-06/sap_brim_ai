"""Reference mode: retrieved passages are reference notes, not ground truth. The model answers as an expert,
may correct a wrong reference or fill gaps from its own knowledge, and claim verification no longer deletes,
repairs or flags the answer (it stays an admin-only diagnostic). REFERENCE_MODE_ENABLED=False restores the
strict evidence-only behavior (offline fakes)."""

import pytest

from backend.app.config import settings

from backend.app.services.llm_service import (
    EVIDENCE_AWARE_ANSWER_INSTRUCTIONS,
    GenerationResult,
    REFERENCE_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    answer_system_prompt,
    llm_service,
)
import sys

from backend.app.services.grounding_service import grounding_service
from backend.app.services.rag_service import rag_service
from backend.app.services.web_search_service import web_search_service

rag_module = sys.modules["backend.app.services.rag_service"]  # the package attribute is the instance

EVIDENCE = [{
    "evidence_id": "E1", "document_name": "BRIM guide", "page_number": 3, "section": "Billing",
    "chunk_text": "A billable item is posted directly to FI-CA without invoicing.", "source_type": "knowledge_base",
}]


def _prompt(query="How is a billable item processed?", evidence=EVIDENCE):
    return llm_service._build_answer_prompt(query, evidence, "knowledge_base")


@pytest.fixture
def reference(monkeypatch):
    monkeypatch.setattr(settings, "REFERENCE_MODE_ENABLED", True)


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.setattr(settings, "REFERENCE_MODE_ENABLED", False)


# ---- prompts ----------------------------------------------------------------------------------------

def test_reference_prompt_treats_passages_as_notes_that_may_be_wrong(reference):
    prompt = _prompt()
    assert "REFERENCE MATERIAL" in prompt and "may be incomplete, outdated or wrong" in prompt
    assert "starting point only" in prompt and "fix what is wrong" in prompt
    assert "using only the evidence above" not in prompt
    assert "Every factual SAP statement must be supported by a passage" not in prompt
    assert "A billable item is posted directly to FI-CA" in prompt  # the reference text is still provided


def test_reference_system_prompt_lets_the_model_correct_and_fill_gaps(reference):
    assert answer_system_prompt() is REFERENCE_SYSTEM_PROMPT
    text = REFERENCE_SYSTEM_PROMPT
    assert "NOT as the truth" in text
    assert "If a reference is wrong" in text and "give the correct answer" in text
    assert "answer fully from your own knowledge" in text
    # An experienced person does not guess identifiers.
    assert "Do not invent transaction codes" in text and "confirm the exact name" in text


def test_no_reference_material_still_asks_for_an_expert_answer(reference):
    prompt = _prompt(evidence=[])
    assert "None found. Answer from your own SAP expertise." in prompt


def test_strict_mode_keeps_the_evidence_only_prompts(strict):
    prompt = _prompt()
    assert answer_system_prompt() is SYSTEM_PROMPT
    assert "using only the evidence above" in prompt and EVIDENCE_AWARE_ANSWER_INSTRUCTIONS in prompt
    assert "REFERENCE MATERIAL" not in prompt


def test_providers_receive_the_mode_system_prompt(reference, monkeypatch):
    seen = []
    monkeypatch.setattr(llm_service, "_has_api_key", lambda provider: provider == "groq")
    monkeypatch.setattr(llm_service, "_get_models", lambda *a, **k: ["m"])
    monkeypatch.setattr(llm_service, "_call_groq", lambda prompt, system, **k: seen.append(system) or "answer")
    result = GenerationResult()
    assert llm_service.generate_answer("q", EVIDENCE, result=result) == "answer"
    assert seen == [REFERENCE_SYSTEM_PROMPT]


# ---- verification no longer rewrites the answer -----------------------------------------------------

FAILING = {
    "grounding_score": 0.2, "verification_status": "unverified", "passes_grounding_gate": False,
    "unsupported_claims": ["c1"], "critical_unsupported_claims": ["c1"],
    "critical_failures": [{"claim": "Use FPXX", "identifier": "FPXX", "status": "unconfirmed_identifier"}],
    "contradicted_claims": ["c2"],
}


def _forbid(*_a, **_k):
    raise AssertionError("must not be called in reference mode")


def test_reference_mode_does_not_repair_or_strip_a_corrected_answer(reference, monkeypatch):
    monkeypatch.setattr(llm_service, "repair_answer", _forbid)
    monkeypatch.setattr(rag_module, "apply_guard", _forbid)
    answer = "In standard SAP a billable item is invoiced first, then posted [E1]. Use FPXX to check it."
    final, verification, repair, status = rag_service._finalize_answer(
        "q", answer, EVIDENCE, "knowledge_base", None, None, verification=dict(FAILING),
    )
    assert final == answer  # the expert's correction is kept as written
    assert repair["attempted"] is False and repair["guard_applied"] is False
    assert verification["grounding_score"] == 0.2 and status == "unverified"  # diagnostic only


def test_reference_mode_still_removes_markers_that_point_to_no_passage(reference):
    final, *_ = rag_service._finalize_answer(
        "q", "Fact one [E1]. Fact two [E9].", EVIDENCE, "knowledge_base", None, None, verification=dict(FAILING),
    )
    assert "[E1]" in final and "[E9]" not in final


def test_strict_mode_still_repairs_and_guards(strict, monkeypatch):
    called = {}
    monkeypatch.setattr(llm_service, "repair_answer", lambda **k: called.setdefault("repair", True) and "")
    monkeypatch.setattr(
        rag_module, "apply_guard",
        lambda *a, **k: called.setdefault("guard", True) and {"applied": False, "answer": a[0], "removed_claims": []},
    )
    rag_service._finalize_answer("q", "Use FPXX [E1].", EVIDENCE, "knowledge_base", None, None, verification=dict(FAILING))
    assert called == {"repair": True, "guard": True}


# ---- whole pipeline ----------------------------------------------------------------------------------

def _pipeline(monkeypatch, answer):
    """process_query with fixed evidence and a fake model whose answer fails claim verification."""
    monkeypatch.setattr(rag_service, "_retrieve_aspect_evidence", lambda *a, **k: (list(EVIDENCE), 0.9, []))
    monkeypatch.setattr(rag_service, "_should_trigger_web_fallback", lambda *a, **k: False)
    monkeypatch.setattr(rag_service, "_should_auto_compare", lambda *a, **k: False)
    monkeypatch.setattr(settings, "MULTI_SOURCE_COMPARE_ENABLED", False)
    monkeypatch.setattr(settings, "REPO_SOURCE_ENABLED", False)
    monkeypatch.setattr(llm_service, "generate_answer", lambda *a, **k: answer)
    monkeypatch.setattr(grounding_service, "verify_answer", lambda *a, **k: dict(FAILING))
    web = []
    monkeypatch.setattr(web_search_service, "search_sap_authoritative", lambda *a, **k: web.append(1) or [])
    return web


def test_pipeline_publishes_the_experts_answer_without_notice_or_web_retry(reference, monkeypatch):
    answer = "A billable item is invoiced first and posted afterwards (the reference text is wrong about that)."
    web = _pipeline(monkeypatch, answer)
    result = rag_service.process_query(None, "How is a billable item processed in SAP BRIM?", chat_history=[])
    assert result["answer"].startswith(answer)
    assert "could not be confirmed" not in result["answer"]  # no unverified notice
    assert web == []  # a failed claim check no longer triggers a second, web-backed generation


def test_pipeline_strict_mode_still_appends_the_notice(strict, monkeypatch):
    web = _pipeline(monkeypatch, "A billable item is invoiced first and posted afterwards.")
    monkeypatch.setattr(llm_service, "repair_answer", lambda **k: "")
    result = rag_service.process_query(None, "How is a billable item processed in SAP BRIM?", chat_history=[])
    assert "could not be confirmed" in result["answer"]
