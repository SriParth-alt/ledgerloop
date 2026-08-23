# LedgerLoop — Project Specification

**Working title:** LedgerLoop *(rename freely — see §14 for alternatives)*
**Tagline:** Deterministic-first settlement reconciliation with an honest exception queue.

| Field | Value |
|---|---|
| Target | Razorpay AI Buildathon 2026 — **Track 04: AI Finance Controller** |
| Applications close | **5 September 2026** |
| Build window | 22 Aug – 4 Sep 2026 (14 days, 1 buffer day) |
| Author | Parth Srivastava |
| Deliverables | Public GitHub repo · 5-minute pitch video · architecture write-up |
| Status | Spec v1.0 — nothing built yet. **All numbers in §9 are targets, not results.** |

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Why This Problem, Why Now](#2-why-this-problem-why-now)
3. [How We Arrived At This Solution](#3-how-we-arrived-at-this-solution)
4. [The Solution — What We Are Building](#4-the-solution--what-we-are-building)
5. [Synthetic Data Design](#5-synthetic-data-design)
6. [The Matching Cascade](#6-the-matching-cascade)
7. [Where the LLM Is Used (and Where It Is Not)](#7-where-the-llm-is-used-and-where-it-is-not)
8. [Failure Modes and Recovery](#8-failure-modes-and-recovery)
9. [Evaluation Methodology](#9-evaluation-methodology)
10. [Tech Stack and Justification](#10-tech-stack-and-justification)
11. [Repository Structure](#11-repository-structure)
12. [14-Day Build Plan](#12-14-day-build-plan)
13. [Pitch Video Script Outline](#13-pitch-video-script-outline)
14. [Scope Guards, Risks, Open Questions](#14-scope-guards-risks-open-questions)

---

## 1. Problem Statement

### 1.1 The operational reality

A mid-sized Indian merchant running on a payment gateway ends every month with three documents that are supposed to describe the same money, and never do:

1. **The internal ledger** — invoices raised, what the business *thinks* it is owed.
2. **The gateway settlement report** — what the PSP says it captured, netted, and paid out.
3. **The bank statement** — what actually landed in the current account.

A finance associate opens all three in Excel and matches them by hand. On a few hundred transactions a month this takes one to three working days. The reason it is manual is not that the task is intellectually hard. It is that the identifiers do not line up:

- **Amounts never match.** The bank credit is `gross − MDR − GST on MDR − TDS`, and sometimes minus a partial refund that settled in the same cycle.
- **Cardinality is not 1:1.** One bank credit typically covers a *batch* of N payments settled in the same cycle. There is no field in the bank statement telling you which N.
- **References are destroyed in transit.** The UTR exists, but it arrives buried in a free-text `narration` column, prefixed, truncated, case-mangled, or absent entirely.
- **Timing is skewed.** Settlement is T+1 to T+3, skipping weekends and bank holidays, so date equality is useless as a join key.
- **The data lies occasionally.** Banks re-post credits. Refunds appear out of order. Someone wires money in out-of-band with no matching invoice.

### 1.2 Why naive automation fails

Two failure archetypes dominate:

**Archetype A — the rules engine that stalls.** A hand-written VLOOKUP or Python script matches the easy 60% and dumps the rest into a spreadsheet the associate still works by hand. Value delivered is real but capped.

**Archetype B — the LLM that guesses.** Throw the three files at a model, ask "reconcile these." It returns confident, well-formatted matches. Some are wrong. In reconciliation, a *wrong match is worse than no match*: an unmatched row stays visible in an exception queue, while a falsely matched row silently closes out and corrupts the books. It surfaces months later during audit, and by then the provenance is gone.

### 1.3 The statement

> Build a finance-ops agent that reconciles a three-source settlement batch end to end, resolves the maximum number of records **without a human**, and — critically — refuses to guess on the rest, handing back a structured, reason-coded exception list with a full audit trail for every decision it did make.

The track brief asks for match rate **and** the exceptions the agent could not resolve. This spec treats the second half as the harder and more valuable engineering problem.

---

## 2. Why This Problem, Why Now

The track framing is that verification capacity, not generation speed, is the 2026 bottleneck — reconciliation, settlement and forecasting are still done by hand.

That framing points at a specific architectural claim, which this project is built to demonstrate:

> **LLMs did not become useful for reconciliation because they got better at matching. They became useful because they got good enough at *reading unstructured evidence* to close the last mile that rules cannot reach — provided something else is holding the line on correctness.**

A bank narration like `NEFT-CR/HDFC/RZRPY0034821/ACME RETAIL PVT/BLR` is a natural-language parsing problem. A regex handles the ones you anticipated. The model handles the ones you did not. But the model must never be the thing that *decides* whether money is matched — only the thing that *proposes evidence* for a decision that a deterministic validator then accepts or rejects.

That is the thesis. Everything in §6 and §7 is the implementation of it.

---

## 3. How We Arrived At This Solution

This section exists because the panel evaluates **problem taste** and **AI judgment** — the reasoning is part of the submission, not scaffolding for it.

### 3.1 Track selection

Five tracks were available. Assessment:

| Track | Assessment | Verdict |
|---|---|---|
| 01 — AI Growth & Agentic Commerce | Highest hype, therefore highest submission volume. Requires depth in Razorpay test-mode APIs plus emerging protocols (UAP, ACP, AP2, x402) that would consume most of a 14-day window just to understand. | Reject — cold start too expensive |
| 02 — AI Risk Manager | Genuine fit with prior ML work. But "fraud detection with precision/recall" is the single most common student portfolio project in existence; differentiation is very hard. | Reject — crowded, hard to stand out |
| 03 — AI Revenue Recovery | Broadest and most demo-friendly, so likely the most-submitted track. Bar requires *measured money recovered* across a batch, which needs a simulation environment built from scratch anyway. | Reject — crowded, and the eval scaffolding is as much work as the product |
| **04 — AI Finance Controller** | Least glamorous framing, therefore likely lowest submission volume. Bar is explicitly about measured accuracy and an honest exception list — which rewards engineering discipline over demo polish. **Directly adjacent to prior work (StatementSync: fingerprinting, idempotent loading, quarantine handling, reconciliation).** | **Selected** |
| 05 — Open Track | No structural advantage, and an unconstrained brief is harder to score well against. | Reject |

The deciding factor is not preference, it is **capital already accumulated**. StatementSync established the ingestion, fingerprinting, idempotency, and quarantine patterns. Track 04 is the only track where those patterns are load-bearing rather than incidental, meaning ~2 days of the 14 are effectively pre-paid.

> **Note on reuse:** LedgerLoop is a **new build in a new repository.** StatementSync contributed design patterns and hard-won lessons, not copied code. State this explicitly in the README and in the pitch — reviewers respect declared lineage and penalise undeclared reuse.

### 3.2 Architecture decision log

**Decision 1 — Should the LLM drive the match, or adjudicate the residual?**

Considered: (a) LLM-first with rules as fallback; (b) rules-first with LLM adjudicating the residual; (c) LLM as an orchestrator calling rule tools.

Chose **(b)**. Reasoning: on a 50–500 record batch, roughly 60–80% of records are resolvable by pure arithmetic and string normalisation. Routing those through a model costs money, adds latency, introduces nondeterminism into re-runs, and destroys auditability — for zero accuracy gain on records that were already solved. Option (c) is architecturally fashionable but adds an unpredictable control flow over money movement, which is the opposite of what a finance system wants.

*This decision is the single most defensible sentence in the pitch:* **"An LLM touches under a quarter of records by design, and it never has final say on any of them."**

**Decision 2 — What happens when the cascade is ambiguous?**

Considered: (a) pick the highest-scoring candidate; (b) refuse and raise an exception.

Chose **(b)**. If a bank credit of ₹48,220 can be explained by two *different* valid subsets of settlements, both explanations are arithmetically perfect and there is no principled tiebreak. Picking one is a coin flip on the books. The system emits `AMBIGUOUS_SUBSET`, attaches both candidate explanations, and lets a human choose in two clicks. This costs headline match rate and buys correctness — and the eval harness will show precisely what that trade cost, in numbers.

**Decision 3 — Where does the "agent" part live?**

The track wants an agent, not a batch script. The agentic surface is deliberately narrow: the **exception resolution loop**. A human resolves an exception; the system inspects the resolution, proposes a *generalised rule* in natural language plus machine-readable form, and — on approval — persists it so the next run resolves that class automatically. The agent learns the merchant's idiosyncrasies over runs instead of being reconfigured by an engineer. Measurable lift, live in the demo.

**Decision 4 — Real Razorpay APIs or synthetic data?**

The track explicitly specifies a 50+ record batch of synthetic data. Synthetic is not a shortcut here — it is a requirement, and it is also the only way to obtain **ground truth**, without which precision and recall cannot be computed at all. The generator is therefore a first-class component, not a fixture (§5).

---

## 4. The Solution — What We Are Building

### 4.1 One-paragraph description

LedgerLoop ingests three misaligned financial files, fingerprints and idempotently loads them into an append-only SQLite ledger, then runs a five-tier matching cascade: exact deterministic → fee-and-date-tolerant deterministic → subset-sum for batched payouts → constrained LLM adjudication of the residual → reason-coded exception queue. Every posted match carries a provenance record naming the tier, the rule, the evidence fields, and the SHA-256 fingerprints of the source rows. A web UI presents the exception queue; resolving an exception can be promoted into a persistent rule. A built-in evaluation harness scores every run against generator ground truth and reports auto-match rate, precision, recall, and **false-match rate**, with an ablation table isolating each tier's contribution.

### 4.2 System diagram

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

### 4.3 Core user flow (this is the demo)

1. `ledgerloop generate --records 250 --chaos high` → three CSVs + hidden ground truth
2. `ledgerloop reconcile --run-id demo` → cascade executes, prints live tier-by-tier counts
3. `ledgerloop report --run-id demo` → metrics table + exception summary
4. Open UI → exception queue, sorted by ₹ value at risk
5. Resolve one `NO_CANDIDATE` exception → system proposes a rule → approve
6. `ledgerloop reconcile --run-id demo2` → auto-match rate rises; show the delta
7. Open any matched row → full provenance: tier, rule, evidence, source hashes

---

## 5. Synthetic Data Design

The generator is the foundation. If it is too clean, the results are meaningless; if it is unrealistic, the panel discounts everything downstream.

### 5.1 Source A — `ledger_invoices.csv` (internal system of record)

```
invoice_id, merchant_id, customer_id, customer_name,
invoice_amount_paise, currency, issue_date, due_date, status
```

### 5.2 Source B — `pg_settlements.csv` (gateway payout report)

```
settlement_id, payment_id, order_id, invoice_ref, customer_name,
gross_amount_paise, fee_paise, gst_on_fee_paise, tds_paise,
net_amount_paise, captured_at, settled_on, utr, status
```
`status ∈ {captured, refunded, partial_refund, disputed}`

### 5.3 Source C — `bank_statement.csv` (bank credits)

```
bank_txn_id, value_date, narration, credit_paise, debit_paise, balance_paise
```
Free-text `narration` only. **No structured reference field** — this is the point.

### 5.4 Ground truth — `truth_links.csv` (never seen by the matcher)

```
bank_txn_id, settlement_id, invoice_id, link_type, chaos_tags
```
Consumed exclusively by the eval harness. Enforce this with a directory boundary and a test that fails if any module outside `eval/` imports it.

### 5.5 Chaos injectors

Each is an independently toggleable flag, so the ablation table can attribute failures to specific real-world phenomena.

| Flag | Effect | Which tier should catch it |
|---|---|---|
| `BATCH` | N settlements collapse into 1 bank credit | T2 |
| `FEES` | `net = gross − fee − gst − tds`, tiered MDR by method | T1 |
| `LAG` | T+1/T+2/T+3, skipping weekends and a holiday calendar | T1 |
| `NARRATION_NOISE` | UTR prefixed, truncated, case-varied, delimiter-varied | T1 (regex) → T3 (residual) |
| `NO_UTR` | UTR absent entirely; only amount + name in narration | T3 |
| `PARTIAL_REFUND` | refund nets against the same settlement cycle | T1/T2 |
| `DUPLICATE_POST` | bank re-posts an identical credit | Idempotency layer |
| `ORPHAN_CREDIT` | credit with no corresponding payment (out-of-band transfer) | T4 → `ORPHAN_CREDIT` |
| `OUT_OF_ORDER` | refund row precedes its payment row in file order | Ingest ordering |
| `PAISE_DRIFT` | ±1–3 paise rounding drift | T1 tolerance |
| `NAME_VARIANT` | `ACME RETAIL PVT LTD` vs `Acme Retail Private Limited` | T1 fuzzy → T3 |
| `DECOY_SUBSET` | a second valid subset sums to the same credit | T2 → `AMBIGUOUS_SUBSET` |

`DECOY_SUBSET` is deliberately adversarial. It exists so the demo can show the system *declining* to match where a naive implementation would confidently pick one and be wrong 50% of the time.

### 5.6 Determinism

Seeded RNG. `--seed 42` reproduces a byte-identical dataset. Ship three canonical fixtures in the repo (`easy`, `realistic`, `adversarial`) so a reviewer can reproduce every published number with one command.

---

## 6. The Matching Cascade

### Tier 0 — Exact deterministic

- Normalise UTR: uppercase, strip non-alphanumerics, drop known bank prefixes (`NEFT`, `IMPS`, `RTGS`, `CR`, `UPI`).
- Extract candidate UTR tokens from `narration` via a length-and-charset regex.
- Match on exact normalised UTR, or exact `(net_amount_paise, value_date)` where that pair is unique on both sides.
- Confidence `1.0`. Auto-post.
- **Uniqueness guard:** if a key maps to more than one candidate on either side, it does not match here — it falls through.

### Tier 1 — Tolerant deterministic

- **Amount:** compute `expected_net` from the fee model; accept within `max(₹1, 0.5%)`.
- **Date:** accept within `[settled_on, settled_on + 3 business days]` against a holiday calendar.
- **Fuzzy UTR:** Levenshtein ≤ 2 on the normalised token (catches truncation and OCR-style drift).
- **Name:** `rapidfuzz` token-set ratio ≥ 88 between customer name and narration.
- Composite confidence from a weighted rule score; **≥ 0.90 auto-posts, below that falls through.**

### Tier 2 — Subset-sum for batched payouts

The technically interesting tier.

For each unmatched bank credit `C`:
1. Build a candidate pool: unmatched settlements where `settled_on` is within the date window and `merchant_id` matches. Cap the pool at **N ≤ 25** by nearest-date pruning.
2. Find every subset `S` with `|Σ net(S) − C| ≤ tolerance`.
   - Bounded DP over paise for small pools; **meet-in-the-middle** (`O(2^(N/2))`) for the general case.
   - Memoise on `(pool_hash, target, tolerance)`.
3. **Exactly one** solution → auto-post, confidence `0.95`, evidence lists every member.
4. **Two or more** solutions → do **not** match. Emit `AMBIGUOUS_SUBSET` carrying all candidate explanations for human choice.
5. Zero solutions → fall through to T3.

Complexity guard: if the pool exceeds 25 after pruning, skip subset-sum and emit `POOL_TOO_LARGE` rather than running an exponential search on a live batch. Bounded compute is itself an engineering signal.

### Tier 3 — LLM adjudication

Covered in full in §7.

### Tier 4 — Exception queue

Every unresolved record lands here with a machine-readable reason code, the evidence gathered so far, the ₹ value at risk, and a suggested next action.

| Reason code | Meaning |
|---|---|
| `NO_CANDIDATE` | Nothing in the window plausibly explains this row |
| `AMBIGUOUS_SUBSET` | ≥2 valid subset explanations exist |
| `AMOUNT_BEYOND_TOLERANCE` | Best candidate is outside the fee-model tolerance |
| `DATE_OUT_OF_WINDOW` | Amount matches but settlement timing is implausible |
| `LOW_CONFIDENCE` | LLM proposed a match below the acceptance threshold |
| `ORPHAN_CREDIT` | Bank credit with no gateway counterpart |
| `DUPLICATE_SUSPECTED` | Fingerprint collision with an already-posted row |
| `LLM_INVALID_OUTPUT` | Model returned malformed or hallucinated identifiers |
| `POOL_TOO_LARGE` | Subset search declined on complexity grounds |

The exception queue is **sorted by rupee value at risk**, not by row order. A finance associate with twenty minutes should spend them on the ₹4L exception, not the ₹340 one. Small detail, strong product-sense signal.

---

## 7. Where the LLM Is Used (and Where It Is Not)

### 7.1 The contract

The model is invoked **only** on residual records that survived T0–T2, and only ever as a *proposer of evidence*. It is given:

- one unmatched bank transaction (raw narration included),
- **at most 8 candidate settlements**, pre-scored and pre-filtered by heuristic,
- the fee model and the date window as explicit context,
- a strict output schema.

It must return exactly one of: a match proposal drawn **only from the supplied candidate IDs**, or `NO_MATCH`.

### 7.2 Output schema (Pydantic v2, enforced)

```python
class Evidence(BaseModel):
    field: str            # e.g. "narration_token" | "amount" | "customer_name"
    bank_value: str
    settlement_value: str
    reasoning: str        # ≤ 200 chars, why these correspond

class Adjudication(BaseModel):
    decision: Literal["MATCH", "NO_MATCH"]
    matched_settlement_ids: list[str] = []
    evidence: list[Evidence] = []
    confidence: float = Field(ge=0.0, le=1.0)
    unresolved_reason: str | None = None
```

### 7.3 The three hard gates

Every LLM response passes through all three before any money is posted:

1. **Schema gate.** Fails validation → `LLM_INVALID_OUTPUT` exception. Never a retry loop that eventually coaxes out a guess.
2. **Membership gate.** Every returned ID is checked against the exact candidate set supplied. Any ID not in that set means the model hallucinated → discard the entire response, raise `LLM_INVALID_OUTPUT`. *This gate is demoed live by injecting a deliberately malformed response.*
3. **Arithmetic gate.** The proposed match is re-verified against the fee model and date window in Python. If the model proposes a pairing whose numbers do not actually work, the numbers win. **The model never overrides arithmetic.**

Only after all three, and `confidence ≥ 0.85`, is the match posted — tagged `tier=3` and flagged in the UI as model-assisted, permanently distinguishable from deterministic matches in the audit trail.

### 7.4 Determinism and cost

- `temperature = 0`.
- Responses cached on `SHA-256(prompt_payload)`. A re-run of the same batch performs **zero** new API calls and produces a byte-identical match set — reconciliation must be reproducible for audit.
- Prompt version string stored in every tier-3 provenance record, so a prompt change is visible in the trail.
- Token count and cost logged per run and reported per 100 records.

### 7.5 What the LLM is explicitly *not* allowed to do

- Not allowed to compute or verify amounts.
- Not allowed to propose IDs outside the candidate set.
- Not allowed to post a match on its own confidence alone.
- Not allowed to resolve `AMBIGUOUS_SUBSET` — ambiguity is a human decision by policy, not a capability gap.
- Not used at all in T0–T2, which by design carry the majority of volume.

---

## 8. Failure Modes and Recovery

Explicitly one of the four judging criteria, so it gets its own first-class section in both the repo and the pitch.

| Failure | Detection | Recovery |
|---|---|---|
| LLM API down / rate-limited | Timeout + status | Exponential backoff (3 attempts), then **whole batch completes without T3** — degraded auto-match rate, zero incorrect matches. Run is marked `degraded=true`. |
| LLM returns malformed JSON | Pydantic validation | `LLM_INVALID_OUTPUT` exception. No retry-until-parseable. |
| LLM hallucinates a settlement ID | Membership gate | Discard whole response, exception, **increment a hallucination counter reported in run metrics.** |
| Malformed input row | Schema validation at ingest | Row quarantined with its raw content and error; batch continues. |
| Duplicate file submitted | SHA-256 row fingerprint | Idempotent upsert; re-running the same file is a no-op. |
| Subset-sum blowup | Pool size guard | `POOL_TOO_LARGE`; bounded compute preserved. |
| Two valid subset explanations | Solution count > 1 | `AMBIGUOUS_SUBSET` with both explanations attached. |
| Process crashes mid-run | Append-only writes + run journal | Resume from the last committed tier; no partial-match corruption. |
| Fee model wrong for a merchant | Systematic T1 misses | Shows up as an exception *cluster* — the UI surfaces clustering, and rule promotion fixes the whole class at once. |

The last row is the strongest product argument in the deck: **the exception queue is not just a to-do list, it is a diagnostic instrument.** Twelve exceptions sharing one reason code and one merchant is not twelve problems, it is one wrong assumption.

---

## 9. Evaluation Methodology

> Every figure below is a **target to be measured**, not a claim. Publish only what the harness actually produces.

### 9.1 Metrics

| Metric | Definition | Why it matters |
|---|---|---|
| **Auto-match rate** | % records resolved with no human | Headline throughput |
| **Precision** | correct matches ÷ posted matches | Correctness of what was posted |
| **Recall** | correct matches ÷ true links | Coverage |
| **False-match rate** | incorrect matches ÷ posted matches | **The headline metric.** In finance this is the one that costs money |
| **Exception rate by reason code** | distribution across T4 codes | Honesty and diagnosability |
| **LLM invocation rate** | % records that touched a model | Evidence for the architectural thesis |
| **Cost per 100 records** | tokens × price | Production viability |
| **Throughput** | records/sec wall clock | Track bar explicitly asks for throughput |
| **Hallucination count** | membership-gate rejections | Proof the gates fire on real data |
| **Value reconciled vs. at risk** | ₹ posted vs ₹ in exceptions | Speaks the finance team's language |

### 9.2 Ablation table (the centrepiece of the pitch)

Run the same `adversarial` fixture with tiers progressively enabled:

| Configuration | Auto-match | Precision | False-match | LLM calls |
|---|---|---|---|---|
| T0 only | — | — | — | 0 |
| T0 + T1 | — | — | — | 0 |
| T0 + T1 + T2 | — | — | — | 0 |
| T0 + T1 + T2 + T3 (full) | — | — | — | — |
| **LLM-only baseline** (control) | — | — | — | all |

The **LLM-only baseline is essential.** It is the control arm that turns "I built a cascade" into "I measured that the cascade beats the obvious approach on the metric that matters, using fewer model calls." Without it, the architecture is an assertion. With it, it is a result.

### 9.3 Rule-promotion lift

Measure auto-match rate before and after promoting the rules generated by resolving five exceptions. Report the delta. This is the evidence that the agentic loop does something, rather than being a UI flourish.

### 9.4 Reproducibility

`make eval` regenerates all fixtures from seeds, runs all configurations, and writes `results/metrics.md`. Every number in the README is produced by that command and by nothing else. Never hand-write a metric into the README.

---

## 10. Tech Stack and Justification

Each choice is defensible in a panel interview — that is the selection criterion, alongside a 14-day delivery window.

| Layer | Choice | Why |
|---|---|---|
| Language | **Python 3.11** | Fastest path; the domain is data manipulation |
| Data | **pandas** | Familiar; dataset is ≤ few thousand rows. *Polars would be nicer signal but is a needless risk at this scale — not the place to learn a new API* |
| Store | **SQLite (WAL mode)** via SQLAlchemy Core | Single reviewable file that ships in the repo; append-only tables; real SQL (window functions, CTEs) demonstrable in interview. No infra to stand up |
| Validation | **Pydantic v2** | Schema gate for both file ingest and LLM output — same tool, two boundaries |
| Fuzzy matching | **rapidfuzz** | C++ speed, clean API, well-known in the domain |
| LLM | **Provider-agnostic adapter**; default Gemini Flash (free tier), Anthropic Claude switchable via env var | Cost control during 14 days of iteration; adapter itself is architectural signal (no vendor lock-in) |
| CLI | **Typer** | Typed commands, auto help, demoable |
| API | **FastAPI** | Prior experience; async; free OpenAPI docs for the reviewer |
| UI | **React + TypeScript + Vite**, Tailwind | Reuse established dashboard patterns; exception queue is a table with actions — do not over-build |
| Tests | **pytest** + hypothesis for the fee model | Property-based tests on money arithmetic is a strong signal; target ≥ 100 tests |
| Quality | **ruff** + **mypy --strict** on core modules | Visible in CI badge |
| CI | **GitHub Actions** — lint, type, test on every push | Reviewers look at the Actions tab |
| Repro | **Makefile** + `uv` or `pip-tools` lockfile | `make demo` must work from a clean clone |

**Deliberately excluded:** LangChain/LlamaIndex (one narrow structured call needs no framework, and a framework here would actively undercut the "AI judgment" argument), vector DB (nothing to retrieve), Docker/Kubernetes (unnecessary for a single-file SQLite app; a Makefile is more honest), any fine-tuning.

Being able to say *"I considered LangChain and rejected it because the entire LLM surface is one schema-constrained call"* is worth more than using it.

---

## 11. Repository Structure

```
ledgerloop/
├── README.md                  # problem → architecture → measured results → run it
├── ARCHITECTURE.md            # the diagram + decision log from §3
├── Makefile                   # make demo | make eval | make test
├── pyproject.toml
├── .github/workflows/ci.yml
│
├── ledgerloop/
│   ├── cli.py                 # Typer entrypoint
│   ├── generate/
│   │   ├── synth.py           # seeded generator
│   │   ├── chaos.py           # injectors from §5.5
│   │   └── fee_model.py       # MDR / GST / TDS tiers
│   ├── ingest/
│   │   ├── loader.py          # fingerprint + idempotent upsert
│   │   ├── schemas.py         # Pydantic row models
│   │   └── quarantine.py
│   ├── store/
│   │   ├── schema.sql
│   │   └── db.py
│   ├── cascade/
│   │   ├── tier0_exact.py
│   │   ├── tier1_tolerant.py
│   │   ├── tier2_subsetsum.py
│   │   ├── tier3_llm.py
│   │   ├── gates.py           # schema / membership / arithmetic
│   │   └── orchestrator.py
│   ├── llm/
│   │   ├── adapter.py         # provider-agnostic
│   │   ├── prompts/v1.py      # versioned
│   │   └── cache.py           # SHA-256 keyed
│   ├── exceptions/
│   │   ├── codes.py
│   │   └── clustering.py      # group by reason + merchant
│   ├── rules/
│   │   ├── promote.py         # resolution → generalised rule
│   │   └── store.yaml
│   ├── audit/provenance.py
│   └── api/main.py            # FastAPI
│
├── eval/                      # ONLY module permitted to read truth_links
│   ├── harness.py
│   ├── ablation.py
│   └── metrics.py
│
├── ui/                        # React
├── fixtures/                  # easy / realistic / adversarial
├── results/metrics.md         # generated, never hand-edited
└── tests/
```

---

## 12. 14-Day Build Plan

Deadline 5 Sep. **Plan to submit 4 Sep.** Never submit on the closing day.

| Day | Date | Deliverable | Done when |
|---|---|---|---|
| 1 | Aug 22 | Repo, CI, schema, fee model + property tests | `make test` green on fee arithmetic |
| 2 | Aug 23 | Generator + all chaos injectors + ground truth | 3 seeded fixtures reproduce byte-identically |
| 3 | Aug 24 | SQLite schema, ingest, fingerprinting, idempotency, quarantine | Re-running the same file is a no-op |
| 4 | Aug 25 | Tier 0 + provenance records | Exact matches post with full audit rows |
| 5 | Aug 26 | Tier 1 tolerant matching | Fee/date/fuzzy paths covered by tests |
| 6 | Aug 27 | Tier 2 subset-sum + meet-in-the-middle | Correct on hand-built N:1 cases |
| 7 | Aug 28 | Tier 2 ambiguity guard + pool cap | `DECOY_SUBSET` produces `AMBIGUOUS_SUBSET`, never a match |
| 8 | Aug 29 | **Eval harness + ablation + LLM-only baseline** | `make eval` writes real numbers |
| 9 | Aug 30 | Tier 3 adjudicator, three gates, caching | Injected hallucination is caught and counted |
| 10 | Aug 31 | Exception queue, reason codes, clustering | Exceptions sorted by ₹ at risk |
| 11 | Sep 1 | Rule promotion loop + measured lift | Before/after auto-match delta recorded |
| 12 | Sep 2 | FastAPI + React UI | Full demo flow clickable end to end |
| 13 | Sep 3 | README, ARCHITECTURE.md, diagram, final `make eval`, clean-clone test | Fresh clone → `make demo` works |
| 14 | Sep 4 | Record and cut 5-min pitch video · **submit** | Submitted |

**Buffer policy:** if Day 12 slips, cut the UI to a static HTML report generated by the CLI and ship. A CLI with real measured numbers beats a pretty UI with none — the bar is throughput, measured accuracy, and an honest exception list, not visual polish.

**Cash forecaster (Track 04 example direction) is explicitly a stretch goal.** Only build it if Day 12 finishes early, and if so keep it simple: empirical settlement-lag distribution from matched history → 14-day inflow projection with P50/P90 bands. No ML. Do not let it displace the eval harness.

---

## 13. Pitch Video Script Outline

Five minutes. Rehearse aloud; a panel watching dozens of these rewards clarity above all.

| Time | Beat | Content |
|---|---|---|
| 0:00–0:35 | **The problem, concretely** | Three files on screen. "Same money, three descriptions, none of them agree. A finance associate matches these by hand for two days a month." Point at the fee deduction and the batched credit |
| 0:35–1:10 | **Why the obvious approach fails** | Show the LLM-only baseline output. It looks great. Then reveal its false-match rate against ground truth. "In reconciliation, a wrong match is worse than no match — it closes silently and surfaces at audit" |
| 1:10–2:20 | **The architecture** | The cascade diagram. Land the thesis: *"An LLM touches under a quarter of records by design, and it never has final say on any of them. Arithmetic decides; the model only proposes evidence."* Walk the three gates |
| 2:20–3:10 | **Live run** | `make demo`. Tier counts stream. Metrics table appears. Open a matched row → full provenance. Open the exception queue sorted by ₹ at risk |
| 3:10–3:45 | **Failure handled gracefully** | Inject a hallucinated settlement ID live. Membership gate rejects it, exception raised, counter increments, **batch keeps running** |
| 3:45–4:20 | **The agentic loop** | Resolve one exception → system proposes a generalised rule → approve → re-run → show the auto-match rate rise |
| 4:20–5:00 | **Numbers and honesty** | Ablation table. State the measured false-match rate out loud. Name what is *not* solved and what the exception queue still contains. Close on reproducibility: "every number here comes from `make eval` on a seeded fixture in the repo" |

Ending on candour rather than a victory lap is the correct read of a bar that says one cherry-picked match proves nothing.

---

## 14. Scope Guards, Risks, Open Questions

### 14.1 Explicit non-goals

Stating these in the README prevents "why didn't you do X" and reads as maturity, not omission.

- No real bank or Razorpay production API integration (track specifies synthetic).
- No multi-currency or FX reconciliation.
- No GST filing, tax computation, or statutory reporting.
- No multi-tenant auth, RBAC, or user accounts.
- No fine-tuned or self-hosted model.
- No general-purpose "finance chatbot" surface.

### 14.2 Risks

| Risk | Mitigation |
|---|---|
| Generator too clean → results meaningless | Build the `adversarial` fixture on Day 2 and tune against it throughout |
| Subset-sum eats days 6–7 | Ship bounded DP first; meet-in-the-middle is an optimisation, not a prerequisite |
| UI eats the back half | Hard cut-off: static HTML report fallback (§12 buffer policy) |
| LLM cost during iteration | Response cache from day one; free-tier default; cached fixture responses committed for CI |
| Overfitting the cascade to one's own generator | Hold out the `adversarial` fixture; tune only on `realistic` |
| Scope creep into the cash forecaster | Explicitly gated behind Day 12 completion |

### 14.3 Open questions to resolve before Day 1

1. **Name.** LedgerLoop / CloseLoop / Tallyd / ReconRail / SettleSense — pick one and register the repo today; renaming mid-build costs an hour.
2. **Team or solo?** Confirm whether the buildathon permits teams. If so, a second person owns generator + UI while you own the cascade + eval.
3. **Reuse policy.** Confirm the rules permit building on patterns from your own prior public work, then declare the StatementSync lineage in the README either way.
4. **Logistics.** The role is in-person Bangalore, 6 or 12 months, ₹75,000/month. You graduate July 2027 — verify compatibility with your campus placement commitments *before* investing 14 days.
5. **LLM provider.** Confirm free-tier quota is sufficient for ~14 days of iteration at your expected call volume, or budget for paid usage.

---

## 15. The One Sentence

If the panel remembers a single thing:

> **LedgerLoop reconciles a three-source settlement batch with an LLM touching under a quarter of the records — because on money, arithmetic decides, the model only proposes evidence, and anything ambiguous is handed back with a reason code rather than guessed.**

---

*Spec v1.0 — 22 August 2026. All metrics are targets pending measurement.*
