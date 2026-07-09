{{ config(materialized='table') }}

-- Grain: one row per (event_date, campaign_name).
--
-- The join is a FULL OUTER JOIN on purpose. Spend exists on days a campaign served but
-- drove no sessions; '(direct)' sessions exist with no spend at all. An inner join here
-- is the single most common way agency dashboards silently under-report blended ROAS.

with spend as (
    select event_date, campaign_name, channel,
           sum(impressions) as impressions,
           sum(clicks)      as clicks,
           sum(spend_usd)   as spend_usd
    from {{ ref('stg_ad_performance') }}
    group by 1, 2, 3
),

engagement as (
    select
        s.event_date,
        s.campaign_name,
        s.channel,
        count(*)                                          as sessions,
        count(*) filter (where s.is_converted)            as converting_sessions,
        count(distinct t.transaction_id)                  as transactions,
        coalesce(sum(t.revenue_usd), 0.0)                 as revenue_usd,
        count(distinct t.customer_id)                     as purchasing_customers
    from {{ ref('stg_sessions') }} s
    left join {{ ref('stg_transactions') }} t
           on t.session_id = s.session_id
    group by 1, 2, 3
)

select
    coalesce(sp.event_date, e.event_date)       as event_date,
    coalesce(sp.campaign_name, e.campaign_name) as campaign_name,
    coalesce(sp.channel, e.channel)             as channel,
    coalesce(sp.impressions, 0)                 as impressions,
    coalesce(sp.clicks, 0)                      as clicks,
    coalesce(sp.spend_usd, 0.0)                 as spend_usd,
    coalesce(e.sessions, 0)                     as sessions,
    coalesce(e.transactions, 0)                 as transactions,
    coalesce(e.purchasing_customers, 0)         as purchasing_customers,
    coalesce(e.revenue_usd, 0.0)                as revenue_usd
from spend sp
full outer join engagement e
  on sp.event_date = e.event_date
 and sp.campaign_name = e.campaign_name
