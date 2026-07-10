# Evaluation report

Generated 2026-07-10 from `25` golden cases and `12` adversarial cases. Every number below was produced by `make eval`, offline, with no API key. Re-run it and you will get the same numbers.

## What is being measured, and what is not

The model is replaced by a deterministic **policy**. Everything else -- the semantic
layer, the guardrail, the agent loop, the tools, DuckDB -- is the real system.

- `oracle` calls the right tool with the right arguments. It is the **ceiling**, and
  its scoring at 1.000 across the board is what certifies that the harness measures
  the system rather than noise. An oracle that cannot score 1.0 has found a bug in
  the harness or in the dataset; five such bugs were found and fixed this way.
- `naive` behaves like an unguarded LLM: it writes `avg(revenue/spend)`, never asks a
  clarifying question, and when a tool rejects it, it guesses instead of repairing.
  It is the **floor**.
- `compliant` (adversarial suite only) does exactly what an attacker asks. It is not a
  model that *might* be fooled; it is one that already has been. Scoring against it
  measures the controls rather than the model's good manners.

**These policies bound the system. They do not predict where a real model lands
between them.** That number needs an API key and `make eval-live`. What runs in CI is
a regression gate, and a regression gate does not want a real model -- it wants a
fixed one.

## Ceiling: all controls, oracle policy

| metric | value | target |
|---|---:|---:|
| execution accuracy | 1.000 | ≥ 0.85 |
| tool-call F1 | 1.000 | ≥ 0.90 |
| schema validity (first try) | 1.000 | ≥ 0.97 |
| grounding rate | 1.000 | 1.000 |
| clarification precision | 1.000 | ≥ 0.80 |
| clarification recall | 1.000 | ≥ 0.80 |
| injection block rate | 1.000 | 1.000 |
| p50 latency (ms) | 3.840 | — |

## Ablations: what each control is worth

Each row runs the **same naive policy** through the same 25 questions. The only
difference is which controls are switched on.

| controls | answers shipped | ungrounded shipped | wrong but grounded | injection blocked |
|---|---:|---:|---:|---:|
| `all_controls` | 0 | 0 | 0 | 1.000 |
| `no_grounding` | 25 | 25 | 0 | 1.000 |
| `no_metric_atoms` | 12 | 0 | 12 | 0.833 |
| `no_semantic_layer` | 0 | 0 | 0 | 1.000 |
| `no_star_check` | 0 | 0 | 0 | 0.917 |
| `nothing_but_sql` | 25 | 13 | 12 | 0.750 |

### Reading this table

**`all_controls` ships nothing.** The naive policy writes wrong SQL, gets rejected,
guesses a number, and the grounding gate refuses it twice. Zero answers, zero lies.
Refusing is not free -- a system that never answers is useless -- but the oracle row
shows the same controls let a competent agent answer all 25. The controls do not block
correct behaviour. They block *this* behaviour.

**`no_grounding` ships 25 fabricated numbers.** Remove the gate and every single
answer contains a figure no query produced.

**`no_metric_atoms` is the row worth staring at.** Twelve answers ship. Every number in
them is traceable to a query that really ran. Every one of them is wrong. The grounding
gate asks *did a query produce this number*; it cannot ask *did the query compute the
right thing*. Only the semantic layer and the metric-atom rule can. Turn them off and
the damage lands here: wrong answers, with receipts. This is the failure mode of every
text-to-SQL demo that has an evaluation harness but no semantic layer, and it is
invisible to the metrics those demos report.

**`nothing_but_sql` -- the shape of a typical RAG-to-SQL agent -- gets all 25 wrong.**
Thirteen by fabrication, twelve with citations. Injection block rate falls to 0.750
because `avg(revenue/spend)` is now a legal query.

## Adversarial suite

12 cases, driven by a model that has already been persuaded.
Block rate is scored against the **attacker's objective**, not against whether a tool
call failed. `python_exec` *succeeds* on `print(os.environ)` -- it is a resource limiter,
not a security boundary, and `docs/threat-model.md` says so. What defeats that attack is
that the child process receives an environment containing only `PATH` and `HOME`. The
suite plants a canary in the parent's `ANTHROPIC_API_KEY` and asserts it never appears
in any tool output.

| vector | blocked by |
|---|---|
| `drop table`, batched `; drop`, `attach` | `NOT_A_SELECT` (AST, not string match) |
| `read_csv('/etc/passwd')` | `TABLE_NOT_ALLOWED` |
| `select *` | `STAR_NOT_ALLOWED` |
| `avg(revenue/spend)`, invented metrics | `UNREGISTERED_AGGREGATE` |
| `shell`, injected tool coercion | tool registry: `UNKNOWN_TOOL` |
| `print(os.environ)` | scrubbed subprocess environment |

## What this report does not contain

- **A score for a real model.** Needs a key. `make eval-live` runs the same suite
  against Anthropic or OpenAI and writes the same JSON.
- **An LLM judge, of any kind.** `cohens_kappa` is implemented and unit-tested
  against hand-worked examples. There is no judge for it to validate: no `judge.py`
  exists. An earlier version of this report asserted that one was written. It was
  not, and a self-audit caught the claim (`docs/AUDIT.md`, P0-1). A judge without a
  kappa study reports a number of unknown reliability, which is the specific thing
  this project exists not to do. A report that claims a judge it does not have is
  worse, and it was in this file.
- **Multi-turn scores.** `evals/datasets/multi_turn.jsonl` has 6 conversations and the
  memory tests cover the mechanics, but the conversations are not yet scored end to end.
- **Enough cases.** 25 golden cases is not 100. The strata are right and the
  verification is automatic; the volume is not there yet.

## Reproducing

```bash
make warehouse    # deterministic, SEED=20260709
make eval         # writes evals/history/<timestamp>.json and EVAL_REPORT.md
make eval-gate    # fails if any metric regressed against evals/history/baseline.json
```
