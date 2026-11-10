import re
import logging
from typing import List, Dict, Any, Tuple, Optional
from backend.app.config import settings

logger = logging.getLogger(__name__)

# Stop words for query token analysis
STOP_WORDS = {
    "what", "is", "the", "a", "an", "how", "to", "in", "of", "and", "for", "with",
    "does", "do", "explain", "describe", "can", "you", "tell", "me", "about", "which",
    "are", "why", "where", "when", "by", "from", "on", "at", "as", "into"
}

# Known SAP technical patterns
SAP_TCODE_REGEX = re.compile(r"\b(/[a-z0-9_]{2,}/[a-z0-9_]+|[a-z]{1,2}[0-9]{2}[a-z0-9]?|[a-z0-9]{4,6})\b", re.IGNORECASE)
SAP_TABLE_REGEX = re.compile(r"\b(dfkk[a-z0-9_]+|fkk[a-z0-9_]+|erdk|erch|but000|but020|mara|marc|vbak|vbap|ekko|ekpo|bseg|bkpf)\b", re.IGNORECASE)
SAP_BRIM_CORE_TERMS = {
    "brim", "som", "cc", "ci", "fi-ca", "fica", "convergent charging", "convergent invoicing",
    "subscription order management", "contract accounts receivable and payable", "billable item",
    "consumption item", "bit", "cit", "rating", "charging", "invoicing", "billing",
    "provider contract", "allowance", "tier", "charge plan", "pricing logic"
}


class EvidenceSelectionService:
    """
    Unified Evidence Selection Service for SAP BRIM Knowledge Assistant.
    
    Responsibilities:
    1. Multi-signal Internal Evidence Confidence Assessment:
       - Cross-encoder / reranking relevance
       - Question coverage & technical identifier matching
       - Component alignment & source authority
       - Consistency and factual sufficiency
       - Decision rule: Triggers Tavily if confidence < INTERNAL_EVIDENCE_THRESHOLD (0.70)
    2. Unified normalization of internal and external candidate passages.
    3. Evaluation of internal-only, external-only, and combined evidence sets.
    4. Selection of the strongest sufficient evidence set passed to answer generation.
    """

    def __init__(self):
        self.internal_threshold = settings.INTERNAL_EVIDENCE_THRESHOLD

    @staticmethod
    def _extract_query_tokens(query: str) -> List[str]:
        raw = re.findall(r"\b[a-z0-9\/\-_]+\b", query.lower())
        return [t for t in raw if t not in STOP_WORDS and len(t) > 1]

    @staticmethod
    def _extract_technical_identifiers(text: str) -> List[str]:
        """Extracts candidate SAP T-codes, table names, and technical identifiers."""
        found = set()
        for m in re.finditer(r"\b(/[a-z0-9_]{2,}/[a-z0-9_]+|[a-z]{1,2}\d{2}[a-z0-9]?)\b", text.lower()):
            found.add(m.group(0))
        for m in SAP_TABLE_REGEX.finditer(text.lower()):
            found.add(m.group(0))
        return list(found)

    def compute_internal_confidence(
        self,
        query: str,
        kb_chunks: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Assesses internal retrieval evidence confidence using a calibrated multi-signal rubric:
        - Signal 1: Top chunk rerank / relevance score (0.35 weight)
        - Signal 2: Question query-term coverage (0.25 weight)
        - Signal 3: SAP Technical identifier match (0.20 weight)
        - Signal 4: Component / BRIM domain alignment (0.10 weight)
        - Signal 5: Evidence consistency across top passages (0.10 weight)
        """
        if not kb_chunks:
            return {
                "confidence": 0.0,
                "should_trigger_tavily": True,
                "signals": {
                    "top_relevance": 0.0,
                    "term_coverage": 0.0,
                    "technical_id_match": 0.0,
                    "domain_alignment": 0.0,
                    "consistency": 0.0
                },
                "reason": "No internal documents retrieved."
            }

        query_tokens = self._extract_query_tokens(query)
        query_tech_ids = self._extract_technical_identifiers(query)

        # 1. Top chunk relevance (top rerank / similarity score)
        scores = [float(c.get("rerank_score", c.get("score", 0.0))) for c in kb_chunks]
        top_relevance = max(scores) if scores else 0.0
        avg_relevance = sum(scores[:3]) / min(len(scores), 3) if scores else 0.0
        relevance_signal = 0.7 * top_relevance + 0.3 * avg_relevance

        # 2. Term coverage across combined top chunks
        combined_text = " ".join((c.get("chunk_text") or "").lower() for c in kb_chunks[:4])
        if query_tokens:
            matched_terms = sum(1 for t in query_tokens if t in combined_text)
            term_coverage = matched_terms / len(query_tokens)
        else:
            term_coverage = 1.0

        # 3. Exact matching of requested SAP technical identifiers
        if query_tech_ids:
            matched_tech = sum(1 for tid in query_tech_ids if tid in combined_text)
            tech_match_signal = matched_tech / len(query_tech_ids)
        else:
            # If query does not specify technical IDs, scale signal with question term coverage
            tech_match_signal = min(1.0, term_coverage)

        # 4. BRIM Component Alignment
        # Check if chunks belong to relevant SAP manuals (BRIM, SOM, CC, CI, FICA, S/4HANA)
        aligned_chunks = 0
        for c in kb_chunks[:4]:
            doc_name = (c.get("document_name") or "").lower()
            section = (c.get("section") or "").lower()
            if any(term in doc_name or term in section for term in ["brim", "som", "cc", "ci", "fica", "fi-ca", "invoicing", "charging", "s4", "s/4", "billing"]):
                aligned_chunks += 1
        domain_alignment_signal = aligned_chunks / min(len(kb_chunks), 4)

        # 5. Distinct Product / Entity Validation
        # If user is asking about distinct cloud products (e.g. Ariba, Concur, SuccessFactors, BTP, Fieldglass)
        # verify that the internal knowledge base chunks actually cover that product
        SAP_DISTINCT_PRODUCTS = {
            "ariba", "concur", "fieldglass", "hybris", "c4c", "qualtrics", "celonis",
            "successfactors", "btp", "signavio", "leanix", "walkme"
        }
        missing_distinct_product = None
        for tok in query_tokens:
            if tok in SAP_DISTINCT_PRODUCTS and tok not in combined_text:
                missing_distinct_product = tok
                break

        # Evidence consistency
        consistency_signal = min(1.0, len(kb_chunks) / 3.0) * (0.8 if (top_relevance - min(scores[:3])) < 0.25 else 0.5)

        # Composite Internal Evidence Confidence
        composite_confidence = (
            0.35 * relevance_signal +
            0.25 * term_coverage +
            0.20 * tech_match_signal +
            0.10 * domain_alignment_signal +
            0.10 * consistency_signal
        )

        # If a requested distinct product is absent from internal corpus, penalize confidence
        if missing_distinct_product:
            composite_confidence = min(composite_confidence * 0.35, 0.40)

        final_confidence = max(0.0, min(0.99, round(composite_confidence, 4)))
        should_trigger = final_confidence < self.internal_threshold

        reason_parts = []
        if missing_distinct_product:
            reason_parts.append(f"Distinct SAP product '{missing_distinct_product}' not found in internal manuals")
        if final_confidence >= self.internal_threshold:
            reason_parts.append(f"Internal confidence {final_confidence:.2f} >= threshold {self.internal_threshold:.2f}")
        else:
            reason_parts.append(f"Internal confidence {final_confidence:.2f} < threshold {self.internal_threshold:.2f}")
            if term_coverage < 0.6:
                reason_parts.append(f"low term coverage ({term_coverage:.2f})")
            if query_tech_ids and tech_match_signal < 1.0:
                reason_parts.append(f"missing technical identifiers ({query_tech_ids})")

        return {
            "confidence": final_confidence,
            "should_trigger_tavily": should_trigger,
            "signals": {
                "top_relevance": round(top_relevance, 4),
                "term_coverage": round(term_coverage, 4),
                "technical_id_match": round(tech_match_signal, 4),
                "domain_alignment": round(domain_alignment_signal, 4),
                "consistency": round(consistency_signal, 4)
            },
            "reason": "; ".join(reason_parts)
        }

    def normalize_internal_chunk(self, chunk: Dict[str, Any]) -> Dict[str, Any]:
        """Normalizes an internal document chunk to the unified evidence representation."""
        chunk_id = chunk.get("chunk_id") or chunk.get("id") or "int_chunk"
        return {
            "source_id": str(chunk_id),
            "source_type": "knowledge_base",
            "title": chunk.get("document_name") or "SAP BRIM Documentation",
            "document_name": chunk.get("document_name") or "SAP BRIM Documentation",
            "page_number": chunk.get("page_number", 1),
            "section": chunk.get("section") or f"Page {chunk.get('page_number', 1)}",
            "url": None,
            "domain": "internal_knowledge_base",
            "chunk_text": chunk.get("chunk_text") or "",
            "authority_tier": 1,  # Verified internal official enterprise manuals
            "relevance_score": float(chunk.get("rerank_score", chunk.get("score", 0.0))),
            "snippet": (chunk.get("chunk_text") or "")[:280] + ("..." if len(chunk.get("chunk_text") or "") > 280 else "")
        }

    def normalize_web_result(self, web_res: Dict[str, Any], index: int) -> Dict[str, Any]:
        """Normalizes an external web result to the unified evidence representation."""
        url = web_res.get("url") or ""
        domain = web_res.get("domain") or "help.sap.com"
        tier = web_res.get("authority_tier", 1 if "help.sap.com" in domain else 2)
        score = float(web_res.get("rerank_score", web_res.get("score", 0.65)))

        return {
            "source_id": f"web_{index}_{domain}",
            "source_type": "web",
            "title": web_res.get("title") or "SAP Official Documentation",
            "document_name": web_res.get("title") or "SAP Official Documentation",
            "page_number": None,
            "section": domain,
            "url": url,
            "domain": domain,
            "chunk_text": web_res.get("chunk_text") or f"{web_res.get('title')}: {web_res.get('snippet')}",
            "authority_tier": tier,
            "relevance_score": score,
            "snippet": (web_res.get("snippet") or "")[:280] + ("..." if len(web_res.get("snippet") or "") > 280 else "")
        }

    def evaluate_evidence_set(
        self,
        query: str,
        passages: List[Dict[str, Any]],
        set_type: str
    ) -> Dict[str, Any]:
        """
        Evaluates a candidate evidence set (internal, external, or combined) using
        a consistent evidence-assessment methodology.
        """
        if not passages:
            return {
                "quality_score": 0.0,
                "coverage": 0.0,
                "sufficiency": False,
                "authority_score": 0.0
            }

        query_tokens = self._extract_query_tokens(query)
        combined_text = " ".join(p["chunk_text"].lower() for p in passages)

        # 1. Relevance: average of top 3 passages weighted by authority tier
        tier_weights = {1: 1.0, 2: 0.85, 3: 0.7}
        weighted_scores = [
            p["relevance_score"] * tier_weights.get(p.get("authority_tier", 1), 0.8)
            for p in passages
        ]
        avg_relevance = sum(weighted_scores[:3]) / min(len(weighted_scores), 3)

        # 2. Coverage of question keywords
        if query_tokens:
            matched_terms = sum(1 for t in query_tokens if t in combined_text)
            coverage = matched_terms / len(query_tokens)
        else:
            coverage = 1.0

        # 3. Authority
        avg_authority = sum(tier_weights.get(p.get("authority_tier", 1), 0.8) for p in passages) / len(passages)

        # Composite quality score
        composite_quality = (0.50 * avg_relevance) + (0.35 * coverage) + (0.15 * avg_authority)
        quality_score = max(0.0, min(1.0, round(composite_quality, 4)))

        # Sufficiency criterion
        sufficiency = (quality_score >= 0.55 and coverage >= 0.60)

        return {
            "quality_score": quality_score,
            "coverage": round(coverage, 4),
            "sufficiency": sufficiency,
            "authority_score": round(avg_authority, 4)
        }

    def select_strongest_evidence(
        self,
        query: str,
        internal_candidates: List[Dict[str, Any]],
        external_candidates: List[Dict[str, Any]],
        internal_confidence_data: Dict[str, Any],
        max_internal_passages: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Selects the strongest sufficient evidence set among:
        A. Internal Knowledge Base
        B. External Authoritative Web
        C. Combined (Internal + External)
        
        Applies consistent evidence-evaluation methodology and preserves provenance.
        """
        normalized_internal = [self.normalize_internal_chunk(c) for c in internal_candidates]
        normalized_external = [self.normalize_web_result(w, idx+1) for idx, w in enumerate(external_candidates)]

        internal_eval = self.evaluate_evidence_set(query, normalized_internal[:6], "internal")
        external_eval = self.evaluate_evidence_set(query, normalized_external[:5], "external")

        # Create combined set (merging top 4 internal with top 3 external, deduplicated)
        combined_passages: List[Dict[str, Any]] = []
        seen_texts = set()

        for p in normalized_internal[:4]:
            t_sub = p["chunk_text"][:80].lower()
            if t_sub not in seen_texts:
                seen_texts.add(t_sub)
                combined_passages.append(p)

        for p in normalized_external[:3]:
            t_sub = p["chunk_text"][:80].lower()
            if t_sub not in seen_texts:
                seen_texts.add(t_sub)
                combined_passages.append(p)

        combined_eval = self.evaluate_evidence_set(query, combined_passages, "combined")

        internal_conf = internal_confidence_data.get("confidence", 0.0)
        ext_triggered = internal_confidence_data.get("should_trigger_tavily", False)

        # For multi-aspect or complex questions with diverse candidate chunks, scale the
        # internal chunk budget to preserve coverage across all aspects without context overflow.
        has_multiple_aspects = len(internal_confidence_data.get("aspects", [])) > 1
        internal_top_k = (
            min(16, max(settings.RERANK_TOP_K, len(internal_confidence_data.get("aspects", [])) * 2, 8))
            if has_multiple_aspects
            else max(settings.RERANK_TOP_K, max_internal_passages or settings.RERANK_TOP_K)
        )

        # Decision Logic:
        # 1. If internal confidence was >= 70% and external was not needed or not triggered:
        if not ext_triggered and internal_eval["sufficiency"]:
            selected_set = normalized_internal[:internal_top_k]
            selected_source_type = "knowledge_base"
            best_quality = internal_eval["quality_score"]
            summary = f"Selected internal SAP documentation (Confidence: {internal_conf:.2f} >= {self.internal_threshold:.2f})."

        # 2. If Tavily was triggered, compare internal, external, and combined sets:
        elif ext_triggered:
            # Check if combining internal + external provides superior coverage and quality
            if (
                combined_passages
                and combined_eval["coverage"] > internal_eval["coverage"]
                and combined_eval["coverage"] > external_eval["coverage"]
                and combined_eval["quality_score"] >= 0.55
                and len(normalized_internal) > 0
                and len(normalized_external) > 0
            ):
                selected_set = combined_passages
                selected_source_type = "combined"
                best_quality = combined_eval["quality_score"]
                summary = (
                    f"Selected combined evidence (Internal + Web). "
                    f"External search triggered because internal confidence was {internal_conf:.2f} < {self.internal_threshold:.2f}."
                )

            # Check if external search alone is strongest
            elif external_eval["quality_score"] >= internal_eval["quality_score"] and external_eval["sufficiency"]:
                selected_set = normalized_external[:5]
                selected_source_type = "web"
                best_quality = external_eval["quality_score"]
                summary = (
                    f"Selected authoritative SAP web documentation (Web Quality: {external_eval['quality_score']:.2f}). "
                    f"Internal confidence was {internal_conf:.2f}."
                )

            # If external search was weak but internal has sufficient evidence
            elif internal_eval["sufficiency"]:
                selected_set = normalized_internal[:internal_top_k]
                selected_source_type = "knowledge_base"
                best_quality = internal_eval["quality_score"]
                summary = f"External search yielded insufficient results; falling back to verified internal evidence (Quality: {best_quality:.2f})."

            else:
                # Both sources insufficient
                selected_set = []
                selected_source_type = "insufficient"
                best_quality = max(internal_eval["quality_score"], external_eval["quality_score"])
                summary = f"Insufficient authoritative evidence in both internal manuals ({internal_conf:.2f}) and approved web sources."

        else:
            # External search disabled or not triggered but internal passed threshold
            selected_set = normalized_internal[:internal_top_k]
            selected_source_type = "knowledge_base"
            best_quality = internal_eval["quality_score"]
            summary = f"Selected internal documentation (Quality: {best_quality:.2f})."

        # Check for contradictions across selected evidence
        conflicts = []
        # Simple heuristic check: if multiple distinct versions are mentioned in opposing statements
        lower_all = " ".join(p["chunk_text"].lower() for p in selected_set)
        if "ecc 6.0" in lower_all and "s/4hana" in lower_all and ("obsolete" in lower_all or "deprecated" in lower_all):
            conflicts.append("Release variance detected between ECC 6.0 and S/4HANA procedures.")

        return {
            "selected_passages": selected_set,
            "source_type": selected_source_type,
            "internal_evidence_confidence": internal_conf,
            "selected_evidence_quality": best_quality,
            "external_search_triggered": ext_triggered,
            "selection_summary": summary,
            "evidence_coverage": combined_eval["coverage"] if selected_source_type == "combined" else (
                external_eval["coverage"] if selected_source_type == "web" else internal_eval["coverage"]
            ),
            "conflicts_detected": conflicts,
            "internal_eval": internal_eval,
            "external_eval": external_eval,
            "combined_eval": combined_eval
        }


evidence_selection_service = EvidenceSelectionService()
