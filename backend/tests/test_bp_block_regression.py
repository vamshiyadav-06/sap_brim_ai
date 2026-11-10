"""
Regression tests for: "If a Business Partner has a block status, where can we view it and how can
we remove the block manually?"

Expected behavior is derived from the reviewed reference evidence in
fixtures/bp_block_reference_evidence.json (verbatim knowledge-base passages with notes on what each
passage does and does not establish). No SAP answer is hard-coded as correct: the "good" scripted
answer only restates what the passages establish, and the "bad" scripted answer contains the failure
modes the reference notes say are NOT established.

Everything is offline: retrieval, reranking, LLM and web search are mocked; no database is used.
"""

import json
import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.app.config import settings
from backend.app.services.rag_service import rag_service
from backend.app.services.restriction_scope_classifier import CATEGORIES
from backend.app.services.sap_identifier_validator import sap_identifier_validator
from backend.app.services.evidence_markers import MARKER_PATTERN

FIXTURE = Path(__file__).parent / "fixtures" / "bp_block_reference_evidence.json"
REFERENCE = json.loads(FIXTURE.read_text(encoding="utf-8"))
QUESTION = REFERENCE["question"]

# Failure modes the reference review says the evidence does NOT establish.
BAD_ANSWER = """## Viewing the block
You can view the block in table KNB1, where the field CVP_XBLCK_B is the universal blocking indicator for the Business Partner [E1].
Postings to a contract account can be prevented by a central posting block [E4].

## Removing the block
1. Run transaction XK02 for suppliers or FD02 for customers and untick the block flag [E1].
2. Go to SPRO -> Cross-Application Components -> SAP Business Partner -> Unblock Partner.
3. Alternatively, clear CVP_XBLCK_B directly in table KNB1 using SE16N.

Removing the block will automatically release all pending invoices for billing."""

# Repair that still recommends an undocumented removal step.
STILL_BAD_REPAIR = """Postings to a contract account can be prevented by a central posting block [E4].
Use transaction XK02 to remove the supplier block [E1]."""

# Only restates what the reviewed passages establish; ambiguity handled with a clarification.
GOOD_ANSWER = """The retrieved documentation does not describe one universal Business Partner block; it covers different restrictions at different levels.
- FI-CA contract account: postings to a contract account can be prevented by a central posting block, which also prevents clearing and reversal for the account [E4].
- FI-CA locks: you can generate a list of locks under Accounting -> Financial Accounting -> Contract Accounts Receivable and Payable -> Periodic Processing -> For Contract Accounts -> Evaluation of Business Blocks [E4].
- Customer/supplier master data: in the S/4HANA 1709 simplification list, blocked customer master data is identified in table KNB1 when the field CVP_XBLCK_B is set [E1].
- In SAP S/4HANA, the user interface for customer and vendor master data is transaction BP [E3].
The evidence does not establish which transaction removes a Business Partner block, so no removal procedure can be confirmed.
Which block type do you see (general BP, customer, supplier, company code, sales area, purchasing organization, or FI-CA contract account), and in which SAP release?"""


def kb_rows():
    return [
        {
            "chunk_id": p["chunk_id"], "document_name": p["document_name"], "page_number": p["page_number"],
            "section": p["section"], "chunk_text": p["chunk_text"], "snippet": p["chunk_text"][:350],
            "source_type": "knowledge_base", "score": p["score"], "rerank_score": p["score"],
        }
        for p in REFERENCE["passages"]
    ]


@contextmanager
def offline_pipeline(initial_answer, repair_answer=None):
    """Mocks every external dependency; records the prompt guidance the generator received."""
    calls = {"guidance": [], "repair_calls": 0, "repair_kwargs": None}

    def fake_generate(query, passages, source_type="knowledge_base", guidance=None, **kwargs):
        calls["guidance"].append(guidance)
        calls["passages"] = passages
        return initial_answer

    def fake_stream(query, passages, source_type="knowledge_base", guidance=None, **kwargs):
        calls["guidance"].append(guidance)
        calls["passages"] = passages
        for i in range(0, len(initial_answer), 17):
            yield initial_answer[i:i + 17]

    def fake_repair(**kwargs):
        calls["repair_calls"] += 1
        calls["repair_kwargs"] = kwargs
        return repair_answer if repair_answer is not None else kwargs["candidate_answer"]

    def fake_rerank(query, candidates, top_n=4):
        return [dict(c) for c in candidates][:max(top_n, 1)], 0.72, True

    with patch.object(settings, "AUTO_MULTI_SOURCE_FALLBACK", False), \
            patch.object(settings, "WEB_FALLBACK_ENABLED", False), \
            patch.object(settings, "MULTI_SOURCE_COMPARE_ENABLED", False), \
            patch.object(settings, "REPO_SOURCE_ENABLED", False), \
            patch.object(rag_service.retrieval, "retrieve", side_effect=lambda db, q, top_k=15, **kw: kb_rows()), \
            patch.object(rag_service.retrieval, "hybrid_search", side_effect=lambda db, q, top_k=None, **kw: kb_rows()), \
            patch("backend.app.services.rag_service.reranking_service.rerank_kb", side_effect=fake_rerank), \
            patch("backend.app.services.rag_service.web_search_service.search_sap_authoritative", return_value=[]) as web, \
            patch("backend.app.services.rag_service.llm_service.generate_answer", side_effect=fake_generate), \
            patch("backend.app.services.rag_service.llm_service.stream_answer", side_effect=fake_stream), \
            patch("backend.app.services.rag_service.llm_service.repair_answer", side_effect=fake_repair):
        calls["web"] = web
        yield calls


def sentences(text):
    plain = MARKER_PATTERN.sub("", text)
    return [s.strip() for line in plain.split("\n") for s in re.split(r"(?<=[.!?])\s+", line) if s.strip()]


@pytest.fixture(scope="module", autouse=True)
def _strict_grounding():
    """These regressions cover the strict evidence-only pipeline (repair + guard); reference mode is the default."""
    with patch.object(settings, "REFERENCE_MODE_ENABLED", False):
        yield


@pytest.fixture(scope="module")
def bad_then_still_bad():
    with offline_pipeline(BAD_ANSWER, STILL_BAD_REPAIR) as calls:
        result = rag_service.process_query(db=MagicMock(), query=QUESTION)
    return result, calls


@pytest.fixture(scope="module")
def good_answer():
    with offline_pipeline(GOOD_ANSWER) as calls:
        result = rag_service.process_query(db=MagicMock(), query=QUESTION)
    return result, calls


# 1. Distinguishes general BP status from customer/supplier/org-level/FI-CA restrictions
def test_1_block_types_are_distinguished_in_retrieval_and_generation(bad_then_still_bad):
    result, calls = bad_then_still_bad
    guidance = calls["guidance"][0]
    for category in CATEGORIES:
        assert category.label in guidance
    assert "not interchangeable" in guidance.lower()
    scope = result["validation_audit"]["restriction_scope"]
    assert scope["ambiguous"] is True
    assert {"bp_general_status", "customer_master_block", "supplier_master_block", "company_code_restriction",
            "sales_area_restriction", "purchasing_org_restriction", "fica_contract_account_restriction"} <= set(scope["candidate_categories"])
    # Evidence was retrieved per candidate block type with the existing hybrid retrieval.
    assert any(p.get("restriction_scope") for p in calls["passages"])


# 2. CVP_XBLCK_B is not presented as a universal operational blocking indicator
def test_2_cvp_xblck_b_not_presented_as_universal_indicator(bad_then_still_bad):
    result, _ = bad_then_still_bad
    for s in sentences(result["answer"]):
        if "CVP_XBLCK_B" in s:
            assert sap_identifier_validator.is_limitation_statement(s), s
            assert not re.search(r"\bis the universal\b", s)
    assert result["validation_audit"]["repair"]["guard_applied"] is True


# 3. XK02 / FD02 are not recommended as the verified solution
def test_3_xk02_fd02_not_recommended_as_verified_solution(bad_then_still_bad):
    result, calls = bad_then_still_bad
    assert calls["repair_calls"] == 1  # exactly one repair attempt
    for s in sentences(result["answer"]):
        if re.search(r"\b(XK02|FD02)\b", s):
            assert sap_identifier_validator.is_limitation_statement(s) or "not available" in s, s
    assert result["answer_decision"] in ("partial", "rejected")
    assert result["verification_status"] != "verified"


# 4. No invented navigation path
def test_4_invented_navigation_path_is_removed(bad_then_still_bad):
    result, _ = bad_then_still_bad
    assert "Unblock Partner" not in result["answer"]
    assert "Cross-Application Components" not in result["answer"]


# 5. Direct database updates are not the unblocking procedure
def test_5_direct_database_update_not_recommended(bad_then_still_bad):
    result, _ = bad_then_still_bad
    for s in sentences(result["answer"]):
        assert not sap_identifier_validator.detect_direct_db_update(s), s


# 6. States what evidence is needed to determine the correct procedure
def test_6_identifies_evidence_needed(bad_then_still_bad):
    result, _ = bad_then_still_bad
    answer = result["answer"]
    assert "Could you confirm the following" in answer
    assert "organizational level" in answer and "SAP product and release" in answer
    assert "which restriction are you seeing" in answer.lower()


# 7. Qualified answer when the block type is unknown
def test_7_qualified_answer_when_block_type_unknown(bad_then_still_bad, good_answer):
    bad_result, _ = bad_then_still_bad
    assert "Not confirmed by the available SAP documentation" in bad_result["answer"]
    good_result, calls = good_answer
    assert calls["repair_calls"] == 0
    assert good_result["answer_decision"] == "accepted"
    assert good_result["answer"].rstrip().endswith("?")  # targeted clarification kept
    counts = good_result["validation_audit"]["claim_counts"]
    assert counts.get("qualified", 0) >= 1 and counts.get("clarification", 0) == 1


# 8. Citations are next to the claims they support
def test_8_citations_next_to_supported_claims(good_answer):
    result, _ = good_answer
    checks = result["validation_audit"]["citation_checks"]
    assert checks["cited_claims"] >= 4
    assert checks["mismatched"] == 0 and checks["invalid_ids"] == []
    markers = set(re.findall(r"E\d+", " ".join(MARKER_PATTERN.findall(result["answer"]))))
    refs = {c.get("ref") for c in result["citations"]}
    assert markers and markers <= refs  # every inline marker resolves to a returned citation


# 9. No unsupported statements about downstream effects of removing the block
def test_9_unsupported_downstream_effects_removed(bad_then_still_bad):
    result, _ = bad_then_still_bad
    assert "release all pending invoices" not in result["answer"]
    # After the guard, the re-verified published answer carries no effect claim.
    assert "unsupported_effect" not in {f["status"] for f in result["validation_audit"]["critical_failures"]}


# 10. Streaming and API response behavior preserved
LEGACY_DONE_KEYS = {
    "type", "answer", "source_type", "grounding_score", "is_in_rag_pipeline", "kb_score", "web_score",
    "winning_score", "source_scores", "selected_source", "verification_status", "internal_evidence_confidence",
    "selected_evidence_quality", "external_search_used", "citations", "web_sources", "images",
    "follow_up_questions", "selection_summary", "comparison_audit",
}


def test_10_streaming_contract_preserved_and_final_answer_is_validated():
    with offline_pipeline(BAD_ANSWER, STILL_BAD_REPAIR):
        events = list(rag_service.stream_query(db=MagicMock(), query=QUESTION))
    types = [e["type"] for e in events]
    assert types[-1] == "done" and "token" in types and types.index("token") < types.index("done")
    assert {e["stage"] for e in events if e["type"] == "status"} >= {"retrieval", "generating", "verifying_claims"}
    done = events[-1]
    assert LEGACY_DONE_KEYS <= set(done)
    assert "Unblock Partner" not in done["answer"] and "release all pending invoices" not in done["answer"]
    assert done["answer_decision"] in ("partial", "rejected")


def test_10_process_query_contract_preserved(good_answer):
    result, _ = good_answer
    assert (LEGACY_DONE_KEYS - {"type"}) <= set(result)
    for citation in result["citations"]:
        assert {"source_type", "chunk_id", "document", "page", "section", "score", "snippet"} <= set(citation)


def test_10_api_hides_markers_and_validation_details_from_regular_users():
    from backend.app.api import messages as messages_api

    done = {
        "type": "done", "answer": "Central posting block [E4]. Locks expire [E2, E4].",
        "source_type": "knowledge_base", "grounding_score": 0.9, "citations": [{"document": "x", "ref": "E4"}],
        "web_sources": [], "images": [], "validation_audit": {"decision": "accepted"}, "answer_decision": "accepted",
    }
    tokens = ["Central posting block [", "E4]. Locks", " expire [E2, E4]."]

    def run(user):
        events = [{"type": "status", "stage": "retrieval", "message": "Searching internal docs"}]
        events += [{"type": "token", "content": t} for t in tokens] + [dict(done)]
        with patch.object(messages_api, "SessionLocal", return_value=MagicMock()), \
                patch.object(messages_api.rag_service, "stream_query", return_value=iter(events)), \
                patch.object(messages_api, "StreamingResponse", side_effect=lambda gen, **kw: gen):
            raw = list(messages_api.handle_streaming_response("chat-1", QUESTION, chat_history=[], current_user=user))
        return [json.loads(chunk[len("data: "):]) for chunk in raw]

    regular = run(SimpleNamespace(id="u1", is_admin=False, role="consultant", email="u@example.com"))
    streamed = "".join(e["content"] for e in regular if e["type"] == "token")
    final = next(e for e in regular if e["type"] == "done")
    assert "[E" not in streamed and "[E" not in final["answer"]
    assert final.get("validation_audit") is None and final.get("answer_decision") is None and final["citations"] == []

    admin = run(SimpleNamespace(id="a1", is_admin=True, role="admin", email="a@example.com"))
    admin_final = next(e for e in admin if e["type"] == "done")
    assert "[E4]" in admin_final["answer"] and admin_final["validation_audit"] == {"decision": "accepted"}
