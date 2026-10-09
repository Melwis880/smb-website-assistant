import os
from dataclasses import replace

import pytest

os.environ["MOCK_LLM"] = "1"  # app.main builds an app at import time; never touch the network in tests

from app.chat import ChatEngine  # noqa: E402
from app.config import Settings  # noqa: E402
from app.llm import Answer, MockModel  # noqa: E402


class SpyModel(MockModel):
    """Mock model that records calls, so tests can assert the LLM was (not) reached."""

    def __init__(self, fixed: str | None = None):
        self.calls: list[str] = []
        self.histories: list[list[tuple[str, str]]] = []
        self.fixed = fixed

    def answer(self, question, hits, history):
        self.calls.append(question)
        self.histories.append(list(history))
        return Answer(self.fixed) if self.fixed is not None else super().answer(question, hits, history)


@pytest.fixture
def settings(tmp_path):
    return replace(Settings(), mock_llm=True, leads_path=tmp_path / "leads.csv")


@pytest.fixture
def spy():
    return SpyModel()


@pytest.fixture
def engine(settings, spy):
    return ChatEngine(settings, model=spy)


@pytest.fixture
def chat(engine):
    """Send messages in one session; returns the last Reply."""
    state = {"sid": None}

    def send(*messages):
        reply = None
        for m in messages:
            state["sid"], reply = engine.handle(state["sid"], m)
        return reply

    return send
