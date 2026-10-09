"""ClaudeModel with a fake SDK client: checks request shape and error handling, no network."""

from types import SimpleNamespace

import anthropic
import httpx2

from app.llm import NO_ANSWER, ClaudeModel
from app.retrieval import Chunk, Hit

HITS = [Hit(Chunk("prices.md", "Price list", "| Filling | EUR 90 |"), 5.0)]


class FakeMessages:
    def __init__(self, text="A small filling costs EUR 90.", error=None):
        self.text, self.error, self.kwargs = text, error, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)],
            usage=SimpleNamespace(input_tokens=321, output_tokens=12),
        )


def make(fake):
    model = ClaudeModel(api_key="test-key", model="claude-haiku-4-5-20251001", business="Brightsmile Dental")
    model.client = SimpleNamespace(messages=fake)
    return model


def test_request_shape_and_usage():
    fake = FakeMessages()
    ans = make(fake).answer("How much is a filling?", HITS, [("user", "hi"), ("assistant", "hello")])
    assert ans.text == "A small filling costs EUR 90." and ans.input_tokens == 321
    assert fake.kwargs["model"] == "claude-haiku-4-5-20251001"
    assert "Brightsmile Dental" in fake.kwargs["system"]
    msgs = fake.kwargs["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert "<context>" in msgs[-1]["content"] and "EUR 90" in msgs[-1]["content"]
    assert msgs[0]["content"] == "<visitor_message>\nhi\n</visitor_message>"  # earlier turns are data too


def test_api_errors_degrade_to_handoff():
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.APIConnectionError(request=req)
    assert make(FakeMessages(error=err)).answer("q", HITS, []).text == NO_ANSWER


def test_no_answer_detection():
    assert make(FakeMessages(text="NO_ANSWER")).answer("q", HITS, []).is_no_answer
    assert make(FakeMessages(text="")).answer("q", HITS, []).is_no_answer


def test_client_gives_up_quickly():
    """Worst case ~21 s (2 attempts x 10 s), not ~60 s: a slow API must not hold a chat thread."""
    client = ClaudeModel(api_key="test-key", model="m", business="b").client
    assert client.max_retries == 1
    assert client.timeout.read == 10.0 and client.timeout.connect == 3.0
