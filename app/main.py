"""FastAPI app: chat API, demo page and the embeddable widget."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .chat import ChatEngine
from .config import MAX_MESSAGE_CHARS, Settings
from .limits import SlidingWindowLimiter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("assistant")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
# A chat can wait seconds on Claude. Chats get their own threads, so a slow API can never take
# the shared pool that serves /healthz, the demo page and widget.js. Extra chats queue here.
CHAT_THREADS = 20


class BodySizeLimit:
    """Reject request bodies over `max_bytes` with 413, before FastAPI parses them.

    Checks Content-Length when present and also counts the bytes actually received, so a
    chunked request without Content-Length can't get around it.
    """

    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.max_bytes <= 0:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
            return await self._reject(scope, receive, send)
        messages, total = [], 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            total += len(message.get("body", b""))
            if total > self.max_bytes:
                return await self._reject(scope, receive, send)
            if not message.get("more_body", False):
                break

        async def replay():
            return messages.pop(0) if messages else await receive()

        await self.app(scope, replay, send)

    async def _reject(self, scope, receive, send):
        response = JSONResponse({"detail": f"Request body too large (max {self.max_bytes} bytes)."}, status_code=413)
        await response(scope, receive, send)


# Demo page: only our own script, styles inline (the page and the widget use <style>), API calls to self.
PAGE_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
# Everything else (JSON, health check): nothing may load or frame it.
DEFAULT_CSP = "default-src 'none'; frame-ancestors 'none'"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    session_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]*$")


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    type: str
    sources: list[str]


def create_app(settings: Settings | None = None, engine: ChatEngine | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = engine or ChatEngine(settings)
    chat_pool = ThreadPoolExecutor(max_workers=CHAT_THREADS, thread_name_prefix="chat")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Retention: purge at startup, then once per interval, in a daemon thread.
        stop = threading.Event()

        def purge_loop() -> None:
            while True:
                try:
                    engine.leads.purge()
                except Exception:  # never let a purge error kill the loop
                    log.exception("Lead purge failed")
                if stop.wait(settings.purge_interval_hours * 3600):
                    return

        threading.Thread(target=purge_loop, name="lead-purge", daemon=True).start()
        yield
        stop.set()
        chat_pool.shutdown(wait=False)

    docs = settings.enable_docs  # /docs, /redoc and /openapi.json only when ENABLE_DOCS=1
    app = FastAPI(
        title=f"{settings.business_name} website assistant (demo)",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )

    app.add_middleware(BodySizeLimit, max_bytes=settings.max_body_bytes)

    # Registered after BodySizeLimit so it wraps it: 413 responses get the headers too.
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path == "/":
            response.headers["Content-Security-Policy"] = PAGE_CSP
        elif request.url.path != "/widget.js" and not (docs and request.url.path in ("/docs", "/redoc")):
            # widget.js is a script for other sites; the docs pages load assets from a CDN
            response.headers["Content-Security-Policy"] = DEFAULT_CSP
        return response

    if settings.allowed_origins:  # otherwise same-origin only: no CORS headers at all
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.allowed_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
        )
    app.state.engine = engine
    per_ip = SlidingWindowLimiter(settings.rate_limit_per_minute, 60)
    log.info("Starting with model=%s mock=%s chunks=%d", engine.model.name, settings.mock_llm, len(engine.retriever.chunks))

    @app.get("/healthz")
    async def healthz() -> dict:  # async: answers even when every worker thread is busy
        return {"status": "ok"}  # no model name or mode: nothing for a scanner to fingerprint

    @app.post("/api/chat", response_model=ChatResponse, responses={429: {}, 413: {}})
    async def chat(req: ChatRequest, request: Request):
        # Behind a reverse proxy, run uvicorn with --forwarded-allow-ips so this is the visitor's IP.
        client_ip = request.client.host if request.client else "unknown"
        if not per_ip.hit(client_ip):
            return JSONResponse(
                {"detail": "Too many messages. Please wait a minute and try again."},
                status_code=429,
                headers={"Retry-After": "60"},
            )
        started = time.perf_counter()
        session_id, reply = await asyncio.get_running_loop().run_in_executor(
            chat_pool, engine.handle, req.session_id, req.message, client_ip
        )
        # One structured log line per turn; no message text or personal data is logged.
        log.info(json.dumps({"event": "chat", "type": reply.type, "ms": round((time.perf_counter() - started) * 1000, 1)}))
        return ChatResponse(session_id=session_id, reply=reply.reply, type=reply.type, sources=reply.sources)

    @app.get("/")
    def demo_page() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/widget.js")
    def widget() -> FileResponse:
        # Loaded on every page of the embedding site: let browsers reuse it for an hour.
        return FileResponse(
            WEB_DIR / "widget.js", media_type="application/javascript", headers={"Cache-Control": "public, max-age=3600"}
        )

    return app


app = create_app()
