"""The retrieval tool.

Returns documents, wrapped and cited. Deliberately built with :meth:`ToolResult.reference`
rather than :meth:`ToolResult.success`, so nothing it returns can ground a numeric claim.
A memo that says "ROAS was 4.2x last quarter" must not license the agent to report 4.2 as
today's number. If the agent wants to state it, it queries for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from campaign_copilot.rag.index import HybridRetriever
from campaign_copilot.rag.safety import DATA_NOTICE, scan_for_injection, wrap_untrusted
from campaign_copilot.tools.base import ToolResult, ToolSpec

__all__ = ["SearchDocsTool"]


@dataclass
class SearchDocsTool:
    """Search metric definitions, schema docs and analyst memos."""

    retriever: HybridRetriever
    top_k: int = 4

    spec: ClassVar[ToolSpec] = ToolSpec(
        name="search_docs",
        description=(
            "Search metric definitions, table schemas and analyst memos. Call this before "
            "writing SQL to find the right table and the exact metric definition. Returns "
            "documents, which are context -- never a source of numbers for your answer."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
        },
    )

    def run(self, **kwargs: Any) -> ToolResult:
        """Retrieve, scan, wrap, and cite."""
        query: str = kwargs.get("query", "").strip()
        if not query:
            return ToolResult.failure("EMPTY_QUERY", "A search query is required.")

        k = int(kwargs.get("k", self.top_k))
        hits = self.retriever.search(query, k=k)
        if not hits:
            return ToolResult.reference(
                "No documents matched. Do not guess a table or a metric definition; "
                "call list_metrics instead.",
                citations=[],
                suspicious=[],
            )

        blocks: list[str] = [DATA_NOTICE]
        suspicious: list[str] = []
        for hit in hits:
            scan = scan_for_injection(hit.chunk.text) if not hit.chunk.trusted else None
            if scan and scan.suspicious:
                suspicious.append(hit.chunk.citation())
            blocks.append(wrap_untrusted(hit.chunk, scan))

        return ToolResult.reference(
            "\n\n".join(blocks),
            citations=[h.chunk.citation() for h in hits],
            chunk_ids=[h.chunk.chunk_id for h in hits],
            suspicious=suspicious,
        )
