{{ config(materialized='table') }}

-- Grain: one row per (event_date, channel). Rolls up campaign_performance_daily, so the
-- two marts are guaranteed to reconcile. Never re-derive from staging.

select
    event_date,
    channel,
    sum(impressions)          as impressions,
    sum(clicks)               as clicks,
    sum(spend_usd)            as spend_usd,
    sum(sessions)             as sessions,
    sum(transactions)         as transactions,
    sum(purchasing_customers) as purchasing_customers,
    sum(revenue_usd)          as revenue_usd
from {{ ref('campaign_performance_daily') }}
group by 1, 2
