#!/bin/sh
set -eu

python -m campaign_copilot.warehouse.generate
cd warehouse
dbt deps --profiles-dir .
dbt build --profiles-dir .
