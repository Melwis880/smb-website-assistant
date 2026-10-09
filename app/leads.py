"""Lead validation and storage. Deterministic code, no model involved."""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import threading
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# The consent text promises this, and LeadStore.purge() enforces it. Not configurable on purpose:
# changing it means changing what visitors agreed to (new CONSENT_TEXT_VERSION).
RETENTION_DAYS = 90
CONSENT_TEXT_VERSION = "v2"
CONSENT_TEXT = (
    "May we store your name, contact details and preferred time so our front desk can contact "
    f"you about this appointment? We automatically delete this request after {RETENTION_DAYS} days. "
    "Please answer yes or no."
)

NAME_RE = re.compile(r"^[^\W\d_](?:[^\W\d_]|[ '\-.])*$", re.UNICODE)
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")
PHONE_ALLOWED_RE = re.compile(r"^\+?[\d\s\-().]+$")

YES = {"yes", "y", "yeah", "yep", "sure", "ok", "okay", "i agree", "agree", "i consent", "yes please"}
NO = {"no", "n", "nope", "no thanks", "i do not agree", "i don't agree", "don't", "do not"}


class ValidationError(ValueError):
    """Raised with a visitor-friendly message."""


def validate_name(raw: str) -> str:
    name = raw.strip().strip(".")
    for prefix in ("my name is ", "i am ", "i'm ", "this is ", "name: "):
        if name.lower().startswith(prefix):
            name = name[len(prefix):].strip()
    if not 2 <= len(name) <= 80 or not NAME_RE.match(name):
        raise ValidationError("Please enter just your name (letters only, 2-80 characters).")
    return " ".join(name.split())


def validate_contact(raw: str) -> tuple[str, str]:
    """Returns (normalised_contact, kind) where kind is 'email' or 'phone'."""
    value = raw.strip().rstrip(".")
    if "@" in value:
        if len(value) <= 254 and EMAIL_RE.match(value):
            return value.lower(), "email"
        raise ValidationError("That email address doesn't look right. Could you check it?")
    digits = re.sub(r"\D", "", value)
    if PHONE_ALLOWED_RE.match(value) and 7 <= len(digits) <= 15:
        return ("+" if value.startswith("+") else "") + digits, "phone"
    raise ValidationError("Please give an email address or a phone number (7-15 digits).")


def validate_preferred_time(raw: str) -> str:
    value = " ".join(raw.split())
    if not 2 <= len(value) <= 120:
        raise ValidationError("Please describe your preferred day/time in a few words (max 120 characters).")
    if re.search(r"https?://|www\.", value, re.IGNORECASE):
        raise ValidationError("Please describe your preferred day/time without links.")
    return value


def parse_consent(raw: str) -> bool | None:
    value = raw.strip().lower().rstrip(".!")
    if value in YES:
        return True
    if value in NO:
        return False
    return None


def csv_safe(value: str) -> str:
    """Neutralise spreadsheet formula injection (=, +, -, @ at the start of a cell)."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


@dataclass
class Lead:
    name: str
    contact: str
    contact_type: str
    preferred_time: str
    session_id: str
    consent: bool = True
    consent_text_version: str = CONSENT_TEXT_VERSION
    created_at: str = ""
    source: str = "website-chat"

    def __post_init__(self) -> None:
        if not self.consent:
            raise ValueError("A lead must not be stored without consent.")
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


FIELDS = list(Lead.__dataclass_fields__)


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def validate_webhook_url(url: str) -> str:
    """Leads are personal data: only send them over https (plain http only to this machine)."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"LEAD_WEBHOOK_URL has no host: {url!r}")
    if parts.scheme == "https" or (parts.scheme == "http" and host in LOCAL_HOSTS):
        return url
    raise ValueError("LEAD_WEBHOOK_URL must use https:// (http:// is only allowed for localhost/127.0.0.1)")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect could forward lead data (and the shared secret) to another host: refuse it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None  # urllib then raises HTTPError for the 3xx response


_WEBHOOK_OPENER = urllib.request.build_opener(_NoRedirect)


def _open_private(path: Path, mode: str):
    """Open for writing; a newly created file gets 0600 (owner read/write only), whatever the umask."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if mode == "a" else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    return os.fdopen(fd, mode, newline="", encoding="utf-8")


class LeadStore:
    def __init__(self, path: Path, webhook_url: str | None = None, webhook_secret: str | None = None):
        self.path = path
        self.webhook_url = validate_webhook_url(webhook_url) if webhook_url else None
        self.webhook_secret = webhook_secret
        self._lock = threading.Lock()

    def save(self, lead: Lead) -> None:
        row = {k: csv_safe(str(v)) for k, v in asdict(lead).items()}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not self.path.exists() or self.path.stat().st_size == 0
            with _open_private(self.path, "a") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDS)
                if new_file:
                    writer.writeheader()
                writer.writerow(row)
        if self.webhook_url:
            # In the background: a slow or down n8n must not delay the visitor's reply.
            threading.Thread(target=self._notify, args=(lead,), daemon=True).start()

    def _notify(self, lead: Lead) -> None:
        """Best-effort POST to an automation webhook (e.g. the n8n workflow in n8n/)."""
        headers = {"Content-Type": "application/json"}
        if self.webhook_secret:
            headers["X-Webhook-Secret"] = self.webhook_secret
        try:
            req = urllib.request.Request(
                self.webhook_url, data=json.dumps(asdict(lead)).encode(), headers=headers, method="POST"
            )
            _WEBHOOK_OPENER.open(req, timeout=5).close()
        except Exception as exc:  # the lead is already saved; never fail the chat on this
            log.warning("Lead webhook failed: %s", exc)

    def purge(self, older_than_days: int = RETENTION_DAYS, now: datetime | None = None) -> int:
        """Delete leads older than `older_than_days`. Returns how many rows were removed.

        Rewrites the file atomically (temp file + rename), keeping 0600 permissions.
        Rows with an unreadable timestamp are kept and logged: deleting data we can't date
        would hide a problem, and keeping it is visible in the log.
        """
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=older_than_days)
        with self._lock:
            if not self.path.exists():
                return 0
            with self.path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            keep = []
            for row in rows:
                try:
                    created = datetime.fromisoformat(row.get("created_at", "").lstrip("'"))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                except ValueError:
                    log.warning("Lead with unreadable created_at kept: %r", row.get("created_at"))
                    keep.append(row)
                    continue
                if created >= cutoff:
                    keep.append(row)
            removed = len(rows) - len(keep)
            if removed:
                tmp = self.path.with_name(self.path.name + ".tmp")
                with _open_private(tmp, "w") as f:
                    writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(keep)
                os.replace(tmp, self.path)
        if removed:
            log.info("Purged %d lead(s) older than %d days", removed, older_than_days)
        return removed

    def read_all(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        with self.path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
