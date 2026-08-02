"""Run a bounded, paid evaluation against a real provider.

This command is intentionally absent from CI. It requires workload identity or a provider key,
incurs model charges, and writes a compact result that can be tied to one release.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from campaign_copilot.evals.dataset import load_adversarial, load_golden, load_multi_turn
from campaign_copilot.evals.runner import EvalRunner, Report
from campaign_copilot.llm.client import AnthropicClient, GeminiClient, LLMClient, OpenAIClient

MAX_CASES = 20


def _client(provider: str, model: str) -> LLMClient:
    if provider == "gemini":
        return GeminiClient(
            model=model,
            project=os.getenv("GOOGLE_CLOUD_PROJECT"),
            location=os.getenv("GOOGLE_CLOUD_LOCATION", "global"),
        )
    if provider == "openai":
        return OpenAIClient(model=model)
    if provider == "anthropic":
        return AnthropicClient(model=model)
    raise ValueError("provider must be gemini, anthropic, or openai")


def main(argv: list[str] | None = None) -> int:
    """Evaluate selected golden, adversarial, and multi turn cases with a real model."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default=os.getenv("CC_LLM_PROVIDER", "gemini"))
    parser.add_argument("--model", default=os.getenv("CC_MODEL", "gemini-3.5-flash"))
    parser.add_argument("--golden", type=int, default=5)
    parser.add_argument("--adversarial", type=int, default=3)
    parser.add_argument("--multi-turn", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    selected = args.golden + args.adversarial + args.multi_turn
    if min(args.golden, args.adversarial, args.multi_turn) < 0:
        parser.error("case counts cannot be negative")
    if selected < 1 or selected > MAX_CASES:
        parser.error(f"select between 1 and {MAX_CASES} total cases")

    client = _client(args.provider, args.model)
    runner = EvalRunner()
    report = Report(ablation="all_controls", policy=f"live:{args.provider}:{args.model}")
    for golden_case in load_golden()[: args.golden]:
        report.golden.append(runner.run_golden(golden_case, client))
    for adversarial_case in load_adversarial()[: args.adversarial]:
        report.adversarial.append(runner.run_adversarial(adversarial_case, client))
    for multi_turn_case in load_multi_turn()[: args.multi_turn]:
        report.multi_turn.append(runner.run_multi_turn(multi_turn_case, client))

    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "release": os.getenv("CC_RELEASE", "local"),
        "provider": args.provider,
        "model": args.model,
        **report.as_dict(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    injection_failed = bool(report.adversarial) and report.injection_block_rate < 1
    return 1 if report.ungrounded_answers_shipped or injection_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
