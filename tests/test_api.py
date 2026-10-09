from fastapi.testclient import TestClient

from app.chat import ChatEngine
from app.main import create_app


def client(settings, spy):
    return TestClient(create_app(settings, ChatEngine(settings, model=spy)))


def test_health_and_static_files(settings, spy):
    c = client(settings, spy)
    assert c.get("/healthz").json() == {"status": "ok"}
    assert "Brightsmile Dental" in c.get("/").text
    r = c.get("/widget.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert r.headers["Cache-Control"] == "public, max-age=3600"  # embedded on every page of the client site


def test_chat_round_trip_keeps_session(settings, spy):
    c = client(settings, spy)
    first = c.post("/api/chat", json={"message": "I'd like to book an appointment"}).json()
    assert first["type"] == "lead_question"
    second = c.post("/api/chat", json={"message": "Anna Smith", "session_id": first["session_id"]}).json()
    assert second["session_id"] == first["session_id"]
    assert "email" in second["reply"].lower()


def test_rejects_bad_input(settings, spy):
    c = client(settings, spy)
    assert c.post("/api/chat", json={"message": ""}).status_code == 422
    assert c.post("/api/chat", json={"message": "hi", "session_id": "../../etc"}).status_code == 422
    assert c.post("/api/chat", json={"message": "x" * 5000}).status_code == 422
    assert c.post("/api/chat", json={"message": "x" * 1001}).status_code == 422  # same cap as the widget


def test_docs_disabled_by_default(settings, spy):
    c = client(settings, spy)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404


def test_docs_can_be_enabled(settings, spy):
    from dataclasses import replace

    c = client(replace(settings, enable_docs=True), spy)
    assert c.get("/openapi.json").status_code == 200
    assert c.get("/docs").status_code == 200


def test_security_headers(settings, spy):
    c = client(settings, spy)
    page = c.get("/")
    csp = page.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-eval" not in csp
    api = c.post("/api/chat", json={"message": "hi"})
    assert api.headers["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"
    for r in (page, api, c.get("/widget.js"), c.get("/healthz")):
        assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_cors_is_same_origin_by_default(settings, spy):
    c = client(settings, spy)
    r = c.post("/api/chat", json={"message": "hi"}, headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_allows_configured_origin_only(settings, spy):
    from dataclasses import replace

    c = client(replace(settings, allowed_origins=["https://www.clinic.example"]), spy)
    ok = c.post("/api/chat", json={"message": "hi"}, headers={"Origin": "https://www.clinic.example"})
    assert ok.headers["access-control-allow-origin"] == "https://www.clinic.example"
    bad = c.post("/api/chat", json={"message": "hi"}, headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in bad.headers


def test_wildcard_origin_logs_warning(monkeypatch, caplog):
    from app.config import Settings

    monkeypatch.setenv("ALLOWED_ORIGINS", "*")
    with caplog.at_level("WARNING"):
        assert Settings.from_env().allowed_origins == ["*"]
    assert "ALLOWED_ORIGINS='*'" in caplog.text
    monkeypatch.delenv("ALLOWED_ORIGINS")
    assert Settings.from_env().allowed_origins == []
