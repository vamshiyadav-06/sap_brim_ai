"""Scholar latency regressions (offline): concurrent per-part web search in the comparator, model cooldowns
for repeatable provider failures, and retrieval cache warm-up off the request path."""

import asyncio
import importlib
import threading
import time
from types import SimpleNamespace

import pytest

from backend.app import main as main_module
from backend.app.config import settings
from backend.app.retrieval import multi_source_compare as compare_module
from backend.app.retrieval.multi_source_compare import MultiSourceComparator
from backend.app.services.conversation_memory_service import MemoryIntent, RECENT_CONTEXT_LIMIT
from backend.app.services.rag_service import _needs_conversation_summary
from backend.app.services import query_rewriter as rewriter_module
from backend.app.services.llm_service import ModelCooldowns, llm_service, model_cooldowns
from backend.app.services.retrieval_service import RetrievalService

ASPECTS = ["CI billable item processing", "FI-CA posting of invoicing documents", "Convergent Charging rating"]
retrieval_module = importlib.import_module("backend.app.services.retrieval_service")


def _web_hit(aspect, n):
    return {
        "source_id": f"web_{n}", "title": aspect, "url": f"https://help.sap.com/docs/{abs(hash(aspect))}/{n}",
        "domain": "help.sap.com", "snippet": f"{aspect} documentation", "chunk_text": f"{aspect} documentation",
        "authority_tier": 1, "authority_class": "official_sap_documentation", "source_type": "web",
    }


# ---------------------------------------------------------------- comparator web search

def test_comparator_searches_question_parts_concurrently_in_order(monkeypatch):
    barrier = threading.Barrier(len(ASPECTS), timeout=2)  # passes only if all parts are in flight together
    searched = []

    def search(aspect):
        searched.append(aspect)
        barrier.wait()
        return [_web_hit(aspect, 1)]

    monkeypatch.setattr(settings, "WEB_FALLBACK_ENABLED", True)
    monkeypatch.setattr(compare_module.web_search_service, "search_sap_authoritative", search)

    result = MultiSourceComparator().execute_multi_source_comparison(
        db=None, query="; ".join(ASPECTS), kb_candidates=[], repo_source_enabled=False,
        multi_source_compare_enabled=True, web_search_available=True, query_aspects=ASPECTS,
    )

    assert sorted(searched) == sorted(ASPECTS)
    web = [c["question_aspect"] for c in result["all_candidates"] if c["source_type"] == "web"]
    assert web == ASPECTS  # merged in question-part order, as the sequential loop did


def test_one_failing_part_does_not_drop_the_other_parts(monkeypatch):
    def search(aspect):
        if aspect == ASPECTS[1]:
            raise RuntimeError("provider hiccup")
        return [_web_hit(aspect, 1)]

    monkeypatch.setattr(settings, "WEB_FALLBACK_ENABLED", True)
    monkeypatch.setattr(compare_module.web_search_service, "search_sap_authoritative", search)

    result = MultiSourceComparator().execute_multi_source_comparison(
        db=None, query="q", kb_candidates=[], repo_source_enabled=False,
        multi_source_compare_enabled=True, web_search_available=True, query_aspects=ASPECTS,
    )

    assert [c["question_aspect"] for c in result["all_candidates"] if c["source_type"] == "web"] == [
        ASPECTS[0], ASPECTS[2]
    ]


# ---------------------------------------------------------------- model cooldowns

class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ProviderError(Exception):
    def __init__(self, message, status_code=None, headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.response = SimpleNamespace(status_code=status_code, headers=headers or {})


def test_unknown_model_is_skipped_for_an_hour():
    clock = FakeClock()
    cooldowns = ModelCooldowns(clock=clock)

    cooldowns.record_failure("groq", "old-model", ProviderError("The model `old-model` does not exist", 404))

    assert not cooldowns.available("groq", "old-model")
    assert cooldowns.available("groq", "other-model") and cooldowns.available("gemini", "old-model")
    clock.now += 3599
    assert not cooldowns.available("groq", "old-model")
    clock.now += 2
    assert cooldowns.available("groq", "old-model")


def test_decommissioned_model_is_treated_as_unknown():
    cooldowns = ModelCooldowns(clock=FakeClock())
    cooldowns.record_failure("groq", "m", ProviderError("model_decommissioned: m has been decommissioned", 400))
    assert not cooldowns.available("groq", "m")


def test_rate_limit_uses_the_provider_retry_after():
    clock = FakeClock()
    cooldowns = ModelCooldowns(clock=clock)

    cooldowns.record_failure("groq", "busy", ProviderError("Rate limit reached", 429, {"retry-after": "7"}))

    assert not cooldowns.available("groq", "busy")
    clock.now += 7.5
    assert cooldowns.available("groq", "busy")


def test_rate_limit_without_retry_after_cools_down_briefly():
    clock = FakeClock()
    cooldowns = ModelCooldowns(clock=clock)

    cooldowns.record_failure("gemini", "flash", ProviderError("429 Resource exhausted: quota"))

    assert not cooldowns.available("gemini", "flash")
    clock.now += ModelCooldowns.RATE_LIMIT_SECONDS + 1
    assert cooldowns.available("gemini", "flash")


def test_request_specific_errors_do_not_cool_a_model_down():
    cooldowns = ModelCooldowns(clock=FakeClock())
    for error in (ProviderError("context length exceeded", 400), ProviderError("Request too large", 413),
                  ProviderError("internal server error", 500)):
        cooldowns.record_failure("groq", "m", error)
    assert cooldowns.available("groq", "m")


class FakeGroq:
    calls = []
    failures = {}

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, model, **_kwargs):
        FakeGroq.calls.append(model)
        if model in FakeGroq.failures:
            raise FakeGroq.failures[model]
        message = SimpleNamespace(content=f"answer from {model}")
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])


@pytest.fixture
def fake_groq(monkeypatch):
    import groq

    FakeGroq.calls, FakeGroq.failures = [], {}
    monkeypatch.setattr(groq, "Groq", FakeGroq)
    monkeypatch.setattr(llm_service, "groq_api_key", "test-key")
    monkeypatch.setattr(llm_service, "allowed_providers", {"groq"})
    monkeypatch.setattr(llm_service, "GROQ_MODELS", ["retired-model", "busy-model", "good-model"])
    monkeypatch.setattr(llm_service, "_prepare_external_prompts", lambda provider, model, prompt, system: (prompt, system))
    model_cooldowns.clear()
    yield FakeGroq
    model_cooldowns.clear()


def test_failed_models_are_not_retried_on_the_next_request(fake_groq):
    fake_groq.failures = {
        "retired-model": ProviderError("The model `retired-model` does not exist", 404),
        "busy-model": ProviderError("Rate limit reached", 429, {"retry-after": "30"}),
    }

    assert llm_service._call_groq("prompt", "system") == "answer from good-model"
    assert llm_service._call_groq("prompt", "system") == "answer from good-model"

    assert fake_groq.calls == ["retired-model", "busy-model", "good-model", "good-model"]


def test_query_rewriter_skips_cooled_models_and_does_not_retry_silently(monkeypatch, fake_groq):
    monkeypatch.setattr(rewriter_module, "Groq", FakeGroq)
    monkeypatch.setattr(settings, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(settings, "LLM_ALLOWED_PROVIDERS", "groq")
    monkeypatch.setattr(settings, "LLM_GROQ_MODELS", "retired-model,good-model")
    monkeypatch.setattr(settings, "LLM_MODEL", "")
    monkeypatch.setattr(rewriter_module, "redact_external_text", lambda text: (text, {}))
    model_cooldowns.record_failure("groq", "retired-model", ProviderError("does not exist", 404))
    rewriter = rewriter_module.QueryRewriter()
    history = [{"role": "user", "content": "How does SAP Convergent Invoicing create invoicing documents?"},
               {"role": "assistant", "content": "It groups billable items."}]

    result = rewriter.resolve_query("what about its tables?", chat_history=history)

    assert result["reason"] == "llm_rewriter_good-model"
    assert fake_groq.calls == ["good-model"]
    assert rewriter.client.kwargs.get("max_retries") == 0


# ---------------------------------------------------------------- retrieval cache warm-up

def test_warm_cache_builds_with_its_own_session_and_closes_it():
    service = RetrievalService()
    sessions = []

    class Session:
        closed = False

        def __enter__(self):
            sessions.append(self)
            return self

        def __exit__(self, *exc):
            self.closed = True

    built = []
    service._get_cache = lambda db: built.append(db)

    service.warm_cache(Session)

    assert built == sessions and sessions[0].closed


def test_warm_cache_failure_is_logged_not_raised():
    service = RetrievalService()

    def broken():
        raise RuntimeError("database is down")

    service.warm_cache(broken)  # must not raise: the first query then builds the cache as before


def test_invalidation_bursts_schedule_one_background_rebuild(monkeypatch):
    service = RetrievalService()
    done = threading.Event()
    builds = []

    def warm(session_factory=None):
        builds.append(session_factory)
        done.set()

    monkeypatch.setattr(settings, "RETRIEVAL_CACHE_WARMUP", True)
    service.warm_cache = warm
    for _ in range(5):
        service.invalidate_cache(rewarm_delay=0.05)

    assert done.wait(2)
    time.sleep(0.1)
    assert len(builds) == 1


def test_invalidation_does_not_rebuild_when_warm_up_is_disabled(monkeypatch):
    service = RetrievalService()
    builds = []
    monkeypatch.setattr(settings, "RETRIEVAL_CACHE_WARMUP", False)
    service.warm_cache = lambda session_factory=None: builds.append(session_factory)

    service.invalidate_cache(rewarm_delay=0.01)
    time.sleep(0.1)

    assert builds == []


def test_startup_warms_the_retrieval_cache_in_the_background(monkeypatch):
    scheduled = []
    monkeypatch.setattr(settings, "RETRIEVAL_CACHE_WARMUP", True)
    monkeypatch.setattr(main_module, "security_settings_errors", lambda: [])
    monkeypatch.setattr(main_module, "warn_if_not_shared", lambda: None)
    monkeypatch.setattr(main_module, "init_db", lambda: None)
    monkeypatch.setattr(main_module.retrieval_service, "schedule_warm_up", lambda delay=0.0: scheduled.append(delay))

    async def start_and_stop():
        async with main_module.lifespan(main_module.app):
            pass

    asyncio.run(start_and_stop())

    assert scheduled == [0.0]


def test_warm_retrieval_index_skips_repeated_chunk_count_queries(monkeypatch):
    service = RetrievalService()
    service._cache_chunks = []
    service._cache_matrix = None
    service._cache_token_index = {}
    service._cache_tech_id_index = {}
    service._cache_doc_lengths = None
    service._cache_idf = {}
    service._cache_count = 12
    service._cache_checked_at = 100.0
    monkeypatch.setattr(retrieval_module.time, "monotonic", lambda: 110.0)

    class Db:
        def query(self, *_args):
            raise AssertionError("A warm retrieval index should not run COUNT(*) on every query")

    assert service._get_cache(Db())[0] is service._cache_chunks


def test_invalidating_retrieval_cache_forces_rebuild_validation(monkeypatch):
    service = RetrievalService()
    service._cache_chunks = []
    service._cache_checked_at = 100.0
    monkeypatch.setattr(settings, "RETRIEVAL_CACHE_WARMUP", False)

    service.invalidate_cache()

    assert service._cache_chunks is None
    assert service._cache_checked_at == 0.0


def test_rag_loads_summary_only_for_old_context_or_memory_recall():
    assert not _needs_conversation_summary(MemoryIntent.TECHNICAL_RAG, [])
    assert not _needs_conversation_summary(MemoryIntent.TECHNICAL_RAG, [{}] * (RECENT_CONTEXT_LIMIT - 1))
    assert _needs_conversation_summary(MemoryIntent.TECHNICAL_RAG, [{}] * RECENT_CONTEXT_LIMIT)
    assert _needs_conversation_summary(MemoryIntent.HISTORY_RECALL, [])
    assert not _needs_conversation_summary(MemoryIntent.FIRST_MESSAGE_LOOKUP, [{}] * RECENT_CONTEXT_LIMIT)
