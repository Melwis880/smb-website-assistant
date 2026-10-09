"""Conversation engine.

The model only ever writes answers to questions. Everything with business or legal weight
(booking flow, validation, consent, storage, handoff) is plain, testable code.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

from . import guard
from .config import Settings
from .leads import (
    CONSENT_TEXT,
    Lead,
    LeadStore,
    ValidationError,
    parse_consent,
    validate_contact,
    validate_name,
    validate_preferred_time,
)
from .llm import AnswerModel, ClaudeModel, MockModel
from .retrieval import Retriever

log = logging.getLogger(__name__)

BOOKING_RE = re.compile(
    r"\b(book|booking|appointment|schedule|reserve|make an? (?:visit|appointment)|come in|see (?:a|the) dentist|"
    r"call me back|callback|contact me)\b",
    re.IGNORECASE,
)
HUMAN_RE = re.compile(r"\b(human|real person|someone|staff|receptionist|speak to|talk to)\b", re.IGNORECASE)
CANCEL_RE = re.compile(r"^\s*(cancel|stop|quit|never ?mind|forget it|exit)\b", re.IGNORECASE)
# Questions *about* booking ("how do I book?", "can I book on Saturday?") are answered from content.
BOOKING_QUESTION_RE = re.compile(r"^\s*(how|can|could|do|does|is|are|what|when|where|which)\b.*\?\s*$", re.IGNORECASE)


@dataclass
class Reply:
    reply: str
    type: str  # answer | fallback | refusal | lead_question | lead_saved | lead_cancelled
    sources: list[str] = field(default_factory=list)


@dataclass
class Session:
    id: str
    history: list[tuple[str, str]] = field(default_factory=list)
    stage: str | None = None  # None | name | contact | time | consent
    draft: dict[str, str] = field(default_factory=dict)
    last_seen: float = field(default_factory=time.time)


class SessionStore:
    """In-memory, size- and age-bounded. Fine for one process; see README limitations."""

    def __init__(self, max_sessions: int = 5000, ttl_seconds: int = 3600):
        self._data: OrderedDict[str, Session] = OrderedDict()
        self._lock = threading.Lock()
        self.max_sessions, self.ttl = max_sessions, ttl_seconds

    def get(self, session_id: str | None) -> Session:
        now = time.time()
        with self._lock:
            for sid in [s for s, v in self._data.items() if now - v.last_seen > self.ttl]:
                del self._data[sid]
            sess = self._data.get(session_id) if session_id else None
            if sess is None:
                sess = Session(id=uuid.uuid4().hex)
                self._data[sess.id] = sess
                while len(self._data) > self.max_sessions:
                    self._data.popitem(last=False)
            sess.last_seen = now
            self._data.move_to_end(sess.id)
            return sess


class ChatEngine:
    def __init__(self, settings: Settings, model: AnswerModel | None = None, retriever: Retriever | None = None):
        self.s = settings
        self.retriever = retriever or Retriever.from_dir(settings.data_dir)
        self.model = model or (
            MockModel()
            if settings.mock_llm
            else ClaudeModel(settings.api_key or "", settings.model, settings.business_name, settings.max_output_tokens)
        )
        self.leads = LeadStore(settings.leads_path, settings.lead_webhook_url, settings.lead_webhook_secret)
        self.sessions = SessionStore()

    # --- canned texts -------------------------------------------------------------------
    def _fallback(self) -> str:
        return (
            "I'm not sure about that, and I don't want to guess. Our team can help: "
            f"{self.s.handoff_contact}. If you'd like, type \"book\" and I'll take your details "
            "so the front desk can get back to you."
        )

    def _refusal(self) -> str:
        return (
            f"I can only help with questions about {self.s.business_name}: our services, prices, "
            "opening hours, insurance and booking."
        )

    # --- main entry point -------------------------------------------------------------------
    def handle(self, session_id: str | None, message: str) -> tuple[str, Reply]:
        sess = self.sessions.get(session_id)
        text = guard.clean_text(message, self.s.max_message_chars)
        if not text:
            return sess.id, Reply("Please type a question.", "fallback")
        reply = self._lead_step(sess, text) if sess.stage else self._route(sess, text)
        sess.history += [("user", text), ("assistant", reply.reply)]
        sess.history = sess.history[-12:]
        return sess.id, reply

    def _route(self, sess: Session, text: str) -> Reply:
        if guard.looks_like_injection(text):
            return Reply(self._refusal(), "refusal")
        if HUMAN_RE.search(text) and not BOOKING_QUESTION_RE.match(text):
            return self._start_lead(sess, intro=f"Of course. You can reach our team at {self.s.handoff_contact}. ")
        if BOOKING_RE.search(text) and not BOOKING_QUESTION_RE.match(text):
            return self._start_lead(sess)
        return self._answer(sess, text)

    def _relevant(self, query: str):
        if self.retriever.coverage(query) < self.s.min_query_coverage:
            return []
        hits = [h for h in self.retriever.search(query) if h.score >= self.s.min_retrieval_score]
        # Drop sections that score far below the best one: less noise for the model and the source list.
        return [h for h in hits if h.score >= 0.4 * hits[0].score] if hits else []

    def _answer(self, sess: Session, text: str) -> Reply:
        hits = self._relevant(text)
        if not hits:
            # Follow-up questions ("and for children?") borrow the previous question's topic.
            prev = next((t for r, t in reversed(sess.history) if r == "user"), None)
            if prev and len(text.split()) <= 6 and self.retriever.coverage(text) >= self.s.min_query_coverage:
                hits = self._relevant(f"{prev} {text}")
        if not hits:
            return Reply(self._fallback(), "fallback")
        ans = self.model.answer(text, hits, sess.history)
        if ans.is_no_answer:
            return Reply(self._fallback(), "fallback")
        if guard.leaks_system_prompt(ans.text):
            return Reply(self._refusal(), "refusal")
        sources = list(dict.fromkeys(f"{h.chunk.source} > {h.chunk.title}" for h in hits))
        return Reply(ans.text, "answer", sources)

    # --- lead capture state machine ---------------------------------------------------------
    def _start_lead(self, sess: Session, intro: str = "") -> Reply:
        sess.stage, sess.draft = "name", {}
        return Reply(
            intro + "I can pass a booking request to our front desk, who will confirm by phone or email "
            "within one working day. What is your name? (Type \"cancel\" at any time to stop.)",
            "lead_question",
        )

    def _lead_step(self, sess: Session, text: str) -> Reply:
        if CANCEL_RE.match(text):
            sess.stage, sess.draft = None, {}
            return Reply("No problem, I've stopped and nothing was saved. Anything else I can help with?", "lead_cancelled")
        if guard.looks_like_injection(text):
            return Reply("Sorry, I can't use that. " + self._question_for(sess.stage), "lead_question")
        try:
            if sess.stage == "name":
                sess.draft["name"] = validate_name(text)
                sess.stage = "contact"
            elif sess.stage == "contact":
                sess.draft["contact"], sess.draft["contact_type"] = validate_contact(text)
                sess.stage = "time"
            elif sess.stage == "time":
                sess.draft["preferred_time"] = validate_preferred_time(text)
                sess.stage = "consent"
            elif sess.stage == "consent":
                return self._consent(sess, text)
        except ValidationError as exc:
            return Reply(str(exc), "lead_question")
        return Reply(self._question_for(sess.stage), "lead_question")

    def _question_for(self, stage: str | None) -> str:
        return {
            "name": "What is your name?",
            "contact": "Thanks! What is the best email address or phone number to reach you?",
            "time": "Which day and time would suit you? (e.g. \"Tuesday morning\" or \"any weekday after 4pm\")",
            "consent": CONSENT_TEXT,
        }.get(stage or "", "")

    def _consent(self, sess: Session, text: str) -> Reply:
        decision = parse_consent(text)
        if decision is None:
            return Reply("Sorry, I need a clear yes or no. " + CONSENT_TEXT, "lead_question")
        draft, sess.stage, sess.draft = sess.draft, None, {}
        if not decision:
            return Reply(
                f"Understood, I have not saved your details. You can reach us at {self.s.handoff_contact}.",
                "lead_cancelled",
            )
        try:
            self.leads.save(Lead(session_id=sess.id, consent=True, **draft))
        except OSError as exc:
            log.error("Could not save lead: %s", exc)
            return Reply(
                f"Sorry, something went wrong on our side and your request was not saved. "
                f"Please contact us directly: {self.s.handoff_contact}.",
                "fallback",
            )
        return Reply(
            f"Thank you, {draft['name']}! Your request is with our front desk. They will contact you via "
            f"{draft['contact_type']} within one working day to confirm a time.",
            "lead_saved",
        )
