"""Answer language: Settings choice, question and answer translation, direct-answer directive."""

import importlib

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.services import languages
from backend.tests.org_helpers import OrgFixtures, headers_for

client = TestClient(app)
llm_module = importlib.import_module("backend.app.services.llm_service")


@pytest.fixture
def fx():
    fixtures = OrgFixtures()
    yield fixtures
    fixtures.cleanup()


def test_the_user_picks_an_answer_language_in_settings(fx):
    user = fx.user()
    headers = headers_for(user.id)
    assert client.get("/api/auth/me", headers=headers).json()["preferred_language"] == "en"
    chosen = client.patch("/api/auth/me/language", headers=headers, json={"language": "hi"})
    assert chosen.status_code == 200 and chosen.json()["preferred_language"] == "hi"
    assert client.get("/api/auth/me", headers=headers).json()["preferred_language"] == "hi"
    assert client.patch("/api/auth/me/language", headers=headers, json={"language": "xx"}).status_code == 422
    assert client.patch("/api/auth/me/language", json={"language": "hi"}).status_code in (401, 403)


def test_english_text_is_recognised_and_other_text_is_not():
    assert languages.looks_english("What is a billable item in SAP BRIM?")
    assert not languages.looks_english("SAP BRIM में बिलिंग क्या है?")
    assert not languages.looks_english("¿Qué es la facturación convergente?")
    assert not languages.looks_english("FKKINVBILL")  # an identifier alone says nothing about the language


def test_questions_are_translated_to_english_only_when_needed(monkeypatch):
    calls = []
    monkeypatch.setattr(languages, "_complete", lambda prompt, system: calls.append(prompt) or "What is FI-CA?")

    assert languages.question_to_english("FI-CA क्या है?", "hi") == "What is FI-CA?"
    assert languages.question_to_english("What is FI-CA?", "hi") == "What is FI-CA?"  # already English
    # An English-setting user who writes in another script is still understood (translated for retrieval)...
    assert languages.question_to_english("FI-CA क्या है?", "en") == "What is FI-CA?"
    # ...but plain English text is never sent for translation.
    assert languages.question_to_english("FKKINVBILL dunning levels", "en") == "FKKINVBILL dunning levels"
    assert len(calls) == 2


def test_answers_are_translated_with_citations_and_identifiers_protected(monkeypatch):
    seen = {}
    monkeypatch.setattr(languages, "_complete", lambda prompt, system: seen.setdefault("p", prompt) and "अनुवादित [E1]")

    assert languages.answer_to_language("Run FKKINVBILL [E1].", "hi") == "अनुवादित [E1]"
    assert "Hindi" in seen["p"] and "[E1]" in seen["p"] and "transaction codes" in seen["p"]
    assert languages.answer_to_language("Run FKKINVBILL [E1].", "en") == "Run FKKINVBILL [E1]."


def test_a_translation_failure_never_loses_the_answer(monkeypatch):
    monkeypatch.setattr(languages, "_complete", lambda prompt, system: None)
    assert languages.answer_to_language("Original answer.", "hi") == "Original answer."
    assert languages.question_to_english("FI-CA क्या है?", "hi") == "FI-CA क्या है?"


def test_direct_answers_are_told_to_write_in_the_chosen_language():
    def build():
        yield llm_module.llm_service._build_sprint_prompt("FI-CA क्या है?", [], None)

    in_hindi = list(languages.scoped(build(), "hi"))[0]
    assert "ANSWER LANGUAGE" in in_hindi and "Hindi" in in_hindi
    in_english = list(languages.scoped(build(), "en"))[0]
    assert "ANSWER LANGUAGE" not in in_english


def test_question_language_defaults_to_the_answer_language_and_can_differ(fx):
    headers = headers_for(fx.user().id)
    assert client.get("/api/auth/me", headers=headers).json()["question_language"] is None
    chosen = client.patch("/api/auth/me/question-language", headers=headers, json={"language": "te"})
    assert chosen.status_code == 200 and chosen.json()["question_language"] == "te"
    assert chosen.json()["preferred_language"] == "en"  # the answer language is independent
    assert client.patch("/api/auth/me/question-language", headers=headers, json={"language": "xx"}).status_code == 422
    cleared = client.patch("/api/auth/me/question-language", headers=headers, json={"language": None})
    assert cleared.status_code == 200 and cleared.json()["question_language"] is None
    assert client.patch("/api/auth/me/question-language", json={"language": "te"}).status_code in (401, 403)


def test_question_language_of_prefers_the_explicit_choice():
    from types import SimpleNamespace

    assert languages.question_language_of(SimpleNamespace(preferred_language="hi", question_language=None)) == "hi"
    assert languages.question_language_of(SimpleNamespace(preferred_language="hi", question_language="te")) == "te"
    assert languages.question_language_of(SimpleNamespace(preferred_language="hi", question_language="en")) == "en"
    assert languages.question_language_of(SimpleNamespace(preferred_language=None, question_language="xx")) == "en"


# ---- the language follows the message, not only Settings ----------------------------------------------------

import json
from types import SimpleNamespace

from backend.app.api import messages as messages_api
from backend.app.services.conversation_memory_service import conversation_memory_service
from backend.app.services.llm_service import llm_service
from backend.tests.test_chat_modes import FakeSession, _sse_events

TELUGU_QUESTION = "సాప్ గురించి వివరించండి"


def test_the_message_decides_the_language_before_settings():
    resolve = languages.resolve_answer_language
    assert resolve("en", "Telugu", "explain me about the sap in telugu") == ("te", None)  # explicit request
    assert resolve("te", "English", "explain sap") == ("en", None)  # asking for English beats a Telugu setting
    assert resolve("en", "", TELUGU_QUESTION) == ("te", None)  # written in Telugu script
    assert resolve("hi", "", "what is dunning") == ("hi", None)  # nothing in the message: Settings
    assert resolve("en", "Swahili", "explain sap in swahili") == ("en", "Swahili")  # not supported
    assert languages.detect_script_language("एसएपी क्या है", "mr") == "mr"  # shared script follows Settings
    assert languages.code_for("తెలుగు") == "te" and languages.code_for("Klingon") is None


def test_the_unsupported_language_reply_points_to_settings():
    notice = languages.unsupported_notice("Swahili")
    assert "Swahili" in notice and "Settings" in notice and "Telugu" in notice
    failed = languages.translation_failed_note("te")
    assert "Settings" in failed and "Telugu" in failed


def _analysis(monkeypatch, **fields):
    from backend.tests.test_intent_analysis import _fake_model
    return _fake_model(monkeypatch, **fields)


def _sprint_done(monkeypatch, question, preferred="en", is_admin=True):
    monkeypatch.setattr(conversation_memory_service, "update_rolling_summary", lambda *_a, **_k: None)
    session = FakeSession()
    monkeypatch.setattr(messages_api, "SessionLocal", lambda: session)
    monkeypatch.setattr(messages_api, "issue_stream_token", lambda *_args: "signed-stream")
    user = SimpleNamespace(id="user-id", is_admin=is_admin, preferred_language=preferred, question_language=None)
    response = messages_api.handle_sprint_streaming_response(
        "chat-id", question, chat_history=[], current_user=user, attachments=[], user_message_id="m1")
    return next(e for e in _sse_events(response) if e["type"] == "done")


def test_sprint_answers_in_the_language_asked_for_in_the_message(monkeypatch):
    _analysis(monkeypatch, requested_language="Telugu", corrected_question="Explain me about the SAP in Telugu")
    seen = {}

    def fake_stream(*_a, **_k):
        seen["block"] = languages.prompt_block()
        yield "తెలుగు సమాధానం"

    monkeypatch.setattr(llm_service, "stream_sprint_reply", fake_stream)
    done = _sprint_done(monkeypatch, "explain me about the sap in telugu", preferred="en")  # Settings say English
    assert "Telugu" in seen["block"] and done["answer"] == "తెలుగు సమాధానం"


def test_sprint_answers_in_the_script_the_question_is_written_in(monkeypatch):
    _analysis(monkeypatch, corrected_question=TELUGU_QUESTION)
    seen = {}

    def fake_stream(*_a, **_k):
        seen["block"] = languages.prompt_block()
        yield "సమాధానం"

    monkeypatch.setattr(llm_service, "stream_sprint_reply", fake_stream)
    _sprint_done(monkeypatch, TELUGU_QUESTION, preferred="en")
    assert "Telugu" in seen["block"]


def test_sprint_tells_the_user_to_use_settings_for_an_unsupported_language(monkeypatch):
    _analysis(monkeypatch, requested_language="Swahili")

    def no_model(*_a, **_k):
        raise AssertionError("no model call expected")

    monkeypatch.setattr(llm_service, "stream_sprint_reply", no_model)
    done = _sprint_done(monkeypatch, "explain sap in swahili")
    assert "Swahili" in done["answer"] and "Settings" in done["answer"]
    assert "token_usage" not in done  # free, like any declined request


def _scholar_events(monkeypatch, question, events, preferred="en"):
    from unittest.mock import MagicMock
    monkeypatch.setattr(messages_api, "SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(messages_api, "_invoke_rag_stream", lambda *a, **k: iter(events))
    monkeypatch.setattr(messages_api, "_saved_memory_text", lambda *a, **k: "")
    monkeypatch.setattr(conversation_memory_service, "embed_message_content", lambda *_a, **_k: None)
    monkeypatch.setattr(conversation_memory_service, "update_rolling_summary", lambda *_a, **_k: None)
    user = SimpleNamespace(id="u1", is_admin=True, preferred_language=preferred, question_language=None)
    response = messages_api.handle_streaming_response(
        "chat-1", question, chat_history=[], current_user=user, user_message_id="m1", conversation_chars=0)
    return _sse_events(response)


def test_scholar_translates_when_the_message_asks_for_a_language(monkeypatch):
    monkeypatch.setattr(languages, "try_answer_to_language", lambda text, lang: f"[{lang}] {text}")
    events = _scholar_events(monkeypatch, "explain sap brim in telugu", [
        {"type": "answer_language", "requested": "Telugu"},
        {"type": "token", "content": "SAP BRIM is billing."},
        {"type": "done", "answer": "SAP BRIM is billing.", "source_type": "knowledge_base", "grounding_score": 0.9},
    ])
    assert events[-1]["answer"] == "[te] SAP BRIM is billing."
    assert not [e for e in events if e["type"] == "answer_language"]  # internal, never sent to the browser


def test_scholar_says_so_when_a_requested_translation_is_not_possible(monkeypatch):
    monkeypatch.setattr(languages, "try_answer_to_language", lambda text, lang: None)  # translation is down
    events = _scholar_events(monkeypatch, TELUGU_QUESTION, [
        {"type": "token", "content": "SAP BRIM is billing."},
        {"type": "done", "answer": "SAP BRIM is billing.", "source_type": "knowledge_base", "grounding_score": 0.9},
    ])
    answer = events[-1]["answer"]
    assert answer.startswith("SAP BRIM is billing.") and "Settings" in answer and "Telugu" in answer


def test_scholar_declines_an_unsupported_language_with_the_settings_notice(monkeypatch):
    _analysis(monkeypatch, requested_language="Swahili")
    from backend.app.services.rag_service import rag_service
    events = list(rag_service.stream_query(None, "explain sap in swahili", chat_history=[], user=None, attachments=None))
    done = events[-1]
    assert done["type"] == "done" and done["source_type"] == "refusal"
    assert "Swahili" in done["answer"] and "Settings" in done["answer"]


def test_scholar_reports_the_requested_language_to_the_chat_handler(monkeypatch):
    _analysis(monkeypatch, category="general", small_talk=True, about_sap=False, requested_language="Hindi")
    from backend.app.services.rag_service import rag_service
    monkeypatch.setattr(llm_service, "stream_emotional_casual_reply", lambda *a, **k: iter(["Hello!"]))
    events = list(rag_service.stream_query(None, "hello in hindi", chat_history=[], user=None, attachments=None))
    assert {"type": "answer_language", "requested": "Hindi"} in events
