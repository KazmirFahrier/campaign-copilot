"""`python -m campaign_copilot.reporting` -- generate the weekly review."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from campaign_copilot.reporting.builder import build_weekly_review
from campaign_copilot.reporting.render import render_markdown, render_pptx
from campaign_copilot.semantic.layer import SemanticLayer
from campaign_copilot.tools.sql import Warehouse

DEFAULT_DB = Path("warehouse/campaign_copilot.duckdb")


def main(argv: list[str] | None = None) -> int:
    """Build the review and render it. Fails loudly if any number cannot be reproduced."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week-end", type=date.fromisoformat, default=date(2025, 12, 31))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=Path("reports"))
    args = parser.parse_args(argv)

    warehouse = Warehouse(args.db)
    review = build_weekly_review(SemanticLayer.load(), warehouse, args.week_end)

    problems = review.figures.verify(lambda sql: warehouse.execute(sql)[1])
    if problems:
        for problem in problems:
            print(f"PROVENANCE FAILURE: {problem}")
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    stem = f"weekly-review-{args.week_end:%Y-%m-%d}"
    (args.out / f"{stem}.md").write_text(render_markdown(review), encoding="utf-8")
    render_pptx(review, args.out / f"{stem}.pptx")
    count = len(review.figures.figures)
    print(f"wrote {args.out / stem}.md and .pptx ({count} figures verified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
