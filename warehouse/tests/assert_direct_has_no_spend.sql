-- '(direct)' is a pseudo-campaign. If it ever acquires spend, attribution has broken
-- and blended_roas becomes silently wrong.
select campaign_name, sum(spend_usd) as spend
from {{ ref('campaign_performance_daily') }}
where campaign_name = '(direct)'
group by 1
having sum(spend_usd) > 0
