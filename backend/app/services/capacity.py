"""Bounded concurrency for the expensive work (answers), so many users cannot take the service down.

Without a limit, a burst of questions starts one thread and holds one or two pooled database connections per
answer until the thread pool and the connection pool are exhausted; then every request, including health
checks and sign-in, waits and fails together. The `answer_bulkhead` limits how many answers run at once, lets
a short queue wait for a free slot, and turns everything beyond that into an immediate, clear 503 (service
busy) or 429 (this user already has enough answers running) with a Retry-After, instead of a pile-up.

A slot is held for the whole life of an answer, including an answer that keeps generating after the browser
left (services/detached_stream.py hands the slot to its producer thread, which releases it when it ends).
A slot that is never released (for example a stream that was never started) is reclaimed when its ticket is
garbage collected, so a bug elsewhere cannot leak capacity forever.
"""

import asyncio
import contextvars
import functools
import logging
import threading
import time
import weakref
from collections import Counter
from typing import Any, AsyncIterator, Callable, Dict, Optional

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from backend.app.config import settings

logger = logging.getLogger(__name__)

BUSY_DETAIL = "The assistant is handling many requests right now. Please try again in a few seconds."
USER_LIMIT_DETAIL = "You already have answers in progress. Wait for one to finish, then try again."


class CapacityExceeded(Exception):
    """No slot could be had in time. `status` is the HTTP status to answer with."""

    def __init__(self, status: int, detail: str, retry_after: int):
        super().__init__(detail)
        self.status, self.detail, self.retry_after = status, detail, retry_after


class AnswerTicket:
    """One occupied slot. `release()` is idempotent; `detached` is set when a background thread took over."""

    __slots__ = ("_bulkhead", "_owner", "_state", "detached", "__weakref__")

    def __init__(self, bulkhead: "Bulkhead", owner: Optional[str], counted: bool):
        self._bulkhead, self._owner = bulkhead, owner
        self._state: Dict[str, bool] = {"released": not counted}
        self.detached = False
        if counted:
            # Safety net: the callback must not reference the ticket itself.
            weakref.finalize(self, bulkhead._reclaim_leaked, owner, self._state)

    def release(self) -> None:
        self._bulkhead._release(self._owner, self._state)


class Bulkhead:
    def __init__(self, name: str, max_concurrent: int, max_waiting: int, wait_seconds: float, per_owner: int = 0):
        self.name = name
        self._cond = threading.Condition()
        self._active = 0
        self._waiting = 0
        self._owners: Counter = Counter()
        self.rejected_busy = 0
        self.rejected_owner = 0
        self.configure(max_concurrent, max_waiting, wait_seconds, per_owner)

    def configure(self, max_concurrent: int, max_waiting: int, wait_seconds: float, per_owner: int = 0) -> None:
        """A max_concurrent of 0 or less switches the limit off."""
        with self._cond:
            self.max_concurrent = int(max_concurrent)
            self.max_waiting = max(0, int(max_waiting))
            self.wait_seconds = max(0.0, float(wait_seconds))
            self.per_owner = max(0, int(per_owner))
            self._cond.notify_all()

    def acquire(self, owner: Optional[str] = None) -> AnswerTicket:
        if self.max_concurrent <= 0:
            return AnswerTicket(self, owner, counted=False)
        deadline = time.monotonic() + self.wait_seconds
        with self._cond:
            if self.per_owner and owner and self._owners[owner] >= self.per_owner:
                self.rejected_owner += 1
                raise CapacityExceeded(429, USER_LIMIT_DETAIL, retry_after=5)
            if self._active >= self.max_concurrent:
                if self._waiting >= self.max_waiting:
                    self.rejected_busy += 1
                    raise CapacityExceeded(503, BUSY_DETAIL, retry_after=5)
                self._waiting += 1
                try:
                    while self._active >= self.max_concurrent:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self.rejected_busy += 1
                            raise CapacityExceeded(503, BUSY_DETAIL, retry_after=10)
                        self._cond.wait(remaining)
                finally:
                    self._waiting -= 1
            self._active += 1
            if owner:
                self._owners[owner] += 1
        return AnswerTicket(self, owner, counted=True)

    def _release(self, owner: Optional[str], state: Dict[str, bool]) -> None:
        with self._cond:
            if state["released"]:
                return
            state["released"] = True
            self._active = max(0, self._active - 1)
            if owner:
                self._owners[owner] -= 1
                if self._owners[owner] <= 0:
                    del self._owners[owner]
            self._cond.notify()

    def _reclaim_leaked(self, owner: Optional[str], state: Dict[str, bool]) -> None:
        if not state["released"]:
            logger.warning("An answer slot was never released; reclaimed it (%s).", self.name)
            self._release(owner, state)

    def drain(self, timeout: float) -> int:
        """Wait for the running answers to finish (graceful shutdown); returns how many are still running."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while self._active > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            return self._active

    def stats(self) -> Dict[str, int]:
        with self._cond:
            return {"active": self._active, "waiting": self._waiting, "limit": self.max_concurrent,
                    "rejected_busy": self.rejected_busy, "rejected_per_user": self.rejected_owner}


def _from_settings() -> Bulkhead:
    return Bulkhead(
        "answers",
        max_concurrent=settings.MAX_CONCURRENT_ANSWERS,
        max_waiting=settings.ANSWER_QUEUE_MAX,
        wait_seconds=settings.ANSWER_QUEUE_WAIT_SECONDS,
        per_owner=settings.MAX_CONCURRENT_ANSWERS_PER_USER,
    )


answer_bulkhead = _from_settings()

_current_ticket: contextvars.ContextVar[Optional[AnswerTicket]] = contextvars.ContextVar("answer_ticket", default=None)


def take_current_ticket() -> Optional[AnswerTicket]:
    """Called by the code that hands an answer to a background thread: that thread now owns the slot and
    releases it when the answer ends, so the endpoint wrapper must not release it on return."""
    ticket = _current_ticket.get()
    if ticket is not None:
        ticket.detached = True
    return ticket


def release_request_connection(db: Any) -> None:
    """End the request session's open read transaction so its pooled connection is free while a long stream
    runs (otherwise every active stream pins one connection until it ends). Nothing is committed that was not
    already pending-free: with unflushed changes this does nothing. Never raises."""
    if db is None:
        return
    try:
        if db.new or db.dirty or db.deleted or not db.in_transaction():
            return
        keep = db.expire_on_commit
        db.expire_on_commit = False  # objects the stream reads later must not trigger a fresh query
        try:
            db.commit()
        finally:
            db.expire_on_commit = keep
    except Exception:
        logger.debug("Could not release the request database connection early", exc_info=True)


async def _hold(iterator: AsyncIterator, ticket: AnswerTicket) -> AsyncIterator:
    try:
        async for chunk in iterator:
            yield chunk
    finally:
        ticket.release()


def answer_capacity(endpoint: Callable) -> Callable:
    """Decorator for endpoints that start an answer (sync endpoints; place it under @router.post). The caller is
    already authenticated, so unauthenticated traffic never occupies a slot."""

    @functools.wraps(endpoint)
    def wrapper(*args, **kwargs):
        owner = getattr(kwargs.get("current_user"), "id", None)
        try:
            ticket = answer_bulkhead.acquire(owner)
        except CapacityExceeded as exc:
            logger.warning("Answer rejected (%s): %s", exc.status, answer_bulkhead.stats())
            raise HTTPException(status_code=exc.status, detail=exc.detail,
                                headers={"Retry-After": str(exc.retry_after)}) from None
        token = _current_ticket.set(ticket)
        try:
            response = endpoint(*args, **kwargs)
        except BaseException:
            ticket.release()
            raise
        finally:
            _current_ticket.reset(token)
        if isinstance(response, StreamingResponse):
            release_request_connection(kwargs.get("db"))
            if not ticket.detached:
                # Not handed to a background producer: the slot lasts until the response has been streamed.
                response.body_iterator = _hold(response.body_iterator, ticket)
            return response
        ticket.release()
        return response

    return wrapper


def sizing_warning() -> Optional[str]:
    """A concurrency limit above what the database pool can serve would move the pile-up into the pool."""
    pool = settings.DATABASE_POOL_SIZE + settings.DATABASE_MAX_OVERFLOW
    if settings.MAX_CONCURRENT_ANSWERS > 0 and settings.MAX_CONCURRENT_ANSWERS + 6 > pool:
        return (f"MAX_CONCURRENT_ANSWERS={settings.MAX_CONCURRENT_ANSWERS} leaves less than 6 of the {pool} database "
                "connections for sign-in, health checks and other requests; lower it or raise DATABASE_POOL_SIZE.")
    return None
