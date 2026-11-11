import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, List, Generator, Optional
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.services.company_info import COMPANY_ANSWER, is_company_question
from backend.app.services import languages
from backend.app.services.sap_classifier import NON_SAP_REFUSAL_RESPONSE, sap_classifier
from backend.app.services.retrieval_service import retrieval_service
from backend.app.services.reranking_service import reranking_service
from backend.app.services.grounding_service import grounding_service
from backend.app.services.web_search_service import web_search_service
from backend.app.services import token_meter
from backend.app.services.search import APPROVED_SAP_DOMAINS
from backend.app.services.evidence_selection_service import evidence_selection_service
from backend.app.services.answer_planner import answer_planner
from backend.app.services.answer_style_router import (
    answer_style_router,
    is_cross_component_handoff,
    is_solution_design_question,
)
from backend.app.services.followup_service import followup_service
from backend.app.services.llm_service import (
    GenerationResult,
    UNAVAILABLE_RESPONSE,
    llm_service,
    filter_generated_links,
)
from backend.app.services.query_rewriter import query_rewriter
from backend.app.services.restriction_scope_classifier import restriction_scope_classifier, RestrictionScope
from backend.app.services.sap_identifier_validator import sap_identifier_validator, content_terms
from backend.app.services.evidence_markers import (
    assign_evidence_ids,
    valid_ids,
    remove_invalid_markers,
    strip_markers,
)
from backend.app.services.answer_guard import apply_guard
from backend.app.services import confidence_scoring
from backend.app.services.conversation_memory_service import (
    RECENT_CONTEXT_LIMIT,
    conversation_memory_service,
    MemoryIntent,
)
from backend.app.services.query_router import query_router, QueryRoute, QueryRoutingDecision
from backend.app.services.intent_analyzer import intent_analyzer
from backend.app.services import document_generator
from backend.app.security.access import retrieval_groups
from backend.app.security.log_safety import privacy_fingerprint

logger = logging.getLogger(__name__)


def _needs_conversation_summary(memory_intent: str, chat_history: Optional[List[Dict[str, Any]]]) -> bool:
    if memory_intent in {
        MemoryIntent.CROSS_CONVERSATION_RECALL,
        MemoryIntent.HISTORY_RECALL,
        MemoryIntent.MIXED,
    }:
        return True
    if memory_intent in {MemoryIntent.FIRST_MESSAGE_LOOKUP, MemoryIntent.LAST_MESSAGE_LOOKUP}:
        return False
    return len(chat_history or []) >= RECENT_CONTEXT_LIMIT


# Scenario questions list their topics in one sentence: "a solution covering SOM, CC rating, ..., while
# handling billing discrepancies and production failures". Each topic is retrieved on its own.
_TOPIC_LIST_LEAD = re.compile(r"^.*?\b(?:covering|including|spanning|across|involving|comprising|such\s+as)\s+", re.I)
_TOPIC_SEPARATOR = re.compile(
    r",\s*(?:and\s+|or\s+)?|\s+while\s+(?:also\s+)?|\s+as\s+well\s+as\s+|\s+along\s+with\s+", re.I
)
_TOPIC_FILLER = re.compile(r"^(?:(?:and|or|also|while|handling|managing|addressing|supporting|with)\s+)+", re.I)
_OPERATIONS_ASKED = re.compile(
    r"\b(?:production|fail(?:s|ed|ing|ures?)?|errors?|issues?|incidents?|discrepanc(?:y|ies)|bottlenecks?"
    r"|troubleshoot\w*|recover\w*|reprocess\w*|monitor\w*|mismatch\w*)\b",
    re.I,
)
# Retrieval queries (not answer content) for the operational ground senior design answers must cover.
OPERATIONS_ASPECTS = [
    "SAP BRIM rating failures and rejected or duplicate usage events: detection and reprocessing",
    "failed billing and invoicing runs in Convergent Invoicing: error billable items, monitoring and restart",
    "reconciliation mismatches between usage, billable items, billing documents, invoicing and FI-CA postings",
    "FI-CA posting errors from invoicing documents and how they are corrected",
    "performance tuning of high-volume billing mass runs and parallel processing",
]
MAX_SCENARIO_ASPECTS = 11
PASSAGES_PER_ASPECT = 2
# Cross-component handoffs need a wider RRF window to retrieve process and receiving-ledger evidence.
CROSS_COMPONENT_RETRIEVAL_TOP_K = 50
CROSS_COMPONENT_RERANK_TOP_K = 8
# Web/repository search runs once per part; for scenario questions only the worst-covered parts are searched.
MAX_EXTERNAL_SEARCH_ASPECTS = 3

# Fallback chain after generation (docs/engineering-memory/RAG_FAILURE_DIAGNOSIS.md): an answer that fails the
# grounding gate is published with this notice instead of being discarded; when no LLM tier answers, approved
# SAP web results are the final fallback; only when that also fails does the user get the error below.
UNVERIFIED_ANSWER_NOTE = (
    "> **Note:** Parts of this answer could not be confirmed against the SAP documentation available to "
    "Clyptus.AI. Check release-specific details in the official SAP documentation before relying on them."
)
WEB_FALLBACK_INTRO = (
    "**The AI answer service is temporarily unavailable**, so this answer is assembled directly from approved "
    "SAP web sources and has not been verified by Clyptus.AI. Open the linked pages for full details."
)
ALL_METHODS_UNAVAILABLE_RESPONSE = (
    "All response-generation methods are temporarily unavailable: none of the AI models could answer and the "
    "SAP web search returned no usable results. Please try again in a few minutes."
)
WEB_FALLBACK_MAX_SOURCES = 5
UNVERIFIED_NOTE_MAX_SOURCES = 3
# Help-portal page chrome that search results carry along with the content.
WEB_NAVIGATION_TEXT = re.compile(
    r"\b(?:log\s*on|sign\s*in|explore\s+sap|learning\s+journeys|what'?s\s+new|cookie|privacy\s+statement|"
    r"terms\s+of\s+use|skip\s+to\s+content|table\s+of\s+contents)\b",
    re.I,
)


class RAGService:
    """
    Production-Grade Evidence-First SAP BRIM Knowledge Assistant Service.
    
    Orchestration Flow:
    1. Query Rewriting & Context Resolution: resolves references, retains technical IDs.
    2. SAP Domain Classifier: strict scope check, returns refusal for non-SAP queries.
    3. Internal Hybrid Retrieval: vector + lexical BM25 matching across the live indexed corpus.
    4. Multi-Signal Evidence Confidence Assessment:
       - If confidence < 0.70 (INTERNAL_EVIDENCE_THRESHOLD), automatically triggers Tavily.
       - Tavily searches approved SAP domains (help.sap.com, community.sap.com, blogs.sap.com).
    5. Unified Evidence Selection:
       - Compares internal-only, external-only, and combined evidence sets.
       - Selects strongest sufficient set with preserved provenance and authority tiers.
    6. Question-Aware Detailed Answer Generation:
       - Injects structure archetype guidelines (conceptual, configuration, troubleshooting, etc.).
    7. Claim-Level Grounding Verification (ANSWER_GROUNDING_THRESHOLD = 0.80):
       - Inspects material factual claims & audits SAP technical identifiers (T-codes, tables, SPRO).
       - Critical claim veto: fabricated identifiers fail the publication gate immediately.
    8. Controlled Answer Repair (MAX_GROUNDING_REPAIR_ATTEMPTS = 1):
       - If verification fails, re-prompts LLM to strip unverified technical instructions.
       - Re-verifies revised answer.
       - If still failing, returns a supported partial answer or explicit abstention.
    9. Recommended Follow-Up Questions (FOLLOWUP_QUESTION_COUNT = 3):
       - Generates exactly 3 relevant, clickable follow-up questions.
    10. Progressive Streaming & Safe Delivery:
       - Yields real progress stages.
       - Buffers candidate output until verification passes before delivering tokens.
    """

    def __init__(self):
        self.retrieval = retrieval_service
        self._comparator = None

    @staticmethod
    def _attachment_passages(query: str, attachments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Select query-relevant windows from uploads so long files are not silently truncated."""
        stop_words = {
            "the", "and", "for", "with", "from", "that", "this", "what", "which", "when",
            "where", "does", "have", "has", "about", "based", "file", "document", "image",
            "please", "could", "would", "explain", "summarize", "tell", "show", "using",
        }
        query_terms = {
            token.lower() for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}", query)
            if token.lower() not in stop_words
        }
        candidates = []
        per_file = []
        max_window = 2600

        for file_index, attachment in enumerate(attachments):
            name = str(attachment.get("name") or "Attached Document")[:500]
            text = str(attachment.get("text") or "")[:150000]
            if not text.strip():
                continue
            chunks = []
            offset = 0
            while offset < len(text):
                end = min(offset + max_window, len(text))
                if end < len(text):
                    boundary = text.rfind("\n", offset + max_window // 2, end)
                    if boundary < offset:
                        boundary = text.rfind(" ", offset + max_window // 2, end)
                    if boundary > offset:
                        end = boundary
                chunk = text[offset:end].strip()
                if chunk:
                    lowered = chunk.lower()
                    score = sum(1 for term in query_terms if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", lowered))
                    page_matches = re.findall(r"--- Page (\d+) ---", text[:offset])
                    page_number = int(page_matches[-1]) if page_matches else 1
                    entry = (score, file_index, len(chunks), name, chunk, len(text), page_number)
                    chunks.append(entry)
                    candidates.append(entry)
                offset = max(end, offset + 1)
            if chunks:
                per_file.append(max(chunks, key=lambda row: row[0]))

        # Keep one best passage from each selected file, then fill remaining context
        # slots by query overlap. Sorting back to source order preserves reading flow.
        selected = list(per_file[:8])
        selected_keys = {(row[1], row[2]) for row in selected}
        for entry in sorted(candidates, key=lambda row: row[0], reverse=True):
            key = (entry[1], entry[2])
            if key not in selected_keys:
                selected.append(entry)
                selected_keys.add(key)
            if len(selected) >= 8:
                break
        selected.sort(key=lambda row: (row[1], row[2]))

        passages = []
        remaining_chars = max(0, int(getattr(settings, "MAX_ATTACHMENT_PROMPT_CHARS", 24000)))
        for _score, file_index, chunk_index, name, chunk, full_length, page_number in selected:
            if remaining_chars <= 0:
                break
            chunk = chunk[:remaining_chars]
            remaining_chars -= len(chunk)
            page_count = attachments[file_index].get("page_count") or 1
            passages.append({
                "source_type": "attached_document",
                "source_id": f"attachment_{file_index + 1}_{chunk_index + 1}",
                "document": name,
                "document_name": name,
                "title": f"Attached File: {name}",
                "page": page_number,
                "page_number": page_number,
                "page_count": page_count,
                "section": f"Uploaded content segment {chunk_index + 1} ({full_length} characters total)",
                "score": 1.0,
                "final_score": 1.0,
                "rerank_score": 1.0,
                "chunk_text": chunk,
                "snippet": chunk[:350] + ("..." if len(chunk) > 350 else ""),
                "authority_tier": "tier_user_attachment",
            })
        return passages

    @staticmethod
    def split_question_aspects(query: str) -> List[str]:
        """Split only clear, independently answerable asks; keep ordinary prose intact."""
        pieces = re.split(
            r"\s*;\s*|\?\s*(?=\S)|\n(?=\s*(?:\d+[.)]|[-*])\s)|,\s*(?=(?:and\s+)?(?:calculate|compute|show|list|identify|compare|distinguish|describe|explain|provide|state)\b)",
            (query or "").strip(), flags=re.IGNORECASE,
        )
        aspects = [re.sub(r"^\s*(?:\d+[.)]|[-*])\s*", "", p).strip(" ?\n\t") for p in pieces]
        aspects = [p for p in aspects if len(p) >= 8]
        if len(aspects) > 1:
            return aspects
        return RAGService.scenario_aspects(query) or [(query or "").strip()]

    @staticmethod
    def is_incident_or_multipart_question(text: str) -> bool:
        t_low = text.lower()
        if "what is the difference" in t_low or len(text.split()) <= 15:
            return False
        has_incident_signals = any(s in t_low for s in [
            "production incident", "month-end", "month end", "production failure",
            "posting discrepancies", "duplicate financial", "duplicated financial",
            "missing billable items", "discrepancies independently"
        ])
        has_multi_issue = (
            ("missing" in t_low or "discrepanc" in t_low)
            and ("duplicate" in t_low or "fi-ca" in t_low or "charging" in t_low)
            and ("recover" in t_low or "reconcil" in t_low or "investigat" in t_low)
        )
        return has_incident_signals or has_multi_issue

    @staticmethod
    def scenario_aspects(query: str) -> List[str]:
        """
        Retrieval parts for a broad design/scenario or complex incident question: the whole question,
        its listed topics, production incident dimensions, and operations aspects. Empty for ordinary questions.
        """
        text = " ".join((query or "").split())
        if is_solution_design_question(text):
            topics = [_TOPIC_FILLER.sub("", t).strip(" ?.") for t in _TOPIC_SEPARATOR.split(_TOPIC_LIST_LEAD.sub("", text, count=1))]
            topics = [t for t in topics if len(t) >= 3]
            aspects = [text]
            if len(topics) >= 2:
                size = 1 if len(topics) <= 4 else 2
                aspects += [f"SAP BRIM {', '.join(topics[i:i + size])}" for i in range(0, len(topics), size)]
            if _OPERATIONS_ASKED.search(text):
                aspects += OPERATIONS_ASPECTS
            return aspects[:MAX_SCENARIO_ASPECTS] if len(aspects) > 1 else []

        if RAGService.is_incident_or_multipart_question(text):
            aspects = [text]
            t_low = text.lower()
            if "billable item" in t_low or "charging" in t_low or "invoicing" in t_low or "100,000" in t_low:
                aspects.append("SAP Convergent Charging to Convergent Invoicing missing billable items RFC loading error")
            if "fi-ca" in t_low or "posting" in t_low:
                aspects.append("Convergent Invoicing to FI-CA posting discrepancies reconciliation key clearing")
            if "duplicate" in t_low or "financial transaction" in t_low:
                aspects.append("FI-CA duplicate financial transactions prevention and reversal checks")
            if "recover" in t_low or "reprocess" in t_low or "safely" in t_low:
                aspects.append("SAP BRIM safe recovery and reprocessing of missing billable items without duplication")
            if "reconcil" in t_low or "validat" in t_low:
                aspects.append("SAP BRIM end-to-end reconciliation matrix record counts and balance validation")
            return aspects[:MAX_SCENARIO_ASPECTS]

        return []

    def _retrieve_aspect_evidence(self, db: Session, query: str, top_k: int, *, hybrid: bool = False, allowed_groups=None):
        aspects = self.split_question_aspects(query)
        if len(aspects) == 1:
            retrieve = self.retrieval.hybrid_search if hybrid else self.retrieval.retrieve
            cross_component = is_cross_component_handoff(query)
            retrieval_top_k = max(top_k, CROSS_COMPONENT_RETRIEVAL_TOP_K) if cross_component else top_k
            rerank_top_k = max(settings.RERANK_TOP_K, CROSS_COMPONENT_RERANK_TOP_K) if cross_component else settings.RERANK_TOP_K
            candidates = retrieve(db, query, top_k=retrieval_top_k, allowed_groups=allowed_groups)
            ranked, score, _ = reranking_service.rerank_kb(query, candidates, top_n=rerank_top_k)
            for item in ranked:
                item["question_aspect"] = query
            return ranked, score, [{"aspect": query, "score": round(float(score), 4), "evidence_count": len(ranked)}]

        merged: Dict[str, Dict[str, Any]] = {}
        aspect_results = []
        for aspect in aspects:
            retrieve = self.retrieval.hybrid_search if hybrid else self.retrieval.retrieve
            candidates = retrieve(db, aspect, top_k=top_k, allowed_groups=allowed_groups)
            ranked, score, _ = reranking_service.rerank_kb(aspect, candidates, top_n=settings.RERANK_TOP_K)
            aspect_results.append({"aspect": aspect, "score": round(float(score), 4), "evidence_count": len(ranked)})
            for item in ranked:
                item["question_aspect"] = aspect
                key = str(item.get("chunk_id") or item.get("id") or item.get("chunk_text", "")[:160])
                prior = merged.get(key)
                if prior is None or float(item.get("rerank_score", item.get("score", 0))) > float(prior.get("rerank_score", prior.get("score", 0))):
                    merged[key] = item
        combined = sorted(merged.values(), key=lambda item: float(item.get("rerank_score", item.get("score", 0))), reverse=True)
        # Use the weakest aspect match to trigger fallback rather than hiding an evidence gap in an average.
        weakest_score = min((row["score"] for row in aspect_results), default=0.0)
        kept = combined[:max(settings.RERANK_TOP_K, settings.TOP_K)]
        kept_ids = {id(item) for item in kept}
        # Every part keeps its own best passages; a global cut would drop the parts that score lower.
        for aspect in aspects:
            for item in [c for c in combined if c.get("question_aspect") == aspect][:PASSAGES_PER_ASPECT]:
                if id(item) not in kept_ids:
                    kept.append(item)
                    kept_ids.add(id(item))
        return kept, weakest_score, aspect_results

    @staticmethod
    def _external_search_aspects(query: str, aspect_results: List[Dict[str, Any]]) -> List[str]:
        """Parts to search outside the knowledge base: all of them, or the worst-covered few for a scenario question."""
        aspects = RAGService.split_question_aspects(query)
        if len(aspects) <= MAX_EXTERNAL_SEARCH_ASPECTS or not is_solution_design_question(query):
            return aspects
        weakest = sorted(aspect_results, key=lambda row: (row["score"], row["evidence_count"]))
        return [row["aspect"] for row in weakest[:MAX_EXTERNAL_SEARCH_ASPECTS]]

    @staticmethod
    def _ensure_aspect_coverage(
        selected: List[Dict[str, Any]],
        internal: List[Dict[str, Any]],
        aspects: List[Dict[str, Any]],
        interleave: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Keep a passage or an explicit non-citable retrieval-gap marker for each ask.
        interleave: put one passage per ask first, so a context limit trims extra passages, not whole asks.
        """
        output = list(selected or [])
        if interleave:
            lead, seen = [], set()
            for row in aspects:
                first = next((p for p in output + list(internal or []) if p.get("question_aspect") == row["aspect"]), None)
                if first is not None and id(first) not in seen:
                    lead.append(first)
                    seen.add(id(first))
            output = lead + [p for p in output if id(p) not in seen]
        for row in aspects:
            aspect = row["aspect"]
            if any(p.get("question_aspect") == aspect for p in output):
                continue
            candidate = next((p for p in internal if p.get("question_aspect") == aspect), None)
            if candidate:
                output.append(candidate)
            else:
                output.append({
                    "source_type": "retrieval_gap",
                    "question_aspect": aspect,
                    "retrieval_gap": True,
                    "chunk_text": "No relevant internal evidence was retrieved for this question part. This is not evidence that the SAP functionality is unsupported.",
                })
        return output

    # ------------------------------------------------------------------
    # Restriction/block scope handling
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_scope(query: str) -> Optional[RestrictionScope]:
        if not getattr(settings, "RESTRICTION_SCOPE_ENABLED", True):
            return None
        scope = restriction_scope_classifier.classify(query)
        return scope if scope.is_restriction_query else None

    def _retrieve_scope_evidence(self, db: Session, scope: Optional[RestrictionScope], *, hybrid: bool = False,
                                 allowed_groups=None) -> List[Dict[str, Any]]:
        """Retrieve evidence per candidate block type with the existing hybrid retrieval + reranker."""
        if not scope:
            return []
        retrieve = self.retrieval.hybrid_search if hybrid else self.retrieval.retrieve
        per_category = max(1, int(getattr(settings, "RESTRICTION_SCOPE_PASSAGES_PER_CATEGORY", 1)))
        passages: List[Dict[str, Any]] = []
        for item in scope.retrieval_queries(limit=int(getattr(settings, "RESTRICTION_SCOPE_MAX_EXPANSIONS", 5))):
            try:
                candidates = retrieve(db, item["query"], top_k=10, allowed_groups=allowed_groups)
                ranked, _, _ = reranking_service.rerank_kb(item["query"], candidates, top_n=per_category)
            except Exception as exc:
                logger.warning("Scope retrieval for %s failed: %s", item["category"], exc)
                continue
            for row in ranked or []:
                tagged = dict(row)
                tagged["restriction_scope"] = item["category"]
                passages.append(tagged)
        return passages

    @staticmethod
    def _ensure_scope_coverage(selected: List[Dict[str, Any]], scope_passages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Add one passage per candidate block type that is not already in the selected evidence."""
        def key(p: Dict[str, Any]) -> str:
            return str(p.get("chunk_id") or p.get("candidate_id") or p.get("source_id") or (p.get("chunk_text") or "")[:160])

        output = list(selected or [])
        by_key = {key(p): p for p in output}
        for passage in scope_passages:
            existing = by_key.get(key(passage))
            if existing is not None:
                existing.setdefault("restriction_scope", passage.get("restriction_scope"))
                continue
            output.append(passage)
            by_key[key(passage)] = passage
        return output

    @staticmethod
    def _prepare_evidence(passages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Copy passages (retrieval caches share dicts) and number them E1..En for inline citation."""
        prepared = [dict(p) for p in (passages or [])]
        return assign_evidence_ids(prepared)

    @staticmethod
    def _generation_guidance(scope: Optional[RestrictionScope], passages: List[Dict[str, Any]]) -> Optional[str]:
        parts: List[str] = []
        if scope:
            parts.append(scope.prompt_guidance())
        if getattr(settings, "TECHNICAL_CLAIM_VALIDATION_ENABLED", True):
            try:
                conflicts = sap_identifier_validator.detect_evidence_conflicts(passages)
            except Exception as exc:
                logger.warning("Evidence conflict detection failed: %s", exc)
                conflicts = []
            if conflicts:
                lines = ["EVIDENCE CONFLICTS (state them; scope each claim to its source/release):"]
                for c in conflicts[:5]:
                    lines.append(
                        f"- {c['identifier']}: documented for use in {', '.join(c['documented_use_in'])}, "
                        f"but described as not available/obsolete in {', '.join(c['unavailable_in'])}."
                    )
                parts.append("\n".join(lines))
        return "\n\n".join(p for p in parts if p) or None

    @staticmethod
    def _verified_token_events(text: str, words_per_event: int = 8) -> Generator[Dict[str, Any], None, None]:
        """Emit already-verified answer text as token events, a few words at a time."""
        pieces = re.findall(r"\S+\s*|\s+", text or "")
        for i in range(0, len(pieces), words_per_event):
            yield {"type": "token", "content": "".join(pieces[i:i + words_per_event])}

    @staticmethod
    def _verification_rank(verification: Dict[str, Any]):
        critical = len(verification.get("critical_failures") or []) + len(verification.get("contradicted_claims") or [])
        return (critical, -float(verification.get("grounding_score") or 0.0))

    def _finalize_answer(
        self,
        query: str,
        answer: str,
        passages: List[Dict[str, Any]],
        source_type: str,
        scope: Optional[RestrictionScope],
        guidance: Optional[str],
        verification: Optional[Dict[str, Any]] = None,
        answer_plan=None,
    ):
        """Verify -> repair once -> deterministic guard -> re-verify before publication.
        In reference mode the passages are only reference notes, so the verification stays a diagnostic
        (admin-only score and status) and the answer is neither repaired nor stripped."""
        if verification is None:
            verification = grounding_service.verify_answer(query, answer, passages, source_type=source_type)
        if settings.REFERENCE_MODE_ENABLED:
            final_answer = remove_invalid_markers(answer, valid_ids(passages))
            if not getattr(settings, "INLINE_EVIDENCE_MARKERS_ENABLED", True):
                final_answer = strip_markers(final_answer)
            repair = {"attempted": False, "adopted": False, "guard_applied": False, "removed_claims": 0}
            return filter_generated_links(final_answer), verification, repair, verification.get("verification_status", "partial")
        repair = {"attempted": False, "adopted": False, "guard_applied": False, "removed_claims": 0}
        final_answer = answer

        if not verification.get("passes_grounding_gate") and settings.MAX_GROUNDING_REPAIR_ATTEMPTS > 0:
            repair["attempted"] = True
            repaired = llm_service.repair_answer(
                query=query,
                candidate_answer=answer,
                unsupported_claims=verification.get("unsupported_claims", []),
                critical_unsupported_ids=verification.get("critical_unsupported_claims", []),
                evidence=passages,
                source_type=source_type,
                critical_failures=verification.get("critical_failures", []),
                guidance=guidance,
                answer_plan=answer_plan,
            )
            if repaired and len(repaired.strip()) > 30 and repaired.strip() != (answer or "").strip():
                repaired_verification = grounding_service.verify_answer(query, repaired, passages, source_type=source_type)
                # Never publish a repair that is worse than the candidate it replaces.
                if self._verification_rank(repaired_verification) <= self._verification_rank(verification):
                    final_answer, verification = repaired, repaired_verification
                    repair["adopted"] = True

        if verification.get("critical_failures") or verification.get("contradicted_claims"):
            guarded = apply_guard(
                final_answer,
                verification,
                allowed_evidence_ids=valid_ids(passages),
                clarification_question=scope.clarification_question if scope else None,
                evidence_needed=scope.evidence_needed if scope and scope.ambiguous else None,
            )
            if guarded["applied"]:
                final_answer = guarded["answer"]
                # Revalidate the degraded answer before it is published.
                verification = grounding_service.verify_answer(query, final_answer, passages, source_type=source_type)
                repair["guard_applied"] = True
                repair["removed_claims"] = len(guarded["removed_claims"])

        final_answer = remove_invalid_markers(final_answer, valid_ids(passages))
        if not getattr(settings, "INLINE_EVIDENCE_MARKERS_ENABLED", True):
            final_answer = strip_markers(final_answer)

        status = verification.get("verification_status", "partial")
        if repair["guard_applied"] and status == "verified":
            # The original procedure was not verified; only a reduced answer is.
            status = "partial"
        final_answer = filter_generated_links(final_answer)
        return final_answer, verification, repair, status

    @staticmethod
    def _attach_answer_style(validation_audit, answer_plan) -> None:
        """Record the layout decision in the admin-only audit (never sent to regular users)."""
        if isinstance(validation_audit, dict) and answer_plan is not None:
            validation_audit["answer_style"] = answer_plan.model_dump()

    def _validation_audit(self, query, document_match_score, passages, verification, aspects, scope, repair) -> Dict[str, Any]:
        try:
            report = confidence_scoring.build_report(
                retrieval_relevance=document_match_score,
                passages=passages,
                verification=verification,
                aspects=aspects,
                scope=scope,
                repair=repair,
            )
            confidence_scoring.log_report(report, query)
            return report
        except Exception as exc:
            logger.warning("Validation audit could not be built: %s", exc)
            return {"decision": "unknown", "error": str(exc)}

    @staticmethod
    def _should_auto_compare(document_match_score: float, confidence_data: Dict[str, Any]) -> bool:
        """Weak document evidence may trigger the GitHub/web comparator, but only when an operator has
        explicitly enabled an extra source (AUDIT_REPORT F-49); the default flags never do."""
        threshold = float(getattr(settings, "DOCUMENT_MATCH_THRESHOLD", 0.80))
        sources_enabled = bool(
            getattr(settings, "REPO_SOURCE_ENABLED", False) or getattr(settings, "MULTI_SOURCE_COMPARE_ENABLED", False)
        )
        return bool(
            getattr(settings, "AUTO_MULTI_SOURCE_FALLBACK", True)
            and sources_enabled
            and (
                float(document_match_score) < threshold
                or confidence_data.get("should_trigger_tavily", False)
            )
        )

    @staticmethod
    def _should_trigger_web_fallback(document_match_score: float, confidence_data: Dict[str, Any]) -> bool:
        """Search the web when retrieval is weak, independently of repository comparison settings."""
        return bool(
            getattr(settings, "AUTO_MULTI_SOURCE_FALLBACK", True)
            and getattr(settings, "WEB_FALLBACK_ENABLED", True)
            and (
                float(document_match_score) < float(getattr(settings, "DOCUMENT_MATCH_THRESHOLD", 0.80))
                or confidence_data.get("should_trigger_tavily", False)
            )
        )

    @property
    def comparator(self):
        if self._comparator is None:
            from backend.app.retrieval.multi_source_compare import multi_source_comparator
            self._comparator = multi_source_comparator
        return self._comparator

    @comparator.setter
    def comparator(self, value):
        self._comparator = value

    def _format_repo_citation(self, raw_chunk: Dict[str, Any]) -> Dict[str, Any]:
        """Formats a GitHub repository chunk into backward-compatible citation schema."""
        repo_url = raw_chunk.get("repo_url", "")
        file_path = raw_chunk.get("file_path", "")
        repo_owner_name = repo_url.replace("https://github.com/", "").strip("/")
        source_label = f"GitHub: {repo_owner_name} ({file_path})" if file_path else f"GitHub: {repo_owner_name}"
        
        return {
            "source_type": "github_repo",
            "source": source_label,
            "repo_url": repo_url,
            "file_path": file_path,
            "commit_hash": raw_chunk.get("commit_hash", ""),
            "license": raw_chunk.get("license", "unknown"),
            "score": raw_chunk.get("score", 0.0),
            "chunk_id": raw_chunk.get("candidate_id") or raw_chunk.get("chunk_id", ""),
            "title": f"{repo_owner_name} - {file_path}"
        }

    @staticmethod
    def _format_kb_citation(raw_chunk: Dict[str, Any]) -> Dict[str, Any]:
        """Create a safe, stable citation for a retrieved knowledge-base chunk."""
        raw_document = str(raw_chunk.get("document_name") or raw_chunk.get("title") or "SAP BRIM Documentation")
        # Retrieval records must never turn a local source path into an API response.
        document = re.split(r"[\\\\/]", raw_document)[-1]
        page = raw_chunk.get("page_number", 1) or 1
        snippet = (raw_chunk.get("snippet") or raw_chunk.get("chunk_text") or "").replace("\ufffd", "-").strip()
        return {
            "source_type": "knowledge_base",
            # Normalized evidence keeps the chunk ID as source_id; without it, distinct chunks
            # from the same page would be merged into one citation by _dedupe_citations.
            "chunk_id": str(raw_chunk.get("chunk_id") or raw_chunk.get("id") or raw_chunk.get("source_id") or ""),
            "document": document,
            "page": page,
            "section": raw_chunk.get("section", "") or f"Page {page}",
            "score": round(float(raw_chunk.get("relevance_score", raw_chunk.get("rerank_score", raw_chunk.get("score", 0.0)))), 4),
            "snippet": snippet,
        }

    @staticmethod
    def _dedupe_citations(citations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Keep one citation per stable chunk (or document/page fallback)."""
        seen = set()
        unique = []
        for citation in citations:
            key = citation.get("chunk_id") or (
                citation.get("source_type", "knowledge_base"),
                citation.get("document", citation.get("source", "")),
                citation.get("page", 1),
                citation.get("section", ""),
            )
            if key not in seen:
                seen.add(key)
                unique.append(citation)
        return unique

    @staticmethod
    def _comparison_summary(comp_result: Dict[str, Any]) -> Dict[str, Any]:
        """Return compact per-source scores and winner details for the API/UI."""
        source_scores: Dict[str, float] = {}
        for candidate in comp_result.get("all_candidates", []):
            if candidate.get("is_eligible"):
                kind = candidate.get("source_type", "unknown")
                source_scores[kind] = max(
                    source_scores.get(kind, 0.0), float(candidate.get("final_score", 0.0))
                )
        comparison = comp_result.get("comparison", {})
        return {
            "source_scores": source_scores,
            "comparison_audit": {
                "sources_compared": comparison.get("sources_compared", []),
                "threshold": comparison.get("threshold", 0.0),
                "winning_source_type": comparison.get("winning_source_type", "insufficient"),
                "winning_score": comparison.get("final_score", 0.0),
                "selection_reason": comparison.get("selection_reason", ""),
                "scores_by_source": source_scores,
            },
        }

    def _generate_grounded_answer(
        self,
        query: str,
        passages: List[Dict[str, Any]],
        source_type: str = "knowledge_base",
        guidance: Optional[str] = None,
        answer_plan=None,
        chat_history: Optional[List[Dict[str, Any]]] = None,
        historical_memory: Optional[List[Dict[str, Any]]] = None,
        summary: Optional[str] = None,
        regeneration_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Generates candidate answer and runs claim grounding verification."""
        generation = GenerationResult()
        candidate_answer = llm_service.generate_answer(
            query, passages, source_type=source_type, guidance=guidance, answer_plan=answer_plan,
            chat_history=chat_history, historical_memory=historical_memory, summary=summary,
            result=generation,
            regeneration_index=regeneration_index,
        )
        if generation.available is False:
            return {
                "answer": UNAVAILABLE_RESPONSE,
                "grounded_score": None,
                "verification_status": "unavailable",
                "passes_grounding_gate": False,
                "unsupported_claims": [],
                "critical_unsupported_claims": [],
                "citations": [],
                "insufficient_evidence": not bool(passages),
                "verification": {
                    "grounding_score": None,
                    "verification_status": "unavailable",
                    "passes_grounding_gate": False,
                    "unsupported_claims": [],
                    "critical_unsupported_claims": [],
                },
                "generation": generation,
            }
        verification = grounding_service.verify_answer(
            query, candidate_answer, passages, source_type=source_type
        )
        return {
            "answer": candidate_answer,
            "grounded_score": verification.get("grounding_score", 0.85),
            "verification_status": verification.get("verification_status", "verified"),
            "passes_grounding_gate": verification.get("passes_grounding_gate", False),
            "unsupported_claims": verification.get("unsupported_claims", []),
            "critical_unsupported_claims": verification.get("critical_unsupported_claims", []),
            "citations": [],
            "insufficient_evidence": not bool(passages),
            "verification": verification,
            "generation": generation,
        }

    @staticmethod
    def _generation_diagnostics(
        generation: GenerationResult,
        retrieved: List[Dict[str, Any]],
        passages: List[Dict[str, Any]],
        final_answer: str,
        candidate_answer: str,
        repair_info: Dict[str, Any],
        answer_plan,
        elapsed_ms: float,
    ) -> Dict[str, Any]:
        """Admin-only diagnostics for one answer, from this request's own GenerationResult."""
        return {
            "correlation_id": str(uuid.uuid4()),
            "model_used": generation.model,
            "provider_used": generation.provider,
            "query_complexity": answer_plan.model_dump().get("archetype", "unknown") if answer_plan else "unknown",
            "retrieved_chunks_count": len(retrieved),
            "final_context_chunks_count": len(passages),
            "estimated_input_tokens": generation.estimated_input_tokens,
            "estimated_output_tokens": len(final_answer.split()) * 4 // 3,
            "configured_output_token_limit": generation.max_output_tokens,
            "finish_reason": generation.finish_reason,
            "is_truncated": generation.is_truncated,
            "was_continued": generation.was_continued,
            "failed_over": generation.failed_over,
            "generation_available": generation.available,
            "failure_category": generation.failure_category,
            "providers_considered": list(generation.providers_considered),
            "providers_attempted": list(generation.providers_attempted),
            "post_processing_modified": bool(repair_info.get("guard_applied", False) or final_answer != candidate_answer),
            "elapsed_time_ms": elapsed_ms,
        }

    def _unavailable_result(
        self,
        *,
        route: str,
        generation: GenerationResult,
        retrieved: List[Dict[str, Any]],
        passages: List[Dict[str, Any]],
        answer_plan,
        elapsed_ms: float,
        kb_score: float = 0.0,
        answer: str = UNAVAILABLE_RESPONSE,
    ) -> Dict[str, Any]:
        """Stable API-compatible response when generation or grounding cannot produce an answer."""
        diagnostics = self._generation_diagnostics(
            generation, retrieved, passages, answer, "", {"attempted": False}, answer_plan, elapsed_ms
        )
        diagnostics["failure_category"] = generation.failure_category or "verification_rejected"
        return {
            "answer": answer,
            "route": route,
            "source_type": "unavailable",
            "grounding_score": None,
            "grounded_score": None,
            "is_in_rag_pipeline": False,
            "kb_score": round(kb_score, 4),
            "web_score": 0.0,
            "winning_score": 0.0,
            "source_scores": {},
            "selected_source": "unavailable",
            "verification_status": "unavailable",
            "internal_evidence_confidence": None,
            "selected_evidence_quality": 0.0,
            "external_search_used": False,
            "citations": [],
            "web_sources": [],
            "images": [],
            "follow_up_questions": [],
            "selection_summary": "A verified response could not be generated.",
            "model_used": generation.model,
            "provider_used": generation.provider,
            "failed_over": generation.failed_over,
            "repair_count": 0,
            "shadow_audit": None,
            "comparison_audit": None,
            "answer_decision": "unavailable",
            "validation_audit": {"decision": "unavailable"},
            "diagnostics": diagnostics,
        }

    @staticmethod
    def _web_passages(*groups: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        """Web evidence with a URL from the given passage lists, first occurrence of each URL."""
        seen, passages = set(), []
        for group in groups:
            for passage in group or []:
                url = passage.get("url")
                if passage.get("source_type") == "web" and url and url not in seen:
                    seen.add(url)
                    passages.append(passage)
        return passages

    def _final_web_search(self, query: str, *known: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        """Approved SAP web results for the last fallback: those this request already fetched, else a new search."""
        passages = self._web_passages(*known)
        if passages or not settings.WEB_FALLBACK_ENABLED:
            return passages
        try:
            return self._web_passages(web_search_service.search_sap_authoritative(query))
        except Exception as exc:
            logger.warning("Final web-search fallback failed (%s)", type(exc).__name__)
            return []

    @staticmethod
    def _source_link(passage: Dict[str, Any]) -> str:
        """Markdown link to a web source; the title cannot break the link syntax."""
        title = re.sub(r"[\[\]()*_`#]", "", str(passage.get("title") or passage.get("domain") or "SAP source")).strip()
        url = str(passage.get("url") or "").replace(" ", "%20").replace("(", "%28").replace(")", "%29")
        return f"[{title or 'SAP source'}]({url})"

    @staticmethod
    def _filter_source_links(text: str) -> str:
        """Web results are restricted to approved SAP domains, so their links may be shown."""
        hosts = ",".join([settings.ALLOWED_LINK_HOSTS, *APPROVED_SAP_DOMAINS])
        return filter_generated_links(text, allowed_hosts=hosts)

    @staticmethod
    def _readable_sentences(passage: Dict[str, Any]) -> List[str]:
        """Prose sentences of a web result: the search snippet first, then the page passage, without page
        navigation, link lists, table-of-contents leaders or questions."""
        title = str(passage.get("title") or "")
        body = str(passage.get("chunk_text") or "")
        if title and body.startswith(title):
            body = body[len(title):]
        text = f"{passage.get('snippet') or ''}\n{body}".replace("�", "'")
        text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"\.{4,}|…{2,}|[#>|*`_]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        sentences, seen = [], set()
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            sentence = sentence.strip(" :;-")
            words = sentence.split()
            key = sentence.lower()
            if not 6 <= len(words) <= 60 or key in seen or sentence.endswith("?"):
                continue
            if sum(w.isalpha() for w in words) < 0.6 * len(words) or WEB_NAVIGATION_TEXT.search(sentence):
                continue
            seen.add(key)
            sentences.append(sentence)
        return sentences

    def _relevant_excerpt(self, query: str, passage: Dict[str, Any], max_chars: int = 420):
        """(relevance, excerpt): the two sentences sharing the most terms with the question, in page order."""
        terms = set(content_terms(query))
        scored = [(len(terms & set(content_terms(s))), i, s) for i, s in enumerate(self._readable_sentences(passage))]
        best = sorted((row for row in scored if row[0] > 0), key=lambda row: (-row[0], row[1]))[:2]
        excerpt = " ".join(s for _, _, s in sorted(best, key=lambda row: row[1]))
        if len(excerpt) > max_chars:
            excerpt = excerpt[:max_chars].rsplit(" ", 1)[0] + "..."
        return sum(score for score, _, _ in best), excerpt

    def _web_fallback_answer(self, query: str, passages: List[Dict[str, Any]]) -> str:
        """Structured answer from web results when no LLM tier is available to synthesize one: the pages
        that address the question, most relevant first, each with the sentences that do."""
        ranked = sorted(
            ((*self._relevant_excerpt(query, p), i, p) for i, p in enumerate(passages)),
            key=lambda row: (-row[0], row[2]),
        )
        # One shared generic word ("functions") does not make a page relevant to a longer question.
        min_score = 2 if len(set(content_terms(query))) >= 3 else 1
        relevant = [row for row in ranked if row[0] >= min_score] or ranked
        items = []
        for number, (_, excerpt, _, passage) in enumerate(relevant[:WEB_FALLBACK_MAX_SOURCES], start=1):
            domain = passage.get("domain") or ""
            line = f"{number}. **{self._source_link(passage)}**" + (f" ({domain})" if domain else "")
            items.append(f"{line}\n   {excerpt}" if excerpt else line)
        body = f"{WEB_FALLBACK_INTRO}\n\n### What SAP sources say\n\n" + "\n".join(items)
        return self._filter_source_links(body)

    def _web_fallback_result(
        self, *, query: str, route: str, generation: GenerationResult, retrieved: List[Dict[str, Any]],
        passages: List[Dict[str, Any]], answer_plan, elapsed_ms: float, kb_score: float,
    ) -> Dict[str, Any]:
        """Final fallback after every LLM tier failed: approved SAP web sources, never reported as verified."""
        answer = self._web_fallback_answer(query, passages)
        result = self._unavailable_result(
            route=route, generation=generation, retrieved=retrieved, passages=passages,
            answer_plan=answer_plan, elapsed_ms=elapsed_ms, kb_score=kb_score, answer=answer,
        )
        _, web_sources = self._build_citations(passages)
        result["diagnostics"]["fallback"] = "web_search"
        result.update({
            "source_type": "web",
            "selected_source": "web",
            "verification_status": "unverified",
            "external_search_used": True,
            "web_sources": web_sources,
            "follow_up_questions": followup_service.generate_starter_followups(),
            "selection_summary": "No LLM tier could generate an answer; answered from approved SAP web sources.",
            "answer_decision": "web_search_fallback",
            "validation_audit": {"decision": "web_search_fallback"},
        })
        return result

    def _publish_unverified(
        self,
        query: str,
        answer: str,
        verification: Dict[str, Any],
        passages: List[Dict[str, Any]],
        source_type: str,
        scope: Optional[RestrictionScope],
    ):
        """
        An answer that still fails the grounding gate after repair (and the web retry) is published rather
        than replaced by an error, but never as verified: claims with contradicted, unsafe or unconfirmed
        technical identifiers are removed first, and a notice (plus any web sources) is appended.
        """
        unconfirmed = set(verification.get("critical_unsupported_claims") or [])
        failures = list(verification.get("critical_failures") or [])
        for claim in verification.get("claims") or []:
            identifiers = sorted(unconfirmed.intersection(claim.get("unsupported_technical_ids") or []))
            if identifiers and claim.get("claim"):
                failures.append({"claim": claim["claim"], "identifier": identifiers[0], "status": "unconfirmed_identifier"})
        if failures or verification.get("contradicted_claims"):
            guarded = apply_guard(
                answer,
                {**verification, "critical_failures": failures},
                allowed_evidence_ids=valid_ids(passages),
                clarification_question=scope.clarification_question if scope else None,
            )
            if guarded["applied"]:
                answer = guarded["answer"]
                verification = grounding_service.verify_answer(query, answer, passages, source_type=source_type)
                if not getattr(settings, "INLINE_EVIDENCE_MARKERS_ENABLED", True):
                    answer = strip_markers(answer)

        status = verification.get("verification_status")
        if status not in ("partial", "unverified"):
            status = "partial" if status == "verified" else "unverified"
        note = UNVERIFIED_ANSWER_NOTE
        web_sources = self._web_passages(passages)[:UNVERIFIED_NOTE_MAX_SOURCES]
        if web_sources:
            note += "\n>\n> Related SAP sources:\n" + "\n".join(f"> - {self._source_link(p)}" for p in web_sources)
        answer = f"{filter_generated_links(answer).rstrip()}\n\n{self._filter_source_links(note)}"
        return answer, verification, status

    def query(self, query: str, db: Optional[Session] = None) -> Dict[str, Any]:
        """Synchronous query convenience entrypoint."""
        if db is None:
            from backend.app.database import get_db
            try:
                db_gen = get_db()
                db = next(db_gen)
            except Exception:
                from unittest.mock import MagicMock
                db = MagicMock()
        return self.process_query(db=db, query=query)

    def process_query(
        self,
        db: Session,
        query: str,
        chat_history: Optional[List[Dict[str, Any]]] = None,
        user: Optional[Any] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        chat_id: Optional[str] = None,
        regeneration_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Synchronous execution of the verified RAG pipeline with enterprise security enforcement."""
        start_time = time.perf_counter()
        # 0. Resolve user identity and role
        user_id = getattr(user, "id", None) if user else None
        # Document-level access control: only documents sharing the caller's groups are retrieved (F-01).
        access_groups = retrieval_groups(user)
        has_attachments = bool(attachments)

        # Strict 20-message conversation context window
        if chat_id and chat_history is None:
            chat_history = conversation_memory_service.get_recent_messages(db, chat_id, limit=20)
        elif chat_history:
            chat_history = chat_history[-20:]

        historical_memory: List[Dict[str, Any]] = []

        # Check for conversation memory intent (exact first question, history recall, cross-conversation)
        memory_intent = conversation_memory_service.classify_intent(query, chat_history=chat_history)
        summary = (
            conversation_memory_service.get_chat_summary(db, chat_id)
            if chat_id and _needs_conversation_summary(memory_intent, chat_history) else None
        )

        if memory_intent == MemoryIntent.FIRST_MESSAGE_LOOKUP:
            first_msg = conversation_memory_service.get_first_user_message(db, chat_id) if chat_id else None
            if first_msg:
                first_clean = re.sub(r"^\[Attached:.*?\]\n\n", "", first_msg["content"].strip(), flags=re.DOTALL)
                ts_str = first_msg["created_at"].strftime("%Y-%m-%d %H:%M:%S UTC") if isinstance(first_msg["created_at"], datetime) else str(first_msg["created_at"])
                ans_text = (
                    f"Your first question in this chat was:\n\n"
                    f"> \"{first_clean}\"\n\n"
                    f"*(Sent: {ts_str}, Message ID: `{first_msg['id']}`)*"
                )
            else:
                ans_text = "There are no previous questions recorded in this conversation."

            follow_ups = followup_service.generate_starter_followups()
            return {
                "answer": ans_text,
                "source_type": "conversation_memory",
                "grounding_score": None,
                "is_in_rag_pipeline": False,
                "kb_score": 0.0,
                "web_score": 0.0,
                "winning_score": 1.0,
                "selected_source": "conversation_memory",
                "verification_status": "unverified",
                "internal_evidence_confidence": 1.0,
                "selected_evidence_quality": 1.0,
                "external_search_used": False,
                "citations": [],
                "web_sources": [],
                "follow_up_questions": follow_ups,
                "selection_summary": "Retrieved first user question from conversation history."
            }

        if memory_intent == MemoryIntent.LAST_MESSAGE_LOOKUP:
            last_msg = conversation_memory_service.get_last_user_message(db, chat_id) if chat_id else None
            if last_msg:
                last_clean = re.sub(r"^\[Attached:.*?\]\n\n", "", last_msg["content"].strip(), flags=re.DOTALL)
                ts_str = last_msg["created_at"].strftime("%Y-%m-%d %H:%M:%S UTC") if isinstance(last_msg["created_at"], datetime) else str(last_msg["created_at"])
                ans_text = (
                    f"Your last question prior to this was:\n\n"
                    f"> \"{last_clean}\"\n\n"
                    f"*(Sent: {ts_str}, Message ID: `{last_msg['id']}`)*"
                )
            else:
                ans_text = "There are no prior messages found in this conversation."

            follow_ups = followup_service.generate_starter_followups()
            return {
                "answer": ans_text,
                "source_type": "conversation_memory",
                "grounding_score": None,
                "is_in_rag_pipeline": False,
                "kb_score": 0.0,
                "web_score": 0.0,
                "winning_score": 1.0,
                "selected_source": "conversation_memory",
                "verification_status": "unverified",
                "internal_evidence_confidence": 1.0,
                "selected_evidence_quality": 1.0,
                "external_search_used": False,
                "citations": [],
                "web_sources": [],
                "follow_up_questions": follow_ups,
                "selection_summary": "Retrieved last message directly from conversation history."
            }

        if memory_intent == MemoryIntent.CROSS_CONVERSATION_RECALL:
            exclude_ids = [m.get("id") for m in (chat_history or [])]
            hist_msgs = conversation_memory_service.search_history(
                db, query, user_id=user_id, current_chat_id=chat_id,
                exclude_message_ids=exclude_ids, cross_conversation=True, top_k=5
            )
            if not hist_msgs:
                ans_text = "I searched your other conversations, but could not find any earlier discussion matching your query."
                return {
                    "answer": ans_text,
                    "source_type": "conversation_memory",
                    "grounding_score": None,
                    "is_in_rag_pipeline": False,
                    "kb_score": 0.0,
                    "web_score": 0.0,
                    "winning_score": 1.0,
                    "selected_source": "conversation_memory",
                    "verification_status": "unverified",
                    "internal_evidence_confidence": 0.0,
                    "selected_evidence_quality": 0.0,
                    "external_search_used": False,
                    "citations": [],
                    "web_sources": [],
                    "follow_up_questions": followup_service.generate_starter_followups(),
                    "selection_summary": "Searched other conversations; no matching records found."
                }
            generation = GenerationResult()
            recalled_ans = llm_service.generate_answer(
                query, evidence=[], source_type="conversation_memory",
                chat_history=chat_history, historical_memory=hist_msgs, summary=summary, result=generation,
            )
            if generation.available is False:
                return self._unavailable_result(
                    route="conversation_memory", generation=generation, retrieved=hist_msgs, passages=[],
                    answer_plan=None, elapsed_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )
            return {
                "answer": recalled_ans,
                "source_type": "conversation_memory",
                "grounding_score": None,
                "is_in_rag_pipeline": False,
                "kb_score": 0.0,
                "web_score": 0.0,
                "winning_score": 1.0,
                "selected_source": "conversation_memory",
                "verification_status": "unverified",
                "internal_evidence_confidence": 1.0,
                "selected_evidence_quality": 1.0,
                "external_search_used": False,
                "citations": [],
                "web_sources": [],
                "follow_up_questions": followup_service.generate_starter_followups(),
                "selection_summary": "Answered from cross-conversation memory."
            }

        if memory_intent == MemoryIntent.HISTORY_RECALL:
            exclude_ids = [m.get("id") for m in (chat_history or [])]
            hist_msgs = conversation_memory_service.search_history(
                db, query, user_id=user_id, current_chat_id=chat_id,
                exclude_message_ids=exclude_ids, cross_conversation=False, top_k=5
            )
            if not hist_msgs and not chat_history:
                ans_text = "I reviewed our conversation history, but could not find any previous mention of that in our messages."
                return {
                    "answer": ans_text,
                    "source_type": "conversation_memory",
                    "grounding_score": None,
                    "is_in_rag_pipeline": False,
                    "kb_score": 0.0,
                    "web_score": 0.0,
                    "winning_score": 1.0,
                    "selected_source": "conversation_memory",
                    "verification_status": "unverified",
                    "internal_evidence_confidence": 0.0,
                    "selected_evidence_quality": 0.0,
                    "external_search_used": False,
                    "citations": [],
                    "web_sources": [],
                    "follow_up_questions": followup_service.generate_starter_followups(),
                    "selection_summary": "Searched conversation history; no matching records found."
                }
            generation = GenerationResult()
            recalled_ans = llm_service.generate_answer(
                query, evidence=[], source_type="conversation_memory",
                chat_history=chat_history, historical_memory=hist_msgs, summary=summary, result=generation,
            )
            if generation.available is False:
                return self._unavailable_result(
                    route="conversation_memory", generation=generation, retrieved=hist_msgs, passages=[],
                    answer_plan=None, elapsed_ms=round((time.perf_counter() - start_time) * 1000, 2),
                )
            return {
                "answer": recalled_ans,
                "source_type": "conversation_memory",
                "grounding_score": None,
                "is_in_rag_pipeline": False,
                "kb_score": 0.0,
                "web_score": 0.0,
                "winning_score": 1.0,
                "selected_source": "conversation_memory",
                "verification_status": "unverified",
                "internal_evidence_confidence": 1.0,
                "selected_evidence_quality": 1.0,
                "external_search_used": False,
                "citations": [],
                "web_sources": [],
                "follow_up_questions": followup_service.generate_starter_followups(),
                "selection_summary": "Answered from historical conversation memory."
            }

        if memory_intent == MemoryIntent.MIXED:
            exclude_ids = [m.get("id") for m in (chat_history or [])]
            historical_memory = conversation_memory_service.search_history(
                db, query, user_id=user_id, current_chat_id=chat_id,
                exclude_message_ids=exclude_ids, cross_conversation=False, top_k=5
            )

        # 1. Intelligent Query Routing Execution: the LLM reads the intent first (also fixes spelling)
        query, analysis, routing_decision = self._route_with_intent(query, chat_history, has_attachments)
        direct_query = analysis.standalone_question if analysis else query
        selected_route = routing_decision.route
        logger.info(
            f"[QueryRouter] Route: {selected_route.value} | "
            f"Requires Retrieval: {routing_decision.requires_retrieval} | Streamed: False"
        )

        # Path A: Sentimental and Casual Questions -> Direct LLM (Bypass RAG completely)
        if not has_attachments and selected_route == QueryRoute.EMOTIONAL_CASUAL:
            logger.debug("RAG route=emotional_casual query_hash=%s query_length=%d", privacy_fingerprint(query), len(query or ""))
            emotional_reply = llm_service.generate_emotional_casual_reply(query, chat_history=chat_history)
            follow_ups = followup_service.generate_starter_followups()
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.info(
                f"[QueryRouter Execution] Route: EMOTIONAL_CASUAL | Retrieval Executed: False | "
                f"Duration: {elapsed_ms}ms | Streamed: False"
            )
            return {
                "answer": emotional_reply,
                "source_type": "conversational",
                "grounding_score": None,
                "is_in_rag_pipeline": False,
                "kb_score": 0.0,
                "web_score": 0.0,
                "winning_score": 0.0,
                "selected_source": "conversational",
                "route": selected_route.value,
                "verification_status": "unverified",
                "internal_evidence_confidence": 0.0,
                "selected_evidence_quality": 0.0,
                "external_search_used": False,
                "citations": [],
                "web_sources": [],
                "follow_up_questions": follow_ups,
                "selection_summary": "Handled directly via LLM without retrieval (emotional/casual query)."
            }

        # Path G: a question that is not about SAP -> answered by the LLM, not refused
        if selected_route == QueryRoute.GENERAL:
            logger.info("[RAG] Fast-path routing: general (direct LLM, no retrieval required) | Streamed: False")
            formats = routing_decision.output_formats
            reply = llm_service.generate_general_reply(
                document_generator.query_with_instructions(query, formats) if formats else query,
                chat_history=chat_history, regeneration_index=regeneration_index)
            if formats and reply != UNAVAILABLE_RESPONSE:
                reply = self._documents_message(reply, formats, user_id, query)
            return self._direct_payload(reply, selected_route, "Answered directly via LLM (general, non-SAP question).")

        # Path K: genuinely ambiguous -> one clarifying question
        if selected_route == QueryRoute.CLARIFICATION:
            return self._direct_payload(
                analysis.clarification_question, selected_route, "Asked the user to clarify an ambiguous question.",
                source_type="conversational",
            )

        # Path B: Simple Conceptual SAP Questions -> Direct LLM (Bypass RAG completely)
        if not has_attachments and settings.SIMPLE_SAP_FAST_PATH_ENABLED and selected_route == QueryRoute.SIMPLE_SAP:
            logger.debug("RAG route=simple_sap query_hash=%s query_length=%d", privacy_fingerprint(query), len(query or ""))
            simple_sap_reply = llm_service.generate_simple_sap_reply(direct_query, chat_history=chat_history)
            follow_ups = followup_service.generate_starter_followups()
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.info(
                f"[QueryRouter Execution] Route: SIMPLE_SAP | Retrieval Executed: False | "
                f"Duration: {elapsed_ms}ms | Streamed: False"
            )
            return {
                "answer": simple_sap_reply,
                "source_type": "direct_llm",
                "grounding_score": None,
                "is_in_rag_pipeline": False,
                "kb_score": 0.0,
                "web_score": 0.0,
                "winning_score": 0.0,
                "selected_source": "direct_llm",
                "route": selected_route.value,
                "verification_status": "unverified",
                "internal_evidence_confidence": 0.0,
                "selected_evidence_quality": 0.0,
                "external_search_used": False,
                "citations": [],
                "web_sources": [],
                "follow_up_questions": follow_ups,
                "selection_summary": "Handled directly via LLM without retrieval (simple conceptual SAP question)."
            }

        # Mixed Queries with Simple SAP Technical Part -> Direct LLM with brief acknowledgment
        if not has_attachments and settings.SIMPLE_SAP_FAST_PATH_ENABLED and selected_route == QueryRoute.MIXED_QUERY and routing_decision.technical_route == QueryRoute.SIMPLE_SAP:
            ack = query_router.get_acknowledgment(routing_decision.emotional_part)
            tech_q = routing_decision.technical_part or query
            logger.debug("RAG route=mixed_simple query_hash=%s query_length=%d", privacy_fingerprint(query), len(query or ""))
            tech_reply = llm_service.generate_simple_sap_reply(tech_q, chat_history=chat_history)
            combined_answer = f"{ack} {tech_reply}".strip()
            follow_ups = followup_service.generate_starter_followups()
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.info(
                f"[QueryRouter Execution] Route: MIXED_QUERY (Simple) | Retrieval Executed: False | "
                f"Duration: {elapsed_ms}ms | Streamed: False"
            )
            return {
                "answer": combined_answer,
                "source_type": "direct_llm",
                "grounding_score": None,
                "is_in_rag_pipeline": False,
                "kb_score": 0.0,
                "web_score": 0.0,
                "winning_score": 0.0,
                "selected_source": "direct_llm",
                "route": selected_route.value,
                "verification_status": "unverified",
                "internal_evidence_confidence": 0.0,
                "selected_evidence_quality": 0.0,
                "external_search_used": False,
                "citations": [],
                "web_sources": [],
                "follow_up_questions": follow_ups,
                "selection_summary": "Handled directly via LLM with emotional acknowledgment and simple SAP answer."
            }

        # Retrieval-backed answer: one implementation shared with stream_query (F-27).
        return self._drain(self._answer_pipeline(
            db, query, chat_history=chat_history, attachments=attachments, summary=summary,
            historical_memory=historical_memory, routing_decision=routing_decision, start_time=start_time,
            access_groups=access_groups,
            analysis=analysis,
            owner_id=user_id,
            streamed=False,
            regeneration_index=regeneration_index,
        ))

    def _route_with_intent(self, query: str, chat_history, has_attachments: bool):
        """
        The first LLM call of a Scholar question: intent and spelling by meaning (intent_analyzer), then the
        processing path. Returns (question to use from here on, analysis or None, routing decision).
        Without an analysis (attachments, analyzer off or unavailable) the rule-based router decides as before.
        """
        analysis = None if has_attachments else intent_analyzer.analyze(query, chat_history)
        formats = [] if has_attachments else document_generator.requested_formats(analysis, query)
        if analysis is None:
            base = query_router.route(query, chat_history=chat_history)
            if formats and base.route in (QueryRoute.SIMPLE_SAP, QueryRoute.MIXED_QUERY, QueryRoute.EMOTIONAL_CASUAL):
                # A document is written from retrieved evidence, never from the short fast paths.
                base = QueryRoutingDecision(route=QueryRoute.COMPLEX_SAP, reason="Document request", cleaned_query=query, requires_retrieval=True)
            base.output_formats = formats if base.route in (QueryRoute.COMPLEX_SAP, QueryRoute.GENERAL) else []
            return query, None, base
        corrected = analysis.corrected_question
        if is_company_question(query) or is_company_question(corrected):
            # The fixed Clyptus profile answers these (in the shared pipeline); not a general question.
            return corrected, None, query_router.route(corrected, chat_history=chat_history)

        def decision(route, reason, retrieval=False, cleaned=corrected):
            return QueryRoutingDecision(route=route, reason=reason, cleaned_query=cleaned, requires_retrieval=retrieval,
                                        output_formats=formats if route in (QueryRoute.COMPLEX_SAP, QueryRoute.GENERAL) else [])

        if analysis.requested_language and languages.code_for(analysis.requested_language) is None:
            return corrected, analysis, decision(QueryRoute.OUT_OF_DOMAIN, "Answer language is not supported")
        if analysis.needs_clarification:
            return corrected, analysis, decision(QueryRoute.CLARIFICATION, "Genuinely ambiguous; asking the user")
        if analysis.small_talk:
            return corrected, analysis, decision(QueryRoute.EMOTIONAL_CASUAL, "LLM: greeting or small talk")
        if not analysis.about_sap:
            if settings.SAP_ONLY_SCOPE:
                return corrected, analysis, decision(QueryRoute.OUT_OF_DOMAIN, "LLM: not about SAP; declined (SAP-only scope)")
            return corrected, analysis, decision(QueryRoute.GENERAL, "LLM: general question, not about SAP")
        base = query_router.route(analysis.standalone_question, chat_history=chat_history)
        if base.route in (QueryRoute.SIMPLE_SAP, QueryRoute.MIXED_QUERY) and not formats:
            return corrected, analysis, base
        return corrected, analysis, decision(
            QueryRoute.COMPLEX_SAP, "LLM: SAP question for the retrieval pipeline", True, analysis.standalone_question
        )

    @staticmethod
    def _decline_message(analysis) -> str:
        """The reply for a declined question: the language notice when the user asked for an unsupported answer
        language, otherwise the SAP-only message."""
        if analysis is not None and analysis.requested_language and languages.code_for(analysis.requested_language) is None:
            return languages.unsupported_notice(analysis.requested_language)
        return NON_SAP_REFUSAL_RESPONSE

    @staticmethod
    def _documents_message(content: str, formats: List[str], owner_id, request: str) -> str:
        """The chat message for a document request: the files are created and linked; if that fails, the content itself."""
        try:
            return document_generator.create_documents(owner_id, content, formats, request_title=request)[0]
        except Exception as exc:
            logger.warning("Document creation failed (%s)", type(exc).__name__)
            return content

    @staticmethod
    def _direct_payload(answer: str, route: QueryRoute, summary: str, source_type: str = "direct_llm") -> Dict[str, Any]:
        """The result of a path that answers without retrieval (general answer, clarifying question)."""
        unavailable = answer == UNAVAILABLE_RESPONSE
        return {
            "answer": answer,
            "source_type": "unavailable" if unavailable else source_type,
            "grounding_score": None,
            "is_in_rag_pipeline": False,
            "kb_score": 0.0,
            "web_score": 0.0,
            "winning_score": 0.0,
            "selected_source": source_type,
            "route": route.value,
            "verification_status": "unavailable" if unavailable else "unverified",
            "internal_evidence_confidence": 0.0,
            "selected_evidence_quality": 0.0,
            "external_search_used": False,
            "citations": [],
            "web_sources": [],
            "follow_up_questions": [],
            "selection_summary": summary,
        }

    @staticmethod
    def _drain(events: Generator[Dict[str, Any], None, Dict[str, Any]]) -> Dict[str, Any]:
        """Run an event generator to completion and return its result (status events are dropped)."""
        while True:
            try:
                next(events)
            except StopIteration as stop:
                return stop.value

    def _answer_pipeline(
        self,
        db: Session,
        query: str,
        *,
        chat_history: Optional[List[Dict[str, Any]]],
        attachments: Optional[List[Dict[str, Any]]],
        summary: Optional[str],
        historical_memory: List[Dict[str, Any]],
        routing_decision,
        start_time: float,
        streamed: bool,
        access_groups=None,
        regeneration_index: Optional[int] = None,
        analysis=None,
        owner_id=None,
    ) -> Generator[Dict[str, Any], None, Dict[str, Any]]:
        """
        The retrieval-backed answer path shared by process_query and stream_query (AUDIT_REPORT F-27):
        resolve -> scope gate -> retrieve -> assess -> select evidence -> generate -> verify/repair/guard
        -> citations. Yields status events; returns the final payload. Callers decide how to deliver it,
        so the two entry points can no longer drift apart.
        """
        has_attachments = bool(attachments)
        selected_route = routing_decision.route

        yield {"type": "status", "stage": "thinking", "message": "Resolving conversation context..."}

        if analysis is not None:
            # The intent analyzer already corrected the question and resolved it against the conversation.
            is_follow_up = analysis.is_follow_up
            cleaned_query = analysis.standalone_question.strip()
        else:
            resolution = query_rewriter.resolve_query(query, chat_history=chat_history)
            is_follow_up = resolution.get("is_follow_up", False)
            cleaned_query = resolution.get("resolved_query", query).strip()

        # Strict SAP scope gate; uploaded attachments are analysed regardless of topic.
        target_check_query = cleaned_query if is_follow_up else query.strip()
        if not has_attachments and is_company_question(query):
            return {
                "answer": COMPANY_ANSWER,
                "source_type": "conversational",
                "grounding_score": None,
                "is_in_rag_pipeline": False,
                "kb_score": 0.0,
                "web_score": 0.0,
                "winning_score": 0.0,
                "selected_source": "company_info",
                "verification_status": "unverified",
                "internal_evidence_confidence": 0.0,
                "selected_evidence_quality": 0.0,
                "external_search_used": False,
                "citations": [],
                "web_sources": [],
                "follow_up_questions": [],
                "selection_summary": "Answered from the fixed Clyptus company profile.",
            }
        declined_by_analysis = analysis is not None and selected_route == QueryRoute.OUT_OF_DOMAIN
        if not has_attachments and (analysis is None or declined_by_analysis):
            # With an analysis the LLM already judged the topic: a non-SAP question is declined, anything else passes.
            is_sap, refusal_msg = (False, self._decline_message(analysis)) if declined_by_analysis else sap_classifier.classify(target_check_query)
            if not is_sap:
                logger.info("Query rejected by SAP classifier (route=%s)", selected_route.value)
                return {
                    "answer": refusal_msg,
                    "source_type": "refusal",
                    "grounding_score": 0.0,
                    "is_in_rag_pipeline": False,
                    "kb_score": 0.0,
                    "web_score": 0.0,
                    "winning_score": 0.0,
                    "selected_source": "refusal",
                    "verification_status": "rejected",
                    "internal_evidence_confidence": 0.0,
                    "selected_evidence_quality": 0.0,
                    "external_search_used": False,
                    "citations": [],
                    "web_sources": [],
                    "follow_up_questions": [],
                    "selection_summary": "Query rejected as outside SAP scope."
                }

        if has_attachments:
            yield {
                "type": "status",
                "stage": "document_analysis",
                "message": f"Analyzing {len(attachments)} attached document(s)..."
            }

        # Internal retrieval: each clear sub-question is retrieved on its own, then merged.
        yield {"type": "status", "stage": "retrieval", "message": "Thinking..."}
        logger.info(f"[RAG] Retrieval started | Query length: {len(cleaned_query)} chars | Target: internal_kb | Top-K: {settings.TOP_K}")
        _retrieval_start = time.perf_counter()
        top_kb_chunks, document_match_score, aspect_results = self._retrieve_aspect_evidence(
            db, cleaned_query, settings.TOP_K, allowed_groups=access_groups
        )
        _retrieval_ms = round((time.perf_counter() - _retrieval_start) * 1000, 1)
        logger.info(f"[RAG] Retrieval completed in {_retrieval_ms}ms: {len(top_kb_chunks)} passages retrieved (document match score: {document_match_score:.2f})")
        # A generic chatbot would be handed every candidate, not the few that survive reranking.
        token_meter.record_retrieved(top_kb_chunks)

        yield {"type": "status", "stage": "assessing_evidence", "message": "Thinking..."}
        internal_confidence_data = evidence_selection_service.compute_internal_confidence(
            cleaned_query, top_kb_chunks
        )
        if len(aspect_results) > 1:
            internal_confidence_data["aspects"] = aspect_results
            internal_confidence_data["confidence"] = min(
                float(internal_confidence_data["confidence"]),
                min((row["score"] for row in aspect_results), default=0.0),
            )
            internal_confidence_data["should_trigger_tavily"] = any(
                row["score"] < settings.INTERNAL_EVIDENCE_THRESHOLD or row["evidence_count"] == 0
                for row in aspect_results
            ) or internal_confidence_data["should_trigger_tavily"]
            internal_confidence_data["reason"] = "Per-part evidence assessment: " + "; ".join(
                f"{row['aspect'][:80]}={row['score']:.2f} ({row['evidence_count']} passages)" for row in aspect_results
            )
        internal_conf = internal_confidence_data["confidence"]
        auto_compare_trigger = self._should_auto_compare(document_match_score, internal_confidence_data)
        web_fallback_trigger = self._should_trigger_web_fallback(document_match_score, internal_confidence_data)
        should_trigger_tavily = auto_compare_trigger or web_fallback_trigger

        # Legacy web-only path; the multi-source comparator fetches its own web evidence.
        web_results = []
        if web_fallback_trigger and not auto_compare_trigger:
            yield {"type": "status", "stage": "web_search", "message": "Thinking..."}
            try:
                web_results = web_search_service.search_sap_authoritative(cleaned_query)
                logger.info(f"[RAG] External web search completed: {len(web_results)} web passages retrieved")
                token_meter.record_retrieved(web_results)
            except Exception as e:
                logger.warning(f"[RAG WARNING] Web search error: {e}")
                web_results = []

        repo_enabled = getattr(settings, "REPO_SOURCE_ENABLED", False)
        compare_enabled = getattr(settings, "MULTI_SOURCE_COMPARE_ENABLED", False)

        # Repository shadow mode: audit what the repository source would have chosen, answer unchanged.
        shadow_audit = None
        if repo_enabled and not compare_enabled:
            try:
                shadow_audit = self.comparator.run_shadow_comparison(
                    db=db, query=cleaned_query, kb_candidates=top_kb_chunks
                )
                logger.info(
                    "[SHADOW MODE AUDIT] Hypothetical winner: %s | Score: %s",
                    shadow_audit.get("hypothetical_winner"), shadow_audit.get("hypothetical_score"),
                )
            except Exception as e:
                logger.warning(f"Shadow mode comparison execution error: {e}")

        yield {"type": "status", "stage": "evidence_selection", "message": "Thinking..."}
        comp_result = {}
        if compare_enabled or auto_compare_trigger:
            yield {"type": "status", "stage": "multi_source_search", "message": "Thinking..."}
            from backend.app.retrieval.multi_source_compare import multi_source_comparator
            comp_result = multi_source_comparator.execute_multi_source_comparison(
                db=db,
                query=cleaned_query,
                kb_candidates=top_kb_chunks,
                repo_source_enabled=repo_enabled,
                multi_source_compare_enabled=True,
                web_search_available=settings.WEB_FALLBACK_ENABLED,
                force_web_search=auto_compare_trigger,
                query_aspects=self._external_search_aspects(cleaned_query, aspect_results),
            )
            winning_cand = comp_result.get("winning_candidate")
            if winning_cand:
                selected_passages = [winning_cand] + [
                    c for c in comp_result.get("eligible_candidates", [])
                    if c["candidate_id"] != winning_cand["candidate_id"]
                ][:3]
                selected_source_type = winning_cand["source_type"]
                selected_quality = winning_cand["final_score"]
                evidence_selection = {
                    "selection_summary": comp_result.get("comparison", {}).get("selection_reason", "Highest-scoring eligible source selected."),
                    "selected_evidence_quality": selected_quality,
                }
            else:
                selected_passages = []
                selected_source_type = "insufficient"
                selected_quality = 0.0
                evidence_selection = {
                    "selection_summary": "No source met the evidence eligibility threshold.",
                    "selected_evidence_quality": 0.0,
                }
        else:
            evidence_selection = evidence_selection_service.select_strongest_evidence(
                query=cleaned_query,
                internal_candidates=top_kb_chunks,
                external_candidates=web_results,
                internal_confidence_data=internal_confidence_data,
                max_internal_passages=(
                    CROSS_COMPONENT_RERANK_TOP_K
                    if is_cross_component_handoff(cleaned_query)
                    else None
                ),
            )
            selected_passages = evidence_selection.get("selected_passages") or top_kb_chunks or web_results or []
            selected_source_type = evidence_selection.get("source_type") or "knowledge_base"
            selected_quality = evidence_selection.get("selected_evidence_quality", 0.8)

        selected_passages = self._ensure_aspect_coverage(
            selected_passages, top_kb_chunks, aspect_results, interleave=is_solution_design_question(cleaned_query)
        )

        if has_attachments:
            selected_passages = self._attachment_passages(cleaned_query, attachments) + selected_passages
            selected_source_type = "attached_document"

        # Do not discard supported subparts/calculations because another aspect is weak.
        if selected_source_type in ("insufficient", "none"):
            selected_passages = top_kb_chunks or web_results
            selected_source_type = selected_passages[0].get("source_type", "knowledge_base") if selected_passages else "insufficient"
            selected_quality = 0.0
            evidence_selection["selection_summary"] = (
                "No candidate met the source eligibility threshold. Answering only independently supportable parts; "
                "this retrieval outcome does not establish that the functionality is unsupported."
            )

        # Block/restriction questions: evidence for each candidate block type.
        scope = self._classify_scope(cleaned_query)
        if scope:
            selected_passages = self._ensure_scope_coverage(
                selected_passages, self._retrieve_scope_evidence(db, scope, allowed_groups=access_groups)
            )
        selected_passages = self._prepare_evidence(selected_passages)
        guidance = self._generation_guidance(scope, selected_passages)
        document_formats = list(getattr(routing_decision, "output_formats", []) or [])
        if document_formats:  # the user asked for a file: the model writes the document itself
            guidance = (f"{guidance}\n\n" if guidance else "") + document_generator.content_instructions(document_formats)
        # Layout only (answer type, tables, headings); routed once and reused by generation and repair.
        answer_plan = answer_style_router.route(cleaned_query, selected_passages, raw_question=query)

        # The draft is generated in full and never shown before verification (F-03).
        yield {"type": "status", "stage": "generating", "message": "Generating response..."}
        gen_res = self._generate_grounded_answer(
            cleaned_query, selected_passages, source_type=selected_source_type, guidance=guidance,
            answer_plan=answer_plan, chat_history=chat_history, historical_memory=historical_memory,
            summary=summary, regeneration_index=regeneration_index,
        )
        generation = gen_res.get("generation") or GenerationResult()
        if generation.available is False:
            # Every LLM tier failed: approved SAP web search is the final fallback.
            yield {"type": "status", "stage": "web_search", "message": "Checking approved SAP web sources..."}
            web_passages = self._final_web_search(
                cleaned_query, selected_passages, web_results, comp_result.get("eligible_candidates")
            )
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            if web_passages:
                logger.info("All LLM tiers failed (category=%s); answering from %d approved web sources.",
                            generation.failure_category, len(web_passages))
                return self._web_fallback_result(
                    query=cleaned_query, route=selected_route.value, generation=generation,
                    retrieved=top_kb_chunks, passages=web_passages, answer_plan=answer_plan,
                    elapsed_ms=elapsed_ms, kb_score=document_match_score,
                )
            logger.warning("All LLM tiers and the web-search fallback failed (category=%s).", generation.failure_category)
            yield {"type": "status", "stage": "unavailable", "message": "No response could be generated."}
            return self._unavailable_result(
                route=selected_route.value,
                generation=generation,
                retrieved=top_kb_chunks,
                passages=selected_passages,
                answer_plan=answer_plan,
                elapsed_ms=elapsed_ms,
                kb_score=document_match_score,
                answer=ALL_METHODS_UNAVAILABLE_RESPONSE,
            )
        candidate_answer = gen_res["answer"]
        initial_score = gen_res.get("grounded_score", 0.85)
        initial_verification = gen_res.get("verification") or {
            "grounding_score": initial_score,
            "verification_status": gen_res.get("verification_status", "verified"),
            "passes_grounding_gate": gen_res.get(
                "passes_grounding_gate", initial_score >= settings.ANSWER_GROUNDING_THRESHOLD
            ),
            "unsupported_claims": gen_res.get("unsupported_claims", []),
            "critical_unsupported_claims": gen_res.get("critical_unsupported_claims", []),
        }

        # Claim verification, one controlled repair, deterministic guard, revalidation.
        yield {"type": "status", "stage": "verifying_claims", "message": "Finalizing response..."}
        final_answer, final_verification, repair_info, verification_status = self._finalize_answer(
            cleaned_query, candidate_answer, selected_passages, selected_source_type, scope, guidance,
            verification=initial_verification, answer_plan=answer_plan,
        )
        if not settings.REFERENCE_MODE_ENABLED and not final_verification.get("passes_grounding_gate", False):
            retry_web = (
                not should_trigger_tavily
                and settings.AUTO_MULTI_SOURCE_FALLBACK
                and settings.WEB_FALLBACK_ENABLED
            )
            web_retry_results = []
            if retry_web:
                yield {"type": "status", "stage": "web_search", "message": "Checking approved SAP web sources..."}
                try:
                    web_retry_results = web_search_service.search_sap_authoritative(cleaned_query)
                except Exception as exc:
                    logger.warning("Grounding-triggered web fallback failed (%s)", type(exc).__name__)

            if web_retry_results:
                retry_passages = self._prepare_evidence(list(top_kb_chunks) + list(web_retry_results))
                retry_source_type = "combined" if top_kb_chunks else "web"
                retry_guidance = self._generation_guidance(scope, retry_passages)
                retry_plan = answer_style_router.route(cleaned_query, retry_passages, raw_question=query)
                yield {"type": "status", "stage": "generating", "message": "Preparing an answer from retrieved and web evidence..."}
                retry_result = self._generate_grounded_answer(
                    cleaned_query, retry_passages, source_type=retry_source_type, guidance=retry_guidance,
                    answer_plan=retry_plan, chat_history=chat_history,
                    historical_memory=historical_memory, summary=summary,
                )
                retry_generation = retry_result.get("generation") or GenerationResult()
                if retry_generation.available is not False:
                    retry_candidate = retry_result["answer"]
                    retry_verification = retry_result.get("verification") or {
                        "grounding_score": retry_result.get("grounded_score"),
                        "verification_status": retry_result.get("verification_status", "verified"),
                        "passes_grounding_gate": retry_result.get("passes_grounding_gate", False),
                        "unsupported_claims": retry_result.get("unsupported_claims", []),
                        "critical_unsupported_claims": retry_result.get("critical_unsupported_claims", []),
                    }
                    yield {"type": "status", "stage": "verifying_claims", "message": "Verifying the web-backed response..."}
                    retry_final, retry_final_verification, retry_repair, retry_status = self._finalize_answer(
                        cleaned_query, retry_candidate, retry_passages, retry_source_type, scope, retry_guidance,
                        verification=retry_verification, answer_plan=retry_plan,
                    )
                    # Keep the web-backed retry unless it is worse supported, even when neither passes the gate:
                    # its evidence is the original evidence plus the web results.
                    if retry_final_verification.get("passes_grounding_gate", False) or (
                        self._verification_rank(retry_final_verification) <= self._verification_rank(final_verification)
                    ):
                        candidate_answer = retry_candidate
                        final_answer = retry_final
                        final_verification = retry_final_verification
                        repair_info = retry_repair
                        verification_status = retry_status
                        generation = retry_generation
                        selected_passages = retry_passages
                        selected_source_type = retry_source_type
                        selected_quality = float(retry_final_verification.get("grounding_score") or 0.0)
                        answer_plan = retry_plan
                        should_trigger_tavily = True
                        evidence_selection["selection_summary"] = "Internal answer failed grounding; retried against approved SAP web evidence."

            if not final_verification.get("passes_grounding_gate", False):
                # A generated answer exists, so it is not replaced by an error message: its unconfirmed
                # technical claims are removed and it is published with a notice, never as verified.
                final_answer, final_verification, verification_status = self._publish_unverified(
                    cleaned_query, final_answer, final_verification, selected_passages, selected_source_type, scope,
                )
                repair_info["published_unverified"] = True
                logger.info("Answer failed the grounding gate; published as %s with a notice.", verification_status)
                evidence_selection["selection_summary"] = (
                    "The answer did not pass claim verification and is published with an explicit notice."
                )
        final_grounding_score = final_verification.get("grounding_score", initial_score)
        validation_audit = self._validation_audit(
            cleaned_query, document_match_score, selected_passages, final_verification, aspect_results, scope, repair_info
        )
        self._attach_answer_style(validation_audit, answer_plan)

        follow_ups = followup_service.generate_followup_questions(
            cleaned_query, strip_markers(final_answer), selected_passages
        )
        citations, web_sources = self._build_citations(selected_passages)
        comparison_summary = self._comparison_summary(comp_result) if comp_result else {
            "source_scores": {"knowledge_base": round(float(document_match_score), 4)},
            "comparison_audit": None,
        }

        # Visual retrieval only when the user explicitly asks for diagrams/illustrations.
        relevant_images = []
        if web_search_service.wants_visuals(cleaned_query):
            try:
                relevant_images = web_search_service.search_sap_images(cleaned_query)
            except Exception as e:
                logger.warning(f"Visual search failed: {e}")

        if document_formats and final_answer.strip() != UNAVAILABLE_RESPONSE:
            final_answer = self._documents_message(final_answer, document_formats, owner_id, cleaned_query)

        # Mixed emotional + technical question: brief acknowledgment before the verified answer.
        if selected_route == QueryRoute.MIXED_QUERY and routing_decision.emotional_part:
            ack = query_router.get_acknowledgment(routing_decision.emotional_part)
            if not final_answer.startswith(ack):
                final_answer = f"{ack}\n\n{final_answer}"

        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
        logger.info(
            f"[QueryRouter Execution] Route: {selected_route.value} | Retrieval Executed: True | "
            f"Duration: {elapsed_ms}ms | Streamed: {streamed}"
        )
        diagnostics = self._generation_diagnostics(
            generation, top_kb_chunks, selected_passages, final_answer, candidate_answer,
            repair_info, answer_plan, elapsed_ms,
        )
        logger.debug(f"Answer Generation Diagnostics: {json.dumps(diagnostics)}")

        return {
            "answer": final_answer,
            "route": selected_route.value,
            "source_type": selected_source_type,
            "grounding_score": final_grounding_score,
            "grounded_score": final_grounding_score,
            "is_in_rag_pipeline": internal_conf >= settings.INTERNAL_EVIDENCE_THRESHOLD,
            "kb_score": round(document_match_score, 4),
            "web_score": round(max((c["calibrated_relevance_score"] for c in comp_result.get("all_candidates", []) if c["source_type"] == "web"), default=0.0), 4) if comp_result else round(selected_quality, 4) if should_trigger_tavily else 0.0,
            "winning_score": round(selected_quality, 4),
            "source_scores": comparison_summary["source_scores"],
            "selected_source": selected_source_type,
            "verification_status": verification_status,
            "internal_evidence_confidence": internal_conf,
            "selected_evidence_quality": selected_quality,
            "external_search_used": should_trigger_tavily,
            "citations": citations,
            "web_sources": web_sources,
            "images": relevant_images,
            "follow_up_questions": follow_ups,
            "selection_summary": evidence_selection.get("selection_summary", ""),
            "model_used": generation.model,
            "provider_used": generation.provider,
            "failed_over": generation.failed_over,
            # Grounding repairs of this answer (admin diagnostic); "regeneration_count" is the user's
            # Regenerate count, which the chat API sets.
            "repair_count": 1 if repair_info["attempted"] else 0,
            "shadow_audit": shadow_audit,
            "comparison_audit": comparison_summary["comparison_audit"],
            # Admin/audit only: stripped for ordinary users by the API layer.
            "answer_decision": validation_audit.get("decision"),
            "validation_audit": validation_audit,
            "diagnostics": diagnostics,
        }

    def _build_citations(self, passages: List[Dict[str, Any]]):
        """Citations (documents, attachments, repositories) and web sources for the selected evidence."""
        citations: List[Dict[str, Any]] = []
        web_sources: List[Dict[str, Any]] = []
        for p in passages:
            src_type = p.get("source_type") or ("web" if p.get("url") else "knowledge_base")
            if src_type == "attached_document":
                citations.append({
                    "source_type": "attached_document",
                    "chunk_id": "att-doc",
                    "document": p.get("document_name") or p.get("document") or "Attached Document",
                    "page": 1,
                    "section": p.get("section") or "Attached Document Content",
                    "score": 1.0,
                    "snippet": (p.get("snippet") or p.get("chunk_text") or "")[:350],
                    "ref": p.get("evidence_id"),
                })
            elif src_type == "knowledge_base":
                citations.append({**self._format_kb_citation(p), "ref": p.get("evidence_id")})
            elif src_type == "github_repo":
                citations.append({
                    "source_type": "github_repo",
                    "chunk_id": str(p.get("candidate_id") or p.get("chunk_id") or ""),
                    "document": p.get("document_name") or f"github:{p.get('repo_name', 'repository')}",
                    "page": 1,
                    "section": p.get("section") or p.get("file_path") or "Repository Source",
                    "score": round(float(p.get("final_score", p.get("calibrated_relevance_score", p.get("score", 0.7)))), 4),
                    "snippet": (p.get("snippet") or p.get("chunk_text") or "").replace("�", "-").strip(),
                    "ref": p.get("evidence_id"),
                })
            elif src_type == "web":
                web_sources.append({
                    "title": p.get("title") or p.get("document_name") or "SAP Official Documentation",
                    "url": p.get("url", ""),
                    "domain": p.get("domain", "help.sap.com"),
                    "snippet": (p.get("snippet") or p.get("chunk_text") or "").replace("�", "-").strip(),
                    "ref": p.get("evidence_id"),
                })
        return self._dedupe_citations(citations), web_sources

    def stream_query(
        self,
        db: Session,
        query: str,
        chat_history: Optional[List[Dict[str, Any]]] = None,
        user: Optional[Any] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        chat_id: Optional[str] = None,
        regeneration_index: Optional[int] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """
        Progressive Streaming Pipeline for SAP BRIM Knowledge Assistant with Security Enforcement.
        Emits realistic status events during retrieval, evidence evaluation, planning,
        and claim verification.
        Buffers candidate answer tokens to guarantee that unverified text is NOT displayed
        before passing the 80% grounding gate.
        Then streams the verified text smoothly to the user.
        """
        start_time = time.perf_counter()
        # 0. Resolve user identity and role
        user_id = getattr(user, "id", None) if user else None
        # Document-level access control: only documents sharing the caller's groups are retrieved (F-01).
        access_groups = retrieval_groups(user)
        has_attachments = bool(attachments)

        # Strict 20-message conversation context window
        if chat_id and chat_history is None:
            chat_history = conversation_memory_service.get_recent_messages(db, chat_id, limit=20)
        elif chat_history:
            chat_history = chat_history[-20:]

        historical_memory: List[Dict[str, Any]] = []

        # Check for conversation memory intent (exact first question, history recall, cross-conversation)
        memory_intent = conversation_memory_service.classify_intent(query, chat_history=chat_history)
        summary = (
            conversation_memory_service.get_chat_summary(db, chat_id)
            if chat_id and _needs_conversation_summary(memory_intent, chat_history) else None
        )

        if memory_intent == MemoryIntent.FIRST_MESSAGE_LOOKUP:
            first_msg = conversation_memory_service.get_first_user_message(db, chat_id) if chat_id else None
            if first_msg:
                first_clean = re.sub(r"^\[Attached:.*?\]\n\n", "", first_msg["content"].strip(), flags=re.DOTALL)
                ts_str = first_msg["created_at"].strftime("%Y-%m-%d %H:%M:%S UTC") if isinstance(first_msg["created_at"], datetime) else str(first_msg["created_at"])
                ans_text = (
                    f"Your first question in this chat was:\n\n"
                    f"> \"{first_clean}\"\n\n"
                    f"*(Sent: {ts_str}, Message ID: `{first_msg['id']}`)*"
                )
            else:
                ans_text = "There are no previous questions recorded in this conversation."

            yield {
                "type": "status",
                "stage": "thinking",
                "message": "Retrieving from conversation memory..."
            }
            words = ans_text.split(" ")
            for i, w in enumerate(words):
                yield {"type": "token", "content": w + (" " if i < len(words) - 1 else "")}

            follow_ups = followup_service.generate_starter_followups()
            yield {
                "type": "done",
                "answer": ans_text,
                "source_type": "conversation_memory",
                "grounding_score": None,
                "is_in_rag_pipeline": False,
                "kb_score": 0.0,
                "web_score": 0.0,
                "winning_score": 1.0,
                "selected_source": "conversation_memory",
                "verification_status": "unverified",
                "internal_evidence_confidence": 1.0,
                "selected_evidence_quality": 1.0,
                "external_search_used": False,
                "citations": [],
                "web_sources": [],
                "follow_up_questions": follow_ups,
                "selection_summary": "Retrieved first user question from conversation history."
            }
            return

        if memory_intent == MemoryIntent.LAST_MESSAGE_LOOKUP:
            last_msg = conversation_memory_service.get_last_user_message(db, chat_id) if chat_id else None
            if last_msg:
                last_clean = re.sub(r"^\[Attached:.*?\]\n\n", "", last_msg["content"].strip(), flags=re.DOTALL)
                ts_str = last_msg["created_at"].strftime("%Y-%m-%d %H:%M:%S UTC") if isinstance(last_msg["created_at"], datetime) else str(last_msg["created_at"])
                ans_text = (
                    f"Your last question prior to this was:\n\n"
                    f"> \"{last_clean}\"\n\n"
                    f"*(Sent: {ts_str}, Message ID: `{last_msg['id']}`)*"
                )
            else:
                ans_text = "There are no prior messages found in this conversation."

            yield {
                "type": "status",
                "stage": "thinking",
                "message": "Retrieving from conversation memory..."
            }
            words = ans_text.split(" ")
            for i, w in enumerate(words):
                yield {"type": "token", "content": w + (" " if i < len(words) - 1 else "")}

            follow_ups = followup_service.generate_starter_followups()
            yield {
                "type": "done",
                "answer": ans_text,
                "source_type": "conversation_memory",
                "grounding_score": None,
                "is_in_rag_pipeline": False,
                "kb_score": 0.0,
                "web_score": 0.0,
                "winning_score": 1.0,
                "selected_source": "conversation_memory",
                "verification_status": "unverified",
                "internal_evidence_confidence": 1.0,
                "selected_evidence_quality": 1.0,
                "external_search_used": False,
                "citations": [],
                "web_sources": [],
                "follow_up_questions": follow_ups,
                "selection_summary": "Retrieved last message directly from conversation history."
            }
            return

        if memory_intent == MemoryIntent.CROSS_CONVERSATION_RECALL:
            exclude_ids = [m.get("id") for m in (chat_history or [])]
            hist_msgs = conversation_memory_service.search_history(
                db, query, user_id=user_id, current_chat_id=chat_id,
                exclude_message_ids=exclude_ids, cross_conversation=True, top_k=5
            )
            if not hist_msgs:
                ans_text = "I searched your other conversations, but could not find any earlier discussion matching your query."
                yield {"type": "status", "stage": "thinking", "message": "Searching conversation history..."}
                words = ans_text.split(" ")
                for i, w in enumerate(words):
                    yield {"type": "token", "content": w + (" " if i < len(words) - 1 else "")}
                yield {
                    "type": "done",
                    "answer": ans_text,
                    "source_type": "conversation_memory",
                    "grounding_score": None,
                    "is_in_rag_pipeline": False,
                    "kb_score": 0.0,
                    "web_score": 0.0,
                    "winning_score": 1.0,
                    "selected_source": "conversation_memory",
                    "verification_status": "unverified",
                    "internal_evidence_confidence": 0.0,
                    "selected_evidence_quality": 0.0,
                    "external_search_used": False,
                    "citations": [],
                    "web_sources": [],
                    "follow_up_questions": followup_service.generate_starter_followups(),
                    "selection_summary": "Searched other conversations; no matching records found."
                }
                return

            yield {"type": "status", "stage": "thinking", "message": "Recalling from other conversations..."}
            token_buffer = ""
            generation = GenerationResult()
            for token in llm_service.stream_answer(
                query, evidence=[], source_type="conversation_memory",
                chat_history=chat_history, historical_memory=hist_msgs, summary=summary, result=generation,
            ):
                token_buffer += token
                yield {"type": "token", "content": token}

            yield {
                "type": "done",
                "answer": token_buffer,
                "source_type": "unavailable" if generation.available is False else "conversation_memory",
                "grounding_score": None,
                "is_in_rag_pipeline": False,
                "kb_score": 0.0,
                "web_score": 0.0,
                "winning_score": 1.0,
                "selected_source": "unavailable" if generation.available is False else "conversation_memory",
                "verification_status": "unavailable" if generation.available is False else "unverified",
                "internal_evidence_confidence": 1.0,
                "selected_evidence_quality": 1.0,
                "external_search_used": False,
                "citations": [],
                "web_sources": [],
                "follow_up_questions": followup_service.generate_starter_followups(),
                "selection_summary": "Answered from cross-conversation memory."
            }
            return

        if memory_intent == MemoryIntent.HISTORY_RECALL:
            exclude_ids = [m.get("id") for m in (chat_history or [])]
            hist_msgs = conversation_memory_service.search_history(
                db, query, user_id=user_id, current_chat_id=chat_id,
                exclude_message_ids=exclude_ids, cross_conversation=False, top_k=5
            )
            if not hist_msgs and not chat_history:
                ans_text = "I reviewed our conversation history, but could not find any previous mention of that in our messages."
                yield {"type": "status", "stage": "thinking", "message": "Searching conversation history..."}
                words = ans_text.split(" ")
                for i, w in enumerate(words):
                    yield {"type": "token", "content": w + (" " if i < len(words) - 1 else "")}
                yield {
                    "type": "done",
                    "answer": ans_text,
                    "source_type": "conversation_memory",
                    "grounding_score": None,
                    "is_in_rag_pipeline": False,
                    "kb_score": 0.0,
                    "web_score": 0.0,
                    "winning_score": 1.0,
                    "selected_source": "conversation_memory",
                    "verification_status": "unverified",
                    "internal_evidence_confidence": 0.0,
                    "selected_evidence_quality": 0.0,
                    "external_search_used": False,
                    "citations": [],
                    "web_sources": [],
                    "follow_up_questions": followup_service.generate_starter_followups(),
                    "selection_summary": "Searched conversation history; no matching records found."
                }
                return

            yield {"type": "status", "stage": "thinking", "message": "Recalling from conversation history..."}
            token_buffer = ""
            generation = GenerationResult()
            for token in llm_service.stream_answer(
                query, evidence=[], source_type="conversation_memory",
                chat_history=chat_history, historical_memory=hist_msgs, summary=summary, result=generation,
            ):
                token_buffer += token
                yield {"type": "token", "content": token}

            yield {
                "type": "done",
                "answer": token_buffer,
                "source_type": "unavailable" if generation.available is False else "conversation_memory",
                "grounding_score": None,
                "is_in_rag_pipeline": False,
                "kb_score": 0.0,
                "web_score": 0.0,
                "winning_score": 1.0,
                "selected_source": "unavailable" if generation.available is False else "conversation_memory",
                "verification_status": "unavailable" if generation.available is False else "unverified",
                "internal_evidence_confidence": 1.0,
                "selected_evidence_quality": 1.0,
                "external_search_used": False,
                "citations": [],
                "web_sources": [],
                "follow_up_questions": followup_service.generate_starter_followups(),
                "selection_summary": "Answered from historical conversation memory."
            }
            return

        if memory_intent == MemoryIntent.MIXED:
            exclude_ids = [m.get("id") for m in (chat_history or [])]
            historical_memory = conversation_memory_service.search_history(
                db, query, user_id=user_id, current_chat_id=chat_id,
                exclude_message_ids=exclude_ids, cross_conversation=False, top_k=5
            )

        # STAGE 0: Intelligent Query Routing Execution: the LLM reads the intent first (also fixes spelling)
        query, analysis, routing_decision = self._route_with_intent(query, chat_history, has_attachments)
        direct_query = analysis.standalone_question if analysis else query
        selected_route = routing_decision.route
        logger.info(
            f"[QueryRouter] Route: {selected_route.value} | "
            f"Requires Retrieval: {routing_decision.requires_retrieval} | Streamed: True"
        )
        if analysis is not None and analysis.requested_language:
            # Consumed by the chat handler (never sent to the browser): the language asked for in this message.
            yield {"type": "answer_language", "requested": analysis.requested_language}

        # Path A: Sentimental and Casual Questions -> Direct LLM Stream (Bypass RAG completely)
        if not has_attachments and selected_route == QueryRoute.EMOTIONAL_CASUAL:
            logger.info("[RAG] Fast-path routing: emotional_casual (direct LLM, no retrieval required)")
            yield {
                "type": "status",
                "stage": "generating",
                "message": "Generating response..."
            }
            token_buffer = ""
            for token in llm_service.stream_emotional_casual_reply(query, chat_history=chat_history, regeneration_index=regeneration_index):
                token_buffer += token
                yield {
                    "type": "token",
                    "content": token
                }

            follow_ups = followup_service.generate_starter_followups()
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.info(
                f"[QueryRouter Execution] Route: EMOTIONAL_CASUAL | Retrieval Executed: False | "
                f"Duration: {elapsed_ms}ms | Streamed: True"
            )
            yield {
                "type": "done",
                "answer": token_buffer,
                "source_type": "conversational",
                "grounding_score": None,
                "is_in_rag_pipeline": False,
                "kb_score": 0.0,
                "web_score": 0.0,
                "winning_score": 0.0,
                "selected_source": "conversational",
                "route": selected_route.value,
                "verification_status": "unverified",
                "internal_evidence_confidence": 0.0,
                "selected_evidence_quality": 0.0,
                "external_search_used": False,
                "citations": [],
                "web_sources": [],
                "follow_up_questions": follow_ups,
                "selection_summary": "Handled directly via LLM without retrieval (emotional/casual query)."
            }
            return

        # Path G: a question that is not about SAP -> streamed by the LLM, not refused
        if selected_route == QueryRoute.GENERAL:
            logger.info("[RAG] Fast-path routing: general (direct LLM, no retrieval required) | Streamed: True")
            formats = routing_decision.output_formats
            yield {"type": "status", "stage": "generating", "message": "Creating your document..." if formats else "Generating response..."}
            token_buffer = ""
            for token in llm_service.stream_general_reply(
                    document_generator.query_with_instructions(query, formats) if formats else query,
                    chat_history=chat_history, regeneration_index=regeneration_index):
                token_buffer += token
                if not formats:
                    yield {"type": "token", "content": token}
            if formats:
                if token_buffer.strip() != UNAVAILABLE_RESPONSE:
                    token_buffer = self._documents_message(token_buffer, formats, user_id, query)
                yield from self._verified_token_events(token_buffer)
            yield {"type": "done", **self._direct_payload(
                token_buffer, selected_route, "Answered directly via LLM (general, non-SAP question).")}
            return

        # Path K: genuinely ambiguous -> one clarifying question
        if selected_route == QueryRoute.CLARIFICATION:
            question = analysis.clarification_question
            yield {"type": "token", "content": question}
            yield {"type": "done", **self._direct_payload(
                question, selected_route, "Asked the user to clarify an ambiguous question.", source_type="conversational")}
            return

        # Path B: Simple Conceptual SAP Questions -> Direct LLM Stream (Bypass RAG completely)
        if not has_attachments and settings.SIMPLE_SAP_FAST_PATH_ENABLED and selected_route == QueryRoute.SIMPLE_SAP:
            logger.info("[RAG] Fast-path routing: simple_sap (direct LLM, no retrieval required)")
            yield {
                "type": "status",
                "stage": "generating",
                "message": "Generating response..."
            }
            token_buffer = ""
            for token in llm_service.stream_simple_sap_reply(direct_query, chat_history=chat_history, regeneration_index=regeneration_index):
                token_buffer += token
                yield {
                    "type": "token",
                    "content": token
                }

            follow_ups = followup_service.generate_starter_followups()
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.info(
                f"[QueryRouter Execution] Route: SIMPLE_SAP | Retrieval Executed: False | "
                f"Duration: {elapsed_ms}ms | Streamed: True"
            )
            yield {
                "type": "done",
                "answer": token_buffer,
                "source_type": "direct_llm",
                "grounding_score": None,
                "is_in_rag_pipeline": False,
                "kb_score": 0.0,
                "web_score": 0.0,
                "winning_score": 0.0,
                "selected_source": "direct_llm",
                "route": selected_route.value,
                "verification_status": "unverified",
                "internal_evidence_confidence": 0.0,
                "selected_evidence_quality": 0.0,
                "external_search_used": False,
                "citations": [],
                "web_sources": [],
                "follow_up_questions": follow_ups,
                "selection_summary": "Handled directly via LLM without retrieval (simple conceptual SAP question)."
            }
            return

        # Mixed Queries with Simple SAP Technical Part -> Direct LLM Stream with brief acknowledgment
        if not has_attachments and settings.SIMPLE_SAP_FAST_PATH_ENABLED and selected_route == QueryRoute.MIXED_QUERY and routing_decision.technical_route == QueryRoute.SIMPLE_SAP:
            ack = query_router.get_acknowledgment(routing_decision.emotional_part)
            tech_q = routing_decision.technical_part or query
            logger.info("[RAG] Fast-path routing: mixed_simple (direct LLM with acknowledgment)")
            yield {
                "type": "status",
                "stage": "generating",
                "message": "Generating response..."
            }
            token_buffer = ack + " "
            yield {
                "type": "token",
                "content": ack + " "
            }
            for token in llm_service.stream_simple_sap_reply(tech_q, chat_history=chat_history, regeneration_index=regeneration_index):
                token_buffer += token
                yield {
                    "type": "token",
                    "content": token
                }

            follow_ups = followup_service.generate_starter_followups()
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.info(
                f"[QueryRouter Execution] Route: MIXED_QUERY (Simple) | Retrieval Executed: False | "
                f"Duration: {elapsed_ms}ms | Streamed: True"
            )
            yield {
                "type": "done",
                "answer": token_buffer.strip(),
                "source_type": "direct_llm",
                "grounding_score": None,
                "is_in_rag_pipeline": False,
                "kb_score": 0.0,
                "web_score": 0.0,
                "winning_score": 0.0,
                "selected_source": "direct_llm",
                "route": selected_route.value,
                "verification_status": "unverified",
                "internal_evidence_confidence": 0.0,
                "selected_evidence_quality": 0.0,
                "external_search_used": False,
                "citations": [],
                "web_sources": [],
                "follow_up_questions": follow_ups,
                "selection_summary": "Handled directly via LLM with emotional acknowledgment and simple SAP answer."
            }
            return

        # Retrieval-backed answer: one implementation shared with process_query (F-27).
        result = yield from self._answer_pipeline(
            db, query, chat_history=chat_history, attachments=attachments, summary=summary,
            historical_memory=historical_memory, routing_decision=routing_decision, start_time=start_time,
            access_groups=access_groups,
            analysis=analysis,
            owner_id=user_id,
            streamed=True,
            regeneration_index=regeneration_index,
        )
        # Only verified text reaches the client: verification, repair and guard already ran (F-03).
        yield from self._verified_token_events(result["answer"])
        yield {"type": "done", **result}


rag_service = RAGService()
