"""Dependency-free keyword retrieval (BM25) over the business's Markdown content.

Each `## ` section of each file in `data/` becomes one chunk. Chunks are small and
self-contained, so retrieving the top 3 gives the model enough context to answer.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

STOPWORDS = frozenset(
    """a an and are as at be by can could do does did for from have has how i if in is it its
    me my of on or our please should so than that the their them there these they this to
    us was we what when where which who why will with would you your yours i'm im i'd
    about all also any get give just know like make need some take tell want""".split()
)

# A tiny, hand-written synonym map. It fixes the most common vocabulary gaps between
# how visitors ask and how the clinic writes, without pulling in an embedding model.
SYNONYMS = {
    "cost": ["price"],
    "costs": ["price"],
    "much": ["price"],
    "expensive": ["price"],
    "fee": ["price"],
    "fees": ["price"],
    "pay": ["payment"],
    "open": ["opening", "hours"],
    "close": ["opening", "hours", "closed"],
    "weekend": ["saturday", "sunday"],
    "address": ["location"],
    "where": ["location", "address"],
    "park": ["parking"],
    "appointment": ["book"],
    "booking": ["book"],
    "schedule": ["book"],
    "kid": ["children", "child"],
    "kids": ["children", "child"],
    "child": ["children"],
    "son": ["children", "child"],
    "daughter": ["children", "child"],
    "insurer": ["insurance"],
    "insurers": ["insurance"],
    "cancel": ["cancellation"],
    "white": ["whitening"],
    "whiten": ["whitening"],
    "toothache": ["emergency"],
    "pain": ["emergency", "toothache"],
    "cleaning": ["check-up"],
    "checkup": ["check-up"],
    "aligners": ["braces", "orthodontic"],
    "invisalign": ["aligners", "braces", "orthodontic"],
    "anxious": ["nervous"],
    "scared": ["nervous"],
    "afraid": ["nervous"],
}

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-']*")


def _stem(token: str) -> str:
    """Very light stemming: enough to match 'fillings' with 'filling'."""
    token = token.strip("'-")
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text: str, expand: bool = False) -> list[str]:
    out: list[str] = []
    for raw in TOKEN_RE.findall(text.lower()):
        raw = re.sub(r"'(s|re|ve|ll|d|m|t)?$", "", raw)
        if raw in STOPWORDS:
            continue
        words = [raw] + (SYNONYMS.get(raw, []) if expand else [])
        out.extend(_stem(w) for w in words)
    return [t for t in out if t]


@dataclass(frozen=True)
class Chunk:
    source: str  # file name, e.g. "prices.md"
    title: str  # section heading
    text: str  # section body (Markdown)

    def render(self) -> str:
        return f"[{self.source} > {self.title}]\n{self.text.strip()}"


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float


def load_chunks(data_dir: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(data_dir.glob("*.md")):
        title, lines = None, []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                if title is not None:
                    chunks.append(Chunk(path.name, title, "\n".join(lines)))
                title, lines = line[3:].strip(), []
            elif title is not None:
                lines.append(line)
        if title is not None:
            chunks.append(Chunk(path.name, title, "\n".join(lines)))
    return chunks


class Retriever:
    """Okapi BM25. Headings are counted twice so a section's topic weighs more than a passing mention."""

    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75):
        if not chunks:
            raise ValueError("No content chunks found; is DATA_DIR correct?")
        self.chunks = chunks
        self.k1, self.b = k1, b
        self.docs = [Counter(tokenize(f"{c.title} {c.title} {c.text}")) for c in chunks]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg_len = sum(self.lengths) / len(self.lengths)
        df: Counter[str] = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    @classmethod
    def from_dir(cls, data_dir: Path) -> "Retriever":
        return cls(load_chunks(data_dir))

    def coverage(self, query: str) -> float:
        """Share of the query's content words (or their synonyms) that appear anywhere in our content.

        BM25 alone happily matches "best pizza *place* in town" to "we *place* implants".
        Requiring most of the question's words to be known filters such one-word coincidences.
        """
        words = tokenize(query)
        if not words:
            return 0.0
        known = sum(1 for w in words if w in self.idf or any(t in self.idf for t in tokenize(w, expand=True)))
        return known / len(words)

    def search(self, query: str, k: int = 3) -> list[Hit]:
        terms = set(tokenize(query, expand=True))
        hits = []
        for chunk, doc, length in zip(self.chunks, self.docs, self.lengths):
            score = 0.0
            for t in terms:
                tf = doc.get(t, 0)
                if tf:
                    norm = tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * length / self.avg_len))
                    score += self.idf[t] * norm
            if score > 0:
                hits.append(Hit(chunk, round(score, 3)))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]
