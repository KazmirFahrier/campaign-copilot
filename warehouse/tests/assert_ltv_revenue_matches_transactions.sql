-- Every dollar of transaction revenue must land in exactly one customer's LTV.
-- Catches dropped rows from the orders -> first_touch join.
with ltv as (select round(sum(lifetime_revenue_usd), 0) as total from {{ ref('customer_ltv') }}),
txn as (select round(sum(revenue_usd), 0) as total from {{ ref('stg_transactions') }})
select ltv.total as ltv_total, txn.total as txn_total
from ltv cross join txn
where abs(ltv.total - txn.total) > 1
