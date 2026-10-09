"""Runtime configuration, read once from environment variables."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


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
    allowed_origins: list[str] = field(default_factory=lambda: ["*"])
    min_retrieval_score: float = 2.0
    max_message_chars: int = 1000
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
        origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
        return cls(
            model=os.getenv("CLAUDE_MODEL", DEFAULT_MODEL),
            mock_llm=mock,
            api_key=api_key,
            data_dir=Path(os.getenv("DATA_DIR", ROOT / "data")),
            leads_path=Path(os.getenv("LEADS_PATH", ROOT / "leads.csv")),
            lead_webhook_url=os.getenv("LEAD_WEBHOOK_URL") or None,
            allowed_origins=origins or ["*"],
            min_retrieval_score=float(os.getenv("MIN_RETRIEVAL_SCORE", "2.0")),
        )
