"""Live token counter and "tokens saved" estimate: the meter only counts inside a metered answer stream,
both chat modes stream running `usage` events and end with `token_usage`, and regular users receive the
counts without any model or provider detail (offline)."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.app.api import messages as messages_api
from backend.app.services import token_meter
from backend.app.services.conversation_memory_service import conversation_memory_service
from backend.app.services.llm_service import llm_service
from backend.app.services.public_payload import public_done_event
from backend.tests.offline_rag import DEFAULT_ANSWER, KB_ROWS, RAG_QUESTION, offline_rag
from backend.tests.test_chat_modes import FakeSession, _sse_events


def test_estimate_rounds_up_to_whole_tokens():
    assert token_meter.estimate_tokens(0) == 0
    assert token_meter.estimate_tokens(1) == 1
    assert token_meter.estimate_tokens(8) == 2
    assert token_meter.estimate_tokens(9) == 3


def test_savings_never_go_below_zero():
    meter = token_meter.TokenMeter(llm_chars=4000, system_chars=400)
    summary = meter.summary(question="q" * 40, answer="a" * 400, conversation_chars=0)
    assert summary["used"] == 1000
    assert summary["baseline"] == 210  # instructions + question + answer, no conversation or passages
    assert summary["saved"] == 0


def test_savings_are_baseline_minus_used():
    meter = token_meter.TokenMeter(llm_chars=4000, system_chars=400, retrieved_chars=40000)
    summary = meter.summary(question="q" * 40, answer="a" * 400, conversation_chars=8000)
    assert summary == {"used": 1000, "baseline": 12210, "saved": 11210}


def test_meter_records_only_while_a_stream_step_runs():
    meter = token_meter.TokenMeter()
    token_meter.stage_prompt("outside the stream", "ignored")

    def steps():
        token_meter.stage_prompt("p" * 40, "s" * 20)
        yield "first"
        token_meter.record_reply("r" * 40)
        token_meter.record_retrieved([{"chunk_text": "c" * 80}, {"content": "w" * 20}, {"title": "no text"}])
        yield "second"

    assert list(token_meter.metered(steps(), meter)) == ["first", "second"]
    token_meter.record_reply("after the stream")
    assert meter.llm_chars == 100 and meter.system_chars == 20 and meter.retrieved_chars == 100


def test_meter_reply_counts_plain_and_streamed_replies():
    @token_meter.meter_reply
    def call():
        return "x" * 12

    @token_meter.meter_reply
    def stream():
        yield "y" * 4
        yield "z" * 4

    def steps():
        yield call()
        yield "".join(stream())

    meter = token_meter.TokenMeter()
    list(token_meter.metered(steps(), meter))
    assert meter.llm_chars == 20


def test_external_prompt_counts_once_the_provider_replies(monkeypatch):
    monkeypatch.setattr("backend.app.services.audit_log.record", lambda *_a, **_k: None)
    meter = token_meter.TokenMeter()

    def steps():
        yield llm_service._prepare_external_prompts("groq", "model", "p" * 30, "s" * 10)
        yield meter.llm_chars
        token_meter.record_reply("ok")
        yield "answered"

    assert list(token_meter.metered(steps(), meter))[1] == 0, "nothing counted before a reply"
    assert meter.llm_chars == 42 and meter.system_chars == 10


def test_failed_attempts_are_not_counted():
    def steps():
        token_meter.stage_prompt("a" * 400, "s" * 100)  # rate-limited, no reply
        token_meter.record_reply("")  # empty reply: still not processed
        token_meter.stage_prompt("b" * 40, "s" * 10)  # the retry that answered
        token_meter.record_reply("r" * 20)
        token_meter.stage_prompt("c" * 400)  # every later attempt failed
        yield "done"

    meter = token_meter.TokenMeter()
    list(token_meter.metered(steps(), meter))
    assert meter.llm_chars == 70 and meter.system_chars == 10


def test_continuation_counts_both_requests():
    def steps():
        token_meter.stage_prompt("p" * 40)  # first request, reply cut off at the length limit
        token_meter.commit_staged()
        token_meter.stage_prompt("c" * 20)  # continuation request
        token_meter.record_reply("r" * 60)  # truncated reply + continuation, returned together
        yield "done"

    meter = token_meter.TokenMeter()
    list(token_meter.metered(steps(), meter))
    assert meter.llm_chars == 120


def _fake_sprint_stream(*_args, **_kwargs):
    token_meter.stage_prompt("p" * 2000, "s" * 400)
    for piece in ("## SAP answer\n\n", "- Direct response\n"):
        token_meter.record_reply(piece)
        yield piece


def _sprint_events(monkeypatch, *, user, memory=None, conversation_chars=None):
    session = FakeSession()
    monkeypatch.setattr(messages_api, "SessionLocal", lambda: session)
    monkeypatch.setattr(messages_api, "issue_stream_token", lambda *_args: "signed-stream")
    monkeypatch.setattr(messages_api.llm_service, "stream_sprint_reply", _fake_sprint_stream)
    monkeypatch.setattr(conversation_memory_service, "update_rolling_summary", lambda *_a, **_k: None)
    response = messages_api.handle_sprint_streaming_response(
        "chat-id", "Explain SAP BRIM", chat_history=[], current_user=user, attachments=[],
        user_message_id="user-message-id", memory=memory, conversation_chars=conversation_chars,
    )
    return _sse_events(response)


def test_sprint_stream_reports_running_usage_and_final_savings(monkeypatch):
    events = _sprint_events(
        monkeypatch, user=SimpleNamespace(id="user-id", is_admin=True), conversation_chars=40000
    )
    usage = [event["tokens"] for event in events if event["type"] == "usage"]
    done = events[-1]

    assert done["type"] == "done"
    assert usage and usage == sorted(usage), "the counter only counts up"
    assert usage[-1] == done["token_usage"]["used"] > 0
    assert done["token_usage"]["saved"] == done["token_usage"]["baseline"] - done["token_usage"]["used"] > 0
    assert events.index(next(e for e in events if e["type"] == "usage")) < events.index(
        next(e for e in events if e["type"] == "token")
    ), "the counter starts before the answer text"


def test_short_sprint_chat_can_save_nothing(monkeypatch):
    events = _sprint_events(monkeypatch, user=SimpleNamespace(id="user-id", is_admin=True), conversation_chars=0)
    assert events[-1]["token_usage"]["saved"] == 0


def test_memory_answer_uses_no_llm_tokens(monkeypatch):
    events = _sprint_events(
        monkeypatch, user=SimpleNamespace(id="user-id", is_admin=True),
        memory={"answer": "Your first question was about dunning."}, conversation_chars=2000,
    )
    usage = events[-1]["token_usage"]
    assert not [event for event in events if event["type"] == "usage"]
    assert usage["used"] == 0 and usage["saved"] == usage["baseline"] > 0


def test_a_declined_question_costs_no_tokens_and_shows_no_savings(monkeypatch):
    charged = []
    monkeypatch.setattr(messages_api, "_charge_tokens", lambda *a, **k: charged.append(a) or 1.0)
    monkeypatch.setattr(messages_api, "_sprint_route", lambda *a, **k: "refusal")
    events = _sprint_events(monkeypatch, user=SimpleNamespace(id="user-id", is_admin=False), conversation_chars=40000)
    done = events[-1]
    assert done["type"] == "done" and done["answer"].startswith("I can only assist with SAP")
    assert "token_usage" not in done and "token_balance" not in done  # no popup, no wallet change
    assert not charged


def test_regular_user_receives_the_counts_only(monkeypatch):
    events = _sprint_events(monkeypatch, user=SimpleNamespace(id="user-id", is_admin=False), conversation_chars=40000)
    assert set(events[-1]["token_usage"]) == {"used", "baseline", "saved", "cost"}

    event = public_done_event({
        "type": "done", "answer": "ok",
        "token_usage": {"used": 3, "baseline": 9, "saved": 6, "model": "secret-model", "provider": "groq"},
    })
    assert event["token_usage"] == {"used": 3, "baseline": 9, "saved": 6, "cost": 0.0}
    assert "token_usage" not in public_done_event({"type": "done", "answer": "ok"})


def test_scholar_baseline_includes_every_retrieved_passage():
    # Scholar generates the whole answer, verifies it, then replays it; stands in for the metered provider call.
    def metered_generate(query, evidence, source_type="knowledge_base", **kwargs):
        token_meter.stage_prompt("p" * 1200, "s" * 400)
        token_meter.record_reply(DEFAULT_ANSWER)
        return DEFAULT_ANSWER

    def run_stream(response):
        async def collect():
            return [chunk async for chunk in response.body_iterator]
        return asyncio.run(collect())

    user = SimpleNamespace(id="u1", is_admin=False)
    with offline_rag(), \
            patch("backend.app.services.rag_service.llm_service.generate_answer", side_effect=metered_generate), \
            patch.object(messages_api, "SessionLocal", return_value=MagicMock()), \
            patch.object(conversation_memory_service, "embed_message_content", return_value=None), \
            patch.object(conversation_memory_service, "update_rolling_summary", return_value=None):
        response = messages_api.handle_streaming_response(
            "chat-1", RAG_QUESTION, chat_history=[], current_user=user, user_message_id="m1", conversation_chars=0
        )
        body = "".join(c if isinstance(c, str) else c.decode() for c in run_stream(response))

    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    done = events[-1]
    usage = [event["tokens"] for event in events if event["type"] == "usage"]
    passages = token_meter.estimate_tokens(sum(len(row["chunk_text"]) for row in KB_ROWS))

    assert done["type"] == "done", events
    assert usage and usage[-1] == done["token_usage"]["used"] > 0
    assert done["token_usage"]["baseline"] >= passages


def test_conversation_size_covers_every_earlier_message():
    import uuid

    from backend.app.database import SessionLocal, init_db
    from backend.app.models.chat import Chat
    from backend.app.models.message import Message
    from backend.app.models.user import User
    from backend.app.security.auth import hash_password

    init_db()
    db = SessionLocal()
    user = User(email=f"tokens_{uuid.uuid4().hex[:8]}@example.com", name="Tokens", is_admin=False,
                password_hash=hash_password(uuid.uuid4().hex), role="consultant")
    db.add(user)
    db.flush()
    chat = Chat(user_id=user.id, title="Token test")
    db.add(chat)
    db.flush()
    earlier = [Message(chat_id=chat.id, role=role, content=text)
               for role, text in [("user", "a" * 30), ("assistant", "b" * 300)] * 15]
    current = Message(chat_id=chat.id, role="user", content="c" * 50)
    db.add_all(earlier + [current])
    db.commit()
    try:
        # All 30 earlier messages count, beyond the 20-message window Clyptus sends; the new question does not.
        assert messages_api._conversation_chars(db, chat.id, current.id) == 15 * 330
        assert messages_api._conversation_chars(db, "no-such-chat") == 0
    finally:
        db.query(User).filter(User.id == user.id).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_sprint_saves_tokens_from_the_fifth_question(monkeypatch):
    def measured_stream(prompt, system_prompt, regeneration_index=None):
        token_meter.stage_prompt(prompt, system_prompt)
        token_meter.record_reply("Answer.")
        yield "Answer."

    monkeypatch.setattr(messages_api.llm_service, "_stream_configured", measured_stream)
    session = FakeSession()
    monkeypatch.setattr(messages_api, "SessionLocal", lambda: session)
    monkeypatch.setattr(messages_api, "issue_stream_token", lambda *_args: "signed-stream")
    monkeypatch.setattr(conversation_memory_service, "update_rolling_summary", lambda *_a, **_k: None)
    history = [{"id": f"m{i}", "role": "user" if i % 2 == 0 else "assistant", "content": "SAP billing detail. " * 100}
               for i in range(8)]  # four earlier questions and answers: the oldest two messages go as excerpts

    response = messages_api.handle_sprint_streaming_response(
        "chat-id", "How is the invoice posted to FI-CA?", chat_history=history,
        current_user=SimpleNamespace(id="user-id", is_admin=False), attachments=[], user_message_id="q",
        memory={}, conversation_chars=sum(len(item["content"]) for item in history),
    )
    usage = _sse_events(response)[-1]["token_usage"]
    assert usage["saved"] > 0, usage
