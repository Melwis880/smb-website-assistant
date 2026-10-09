import pytest


def test_answers_from_content_with_sources(chat):
    reply = chat("How much does a root canal on a molar cost?")
    assert reply.type == "answer"
    assert "EUR 750" in reply.reply
    assert any(s.startswith("prices.md") for s in reply.sources)


def test_answer_says_what_is_not_offered(chat):
    reply = chat("Do you do braces?")
    assert reply.type == "answer"
    assert "do not offer" in reply.reply


@pytest.mark.parametrize(
    "question",
    [
        "What is the capital of France?",
        "Who will win the next election?",
        "Can you write my homework essay?",
        "What's the best pizza place in town?",
    ],
)
def test_out_of_scope_falls_back_to_human_without_calling_model(chat, spy, question):
    reply = chat(question)
    assert reply.type == "fallback"
    assert "not sure" in reply.reply and "+00 123 456 789" in reply.reply
    assert spy.calls == []


def test_model_no_answer_becomes_handoff(settings):
    from app.chat import ChatEngine
    from tests.conftest import SpyModel

    engine = ChatEngine(settings, model=SpyModel(fixed="NO_ANSWER"))
    _, reply = engine.handle(None, "Do you offer root canal treatment for children?")
    assert reply.type == "fallback"


def test_follow_up_uses_previous_topic(chat):
    chat("Is there parking?")
    reply = chat("and for wheelchairs?")
    assert reply.type == "answer"
    assert "wheelchair" in reply.reply


def test_question_about_booking_is_answered_not_turned_into_lead(chat):
    reply = chat("How do I book an appointment?")
    assert reply.type == "answer"


def test_human_handoff_request_offers_contact_and_lead(chat):
    reply = chat("I want to talk to a real person")
    assert reply.type == "lead_question"
    assert "+00 123 456 789" in reply.reply


def test_empty_message(chat):
    assert chat("   ").type == "fallback"


def test_sessions_are_isolated(engine):
    sid_a, _ = engine.handle(None, "I'd like to book an appointment")
    sid_b, reply_b = engine.handle(None, "Anna Smith")
    assert sid_a != sid_b
    assert reply_b.type != "lead_question"  # B is not inside A's booking flow
