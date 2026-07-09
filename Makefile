.PHONY: install warehouse test lint fmt types check clean

install:
	pip install -e ".[dev,warehouse]"
	pre-commit install

warehouse:                ## Regenerate raw tables, then build staging + marts
	python -m campaign_copilot.warehouse.generate
	cd warehouse && dbt deps --profiles-dir . && dbt build --profiles-dir .

test:
	pytest --cov --cov-report=term-missing

lint:
	ruff check src tests

fmt:
	ruff format src tests

types:
	mypy

check: lint types test

clean:
	rm -rf warehouse/target warehouse/logs warehouse/*.duckdb .pytest_cache .mypy_cache .ruff_cache
