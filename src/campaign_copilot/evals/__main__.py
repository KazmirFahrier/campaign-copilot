"""`python -m campaign_copilot.evals` -- run the suite, write the report, gate on regression."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from campaign_copilot.evals.dataset import load_adversarial, load_golden
from campaign_copilot.evals.report import (
    BASELINE,
    check_regression,
    render_report,
    save_history,
)
from campaign_copilot.evals.runner import Ablation, EvalRunner

GRID: list[tuple[Ablation, str]] = [
    (Ablation("all_controls"), "oracle"),
    (Ablation("all_controls"), "naive"),
    (Ablation("no_grounding", grounding=False), "naive"),
    (Ablation("no_metric_atoms", metric_atoms=False), "naive"),
    (Ablation("no_semantic_layer", semantic_layer=False), "naive"),
    (
        Ablation("nothing_but_sql", grounding=False, metric_atoms=False, semantic_layer=False),
        "naive",
    ),
]


def main(argv: list[str] | None = None) -> int:
    """Run the ablation grid."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="write markdown here")
    parser.add_argument("--gate", action="store_true", help="fail on regression vs baseline")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--no-history", action="store_true")
    args = parser.parse_args(argv)

    golden, adversarial = load_golden(), load_adversarial()
    rows = [
        EvalRunner(ablation=ablation).run_suite(golden, adversarial, policy=policy).as_dict()
        for ablation, policy in GRID
    ]

    ceiling = next(
        r for r in rows if r["ablation"] == "all_controls" and r["policy"] == "oracle"
    )
    print(json.dumps(ceiling, indent=2))

    if not args.no_history:
        print(f"\nwrote {save_history(rows)}", file=sys.stderr)
    if args.report:
        args.report.write_text(render_report(rows), encoding="utf-8")
        print(f"wrote {args.report}", file=sys.stderr)
    if args.write_baseline:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(ceiling, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {BASELINE}", file=sys.stderr)

    if args.gate:
        if not BASELINE.exists():
            print("no baseline; run --write-baseline first", file=sys.stderr)
            return 1
        failures = check_regression(ceiling, json.loads(BASELINE.read_text()))
        if failures:
            print("\nREGRESSION:", file=sys.stderr)
            for failure in failures:
                print(f"  - {failure}", file=sys.stderr)
            return 1
        print("\nno regression against baseline", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
