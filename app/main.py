"""FastAPI app: chat API, demo page and the embeddable widget."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .chat import ChatEngine
from .config import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("assistant")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]*$")


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    type: str
    sources: list[str]


def create_app(settings: Settings | None = None, engine: ChatEngine | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = engine or ChatEngine(settings)
    app = FastAPI(title=f"{settings.business_name} website assistant (demo)")
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.allowed_origins, allow_methods=["GET", "POST"], allow_headers=["Content-Type"]
    )
    app.state.engine = engine
    log.info("Starting with model=%s mock=%s chunks=%d", engine.model.name, settings.mock_llm, len(engine.retriever.chunks))

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "model": engine.model.name, "mock": settings.mock_llm}

    @app.post("/api/chat", response_model=ChatResponse)
    def chat(req: ChatRequest) -> ChatResponse:
        started = time.perf_counter()
        session_id, reply = engine.handle(req.session_id, req.message)
        # One structured log line per turn; no message text or personal data is logged.
        log.info(json.dumps({"event": "chat", "type": reply.type, "ms": round((time.perf_counter() - started) * 1000, 1)}))
        return ChatResponse(session_id=session_id, reply=reply.reply, type=reply.type, sources=reply.sources)

    @app.get("/")
    def demo_page() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/widget.js")
    def widget() -> FileResponse:
        return FileResponse(WEB_DIR / "widget.js", media_type="application/javascript")

    return app


app = create_app()
