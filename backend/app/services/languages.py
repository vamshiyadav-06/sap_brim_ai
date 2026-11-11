"""Answer language (Settings): a user asks in their selected language or English and gets answers in the
selected language.

* Low (direct) answers: the model is told to write the whole answer in the language.
* High (verified) answers: grounding checks compare English words against English SAP documentation, so the
  verified draft stays English and only the finished answer is translated (citation markers kept).
* Questions in another language are translated to English first, because the SAP-topic check and the
  retrieval work on English. The user's original wording is what is stored in the chat.
* Any translation failure falls back to the original text: a user always gets an answer.
"""

import contextvars
import logging
import re
from typing import Dict, Iterable, Iterator, Optional, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")

DEFAULT_LANGUAGE = "en"
# code -> (English name, native name)
LANGUAGES: Dict[str, tuple] = {
    "en": ("English", "English"),
    "hi": ("Hindi", "हिन्दी"),
    "te": ("Telugu", "తెలుగు"),
    "ta": ("Tamil", "தமிழ்"),
    "kn": ("Kannada", "ಕನ್ನಡ"),
    "ml": ("Malayalam", "മലയാളം"),
    "mr": ("Marathi", "मराठी"),
    "bn": ("Bengali", "বাংলা"),
    "gu": ("Gujarati", "ગુજરાતી"),
    "pa": ("Punjabi", "ਪੰਜਾਬੀ"),
    "ur": ("Urdu", "اردو"),
    "es": ("Spanish", "Español"),
    "fr": ("French", "Français"),
    "de": ("German", "Deutsch"),
    "pt": ("Portuguese", "Português"),
    "it": ("Italian", "Italiano"),
    "nl": ("Dutch", "Nederlands"),
    "ar": ("Arabic", "العربية"),
    "zh": ("Chinese (Simplified)", "简体中文"),
    "ja": ("Japanese", "日本語"),
    "ko": ("Korean", "한국어"),
    "ru": ("Russian", "Русский"),
    "tr": ("Turkish", "Türkçe"),
    "id": ("Indonesian", "Bahasa Indonesia"),
}

_ENGLISH_HINTS = re.compile(
    r"\b(the|is|are|what|how|why|when|where|which|can|does|do|to|of|in|for|and|with|a|an|my|our|i|we|please|explain)\b",
    re.I,
)


def normalize(code: Optional[str]) -> str:
    code = (code or "").strip().lower()
    return code if code in LANGUAGES else DEFAULT_LANGUAGE


def question_language_of(user) -> str:
    """The language this user writes questions in: their explicit choice, else the answer language."""
    chosen = getattr(user, "question_language", None)
    if chosen:
        return normalize(chosen)
    return normalize(getattr(user, "preferred_language", None))


def english_name(code: str) -> str:
    return LANGUAGES[normalize(code)][0]


def is_english(code: Optional[str]) -> bool:
    return normalize(code) == DEFAULT_LANGUAGE


def looks_english(text: str) -> bool:
    """Plain ASCII text with common English words needs no translation (SAP terms alone do not count)."""
    text = text or ""
    if any(ord(ch) > 127 for ch in text if ch.isalpha()):
        return False
    return len(_ENGLISH_HINTS.findall(text)) >= 1


def _complete(prompt: str, system: str) -> Optional[str]:
    from backend.app.services.llm_service import llm_service  # avoid an import cycle

    try:
        reply = llm_service.generate_completion(prompt, system)
    except Exception as exc:
        logger.warning("Translation call failed (%s)", type(exc).__name__)
        return None
    reply = (reply or "").strip()
    return reply or None


def _has_non_ascii_letters(text: str) -> bool:
    return any(ord(ch) > 127 for ch in (text or "") if ch.isalpha())


def question_to_english(question: str, language: str) -> str:
    """The question in English for the SAP-topic check and retrieval; the original on any failure.
    A question written in another script is translated even when the user's setting is English."""
    if not question.strip() or looks_english(question):
        return question
    if is_english(language) and not _has_non_ascii_letters(question):
        return question
    translated = _complete(
        f"Translate this question to English. Reply with the translation only, no quotes or notes. Keep SAP "
        f"transaction codes, table and field names, product names and abbreviations (SAP BRIM, FI-CA, SOM, CC, CI) "
        f"unchanged.\n\n{question}",
        "You are a precise technical translator for SAP documentation.",
    )
    return translated if translated and len(translated) <= max(2000, len(question) * 6) else question


def try_answer_to_language(answer: str, language: str) -> Optional[str]:
    """A finished (English) answer written in `language`; None when the translation could not be made."""
    if is_english(language) or not answer.strip():
        return answer
    return _complete(
        f"Translate the following answer into {english_name(language)}. Keep the Markdown structure exactly "
        f"(headings, lists, tables, bold, code blocks). Do not translate or change: SAP transaction codes, table "
        f"and field names, program and class names, code, URLs, product names and abbreviations, or citation "
        f"markers such as [E1] or [E2][E3]. Reply with the translation only.\n\n{answer}",
        "You are a precise technical translator for SAP documentation. You never add, remove or change facts.",
    )


def answer_to_language(answer: str, language: str) -> str:
    """A finished (English) answer written in the user's language; the original on any failure."""
    return try_answer_to_language(answer, language) or answer


# --- which language this answer is written in -----------------------------------------------------------------

# Unicode blocks of the non-Latin scripts: the message itself shows the language. Latin-script languages
# (Spanish, French...) cannot be told from the script; they follow the user's setting or an explicit request.
_SCRIPTS = (
    (0x0C00, 0x0C7F, "te"), (0x0B80, 0x0BFF, "ta"), (0x0C80, 0x0CFF, "kn"), (0x0D00, 0x0D7F, "ml"),
    (0x0980, 0x09FF, "bn"), (0x0A80, 0x0AFF, "gu"), (0x0A00, 0x0A7F, "pa"), (0x0900, 0x097F, "hi"),
    (0x0600, 0x06FF, "ar"), (0x3040, 0x30FF, "ja"), (0x4E00, 0x9FFF, "zh"), (0xAC00, 0xD7AF, "ko"),
    (0x0400, 0x04FF, "ru"),
)


def detect_script_language(text: str, preferred: Optional[str] = None) -> Optional[str]:
    """The language a message is written in when it uses a non-Latin script, else None. Scripts shared by two
    supported languages (Devanagari: Hindi/Marathi, Arabic script: Arabic/Urdu) follow the user's setting."""
    counts: Dict[str, int] = {}
    for ch in text or "":
        cp = ord(ch)
        for low, high, code in _SCRIPTS:
            if low <= cp <= high:
                counts[code] = counts.get(code, 0) + 1
                break
    if not counts:
        return None
    code = max(counts, key=counts.get)
    preferred = normalize(preferred)
    if code == "hi" and preferred == "mr":
        return "mr"
    if code == "ar" and preferred == "ur":
        return "ur"
    return code


def code_for(value: Optional[str]) -> Optional[str]:
    """The supported language code for a code, English name or native name ("te", "Telugu", "తెలుగు"); else None."""
    wanted = (value or "").strip().lower()
    if not wanted:
        return None
    for code, (english, native) in LANGUAGES.items():
        if wanted in (code, english.lower(), native.lower(), english.lower().split(" (")[0]):
            return code
    return None


def resolve_answer_language(preferred: Optional[str], requested: Optional[str], text: str):
    """(language to answer in, unsupported language name or None) for one message.
    An explicit request in the message wins ("explain in Telugu"), then the script the message is written in,
    then the user's Settings language. A requested language that is not supported is returned by name."""
    asked = (requested or "").strip()
    if asked:
        code = code_for(asked)
        if code:
            return code, None
        return normalize(preferred), asked
    return detect_script_language(text, preferred) or normalize(preferred), None


def supported_names() -> str:
    return ", ".join(name for name, _native in LANGUAGES.values())


def unsupported_notice(name: str) -> str:
    """Fixed reply when the user asks for a language the assistant cannot write in."""
    name = re.sub(r"[^\w \-]", "", name or "").strip()[:40] or "that language"
    return (
        f"I can't write answers in {name} yet. To get answers in another language, go to Settings and change "
        f"the language to one of: {supported_names()}. I'm happy to answer in English until then."
    )


def translation_failed_note(language: str) -> str:
    name = english_name(language)
    return (
        f"\n\n_I couldn't write this answer in {name} just now. Please try again, or go to Settings and set "
        f"your language to {name}._"
    )


# --- prompt directive for direct (Low) answers ------------------------------------------------------

_active: contextvars.ContextVar[str] = contextvars.ContextVar("answer_language", default=DEFAULT_LANGUAGE)


def scoped(events: Iterable[T], language: str) -> Iterator[T]:
    """Steps `events` with the answer language active (set around every step, as token_meter does)."""
    iterator = iter(events)
    language = normalize(language)
    while True:
        token = _active.set(language)
        try:
            item = next(iterator)
        except StopIteration:
            return
        finally:
            _active.reset(token)
        yield item


def prompt_block() -> str:
    language = _active.get()
    if is_english(language):
        return ""
    name = english_name(language)
    return (
        f"ANSWER LANGUAGE: The user reads {name}. Write the whole answer in {name}, even if the question or the "
        f"retrieved text is in another language. Keep SAP transaction codes, table and field names, code, product "
        f"names and abbreviations (SAP BRIM, FI-CA, SOM, CC, CI) in their original form."
    )
