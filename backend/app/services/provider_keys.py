"""Fallback API keys for the LLM providers.

Each provider (groq, gemini, openrouter) keeps its normal key (``GROQ_API_KEY`` ...) and may have up to
three more, ``GROQ_API_KEY_FALLBACK_1`` to ``_3`` and the same for the other providers. A key is a *slot*:
every slot has its own cooldown record, so a key that was rejected (401/403), is out of money (402) or hit
its quota (429) is skipped for a while while the next key of the same provider answers. Only when every key
of a provider is unusable does the request move on to the next provider.

Nothing here logs or returns a key value: a slot's ``key`` is excluded from ``repr`` and logs use the slot's
ordinal ("key 2 of 3").
"""

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, List, Optional

from backend.app.config import settings

logger = logging.getLogger(__name__)

PROVIDERS = ("groq", "gemini", "openrouter")
FALLBACK_KEY_COUNT = 3
# Values people leave in templates; they must never be sent to a provider.
_PLACEHOLDER_WORDS = {"disabled", "none", "off", "null", "changeme", "change-me", "your-key-here"}


@dataclass(frozen=True)
class KeySlot:
    """One API key of one provider. ``name`` is the cooldown namespace (the provider itself for the primary
    key, so existing behaviour and tests are unchanged, and ``provider#N`` for fallback key N)."""

    provider: str
    name: str
    key: str = field(repr=False)
    ordinal: int  # 1 = the normal key, 2 to 4 = fallback keys 1 to 3


def _clean(provider: str, raw: object) -> str:
    value = str(raw or "").strip().strip("\"'")
    if provider == "gemini":
        value = value.lstrip(". ")  # the primary Gemini key has always been cleaned this way
    lowered = value.lower()
    if not value or lowered in _PLACEHOLDER_WORDS or lowered.startswith(("replace", "your_", "your-")):
        return ""
    return value


def fallback_keys(provider: str) -> List[str]:
    """The configured fallback keys of a provider in order; unset, placeholder and repeated values dropped."""
    found: List[str] = []
    for number in range(1, FALLBACK_KEY_COUNT + 1):
        value = _clean(provider, getattr(settings, f"{provider.upper()}_API_KEY_FALLBACK_{number}", ""))
        if value and value not in found:
            found.append(value)
    return found


def key_slots(provider: str, primary: Optional[str]) -> List[KeySlot]:
    """All usable keys of a provider, primary first. A provider with only fallback keys still works."""
    slots: List[KeySlot] = []
    seen = set()
    candidates = [("", _clean(provider, primary))] + [
        (f"#{index}", value) for index, value in enumerate(fallback_keys(provider), start=1)
    ]
    for suffix, value in candidates:
        if not value or value in seen:
            continue
        seen.add(value)
        slots.append(KeySlot(provider=provider, name=f"{provider}{suffix}", key=value, ordinal=len(slots) + 1))
    return slots


class _GeminiKeyGate:
    """The Gemini SDK keeps one process-wide API key (``genai.configure``). Calls that use the *same* key may
    overlap freely; a call with a *different* key waits (bounded) until the others finish, so a request never
    runs with another key's configuration and its failures are charged to the right slot."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active_key: Optional[str] = None
        self._in_flight = 0

    @contextmanager
    def use(self, key: str, wait_seconds: float = 30.0) -> Iterator[None]:
        with self._condition:
            self._condition.wait_for(
                lambda: self._in_flight == 0 or self._active_key == key, timeout=wait_seconds
            )
            # After a timeout we proceed rather than hang the request; the rare overlap only risks one call
            # being counted against the neighbouring key.
            self._active_key = key
            self._in_flight += 1
        try:
            yield
        finally:
            with self._condition:
                self._in_flight -= 1
                if self._in_flight == 0:
                    self._active_key = None
                self._condition.notify_all()


gemini_key_gate = _GeminiKeyGate()
