# campaign-copilot

An LLM analyst for marketing data. It answers questions like *"which channels beat a 2x ROAS
last quarter, excluding branded search?"* by planning, writing SQL against a governed semantic
layer, executing it in a sandbox, and refusing to state a number it cannot trace to a query.

[![CI](https://github.com/KazmirFahrier/campaign-copilot/actions/workflows/ci.yml/badge.svg)](https://github.com/KazmirFahrier/campaign-copilot/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

> **Status: Phase 0 + guardrails complete.** The warehouse, semantic layer, and SQL guardrail
> are built and tested. The agent loop, RAG, evaluation harness, and deployment are not.
> This README describes what exists; the roadmap at the bottom describes what does not.
> Nothing here claims to be finished.

---

## The thesis

Most LLM-over-SQL demos are a prompt, a schema dump, and a hope. They fail in a specific,
boring, expensive way: the model produces *plausible* arithmetic. Ask one for ROAS by channel
and there is a good chance you get `avg(revenue / spend)` instead of `sum(revenue) / sum(spend)`.

Those are different numbers. On this warehouse:

| channel | `sum(rev)/sum(spend)` | `avg(rev/spend)` | error |
|---|---:|---:|---:|
| paid_search_brand | 9.701 | 9.836 | **+1.4%** |
| paid_social | 0.637 | 0.652 | **+2.4%** |
| affiliate | 1.554 | 1.577 | **+1.5%** |
| display | 0.392 | 0.399 | **+1.5%** |
| video | 0.911 | 0.919 | **+0.9%** |

The bias is small, systematic, and always upward — the worst possible combination, because it
survives a sanity check and inflates every number a client sees. Across the ~$1.46M of modelled
spend, "roughly 1.5% too good" is a number nobody catches and everybody reports.

`campaign-copilot` is built so that this class of error is *structurally impossible* rather
than prompt-discouraged:

- **Metrics are defined once**, in [`semantic/metrics.yml`](semantic/metrics.yml), as
  ratio-of-sums with explicit `nullif` divide-by-zero handling.
- **The SQL guardrail rejects any aggregate** that is not an atom of a registered metric.
  `avg(revenue_usd / spend_usd)` does not reach the database. It raises
  `UNREGISTERED_AGGREGATE` and the error text is fed back to the model.
- **Ambiguity is data.** Each metric carries an `ambiguity` note. `roas` records that campaign
  ROAS excludes direct traffic while `blended_roas` includes it, and that a question which does
  not specify is ambiguous. The agent must either disambiguate or state its assumption — and the
  eval harness scores which it chose.

## Architecture

```
question
   │
   ▼
┌──────────────┐   ambiguity notes    ┌────────────────────┐
│  agent loop  │ ◄────────────────────│  semantic layer    │  metrics.yml
│  (Phase 2)   │   metric expressions │  MetricRegistry    │  single definition
└──────┬───────┘                      └────────────────────┘
       │ candidate SQL
       ▼
┌──────────────────────────────────────────┐
│  SqlGuard  (sqlglot AST, not regex)      │
│  · one statement, SELECT only            │
│  · table allowlist, CTE-aware            │
│  · no read_csv / ATTACH / COPY / INSTALL │
│  · no SELECT *                           │
│  · aggregates ⊆ registered metric atoms  │
│  · LIMIT injected and clamped            │
└──────┬───────────────────────────────────┘
       │ rewritten SQL
       ▼
┌──────────────┐        ┌───────────────────────────────┐
│   DuckDB     │ ◄──────│ dbt: staging → marts          │
│  warehouse   │        │ + 3 singular reconciliation   │
└──────────────┘        │   tests                       │
                        └───────────────────────────────┘
```

## Quick start

No cloud credentials, no API key. Clone and run.

```bash
pip install -e ".[dev,warehouse]"
make warehouse     # generate 1M sessions, build staging + marts
make check         # ruff + mypy --strict + pytest
```

The warehouse is a build artifact and is **not committed**. `SEED = 20260709` makes
`make warehouse` reproducible byte-for-byte. Committing the `.duckdb` file would be a
claim of reproducibility that was never tested.

## What the data is

A deterministic generator, not the BigQuery GA4 public dataset. That is a deliberate trade:
the evaluation harness in Phase 4 needs *verifiable* ground truth, and with a generator the
true ROAS of every campaign is known from the data-generating process rather than from a
second hand-written query that might share the first one's bug.

Structure is baked in so that questions have non-trivial answers: weekly seasonality, a Q4
lift, creative fatigue on `holiday_retarget`, branded search converting ~5x non-brand, and ~3%
of sessions with no campaign attribution (the `direct` pseudo-channel, which has revenue and
zero spend — so its ROAS is `NULL`, not `inf`, and there is a test that says so).

The schema mirrors `bigquery-public-data.google_analytics_sample` closely enough that pointing
this at BigQuery is a dbt profile change, not a rewrite.

## The guardrail

Static analysis on the parsed AST. String matching on the word `DROP` is defeated by `dRoP`,
by `/**/`, and by a semicolon; there is a test for each.

```python
>>> guard.check("select channel, avg(revenue_usd / spend_usd) from main_marts.campaign_performance_daily group by 1")
GuardrailViolation: UNREGISTERED_AGGREGATE: Aggregate 'avg(revenue_usd / spend_usd)' is not an
atom of any registered metric. Metrics are defined once, in semantic/metrics.yml. In particular,
an average of a per-row ratio is not the ratio of the sums, and is wrong.
```

The violation `code` is machine-readable on purpose: it is what gets returned to the model as a
structured tool error, so the repair loop has something to act on instead of retrying the same
hallucination.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | dbt warehouse, semantic layer, CI, packaging | ✅ done |
| 1 | LLM core: provider adapters, structured outputs, token budget, multi-turn memory | ⬜ next |
| 2 | Agent + MCP tool servers (SQL, sandboxed Python, ads API) | 🟨 guardrail done |
| 3 | Hybrid RAG over schema and metric docs, grounded citations | ⬜ |
| 4 | **Evaluation harness** — golden SQL, multi-turn regression, κ-validated LLM judge | ⬜ |
| 5 | Document automation (Docs/Slides API weekly report) | ⬜ |
| 6 | Cloud Run deploy, Terraform, TypeScript streaming UI | ⬜ |

Phase 4 is the point of the project. Everything before it exists to make the evaluation
meaningful, and everything after it exists to make the evaluation observable in production.

## License

MIT.
