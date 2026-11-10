"""
Answer Style Router: decides how an answer is laid out, never what it may claim.

Evidence, citation and grounding rules stay in llm_service's prompts and grounding_service.
This module only picks the layout: answer type, detail level, table/steps and a headings budget.

Routing is hybrid:
1. Rules decide the obvious cases (explicit format requests, comparisons, procedures,
   troubleshooting, document questions, short factual questions, explanations). They build on
   answer_planner's existing question archetypes instead of re-classifying from scratch.
2. Questions the rules cannot place get one small, cached LLM classification.
3. Anything that fails falls back to a balanced technical explanation.
"""

import json
import logging
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from backend.app.config import settings
from backend.app.services.answer_planner import TROUBLESHOOTING_PATTERN, answer_planner

logger = logging.getLogger(__name__)

AnswerType = Literal[
    "direct_answer", "technical_explanation", "comparison", "step_by_step", "troubleshooting", "document_research",
    "solution_design", "production_incident",
]
DetailLevel = Literal["concise", "balanced", "detailed"]
ANSWER_TYPES = AnswerType.__args__
DETAIL_LEVELS = DetailLevel.__args__

# answer_planner archetypes whose instructions carry domain-specific safety content
# (reconciliation safeguards, block-type scoping, production incidents). Their structure is kept in the prompt.
DOMAIN_STRUCTURED_ARCHETYPES = {"upgrade_invoice_reconciliation", "restriction_block", "production_incident"}


class AnswerPlan(BaseModel):
    """Internal layout plan for one answer. Never shown to end users."""

    answer_type: AnswerType
    detail_level: DetailLevel
    needs_table: bool = False
    needs_steps: bool = False
    needs_citations: bool = True
    source_mode: Literal["internal", "web", "hybrid"] = "internal"
    # Headings budget; 0 means no headings at all. Up to 10 for comprehensive incident answers.
    max_sections: int = Field(default=4, ge=0, le=10)
    # Formatting the user asked for in their own words; overrides the defaults above.
    explicit_requests: List[str] = Field(default_factory=list)
    domain_archetype: str = "conceptual"
    routed_by: Literal["explicit", "rules", "llm", "fallback"] = "rules"


class _LLMStyleChoice(BaseModel):
    """Shape the classifier must return; anything else is rejected."""

    answer_type: AnswerType
    detail_level: DetailLevel = "balanced"
    needs_table: bool = False
    needs_steps: bool = False


# ---------------------------------------------------------------------------
# Rule signals
# ---------------------------------------------------------------------------

_TABLE_REQUEST = re.compile(
    r"\b(?:in|as|into|with|use|using)\s+(?:a\s+|the\s+)?(?:markdown\s+)?table\b|\btabular\b|\btable\s+format\b"
    r"|\bmatrix\b|\btabulate\b",
    re.I,
)
_NO_TABLE = re.compile(r"\b(?:no|without|avoid|don'?t\s+use|do\s+not\s+use)\s+(?:a\s+|any\s+)?tables?\b", re.I)
_BULLETS = re.compile(r"\bbullet(?:s|ed)?(?:\s+points?)?\b|\b(?:as|in)\s+a\s+list\b", re.I)
_STEPS = re.compile(
    r"\bstep[\s-]by[\s-]step\b|\bsteps?\s+(?:to|for)\b|\bhow\s+(?:do|can|should|would)\s+(?:i|we|you|one)\b"
    r"|\bhow\s+to\b|\bwalk\s+me\s+through\b|\b(?:procedure|instructions?)\s+(?:to|for)\b",
    re.I,
)
_COMPARE = re.compile(
    r"\bdiffer(?:s|ence|ences|ent)?\b|\bcompar(?:e|ed|ing|ison)\b|\bversus\b|\bvs\.?\s|\bpros\s+and\s+cons\b"
    r"|\bwhich\s+(?:is|one\s+is)\s+better\b|\bsimilarit(?:y|ies)\b",
    re.I,
)
_DOC_REFERENCE = re.compile(
    r"\b(?:this|the|attached|uploaded|my)\s+(?:document|doc|file|pdf|spec(?:ification)?|attachment|screenshot)\b"
    r"|\baccording\s+to\s+the\b|\bin\s+the\s+(?:attached|uploaded)\b",
    re.I,
)
_FACTUAL_START = re.compile(
    r"^\s*(?:what\s+(?:is|are|does|do)\b|what'?s\b|which\b|when\b|who\b|where\b|is\s+(?:there|it)\b"
    r"|are\s+there\b|can\s+(?:i|we|you)\b|does\b|define\b)",
    re.I,
)
_EXPLAIN = re.compile(
    r"\b(?:explain|describe|elaborate|why|overview|architecture|lifecycle|end[\s-]to[\s-]end|works?|working"
    r"|role\s+of|mechanism|in\s+depth|deep[\s-]dive)\b|\bhow\s+(?:does|do|is|are|did)\b(?!\s+(?:i|we|you|one)\b)",
    re.I,
)
_CONCISE = re.compile(
    r"\b(?:brief(?:ly)?|short(?:ly)?|concise(?:ly)?|summar(?:y|ise|ize)|one[\s-]liner|tl;?dr|quick(?:ly)?"
    r"|in\s+(?:one|a\s+few|two|three|2|3)\s+(?:lines?|sentences?|words?|points?))\b",
    re.I,
)
_DETAILED = re.compile(
    r"\b(?:in\s+detail|detailed|deep[\s-]dive|comprehensive(?:ly)?|thorough(?:ly)?|elaborate|exhaustive"
    r"|in[\s-]depth|everything\s+about)\b",
    re.I,
)
# Design, interview and experience framing: the user wants a senior consultant's solution, not one fact.
_SCENARIO_FRAMING = re.compile(
    r"\b(?:design|architect)(?:\s+and\s+(?:implement|build|deliver))?\s+(?:an?\s+|the\s+)?(?:end[\s-]to[\s-]end|complete|overall)\b"
    r"|\bend[\s-]to[\s-]end\s+(?:sap\s+)?(?:brim\s+)?(?:solution|implementation|design|architecture|landscape)\b"
    r"|\b(?:interview|candidate)\b|\b\d+\s*\+?\s*(?:years?|yrs?)\b[^.?\n]{0,20}\bexp(?:erience|erienced)?\b"
    r"|\bin\s+your\s+(?:experience|project|last\s+project)\b|\breal[\s-]time\s+scenario\b|\bscenario[\s-]based\b",
    re.I,
)
_HOW_WOULD_YOU = re.compile(
    r"\bhow\s+(?:would|will|should)\s+you\s+(?:design|implement|architect|approach|handle|build|plan|structure"
    r"|set\s+up|support|deliver|run|manage)\b",
    re.I,
)
_BRIM_COMPONENTS = {
    "SOM": re.compile(r"\b(?:subscription\s+order\s+management|som)\b", re.I),
    "CC": re.compile(r"\b(?:convergent\s+charging|cc)\b", re.I),
    "CI": re.compile(r"\b(?:convergent\s+invoicing|ci)\b", re.I),
    "FI-CA": re.compile(r"\b(?:fi[- ]?ca|contract\s+accounts?\s+receivable\s+and\s+payable)\b", re.I),
}
_COMPONENT_HANDOFF = re.compile(
    r"\b(?:post|transfer|send|move|flow|integrat\w*|consolidat\w*|map|bill|process|create|generate)\b",
    re.I,
)


def is_cross_component_handoff(question: str) -> bool:
    """Recognize a process question that explicitly crosses two or more BRIM components."""
    text = question or ""
    components = sum(bool(pattern.search(text)) for pattern in _BRIM_COMPONENTS.values())
    return components >= 2 and bool(_COMPONENT_HANDOFF.search(text))


def is_solution_design_question(text: str) -> bool:
    """Broad design/scenario questions. A plain "how would you" about a specific error stays troubleshooting."""
    text = text or ""
    if _SCENARIO_FRAMING.search(text):
        return True
    return bool(_HOW_WOULD_YOU.search(text)) and not TROUBLESHOOTING_PATTERN.search(text)

# Successful LLM classifications by normalized question; failures are not cached.
_classifier_cache: Dict[str, _LLMStyleChoice] = {}
_CLASSIFIER_CACHE_MAX = 256

_CLASSIFIER_PROMPT = """Decide how an answer to this SAP question should be laid out. Do not answer it.
Reply with one JSON object only, no prose:
{{"answer_type": "...", "detail_level": "...", "needs_table": false, "needs_steps": false}}

answer_type, choose one:
- direct_answer: a single fact, definition or yes/no
- technical_explanation: how or why something works, concepts, architecture
- comparison: contrasting two or more things
- step_by_step: a procedure the user wants to carry out
- troubleshooting: an error, failure or unexpected behaviour
- document_research: questions about a specific supplied document
- solution_design: designing, implementing or operating a whole solution, or a scenario/interview question
detail_level: concise, balanced or detailed.
needs_table: true only for comparisons or genuinely tabular data.
needs_steps: true only when the answer is an ordered procedure or sequence.

Question: {question}"""


class AnswerStyleRouter:

    def route(
        self,
        question: str,
        evidence: Optional[List[Dict[str, Any]]] = None,
        raw_question: Optional[str] = None,
    ) -> AnswerPlan:
        """
        question: the (possibly follow-up-resolved) question the answer is generated for.
        raw_question: what the user literally typed; formatting instructions are read from both.
        """
        try:
            return self._route(question or "", evidence or [], raw_question)
        except Exception as exc:  # routing must never break answer generation
            logger.warning("Answer style routing failed (%s); using safe fallback.", exc)
            return self.fallback_plan(evidence or [])

    def _route(self, question: str, evidence: List[Dict[str, Any]], raw_question: Optional[str]) -> AnswerPlan:
        text = question if not raw_question or raw_question.strip() == question.strip() else f"{raw_question}\n{question}"
        archetype = answer_planner.plan_answer(question)["question_type"]
        if archetype in {"conceptual", "tcode_table", "version_specific", "followup"} and is_cross_component_handoff(question):
            archetype = "integration"
        has_attachment = any(e.get("source_type") == "attached_document" for e in evidence)
        explicit = self._explicit_requests(text)

        answer_type, routed_by, extra = self._classify(question, text, archetype, has_attachment, explicit)
        detail = self._detail_level(question, answer_type, archetype, explicit, extra.get("detail_level"))

        needs_table = (
            answer_type == "comparison"
            or archetype == "upgrade_invoice_reconciliation"
            or bool(extra.get("needs_table"))
        )
        if "table" in explicit:
            needs_table = True
        if "no_table" in explicit:
            needs_table = False

        needs_steps = (
            answer_type in ("step_by_step", "troubleshooting", "solution_design", "production_incident")
            or archetype == "integration"
            or "steps" in explicit
            or bool(extra.get("needs_steps"))
        )

        return AnswerPlan(
            answer_type=answer_type,
            detail_level=detail,
            needs_table=needs_table,
            needs_steps=needs_steps,
            needs_citations=self._has_citable_evidence(evidence),
            source_mode=self._source_mode(evidence),
            max_sections=self._max_sections(answer_type, detail, archetype),
            explicit_requests=explicit,
            domain_archetype=archetype,
            routed_by=routed_by,
        )

    # -- classification ------------------------------------------------------

    def _classify(self, question: str, text: str, archetype: str, has_attachment: bool, explicit: List[str]):
        """Returns (answer_type, routed_by, extra hints from the LLM classifier)."""
        if "steps" in explicit and archetype not in ("troubleshooting", "upgrade_invoice_reconciliation", "production_incident"):
            return "step_by_step", "explicit", {}

        # Checked before the archetypes: a design question naming "FI-CA integration" is not an
        # integration explanation. Archetypes with domain safety structure keep precedence.
        if archetype not in DOMAIN_STRUCTURED_ARCHETYPES and is_solution_design_question(text):
            return "solution_design", "rules", {}

        # Archetypes answer_planner already identifies reliably.
        mapped = {
            "production_incident": "production_incident",
            "upgrade_invoice_reconciliation": "troubleshooting",
            "restriction_block": "technical_explanation",
            "comparison": "comparison",
            "configuration": "step_by_step",
            "troubleshooting": "troubleshooting",
            "integration": "technical_explanation",
        }.get(archetype)
        if mapped:
            return mapped, "rules", {}

        # Remaining archetypes (conceptual, tcode_table, version_specific, followup) are open.
        # The planner checks identifiers before errors, so "FKKBIX error ... how do I fix it" lands here.
        if TROUBLESHOOTING_PATTERN.search(question):
            return "troubleshooting", "rules", {}
        if _COMPARE.search(question):
            return "comparison", "rules", {}
        if _STEPS.search(question):
            return "step_by_step", "rules", {}
        if has_attachment or _DOC_REFERENCE.search(text):
            return "document_research", "rules", {}
        if self._is_short_factual(question):
            return "direct_answer", "rules", {}
        if _EXPLAIN.search(question):
            return "technical_explanation", "rules", {}

        choice = self._llm_classify(question)
        if choice:
            return choice.answer_type, "llm", choice.model_dump()
        return self._default_type(), "fallback", {}

    @staticmethod
    def _is_short_factual(question: str) -> bool:
        return (
            bool(_FACTUAL_START.search(question))
            and len(question.split()) <= 14
            and question.count("?") <= 1
            and not _EXPLAIN.search(question)
        )

    def _llm_classify(self, question: str) -> Optional[_LLMStyleChoice]:
        if not getattr(settings, "ANSWER_STYLE_LLM_CLASSIFIER_ENABLED", True):
            return None
        key = " ".join(question.lower().split())
        if key in _classifier_cache:
            return _classifier_cache[key]

        from backend.app.services.llm_service import llm_service  # avoid an import cycle

        try:
            raw = llm_service.classify_answer_style(_CLASSIFIER_PROMPT.format(question=question[:1500]))
        except Exception as exc:
            logger.info("Answer style classifier call failed: %s", exc)
            return None
        choice = self.parse_classifier_output(raw)
        if choice:
            if len(_classifier_cache) >= _CLASSIFIER_CACHE_MAX:
                _classifier_cache.pop(next(iter(_classifier_cache)))
            _classifier_cache[key] = choice
        return choice

    @staticmethod
    def parse_classifier_output(raw: Optional[str]) -> Optional[_LLMStyleChoice]:
        """Validate the classifier's JSON; None for anything malformed or out of range."""
        if not raw:
            return None
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return None
        try:
            return _LLMStyleChoice.model_validate(json.loads(match.group(0)))
        except (ValueError, ValidationError):
            return None

    # -- plan attributes -----------------------------------------------------

    @staticmethod
    def _explicit_requests(text: str) -> List[str]:
        found = []
        if _NO_TABLE.search(text):
            found.append("no_table")
        elif _TABLE_REQUEST.search(text):
            found.append("table")
        if _BULLETS.search(text):
            found.append("bullets")
        if re.search(r"\bstep[\s-]by[\s-]step\b", text, re.I):
            found.append("steps")
        if _CONCISE.search(text):
            found.append("concise")
        elif _DETAILED.search(text):
            found.append("detailed")
        return found

    @staticmethod
    def _detail_level(question: str, answer_type: str, archetype: str, explicit: List[str], hinted: Optional[str]) -> str:
        if "concise" in explicit:
            return "concise"
        if "detailed" in explicit:
            return "detailed"
        if archetype in ("upgrade_invoice_reconciliation", "production_incident", "integration") or answer_type in ("solution_design", "production_incident"):
            return "detailed"
        if answer_type == "direct_answer":
            return "concise"
        if question.count("?") >= 2 or len(question.split()) >= 45:
            return "detailed"  # multi-part questions need room for every part
        if hinted in DETAIL_LEVELS:
            return hinted
        default = getattr(settings, "ANSWER_STYLE_DEFAULT_DETAIL", "balanced")
        return default if default in DETAIL_LEVELS else "balanced"

    @staticmethod
    def _max_sections(answer_type: str, detail: str, archetype: str) -> int:
        if archetype == "production_incident" or answer_type == "production_incident":
            return 10
        if archetype == "upgrade_invoice_reconciliation":
            return 7  # the planner mandates seven sections for this diagnostic
        if answer_type == "direct_answer" or detail == "concise":
            return 0
        if answer_type == "solution_design":
            return 8  # architecture, integration, playbook, monitoring, performance, recovery
        budget = max(1, min(10, int(getattr(settings, "ANSWER_STYLE_MAX_SECTIONS", 4))))
        return min(10, budget + 2) if detail == "detailed" else budget

    @staticmethod
    def _has_citable_evidence(evidence: List[Dict[str, Any]]) -> bool:
        return any(
            e.get("source_type") != "retrieval_gap" and not e.get("retrieval_gap")
            and (e.get("chunk_text") or e.get("snippet"))
            for e in evidence
        )

    @staticmethod
    def _source_mode(evidence: List[Dict[str, Any]]) -> str:
        kinds = {
            "web" if (e.get("source_type") == "web" or e.get("url")) else "internal"
            for e in evidence
            if e.get("source_type") != "retrieval_gap" and not e.get("retrieval_gap")
        }
        if kinds == {"web"}:
            return "web"
        return "hybrid" if kinds == {"web", "internal"} else "internal"

    @staticmethod
    def _default_type() -> str:
        configured = getattr(settings, "ANSWER_STYLE_DEFAULT", "technical_explanation")
        return configured if configured in ANSWER_TYPES else "technical_explanation"

    def fallback_plan(self, evidence: Optional[List[Dict[str, Any]]] = None) -> AnswerPlan:
        evidence = evidence or []
        return AnswerPlan(
            answer_type="technical_explanation",
            detail_level="balanced",
            needs_citations=self._has_citable_evidence(evidence),
            source_mode=self._source_mode(evidence),
            max_sections=self._max_sections("technical_explanation", "balanced", "conceptual"),
            routed_by="fallback",
        )


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

FORMATTING_RULES = """FORMATTING RULES:
- Answer the user's actual question in the first sentence or two. No introduction that restates the question.
- Structure every answer that is longer than a few sentences: a bolded one-sentence answer first, then short sections each under its own "## " Markdown heading, with bullets for related items, numbered lists for ordered procedures, and **bold** for key terms, identifiers and T-codes. Keep paragraphs to three sentences or fewer; never return one unbroken block of text.
- Never write HTML tags such as <br> in an answer. In a table cell keep one short phrase; put longer lists in bullets below the table.
- Do not default to Markdown tables. Use a table only for a real comparison or genuinely tabular data, with short cells (no paragraphs inside cells).
- Give each heading a short, specific title. Never repeat a heading's content in another section.
- State each fact once. Do not add a summary or conclusion that repeats what was already said, and no closing "Key points", "Summary" or "Overview" section that restates earlier content.
- Keep important technical details, identifiers and source terminology; never drop critical evidence just to be shorter.
- Formatting never relaxes the evidence rules: cite with [E#] markers exactly as instructed, and do not invent facts, references, URLs or citations.
- Use only the individual evidence IDs shown, such as [E2] or [E1][E3]; never ranges like [E1-E5] or labels like [All evidence].
- State uncertainty and conflicting evidence plainly."""

_LAYOUTS = {
    "direct_answer": (
        "Give the answer in the first sentence. Add one or two sentences of supporting context only if they help. "
        "No headings, no table, no closing summary."
    ),
    "technical_explanation": (
        "Open with a bolded one- or two-sentence direct answer. Then use \"## \" sections such as the key components, "
        "how it works (a numbered list when it is a flow) and the relevant technical details, each as short bullets or "
        "paragraphs. Add a final \"## Limitations\" section only where the evidence states caveats."
    ),
    "comparison": (
        "Start with one or two sentences naming the main distinction. Then, if it genuinely helps, one comparison "
        "table with short cells. Follow with the key differences the table cannot show and a short practical "
        "conclusion grounded in the evidence. Do not restate the table in prose."
    ),
    "step_by_step": (
        "List prerequisites only if the evidence documents any. Then give the steps as a numbered list in order, "
        "with code or commands in fenced code blocks where needed. Finish with how to verify the result, and common "
        "issues only if the evidence covers them. Every step must come from a cited passage; if the evidence does "
        "not document the procedure, say so instead of writing steps."
    ),
    "troubleshooting": (
        "Lead with the likely cause, labelled \"Confirmed\" only when a cited passage establishes it and "
        "\"Suspected\" otherwise. Then the supporting evidence, numbered diagnostic steps, the fix or workaround, and "
        "how to verify it. Never present a suspected cause as confirmed."
    ),
    "document_research": (
        "Answer from the document passages, keeping the document's own terminology, and cite them. Say clearly what "
        "the document does not cover. Use a table only for genuinely tabular content (records, field lists) or if "
        "the user asked for one."
    ),
    "solution_design": (
        "This is a solution-design or scenario question of the kind asked of experienced consultants. Answer every "
        "part the user listed, each from the evidence retrieved for it, in this order where it applies:\n"
        "1. Architecture: the components, the business object each one owns, and how they hand data to each other "
        "(the integration mechanisms). State where a component runs and which product versions apply only as a "
        "cited passage states them.\n"
        "2. How the listed requirements are met (for example pricing, subscription lifecycle, high-volume usage).\n"
        "3. If the question mentions failures, issues, discrepancies, bottlenecks or production support: a "
        "troubleshooting playbook with one short entry per failure type the question names or implies (such as "
        "rating failures, duplicate usage events, failed billing or invoicing runs, reconciliation mismatches, "
        "FI-CA posting errors). Each entry gives the symptom, where to look (monitors, logs, statuses), likely causes, "
        "the resolution and how to reprocess safely, and prevention. Call a cause confirmed only when a cited "
        "passage establishes it.\n"
        "4. Operations: monitoring and alerting, error reprocessing, performance tuning and recovery, where asked.\n"
        "Prefer the concrete checks, monitors, logs, statuses and decisions the evidence names over generic statements. "
        "If the question is framed as an interview or about experience, write a model answer in the first person "
        "(\"When a billing run fails, I first ...\") that shows how the consultant diagnoses and resolves problems; "
        "never invent client names, project figures, volumes or outcomes. Name the parts the evidence does not "
        "cover once, briefly, at the end instead of repeating \"not covered\" in every section."
    ),
    "production_incident": (
        "This is a production incident / complex scenario question requiring senior consultant analysis. "
        "Structure your response using the 10 required sections where applicable:\n"
        "1. Executive Summary: Core assessment and recommended approach; what is known vs uncertain.\n"
        "2. Problem Analysis: Break into individual symptoms and affected components independently.\n"
        "3. Evidence and Findings: Present technical findings from retrieved evidence with bracketed citation markers.\n"
        "4. Root-Cause Analysis: Plausible causes and evidence needed; never present unverified cause as confirmed.\n"
        "5. Step-by-Step Investigation: Sequence of logs, status monitors, reconciliation checks, and interfaces.\n"
        "6. Corrective and Recovery Actions: Safe actions per failure category, prerequisites, avoid destructive actions.\n"
        "7. Duplicate Prevention and Financial Integrity: Verify status before retry, distinguish missing vs delayed vs failed vs duplicate.\n"
        "8. Validation and Reconciliation: Confirm resolution with counts, totals, and financial reconciliation.\n"
        "9. Risks and Limitations: Unresolved questions, operational risks, and documentation gaps.\n"
        "10. Final Recommendations: Immediate prioritized next steps and targeted follow-up questions.\n"
        "Aim for approximately 1,200–2,000 words when the scenario and evidence justify that level of detail."
    ),
}

_EXPLICIT_LABELS = {
    "table": "a table",
    "no_table": "no tables",
    "bullets": "bullet points",
    "steps": "step-by-step instructions",
    "concise": "a brief answer",
    "detailed": "a detailed answer",
}


def render_style_instructions(plan: AnswerPlan) -> str:
    """Formatting directive for the generation prompt. Internal; the model must not echo it."""
    lines = [
        "ANSWER FORMAT (internal guidance; never mention it or reproduce it in the answer):",
        f"Answer type: {plan.answer_type.replace('_', ' ')}. Detail level: {plan.detail_level}.",
        _LAYOUTS[plan.answer_type],
    ]

    if plan.max_sections == 0:
        lines.append("Do not use headings.")
    else:
        lines.append(
            f"Use \"## \" Markdown headings to separate the distinct parts, at most {plan.max_sections}; "
            "a very short answer may use none."
        )

    if plan.detail_level == "concise":
        lines.append("Keep it concise, clear, and direct with an example when useful (typically 100-250 words).")
    elif plan.detail_level == "detailed":
        lines.append("Be comprehensive and thorough on every part the evidence supports, providing senior-level technical depth without unnecessary padding or repetition.")
    else:
        lines.append("Cover what the question needs with detailed explanation, component interactions, and practical examples, without background it did not ask for.")

    if plan.needs_table:
        lines.append("A table is appropriate here; keep cells short.")
    else:
        lines.append("Do not use a Markdown table unless the evidence contains genuinely tabular data.")

    if plan.needs_steps and plan.answer_type not in ("step_by_step", "troubleshooting", "solution_design", "production_incident"):
        lines.append("Present any ordered sequence or process flow as a numbered list.")

    if not plan.needs_citations:
        lines.append("No citable evidence was retrieved: cite nothing, and say plainly what could not be established.")
    elif plan.source_mode == "hybrid":
        lines.append(
            "Evidence comes from internal documents and from web sources. Make clear which statements rest on web "
            "sources (for example \"SAP Help Portal states ... [E3]\") so internal and external evidence stay distinguishable."
        )
    elif plan.source_mode == "web":
        lines.append("All evidence comes from web sources, not internal documents; do not present it as internal documentation.")

    if plan.explicit_requests:
        wanted = ", ".join(_EXPLICIT_LABELS[r] for r in plan.explicit_requests if r in _EXPLICIT_LABELS)
        lines.append(
            f"The user explicitly asked for {wanted}. Follow that over the defaults above "
            "(but never over the evidence and citation rules)."
        )

    return "\n".join(lines) + "\n\n" + FORMATTING_RULES


def domain_structure(plan: AnswerPlan, question: str) -> str:
    """answer_planner's instructions for archetypes whose structure carries domain safety content."""
    if plan.domain_archetype not in DOMAIN_STRUCTURED_ARCHETYPES:
        return ""
    return answer_planner.plan_answer(question).get("instructions", "")


answer_style_router = AnswerStyleRouter()
