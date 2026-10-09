"""Runtime configuration, read once from environment variables."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
# Longest visitor message: the API rejects longer ones (422), the widget input stops at it.
MAX_MESSAGE_CHARS = 1000


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    model: str = DEFAULT_MODEL
    mock_llm: bool = True
    api_key: str | None = None
    data_dir: Path = ROOT / "data"
    leads_path: Path = ROOT / "leads.csv"
    lead_webhook_url: str | None = None
    lead_webhook_secret: str | None = None
    # Empty = same-origin only (no CORS headers). List the sites that embed the widget.
    allowed_origins: list[str] = field(default_factory=list)
    enable_docs: bool = False
    min_retrieval_score: float = 2.0
    min_query_coverage: float = 0.5
    purge_interval_hours: float = 24.0
    # Abuse limits (0 = unlimited). In-memory, per process; see README "Production hardening".
    max_body_bytes: int = 16 * 1024
    rate_limit_per_minute: int = 20
    leads_per_ip_per_day: int = 3
    llm_daily_call_budget: int = 1000
    max_message_chars: int = MAX_MESSAGE_CHARS
    max_output_tokens: int = 400
    business_name: str = "Brightsmile Dental"
    handoff_contact: str = "+00 123 456 789 or hello@brightsmile.example"

    @classmethod
    def from_env(cls) -> "Settings":
        api_key = os.getenv("ANTHROPIC_API_KEY") or None
        mock = _flag("MOCK_LLM")
        if not mock and not api_key:
            log.warning("ANTHROPIC_API_KEY is not set; falling back to MOCK_LLM mode.")
            mock = True
        origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
        if "*" in origins:
            log.warning("ALLOWED_ORIGINS='*': any website can call this API from a visitor's browser. Demo use only.")
        return cls(
            model=os.getenv("CLAUDE_MODEL", DEFAULT_MODEL),
            mock_llm=mock,
            api_key=api_key,
            data_dir=Path(os.getenv("DATA_DIR", ROOT / "data")),
            leads_path=Path(os.getenv("LEADS_PATH", ROOT / "leads.csv")),
            lead_webhook_url=os.getenv("LEAD_WEBHOOK_URL") or None,
            lead_webhook_secret=os.getenv("LEAD_WEBHOOK_SECRET") or None,
            allowed_origins=origins,
            enable_docs=_flag("ENABLE_DOCS"),
            min_retrieval_score=float(os.getenv("MIN_RETRIEVAL_SCORE", "2.0")),
            min_query_coverage=float(os.getenv("MIN_QUERY_COVERAGE", "0.5")),
            purge_interval_hours=float(os.getenv("LEAD_PURGE_INTERVAL_HOURS", "24")),
            max_body_bytes=int(os.getenv("MAX_BODY_BYTES", str(16 * 1024))),
            rate_limit_per_minute=int(os.getenv("RATE_LIMIT_PER_MINUTE", "20")),
            leads_per_ip_per_day=int(os.getenv("LEADS_PER_IP_PER_DAY", "3")),
            llm_daily_call_budget=int(os.getenv("LLM_DAILY_CALL_BUDGET", "1000")),
        )
