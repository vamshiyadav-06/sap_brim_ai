"""
Answer Style Router: routing decisions, prompt directives, fallback, and pipeline/SSE integration.

Offline: the LLM classifier and generation are mocked; no API keys or network are needed.
"""

from unittest.mock import MagicMock, patch

import pytest

from backend.app.config import settings
from backend.app.services import answer_style_router as router_module
from backend.app.services.answer_planner import answer_planner
from backend.app.services.answer_style_router import AnswerStyleRouter, answer_style_router, render_style_instructions
from backend.app.services.llm_service import llm_service
from backend.app.services.rag_service import rag_service
from backend.tests.test_bp_block_regression import GOOD_ANSWER, QUESTION, offline_pipeline

KB = [{"source_type": "knowledge_base", "evidence_id": "E1", "document_name": "CI Guide", "page_number": 4,
       "chunk_text": "Billable items are grouped into billing documents by the billing process [E1]."}]
WEB = [{"source_type": "web", "evidence_id": "E2", "url": "https://help.sap.com/ci", "title": "SAP Help: Billing",
        "domain": "help.sap.com", "snippet": "Billing groups billable items using grouping criteria."}]
ATTACHMENT = [{"source_type": "attached_document", "evidence_id": "E1", "document_name": "spec.pdf",
               "chunk_text": "Interface /CUST/BIT_INBOUND transmits BIT records nightly."}]
GAP = [{"source_type": "retrieval_gap", "retrieval_gap": True, "chunk_text": "No passage found for part 2."}]


@pytest.fixture(autouse=True)
def _clean_classifier(monkeypatch):
    """Isolate classifier cache and never reach a real provider."""
    router_module._classifier_cache.clear()
    monkeypatch.setattr(llm_service, "classify_answer_style", MagicMock(return_value=None))
    yield
    router_module._classifier_cache.clear()


def route(question, evidence=KB, raw=None):
    return answer_style_router.route(question, evidence, raw_question=raw)


def prompt_for(question, evidence=KB, raw=None):
    plan = route(question, evidence, raw)
    return plan, llm_service._build_answer_prompt(question, evidence, "knowledge_base", answer_plan=plan)


# 1. Simple factual question -> concise direct answer
def test_simple_factual_question_gets_concise_direct_answer():
    plan, prompt = prompt_for("What is transaction FPC1 used for?")
    assert plan.answer_type == "direct_answer"
    assert plan.detail_level == "concise" and plan.max_sections == 0 and not plan.needs_table
    assert "Give the answer in the first sentence" in prompt
    assert "Do not use headings." in prompt


# 2. Technical explanation does not default to a table
def test_technical_explanation_is_not_a_table_by_default():
    plan, prompt = prompt_for("How does SAP Convergent Invoicing aggregate billable items into billing documents?")
    assert plan.answer_type == "technical_explanation"
    assert plan.needs_table is False
    assert "Do not use a Markdown table unless the evidence contains genuinely tabular data." in prompt
    assert "A table is appropriate here" not in prompt


def test_cross_component_posting_question_gets_detailed_ordered_explanation():
    question = "How does Convergent Invoicing post billing and invoice documents to FI-CA?"
    plan, prompt = prompt_for(question)

    assert plan.answer_type == "technical_explanation"
    assert plan.domain_archetype == "integration"
    assert plan.detail_level == "detailed"
    assert plan.needs_steps is True
    assert "Be comprehensive and thorough on every part the evidence supports" in prompt
    assert "Present any ordered sequence or process flow as a numbered list." in prompt


def test_simple_component_definition_stays_concise():
    plan = route("What is FI-CA?")

    assert plan.answer_type == "direct_answer"
    assert plan.detail_level == "concise"


# 3. Explicit table request produces a table
def test_explicit_table_request_enables_table():
    plan, prompt = prompt_for("Explain the billable item statuses in a table")
    assert plan.needs_table is True and "table" in plan.explicit_requests
    assert "The user explicitly asked for a table" in prompt


# 4. Comparison format
def test_comparison_question_uses_comparison_format():
    plan, prompt = prompt_for("What is the difference between Convergent Invoicing and FI-CA?")
    assert plan.answer_type == "comparison" and plan.needs_table is True
    assert "Do not restate the table in prose." in prompt


def test_comparison_wording_planner_misses_is_still_a_comparison():
    assert route("Billing documents vs invoicing documents: which is better for reporting?").answer_type == "comparison"


# 5. How-to produces ordered steps
@pytest.mark.parametrize("question", [
    "How do I create a billable item class?",
    "How to configure billable item classes in SPRO?",
])
def test_how_to_question_gets_ordered_steps(question):
    plan, prompt = prompt_for(question)
    assert plan.answer_type == "step_by_step" and plan.needs_steps
    assert "give the steps as a numbered list in order" in prompt
    assert "if the evidence does not document the procedure, say so instead of writing steps" in prompt


# 6. Troubleshooting separates confirmed evidence from hypotheses
def test_troubleshooting_separates_confirmed_from_suspected():
    plan, prompt = prompt_for("Billing run fails with error message FKKBIX 012, how do I fix it?")
    assert plan.answer_type == "troubleshooting"
    assert '"Confirmed" only when a cited passage establishes it' in prompt
    assert "Never present a suspected cause as confirmed." in prompt


def test_issued_is_not_mistaken_for_an_issue():
    assert answer_planner.plan_answer("How are invoices issued in Convergent Invoicing?")["question_type"] != "troubleshooting"
    assert route("How are invoices issued in Convergent Invoicing?").answer_type == "technical_explanation"


# 7. Document questions keep citations
def test_document_question_keeps_citation_rules_and_evidence_ids():
    plan, prompt = prompt_for("Which RFC function transmits BIT records?", evidence=ATTACHMENT)
    assert plan.answer_type == "document_research" and plan.needs_citations
    assert "E1 | Attached Document: spec.pdf" in prompt
    assert "Put that passage's ID in square brackets immediately after the claim" in prompt
    assert "cite them" in prompt


# 8. Web results stay distinguishable from internal evidence
def test_hybrid_evidence_keeps_web_sources_distinguishable():
    plan, prompt = prompt_for("How does billing group billable items?", evidence=KB + WEB)
    assert plan.source_mode == "hybrid"
    assert "Make clear which statements rest on web sources" in prompt
    assert "E2 | Web: SAP Help: Billing" in prompt and "E1 | Document: CI Guide" in prompt


def test_web_only_evidence_is_not_presented_as_internal():
    plan = route("How does billing group billable items?", evidence=WEB)
    assert plan.source_mode == "web"
    assert "do not present it as internal documentation" in render_style_instructions(plan)


# 9. Insufficient evidence -> no fabricated citations
def test_no_citable_evidence_forbids_citations():
    plan, prompt = prompt_for("How does billing group billable items?", evidence=GAP)
    assert plan.needs_citations is False
    assert "cite nothing, and say plainly what could not be established" in prompt
    assert "Omit any part the evidence cannot support" in prompt


# 10. Classification failure -> safe fallback
AMBIGUOUS = "Billable item aggregation in CI"


def test_classifier_exception_falls_back_safely():
    llm_service.classify_answer_style.side_effect = RuntimeError("provider down")
    plan = route(AMBIGUOUS)
    assert plan.routed_by == "fallback"
    assert plan.answer_type == "technical_explanation" and plan.detail_level == "balanced"


@pytest.mark.parametrize("reply", [
    "I think it should be an explanation.",
    '{"answer_type": "essay", "detail_level": "balanced"}',
    '{"answer_type": "comparison", "detail_level": "huge"}',
])
def test_invalid_classifier_output_falls_back(reply):
    llm_service.classify_answer_style.return_value = reply
    assert route(AMBIGUOUS).routed_by == "fallback"


def test_valid_classifier_output_is_used_and_cached():
    llm_service.classify_answer_style.return_value = (
        'Here you go: {"answer_type": "technical_explanation", "detail_level": "detailed", '
        '"needs_table": false, "needs_steps": true}'
    )
    first, second = route(AMBIGUOUS), route(AMBIGUOUS)
    assert first.routed_by == "llm" and first.detail_level == "detailed" and first.needs_steps
    assert second == first
    assert llm_service.classify_answer_style.call_count == 1


def test_rules_decide_obvious_cases_without_an_llm_call():
    for q in ["What is transaction FPC1 used for?", "How do I create a billable item class?",
              "Difference between CI and FI-CA", "Explain the billing process"]:
        route(q)
    llm_service.classify_answer_style.assert_not_called()


def test_classifier_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "ANSWER_STYLE_LLM_CLASSIFIER_ENABLED", False)
    assert route(AMBIGUOUS).routed_by == "fallback"
    llm_service.classify_answer_style.assert_not_called()


def test_routing_exception_never_breaks_generation(monkeypatch):
    monkeypatch.setattr(AnswerStyleRouter, "_route", MagicMock(side_effect=ValueError("boom")))
    plan = route("anything")
    assert plan.routed_by == "fallback" and plan.answer_type == "technical_explanation"


# 11. SSE streaming keeps working and carries the plan internally only
def test_streaming_pipeline_passes_plan_and_keeps_event_contract():
    captured = {}

    def capturing_generate(query, passages, source_type="knowledge_base", guidance=None, answer_plan=None, **kwargs):
        captured["plan"] = answer_plan
        return GOOD_ANSWER

    # The streaming path generates (and verifies) the whole draft before streaming it (F-03/F-23).
    with offline_pipeline(GOOD_ANSWER) as calls, \
            patch("backend.app.services.rag_service.llm_service.generate_answer", side_effect=capturing_generate):
        events = list(rag_service.stream_query(db=MagicMock(), query=QUESTION))

    types = [e["type"] for e in events]
    assert types[-1] == "done" and "token" in types and types.index("token") < types.index("done")
    plan = captured["plan"]
    assert plan is not None and plan.domain_archetype == "restriction_block"
    done = events[-1]
    assert done["validation_audit"]["answer_style"]["answer_type"] == plan.answer_type
    # The plan is never part of what the user reads.
    streamed = "".join(e["content"] for e in events if e["type"] == "token")
    assert "answer_type" not in streamed and "ANSWER FORMAT" not in done["answer"]
    # Repair (if any) reuses the same plan instead of re-routing.
    if calls["repair_kwargs"]:
        assert calls["repair_kwargs"]["answer_plan"] is plan


# 12. Existing behaviour stays available
def test_router_disabled_restores_legacy_archetype_prompt(monkeypatch):
    monkeypatch.setattr(settings, "ANSWER_STYLE_ROUTER_ENABLED", False)
    prompt = llm_service._build_answer_prompt("How to configure billable item classes in SPRO?", KB, "knowledge_base")
    assert "QUESTION ARCHETYPE:" in prompt and "STRUCTURE REQUIREMENT (Configuration)" in prompt
    assert "ANSWER FORMAT" not in prompt


def test_domain_structures_with_safety_content_are_kept():
    _, prompt = prompt_for("Reconcile billing discrepancies after the upgrade")
    assert "Stage-by-Stage Reconciliation Matrix" in prompt and "financial-side-effect safeguards" in prompt
    _, prompt = prompt_for(QUESTION)
    assert "STRUCTURE REQUIREMENT (Block or Restriction)" in prompt


def test_generic_archetypes_no_longer_force_rigid_sections():
    _, prompt = prompt_for("What is SAP Convergent Invoicing?")
    assert "STRUCTURE REQUIREMENT" not in prompt
    assert "Practical Example or Business Use Case" not in prompt


# 13. Repetition is discouraged
def test_prompt_forbids_repeated_facts_and_summaries():
    _, prompt = prompt_for("Explain the end-to-end billing flow from billable items to invoicing in detail")
    assert "State each fact once." in prompt
    assert "Do not add a summary or conclusion that repeats what was already said" in prompt
    assert 'no closing "Key points", "Summary" or "Overview" section' in prompt


def test_prompt_forbids_invented_citation_markers():
    _, prompt = prompt_for("How does billing group billable items?")
    assert "never ranges like [E1-E5] or labels like [All evidence]" in prompt


# 14. Explicit user formatting overrides routing defaults
def test_explicit_no_table_overrides_comparison_default():
    plan, prompt = prompt_for("Compare CI and FI-CA without a table")
    assert plan.answer_type == "comparison" and plan.needs_table is False
    assert "The user explicitly asked for no tables" in prompt


def test_explicit_brevity_overrides_detail():
    plan = route("Explain the end-to-end billing process briefly")
    assert plan.detail_level == "concise" and plan.max_sections == 0


def test_explicit_step_by_step_overrides_explanation():
    plan = route("Explain how billing works, step by step")
    assert plan.answer_type == "step_by_step" and plan.routed_by == "explicit"


def test_format_request_in_follow_up_is_read_from_the_raw_question():
    plan = route("How does billing group billable items?", raw="now put that in a table")
    assert plan.needs_table is True
