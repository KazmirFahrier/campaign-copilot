with source as (select * from {{ source('raw', 'raw_campaigns') }})

select
    campaign_name,
    channel,
    cast(flight_start as date) as flight_start,
    cast(flight_end as date)   as flight_end,
    cast(is_branded as boolean) as is_branded
from source
