"""
Query Router Service for Clyptus.AI SAP BRIM Assistant.

Classifies incoming user messages into dedicated execution paths:
1. CONVERSATION_MEMORY: Explicitly refers to previous messages or chats.
2. MIXED_QUERY: Mixed emotional/sentimental and technical questions.
3. EMOTIONAL_CASUAL: Greetings, sentimental, gratitude, and casual conversational messages -> Direct LLM.
4. SIMPLE_SAP: Simple conceptual SAP definitions and overviews -> Direct LLM.
5. COMPLEX_SAP: Deep technical, troubleshooting, SPRO, integration flow, or evidence-dependent questions -> Full RAG.
6. OUT_OF_DOMAIN: Questions unrelated to SAP or conversation/emotion -> Refusal.

Guarantees:
- Zero vector retrieval, zero web search, and zero reranking executed before path decision.
- Direct LLM paths bypass the retrieval pipeline entirely for speed and efficiency.
- Preserves 100% of existing RAG pipeline and conversation-memory functionality.
"""

import logging
import re
from enum import Enum
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field

from backend.app.services.sap_classifier import (
    SAPClassifier,
    SAP_KEYWORDS,
    SAP_CORE_MODULES,
    SENTIMENTAL_AND_GREETING_PATTERNS,
    GENERAL_UNRELATED_PATTERNS,
)
from backend.app.services.conversation_memory_service import (
    conversation_memory_service,
    MemoryIntent,
)

logger = logging.getLogger(__name__)


class QueryRoute(str, Enum):
    EMOTIONAL_CASUAL = "EMOTIONAL_CASUAL"
    SIMPLE_SAP = "SIMPLE_SAP"
    COMPLEX_SAP = "COMPLEX_SAP"
    CONVERSATION_MEMORY = "CONVERSATION_MEMORY"
    MIXED_QUERY = "MIXED_QUERY"
    OUT_OF_DOMAIN = "OUT_OF_DOMAIN"
    # Set only by the LLM intent analyzer (see intent_analyzer.py), never by the rule-based router.
    GENERAL = "GENERAL"  # a question that is not about SAP: answered directly by the LLM
    CLARIFICATION = "CLARIFICATION"  # genuinely ambiguous: ask the user one question


@dataclass
class QueryRoutingDecision:
    route: QueryRoute
    reason: str
    cleaned_query: str
    requires_retrieval: bool
    emotional_part: Optional[str] = None
    technical_part: Optional[str] = None
    technical_route: Optional[QueryRoute] = None
    # Files the user asked for (docx, pdf, ...): the answer is written as a document and delivered as a download.
    output_formats: List[str] = field(default_factory=list)


class QueryRouter:
    """
    Intelligent Query Router determining the processing path for all user messages.
    """

    # Emotional and Sentimental Patterns
    EMOTIONAL_PATTERNS = [
        r"\b(i('?m| am)\s+(feeling\s+)?(sad|depressed|stressed|lonely|down|upset|anxious|tired|overwhelmed|worried|nervous|exhausted))\b",
        r"\b(i('?m| am)\s+(so\s+)?frustrated(\s+with|\s+at|\s+about|\s+because|\s+that)?)\b",
        r"\b(feeling\s+(sad|stressed|lonely|anxious|nervous|down|blue|frustrated|overwhelmed))\b",
        r"\b(had a (bad|rough|hard|terrible) day|rough day|bad day)\b",
        r"\b(nobody understands me|no one understands me|feeling alone|feel so alone)\b",
        r"\b(can i talk to you|i need someone to (talk to|listen)|listen to me)\b",
        r"\b(worried about my (interview|exam|job|test|work)|nervous about my (interview|exam|job|test|work))\b",
        r"\b(stressed about|anxious about)\b",
    ]

    # Casual Greeting and Chat Patterns
    CASUAL_GREETING_PATTERNS = [
        r"^(hi+|hey+|hello+|howdy|hola|namaste|greetings)\b",
        r"^(good\s+(morning|afternoon|evening|night|day))\b",
        r"\b(how\s+are\s+you|how\s+r\s+u|how're\s+you|how\s+is\s+it\s+going|how\s+do\s+you\s+feel)\b",
        r"\b(what\s+are\s+you\s+doing|what're\s+you\s+doing|what\s+are\s+u\s+doing|what\s+r\s+u\s+doing)\b",
        r"\b(who\s+are\s+you|what\s+is\s+your\s+name|what's\s+your\s+name|introduce\s+yourself)\b",
        r"\b(what\s+can\s+you\s+do|what\s+do\s+you\s+do|tell\s+me\s+about\s+yourself|how\s+can\s+you\s+help\s+me)\b",
        r"\b(thank\s+you|thanks|thx|thank\s+u)\b",
        r"\b(bye|goodbye|see\s+you|cya|have a (good|great|nice) day)\b",
        r"^(ok|okay|cool|nice|great|awesome|fine|alright|sure)\b",
        r"\b(tell\s+me\s+a\s+joke|make\s+me\s+laugh|sing\s+a\s+song|write\s+a\s+poem)\b",
        r"\b(are\s+you\s+(happy|sad|real|human|ai|bot))\b",
    ]

    # Complex Technical Indicators (Requires RAG)
    COMPLEX_TECHNICAL_PATTERNS = [
        # Integration flows
        r"\b(integration\s+flow|som-to-cc|som-to-ci|som-to-cc-to-ci-to-fi-ca|cc-to-ci|ci-to-fi-ca|end-to-end\s+flow|data\s+flow|complete\s+flow)\b",
        # Troubleshooting, missing items, issues, dumps
        r"\b(why\s+(is|are)|missing\b|troubleshoot|troubleshooting|error|errors|failed|failing|failure|dump|st22|slg1|sm21|incorrect|corrupt|reconcil(e|iation))\b",
        # SPRO Customizing & Configuration paths
        r"\b(spro|customizing|configuration\s+path|how\s+do\s+i\s+configure|how\s+to\s+configure|configure\s+the|implementation\s+steps|implementation\s+guide|step-by-step|procedure|step\s+by\s+step)\b",
        # Technical artifacts (tables, tcodes, BAPIs, BADIs)
        r"\b(transaction\s+codes?|database\s+tables?|tcode|t-code|bapi|badi|idoc|user\s+exit|acdoca|dfkkop|fpy1|fpy2|fkkbi|fpcpl)\b",
        # Incidents & scenario questions
        r"\b(production\s+incident|production\s+issue|incident|scenario(-based)?\s+questions?|interview\s+questions?|advanced\s+scenario)\b",
        # In-depth or comparison requests.  Comparisons need product/version-aware
        # evidence and must not be answered from the direct-definition path.
        r"\b(compare|comparison|difference\s+between|multiple\s+documents|in\s+detail|detailed|explain\s+in\s+detail|deep\s+dive|comprehensive)\b",
    ]

    # Simple Conceptual SAP Question Patterns
    SIMPLE_SAP_PATTERNS = [
        # Definitional
        r"^(what\s+is|what's|what\s+are|define|meaning\s+of|overview\s+of)\s+([a-z0-9\/\-\s]+)$",
        # Acronym expansion
        r"^(what\s+does\s+.+\s+stand\s+for|what\s+is\s+the\s+full\s+form\s+of|full\s+form\s+of)\b",
        # Purpose / Components
        r"^(what\s+is\s+the\s+(basic\s+)?purpose\s+of|purpose\s+of)\s+",
        r"^(what\s+are\s+the\s+main\s+components\s+of|main\s+components\s+of|components\s+of)\s+",
        # Simple explanation
        r"^(explain\s+.+\s+in\s+simple\s+(words|terms)|explain\s+.+\s+simply)\b",
    ]

    @classmethod
    def _has_emotional_signal(cls, text: str) -> Tuple[bool, Optional[str]]:
        """Detects whether text contains emotional or sentimental expressions."""
        for pattern in cls.EMOTIONAL_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return True, match.group(0)
        return False, None

    @classmethod
    def _has_casual_greeting_signal(cls, text: str) -> bool:
        """Detects greetings, casual chats, gratitude, and acknowledgments."""
        for pattern in cls.CASUAL_GREETING_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False

    @classmethod
    def _is_sap_related(cls, text: str) -> bool:
        """Checks if text contains SAP domain keywords, core modules, or enterprise configuration/troubleshooting patterns."""
        t = text.lower()
        if SAPClassifier._contains_term(t, "sap"):
            return True
        if any(SAPClassifier._contains_term(t, mod) for mod in SAP_CORE_MODULES):
            return True
        if any(SAPClassifier._contains_term(t, kw) for kw in SAP_KEYWORDS if kw != "sap"):
            return True
        if cls._is_complex_technical(t):
            return True
        return False

    @classmethod
    def _is_complex_technical(cls, text: str) -> bool:
        """Checks if SAP query matches complex/troubleshooting/implementation patterns."""
        t = text.lower()
        for pattern in cls.COMPLEX_TECHNICAL_PATTERNS:
            if re.search(pattern, t, re.IGNORECASE):
                return True
        return False

    @classmethod
    def _is_simple_sap(cls, text: str) -> bool:
        """
        Determines whether an SAP query is a simple conceptual, definitional, or overview question.
        Examples: 'What is SAP?', 'What is SAP BRIM?', 'What does FI-CA stand for?'
        """
        t = text.lower().strip().rstrip("?.!")
        # If it has complex markers, it's not simple
        if cls._is_complex_technical(t):
            return False

        # Match simple conceptual patterns
        for pattern in cls.SIMPLE_SAP_PATTERNS:
            if re.search(pattern, t, re.IGNORECASE):
                return True

        # Short bare concepts (e.g. "SAP BRIM", "FI-CA", "SOM", "Convergent Charging")
        words = t.split()
        if len(words) <= 4 and cls._is_sap_related(t):
            return True

        return False

    @classmethod
    def _is_conversation_memory(cls, query: str, chat_history: Optional[List[Dict[str, Any]]] = None) -> bool:
        """Checks if the query explicitly asks about past messages or previous chat history."""
        intent = conversation_memory_service.classify_intent(query, chat_history=chat_history)
        if intent in (
            MemoryIntent.FIRST_MESSAGE_LOOKUP,
            MemoryIntent.LAST_MESSAGE_LOOKUP,
            MemoryIntent.CROSS_CONVERSATION_RECALL,
            MemoryIntent.HISTORY_RECALL,
        ):
            return True
        return False

    @classmethod
    def _strip_leading_greetings(cls, query: str) -> str:
        """Strips leading greeting tokens (e.g. 'Hey, ', 'Hello! ') to inspect the underlying question."""
        cleaned = re.sub(r"^(hi+|hey+|hello+|good\s+(morning|afternoon|evening))\s*[,!.:-]*\s*", "", query.strip(), flags=re.IGNORECASE)
        return cleaned.strip()

    @classmethod
    def get_acknowledgment(cls, emotional_part: Optional[str]) -> str:
        """Returns a brief, natural acknowledgment for mixed queries."""
        p = (emotional_part or "").lower()
        if any(w in p for w in ["frustrat"]):
            return "I hear your frustration."
        if any(w in p for w in ["worried", "nervous"]):
            return "I understand your concern."
        if any(w in p for w in ["stress", "stressed", "overwhelm"]):
            return "I'm sorry today has been stressful."
        if any(w in p for w in ["sad", "down", "upset"]):
            return "I'm sorry you're having a tough time."
        if any(w in p for w in ["tired", "exhaust"]):
            return "Hang in there."
        return "I understand how you feel."

    @classmethod
    def _extract_technical_part(cls, raw_query: str, emotional_signal: Optional[str]) -> str:
        """Extracts the technical inquiry portion from a mixed query."""
        if not emotional_signal:
            return raw_query
        # Remove emotional signal if at start or end
        cleaned = re.sub(re.escape(emotional_signal), "", raw_query, flags=re.IGNORECASE)
        # Strip leading punctuation/conjunctions like ";", ",", "because", "and", "so", "-"
        cleaned = re.sub(r"^[\s,;:\-–—]+", "", cleaned)
        cleaned = re.sub(r"^(because|and|so|but|yet)\s+", "", cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.strip()
        return cleaned if len(cleaned) > 2 else raw_query

    def route(
        self,
        query: str,
        chat_history: Optional[List[Dict[str, Any]]] = None
    ) -> QueryRoutingDecision:
        """
        Classifies user query and determines the optimal processing path.

        Priority order:
        1. CONVERSATION_MEMORY
        2. MIXED_QUERY
        3. EMOTIONAL_CASUAL
        4. SIMPLE_SAP
        5. COMPLEX_SAP
        6. OUT_OF_DOMAIN
        """
        raw_query = (query or "").strip()
        if not raw_query:
            return QueryRoutingDecision(
                route=QueryRoute.EMOTIONAL_CASUAL,
                reason="Empty query",
                cleaned_query="",
                requires_retrieval=False
            )

        # -------------------------------------------------------------
        # 1. PRIORITY: CONVERSATION_MEMORY
        # -------------------------------------------------------------
        if self._is_conversation_memory(raw_query, chat_history=chat_history):
            return QueryRoutingDecision(
                route=QueryRoute.CONVERSATION_MEMORY,
                reason="Explicit historical memory recall query",
                cleaned_query=raw_query,
                requires_retrieval=False  # Memory lookup handles its own targeted queries
            )

        # -------------------------------------------------------------
        # Strip superficial greetings to check underlying intent
        # (e.g. 'Hey, what is SAP BRIM?' -> underlying 'what is SAP BRIM?')
        # -------------------------------------------------------------
        stripped_query = self._strip_leading_greetings(raw_query)
        has_emotion, emotional_signal = self._has_emotional_signal(raw_query)
        is_sap_raw = self._is_sap_related(raw_query)
        is_sap_stripped = self._is_sap_related(stripped_query) if stripped_query else False
        is_sap = is_sap_raw or is_sap_stripped

        # -------------------------------------------------------------
        # 2. PRIORITY: MIXED_QUERY (Emotional + Technical SAP)
        # -------------------------------------------------------------
        if has_emotion and is_sap:
            tech_part = self._extract_technical_part(raw_query, emotional_signal)
            # Determine whether the technical part is simple or complex
            technical_is_complex = self._is_complex_technical(tech_part or raw_query)
            tech_route = QueryRoute.COMPLEX_SAP if technical_is_complex else QueryRoute.SIMPLE_SAP
            return QueryRoutingDecision(
                route=QueryRoute.MIXED_QUERY,
                reason="Mixed query containing both emotional expression and SAP technical inquiry",
                cleaned_query=raw_query,
                requires_retrieval=(tech_route == QueryRoute.COMPLEX_SAP),
                emotional_part=emotional_signal,
                technical_part=tech_part or raw_query,
                technical_route=tech_route
            )

        # -------------------------------------------------------------
        # 3. PRIORITY: EMOTIONAL_CASUAL
        # -------------------------------------------------------------
        # Check if query is pure emotional/sentimental without SAP technical inquiry
        if has_emotion and not is_sap:
            return QueryRoutingDecision(
                route=QueryRoute.EMOTIONAL_CASUAL,
                reason="Emotional/sentimental inquiry without technical SAP question",
                cleaned_query=raw_query,
                requires_retrieval=False,
                emotional_part=emotional_signal
            )

        # Check if query is pure greeting, gratitude, or casual chat without SAP question
        # Note: If user says 'Hey, what is SAP?', stripped_query is 'what is SAP?', which is SAP-related.
        # So only route to EMOTIONAL_CASUAL if the remainder is not an SAP question!
        is_casual = self._has_casual_greeting_signal(raw_query)
        if is_casual and (not stripped_query or not is_sap_stripped):
            return QueryRoutingDecision(
                route=QueryRoute.EMOTIONAL_CASUAL,
                reason="Pure casual conversational greeting or gratitude",
                cleaned_query=raw_query,
                requires_retrieval=False
            )

        # -------------------------------------------------------------
        # 4. PRIORITY: SIMPLE_SAP vs 5. COMPLEX_SAP
        # -------------------------------------------------------------
        target_sap_text = stripped_query if (stripped_query and is_sap_stripped) else raw_query

        if is_sap:
            if self._is_simple_sap(target_sap_text):
                return QueryRoutingDecision(
                    route=QueryRoute.SIMPLE_SAP,
                    reason="Simple conceptual, definitional, or overview SAP question",
                    cleaned_query=target_sap_text,
                    requires_retrieval=False
                )
            else:
                return QueryRoutingDecision(
                    route=QueryRoute.COMPLEX_SAP,
                    reason="Complex, implementation, troubleshooting, or evidence-dependent SAP question",
                    cleaned_query=target_sap_text,
                    requires_retrieval=True
                )

        # -------------------------------------------------------------
        # 6. PRIORITY: OUT_OF_DOMAIN
        # -------------------------------------------------------------
        return QueryRoutingDecision(
            route=QueryRoute.OUT_OF_DOMAIN,
            reason="Query is outside SAP scope and not conversational/emotional",
            cleaned_query=raw_query,
            requires_retrieval=False
        )


query_router = QueryRouter()
