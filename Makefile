.PHONY: install warehouse test lint fmt types check clean eval eval-gate eval-baseline

install:
	pip install -e ".[dev,warehouse]"
	pre-commit install

warehouse:                ## Regenerate raw tables, then build staging + marts
	python -m campaign_copilot.warehouse.generate
	cd warehouse && dbt deps --profiles-dir . && dbt build --profiles-dir .

test:
	pytest --cov --cov-report=term-missing

eval:                     ## Run the ablation grid, write history + EVAL_REPORT.md
	python -m campaign_copilot.evals --report EVAL_REPORT.md

eval-gate:                ## Fail if any metric regressed against the committed baseline
	python -m campaign_copilot.evals --gate --no-history

eval-baseline:            ## Freeze the current ceiling as the new baseline
	python -m campaign_copilot.evals --write-baseline --no-history

lint:
	ruff check src tests

fmt:
	ruff format src tests

types:
	mypy

check: lint types test eval-gate

clean:
	rm -rf warehouse/target warehouse/logs warehouse/*.duckdb .pytest_cache .mypy_cache .ruff_cache
