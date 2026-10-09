"""Prompt-injection hygiene.

Defence in depth, none of which relies on the model "behaving":
1. Visitor text is length-capped and stripped of control characters.
2. Obvious injection phrasing is caught before any model call (cheap, deterministic).
3. Visitor text is passed to the model inside <visitor_message> tags and the system
   prompt says that content is data, never instructions.
4. The model's output is checked for a canary string that only exists in the system
   prompt; if it leaks, the answer is replaced.
Lead capture and consent never depend on the model at all (see chat.py).
"""

from __future__ import annotations

import re
import unicodedata

CANARY = "BSD-CANARY-7f3a9c"

_PATTERNS = [
    # "ignore (all) (your/the/previous...) instructions", but not "ignore my previous dentist's instructions"
    r"\b(ignore|disregard|forget|override|bypass)\s+(?:(?:all|any|of|the|your|these|those|previous|prior|above|earlier|preceding|system)\s+){1,4}(instructions?|rules?|prompts?|directions?|guidelines?)\b",
    r"\b(system|developer|hidden|initial)\s+(prompt|message|instructions?)\b",
    r"\byou\s+are\s+now\b",
    r"\b(act|pretend|roleplay)\s+(as|like)\b",
    r"\b(pretend|imagine)\s+(that\s+)?(you\s*(are|'re)|to\s+be)\b",
    r"\bnew\s+(instructions?|rules?|persona)\b",
    r"\b(jailbreak|dan\s+mode|developer\s+mode)\b",
    r"</?\s*(system|visitor_message|context|instructions?)\s*>",
    r"\breveal\b.{0,30}\b(prompt|instructions?|rules?|configuration|api\s*key)\b",
    # "show your instructions", "print the system prompt", but not "show me the aftercare instructions"
    r"\b(print|repeat|output|show)\b.{0,30}\b(your\s+(system\s+)?(prompt|instructions?)|system\s+prompt|everything\s+above)\b",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in _PATTERNS), re.IGNORECASE | re.DOTALL)


def clean_text(text: str, max_chars: int) -> str:
    """Normalise unicode, drop control characters, collapse whitespace, cap length."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def looks_like_injection(text: str) -> bool:
    return bool(_INJECTION_RE.search(text))


def leaks_system_prompt(output: str) -> bool:
    return CANARY in output
