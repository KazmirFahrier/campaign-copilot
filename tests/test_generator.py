"""Generator tests.

The eval harness in Phase 4 checks agent answers against ground truth derived from the
data-generating process. That is only sound if the generator is (a) deterministic given
``SEED`` and (b) actually contains the structure the golden questions ask about. Both are
asserted here, at a small ``scale`` so the suite stays under a second.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from campaign_copilot.warehouse.generate import SEED, generate, load

SCALE = 0.01


@pytest.fixture(scope="module")
def frames() -> dict[str, pd.DataFrame]:
    return generate(np.random.default_rng(SEED), scale=SCALE)


# --------------------------------------------------------------- reproducibility


def test_generation_is_deterministic_given_the_seed() -> None:
    """If this fails, every number in EVAL_REPORT.md is unfalsifiable."""
    a = generate(np.random.default_rng(SEED), scale=SCALE)
    b = generate(np.random.default_rng(SEED), scale=SCALE)
    for name in a:
        pd.testing.assert_frame_equal(a[name], b[name])


def test_a_different_seed_produces_different_data() -> None:
    a = generate(np.random.default_rng(SEED), scale=SCALE)
    b = generate(np.random.default_rng(SEED + 1), scale=SCALE)
    assert not a["raw_ad_performance"].equals(b["raw_ad_performance"])


# -------------------------------------------------------------------- invariants


def test_expected_tables_are_produced(frames: dict[str, pd.DataFrame]) -> None:
    assert set(frames) == {
        "raw_campaigns",
        "raw_ad_performance",
        "raw_sessions",
        "raw_transactions",
    }


def test_no_table_is_empty(frames: dict[str, pd.DataFrame]) -> None:
    for name, df in frames.items():
        assert len(df) > 0, name


def test_clicks_never_exceed_impressions(frames: dict[str, pd.DataFrame]) -> None:
    ad = frames["raw_ad_performance"]
    assert (ad["clicks"] <= ad["impressions"]).all()


def test_spend_is_non_negative(frames: dict[str, pd.DataFrame]) -> None:
    assert (frames["raw_ad_performance"]["spend_usd"] >= 0).all()


def test_revenue_is_positive_for_every_transaction(frames: dict[str, pd.DataFrame]) -> None:
    assert (frames["raw_transactions"]["revenue_usd"] > 0).all()


def test_every_transaction_belongs_to_a_session(frames: dict[str, pd.DataFrame]) -> None:
    sessions = set(frames["raw_sessions"]["session_id"])
    orphans = set(frames["raw_transactions"]["session_id"]) - sessions
    assert not orphans


# ------------------------------------------- structure the golden questions rely on


def test_direct_traffic_exists_and_has_no_campaign(frames: dict[str, pd.DataFrame]) -> None:
    """The `(direct)` pseudo-channel is what makes blended vs campaign ROAS ambiguous."""
    sessions = frames["raw_sessions"]
    unattributed = sessions["campaign_name"].isna() | (sessions["campaign_name"] == "(direct)")
    assert unattributed.any(), "no unattributed traffic: the ambiguity test cases are dead"


def test_branded_search_outconverts_nonbrand(frames: dict[str, pd.DataFrame]) -> None:
    """"Exclude branded search" is only a meaningful instruction if brand really differs."""
    s = frames["raw_sessions"]
    rates = s.groupby("channel")["is_converted"].mean()
    assert rates["paid_search_brand"] > 2 * rates["paid_search_nonbrand"]


# ------------------------------------------------------------------------- loading


def test_load_writes_a_queryable_raw_schema(tmp_path, frames: dict[str, pd.DataFrame]) -> None:
    import duckdb

    db = tmp_path / "test.duckdb"
    load(db, frames)
    con = duckdb.connect(str(db), read_only=True)
    (n,) = con.execute("select count(*) from raw.raw_ad_performance").fetchone()
    assert n == len(frames["raw_ad_performance"])
