import heapq
import re
import math
import logging
import threading
import time
import numpy as np
from typing import List, Dict, Any, FrozenSet, Optional, Set, Tuple
from collections import defaultdict
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models.chunk import DocumentChunk
from backend.app.models.document import Document
from backend.app.security.access import document_visible
from backend.app.services.embedding_service import embedding_service

logger = logging.getLogger(__name__)

# Uploads and re-indexing invalidate in bursts; one rebuild after they settle is enough.
REWARM_DELAY_SECONDS = 5.0
# Avoid an exact PostgreSQL COUNT(*) on every RAG request. API-driven corpus changes call
# invalidate_cache immediately; this interval also catches out-of-process DB edits.
CACHE_VALIDATION_INTERVAL_SECONDS = 30.0

STOP_WORDS = {
    "what", "is", "the", "a", "an", "how", "to", "in", "of", "and", "for", "with",
    "does", "do", "explain", "describe", "can", "you", "tell", "me", "about", "which",
    "are", "why", "where", "when", "by", "from", "on", "at", "as", "into", "onto",
    "should", "would", "could", "been", "being", "have", "has", "had"
}

# High-precision SAP technical identifier patterns
TECH_ID_PATTERN = re.compile(
    r"\b(/[a-z0-9_]{2,}/[a-z0-9_]+|fkkbix_[a-z0-9_]+|dfkk[a-z0-9_]+|fkk[a-z0-9_]+|erdk|erch|but000|but020|"
    r"mara|marc|vbak|vbap|ekko|ekpo|bseg|bkpf|v_fkk[a-z0-9_]+|vkont|gpart|fpe[0-9]|fp[0-9]{2}|"
    r"vk[0-9]{2}|va[0-9]{2}|vf[0-9]{2}|me[0-9]{2}[a-z]?|se[0-9]{2}[a-z]?|sm[0-9]{2}|spro|swo1|sicf)\b",
    re.IGNORECASE
)


class RetrievalService:
    @staticmethod
    def _top_score_indices(scores: np.ndarray, eligible: np.ndarray, limit: int) -> List[int]:
        """Select and order the best eligible scores without sorting the full corpus."""
        candidate_indices = np.flatnonzero(eligible)
        if limit <= 0 or candidate_indices.size == 0:
            return []
        if candidate_indices.size > limit:
            candidate_scores = scores[candidate_indices]
            partition = np.argpartition(candidate_scores, candidate_scores.size - limit)[-limit:]
            candidate_indices = candidate_indices[partition]
        return sorted(candidate_indices.tolist(), key=lambda idx: (-float(scores[idx]), idx))

    @staticmethod
    def preprocess_query(query: str) -> str:
        """Normalizes whitespace and standardizes common punctuation."""
        return re.sub(r"[ \t]+", " ", query).strip()

    @staticmethod
    def extract_query_tokens(query: str) -> List[str]:
        raw = re.findall(r"\b[a-z0-9\/\-_]+\b", query.lower())
        return [t for t in raw if t not in STOP_WORDS and len(t) > 1]

    @staticmethod
    def extract_technical_identifiers(text: str) -> List[str]:
        found = set()
        for m in TECH_ID_PATTERN.finditer(text):
            tok = m.group(0).lower()
            if tok not in STOP_WORDS:
                found.add(tok)
        return list(found)

    def __init__(self):
        self._cache_chunks: Optional[List[Dict[str, Any]]] = None
        self._cache_matrix: Optional[np.ndarray] = None
        self._cache_token_index: Optional[Dict[str, List[int]]] = None
        self._cache_tech_id_index: Optional[Dict[str, List[int]]] = None
        self._cache_doc_lengths: Optional[np.ndarray] = None
        self._avg_doc_len: float = 100.0
        self._cache_idf: Optional[Dict[str, float]] = None
        self._cache_count: int = -1
        self._cache_checked_at: float = 0.0
        self._query_result_cache: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()
        self._warm_timer: Optional[threading.Timer] = None
        self._warm_lock = threading.Lock()

    def invalidate_cache(self, rewarm_delay: float = REWARM_DELAY_SECONDS):
        with self._cache_lock:
            self._cache_chunks = None
            self._cache_matrix = None
            self._cache_token_index = None
            self._cache_tech_id_index = None
            self._cache_doc_lengths = None
            self._cache_idf = None
            self._cache_count = -1
            self._cache_checked_at = 0.0
            self._query_result_cache.clear()
        if settings.RETRIEVAL_CACHE_WARMUP:
            self.schedule_warm_up(delay=rewarm_delay)

    def warm_cache(self, session_factory=None) -> None:
        """Build the chunk index now, with its own session, so no user request has to wait for it."""
        if session_factory is None:
            from backend.app.database import SessionLocal
            session_factory = SessionLocal
        try:
            with session_factory() as db:
                self._get_cache(db)
        except Exception as exc:
            logger.warning("Retrieval cache warm-up failed (%s); the next query builds it instead.", type(exc).__name__)

    def schedule_warm_up(self, delay: float = 0.0) -> None:
        """Warm the cache in a background thread after `delay` seconds; a newer call replaces a pending one."""
        with self._warm_lock:
            if self._warm_timer is not None:
                self._warm_timer.cancel()
            self._warm_timer = threading.Timer(delay, self.warm_cache)
            self._warm_timer.daemon = True
            self._warm_timer.start()

    def _get_cache(self, db: Session):
        with self._cache_lock:
            now = time.monotonic()
            if (
                self._cache_chunks is not None
                and now - self._cache_checked_at < CACHE_VALIDATION_INTERVAL_SECONDS
            ):
                return (
                    self._cache_chunks,
                    self._cache_matrix,
                    self._cache_token_index,
                    self._cache_tech_id_index,
                    self._cache_doc_lengths,
                    self._avg_doc_len,
                    self._cache_idf
                )

            current_count = db.query(DocumentChunk).count()
            if self._cache_chunks is not None and self._cache_count == current_count:
                self._cache_checked_at = now
                return (
                    self._cache_chunks,
                    self._cache_matrix,
                    self._cache_token_index,
                    self._cache_tech_id_index,
                    self._cache_doc_lengths,
                    self._avg_doc_len,
                    self._cache_idf
                )

            # Explicit, stable order: tie-breaking in the ranking channels depends on position.
            all_chunks = db.query(DocumentChunk).order_by(DocumentChunk.insertion_order).all()
            # Access groups per document; changing them must call invalidate_cache() (F-01).
            doc_groups = {
                doc_id: frozenset(groups or ())
                for doc_id, groups in db.query(Document.id, Document.allowed_groups).all()
            }
            doc_tiers = {
                doc_id: tier or "self_authored"
                for doc_id, tier in db.query(Document.id, Document.source_tier).all()
            }
            chunks_meta = []
            vecs = []
            dim = settings.EMBEDDING_DIM

            token_to_indices = defaultdict(list)
            tech_to_indices = defaultdict(list)
            doc_lengths = []

            for idx, c in enumerate(all_chunks):
                emb = c.embedding
                if emb and len(emb) == dim:
                    v = np.array(emb, dtype=np.float32)
                    norm = float(np.linalg.norm(v))
                    v = v / norm if norm > 0 else np.zeros(dim, dtype=np.float32)
                else:
                    v = np.zeros(dim, dtype=np.float32)
                vecs.append(v)

                chunk_text = c.chunk_text or ""
                text_lower = chunk_text.lower()
                raw_tokens = re.findall(r"\b[a-z0-9\/\-_]+\b", text_lower)
                tokens = [t for t in raw_tokens if t not in STOP_WORDS and len(t) > 1]
                doc_lengths.append(len(tokens))

                unique_tokens = set(tokens)
                for tok in unique_tokens:
                    token_to_indices[tok].append(idx)

                tech_ids = self.extract_technical_identifiers(chunk_text)
                for tid in tech_ids:
                    tech_to_indices[tid].append(idx)

                chunks_meta.append({
                    "chunk_id": c.id,
                    "document_id": c.document_id,
                    "document_name": c.document_name,
                    "page_number": c.page_number,
                    "section": c.section or f"Page {c.page_number}",
                    "chunk_text": chunk_text,
                    # Lowercased once here; BM25 and coverage scoring read it for every query term.
                    "text_lower": text_lower,
                    "tech_ids": tech_ids,
                    "allowed_groups": doc_groups.get(c.document_id, frozenset()),
                    "provenance_tier": doc_tiers.get(c.document_id, "self_authored"),
                    "embedding_compatible": (
                        c.embedding_model == embedding_service.embedding_model
                        and c.embedding_version == embedding_service.embedding_version
                    ),
                })

            N = len(chunks_meta)
            doc_lengths_arr = np.array(doc_lengths, dtype=np.float32)
            avg_len = float(np.mean(doc_lengths_arr)) if N > 0 else 100.0

            idf_dict = {}
            for tok, doc_list in token_to_indices.items():
                df = len(doc_list)
                idf_dict[tok] = max(0.1, math.log(1.0 + (N - df + 0.5) / (df + 0.5)))

            self._cache_chunks = chunks_meta
            self._cache_matrix = np.array(vecs, dtype=np.float32) if vecs else np.zeros((0, dim), dtype=np.float32)
            self._cache_token_index = dict(token_to_indices)
            self._cache_tech_id_index = dict(tech_to_indices)
            self._cache_doc_lengths = doc_lengths_arr
            self._avg_doc_len = avg_len
            self._cache_idf = idf_dict
            self._cache_count = current_count
            self._cache_checked_at = time.monotonic()

            return (
                self._cache_chunks,
                self._cache_matrix,
                self._cache_token_index,
                self._cache_tech_id_index,
                self._cache_doc_lengths,
                self._avg_doc_len,
                self._cache_idf
            )

    def hybrid_search(
        self,
        db: Session,
        query: str,
        top_k: Optional[int] = None,
        document_filter: Optional[str] = None,
        allowed_groups: Optional[FrozenSet[str]] = None,
        **kwargs
    ) -> List[Dict[str, Any]]:
        """
        Independent Multi-Channel Enterprise Retrieval Pipeline:
        1. Channel A: Dense Semantic Vector Retrieval
        2. Channel B: Independent Full-Corpus Lexical BM25 Retrieval
        3. Channel C: Exact SAP Technical Identifier Retrieval
        4. Multi-Channel Reciprocal Rank Fusion (RRF) with identifier boosting
        5. Near-duplicate chunk suppression

        allowed_groups: None = every document (admins); otherwise only chunks of documents sharing a
        group. Applied in every channel before ranking, so restricted chunks can never be returned.
        """
        k = top_k or settings.TOP_K
        processed_query = self.preprocess_query(query)
        groups_key = None if allowed_groups is None else tuple(sorted(allowed_groups))

        cache_key = (
            processed_query, k, document_filter, groups_key,
            settings.INCLUDE_SELF_AUTHORED_DOCS,
        )
        with self._cache_lock:
            if cache_key in self._query_result_cache:
                return list(self._query_result_cache[cache_key])

        query_tokens = self.extract_query_tokens(processed_query)
        query_tech_ids = self.extract_technical_identifiers(processed_query)

        query_vector_list = embedding_service.embed_text(processed_query)
        query_vector = np.array(query_vector_list, dtype=np.float32)
        q_norm = float(np.linalg.norm(query_vector))
        if q_norm > 0:
            query_vector = query_vector / q_norm

        (
            cached_meta,
            cached_matrix,
            token_index,
            tech_index,
            doc_lengths,
            avg_doc_len,
            idf_dict
        ) = self._get_cache(db)

        if not cached_meta or cached_matrix.shape[0] == 0:
            return []

        CANDIDATES_PER_CHANNEL = max(60, k * 4)

        # Eligibility (access groups, optional document filter) is decided before any ranking.
        eligible = np.fromiter(
            (
                document_visible(meta["allowed_groups"], allowed_groups)
                and (settings.INCLUDE_SELF_AUTHORED_DOCS or meta["provenance_tier"] != "self_authored")
                and (not document_filter or meta["document_name"] == document_filter)
                for meta in cached_meta
            ),
            dtype=bool,
            count=len(cached_meta),
        )
        if not eligible.any():
            return []

        # CHANNEL 1: INDEPENDENT DENSE VECTOR RETRIEVAL
        sims = cached_matrix @ query_vector
        # Cosine similarity already has a meaningful zero point. Mapping it to
        # (cosine + 1) / 2 gives unrelated vectors a misleading 0.50 baseline.
        normalized_sims = np.clip(sims, 0.0, 1.0)
        dense_eligible = eligible & np.fromiter(
            (meta["embedding_compatible"] for meta in cached_meta), dtype=bool, count=len(cached_meta)
        )
        top_vec_indices = self._top_score_indices(
            normalized_sims, dense_eligible, CANDIDATES_PER_CHANNEL
        )

        vec_rank_map: Dict[int, int] = {}
        for rank, idx in enumerate(top_vec_indices, start=1):
            vec_rank_map[idx] = rank

        # CHANNEL 2: INDEPENDENT LEXICAL BM25 RETRIEVAL (FULL CORPUS)
        bm25_scores = defaultdict(float)
        k1 = 1.5
        b = 0.75

        for tok in query_tokens:
            if tok in token_index:
                tok_idf = idf_dict.get(tok, 0.5)
                doc_list = token_index[tok]
                for d_idx in doc_list:
                    if not eligible[d_idx]:
                        continue
                    tf = cached_meta[d_idx]["text_lower"].count(tok)
                    d_len = doc_lengths[d_idx]
                    denom = tf + k1 * (1.0 - b + b * (d_len / avg_doc_len))
                    score = tok_idf * ((tf * (k1 + 1.0)) / max(0.001, denom))
                    if len(tok) in (2, 3, 4) and (tok in {"som", "cc", "ci", "fica", "bit", "cit", "rar", "mm", "sd", "fi", "co"}):
                        score *= 1.4
                    bm25_scores[d_idx] += score

        sorted_bm25 = heapq.nsmallest(
            CANDIDATES_PER_CHANNEL,
            bm25_scores.items(),
            key=lambda item: (-item[1], item[0]),
        )
        lex_rank_map: Dict[int, int] = {}
        for rank, (idx, _) in enumerate(sorted_bm25, start=1):
            lex_rank_map[idx] = rank

        # CHANNEL 3: EXACT TECHNICAL IDENTIFIER RETRIEVAL
        tech_hits = defaultdict(int)
        for tid in query_tech_ids:
            if tid in tech_index:
                for d_idx in tech_index[tid]:
                    if not eligible[d_idx]:
                        continue
                    tech_hits[d_idx] += 1

        sorted_tech = sorted(tech_hits.items(), key=lambda x: x[1], reverse=True)[:30]
        tech_rank_map: Dict[int, int] = {}
        for rank, (idx, _) in enumerate(sorted_tech, start=1):
            tech_rank_map[idx] = rank

        # CHANNEL 4: RECIPROCAL RANK FUSION (RRF)
        RRF_K = 60
        all_candidate_indices = set(vec_rank_map.keys()) | set(lex_rank_map.keys()) | set(tech_rank_map.keys())
        rrf_fused_scores: Dict[int, float] = {}

        w_vec = 1.0
        w_lex = 0.85
        w_tech = 1.30

        for idx in all_candidate_indices:
            score = 0.0
            if idx in vec_rank_map:
                score += w_vec / (RRF_K + vec_rank_map[idx])
            if idx in lex_rank_map:
                score += w_lex / (RRF_K + lex_rank_map[idx])
            if idx in tech_rank_map:
                score += w_tech / (RRF_K + tech_rank_map[idx])
            rrf_fused_scores[idx] = score

        sorted_candidates = sorted(rrf_fused_scores.items(), key=lambda x: x[1], reverse=True)

        # NEAR-DUPLICATE SUPPRESSION & CALIBRATED SCORES
        results: List[Dict[str, Any]] = []
        seen_passages: List[Set[str]] = []

        for idx, fused_score in sorted_candidates:
            meta = cached_meta[idx]
            
            # Phase 1: Exclude off-domain documents when flag is enabled (default False)
            if getattr(settings, "EXCLUDE_OFF_DOMAIN_DOCS", False):
                try:
                    from backend.app.corpus.tier_definitions import DOCUMENT_TIER_MAPPING
                    if DOCUMENT_TIER_MAPPING.get(meta["document_name"], {}).get("is_off_domain", False):
                        continue
                except Exception:
                    pass

            v_score = float(normalized_sims[idx]) if meta["embedding_compatible"] else 0.0
            b_score = float(bm25_scores.get(idx, 0.0))
            
            c_text = meta["chunk_text"]
            c_lower = meta["text_lower"]
            c_words = set(re.findall(r"\b[a-z]{3,}\b", c_lower))

            is_duplicate = False
            for prev_words in seen_passages:
                intersection = len(c_words & prev_words)
                union = len(c_words | prev_words)
                if union > 0 and (intersection / union) > 0.82:
                    is_duplicate = True
                    break

            if is_duplicate:
                continue

            seen_passages.append(c_words)

            token_coverage = (
                sum(1 for token in query_tokens if token in c_lower) / len(query_tokens)
                if query_tokens else 0.5
            )
            identifier_match = 1.0 if query_tech_ids and idx in tech_rank_map else 0.0
            phrase_match = 1.0 if len(query_tokens) > 1 and " ".join(query_tokens[:3]) in c_lower else 0.0
            calibrated_score = round(min(0.99, max(0.0, (
                0.60 * v_score + 0.25 * token_coverage +
                0.10 * identifier_match + 0.05 * phrase_match
            ))), 4)

            results.append({
                "chunk_id": meta["chunk_id"],
                "document_id": meta["document_id"],
                "document_name": meta["document_name"],
                "provenance_tier": meta["provenance_tier"],
                "source_type": "knowledge_base",
                "page_number": meta["page_number"],
                "section": meta["section"],
                "chunk_text": c_text,
                "snippet": c_text[:350],
                "vector_score": round(v_score, 4),
                "bm25_score": round(b_score, 4),
                "rrf_score": round(fused_score, 6),
                "has_tech_id_match": bool(idx in tech_rank_map),
                "score": calibrated_score,
            })

            if len(results) >= k:
                break

        with self._cache_lock:
            if len(self._query_result_cache) >= 512:
                self._query_result_cache.pop(next(iter(self._query_result_cache)))
            self._query_result_cache[cache_key] = results
        return results

    def retrieve(self, db: Session, query: str, top_k: int = 15,
                 allowed_groups: Optional[FrozenSet[str]] = None) -> List[Dict[str, Any]]:
        """Standard retrieval interface delegating to hybrid search."""
        return self.hybrid_search(db, query, top_k=top_k, allowed_groups=allowed_groups)


retrieval_service = RetrievalService()
