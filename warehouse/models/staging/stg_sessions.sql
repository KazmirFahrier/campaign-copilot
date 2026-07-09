with source as (select * from {{ source('raw', 'raw_sessions') }})

select
    session_id,
    cast(event_date as date) as event_date,
    -- Direct traffic has no campaign. Label it explicitly rather than dropping it,
    -- so that blended (all-traffic) metrics stay reconcilable with channel metrics.
    coalesce(campaign_name, '(direct)') as campaign_name,
    channel,
    device_category,
    country,
    cast(pageviews as integer) as pageviews,
    cast(session_duration_sec as integer) as session_duration_sec,
    cast(is_converted as boolean) as is_converted
from source
