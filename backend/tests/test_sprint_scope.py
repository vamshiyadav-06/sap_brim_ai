"""Low (Sprint) mode answers only SAP questions: the ways a non-SAP question used to slip through."""

from backend.app.api import messages as messages_api
from backend.app.services import intent_analyzer as ia
from backend.app.services import user_memory
from backend.app.services.llm_service import SPRINT_SYSTEM_PROMPT

SAP_HISTORY = [
    {"id": "h1", "role": "user", "content": "How does dunning work in SAP FI-CA?"},
    {"id": "h2", "role": "assistant", "content": "Dunning in FI-CA runs per contract account."},
]

MEMORY = "I run S/4HANA 2023 on-premise. My team is Billing."


# ---- saved memory no longer opens the door to every question that says "I" or "my" ------------------------

def test_a_personal_pronoun_alone_does_not_make_a_question_about_the_saved_memory():
    for question in (
        "How do I cook pasta for my family?",
        "What is the best way to learn the guitar?",
        "Tell me a joke about my boss",
        "How do I plant tomatoes?",
    ):
        assert not user_memory.matches_query(MEMORY, question), question


def test_real_questions_about_the_user_still_match_their_saved_memory():
    for question in (
        "Which S/4HANA release do I run?",
        "Who am I?",
        "Do you remember my setup?",
        "What did I save about billing?",
    ):
        assert user_memory.matches_query(MEMORY, question), question
    assert not user_memory.matches_query("", "Who am I?")


# ---- the rule-based fallback: only a real follow-up inherits the SAP topic of the chat -------------------

def test_a_new_subject_does_not_inherit_the_sap_topic_of_the_chat():
    for question in ("Tell me a joke", "What is the weather in Paris today?", "Who won the match yesterday?"):
        assert not messages_api._sprint_allowed(question, SAP_HISTORY, None), question


def test_a_real_follow_up_still_inherits_the_sap_topic_of_the_chat():
    for question in ("Explain it in more detail", "and the steps?", "Can you give an example of that?"):
        assert messages_api._sprint_allowed(question, SAP_HISTORY, None), question


# ---- the model is told the same rules --------------------------------------------------------------------

def test_the_sprint_prompt_refuses_other_subjects_even_in_an_sap_conversation():
    assert "any other subject" in SPRINT_SYSTEM_PROMPT and "even when earlier messages were about SAP" in SPRINT_SYSTEM_PROMPT


def test_the_intent_analysis_treats_a_new_subject_as_not_sap():
    assert "starts a new subject is NOT about SAP" in ia._SYSTEM and "When unsure" in ia._SYSTEM
