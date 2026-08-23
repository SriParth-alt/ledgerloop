.PHONY: setup test lint fmt demo eval clean

setup:
	python -m pip install --upgrade pip
	pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check ledgerloop eval tests
	mypy ledgerloop/money.py ledgerloop/generate/fee_model.py ledgerloop/llm/contract.py

fmt:
	ruff check --fix ledgerloop eval tests
	ruff format ledgerloop eval tests

# End-to-end: generate a batch, reconcile it, print the report.
# This is the command the pitch video runs.
demo:
	ledgerloop generate --fixture realistic --records 250
	ledgerloop reconcile --run-id demo
	ledgerloop report --run-id demo

# Full ablation across fixtures. Writes results/metrics.md.
# Every number in README.md comes from here and from nowhere else.
eval:
	ledgerloop evaluate --all-fixtures --out results/metrics.md

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache **/__pycache__ *.egg-info
	rm -f ledgerloop.db
