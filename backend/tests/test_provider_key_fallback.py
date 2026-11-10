"""Fallback API keys per provider (services/provider_keys.py, LLMService._rotate_keys / _stream_with_keys).

The real provider functions run against fake SDK clients whose behaviour depends on the API key they were
built with. Covered: a rejected or out-of-quota key hands over to the provider's next key before the next
provider is tried; failures that are not the key's fault do not burn the other keys; a stream never changes
key after it sent text; unset or placeholder keys are ignored; key values never reach the logs; Gemini calls
with different keys never overlap (its SDK has one process-wide key). Offline: no network, no real key."""

import importlib
import logging
import sys
import threading
import time
from types import SimpleNamespace

import groq
import openai
import pytest

from backend.app.config import settings
from backend.app.services import provider_keys
from backend.app.services.llm_service import GenerationResult, LLMStreamInterrupted, model_cooldowns

llm_module = importlib.import_module("backend.app.services.llm_service")
service = llm_module.llm_service

K1, F1, F2, F3 = "key-primary-0001", "key-fallback-0002", "key-fallback-0003", "key-fallback-0004"
ANSWER = "Convergent Invoicing bills the billable items of a contract account."


class StatusError(Exception):
    """An SDK error carrying an HTTP status, like groq.APIStatusError."""

    def __init__(self, status, message="error"):
        super().__init__(f"Error code: {status} - {message}")
        self.status_code = status


def auth_error():
    return StatusError(401, "invalid api key")


def quota_error():
    return StatusError(429, "rate limit exceeded: quota")


class FakeStream:
    def __init__(self, pieces, error=None):
        self.pieces, self.error = list(pieces), error

    def __iter__(self):
        for index, piece in enumerate(self.pieces):
            # A complete stream ends with a finish_reason; one that stops without it is treated as cut off.
            last = index == len(self.pieces) - 1 and self.error is None
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=piece),
                                                           finish_reason="stop" if last else None)])
        if self.error is not None:
            raise self.error

    def close(self):
        pass


class FakeClient:
    """Stands in for groq.Groq and openai.OpenAI. `behaviour[api_key](model)` returns text, a FakeStream or an
    exception to raise; every request is logged as (api_key, model, stream)."""

    calls = []
    behaviour = {}

    def __init__(self, **kwargs):
        self.api_key = kwargs.get("api_key")
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        stream = bool(kwargs.get("stream"))
        FakeClient.calls.append((self.api_key, kwargs["model"], stream))
        reply = FakeClient.behaviour[self.api_key](kwargs["model"])
        if isinstance(reply, BaseException):
            raise reply
        if stream:
            return reply if isinstance(reply, FakeStream) else FakeStream([reply])
        choice = SimpleNamespace(message=SimpleNamespace(content=reply), finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=None)


def keys_called():
    return [call[0] for call in FakeClient.calls]


@pytest.fixture(autouse=True)
def env(monkeypatch):
    FakeClient.calls, FakeClient.behaviour = [], {}
    monkeypatch.setattr(groq, "Groq", FakeClient)
    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    monkeypatch.setattr(settings, "LLM_LAST_RESORT_MODELS", "")
    monkeypatch.setattr(settings, "LLM_REQUEST_DEADLINE_SECONDS", 120.0)
    monkeypatch.setattr(service, "allowed_providers", {"groq", "gemini", "openrouter"})
    monkeypatch.setattr(service, "groq_api_key", K1)
    monkeypatch.setattr(service, "openrouter_api_key", "")
    monkeypatch.setattr(service, "gemini_api_key", "")
    monkeypatch.setattr(service, "GROQ_MODELS", ["gq-a", "gq-b"])
    monkeypatch.setattr(service, "OPENROUTER_MODELS", ["or-a"])
    monkeypatch.setattr(service, "GEMINI_MODELS", ["gm-a"])
    monkeypatch.setattr(service, "_prepare_external_prompts", lambda provider, model, prompt, system: (prompt, system))
    for provider in provider_keys.PROVIDERS:
        for number in (1, 2, 3):
            monkeypatch.setattr(settings, f"{provider.upper()}_API_KEY_FALLBACK_{number}", "")
    model_cooldowns.clear()
    yield
    model_cooldowns.clear()


def set_fallbacks(monkeypatch, provider, *values):
    for number, value in enumerate(values, start=1):
        monkeypatch.setattr(settings, f"{provider.upper()}_API_KEY_FALLBACK_{number}", value)


# ----------------------------------------------------------------------------- the key pool itself

def test_slots_keep_order_and_drop_empty_placeholder_and_repeated_keys(monkeypatch):
    set_fallbacks(monkeypatch, "groq", "  " + F1 + "  ", "disabled", F1)
    slots = provider_keys.key_slots("groq", K1)
    assert [slot.key for slot in slots] == [K1, F1]
    assert [slot.name for slot in slots] == ["groq", "groq#1"]
    assert [slot.ordinal for slot in slots] == [1, 2]


def test_without_fallback_keys_the_provider_has_one_slot_named_after_the_provider():
    slots = provider_keys.key_slots("groq", K1)
    assert len(slots) == 1 and slots[0].name == "groq"
    assert provider_keys.key_slots("groq", "") == []


def test_a_provider_with_only_fallback_keys_still_works(monkeypatch):
    set_fallbacks(monkeypatch, "groq", F1)
    monkeypatch.setattr(service, "groq_api_key", "")
    FakeClient.behaviour = {F1: lambda model: ANSWER}
    assert service._has_api_key("groq")
    assert service._call_groq("p", "s") == ANSWER
    assert keys_called() == [F1]


def test_slot_repr_never_shows_the_key(monkeypatch):
    set_fallbacks(monkeypatch, "groq", F1)
    assert all(K1 not in repr(slot) and F1 not in repr(slot) for slot in provider_keys.key_slots("groq", K1))


def test_gemini_keys_are_cleaned_like_the_primary_key(monkeypatch):
    set_fallbacks(monkeypatch, "gemini", ". " + F1)
    assert provider_keys.fallback_keys("gemini") == [F1]


# ----------------------------------------------------------------------------- Groq: non-streaming

def test_a_rejected_key_hands_over_to_the_next_key_of_the_same_provider(monkeypatch):
    set_fallbacks(monkeypatch, "groq", F1, F2)
    FakeClient.behaviour = {K1: lambda m: auth_error(), F1: lambda m: ANSWER, F2: lambda m: "never"}
    assert service._call_groq("p", "s") == ANSWER
    assert keys_called() == [K1, F1]  # the rejected key is not retried for the second model, F2 is not touched
    assert not model_cooldowns.available("groq", "gq-a")  # the normal key is cooling down ...
    assert model_cooldowns.available("groq#1", "gq-a")  # ... the fallback key is not


def test_a_key_that_hit_its_quota_hands_over_to_the_next_key(monkeypatch):
    set_fallbacks(monkeypatch, "groq", F1)
    FakeClient.behaviour = {K1: lambda m: quota_error(), F1: lambda m: ANSWER}
    assert service._call_groq("p", "s") == ANSWER
    assert keys_called() == [K1, K1, F1]  # both models of the first key were tried, then the next key


def test_a_cooling_key_is_skipped_on_the_next_request(monkeypatch):
    set_fallbacks(monkeypatch, "groq", F1)
    FakeClient.behaviour = {K1: lambda m: auth_error(), F1: lambda m: ANSWER}
    service._call_groq("p", "s")
    FakeClient.calls.clear()
    assert service._call_groq("p", "s") == ANSWER
    assert keys_called() == [F1]


def test_a_failure_that_is_not_the_keys_fault_does_not_spend_the_other_keys(monkeypatch):
    set_fallbacks(monkeypatch, "groq", F1)
    FakeClient.behaviour = {K1: lambda m: "", F1: lambda m: ANSWER}  # empty replies are request-level
    assert service._call_groq("p", "s") is None
    assert F1 not in keys_called()


def test_when_every_key_is_dead_the_next_provider_answers(monkeypatch):
    set_fallbacks(monkeypatch, "groq", F1)
    monkeypatch.setattr(service, "openrouter_api_key", "or-key")
    FakeClient.behaviour = {K1: lambda m: auth_error(), F1: lambda m: auth_error(), "or-key": lambda m: ANSWER}
    meta = GenerationResult()
    answer = service._answer_from_providers(["groq", "openrouter"], "question", meta, None)
    assert answer == ANSWER and meta.providers_attempted == ["groq", "openrouter"]
    assert keys_called() == [K1, F1, "or-key"]


def test_a_provider_keeps_working_until_all_its_keys_are_dead_then_fails_fast(monkeypatch):
    set_fallbacks(monkeypatch, "groq", F1)
    FakeClient.behaviour = {K1: lambda m: auth_error(), F1: lambda m: auth_error()}
    assert service._call_groq("p", "s") is None
    FakeClient.calls.clear()
    assert service._call_groq("p", "s") is None
    assert FakeClient.calls == []  # every key is cooling down: no request is sent at all


# ----------------------------------------------------------------------------- Groq: streaming

def test_a_stream_rejected_before_any_text_moves_to_the_next_key(monkeypatch):
    set_fallbacks(monkeypatch, "groq", F1)
    FakeClient.behaviour = {K1: lambda m: auth_error(), F1: lambda m: FakeStream(["Hello ", "world"])}
    assert "".join(service._stream_groq("p", "s")) == "Hello world"
    assert keys_called() == [K1, F1]


def test_a_stream_never_changes_key_after_it_sent_text(monkeypatch):
    set_fallbacks(monkeypatch, "groq", F1)
    FakeClient.behaviour = {K1: lambda m: FakeStream(["Half an ans"], error=auth_error()), F1: lambda m: FakeStream(["x"])}
    received = []
    with pytest.raises(LLMStreamInterrupted):
        for chunk in service._stream_groq("p", "s"):
            received.append(chunk)
    assert received == ["Half an ans"] and F1 not in keys_called()


# ----------------------------------------------------------------------------- OpenRouter

def test_openrouter_rotates_keys_for_calls_and_streams(monkeypatch):
    set_fallbacks(monkeypatch, "openrouter", F1)
    monkeypatch.setattr(service, "openrouter_api_key", K1)
    FakeClient.behaviour = {K1: lambda m: StatusError(402, "insufficient credits"), F1: lambda m: ANSWER}
    assert service._call_openrouter("p", "s") == ANSWER
    assert keys_called() == [K1, F1]
    FakeClient.calls.clear()
    model_cooldowns.clear()
    FakeClient.behaviour = {K1: lambda m: auth_error(), F1: lambda m: FakeStream(["ok"])}
    assert "".join(service._stream_openrouter("p", "s")) == "ok"
    assert keys_called() == [K1, F1]


# ----------------------------------------------------------------------------- Gemini

def fake_genai(monkeypatch, behaviour):
    state = {"key": None, "calls": []}

    def configure(api_key=None, **_kwargs):
        state["key"] = api_key

    class GenerativeModel:
        def __init__(self, model_name, system_instruction=None):
            self.name = model_name

        def generate_content(self, prompt, generation_config=None, request_options=None, stream=False):
            state["calls"].append((state["key"], self.name, stream))
            reply = behaviour[state["key"]](self.name)
            if isinstance(reply, BaseException):
                raise reply
            if stream:
                return iter([SimpleNamespace(text=piece) for piece in reply])
            return SimpleNamespace(candidates=[SimpleNamespace(finish_reason=1)], text=reply, usage_metadata=None)

    module = SimpleNamespace(configure=configure, GenerativeModel=GenerativeModel,
                             types=SimpleNamespace(GenerationConfig=lambda **kwargs: kwargs))
    monkeypatch.setitem(sys.modules, "google.generativeai", module)
    google = sys.modules.get("google")
    if google is not None:
        monkeypatch.setattr(google, "generativeai", module, raising=False)
    return state


def test_gemini_rotates_keys_for_calls_and_streams(monkeypatch):
    set_fallbacks(monkeypatch, "gemini", F1)
    monkeypatch.setattr(service, "gemini_api_key", K1)
    state = fake_genai(monkeypatch, {K1: lambda m: auth_error(), F1: lambda m: ANSWER})
    assert service._call_gemini("p", "s") == ANSWER
    assert [call[0] for call in state["calls"]] == [K1, F1]
    state["calls"].clear()
    model_cooldowns.clear()
    state = fake_genai(monkeypatch, {K1: lambda m: auth_error(), F1: lambda m: ["a", "b"]})
    assert "".join(service._stream_gemini("p", "s")) == "ab"
    assert [call[0] for call in state["calls"]] == [K1, F1]


def test_gemini_calls_with_different_keys_never_overlap():
    gate = provider_keys._GeminiKeyGate()
    events, lock = [], threading.Lock()

    def worker(key, hold):
        with gate.use(key, wait_seconds=5):
            with lock:
                events.append(("in", key))
            time.sleep(hold)
            with lock:
                events.append(("out", key))

    first = threading.Thread(target=worker, args=("A", 0.3))
    first.start()
    time.sleep(0.05)
    same = threading.Thread(target=worker, args=("A", 0.05))
    other = threading.Thread(target=worker, args=("B", 0.05))
    same.start()
    other.start()
    for thread in (first, same, other):
        thread.join(5)
    active, overlapped = {}, False
    for kind, key in events:
        if kind == "in":
            active[key] = active.get(key, 0) + 1
            overlapped = overlapped or len([k for k, n in active.items() if n]) > 1
        else:
            active[key] -= 1
    assert not overlapped, events
    assert len([e for e in events if e[0] == "in"]) == 3  # nobody was dropped


def test_gate_gives_up_waiting_instead_of_hanging_a_request():
    gate = provider_keys._GeminiKeyGate()
    finished = []

    def holder():
        with gate.use("A"):
            time.sleep(0.6)

    thread = threading.Thread(target=holder)
    thread.start()
    time.sleep(0.05)
    started = time.monotonic()
    with gate.use("B", wait_seconds=0.1):
        finished.append(time.monotonic() - started)
    thread.join(5)
    assert finished and finished[0] < 0.5


# ----------------------------------------------------------------------------- privacy and preflight

def test_key_values_never_appear_in_logs(monkeypatch, caplog):
    set_fallbacks(monkeypatch, "groq", F1, F2)
    FakeClient.behaviour = {K1: lambda m: auth_error(), F1: lambda m: quota_error(), F2: lambda m: ANSWER}
    with caplog.at_level(logging.DEBUG):
        assert service._call_groq("p", "s") == ANSWER
    for secret in (K1, F1, F2):
        assert secret not in caplog.text
    assert "trying the next key" in caplog.text


def test_preflight_counts_fallback_keys_without_reporting_them():
    from backend.app import preflight

    values = SimpleNamespace(GROQ_API_KEY="", LLM_API_KEY="", GROQ_API_KEY_FALLBACK_1=F1,
                             GROQ_API_KEY_FALLBACK_2="", GROQ_API_KEY_FALLBACK_3=" ")
    assert preflight._fallback_keys(values, "groq") == [F1]
    assert preflight._provider_key(values, "groq") == F1  # a fallback-only setup counts as configured
