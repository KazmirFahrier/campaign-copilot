{{ config(materialized='table') }}

-- Grain: one row per customer.
--
-- acquisition_channel is FIRST-TOUCH on the customer's first order. This is a modelling
-- choice, not a fact: last-touch and linear attribution give different answers. The
-- semantic layer records the choice so the agent cites it instead of assuming it.

with orders as (
    select
        t.customer_id,
        t.transaction_id,
        t.event_date,
        t.revenue_usd,
        s.channel,
        s.campaign_name,
        row_number() over (
            partition by t.customer_id
            order by t.event_date, t.transaction_id
        ) as order_seq
    from {{ ref('stg_transactions') }} t
    join {{ ref('stg_sessions') }} s on s.session_id = t.session_id
),

first_touch as (
    select customer_id, channel as acquisition_channel,
           campaign_name as acquisition_campaign, event_date as first_order_date
    from orders where order_seq = 1
)

select
    o.customer_id,
    f.acquisition_channel,
    f.acquisition_campaign,
    f.first_order_date,
    max(o.event_date)                       as last_order_date,
    count(*)                                as orders,
    round(sum(o.revenue_usd), 2)            as lifetime_revenue_usd,
    round(avg(o.revenue_usd), 2)            as avg_order_value_usd,
    count(*) > 1                            as is_repeat_customer,
    date_diff('day', f.first_order_date, max(o.event_date)) as customer_age_days
from orders o
join first_touch f using (customer_id)
group by 1, 2, 3, 4
