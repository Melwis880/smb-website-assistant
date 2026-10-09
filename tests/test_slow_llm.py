"""A slow Claude API must not take the server down with it."""

import asyncio
import threading
from dataclasses import replace

import httpx

from app.chat import ChatEngine
from app.llm import MockModel
from app.main import CHAT_THREADS, create_app


class BlockedModel(MockModel):
    """Holds every call until released: stands in for a hanging or retrying Claude request."""

    def __init__(self):
        self.release = threading.Event()
        self.entered = 0
        self._lock = threading.Lock()

    def answer(self, question, hits, history):
        with self._lock:
            self.entered += 1
        self.release.wait(10)
        return super().answer(question, hits, history)


def test_health_and_widget_respond_while_chats_hang(settings):
    s = replace(settings, rate_limit_per_minute=0)
    model = BlockedModel()
    app = create_app(s, ChatEngine(s, model=model))

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=15) as c:
            # More hanging chats than AnyIO's default pool of 40 threads.
            chats = [
                asyncio.create_task(c.post("/api/chat", json={"message": "Are you open on Saturday?"}))
                for _ in range(45)
            ]
            try:
                for _ in range(200):  # wait until the chat threads are all stuck inside the model
                    if model.entered >= CHAT_THREADS:
                        break
                    await asyncio.sleep(0.01)
                health = await asyncio.wait_for(c.get("/healthz"), timeout=0.5)
                widget = await asyncio.wait_for(c.get("/widget.js"), timeout=0.5)
            finally:
                model.release.set()
            replies = await asyncio.gather(*chats)
        return health, widget, replies

    health, widget, replies = asyncio.run(scenario())
    assert model.entered >= CHAT_THREADS
    assert health.json() == {"status": "ok"} and widget.status_code == 200
    assert all(r.status_code == 200 for r in replies)  # queued chats are served, not dropped
