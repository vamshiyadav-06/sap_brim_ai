"""Answer streams that outlive the browser connection (owner request 2026-11-04).

An answer's SSE generator also saves the answer when it reaches its end. Starlette stops pulling from a
response's iterator when the client disconnects, which used to stop generation and lose the answer whenever
the user started a new chat, opened another one, reloaded or closed the tab. `detached` runs the generator in
its own thread and hands its events to the response through a queue: the generator always runs to the end,
and a disconnected response simply stops reading.

Only an explicit Stop cancels an answer: the save-partial endpoint registers the stream id with
`stream_cancellations.cancel`, and the runner closes the generator before its next event, so it saves nothing
further (the stopped text was saved by save-partial). Cancellation is per process: with several backend
processes, a Stop handled by another process does not reach the generator, which then finishes and upgrades
the saved partial answer (the existing "interrupted" upgrade in messages.py).
"""

import contextvars
import hashlib
import logging
import queue
import threading
import time
from typing import Dict, Iterator, Optional

from backend.app.services.capacity import take_current_ticket

logger = logging.getLogger(__name__)

# Matches the stream token lifetime (security/stream_tokens.py): an older id cannot be used to stop anything.
CANCELLATION_TTL_SECONDS = 2 * 60 * 60
_END = object()


class StreamCancellations:
    """Stream ids the user stopped, held as SHA-256 digests (the id is a credential) for a limited time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stopped: Dict[str, float] = {}

    @staticmethod
    def _digest(stream_id: str) -> str:
        return hashlib.sha256(stream_id.encode("utf-8")).hexdigest()

    def cancel(self, stream_id: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._stopped = {key: at for key, at in self._stopped.items() if now - at < CANCELLATION_TTL_SECONDS}
            self._stopped[self._digest(stream_id)] = now

    def is_cancelled(self, stream_id: Optional[str]) -> bool:
        if not stream_id:
            return False
        with self._lock:
            return self._digest(stream_id) in self._stopped

    def clear(self) -> None:
        with self._lock:
            self._stopped.clear()


stream_cancellations = StreamCancellations()


def detached(events: Iterator[str], stream_id: Optional[str] = None) -> Iterator[str]:
    """Yields `events` while running them to completion in a background thread, whether or not anyone is
    still reading. Context variables (request context, generation context) are carried into the thread.

    The capacity slot of the request (services/capacity.py), if any, is taken over here: the producer thread
    releases it when the answer ends, so an answer that outlives the browser still counts as running. This
    function is deliberately not a generator: it must read the slot in the endpoint's own context, while the
    generator below only starts later, in the response's context."""
    return _detached(events, stream_id, take_current_ticket())


def _detached(events: Iterator[str], stream_id: Optional[str], ticket) -> Iterator[str]:
    channel: "queue.Queue[object]" = queue.Queue()
    context = contextvars.copy_context()

    def produce() -> None:
        try:
            for event in events:
                channel.put(event)
                if stream_cancellations.is_cancelled(stream_id):
                    logger.info("Answer stream stopped by the user; generation cancelled")
                    break
        except Exception:  # the answer generators report their own errors; this only guards the thread
            logger.exception("Answer stream failed in the background")
        finally:
            try:
                close = getattr(events, "close", None)
                if close is not None:
                    close()
            except Exception:
                logger.exception("Closing the answer stream failed")
            finally:
                if ticket is not None:
                    ticket.release()
                channel.put(_END)

    threading.Thread(target=context.run, args=(produce,), name="answer-stream", daemon=True).start()
    while True:
        item = channel.get()
        if item is _END:
            return
        yield item  # type: ignore[misc]
