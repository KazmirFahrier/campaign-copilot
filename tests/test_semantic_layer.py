"""Semantic-layer tests. The important one is `test_average_of_ratios_is_a_different_number`.

It executes both the correct and the incorrect aggregation against the real warehouse and
asserts they disagree. That is the empirical justification for the entire semantic layer:
if the two numbers agreed, none of this machinery would be worth its weight.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from campaign_copilot.semantic.layer import (
    SemanticError,
    SemanticLayer,
    UnknownMetricError,
)

WAREHOUSE = Path(__file__).resolve().parents[1] / "warehouse" / "campaign_copilot.duckdb"
TABLE = "main_marts.campaign_performance_daily"


@pytest.fixture(scope="module")
def layer() -> SemanticLayer:
    return SemanticLayer.load()


@pytest.fixture(scope="module")
def con() -> duckdb.DuckDBPyConnection:
    if not WAREHOUSE.exists():
        pytest.skip("warehouse not built; run `make warehouse`")
    return duckdb.connect(str(WAREHOUSE), read_only=True)


# ------------------------------------------------------------------- registry


def test_core_metrics_are_registered(layer: SemanticLayer) -> None:
    for name in ("spend", "revenue", "roas", "cac", "cpa", "ctr", "cpc", "cvr", "aov"):
        assert layer.metric(name).expression


def test_unknown_metric_suggests_a_correction(layer: SemanticLayer) -> None:
    with pytest.raises(UnknownMetricError) as err:
        layer.metric("roass")
    assert "roas" in err.value.suggestions


def test_every_ratio_metric_guards_against_divide_by_zero(layer: SemanticLayer) -> None:
    for metric in layer.metrics.values():
        if "/" in metric.expression:
            assert "nullif" in metric.expression.lower(), metric.name


def test_metric_cannot_be_computed_below_its_declared_grain(layer: SemanticLayer) -> None:
    """blended_roas is defined per-date only. Asking for it per-campaign is meaningless."""
    with pytest.raises(SemanticError, match="not defined at grain"):
        layer.build_query(["blended_roas"], ["campaign_name"])


def test_ambiguous_metric_surfaces_a_note_for_the_agent(layer: SemanticLayer) -> None:
    notes = layer.ambiguity_notes(["roas"])
    assert notes and "blended" in notes[0].lower()


def test_unambiguous_metric_surfaces_nothing(layer: SemanticLayer) -> None:
    assert layer.ambiguity_notes(["ctr"]) == []


# -------------------------------------------------------------------- compile


def test_build_query_is_executable(layer: SemanticLayer, con) -> None:
    sql = layer.build_query(["spend", "roas"], ["channel"], order_by="spend")
    rows = con.execute(sql).fetchall()
    channels = {r[0] for r in rows}
    assert "paid_search_brand" in channels
    assert "direct" in channels, "the unattributed pseudo-channel must survive the join"


def test_zero_spend_channel_yields_null_roas_not_infinity(layer: SemanticLayer, con) -> None:
    """`direct` has revenue and no spend. nullif() must turn its ROAS into NULL.

    A NULL is a question the agent has to surface. An `inf` is a number it will confidently
    report to a client.
    """
    sql = layer.build_query(["spend", "roas"], ["channel"], filters=["channel = 'direct'"])
    (_, spend, roas), *_ = con.execute(sql).fetchall()
    assert spend == 0
    assert roas is None


def test_order_by_must_reference_a_selected_column(layer: SemanticLayer) -> None:
    with pytest.raises(SemanticError, match="Cannot order by"):
        layer.build_query(["spend"], ["channel"], order_by="clicks")


# ------------------------------------------------- the reason this layer exists


def test_average_of_ratios_is_a_different_number(layer: SemanticLayer, con) -> None:
    correct_sql = layer.build_query(["roas"], ["channel"], order_by="roas")
    correct = dict(con.execute(correct_sql).fetchall())

    naive_sql = f"""
        select channel, avg(revenue_usd / nullif(spend_usd, 0)) as roas
        from {TABLE}
        where spend_usd > 0
        group by 1
    """
    naive = dict(con.execute(naive_sql).fetchall())

    comparable = [ch for ch in correct if correct[ch] is not None and naive.get(ch) is not None]
    assert comparable, "no channel had spend on both sides of the comparison"

    disagreements = {
        ch: (correct[ch], naive[ch])
        for ch in comparable
        if abs(correct[ch] - naive[ch]) / correct[ch] > 0.01
    }
    assert disagreements, (
        "If ratio-of-sums equalled average-of-ratios on this data, the semantic layer "
        "would be pointless. It does not."
    )


def test_aggregate_atoms_exclude_any_averaged_ratio(layer: SemanticLayer) -> None:
    atoms = layer.aggregate_atoms()
    assert "sum(revenue_usd)" in atoms
    assert "sum(spend_usd)" in atoms
    assert not any(a.startswith("avg(") for a in atoms)
