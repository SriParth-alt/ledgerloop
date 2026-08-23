# LedgerLoop — Architecture Decision Record

Decisions recorded here are the ones a reviewer is entitled to challenge. Each entry states what
forced the choice, what was chosen, what was rejected and why, and what the choice costs —
including the parts that are not flattering.

The first five entries are transcribed from `PROJECT_SPEC.md` §3, which is the source of truth for
design rationale. Their **Date** is the date of Spec v1.0 (22 Aug 2026), when the decisions were
actually made, not the date this file was written.

Supersede rather than edit: if a decision changes, add a new ADR and set the old one's status to
`Superseded by ADR-00X`. The trail of reversals is the useful part.

---

## ADR-001 — Track selection: Track 04, AI Finance Controller

**Date:** 2026-08-22
**Status:** Accepted

**Context:** The Razorpay AI Buildathon 2026 offers five tracks and a 14-day build window with one
buffer day. The panel evaluates problem taste and AI judgment, not just a working demo, so the
choice of problem is itself scored. With a fixed window, the deciding constraint is not which
problem is most interesting but which one converts capital already accumulated into delivered work
rather than spending days on cold start.

**Decision:** We target Track 04 (AI Finance Controller), because its bar — measured accuracy and
an honest exception list — rewards engineering discipline over demo polish, and because prior work
on StatementSync (fingerprinting, idempotent loading, quarantine handling) is load-bearing here
rather than incidental.

**Alternatives considered:**
- **Track 01 — AI Growth & Agentic Commerce.** Lost: highest hype means highest submission volume,
  and the emerging protocols (UAP, ACP, AP2, x402) plus Razorpay test-mode API depth would consume
  most of the 14 days just to understand.
- **Track 02 — AI Risk Manager.** Lost: genuine fit with prior ML work, but "fraud detection with
  precision/recall" is the single most common student portfolio project in existence, so
  differentiation is very hard.
- **Track 03 — AI Revenue Recovery.** Lost: broadest and most demo-friendly, therefore likely the
  most-submitted; and its bar requires measured money recovered across a batch, which means
  building a simulation environment from scratch anyway.
- **Track 05 — Open Track.** Lost: no structural advantage, and an unconstrained brief is harder to
  score well against.

**Consequences:** Roughly 2 of the 14 days are effectively pre-paid by patterns already worked out.
The framing is the least glamorous of the five, so the submission has to win on measured numbers
rather than on the first thirty seconds of the video — if the eval harness slips, there is no
visual spectacle to fall back on. Reuse of prior patterns creates an obligation: the StatementSync
lineage must be declared explicitly in the README and the pitch, because reviewers respect declared
lineage and penalise undeclared reuse. This is a new build in a new repository; patterns and
lessons carry over, code does not.

---

## ADR-002 — Deterministic tiers decide; the LLM adjudicates only the residual

**Date:** 2026-08-22
**Status:** Accepted

**Context:** The core architectural question is whether the model drives matching or cleans up
after it. On a 50–500 record batch, roughly 60–80% of records are resolvable by pure arithmetic and
string normalisation. The track framing is that verification capacity, not generation speed, is the
2026 bottleneck — which points at the model being useful for reading unstructured evidence, not for
deciding outcomes. Something else has to hold the line on correctness.

**Decision:** We run a deterministic cascade first (T0 exact, T1 tolerant, T2 subset-sum) and invoke
the model only on the residual that survives it, as a proposer of evidence whose every proposal is
re-verified in Python.

**Alternatives considered:**
- **LLM-first, rules as fallback.** Lost: routes records that arithmetic had already solved through
  a model — paying money, latency, and nondeterminism in re-runs for zero accuracy gain, and
  destroying auditability on the majority of volume.
- **LLM as an orchestrator calling rule tools.** Lost: architecturally fashionable, but it puts an
  unpredictable control flow over money movement, which is the opposite of what a finance system
  wants.

**Consequences:** This buys the most defensible sentence in the pitch — *"an LLM touches under a
quarter of records by design, and it never has final say on any of them"* — plus reproducible
re-runs and a low per-batch cost. It costs real engineering: the deterministic tiers have to
actually work, so days 4–7 go to fee arithmetic, tolerance bands, and subset-sum instead of prompt
iteration, and any tier that underperforms pushes load onto T3 rather than being papered over by
it. The invocation-rate claim is a measured number, not an assertion, so it has to come out of the
eval harness — if it lands above a quarter, the sentence changes.

---

## ADR-003 — Ambiguity raises an exception; the system never picks a winner

**Date:** 2026-08-22
**Status:** Accepted

**Context:** A bank credit of ₹48,220 can be explained by two different valid subsets of
settlements. Both explanations are arithmetically perfect, so no principled tiebreak exists — any
ranking we invent is a preference dressed as a reason. In reconciliation a wrong match is worse
than no match: an unmatched row stays visible in a queue, while a falsely matched row closes out
silently, corrupts the books, and surfaces months later at audit with its provenance gone.

**Decision:** When two or more valid explanations exist, we emit `AMBIGUOUS_SUBSET` with every
candidate explanation attached and let a human choose in two clicks.

**Alternatives considered:**
- **Pick the highest-scoring candidate.** Lost: with both subsets arithmetically exact, the score
  is a tiebreak we invented, so this is a coin flip on the books — wrong roughly half the time by
  construction.

**Consequences:** This costs headline auto-match rate, visibly and on purpose, and the eval harness
is built to show precisely what that trade cost in numbers rather than hide it. It buys the metric
that actually matters in finance — false-match rate — and it makes the demo stronger, because the
`DECOY_SUBSET` chaos injector exists specifically so the system can be shown declining a match that
a naive implementation would take confidently. The policy extends to Tier 3: the model is not
permitted to resolve ambiguity either, because this is a decision we have chosen to give a human,
not a capability gap we are waiting to close.

---

## ADR-004 — The agentic surface is the exception→rule promotion loop, not the cascade

**Date:** 2026-08-22
**Status:** Accepted

**Context:** The track wants an agent, not a batch script, and the cascade — deliberately — is not
agentic: it is arithmetic with one narrow, schema-constrained model call on the residual (ADR-002).
Something in the system still has to learn. A merchant's idiosyncrasies otherwise require an
engineer to reconfigure the matcher, which does not scale and is not what the brief is asking for.
Meanwhile, a cluster of exceptions sharing one reason code and one merchant is not twelve problems,
it is one wrong assumption.

**Decision:** We locate the agentic surface in the exception resolution loop: a human resolves an
exception, the system inspects the resolution and proposes a generalised rule in natural language
plus machine-readable form, and on approval it persists so the next run resolves that class
automatically.

**Alternatives considered:**
- **Make the cascade itself agentic** (a model plans and orchestrates the tiers). Lost: rejected in
  ADR-002 — unpredictable control flow over money movement.
- **Ship the exception queue as a read-only to-do list.** Lost: it would be a UI flourish with no
  measurable effect, and the system would learn nothing across runs.

**Consequences:** The learning is measurable and demoable — §9.3 requires reporting auto-match rate
before and after promoting rules from five resolved exceptions, so the loop has to produce a real
delta or it is exposed as decoration. It buys a system that adapts to a merchant over runs instead
of being reconfigured by an engineer, and it reframes the exception queue as a diagnostic
instrument rather than a backlog. The costs: promotion is a write path that changes future matching
behaviour, so a bad generalisation from a single resolution can create false matches at scale, and
human approval is the only gate on it. It also lands on Day 11, downstream of almost everything,
making it the most likely casualty of a schedule slip.

---

## ADR-005 — Synthetic data with generated ground truth, not real API data

**Date:** 2026-08-22
**Status:** Accepted

**Context:** The track explicitly specifies a 50+ record batch of synthetic data. Independently of
that requirement, the headline metrics — precision, recall, and above all false-match rate — are
undefined without knowing which links are actually correct. Real gateway data does not arrive with
a labelled answer key, so measuring correctness on it is not possible within this window. Synthetic
is therefore not a shortcut here; it is the only route to ground truth.

**Decision:** We generate all three sources from a seeded generator that also emits
`truth_links.csv`, and treat that generator as a first-class component of the system rather than a
test fixture.

**Alternatives considered:**
- **Real Razorpay test-mode API data.** Lost: the track specifies synthetic, and without ground
  truth precision and recall cannot be computed at all — the central claim would be unmeasurable.
- **Hand-built fixtures.** Lost: too small and too clean to produce meaningful rates, and they
  cannot be regenerated byte-identically from a seed, so published numbers would not be
  reproducible.

**Consequences:** This is what makes every number in the submission reproducible: `--seed 42`
reproduces a byte-identical dataset, three canonical fixtures (`easy`, `realistic`, `adversarial`)
ship in the repo, and a reviewer can regenerate every published figure with one command. It also
creates the project's sharpest risk — we are grading ourselves on our own exam. A generator that is
too clean makes the results meaningless; one that is unrealistic makes the panel discount
everything downstream. Two mitigations are load-bearing: the `adversarial` fixture is held out and
tuning happens only on `realistic`, and ground truth is structurally walled off, with `eval/` the
only package permitted to read it, enforced by `tests/test_no_truth_leak.py`. That test is the
reason the numbers can be trusted, because a leak would not look like a failure — it would look
like excellent results.
