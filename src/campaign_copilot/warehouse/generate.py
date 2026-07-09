"""Deterministic synthetic marketing-analytics dataset.

Why synthetic rather than the BigQuery GA4 public dataset:

* The eval harness (Phase 4) needs *verifiable* ground truth. With a generator we know
  the true ROAS of every campaign, so a golden-SQL answer can be checked against the
  data-generating process rather than against a second hand-written query.
* The repo must clone-and-run with no cloud credentials.

The schema mirrors the shape of ``bigquery-public-data.google_analytics_sample`` closely
enough that swapping in BigQuery is a profile change, not a rewrite.

Structure deliberately baked in, so that analytical questions have non-trivial answers:

* Weekly seasonality (weekend traffic dip) and a Q4 holiday lift.
* Channel-specific conversion rates and CPCs, so CAC genuinely differs by channel.
* One campaign ("holiday_retarget") with creative fatigue: CTR decays over its flight.
* Branded search converts at ~5x non-brand, which is why "exclude branded" is a real
  analyst instruction and a good ambiguity test case.
* ~3% of sessions have a NULL campaign (direct traffic) to force COALESCE discipline.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

SEED = 20260709
START = date(2025, 1, 1)
END = date(2025, 12, 31)


@dataclass(frozen=True)
class Channel:
    """A marketing channel and its true, generative economics."""

    name: str
    cpc: float  # mean cost per click, USD
    ctr: float  # mean click-through rate
    cvr: float  # mean session -> transaction rate
    aov: float  # mean order value, USD
    daily_impressions: int


CHANNELS: tuple[Channel, ...] = (
    Channel(
        "paid_search_brand", cpc=0.85, ctr=0.115, cvr=0.098, aov=94.0, daily_impressions=42_000
    ),
    Channel(
        "paid_search_nonbrand",
        cpc=2.40,
        ctr=0.038,
        cvr=0.021,
        aov=78.0,
        daily_impressions=155_000,
    ),
    Channel("paid_social", cpc=1.10, ctr=0.014, cvr=0.011, aov=68.0, daily_impressions=310_000),
    Channel("display", cpc=0.55, ctr=0.006, cvr=0.004, aov=61.0, daily_impressions=520_000),
    Channel("video", cpc=0.32, ctr=0.009, cvr=0.005, aov=72.0, daily_impressions=480_000),
    Channel("affiliate", cpc=1.75, ctr=0.052, cvr=0.034, aov=88.0, daily_impressions=38_000),
)

# campaign -> (channel, flight_start, flight_end, budget_multiplier, fatigue)
CAMPAIGNS: dict[str, tuple[str, date, date, float, bool]] = {
    "always_on_brand": ("paid_search_brand", START, END, 1.0, False),
    "always_on_nonbrand": ("paid_search_nonbrand", START, END, 1.0, False),
    "spring_prospecting": ("paid_social", date(2025, 3, 1), date(2025, 5, 31), 1.3, False),
    "summer_video_push": ("video", date(2025, 6, 1), date(2025, 8, 31), 1.5, False),
    "holiday_retarget": ("paid_social", date(2025, 10, 15), date(2025, 12, 26), 2.1, True),
    "evergreen_display": ("display", START, END, 0.8, False),
    "partner_affiliate": ("affiliate", START, END, 1.0, False),
}

DEVICES = ("desktop", "mobile", "tablet")
DEVICE_W = (0.41, 0.52, 0.07)
COUNTRIES = ("United States", "Canada", "United Kingdom", "Germany", "Australia")
COUNTRY_W = (0.62, 0.11, 0.13, 0.08, 0.06)


def _seasonality(d: date) -> float:
    """Multiplicative demand index for a given calendar day."""
    weekend = 0.82 if d.weekday() >= 5 else 1.0
    # Smooth annual cycle peaking in late Q4.
    doy = d.timetuple().tm_yday
    annual = 1.0 + 0.18 * np.sin(2 * np.pi * (doy - 80) / 365.0)
    holiday = 1.0
    if date(2025, 11, 24) <= d <= date(2025, 12, 2):  # Black Friday / Cyber Monday
        holiday = 2.35
    elif date(2025, 12, 3) <= d <= date(2025, 12, 20):
        holiday = 1.45
    return float(weekend * annual * holiday)


def _fatigue(d: date, flight_start: date, flight_end: date) -> float:
    """CTR decay across a campaign flight. 1.0 on day one, ~0.55 at the end."""
    span = max((flight_end - flight_start).days, 1)
    progress = (d - flight_start).days / span
    return float(1.0 - 0.45 * progress)


def _daterange(start: date, end: date) -> Iterator[date]:
    for i in range((end - start).days + 1):
        yield start + timedelta(days=i)


def generate(rng: np.random.Generator, scale: float = 0.15) -> dict[str, pd.DataFrame]:
    """Vectorized generation. ``scale`` shrinks impression volume for a fast local build."""
    ch_by_name = {c.name: c for c in CHANNELS}

    ad_rows: list[dict] = []
    blocks: list[dict] = []

    for d in _daterange(START, END):
        season = _seasonality(d)

        for campaign, (ch_name, f_start, f_end, budget_mult, has_fatigue) in CAMPAIGNS.items():
            if not (f_start <= d <= f_end):
                continue
            ch = ch_by_name[ch_name]
            fatigue = _fatigue(d, f_start, f_end) if has_fatigue else 1.0

            impressions = int(rng.poisson(ch.daily_impressions * budget_mult * season * scale))
            ctr = float(np.clip(rng.normal(ch.ctr * fatigue, ch.ctr * 0.12), 1e-5, 1.0))
            clicks = int(rng.binomial(impressions, ctr))

            cpc = float(max(rng.normal(ch.cpc, ch.cpc * 0.09), 0.05))
            cpc *= 1.0 + 0.22 * (season - 1.0)  # auction pressure tracks demand
            spend = round(clicks * cpc, 2)

            ad_rows.append(
                {
                    "event_date": d,
                    "campaign_name": campaign,
                    "channel": ch_name,
                    "impressions": impressions,
                    "clicks": clicks,
                    "spend_usd": spend,
                }
            )

            n = int(rng.binomial(clicks, 0.94))  # ~6% bounce before load
            if n == 0:
                continue

            cvr = float(np.clip(rng.normal(ch.cvr, ch.cvr * 0.15), 0.0, 1.0))
            attributed = rng.random(n) > 0.03  # 3% direct / dark traffic
            blocks.append(
                {
                    "n": n,
                    "event_date": np.full(n, d, dtype=object),
                    "campaign_name": np.where(attributed, campaign, None),
                    "channel": np.where(attributed, ch_name, "direct"),
                    "device_category": rng.choice(DEVICES, size=n, p=DEVICE_W),
                    "country": rng.choice(COUNTRIES, size=n, p=COUNTRY_W),
                    "pageviews": rng.integers(1, 14, size=n),
                    "session_duration_sec": np.abs(rng.normal(180, 140, size=n)).astype(int),
                    "is_converted": rng.random(n) < cvr,
                    "aov": np.full(n, ch.aov),
                    "season": np.full(n, season),
                }
            )

    cat = lambda k: np.concatenate([b[k] for b in blocks])  # noqa: E731
    total = sum(b["n"] for b in blocks)
    sess_idx = np.arange(1, total + 1)

    sessions = pd.DataFrame(
        {
            "session_id": [f"s_{i:08d}" for i in sess_idx],
            "event_date": cat("event_date"),
            "campaign_name": cat("campaign_name"),
            "channel": cat("channel"),
            "device_category": cat("device_category"),
            "country": cat("country"),
            "pageviews": cat("pageviews"),
            "session_duration_sec": cat("session_duration_sec"),
            "is_converted": cat("is_converted"),
        }
    )

    conv = sessions["is_converted"].to_numpy()
    n_txn = int(conv.sum())
    aov = cat("aov")[conv]
    season = cat("season")[conv]

    # Repeat purchase via a power-law customer universe: a small head of loyal customers
    # accounts for a disproportionate share of orders, which is what makes LTV interesting.
    # Tuned to ~30% repeat rate / ~1.4 orders per customer, the real ecommerce band.
    n_customers = max(int(n_txn * 1.6), 1)
    weights = 1.0 / np.arange(1, n_customers + 1) ** 0.35
    weights /= weights.sum()
    cust = rng.choice(n_customers, size=n_txn, p=weights)

    revenue = np.maximum(rng.normal(aov, aov * 0.28), 5.0) * (1.0 + 0.12 * (season - 1.0))

    transactions = pd.DataFrame(
        {
            "transaction_id": [f"t_{i:08d}" for i in range(1, n_txn + 1)],
            "session_id": sessions.loc[conv, "session_id"].to_numpy(),
            "customer_id": [f"c_{i:07d}" for i in cust],
            "event_date": sessions.loc[conv, "event_date"].to_numpy(),
            "revenue_usd": np.round(revenue, 2),
            "items": rng.integers(1, 6, size=n_txn),
        }
    )

    campaigns = pd.DataFrame(
        [
            {
                "campaign_name": name,
                "channel": ch,
                "flight_start": fs,
                "flight_end": fe,
                "is_branded": ch == "paid_search_brand",
            }
            for name, (ch, fs, fe, _, _) in CAMPAIGNS.items()
        ]
    )

    return {
        "raw_ad_performance": pd.DataFrame(ad_rows),
        "raw_sessions": sessions,
        "raw_transactions": transactions,
        "raw_campaigns": campaigns,
    }


def load(db_path: Path, frames: dict[str, pd.DataFrame]) -> None:
    """Write the generated raw frames into the ``raw`` schema of a DuckDB file."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        for name, df in frames.items():
            con.register("_tmp", df)
            con.execute(f"CREATE OR REPLACE TABLE raw.{name} AS SELECT * FROM _tmp")
            con.unregister("_tmp")
        con.execute("CHECKPOINT")
    finally:
        con.close()


def main() -> None:
    """Regenerate the raw tables from ``SEED``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("warehouse/campaign_copilot.duckdb"))
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--scale", type=float, default=0.15)
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed)
    frames = generate(rng, scale=args.scale)
    load(args.db, frames)

    for name, df in frames.items():
        print(f"{name:24s} {len(df):>9,} rows")
    print(f"\nWrote {args.db}")


if __name__ == "__main__":
    main()
