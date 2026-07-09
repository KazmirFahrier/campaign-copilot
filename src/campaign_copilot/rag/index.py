"""Hybrid retrieval: BM25 + dense, fused by reciprocal rank.

Why both. Lexical search finds `paid_search_nonbrand` when the user types
`paid_search_nonbrand`; dense search finds it when the user types "non-branded paid
search". Metric names and column names are exactly the kind of rare, high-signal token
that embeddings smear and BM25 nails, and analyst prose is exactly the opposite. Choosing
one is choosing which half of your questions to answer badly.

On the embedder. :class:`HashingEmbedder` runs offline, deterministically, with no model
download and no API key, which is what makes this testable in CI. It is **lexical, not
semantic**: hashed word features with sublinear term-frequency weighting and L2
normalization. It will not know that "spend" and "cost" are related. It is a stand-in that
keeps the retrieval *plumbing* honest -- fusion, ranking, citation spans -- while the
:class:`OpenAIEmbedder` is what runs in production. Calling it an embedding model would be
the same category of lie as calling `python_exec` a sandbox, so it is not called that here.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from campaign_copilot.rag.corpus import Chunk

__all__ = [
    "BM25Index",
    "DenseIndex",
    "Embedder",
    "HashingEmbedder",
    "HybridRetriever",
    "OpenAIEmbedder",
    "SearchHit",
    "reciprocal_rank_fusion",
    "tokenize",
]

_WORD = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, plus the intact underscored identifier.

    `paid_search_brand` yields `paid`, `search`, `brand` *and* `paid_search_brand`. Losing
    the compound makes the query "paid_search_brand ROAS" match every paid channel; losing
    the parts makes "branded search" match nothing.
    """
    lowered = text.lower()
    tokens = _WORD.findall(lowered)
    compounds = re.findall(r"[a-z0-9]+(?:_[a-z0-9]+)+", lowered)
    return [*tokens, *compounds]


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A retrieved chunk and the score that retrieved it."""

    chunk: Chunk
    score: float


# --------------------------------------------------------------------------- bm25


@dataclass
class BM25Index:
    """Okapi BM25 over the corpus. Hand-rolled: forty lines, no dependency, testable."""

    chunks: Sequence[Chunk]
    k1: float = 1.5
    b: float = 0.75
    _docs: list[list[str]] = field(init=False, repr=False)
    _df: Counter[str] = field(init=False, repr=False)
    _avg_len: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Tokenize the corpus and precompute document frequencies."""
        self._docs = [tokenize(c.text) for c in self.chunks]
        self._df = Counter()
        for doc in self._docs:
            self._df.update(set(doc))
        self._avg_len = (
            (sum(len(d) for d in self._docs) / len(self._docs)) if self._docs else 0.0
        )

    def _idf(self, term: str) -> float:
        n = len(self._docs)
        df = self._df.get(term, 0)
        # Robertson/Sparck Jones idf, floored at zero so a term in every document
        # contributes nothing rather than pulling scores negative.
        return max(0.0, math.log((n - df + 0.5) / (df + 0.5) + 1.0))

    def search(self, query: str, k: int = 5) -> list[SearchHit]:
        """Return the ``k`` highest-scoring chunks."""
        terms = tokenize(query)
        scored: list[SearchHit] = []
        for chunk, doc in zip(self.chunks, self._docs, strict=True):
            counts = Counter(doc)
            length = len(doc) or 1
            score = 0.0
            for term in terms:
                tf = counts.get(term, 0)
                if not tf:
                    continue
                denom = tf + self.k1 * (1 - self.b + self.b * length / (self._avg_len or 1))
                score += self._idf(term) * (tf * (self.k1 + 1)) / denom
            if score > 0:
                scored.append(SearchHit(chunk, score))
        scored.sort(key=lambda h: (-h.score, h.chunk.chunk_id))
        return scored[:k]


# --------------------------------------------------------------------- embeddings


class Embedder(Protocol):
    """Anything that maps text to a fixed-width vector."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch."""
        ...


@dataclass(frozen=True, slots=True)
class HashingEmbedder:
    """Deterministic, offline, and lexical. Not a semantic model; see the module docstring."""

    dim: int = 512

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for term, count in Counter(tokenize(text)).items():
            digest = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[bucket] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch, deterministically."""
        return [self._vector(t) for t in texts]


class OpenAIEmbedder:
    """Production embedder. Imported lazily; never touched by the test suite."""

    def __init__(self, model: str = "text-embedding-3-small") -> None:
        """Import the SDK lazily so the package installs without it."""
        import openai

        self._client = openai.OpenAI()
        self.model = model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch via the API."""
        response = self._client.embeddings.create(model=self.model, input=list(texts))
        return [item.embedding for item in response.data]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


@dataclass
class DenseIndex:
    """Brute-force cosine search over the corpus.

    Below roughly ten thousand chunks an ANN index is complexity without benefit. The
    production swap is pgvector, which is a storage change rather than a rewrite here.
    """

    chunks: Sequence[Chunk]
    embedder: Embedder
    _vectors: list[list[float]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Embed the corpus once."""
        self._vectors = self.embedder.embed([c.text for c in self.chunks])

    def search(self, query: str, k: int = 5) -> list[SearchHit]:
        """Return the ``k`` nearest chunks by cosine similarity."""
        (q,) = self.embedder.embed([query])
        hits = [
            SearchHit(chunk, _cosine(q, vec))
            for chunk, vec in zip(self.chunks, self._vectors, strict=True)
        ]
        hits.sort(key=lambda h: (-h.score, h.chunk.chunk_id))
        return [h for h in hits[:k] if h.score > 0]


# ------------------------------------------------------------------------ fusion


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[SearchHit]], *, k: int = 60, top_k: int = 5
) -> list[SearchHit]:
    """Fuse ranked lists by reciprocal rank.

    RRF ignores the scores and uses only the positions, which is the point: BM25 scores and
    cosine similarities are not on a comparable scale, and normalizing them is a hyper-
    parameter nobody tunes and everybody gets wrong.
    """
    scores: dict[str, float] = {}
    seen: dict[str, Chunk] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            scores[hit.chunk.chunk_id] = scores.get(hit.chunk.chunk_id, 0.0) + 1.0 / (k + rank)
            seen[hit.chunk.chunk_id] = hit.chunk
    fused = [SearchHit(seen[cid], score) for cid, score in scores.items()]
    fused.sort(key=lambda h: (-h.score, h.chunk.chunk_id))
    return fused[:top_k]


@dataclass
class HybridRetriever:
    """BM25 and dense, fused. The only retriever the tools should ever hold."""

    bm25: BM25Index
    dense: DenseIndex
    candidates: int = 10

    @classmethod
    def build(
        cls, chunks: Sequence[Chunk], embedder: Embedder | None = None
    ) -> HybridRetriever:
        """Construct both indexes over the same corpus."""
        return cls(
            bm25=BM25Index(chunks),
            dense=DenseIndex(chunks, embedder or HashingEmbedder()),
        )

    def search(self, query: str, k: int = 5) -> list[SearchHit]:
        """Retrieve ``k`` chunks, fusing lexical and dense candidates."""
        return reciprocal_rank_fusion(
            [
                self.bm25.search(query, self.candidates),
                self.dense.search(query, self.candidates),
            ],
            top_k=k,
        )
