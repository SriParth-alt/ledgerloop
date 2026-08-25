# LedgerLoop

**Deterministic-first settlement reconciliation with an honest exception queue.**

Razorpay AI Buildathon 2026 — Track 04, AI Finance Controller.

---

## The problem

A merchant ends every month with three documents describing the same money, none of which
agree: the internal invoice ledger, the gateway settlement report, and the bank statement.
A finance associate matches them by hand.

They disagree for structural reasons, not sloppy ones:

- **Amounts never match.** The bank credit is `gross − MDR − GST on MDR − TDS`.
- **Cardinality is not 1:1.** One bank credit covers a batch of N payments, and nothing in
  the statement says which N.
- **References are destroyed in transit.** The UTR arrives buried in free-text narration —
  prefixed, truncated, or absent.
- **Timing is skewed.** T+1 to T+3, skipping weekends and bank holidays.

## The thesis

> On money, **arithmetic decides and the model only proposes evidence.**

An LLM touches under a quarter of records by design, and it never has final say on any of
them. Tiers 0–2 are pure deterministic matching. Tier 3 calls a model only on the residual,
and every proposal passes three gates — schema, membership, arithmetic — before a rupee
moves.

This matters because **a false match is worse than an exception.** An unmatched row stays
visible in a queue. A falsely matched row silently closes out, corrupts the books, and
surfaces months later at audit with its provenance gone.

## Architecture

```
  ledger_invoices.csv    pg_settlements.csv    bank_statement.csv
          └──────────────────────┴─────────────────────┘
                                 ▼
                      INGEST  (SHA-256 fingerprint, idempotent, quarantine)
                                 ▼
                      SQLite  (append-only)
                                 ▼
        ┌────────────────────────────────────────────┐
        │  T0  exact          (UTR / amount+date)    │──▶ auto-post
        │  T1  tolerant       (fees, windows, fuzzy) │──▶ auto-post
        │  T2  subset-sum     (N:1 batches)          │──▶ auto-post | AMBIGUOUS
        │  T3  LLM adjudication, three gates         │──▶ propose → verify → post
        │  T4  exception queue, reason-coded         │──▶ human
        └────────────────────────────────────────────┘
                     ▼                        ▼
              AUDIT TRAIL              EXCEPTION UI → rule promotion
```

Full design rationale: [`PROJECT_SPEC.md`](PROJECT_SPEC.md).

Decision log: [`DECISIONS.md`](DECISIONS.md) — architecture decision records, one per choice, each
with the alternatives rejected and what the choice costs.

## Results

> **PLACEHOLDER — do not fill by hand.** Every number below is produced by `make eval`
> against a seeded fixture committed to this repo. If a cell is empty, the measurement has
> not been run yet. Delete this blockquote once real numbers land.

| Configuration | Auto-match | Precision | False-match | LLM calls |
|---|---|---|---|---|
| T0 only | — | — | — | 0 |
| T0 + T1 | — | — | — | 0 |
| T0 + T1 + T2 | — | — | — | 0 |
| Full cascade | — | — | — | — |
| **LLM-only baseline** | — | — | — | all |

The LLM-only row is the control arm. Without it, the cascade is an opinion rather than a
result.

## Run it

```bash
make setup     # install
make test      # 153 tests, no API key needed
make demo      # generate → reconcile → report
make eval      # full ablation, writes results/metrics.md
```

## Design rules

These are load-bearing, not style preferences. They are enforced by tests where possible.

1. Tiers 0–2 never call a model.
2. The LLM never overrides arithmetic — every proposal is recomputed in Python.
3. The LLM may only return IDs from the candidate set it was given; anything else discards
   the whole response and increments a hallucination counter.
4. Ambiguity is never resolved by guessing. Two valid subset explanations → exception.
5. All money is integer paise. No floats in any money path.
6. `eval/` is the only package that may read ground truth — enforced by
   `tests/test_no_truth_leak.py`.
7. No metric is ever hand-written into this file.

## Status

In progress. 153 tests pass.

**Implemented and tested:** money arithmetic (integer paise), the MDR/GST/TDS fee model and
settlement-date math, the Tier 3 LLM output contract, exception reason codes, the SQL schema,
and the synthetic data generator — all twelve chaos injectors from `PROJECT_SPEC.md` §5.5,
seeded and byte-identical reproducible, with ground truth. `ledgerloop generate` works.

**Stubbed, with implementation notes:** the five cascade tiers, ingest and fingerprinting,
the store, the exception queue, rule promotion, the API, and the eval harness. The remaining
CLI commands exit non-zero rather than pretending to run.

No metric has been measured yet. See `PROJECT_SPEC.md` §12 for the day-by-day plan and
[`DECISIONS.md`](DECISIONS.md) for why the architecture is shaped this way.

## Lineage

Ingestion patterns — row fingerprinting, idempotent loading, and quarantine handling — build
on approaches worked out in an earlier personal project (StatementSync). This is a new
implementation in a new repository, not copied code. Declared for transparency.
