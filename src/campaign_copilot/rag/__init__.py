"""Hybrid retrieval over the semantic layer, the schema, and analyst memos."""

from campaign_copilot.rag.corpus import Chunk, build_corpus, chunk_markdown
from campaign_copilot.rag.index import (
    BM25Index,
    DenseIndex,
    Embedder,
    HashingEmbedder,
    HybridRetriever,
    OpenAIEmbedder,
    SearchHit,
    reciprocal_rank_fusion,
    tokenize,
)
from campaign_copilot.rag.safety import (
    DATA_NOTICE,
    InjectionScan,
    scan_for_injection,
    wrap_untrusted,
)

__all__ = [
    "DATA_NOTICE",
    "BM25Index",
    "Chunk",
    "DenseIndex",
    "Embedder",
    "HashingEmbedder",
    "HybridRetriever",
    "InjectionScan",
    "OpenAIEmbedder",
    "SearchHit",
    "build_corpus",
    "chunk_markdown",
    "reciprocal_rank_fusion",
    "scan_for_injection",
    "tokenize",
    "wrap_untrusted",
]
