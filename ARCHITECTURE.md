# Architecture

How LedgerLoop is put together, why it is shaped this way, and which of those choices
survived contact with measurement.

This is the map. [`DECISIONS.md`](DECISIONS.md) is the territory — 34 ADRs, most of them
written *after* a measurement contradicted something we believed. Where the two disagree,
`DECISIONS.md` wins, because it is dated.

---

## The shape of it

```
  ledger_invoices.csv    pg_settlements.csv    bank_statement.csv
          │                      │                     │
          └──────────────┬───────┴─────────────────────┘
                         ▼
              ┌──────────────────────┐
              │  INGEST              │  SHA-256 row fingerprint
              │  · schema validate   │  idempotent upsert
              │  · normalise         │  malformed → quarantine
              └──────────┬───────────┘
                         ▼
              ┌──────────────────────┐
              │  SQLite (append-only)│  invoices · settlements · bank_txns
              │                      │  match_records · exceptions · rules
              └──────────┬───────────┘
                         ▼
        ┌────────────────────────────────────┐
        │       MATCHING CASCADE             │
        │  T0  exact (UTR / amount+date)     │──▶ auto-post
        │  T1  tolerant (fees, windows, fuzz)│──▶ auto-post
        │  T2  subset-sum (N:1 batches)      │──▶ auto-post | AMBIGUOUS
        │  T3  LLM adjudication (residual)   │──▶ propose → validate → post
        │  T4  exception queue (reason-coded)│──▶ human
        └────────────────┬───────────────────┘
                         ▼
     ┌───────────────────┴────────────────────┐
     ▼                                        ▼
┌─────────────┐                    ┌─────────────────────┐
│ AUDIT TRAIL │                    │ EXCEPTION UI        │
│ provenance  │                    │ resolve → propose   │
│ per match   │                    │ rule → persist      │
└─────────────┘                    └──────────┬──────────┘
                                              │ replay
                         ┌────────────────────┘
                         ▼
              ┌──────────────────────┐
              │  EVAL HARNESS        │  vs. ground truth
              │  P / R / false-match │  ablation by tier
              │  cost · throughput   │  LLM invocation rate
              └──────────────────────┘
```

---

## The one-sentence thesis

**An LLM touches under a quarter of records by design, and it never has final say on any
of them.**

Everything below is either a mechanism that makes that sentence true, or a measurement
that tests whether it was worth saying.

---

## Why a cascade, and why the model goes last

Four decisions set the architecture before any code was written. They are reproduced from
§3.2 of the spec because they are the ones a reviewer will push on.

### 1. The LLM adjudicates the residual; it does not drive the match

Considered: LLM-first with rules as fallback; rules-first with the LLM adjudicating the
residual; the LLM as an orchestrator calling rule tools.

**Chose rules-first.** On a 50–500 record batch, most records are resolvable by pure
arithmetic and string normalisation. Routing those through a model costs money, adds
latency, introduces nondeterminism into re-runs, and destroys auditability — for zero
accuracy gain on records that were already solved. The orchestrator option is
architecturally fashionable but puts unpredictable control flow over money movement,
which is the opposite of what a finance system wants.

**This is the decision the measurement vindicated, and not for the reason we expected.**
See "What the numbers actually showed" below.

### 2. Ambiguity raises; it never guesses

Considered: pick the highest-scoring candidate, or refuse.

**Chose refuse.** If a credit of ₹48,220 can be explained by two *different* valid subsets
of settlements, both are arithmetically perfect and there is no principled tiebreak.
Picking one is a coin flip on the books. The system emits `AMBIGUOUS_SUBSET`, attaches both
explanations, and lets a human choose. This costs headline match rate and buys correctness,
and the eval harness shows exactly what the trade cost.

### 3. The agentic surface is the exception-resolution loop

A human resolves an exception; the system inspects the resolution, proposes a generalised
rule in prose and machine-readable form, and — **on explicit approval** — persists it so
the next run resolves that class automatically.

Approval is a separate `--approve` flag rather than implied by resolving, because a
resolution records what a human decided about one record while a rule fires forever on
batches nobody has looked at (ADR-004, ADR-030).

### 4. Synthetic data is a first-class component, not a fixture

Synthetic is not a shortcut. It is the only way to obtain **ground truth**, without which
precision and recall cannot be computed at all. The generator builds a world, freezes the
truth, then renders lossy views of it and degrades them (§5).

`eval/` is the only package permitted to read `truth_links.csv`, and
`tests/test_no_truth_leak.py` fails the build on any import of `eval` from inside
`ledgerloop/`. That test has caught a real violation (ADR-023).

---

## The five tiers

| Tier | Mechanism | Calls a model | Can raise |
|---|---|---|---|
| 0 | Exact reference or unique amount+date, both requiring amount agreement | no | no |
| 1 | Fee-model recomputation, settlement window, fuzzy reference and name | no | no |
| 2 | Bounded subset-sum for batched payouts | no | yes |
| 3 | Constrained adjudication of what remains | **yes** | yes |
| 4 | Exception queue, reason-coded and clustered | no | yes |

**Tiers 0–2 must never call a model.** If a match can be made by arithmetic or string
normalisation, it is made by arithmetic.

Two details that are easy to get wrong and were:

- **Tier 0's reference rule requires amount corroboration.** Without it, batched credits
  matched their lead settlement at confidence 1.0 — a 25% false-match class that every unit
  test passed through (ADR-018).
- **Tier 2 is a decision procedure, not a search.** It answers "zero, exactly one, or two or
  more explanations" and stops at two, because the third does not change the decision. Only
  `AMBIGUOUS_SUBSET` is terminal; `POOL_TOO_LARGE` is not, or Tier 3 would be denied the
  records it exists to handle (ADR-020, ADR-021).

---

## Where the LLM is, and what stops it

Tier 3 receives a credit and a ranked candidate list, and proposes a settlement set. Before
anything is posted it passes **three gates and a threshold**:

1. **Schema.** Malformed output is rejected outright. No retry loop that coaxes out a
   parseable answer — a model that emitted invalid JSON for this input had nothing
   confident to say about it.
2. **Membership.** Any identifier outside the candidate set discards the **whole** response,
   not just the bad id. One fabricated id is evidence the model was pattern-completing
   rather than reading, which devalues the ids that happened to be real.
3. **Arithmetic.** The proposal is re-verified in Python against the fee model and the date
   window. If the numbers disagree with the model, **the numbers win**.

Then a confidence threshold. It is deliberately not called a fourth gate: the gates are
correctness checks, the threshold is a tuning knob.

**The gates reduce false matches. They do not eliminate them.** One proposal in the
LLM-only arm passed all three plus the threshold and was still wrong, because a different
settlement set can reconcile to the same rupee on a nearby date. ADR-034 states this
plainly rather than letting "every proposal is re-verified" be read as "a wrong match is
impossible".

### Reproducibility

Every response is cached, keyed on the prompt **and the model**, and the cache is
committed. A re-run makes zero API calls and produces a byte-identical match set — which
is what lets CI and any reviewer reproduce every Tier 3 figure with **no API key and no
cost**.

That claim was false until day 13. The cache key was read off the live adapter's name, so a
run without a key computed its keys under the empty string and missed every committed
response. `tests/test_demo_is_reproducible.py` now pins the keyless path (ADR-035).

### Vendor independence

`LLMAdapter` is a Protocol with one method. The project moved from Anthropic to Gemini on
day 12 for cost reasons, and the change touched exactly one file — not the tiers, not the
gates, not the cache, not the orchestrator. Both implementations remain in the tree,
because one implementation proves nothing (ADR-024, ADR-031).

---

## What the numbers actually showed

Generated figures live in [`results/metrics.md`](results/metrics.md) and are summarised in
the README. Three findings changed how the architecture should be *described*:

**The cascade beats the LLM-only baseline on both axes at once** — higher auto-match and
higher precision, on the same fixtures with the same model. Both arms run the same three
gates, so the gates are not what separates them. What separates them is how many questions
the model was asked. **The cheapest way to avoid a wrong answer from a model is not to ask
it the question.**

**Tier 3 contributes nothing on `realistic`** and a real gain on `adversarial`. A tier that
correctly declines everything on an easier fixture is worth reporting as plainly as one
that adds a lot — it is also evidence the gates are not rubber-stamping.

**The rule-promotion lift is 0.00%, and the reason is architectural.** Tier 1 is the only
tier that recomputes the fee model; Tiers 0 and 2 reconcile against the net the gateway
reported. So a wrong fee model — the error class the loop repairs — is nearly unobservable
here. That is a robustness property, and the honest reason §8's "wrong fee model surfaces as
an exception cluster" does not hold in this cascade (ADR-029).

---

## The sharp edge

ADR-018, ADR-027, ADR-029, ADR-031 and ADR-033 all record defects that **every passing unit
test missed**. Unit tests over hand-built rows cannot see a tier meeting a chaos injector on
a real fixture, and they cannot see a published number that is wrong in a flattering
direction.

Measure against a fixture before believing a tier works.
