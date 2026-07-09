# campaign-copilot

An LLM analyst for marketing data. It answers questions like *"which channels beat a 2x ROAS
last quarter, excluding branded search?"* by planning, writing SQL against a governed semantic
layer, executing it in a sandbox, and refusing to state a number it cannot trace to a query.

[![CI](https://github.com/KazmirFahrier/campaign-copilot/actions/workflows/ci.yml/badge.svg)](https://github.com/KazmirFahrier/campaign-copilot/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

> **Status: Phases 0-1 complete.** The warehouse, semantic layer, SQL guardrail, and LLM
> core are built and tested (85 tests, 87% branch coverage, `mypy --strict`). The agent
> loop, RAG, evaluation harness, and deployment are not. This README describes what
> exists; the roadmap at the bottom describes what does not. Nothing here claims to be
> finished.

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

## The LLM core

Nothing in `src/campaign_copilot/llm/` imports a vendor SDK at module scope. The agent
depends on the `LLMClient` protocol; `AnthropicClient` and `OpenAIClient` import their SDKs
lazily inside `__init__`, and `ScriptedClient` replays a fixed list of responses. That last
one is why the Phase 4 regression suite can run the whole agent loop in CI, deterministically,
with no key and no cost.

Three pieces do the real work:

- **`structured.py`** — every response is a validated Pydantic model. On a validation failure
  the *specific* error is fed back to the model, which is the difference between a repair and
  a retry. The loop is bounded at `max_repairs`; an unbounded repair loop is a cost incident.
  `RepairStats.valid_first_try` is recorded per call and aggregates into the
  `schema_validity_rate` metric in the eval report.
- **`tokens.py`** — `HeuristicCounter` divides by 3.2 rather than 4, so it *over*-estimates.
  A budget that passes offline must pass at the provider; erring low is the bug. Eviction is
  newest-first, explicit, and returned to the caller. If the non-evictable content alone
  overflows, it raises rather than silently truncating.
- **`memory.py`** — verbatim buffer for pronoun resolution, rolling summary for everything
  older, and **pinned facts that never evict**. A summary that quietly drops "exclude branded
  search" produces an answer that is wrong in the one way the user explicitly asked it not
  to be. There is a test named after exactly that.

Prompts are files with content hashes (`prompts.py`), not f-strings. Every eval run records
the fingerprint, so a metric regression can be bisected to a prompt change.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | dbt warehouse, semantic layer, CI, packaging | ✅ done |
| 1 | LLM core: provider adapters, structured outputs, token budget, multi-turn memory | ✅ done |
| 2 | Agent + MCP tool servers (SQL, sandboxed Python, ads API) | 🟨 guardrail done |
| 3 | Hybrid RAG over schema and metric docs, grounded citations | ⬜ |
| 4 | **Evaluation harness** — golden SQL, multi-turn regression, κ-validated LLM judge | ⬜ |
| 5 | Document automation (Docs/Slides API weekly report) | ⬜ |
| 6 | Cloud Run deploy, Terraform, TypeScript streaming UI | ⬜ |

Phase 4 is the point of the project. Everything before it exists to make the evaluation
meaningful, and everything after it exists to make the evaluation observable in production.

## License

MIT.
