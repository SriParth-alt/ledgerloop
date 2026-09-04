.PHONY: setup test lint fmt demo eval clean

setup:
	python -m pip install --upgrade pip
	pip install -e ".[dev]"

test:
	pytest

# The strict sweep covers the cascade and the LLM surface -- the code where a type
# error would move money. CLAUDE.md has described the gate this way since day 1 while the
# Makefile checked three files; the documentation was ahead of the gate, so the gate moved.
lint:
	ruff check ledgerloop eval tests
	mypy ledgerloop/money.py ledgerloop/generate/fee_model.py ledgerloop/llm/contract.py
	mypy ledgerloop/cascade ledgerloop/llm --strict

fmt:
	ruff check --fix ledgerloop eval tests
	ruff format ledgerloop eval tests

# End-to-end: generate a batch, reconcile it, print the report.
# This is the command the pitch video runs.
#
# `adversarial` rather than `realistic`, deliberately. On realistic the deterministic
# tiers resolve everything Tier 3 could have, so the live output reads "tier 3  0
# credits" -- honest, and it makes the tier the pitch is about look inert. On
# adversarial Tier 3 adds real matches on top of T0-T2 and the gates are visible
# rejecting proposals. It is also the harder fixture, which is the point.
#
# Runs with no API key: Tier 3 is served entirely from the committed response cache
# in fixtures/llm_cache (ADR-035). CI runs this target on every push.
demo:
	# Matches are append-only and a run id is unique (ADR-013), so reconciling into an
	# existing run is refused -- correctly. Without this the target is single-use:
	# rehearse once, then watch it error on camera during take two.
	rm -f ledgerloop.db
	ledgerloop generate --fixture adversarial --records 250
	ledgerloop reconcile --run-id demo --fixture adversarial
	ledgerloop report --run-id demo --fixture adversarial --html results/report.html

# Full ablation across fixtures. Writes results/metrics.md.
# Every number in README.md comes from here and from nowhere else.
eval:
	ledgerloop evaluate --all-fixtures --out results/metrics.md

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache **/__pycache__ *.egg-info
	rm -f ledgerloop.db
