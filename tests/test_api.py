from fastapi.testclient import TestClient

from app.chat import ChatEngine
from app.main import create_app


def client(settings, spy):
    return TestClient(create_app(settings, ChatEngine(settings, model=spy)))


def test_health_and_static_files(settings, spy):
    c = client(settings, spy)
    assert c.get("/healthz").json()["mock"] is True
    assert "Brightsmile Dental" in c.get("/").text
    r = c.get("/widget.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]


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
