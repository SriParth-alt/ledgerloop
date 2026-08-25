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

---

## ADR-006 — Per-concern RNG streams, not one sequential generator

**Date:** 2026-08-24
**Status:** Accepted

**Context:** §5.6 requires `--seed 42` to reproduce a byte-identical dataset, and §5.5 requires each
chaos injector to sit behind an independently toggleable flag "so the ablation table can attribute
failures to specific real-world phenomena". Those two requirements pull in different directions once
you write the obvious implementation: a single `random.Random(seed)` drawn sequentially through
generation. That is reproducible, but it is not independent.

**Decision:** Every random decision draws from a stream derived from `(seed, concern, row_index)`
through `blake2b`, so each injector consumes its own entropy and disturbs no other.

**Alternatives considered:**
- **One sequential `Random(seed)`.** Lost: enabling any injector consumes a different number of
  draws, which shifts every subsequent value. `easy` and `realistic` at the same seed would then
  share no rows at all, and a difference between two ablation runs could not be attributed to the
  flag that was toggled — it would just be two unrelated datasets.
- **Deriving stream keys with Python's built-in `hash()`.** Lost: `hash()` is salted per process via
  `PYTHONHASHSEED`, so byte-identical reproduction would hold within one run and fail between runs,
  intermittently and invisibly. `blake2b` is stable across processes and platforms.

**Consequences:** This buys genuine flag independence: the three fixtures at one seed describe *the
same underlying world* at three corruption levels, which is the only thing that makes the §9.2
ablation table a comparison rather than a collection of unrelated numbers. It is also what lets
`test_enabling_narration_noise_changes_only_narrations` assert that every amount and date is
byte-for-byte unchanged. The cost is real: it is more machinery than one generator, and every new
injector must remember to claim its own concern name. A copy-pasted concern string would silently
couple two injectors, and the tests only cover the pairs they explicitly compare — nothing detects
the general case. That is a known sharp edge, not an oversight.

---

## ADR-007 — Ground truth is built first, then rendered, then corrupted

**Date:** 2026-08-24
**Status:** Accepted

**Context:** The generator has to emit `truth_links.csv` alongside three source files. There are two
ways to get there: generate the files and then work out the links by inspecting them, or build the
world with the links known by construction and render the files as lossy views of it. Since every
precision, recall and false-match figure in the submission is computed against this file, the way it
is produced determines whether those figures mean anything.

**Decision:** We build the world first, freeze ground truth, and only then render and degrade the
views — splitting injectors into structural ones that run before the freeze and cosmetic ones that
run after and cannot touch a link.

**Alternatives considered:**
- **Derive truth by inspecting the generated files.** Lost: a generator bug and a matcher bug could
  cancel each other out, and the failure would be invisible — the metrics would simply look
  excellent, which is the one failure mode this project cannot afford.
- **One undifferentiated list of injectors.** Lost: without the structural/cosmetic split there is no
  principled statement of which injectors are *allowed* to change truth, so the guarantee could not
  be tested.

**Consequences:** This makes `test_cosmetic_injectors_never_alter_ground_truth` possible: adding
narration noise, dropping UTRs, varying names and shuffling file order must leave the link set
identical. It also means corruption can be made arbitrarily aggressive without any risk of
invalidating the answer key. The cost is that the split is maintained by hand — a new injector placed
in the wrong phase could corrupt truth silently, and the test only covers the four cosmetic flags it
names. `PAISE_DRIFT` additionally has to be suppressed on decoy credits, because ADR-003 requires the
competing explanations to be arithmetically *perfect*; drift would leave both merely approximate and
turn a genuine ambiguity back into a tiebreak.

---

## ADR-008 — The generator writes CSV with the standard library, not pandas

**Date:** 2026-08-24
**Status:** Accepted

**Context:** §10 selects pandas as the data layer, and that remains right for reading and analysis.
Writing is a different problem: §5.6 requires byte-identical output from a seed, and the project is
developed on Windows while CI runs on Linux. `pandas.to_csv` carries quoting and numeric-formatting
behaviour that varies across versions, and the default line terminator differs by platform.

**Decision:** The generator writes through `csv.writer` with an explicit `lineterminator="\n"` and
`newline=""`, pinning the bytes; pandas stays the choice everywhere data is read or analysed.

**Alternatives considered:**
- **`pandas.to_csv`.** Lost: it adds version-dependent formatting surface to the one output that must
  be reproducible, for no benefit at a few hundred rows we already hold in memory.
- **Accepting the platform default line terminator.** Lost: reproducibility would hold on the
  development machine and fail in CI, which is the most expensive place to discover it.

**Consequences:** Output is byte-identical across platforms, verified by hashing two runs of the same
seed. A reviewer comparing this against §10 will notice the deviation, which is why it is recorded
here rather than left as an unexplained inconsistency. The cost is a second CSV idiom in the
codebase: anyone adding a writer must remember both parameters, and forgetting either breaks
reproducibility on Windows only — the failure would not reproduce in CI, which is the worst possible
shape for a bug. `test_written_files_use_lf_line_endings` guards the line terminator specifically.

---

## ADR-009 — Chaos intensity governs observability, never economics

**Date:** 2026-08-25
**Status:** Accepted

**Context:** `ChaosProfile.intensity` was applied uniformly to every injector, including
`PARTIAL_REFUND`. At the `adversarial` setting of 0.60 that produced a batch in which **40% of
settlements never reached the bank at all** — arithmetically consistent, since refunded and disputed
settlements correctly carry no truth link, but not a bank statement any finance reviewer would
recognise. Real refund and dispute rates sit in low single digits. §14.2 warns that a generator too
clean makes the results meaningless; this was the same failure mirrored, and it discredits the
fixture rather than the matcher.

**Decision:** Refund and dispute frequency is fixed by `REFUND_EVENT_RATE` (5%) independently of
`intensity`, which now governs only how badly the *observability* of a batch is degraded.

**Alternatives considered:**
- **Keep one intensity knob for everything.** Lost: it conflates two unrelated dimensions. How often
  money fails to arrive is a property of the merchant's business; how mangled the narration is, is a
  property of the bank's file format. Tying them together makes the adversarial fixture
  simultaneously too noisy and economically absurd.
- **Lower `adversarial` intensity overall.** Lost: that would weaken every observability injector at
  once, which is precisely the difficulty the held-out fixture exists to provide.

**Consequences:** Never-settled settlements drop from 40% to 2.8%, and the `adversarial` fixture
keeps every injector at full strength while still looking like a real statement. The cost is a second
knob: intensity no longer explains the whole corruption story, so anyone tuning a fixture has to know
that refund frequency lives in a module constant instead. There is also a defensible-but-arbitrary
number in the code now — 5% is plausible, not sourced from a real merchant, and the write-up should
say so rather than imply it was measured.

---

## ADR-010 — `link_type` records cardinality only; events go in `chaos_tags`

**Date:** 2026-08-25
**Status:** Accepted

**Context:** The ground-truth vocabulary originally mixed two independent questions. `REFUND_OFFSET`
described what *happened* to a settlement, while `ONE_TO_ONE` and `BATCH_MEMBER` described the
*shape* of the link. Because a row can only carry one value, a refunded settlement inside a batch was
labelled `refund_offset` and its batch membership was lost — 44 rows in one 250-record fixture.

**Decision:** `link_type` is `ONE_TO_ONE`, `BATCH_MEMBER` or `ORPHAN_CREDIT` and answers only "how
many settlements explain this credit"; refunds and re-posts are recorded in `TruthLink.chaos_tags`.

**Alternatives considered:**
- **Keep the mixed vocabulary.** Lost: on day 8 the ablation would under-count batched links and the
  shortfall would look like the batching injector firing less often — a measurement error that
  reads as a data property, which is the hardest kind to notice.
- **Add a compound type such as `batch_member_refunded`.** Lost: the vocabulary would multiply with
  every new event, and `eval/` would need to parse the name apart to recover cardinality anyway.

**Consequences:** Cardinality and event are now independently queryable, which is what the
per-phenomenon attribution in §9.2 needs. Both `REFUND_OFFSET` and `DUPLICATE_POST` were removed from
the enum: with events living in tags, neither had a remaining use, and a reserved-but-unused member
invites someone to reach for it later and reintroduce the conflation. The cost is that the vocabulary
was already declared fixed in ADR-005's spirit — changing it now is cheap only because no fixture has
shipped yet. After day 8 this would have meant regenerating every published number.

---

## ADR-011 — A re-posted credit carries a new identity, and its zero cardinality is `ORPHAN_CREDIT`

**Date:** 2026-08-25
**Status:** Accepted

**Context:** `DUPLICATE_POST` originally emitted a byte-identical bank row reusing the same
`bank_txn_id`, on the reasoning that the ingest fingerprint would absorb it. Day 3's target file
contradicts that directly: `ingest/loader.py` requires the injector to "surface as
DUPLICATE_SUSPECTED rather than silently double-counting money". A byte-identical row cannot — its
fingerprint matches, so it is absorbed with no exception raised. It would also collide on
`PRIMARY KEY (run_id, bank_txn_id)` and fail the insert before any detection could run. Separately,
ground truth needs a way to say what this credit is, since every credit must appear in
`truth_links.csv`.

**Decision:** The re-post keeps the same amount, value date and narration but takes a new
`bank_txn_id`; in ground truth it is `link_type = ORPHAN_CREDIT` carrying `chaos_tags =
(DUPLICATE_POST,)`.

**Alternatives considered:**
- **Keep the identical row and identical id.** Lost: it is silently absorbed, which is the exact
  behaviour the loader TODO forbids, and it breaks the primary key.
- **Reintroduce `LinkType.DUPLICATE_POST`.** Lost: that is an *event*, and ADR-010 established that
  `link_type` answers cardinality only. A re-post genuinely has zero settlements behind it, which is
  what `ORPHAN_CREDIT` already means; adding an event-shaped member would reintroduce precisely the
  conflation ADR-010 removed.
- **Give the re-post no truth link at all.** Lost: every bank credit must be accounted for in ground
  truth, or day 8 scores it as a false negative no matcher could ever have resolved.

**Consequences:** The generator now produces an input the idempotency layer can actually be tested
against — same money, different identity — rather than one the fingerprint trivially eats. Whole-file
re-ingestion still no-ops, because every fingerprint matches. The cost is a genuine readability wart:
a reviewer opening `truth_links.csv` sees a row labelled `orphan_credit` that is really a duplicate,
and has to read the tag beside it to understand. That is the price of keeping cardinality and event
orthogonal, and it is cheaper than a vocabulary that multiplies with every new phenomenon. `eval/`
must therefore branch on the tag, not the link type, when deciding whether a credit should end as
`ORPHAN_CREDIT` or `DUPLICATE_SUSPECTED` — those are different expected outcomes for rows that look
identical in the `link_type` column.
