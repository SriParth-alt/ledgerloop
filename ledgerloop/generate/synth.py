"""Seeded synthetic data generator.

TODO(day-2): emit ledger_invoices.csv, pg_settlements.csv, bank_statement.csv and
truth_links.csv from a seed. `--seed 42` must reproduce byte-identical files, so
use an explicit `random.Random(seed)` instance rather than module-level random.

Ship three fixtures: easy, realistic, adversarial. Tune the cascade only against
`realistic`; hold `adversarial` out so the final numbers are not self-graded.
"""

from __future__ import annotations
