"""Capacity and failure isolation (services/capacity.py, detached_stream.py, main.py).

Many users at once must neither take the service down nor let one failing request or answer affect another:
the number of running answers is bounded (clear 503/429 beyond it), a slot lasts as long as the answer and is
always given back, health checks are not starved by busy answer threads, a full connection pool answers 503,
and a failing task is contained. Runs against the *_test database; no provider is called."""

import asyncio
import gc
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI, Header
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import TimeoutError as ConnectionPoolTimeout
from sqlalchemy.orm import Session, sessionmaker

from backend.app import database, main
from backend.app.config import settings
from backend.app.services import capacity
from backend.app.services.capacity import (
    Bulkhead,
    CapacityExceeded,
    answer_bulkhead,
    answer_capacity,
    release_request_connection,
    sizing_warning,
)
from backend.app.services.detached_stream import detached


@pytest.fixture(autouse=True)
def restore_bulkhead():
    yield
    answer_bulkhead.configure(settings.MAX_CONCURRENT_ANSWERS, settings.ANSWER_QUEUE_MAX,
                              settings.ANSWER_QUEUE_WAIT_SECONDS, settings.MAX_CONCURRENT_ANSWERS_PER_USER)
    gc.collect()


# ------------------------------------------------------------------------------------------- the bulkhead

def test_never_more_answers_run_than_the_limit_even_under_a_burst():
    bulkhead = Bulkhead("t", max_concurrent=3, max_waiting=100, wait_seconds=10)
    running, peak, lock = 0, 0, threading.Lock()

    def worker():
        nonlocal running, peak
        ticket = bulkhead.acquire()
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.03)
        with lock:
            running -= 1
        ticket.release()

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert peak <= 3 and peak >= 2
    assert bulkhead.stats()["active"] == 0 and bulkhead.stats()["waiting"] == 0


def test_a_full_queue_rejects_immediately_with_503_and_a_retry_hint():
    bulkhead = Bulkhead("t", max_concurrent=1, max_waiting=0, wait_seconds=5)
    held = bulkhead.acquire()
    started = time.monotonic()
    with pytest.raises(CapacityExceeded) as caught:
        bulkhead.acquire()
    assert time.monotonic() - started < 0.5  # no waiting when the queue is full
    assert caught.value.status == 503 and caught.value.retry_after > 0
    held.release()
    bulkhead.acquire().release()  # capacity is back


def test_a_waiting_request_gets_the_slot_when_one_is_released_and_gives_up_after_the_wait():
    bulkhead = Bulkhead("t", max_concurrent=1, max_waiting=2, wait_seconds=0.4)
    held = bulkhead.acquire()
    threading.Timer(0.1, held.release).start()
    bulkhead.acquire().release()  # waited ~0.1 s, then got the slot
    held = bulkhead.acquire()
    started = time.monotonic()
    with pytest.raises(CapacityExceeded):
        bulkhead.acquire()
    assert 0.3 < time.monotonic() - started < 2.0
    held.release()


def test_one_user_cannot_take_every_slot():
    bulkhead = Bulkhead("t", max_concurrent=10, max_waiting=5, wait_seconds=1, per_owner=2)
    first, second = bulkhead.acquire("alice"), bulkhead.acquire("alice")
    with pytest.raises(CapacityExceeded) as caught:
        bulkhead.acquire("alice")
    assert caught.value.status == 429
    bulkhead.acquire("bob").release()  # other users are unaffected
    first.release()
    bulkhead.acquire("alice").release()
    second.release()
    assert bulkhead.stats()["rejected_per_user"] == 1


def test_release_is_idempotent_and_the_limit_can_be_switched_off():
    bulkhead = Bulkhead("t", max_concurrent=1, max_waiting=0, wait_seconds=0)
    ticket = bulkhead.acquire()
    ticket.release()
    ticket.release()
    assert bulkhead.stats()["active"] == 0
    bulkhead.configure(0, 0, 0)
    tickets = [bulkhead.acquire("u") for _ in range(50)]  # limit off: nothing is counted or rejected
    assert bulkhead.stats()["active"] == 0 and len(tickets) == 50


def test_a_slot_that_was_never_released_is_reclaimed():
    bulkhead = Bulkhead("t", max_concurrent=1, max_waiting=0, wait_seconds=0)
    bulkhead.acquire("alice")  # the ticket is dropped without release()
    gc.collect()
    assert bulkhead.stats()["active"] == 0
    bulkhead.acquire("alice").release()


def test_drain_waits_for_running_answers_then_reports_the_rest():
    bulkhead = Bulkhead("t", max_concurrent=2, max_waiting=0, wait_seconds=0)
    ticket = bulkhead.acquire()
    threading.Timer(0.15, ticket.release).start()
    assert bulkhead.drain(2.0) == 0
    stuck = bulkhead.acquire()
    assert bulkhead.drain(0.1) == 1
    stuck.release()


def test_sizing_warning_flags_a_limit_the_connection_pool_cannot_serve(monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_POOL_SIZE", 5)
    monkeypatch.setattr(settings, "DATABASE_MAX_OVERFLOW", 5)
    monkeypatch.setattr(settings, "MAX_CONCURRENT_ANSWERS", 8)
    assert "MAX_CONCURRENT_ANSWERS=8" in sizing_warning()
    monkeypatch.setattr(settings, "MAX_CONCURRENT_ANSWERS", 4)
    assert sizing_warning() is None
    monkeypatch.setattr(settings, "MAX_CONCURRENT_ANSWERS", 0)
    assert sizing_warning() is None


# ------------------------------------------- the decorator on real endpoints (FastAPI), concurrent requests

STREAM_OBSERVED: list = []


def build_app(holder: threading.Event, started: threading.Event):
    app = FastAPI()

    def user(x_user: str = Header("anon")):
        return SimpleNamespace(id=x_user)

    @app.get("/work")
    @answer_capacity
    def work(current_user=Depends(user)):
        started.set()
        holder.wait(10)
        return {"ok": True}

    @app.get("/boom")
    @answer_capacity
    def boom(current_user=Depends(user)):
        raise RuntimeError("endpoint failed")

    @app.get("/stream")
    @answer_capacity
    def stream(current_user=Depends(user)):
        def chunks():
            for _ in range(3):
                time.sleep(0.05)
                STREAM_OBSERVED.append(answer_bulkhead.stats()["active"])  # slots in use while streaming
                yield "x"

        return StreamingResponse(chunks())

    return app


def run_async(coro):
    return asyncio.run(coro)


def test_endpoint_slot_busy_returns_503_with_retry_after_then_recovers():
    answer_bulkhead.configure(1, 0, 0.1, 0)
    hold, started = threading.Event(), threading.Event()
    app = build_app(hold, started)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            first = asyncio.create_task(client.get("/work"))
            await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
            busy = await client.get("/work")
            hold.set()
            done = await first
            again = await client.get("/work")
            return busy, done, again

    busy, done, again = run_async(scenario())
    assert busy.status_code == 503 and int(busy.headers["Retry-After"]) > 0
    assert "try again" in busy.json()["detail"].lower()
    assert done.status_code == 200 and again.status_code == 200  # the slot was given back


def test_per_user_limit_returns_429_for_that_user_only():
    answer_bulkhead.configure(5, 5, 0.1, 1)
    hold, started = threading.Event(), threading.Event()
    app = build_app(hold, started)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            first = asyncio.create_task(client.get("/work", headers={"x-user": "alice"}))
            await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
            second_alice = await client.get("/work", headers={"x-user": "alice"})
            started.clear()
            bob = asyncio.create_task(client.get("/work", headers={"x-user": "bob"}))
            await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
            hold.set()
            return second_alice, await first, await bob

    second_alice, first, bob = run_async(scenario())
    assert second_alice.status_code == 429 and first.status_code == 200 and bob.status_code == 200


def test_an_endpoint_that_fails_gives_its_slot_back_and_the_next_request_works():
    answer_bulkhead.configure(1, 0, 0.1, 0)
    app = build_app(threading.Event(), threading.Event())
    client = TestClient(app, raise_server_exceptions=False)
    for _ in range(3):
        assert client.get("/boom").status_code == 500
    assert answer_bulkhead.stats()["active"] == 0
    assert client.get("/stream").status_code == 200


def test_a_streamed_answer_keeps_its_slot_until_the_stream_has_ended():
    answer_bulkhead.configure(1, 0, 0.05, 0)
    app = build_app(threading.Event(), threading.Event())
    STREAM_OBSERVED.clear()
    assert TestClient(app).get("/stream").status_code == 200
    # While the generator ran, after the endpoint function had returned, the slot was still held ...
    assert STREAM_OBSERVED == [1, 1, 1]
    # ... and it is given back once the response has been sent.
    deadline = time.monotonic() + 3
    while answer_bulkhead.stats()["active"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert answer_bulkhead.stats()["active"] == 0


# --------------------------------------- slot ownership by the background producer (detached answers)

def test_a_detached_answer_keeps_its_slot_after_the_browser_left_and_gives_it_back_at_the_end():
    answer_bulkhead.configure(2, 0, 0.1, 0)
    ticket = answer_bulkhead.acquire("alice")
    token = capacity._current_ticket.set(ticket)
    finished = threading.Event()

    def events():
        yield "first"
        time.sleep(0.4)  # still generating after the reader is gone
        yield "second"
        finished.set()

    try:
        stream = detached(events())
    finally:
        capacity._current_ticket.reset(token)
    assert ticket.detached
    assert next(iter(stream)) == "first"
    del stream  # the browser disconnected: nobody reads any more
    gc.collect()
    assert answer_bulkhead.stats()["active"] == 1  # the producer thread is still working
    assert finished.wait(5)
    deadline = time.monotonic() + 3
    while answer_bulkhead.stats()["active"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert answer_bulkhead.stats()["active"] == 0


def test_a_failing_answer_is_contained_releases_its_slot_and_does_not_disturb_others():
    answer_bulkhead.configure(3, 0, 0.1, 0)

    def failing():
        yield "partial"
        raise RuntimeError("provider exploded")

    def healthy():
        for piece in ("a", "b", "c"):
            time.sleep(0.02)
            yield piece

    tickets, outputs = [], {}

    def run(name, events):
        ticket = answer_bulkhead.acquire(name)
        token = capacity._current_ticket.set(ticket)
        try:
            stream = detached(events())
        finally:
            capacity._current_ticket.reset(token)
        outputs[name] = list(stream)

    threads = [threading.Thread(target=run, args=(name, fn)) for name, fn in
               (("bad", failing), ("good1", healthy), ("good2", healthy))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert outputs["good1"] == outputs["good2"] == ["a", "b", "c"]
    assert outputs["bad"] == ["partial"]  # the failure ended that stream only
    deadline = time.monotonic() + 3
    while answer_bulkhead.stats()["active"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert answer_bulkhead.stats()["active"] == 0


def test_a_stream_that_was_never_started_does_not_leak_its_slot():
    answer_bulkhead.configure(1, 0, 0.05, 0)
    ticket = answer_bulkhead.acquire("alice")
    token = capacity._current_ticket.set(ticket)
    try:
        stream = detached(iter(["x"]))  # created, never iterated: the client vanished before the first byte
    finally:
        capacity._current_ticket.reset(token)
    del ticket, stream
    gc.collect()
    assert answer_bulkhead.stats()["active"] == 0


# ------------------------------------------------------- the request's database connection during a stream

def _checked_out_during_stream(decorated: bool) -> int:
    app = FastAPI()

    def user():
        return SimpleNamespace(id="u")

    def endpoint(current_user=Depends(user), db: Session = Depends(database.get_db)):
        db.execute(text("select 1"))  # leaves a read transaction open, like the answer endpoints' last query

        def chunks():
            for _ in range(5):
                time.sleep(0.25)
                yield "x"

        return StreamingResponse(chunks())

    app.get("/s")(answer_capacity(endpoint) if decorated else endpoint)
    client = TestClient(app)
    seen, done = [], threading.Event()
    base = database.engine.pool.checkedout()

    def request():
        client.get("/s")
        done.set()

    threading.Thread(target=request, daemon=True).start()
    time.sleep(0.4)  # past the endpoint function, inside the stream
    while not done.is_set():
        seen.append(database.engine.pool.checkedout() - base)
        time.sleep(0.1)
    return max(seen)


def test_a_running_stream_does_not_pin_a_database_connection():
    assert _checked_out_during_stream(decorated=False) == 1  # the problem: one connection per active stream
    assert _checked_out_during_stream(decorated=True) == 0  # released before the stream starts


def test_releasing_the_connection_early_never_commits_unsaved_changes():
    class Session_:
        new, dirty, deleted = [object()], [], []
        expire_on_commit = True
        committed = False

        def in_transaction(self):
            return True

        def commit(self):
            self.committed = True

    session = Session_()
    release_request_connection(session)
    assert session.committed is False  # pending work is left alone
    release_request_connection(None)
    release_request_connection(SimpleNamespace())  # a stand-in object without session methods: no error


# -------------------------------------------------------------------- health, pool exhaustion, error handling

def test_health_stays_fast_while_every_worker_thread_is_busy_with_long_requests():
    app = FastAPI()
    app.add_api_route("/health", main.health_check)
    app.add_api_route("/health_sync_style", lambda: main._probe_dependencies())  # the old, thread-pool style

    @app.get("/slow")
    def slow():
        time.sleep(1.5)
        return {}

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=20) as client:
            busy = [asyncio.create_task(client.get("/slow")) for _ in range(60)]  # more than the 40-thread default
            await asyncio.sleep(0.3)
            started = time.monotonic()
            health = await client.get("/health")
            fast = time.monotonic() - started
            started = time.monotonic()
            old_style = await client.get("/health_sync_style")
            stalled = time.monotonic() - started
            await asyncio.gather(*busy)
            return health, fast, old_style, stalled

    health, fast, old_style, stalled = run_async(scenario())
    assert health.status_code == 200 and health.json()["status"] == "healthy"
    assert fast < 1.0, f"health took {fast:.2f}s"
    assert stalled > fast + 0.5, (fast, stalled)  # control: on the shared thread pool it queues behind the work


def test_a_hung_dependency_makes_health_report_unhealthy_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(settings, "HEALTH_CHECK_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(main, "_probe_dependencies", lambda: time.sleep(1.5))
    started = time.monotonic()
    response = TestClient(main.app).get("/health")
    assert response.status_code == 503 and response.json()["status"] == "unhealthy"
    assert time.monotonic() - started < 1.2


def test_an_exhausted_connection_pool_answers_503_busy_not_500():
    tiny = create_engine(database.engine.url, pool_size=1, max_overflow=0, pool_timeout=0.2)
    sessions = sessionmaker(bind=tiny)
    app = FastAPI()
    app.add_exception_handler(ConnectionPoolTimeout, main.connection_pool_exhausted_handler)
    gate, entered = threading.Event(), threading.Event()

    def db_dependency():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    @app.get("/q")
    def query(db: Session = Depends(db_dependency)):
        db.execute(text("select 1"))
        entered.set()
        gate.wait(5)
        return {}

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            first = asyncio.create_task(client.get("/q"))
            await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)
            second = await client.get("/q")
            gate.set()
            return second, await first

    second, first = run_async(scenario())
    tiny.dispose()
    assert second.status_code == 503 and second.headers["Retry-After"]
    assert "try again" in second.json()["detail"].lower()
    assert first.status_code == 200


def test_an_unexpected_error_returns_a_request_id_and_the_next_request_is_unaffected():
    app = FastAPI()
    app.add_exception_handler(Exception, main.global_exception_handler)

    @app.get("/boom")
    def boom():
        raise ValueError("secret internal detail")

    @app.get("/ok")
    def ok():
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    failed = client.get("/boom")
    assert failed.status_code == 500
    body = failed.json()
    assert "secret internal detail" not in failed.text  # nothing internal is shown
    assert body["request_id"] == failed.headers["X-Request-ID"] and len(body["request_id"]) == 12
    assert client.get("/ok").json() == {"ok": True}


# ---------------------------------------------------------------------------------------- web search timeout

def test_web_search_calls_carry_an_explicit_short_timeout(monkeypatch):
    from backend.app.services.search.tavily_provider import TavilySearchProvider

    calls = []

    class FakeTavily:
        def search(self, **kwargs):
            calls.append(kwargs)
            return {"results": [{"url": "https://help.sap.com/x", "content": "c", "title": "t"}], "images": []}

    monkeypatch.setattr(settings, "WEB_SEARCH_TIMEOUT_SECONDS", 7.0)
    provider = TavilySearchProvider(api_key="disabled")
    provider._client = FakeTavily()
    assert provider.search("posting area")[0].url == "https://help.sap.com/x"
    provider.search_images("posting area")
    assert [call["timeout"] for call in calls] == [7, 7]  # not the SDK's 60-second default
