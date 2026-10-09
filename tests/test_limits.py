from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.chat import ChatEngine
from app.limits import DailyBudget, SlidingWindowLimiter
from app.main import create_app

BOOKING = ("I'd like to book an appointment", "Anna Smith", "anna@example.com", "Tuesday morning")


# --- building blocks ---------------------------------------------------------------------
def test_sliding_window_limits_and_recovers():
    lim = SlidingWindowLimiter(limit=2, window_seconds=60)
    assert lim.hit("a", now=0) and lim.hit("a", now=1)
    assert not lim.hit("a", now=2)
    assert not lim.allowed("a", now=59)
    assert lim.hit("b", now=2)  # per key
    assert lim.hit("a", now=61)  # first event left the window


def test_allowed_does_not_record():
    lim = SlidingWindowLimiter(limit=1, window_seconds=60)
    assert lim.allowed("a", now=0) and lim.allowed("a", now=0)
    assert lim.hit("a", now=0)


def test_limiter_memory_is_bounded():
    lim = SlidingWindowLimiter(limit=5, window_seconds=60, max_keys=100)
    for i in range(1000):
        lim.hit(f"ip{i}", now=0)
    assert len(lim._events) == 100


def test_zero_means_unlimited():
    assert all(SlidingWindowLimiter(0, 60).hit("a") for _ in range(100))
    assert all(DailyBudget(0).try_spend() for _ in range(100))


def test_daily_budget_resets_next_utc_day():
    budget = DailyBudget(2)
    day1 = datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc)
    assert budget.try_spend(day1) and budget.try_spend(day1)
    assert not budget.try_spend(day1)
    assert budget.try_spend(day1 + timedelta(hours=2))


# --- HTTP layer -------------------------------------------------------------------------
def client(settings, spy, **overrides):
    s = replace(settings, **overrides)
    return TestClient(create_app(s, ChatEngine(s, model=spy)))


def test_body_over_16kb_is_rejected_with_413(settings, spy):
    c = client(settings, spy)
    big = '{"message": "' + "a" * 17_000 + '"}'
    r = c.post("/api/chat", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert spy.calls == []


def test_chunked_body_without_content_length_is_also_capped(settings, spy):
    c = client(settings, spy)

    def chunks():
        yield b'{"message": "'
        for _ in range(20):
            yield b"a" * 1024
        yield b'"}'

    r = c.post("/api/chat", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_body_limit_is_configurable(settings, spy):
    c = client(settings, spy, max_body_bytes=64)
    assert c.post("/api/chat", json={"message": "Are you open on Saturday?"}).status_code == 200
    assert c.post("/api/chat", json={"message": "x" * 100}).status_code == 413


def test_rate_limit_per_ip_returns_429(settings, spy):
    c = client(settings, spy, rate_limit_per_minute=3)
    codes = [c.post("/api/chat", json={"message": "Are you open on Saturday?"}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    r = c.post("/api/chat", json={"message": "hi"})
    assert r.headers["Retry-After"] == "60"


# --- engine limits ----------------------------------------------------------------------
def test_lead_cap_per_ip(settings, spy):
    engine = ChatEngine(replace(settings, leads_per_ip_per_day=1), model=spy)
    sid_a = sid_b = None
    # B starts a flow before A finishes, so the cap must also hold at the consent step
    for m in BOOKING:
        sid_a, _ = engine.handle(sid_a, m, client_ip="1.2.3.4")
        sid_b, _ = engine.handle(sid_b, m, client_ip="1.2.3.4")
    assert engine.handle(sid_a, "yes", client_ip="1.2.3.4")[1].type == "lead_saved"
    reply = engine.handle(sid_b, "yes", client_ip="1.2.3.4")[1]
    assert reply.type == "fallback" and "+00 123 456 789" in reply.reply
    # a new booking from the same IP is refused up front, without asking for details
    assert engine.handle(None, "I'd like to book an appointment", client_ip="1.2.3.4")[1].type == "fallback"
    # another IP is unaffected
    assert engine.handle(None, "I'd like to book an appointment", client_ip="5.6.7.8")[1].type == "lead_question"
    assert len(engine.leads.read_all()) == 1


def test_llm_budget_switches_to_fallback(settings, spy):
    engine = ChatEngine(replace(settings, llm_daily_call_budget=2), model=spy)
    types = [engine.handle(None, "Are you open on Saturday?")[1].type for _ in range(3)]
    assert types == ["answer", "answer", "fallback"]
    assert len(spy.calls) == 2
    # questions that never needed the model still work as before
    assert engine.handle(None, "What is the capital of France?")[1].type == "fallback"


def test_allowed_does_not_create_keys():
    lim = SlidingWindowLimiter(limit=1, window_seconds=60, max_keys=10)
    lim.hit("real", now=0)
    for i in range(100):
        assert lim.allowed(f"probe{i}", now=1)
    assert list(lim._events) == ["real"]  # the recorded key was not evicted by mere checks
    assert not lim.allowed("real", now=59) and lim.allowed("real", now=60)
