"""Regression tests for the single-pass scoring loops in retrieval, grounding and aspect merging (offline)."""

from types import SimpleNamespace

import numpy as np

from backend.app.config import settings
from backend.app.models.chunk import DocumentChunk
from backend.app.models.document import Document
from backend.app.services.embedding_service import embedding_service
from backend.app.services.evidence_markers import assign_evidence_ids
from backend.app.services.grounding_service import GroundingService
from backend.app.services.rag_service import RAGService
from backend.app.services.reranking_service import reranking_service
from backend.app.services.retrieval_service import RetrievalService


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def count(self):
        return len(self.rows)

    def order_by(self, *_):
        return self

    def all(self):
        return list(self.rows)


class _Session:
    """Just enough of a SQLAlchemy session for RetrievalService._get_cache."""

    def __init__(self, chunks):
        self.chunks = chunks

    def query(self, *cols):
        if cols[0] is DocumentChunk:
            return _Rows(self.chunks)
        doc_ids = sorted({c.document_id for c in self.chunks})
        if cols[1] is Document.allowed_groups:
            return _Rows([(d, []) for d in doc_ids])
        return _Rows([(d, "official_sap") for d in doc_ids])


def _chunk(i, text, vector):
    return SimpleNamespace(
        id=f"chunk-{i}", document_id="doc-1", document_name="Guide.pdf", page_number=i + 1,
        section=None, chunk_text=text, embedding=vector, insertion_order=i,
        embedding_model=embedding_service.embedding_model, embedding_version=embedding_service.embedding_version,
    )


def test_lexical_channel_matches_mixed_case_chunks_and_returns_original_text(monkeypatch):
    dim = settings.EMBEDDING_DIM
    zero = [0.0] * dim
    chunks = [
        _chunk(0, "The FKKBIX_DISP Monitor shows Billable Items before Convergent Invoicing.", zero),
        _chunk(1, "Opening hours of the canteen are posted at the entrance.", zero),
    ]
    monkeypatch.setattr(embedding_service, "embed_text", lambda text: [1.0] + [0.0] * (dim - 1))
    service = RetrievalService()

    results = service.hybrid_search(_Session(chunks), "billable items monitor", top_k=5)

    assert results[0]["chunk_id"] == "chunk-0"
    assert results[0]["bm25_score"] > 0
    assert results[0]["chunk_text"] == chunks[0].chunk_text  # original casing is preserved
    assert all(r["chunk_id"] != "chunk-1" or r["bm25_score"] == 0 for r in results)


def test_cache_holds_lowercased_text_once_per_chunk():
    dim = settings.EMBEDDING_DIM
    chunk = _chunk(0, "Convergent Invoicing posts to FI-CA.", np.ones(dim).tolist())
    service = RetrievalService()

    meta = service._get_cache(_Session([chunk]))[0]

    assert meta[0]["text_lower"] == "convergent invoicing posts to fi-ca."
    assert meta[0]["chunk_text"] == "Convergent Invoicing posts to FI-CA."


def test_dense_candidate_selection_matches_full_sort_for_eligible_scores():
    scores = np.array([0.12, 0.93, 0.71, 0.99, 0.85, 0.42], dtype=np.float32)
    eligible = np.array([True, False, True, True, True, True])

    selected = RetrievalService._top_score_indices(scores, eligible, 3)

    assert selected == [3, 4, 2]


def test_dense_candidate_selection_orders_ties_by_corpus_index():
    scores = np.array([0.8, 0.8, 0.2, 0.8], dtype=np.float32)
    eligible = np.ones(4, dtype=bool)

    selected = RetrievalService._top_score_indices(scores, eligible, 3)

    assert selected == [0, 1, 3]


def test_overlap_scores_drive_best_passage_and_supporting_sources_consistently():
    passages = assign_evidence_ids([
        {"source_type": "knowledge_base", "source_id": "kb-1",
         "chunk_text": "Convergent Invoicing creates invoicing documents from billable items."},
        {"source_type": "retrieval_gap", "retrieval_gap": True, "question_aspect": "x",
         "chunk_text": "No relevant internal evidence was retrieved for this question part."},
        {"source_type": "knowledge_base", "source_id": "kb-2",
         "chunk_text": "Billing documents are posted to FI-CA contract accounts by the invoicing run."},
    ])
    answer = "The invoicing run posts billing documents to FI-CA contract accounts [E2]."

    claim = GroundingService().verify_answer("FI-CA posting", answer, passages)["claims"][0]

    assert claim["best_evidence_id"] == "E2"  # the gap passage is never a citable best match
    assert claim["citation_status"] == "correct"
    assert "kb-2" in claim["supporting_source_ids"]


def test_aspect_merge_keeps_each_passage_once(monkeypatch):
    shared = {"chunk_id": "shared", "chunk_text": "SOM and CI", "score": 0.9}
    per_aspect = {
        "Subscription order management with SOM": [shared, {"chunk_id": "som", "chunk_text": "SOM", "score": 0.8}],
        "Convergent Invoicing billing runs in CI": [shared, {"chunk_id": "ci", "chunk_text": "CI", "score": 0.7}],
    }
    service = RAGService()
    monkeypatch.setattr(RAGService, "split_question_aspects", staticmethod(lambda q: list(per_aspect)))
    monkeypatch.setattr(service.retrieval, "retrieve", lambda db, aspect, top_k, allowed_groups=None: per_aspect[aspect])
    # The global cut keeps two passages; "ci" survives only through the per-aspect guarantee.
    monkeypatch.setattr(settings, "RERANK_TOP_K", 2)
    monkeypatch.setattr(settings, "TOP_K", 1)

    kept, _, aspect_results = service._retrieve_aspect_evidence(None, "q", 5)

    ids = [item["chunk_id"] for item in kept]
    assert len(ids) == len(set(ids))
    assert {"som", "ci"} <= set(ids)
    assert len(aspect_results) == 2


def test_cross_component_handoff_uses_wider_candidate_and_rerank_windows(monkeypatch):
    service = RAGService()
    calls = {}
    candidates = [{"chunk_id": f"c{i}", "chunk_text": f"evidence {i}", "score": 0.9 - i / 100}
                  for i in range(50)]

    def retrieve(_db, _query, top_k, allowed_groups=None):
        calls["retrieval_top_k"] = top_k
        return candidates[:top_k]

    def rerank(_query, rows, top_n):
        calls["rerank_top_n"] = top_n
        return rows[:top_n], 0.9, True

    monkeypatch.setattr(service.retrieval, "retrieve", retrieve)
    monkeypatch.setattr(reranking_service, "rerank_kb", rerank)
    monkeypatch.setattr(settings, "TOP_K", 10)
    monkeypatch.setattr(settings, "RERANK_TOP_K", 4)

    rows, _, _ = service._retrieve_aspect_evidence(
        None, "How does Convergent Invoicing post billing and invoice documents to FI-CA?", 10
    )

    assert calls == {"retrieval_top_k": 50, "rerank_top_n": 8}
    assert len(rows) == 8


def test_single_component_question_keeps_configured_retrieval_windows(monkeypatch):
    service = RAGService()
    calls = {}

    def retrieve(_db, _query, top_k, allowed_groups=None):
        calls["retrieval_top_k"] = top_k
        return [{"chunk_id": "one", "chunk_text": "FI-CA", "score": 0.8}]

    def rerank(_query, rows, top_n):
        calls["rerank_top_n"] = top_n
        return rows, 0.8, True

    monkeypatch.setattr(service.retrieval, "retrieve", retrieve)
    monkeypatch.setattr(reranking_service, "rerank_kb", rerank)
    monkeypatch.setattr(settings, "TOP_K", 10)
    monkeypatch.setattr(settings, "RERANK_TOP_K", 4)

    service._retrieve_aspect_evidence(None, "What does FI-CA do?", 10)

    assert calls == {"retrieval_top_k": 10, "rerank_top_n": 4}


def test_evidence_selection_keeps_wide_handoff_rerank_set():
    from backend.app.services.evidence_selection_service import EvidenceSelectionService

    rows = [
        {"chunk_id": f"c{i}", "chunk_text": f"Convergent Invoicing FI-CA passage {i}", "score": 0.9}
        for i in range(8)
    ]
    selector = EvidenceSelectionService()
    result = selector.select_strongest_evidence(
        query="How does Convergent Invoicing post documents to FI-CA?",
        internal_candidates=rows,
        external_candidates=[],
        internal_confidence_data={"confidence": 0.9, "should_trigger_tavily": False},
        max_internal_passages=8,
    )

    assert len(result["selected_passages"]) == 8
