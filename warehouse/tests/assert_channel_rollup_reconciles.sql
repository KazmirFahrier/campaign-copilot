-- channel_attribution is a rollup of campaign_performance_daily. If totals drift by more
-- than floating-point noise, a join has fanned out. Fail loudly.
with campaign_total as (
    select round(sum(spend_usd), 2) as spend, round(sum(revenue_usd), 2) as revenue
    from {{ ref('campaign_performance_daily') }}
),
channel_total as (
    select round(sum(spend_usd), 2) as spend, round(sum(revenue_usd), 2) as revenue
    from {{ ref('channel_attribution') }}
)
select 'spend' as metric, c.spend as campaign_value, h.spend as channel_value
from campaign_total c cross join channel_total h
where abs(c.spend - h.spend) > 0.01
union all
select 'revenue', c.revenue, h.revenue
from campaign_total c cross join channel_total h
where abs(c.revenue - h.revenue) > 0.01
