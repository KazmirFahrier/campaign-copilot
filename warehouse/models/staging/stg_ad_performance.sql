with source as (select * from {{ source('raw', 'raw_ad_performance') }})

select
    cast(event_date as date)      as event_date,
    campaign_name,
    channel,
    cast(impressions as bigint)   as impressions,
    cast(clicks as bigint)        as clicks,
    cast(spend_usd as double)     as spend_usd
from source
