with source as (select * from {{ source('raw', 'raw_transactions') }})

select
    transaction_id,
    session_id,
    customer_id,
    cast(event_date as date) as event_date,
    cast(revenue_usd as double) as revenue_usd,
    cast(items as integer) as items
from source
