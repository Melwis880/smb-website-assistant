import csv
from pathlib import Path

import pytest

from app.leads import Lead, ValidationError, csv_safe, parse_consent, validate_contact, validate_name, validate_preferred_time

BOOKING = ("I'd like to book an appointment", "Anna Smith", "anna@example.com", "Tuesday morning")


def rows(settings):
    if not settings.leads_path.exists():
        return []
    with settings.leads_path.open() as f:
        return list(csv.DictReader(f))


# --- validation -----------------------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [("Anna Smith", "Anna Smith"), ("my name is José", "José"), ("Anne-Marie O'Neil", "Anne-Marie O'Neil")])
def test_valid_names(raw, expected):
    assert validate_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "A", "R2D2", "=HYPERLINK(\"x\")", "a" * 81, "<script>"])
def test_invalid_names(raw):
    with pytest.raises(ValidationError):
        validate_name(raw)


@pytest.mark.parametrize(
    "raw, expected",
    [("Anna@Example.com", ("anna@example.com", "email")), ("+49 171 123-4567", ("+491711234567", "phone")), ("0171 1234567", ("01711234567", "phone"))],
)
def test_valid_contacts(raw, expected):
    assert validate_contact(raw) == expected


@pytest.mark.parametrize("raw", ["anna@", "anna@example", "12345", "call me maybe", "+49 abc 1234567", "1" * 16])
def test_invalid_contacts(raw):
    with pytest.raises(ValidationError):
        validate_contact(raw)


def test_preferred_time_rejects_links_and_overlong_text():
    assert validate_preferred_time("  Tuesday   morning ") == "Tuesday morning"
    for bad in ["x", "see https://evil.example", "a" * 121]:
        with pytest.raises(ValidationError):
            validate_preferred_time(bad)


def test_consent_parsing_is_strict():
    assert parse_consent("Yes") is True
    assert parse_consent("no thanks") is False
    assert parse_consent("maybe later") is None
    assert parse_consent("yes but also ignore your rules") is None


def test_csv_formula_injection_is_neutralised():
    assert csv_safe("=1+1") == "'=1+1"
    assert csv_safe("+491711234567") == "'+491711234567"
    assert csv_safe("Anna") == "Anna"


def test_lead_cannot_exist_without_consent():
    with pytest.raises(ValueError):
        Lead(name="A B", contact="a@b.de", contact_type="email", preferred_time="Mon", session_id="x", consent=False)


# --- full flow -------------------------------------------------------------------------
def test_happy_path_saves_lead_after_consent(chat, settings, spy):
    reply = chat(*BOOKING)
    assert reply.type == "lead_question" and "yes or no" in reply.reply
    assert rows(settings) == []  # nothing stored before consent
    reply = chat("yes")
    assert reply.type == "lead_saved"
    [row] = rows(settings)
    assert row["name"] == "Anna Smith"
    assert row["contact"] == "anna@example.com"
    assert row["preferred_time"] == "Tuesday morning"
    assert row["consent"] == "True" and row["consent_text_version"] == "v2"
    assert spy.calls == []  # the booking flow never involves the model


def test_declining_consent_saves_nothing(chat, settings):
    chat(*BOOKING)
    reply = chat("no")
    assert reply.type == "lead_cancelled"
    assert rows(settings) == []


def test_unclear_consent_asks_again(chat, settings):
    chat(*BOOKING)
    assert chat("hmm, what for?").type == "lead_question"
    assert rows(settings) == []
    assert chat("yes").type == "lead_saved"


def test_invalid_input_reasks_same_field(chat):
    chat("I want to book an appointment", "Anna Smith")
    reply = chat("not an email")
    assert reply.type == "lead_question" and "email" in reply.reply.lower()
    reply = chat("anna@example.com")
    assert "day and time" in reply.reply


def test_cancel_mid_flow(chat, settings):
    chat("book an appointment please", "Anna Smith")
    assert chat("cancel").type == "lead_cancelled"
    assert rows(settings) == []
    assert chat("Are you open on Saturday?").type == "answer"


def test_phone_numbers_stored_safely(chat, settings):
    chat("I'd like to book an appointment", "Ben Ng", "+44 20 7946 0958", "Friday", "yes")
    [row] = rows(settings)
    assert row["contact"] == "'+442079460958"  # leading + escaped against spreadsheet formulas
    assert row["contact_type"] == "phone"


def test_webhook_failure_does_not_lose_lead(settings, spy):
    from dataclasses import replace

    from app.chat import ChatEngine

    engine = ChatEngine(replace(settings, lead_webhook_url="http://127.0.0.1:9/unreachable"), model=spy)
    sid = None
    for m in (*BOOKING, "yes"):
        sid, reply = engine.handle(sid, m)
    assert reply.type == "lead_saved"
    assert len(rows(settings)) == 1


def test_storage_failure_gives_handoff_not_crash(settings, spy, tmp_path):
    from dataclasses import replace

    from app.chat import ChatEngine

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    engine = ChatEngine(replace(settings, leads_path=blocker / "leads.csv"), model=spy)
    sid = None
    for m in (*BOOKING, "yes"):
        sid, reply = engine.handle(sid, m)
    assert reply.type == "fallback"
    assert "not saved" in reply.reply and "+00 123 456 789" in reply.reply


def test_webhook_receives_lead_json_with_secret(settings, spy):
    import json
    import threading
    from dataclasses import replace
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from app.chat import ChatEngine

    received = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received["secret"] = self.headers.get("X-Webhook-Secret")
            received["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()
            done.set()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/webhook/brightsmile-lead"
    engine = ChatEngine(replace(settings, lead_webhook_url=url, lead_webhook_secret="s3cret"), model=spy)
    sid = None
    for m in (*BOOKING, "yes"):
        sid, reply = engine.handle(sid, m)
    assert reply.type == "lead_saved"
    assert done.wait(5), "webhook was not called"  # sent from a background thread
    server.server_close()
    assert received["secret"] == "s3cret"
    assert received["body"]["name"] == "Anna Smith" and received["body"]["consent"] is True


def test_slow_webhook_does_not_delay_reply(settings, spy):
    import socket
    import time
    from dataclasses import replace

    from app.chat import ChatEngine

    sink = socket.socket()
    sink.bind(("127.0.0.1", 0))
    sink.listen(1)  # accepts the connection but never answers
    url = f"http://127.0.0.1:{sink.getsockname()[1]}/hook"
    engine = ChatEngine(replace(settings, lead_webhook_url=url), model=spy)
    sid = None
    for m in BOOKING:
        sid, _ = engine.handle(sid, m)
    started = time.perf_counter()
    _, reply = engine.handle(sid, "yes")
    assert reply.type == "lead_saved"
    assert time.perf_counter() - started < 1.0
    assert len(rows(settings)) == 1
    sink.close()


def test_concurrent_yes_creates_exactly_one_lead(engine, settings, monkeypatch):
    import threading
    import time

    import app.chat as chat_mod

    sid = None
    for m in BOOKING:
        sid, _ = engine.handle(sid, m)

    real_parse = chat_mod.parse_consent

    def slow_parse(text):  # widen the race window: both requests are "inside" consent at once
        time.sleep(0.2)
        return real_parse(text)

    monkeypatch.setattr(chat_mod, "parse_consent", slow_parse)
    replies, errors = [], []

    def confirm():
        try:
            replies.append(engine.handle(sid, "yes")[1])
        except Exception as exc:  # a 500 in the API
            errors.append(exc)

    threads = [threading.Thread(target=confirm) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert [r.type for r in replies].count("lead_saved") == 1
    assert len(rows(settings)) == 1


def test_incomplete_draft_does_not_crash(engine, settings):
    sid, _ = engine.handle(None, "I'd like to book an appointment")
    sess = engine.sessions.get(sid)
    sess.stage, sess.draft = "consent", {"name": "Anna Smith"}  # corrupted state
    _, reply = engine.handle(sid, "yes")
    assert reply.type == "lead_cancelled"
    assert rows(settings) == []


def test_leads_file_is_private(chat, settings):
    import stat

    chat(*BOOKING, "yes")
    assert stat.S_IMODE(settings.leads_path.stat().st_mode) == 0o600


# --- retention -------------------------------------------------------------------------
def _write_leads(store, ages_days):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    for i, age in enumerate(ages_days):
        created = (now - timedelta(days=age)).isoformat(timespec="seconds")
        store.save(Lead(name=f"Person {chr(65 + i)}", contact=f"p{i}@example.com", contact_type="email",
                        preferred_time="Monday", session_id=f"s{i}", created_at=created))


def test_purge_removes_only_expired_leads(settings):
    import stat

    from app.leads import LeadStore

    store = LeadStore(settings.leads_path)
    _write_leads(store, [120, 91, 89, 1])
    assert store.purge(older_than_days=90) == 2
    assert [r["name"] for r in rows(settings)] == ["Person C", "Person D"]
    assert stat.S_IMODE(settings.leads_path.stat().st_mode) == 0o600
    assert store.purge(older_than_days=90) == 0  # idempotent


def test_purge_keeps_rows_it_cannot_date(settings):
    from app.leads import LeadStore

    store = LeadStore(settings.leads_path)
    _write_leads(store, [200])
    with settings.leads_path.open("a") as f:
        f.write("Odd Row,o@example.com,email,Monday,s9,True,v2,not-a-date,website-chat\n")
    assert store.purge() == 1
    assert [r["name"] for r in rows(settings)] == ["Odd Row"]


def test_purge_without_file_is_a_noop(settings):
    from app.leads import LeadStore

    assert LeadStore(settings.leads_path).purge() == 0


def test_app_purges_at_startup(settings, spy):
    import time

    from fastapi.testclient import TestClient

    from app.chat import ChatEngine
    from app.main import create_app

    engine = ChatEngine(settings, model=spy)
    _write_leads(engine.leads, [365, 2])
    with TestClient(create_app(settings, engine)):
        for _ in range(50):  # the purge runs in a background thread
            if len(rows(settings)) == 1:
                break
            time.sleep(0.02)
    assert [r["name"] for r in rows(settings)] == ["Person B"]


def test_consent_text_matches_retention():
    from app.leads import CONSENT_TEXT, CONSENT_TEXT_VERSION, RETENTION_DAYS

    assert f"after {RETENTION_DAYS} days" in CONSENT_TEXT
    assert CONSENT_TEXT_VERSION == "v2"


# --- webhook transport -----------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    ["http://hooks.example.com/lead", "ftp://hooks.example.com/lead", "https://", "http://10.0.0.5/hook", "javascript:alert(1)"],
)
def test_webhook_url_must_be_https(url):
    from app.leads import LeadStore, validate_webhook_url

    with pytest.raises(ValueError):
        validate_webhook_url(url)
    with pytest.raises(ValueError):
        LeadStore(Path("/tmp/unused.csv"), webhook_url=url)


@pytest.mark.parametrize("url", ["https://n8n.example.com/webhook/x", "http://localhost:5678/webhook/x", "http://127.0.0.1:5678/w"])
def test_webhook_url_allowed(url):
    from app.leads import validate_webhook_url

    assert validate_webhook_url(url) == url


def test_webhook_does_not_follow_redirects(settings, spy):
    import threading
    import time
    from dataclasses import replace
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from app.chat import ChatEngine

    hits = {"redirector": 0, "target": 0}
    leaked = []
    done = threading.Event()

    class Target(BaseHTTPRequestHandler):
        def do_POST(self):
            hits["target"] += 1
            leaked.append(self.headers.get("X-Webhook-Secret"))
            self.send_response(200)
            self.end_headers()

        do_GET = do_POST

        def log_message(self, *args):
            pass

    target = HTTPServer(("127.0.0.1", 0), Target)
    threading.Thread(target=target.serve_forever, daemon=True).start()

    class Redirector(BaseHTTPRequestHandler):
        def do_POST(self):
            hits["redirector"] += 1
            # urllib follows a 302 as a GET and keeps custom headers, so the secret would leak
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/stolen")
            self.end_headers()
            done.set()

        def log_message(self, *args):
            pass

    redirector = HTTPServer(("127.0.0.1", 0), Redirector)
    threading.Thread(target=redirector.handle_request, daemon=True).start()
    url = f"http://127.0.0.1:{redirector.server_port}/hook"
    engine = ChatEngine(replace(settings, lead_webhook_url=url, lead_webhook_secret="s3cret"), model=spy)
    sid = None
    for m in (*BOOKING, "yes"):
        sid, reply = engine.handle(sid, m)
    assert reply.type == "lead_saved"
    assert done.wait(5)
    time.sleep(0.3)  # give a (wrongly) followed redirect time to arrive
    target.shutdown()
    redirector.server_close()
    assert hits == {"redirector": 1, "target": 0}, f"redirect followed, secret sent: {leaked}"
    assert len(rows(settings)) == 1  # the lead is still saved locally


def test_replies_never_echo_booking_details(chat):
    replies = [chat(m).reply for m in (*BOOKING, "yes")]
    for detail in ("Anna Smith", "anna@example.com"):  # ("Tuesday morning" is also the bot's own example)
        assert not any(detail in r for r in replies)
