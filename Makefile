.PHONY: install warehouse test lint fmt types check clean eval eval-live eval-gate eval-baseline report serve serve-executor web infra lock audit

install:
	pip install -e ".[dev,warehouse]"
	pre-commit install

warehouse:                ## Regenerate raw tables, then build staging + marts
	sh scripts/build_warehouse.sh

test:
	pytest --cov --cov-report=term-missing

eval:                     ## Run the ablation grid, write history + EVAL_REPORT.md
	python -m campaign_copilot.evals --report EVAL_REPORT.md

eval-live:                ## Bounded real provider evaluation; never runs in CI
	python -m campaign_copilot.evals.live --output evals/live/latest.json

eval-gate:                ## Fail if any metric regressed against the committed baseline
	python -m campaign_copilot.evals --gate --no-history

eval-baseline:            ## Freeze the current ceiling as the new baseline
	python -m campaign_copilot.evals --write-baseline --no-history

report:                   ## Generate the weekly review; fails if any figure cannot be reproduced
	python -m campaign_copilot.reporting --out reports

serve:                    ## Run the api locally (in-process executor: NOT a security boundary)
	uvicorn campaign_copilot.service.app:create_app --factory --reload --port 8080

serve-executor:           ## Run the executor locally
	uvicorn campaign_copilot.service.executor:create_app --factory --port 8081

web:                      ## Typecheck and test the TypeScript client
	cd web && npm ci && npx tsc --noEmit && npm test

infra:                    ## Format-check and validate the Terraform
	cd deploy/terraform && terraform fmt -check && terraform init -backend=false && terraform validate

lock:                     ## Refresh the cross-platform lock and hashed Docker inputs
	uv lock
	uv export --frozen --no-header --no-dev --extra reporting --extra service --no-emit-project --format requirements-txt --output-file requirements-production.lock
	uv export --frozen --no-header --no-dev --extra executor --no-emit-project --format requirements-txt --output-file requirements-executor.lock
	uv export --frozen --no-header --only-group build --no-emit-project --format requirements-txt --output-file requirements-build.lock

audit:                    ## Fail on known vulnerabilities in the production dependency set
	uvx pip-audit --strict --requirement requirements-production.lock
	uvx pip-audit --strict --requirement requirements-executor.lock

lint:
	ruff check src tests

fmt:
	ruff format src tests

types:
	mypy

check: lint types test eval-gate infra

clean:
	rm -rf warehouse/target warehouse/logs warehouse/*.duckdb .pytest_cache .mypy_cache .ruff_cache
