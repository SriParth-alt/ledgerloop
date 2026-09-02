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

---

## ADR-012 — Duplicate detection keys on amount, date **and** narration

**Date:** 2026-08-25
**Status:** Accepted

**Context:** A re-posted bank credit has to be distinguished from a credit that merely resembles
another. The obvious signature is `(credit_paise, value_date)` — same money, same day. That is wrong
here, and wrong in a way that would have looked fine in a demo: `DECOY_SUBSET` exists specifically to
put two credits of identical value on the same date, so every decoy pair would be reported as a
re-post. The adversarial fixture would then arrive at Tier 2 pre-polluted with false
`DUPLICATE_SUSPECTED` exceptions, and the ambiguity demo would be buried in noise of our own making.

**Decision:** Two credits are "the same money" only when amount, value date **and** narration all
match; the generator's re-post is character-identical in narration, and a decoy pair is not.

**Alternatives considered:**
- **`(amount, date)` alone.** Lost: flags every `DECOY_SUBSET` pair, which is a false positive
  manufactured by our own fixture.
- **Fuzzy narration comparison.** Lost: a genuine re-post is a byte-for-byte repeat of the same
  statement line. Fuzziness here buys nothing and imports a threshold to defend.
- **Detect duplicates later, in the cascade.** Lost: by then the row is already a matching candidate,
  and the cheapest place to notice that two rows describe one payment is while reading them.

**Consequences:** A re-post that the bank re-words — same money, different narration text — is missed
at ingest. That is a real gap, and the honest mitigation is that it degrades to an ordinary unmatched
row and reaches the exception queue anyway, rather than being silently absorbed. The false-positive
direction is the one that matters more, because a `DUPLICATE_SUSPECTED` on a legitimate credit sends
a human to investigate money that is fine. `test_decoy_subsets_do_not_trigger_duplicate_suspicion`
guards that direction specifically, and it asserts the fixture still contains the tricky case so it
cannot quietly become vacuous.

---

## ADR-013 — Idempotency is scoped to a run, not to the database

**Date:** 2026-08-25
**Status:** Accepted

**Context:** §8 promises that "re-running the same file is a no-op", which reads as a global
statement about the database. But every table is keyed `(run_id, ...)`, and §9.2's ablation works by
executing the same fixture under several configurations and diffing the results. Those two
requirements are in tension: if ingest refused to load a file it had ever seen, the second
configuration in an ablation would have no rows to reconcile.

**Decision:** Fingerprint checks are scoped to the current `run_id`. Re-running ingest for the same
run is a no-op; the same file under a new run loads a fresh, independent copy.

**Alternatives considered:**
- **Global fingerprint uniqueness.** Lost: the second and later arms of an ablation would silently
  ingest nothing and report a zero match rate against an empty batch.
- **Share one copy of the source rows across runs.** Lost: runs stop being independent objects, and a
  correction applied during one run would retroactively alter the inputs of an earlier one —
  destroying exactly the provenance the append-only design exists to protect.

**Consequences:** Runs are genuinely comparable, which is what §9.2 needs. Storage is duplicated
across runs, which at a few hundred rows is irrelevant and would matter at a scale this project does
not target. The wording in §8 is now narrower than it sounds and should be read as "within a run";
that is a documentation debt, not a behaviour change. The index added for this is composite —
`(run_id, row_sha256)` — matching the access pattern exactly.

---

## ADR-014 — Append-only governs matching decisions; a run's own lifecycle is updated in place

**Date:** 2026-08-25
**Status:** Accepted

**Context:** The append-only rule is stated without qualification: corrections are new rows, never
UPDATEs, because an UPDATE destroys the audit trail. `finish_run` breaks that letter — it writes
`finished_at` and `degraded` onto an existing `runs` row. This is worth recording precisely because it
looks like a violation of a rule the project calls non-negotiable.

**Decision:** The append-only guarantee covers the tables that carry decisions — `match_records`,
`exceptions`, `quarantine`, and the three source tables. A run row is opened when the run starts and
closed when it ends, in place.

**Alternatives considered:**
- **Append a second row to close the run.** Lost: `schema.sql` declares `run_id` as the primary key of
  `runs` with a nullable `finished_at`, so a second row is not insertable. The schema already encodes
  the intent that a run is one row with a lifecycle.
- **A separate `run_events` table.** Lost: it is the same information behind a join, and it would
  invite the run's *outcome* to drift away from the run itself.

**Consequences:** The audit trail is unaffected: nothing that records a decision about money is ever
mutated, and `degraded` — which says a run's numbers are not comparable to a full run's — is written
exactly once, at the end. The cost is that "append-only" is now a rule with an exception, and anyone
reading `CLAUDE.md` will meet the unqualified version first. That is why this ADR exists; the rule
should probably be reworded to say *decisions* are append-only.

---

## ADR-015 — Tier 0's amount-and-date rule ships with a known false-match hole

**Date:** 2026-08-25
**Status:** Accepted

**Context:** §6 gives Tier 0 two rules: exact normalised UTR, and an exactly-unique
`(net_amount_paise, value_date)` pair. The uniqueness guard closes the ambiguous cases — a key
claimed by two rows on either side falls through. It does **not** close a subtler one. If a batched
credit's total coincidentally equals some unrelated settlement's net on the same date, and each key
happens to be unique on its own side, Tier 0 posts a wrong match at confidence 1.0 and nothing later
revisits it. The tier cannot detect this, because it has no way to know a credit was batched — that
is precisely what Tier 2 exists to work out.

**Decision:** We implement §6 as written, including the amount-and-date rule, and record the hole
here rather than quietly departing from the spec.

**Alternatives considered:**
- **Defer the amount-and-date rule to Tier 1.** Lost *for now*: Tier 1 recomputes the expected net
  from the fee model, so a coincidental total would have to survive a second, independent check —
  genuinely safer. But it changes what §6 says Tier 0 is, and the ablation table has a "T0 only" row
  whose meaning would shift. Worth revisiting on day 8 with a measurement in hand instead of an
  argument.
- **Drop the rule entirely and match only on references.** Lost: NO_UTR chaos removes the reference
  from a real share of rows, and those records would fall to Tier 3 — pushing model usage up for
  records arithmetic could have settled, which is the opposite of ADR-002.

**Consequences:** This is a knowing acceptance of a correctness risk, and it sits uneasily beside the
rule that a false match is worse than an exception. Two things make it tolerable rather than
reckless. The reference rule runs first and its matches are removed, so the weaker rule draws from a
smaller pool — on the adversarial fixture at 250 records, 51 of 65 Tier 0 matches came from the
reference rule and only 14 from amount-and-date. And the eval harness measures exactly this: if day 8
reports a non-zero false-match rate, `rule_id` attributes it to `T0-AMOUNT-DATE-UNIQUE` immediately,
and the first alternative above becomes the fix. Shipping the rule and measuring it is more honest
than dropping it on suspicion — but the measurement is not optional, and this ADR is the reminder.

**Update, 2026-08-26 (day 8).** Measured. On `adversarial` at 250 records the T0-only arm posted 22
matches with **zero** wrong, and the false-match rate is 0.0% at every arm on every fixture. The hole
described above is real in principle and did not fire in practice, so the rule stays as specified.
This ADR stays open rather than closed: the risk is a property of the rule, not of this fixture, and
a different seed or a denser batch could still surface it. `rule_id` is what would attribute it.

---

## ADR-016 — An ablation arm is a configuration; a missing model is a degradation

**Date:** 2026-08-25
**Status:** Accepted

**Context:** A run can end up executing fewer than four tiers for two entirely different reasons.
`--tiers 0,1,2` is the ablation harness deliberately running an arm of §9.2's table. `--no-llm`, or a
model that is down, is a failure the batch survived. The `runs.degraded` column is a single flag, and
which of these it means determines whether a run's numbers can be compared to another's.

**Decision:** `degraded` is set only when tier 3 was requested and could not run. Running fewer tiers
on purpose leaves it clear.

**Alternatives considered:**
- **Mark any run with fewer than four tiers as degraded.** Lost: every row of the ablation table
  except the last would be flagged, and the flag would carry no information at all.
- **Never set the flag and infer degradation from `tiers_enabled`.** Lost: `--tiers 0,1,2,3 --no-llm`
  and `--tiers 0,1,2` would then be indistinguishable in the store, and the first one's auto-match
  rate would silently be quoted as a full-cascade result.

**Consequences:** §9.2's arms stay comparable, and §8's promise — the batch completes without Tier 3,
auto-match rate falls, correctness does not — becomes checkable in the data rather than asserted in
prose. `finish_run` now also rewrites `tiers_enabled` with what actually executed, because the value
set at `start_run` is an intention and a run whose label disagrees with its behaviour would poison
every comparison drawn from it. That is a second in-place write on the `runs` row, under the same
reasoning as ADR-014.

---

## ADR-017 — Tier implementations are pure functions over rows, not over a connection

**Date:** 2026-08-25
**Status:** Accepted

**Context:** Each tier needs unmatched bank rows and settlements, and produces proposed matches. The
obvious shape is for a tier to take a database connection and do its own reading and writing. The
project also makes a strong public claim — tiers 0 to 2 never call a model — that a panel is entitled
to want evidence for beyond an assurance.

**Decision:** `match_tier0(bank_txns, settlements) -> list[ProposedMatch]` takes rows and returns
proposals. The orchestrator does every read and every write.

**Alternatives considered:**
- **Tiers take the connection.** Lost: constructing a difficult case then means staging it through a
  CSV and an ingest, so the awkward cases — a reference shared by two settlements, a decoy pair —
  are expensive to write and get skipped. It also puts I/O and matching logic in one module, where a
  future network call would not look out of place.
- **A repository object injected into each tier.** Lost: the indirection buys nothing at this size and
  makes the import-block argument below weaker.

**Consequences:** Hard rule 1 becomes auditable by inspection: `tier0_exact.py` imports `re`,
`collections`, `datetime`, and two data types. A module with no client and no connection cannot call
a model, and that is demonstrable in a panel by scrolling to the imports — considerably stronger than
a sentence in a README. The tier is also trivially testable, which is why most of `test_tier0.py`
covers cases where the tier must *decline*. The cost is that the orchestrator holds all the state:
it tracks which settlements have been claimed across tiers, and a bug there could let one settlement
be spent twice with no tier being at fault. That risk is concentrated in one place and covered by
`test_no_settlement_is_matched_by_two_credits`, but it is real and it will grow as tiers 1-3 land.

---

## ADR-018 — Tier 0's reference rule requires the amount to corroborate it

**Date:** 2026-08-26
**Status:** Accepted. Amends the rule described in §6.

**Context:** §6 specifies Tier 0's first rule as "match on exact normalised UTR", with no condition on
the amount. Taken literally that is wrong, and it was implemented literally on day 4. A batched
credit's narration carries only its *lead* settlement's reference, so the reference rule paired a
credit covering N settlements with exactly one of them — at confidence 1.0, marking the credit
resolved, orphaning the other members and under-explaining the money. Measured on the `realistic`
fixture at 250 records, **45 of 180 posted matches (25%) had a credit that did not equal the
settlement it was matched to.**

This was found on day 5 while measuring Tier 1, not by a test. Day 4's tests all passed: every one of
them constructed a 1:1 case, so none of them could see it.

**Decision:** Both Tier 0 rules now require exact amount agreement. A reference that matches while the
money does not is evidence that the credit is a *batch*, and it falls through to Tier 2.

**Alternatives considered:**
- **Leave it and let day 8 measure it.** Lost: the eval harness exists to find defects we do not
  already know about. Spending it re-confirming a 25% false-match class we can see today wastes the
  measurement, and every ablation arm built on that Tier 0 would need rerunning anyway.
- **Have the reference rule emit a partial match covering the lead settlement only.** Lost: a match
  asserting `credit = {A}` when the truth is `credit = {A, B}` is wrong, not partially right. It also
  consumes the credit, so Tier 2 never sees the batch it was built to solve.
- **Accept the amount within Tier 1's tolerance rather than exactly.** Lost: Tier 0 is the exact tier.
  Drift belongs to Tier 1, which has a tolerance band and records 0.99 rather than asserting a
  certainty it does not have.

**Consequences:** On `realistic`, posted matches fall from 180 to 135 and large amount mismatches fall
from 45 to zero. That headline drop is the point: rule 5 says a false match is worse than an
exception, and 45 of the lost matches were wrong while the other 46 simply moved to Tier 1, where
their paise drift is handled honestly. The residual grows from 5 to 50, which is Tier 2's actual
workload finally becoming visible — before this, batched credits were being consumed by Tier 0 and
the ablation would have shown Tier 2 contributing almost nothing.

The cost is a departure from §6 as written, which must be stated in the write-up rather than
glossed. The deeper lesson is about the tests, not the rule: sixteen Tier 0 tests passed throughout,
because every one of them built a 1:1 case. The defect lived in the interaction between a tier and a
chaos injector, and only appeared when the two met on a real fixture. Unit tests over hand-built rows
cannot find that class of bug, and day 8's harness is the thing that can.

---

## ADR-019 — Tier 2's tolerance band is flat, not proportional

**Date:** 2026-08-26
**Status:** Accepted

**Context:** Tier 2 initially reused Tier 1's tolerance — the larger of a flat rupee and 0.5%. On
`realistic` at 250 records that produced **4 matches and 36 `AMBIGUOUS_SUBSET` exceptions**. The
ambiguity looked like a property of the data. It was not: on a ₹30,000 credit, 0.5% is a ±₹150
window, and across a twenty-five settlement pool many subsets land inside it. The band had stopped
absorbing error and started inventing coincidences.

**Decision:** Tier 2 uses its own `subset_tolerance_bps`, defaulting to zero, so its band is the flat
rupee alone.

**Alternatives considered:**
- **Keep Tier 1's band.** Lost: it reports genuine batches as ambiguous, which is not a conservative
  failure — it is the *wrong diagnosis*. A finance associate reading 36 `AMBIGUOUS_SUBSET` rows would
  conclude the data is pathological when the tolerance is simply too wide.
- **Scale the band by subset size**, e.g. by √N. Lost: it invents a formula to defend with no
  grounding in what the error actually is, and every value of N would need justifying separately.
- **Tighten Tier 1's band to match.** Lost: Tier 1 genuinely needs the relative component, for the
  reason below.

**Consequences:** The justification is structural rather than tuned, which matters because a
tolerance that was fitted to a fixture would be indefensible. Tier 1 *recomputes* the expected net
from the fee model and must absorb that imprecision, so a relative band is right there. Tier 2 sums
the settlements' **reported** nets — there is no recomputation and therefore no model error to
absorb. The only thing it must tolerate is `PAISE_DRIFT`, one to three paise, which the flat rupee
covers a hundred times over.

Measured effect on `realistic`: matches 4 → 41, ambiguities 36 → 5, declines 10 → 4. The remaining
ambiguities are close to the planted decoy count, which is what ADR-003 always intended the exception
to mean. The cost is a real one: a merchant whose fee model is genuinely mis-specified would now miss
Tier 2 rather than scrape in, and §8 says that should surface as an exception cluster. It will — as
`NO_CANDIDATE` at the queue rather than as ambiguity here, which is the more honest signal. The knob
is in config precisely so the ablation can vary it instead of taking this reasoning on faith.

---

## ADR-020 — Only ambiguity terminates a record; every other decline falls through

**Date:** 2026-08-26
**Status:** Accepted

**Context:** Tier 2 is the first tier that can raise as well as match, which forced a question no
earlier tier had to answer: when a tier declines a credit *with a reason*, does that credit still
reach the next tier? The first implementation treated every raised exception as terminal. Measured
on all three fixtures, the residual reaching Tier 3 was **zero** — 105 credits declined as
`POOL_TOO_LARGE` had been swallowed, and the tier the whole architecture is built around would have
received nothing.

**Decision:** Only `AMBIGUOUS_SUBSET` removes a credit from the residual, expressed as `TERMINAL` in
`exceptions/codes.py`. Everything else falls through.

**Alternatives considered:**
- **Every exception is terminal.** Lost: it conflates a *policy* refusal with a *capability* limit,
  and silently starves Tier 3.
- **No exception is terminal.** Lost: an `AMBIGUOUS_SUBSET` credit reaching Tier 3 would hand the
  model the one decision §7.5 explicitly forbids it from making. Ambiguity is not a gap in our
  capability that a better tier might close; it is a decision we have chosen to give a human.

**Consequences:** The distinction is worth stating precisely because it is easy to state loosely.
Ambiguity is terminal *by policy* — the arithmetic is perfect on both explanations and no tier,
model or otherwise, is permitted to pick. `POOL_TOO_LARGE` is terminal *by nothing*: it records that
a deterministic search declined on complexity grounds, and Tier 3 sees at most eight pre-filtered
candidates, so it may well resolve what the subset search would not. The settlements in an ambiguous
pool stay unclaimed either way, because a human may resolve the credit differently from any
explanation we offered.

---

## ADR-021 — An oversized candidate pool is declined, not pruned

**Date:** 2026-08-26
**Status:** Accepted

**Context:** §6 says two things that cannot both apply: "cap the pool at N ≤ 25 by nearest-date
pruning", and "if the pool exceeds 25 after pruning, emit `POOL_TOO_LARGE`". If pruning caps it, it
can never exceed. A decision was needed about which half is operative.

**Decision:** If the candidate pool exceeds the cap, Tier 2 declines without searching. Nearest-date
pruning is not implemented.

**Alternatives considered:**
- **Prune to the nearest 25 and search.** Lost, and this is the important one: the tier's entire
  output is the claim "exactly one subset explains this credit". Searching a truncated pool makes
  that a claim about a set we had already thrown members out of — the true batch might include a
  settlement the pruning discarded, and the tier would then post a confidently wrong unique answer.
  That is precisely the failure rule 5 exists to prevent, and it would be invisible in the metrics
  because it looks like a successful match.
- **Raise the cap.** Lost: the cap is not the real bound anyway. Twenty-five members still permits
  2^25 nodes, which is why the search also carries an explicit node budget.

**Consequences:** Pools are shrunk instead by a filter that costs nothing: a settlement whose net
exceeds the credit cannot be a member of any subset summing to it, because every amount is positive.
That is a proof rather than a heuristic, and it took `POOL_TOO_LARGE` on `adversarial` from 105
declines to 43 without discarding a single reachable solution. What remains declined is genuinely
dense — and under ADR-020 those credits still reach Tier 3 rather than being lost.

The cost is a departure from §6 as written, which the write-up must state rather than gloss. The
gain is that "bounded compute is itself an engineering signal" becomes true instead of decorative:
both the pool cap and the node budget now actually refuse work rather than quietly truncating it.

---

## ADR-022 — Meet-in-the-middle is cut, and the cut is measured rather than assumed

**Date:** 2026-08-26
**Status:** Accepted

**Context:** §6 specifies meet-in-the-middle, `O(2^(N/2))`, "for the general case", and §14.2 marks it
an optimisation rather than a prerequisite with a note to ship bounded search first. The two-day
calendar slip recorded in `CLAUDE.md` also nominated it as one of the two cuts absorbing that slip.
Cutting on schedule pressure alone would be a weak answer in an interview; cutting because the
measurement says it is unnecessary is a different claim entirely.

**Decision:** Tier 2 ships with branch-and-bound depth-first search and no meet-in-the-middle.

**Alternatives considered:**
- **Implement it anyway.** Lost: it buys nothing measurable at this scale and would consume the day
  the eval harness needs — and the harness is what turns every other number in the project from a
  count into a measurement.
- **Cut it silently.** Lost: a reviewer reading §6 will look for it. An unexplained absence reads as
  something that was forgotten; a measured absence reads as a decision.

**Consequences:** Measured on the adversarial fixture, warm, median of three runs:

| settlements | credits | Tier 2 wall clock |
|---|---|---|
| 250 | 165 | 27 ms |
| 500 | 320 | 104 ms |
| 1000 | 633 | 413 ms |

Roughly quadratic in batch size, driven by the number of credits times pool size rather than by the
exponent — which is the point: the prunes and the node budget already prevent the search from
reaching the regime meet-in-the-middle exists to rescue. The track brief specifies 50+ record
batches; this is four orders of magnitude of headroom above that.

The cost is that a genuinely pathological pool — many settlements, all small, a large target — would
exhaust the node budget and be declined as `POOL_TOO_LARGE` where meet-in-the-middle might have
resolved it. Under ADR-020 those credits still reach Tier 3, so the failure is a downgrade rather
than a loss. If day 8 shows `POOL_TOO_LARGE` carrying a meaningful share of the residual, this
decision is the one to revisit — and the measurement above is the baseline to beat.

---

## ADR-023 — Evaluation commands live in `eval/`, and the entrypoint composes both halves

**Date:** 2026-08-26
**Status:** Accepted

**Context:** `report` and `evaluate` were written into `ledgerloop/cli.py` alongside `generate` and
`reconcile`, which is where a CLI's commands normally go. The build failed immediately:

    FAILED tests/test_no_truth_leak.py::test_matcher_never_imports_eval_package[cli.py]

Both commands need ground truth to score against, so both import `eval`, and `cli.py` sits inside the
package that may never read truth. The guard has been enforcing that boundary since day 1 with
nothing on the other side of it. Day 8 gave it something to catch, and it caught it on the first try.

**Decision:** `report` and `evaluate` live in `eval/cli.py`, which imports the matcher's Typer app and
registers onto it. The published entrypoint becomes `eval.cli:main`.

**Alternatives considered:**
- **Add `cli.py` to `TRUTH_AUTHORING_DIRS`.** Refused outright. Widening the exemption is the one
  change the guard's own docstring forbids, and the exemption exists for the generator — which
  *writes* truth — not for anything that reads it.
- **Import `eval` lazily inside the function bodies.** Lost, and it is worth saying why it is worse
  than the honest version: it would pass the guard only because the guard walks import statements,
  while leaving the matcher package genuinely able to read ground truth at runtime. Defeating a
  correctness check by hiding from it is not a fix.
- **Move the whole CLI into `eval/`.** Lost: `generate` and `reconcile` have no business depending on
  the evaluator, and the matcher should stay runnable without it.

**Consequences:** The dependency now runs one way and only one way — `eval` knows about the matcher,
the matcher never learns an evaluator exists — so the capability to read truth cannot drift sideways
into the cascade by someone adding an import in the wrong file. The cost is a mild surprise in the
layout: the published `ledgerloop` script resolves to `eval.cli:main`, which reads oddly until you
know why, and §11's repository structure does not show it. That is worth stating in the write-up,
because a reviewer who notices it and is not told the reason will assume it is an accident.

The wider lesson is about the guard rather than the layout. It was written on day 1 to protect a
boundary that had no traffic across it for seven days, and the moment traffic appeared it caught a
real violation in the first thing built on top of it. That is the argument for writing structural
tests before the code they constrain.

---

## ADR-024 — Gate failures are tested with a scripted adapter, not a live model

**Date:** 2026-08-31
**Status:** Accepted

**Context:** §7.3 requires the membership gate to be *demoed live* by injecting a deliberately
malformed response. That is not something a real model can be asked to do. You cannot instruct a
model to fabricate a settlement identifier on cue, and if you could, the demonstration would prove
nothing about the gate — only that the model complied.

**Decision:** The adapter is a `Protocol` with a `ScriptedAdapter` implementation that returns
prepared responses in order. Every gate failure in the test suite is driven by a hand-written
response. No test makes a network call.

**Alternatives considered:**
- **Record real responses and replay them.** Lost: it captures the responses a model happened to give,
  which are overwhelmingly well-formed. The failure modes the gates exist for — a fabricated
  identifier, a confident proposal whose arithmetic is wrong — would have to be hand-edited into the
  recording anyway, at which point the recording is doing nothing.
- **Test against a live model.** Lost: non-deterministic, costs money per run, needs a key in CI, and
  cannot produce the one failure §7.3 asks to demonstrate.

**Consequences:** Every rejection path is covered, including ones a real model might never produce in
a thousand runs — a response naming two fabricated ids, a proposal at confidence 0.99 whose amounts
are three orders of magnitude out, a response that fails membership *and* arithmetic so the gate order
can be asserted. The scripted adapter is also what makes the live demo possible: hand it
`["STL_GHOST"]` on stage and the gate rejects, the counter increments, and the batch keeps running.

The cost is that these tests say nothing about whether a real model produces useful proposals. That
is a genuinely different question, it needs an API key, and it is answered by the ablation table
rather than by the suite. Nobody should read 32 passing tests as evidence that Tier 3 *works* — only
that it cannot be made to post something the gates should have caught.

---

## ADR-025 — `--no-llm` runs Tier 3 without an adapter rather than skipping it

**Date:** 2026-08-31
**Status:** Accepted

**Context:** §8 promises that when the model is unavailable the batch completes, the auto-match rate
falls, and correctness does not. There are two ways to implement that: skip Tier 3 entirely, or run it
with no adapter so every record it would have adjudicated becomes an exception.

**Decision:** Tier 3 runs. With no adapter and no cached response, each affected record raises
`MODEL_UNAVAILABLE` and the run is marked degraded.

**Alternatives considered:**
- **Skip the tier.** Lost: the residual would go silently unexamined. The run would look identical to
  a `--tiers 0,1,2` ablation arm, and the only difference — that fifty records *should* have been
  adjudicated and were not — would exist nowhere in the data.

**Consequences:** Degradation is visible where it matters, in the exception queue. On the adversarial
fixture a `--no-llm` run raises 50 `MODEL_UNAVAILABLE` rows carrying their value at risk, so a human
can see exactly what went unexamined and what it was worth. This is also what makes ADR-016's
`degraded` flag meaningful rather than decorative — the flag says the numbers are not comparable, and
the queue says why.

The cost is fifty queue rows that are not really the merchant's problem: they are our outage, filed
against their reconciliation. A run that degrades produces a noisier queue than one that succeeds,
and the reason code is the only thing distinguishing them.

---

## ADR-026 — The provider adapter ships unexercised, and the cached fixtures are a human's job

**Date:** 2026-08-31
**Status:** Accepted

**Context:** Tier 3 needs a real model to produce real numbers, and `cache.py` requires committed
fixture responses so CI can run Tier 3 without a key. Neither was possible today: the work was done
without API credentials, and sending a merchant's data to a model provider is not a step to take
without an explicit decision.

**Decision:** The `AnthropicAdapter` is written and documented as **exercised by no test**. The
committed cache fixture is deferred to a run performed by a human with a key.

**Alternatives considered:**
- **Ship no real adapter until it can be tested.** Lost: the provider-agnostic seam is itself an
  architectural claim (§10 — "no vendor lock-in"), and an interface with only a test double behind it
  does not demonstrate it.
- **Mark the adapter as tested because the interface is.** Refused. The `Protocol` is covered; the
  HTTP call is not, and saying otherwise in a project whose entire argument is honest measurement
  would be the wrong kind of inconsistency.

**Consequences:** The two rows of §9.2 that need Tier 3 — Full cascade and LLM-only baseline — remain
*not yet measured*, and the LLM-only control arm is the one that turns the cascade from an assertion
into a result. That is the single largest gap in the submission today, and it closes with one run:
generate the adversarial fixture, run the cascade with a key, commit the resulting cache directory,
and the ablation completes without a key thereafter.

Until then the honest statement is that Tier 3's *safety* is thoroughly tested and its *usefulness* is
not measured at all. Those are different claims and the write-up should not blur them.

---

## ADR-027 — Every unmatched credit is swept into the queue with a reason code

**Date:** 2026-09-01
**Status:** Accepted

**Context:** §6 says every unresolved record lands in the exception queue with a machine-readable
reason code. That was false from day 4 until today. Exceptions only existed where some tier had
actively objected — an ambiguous subset, an oversized pool, a duplicate. A credit that simply reached
the end unmatched left **no trace at all**: counted in the residual, present in no exception row,
invisible to anyone reading the queue. On the adversarial fixture at 250 records that was 56 credits;
on the T0-only ablation arm it was 143.

The exception distribution in `results/metrics.md` therefore reported one exception against 143
unexplained credits, which is not a small inaccuracy — it is the report describing a different run
from the one that happened.

**Decision:** After every tier has run, the orchestrator sweeps the residual and gives each remaining
credit a reason code. `ORPHAN_CREDIT` when no settlement fell in its window at all; `NO_CANDIDATE`
when settlements were there and none reconciled.

**Alternatives considered:**
- **Leave the residual as a count.** Lost: "unmatched: 56" tells an associate how much work remains
  and nothing about what any of it is. §6 asks for a reason code and a suggested action precisely
  because a queue without them is a number, not a queue.
- **Raise an exception per unmatched settlement as well as per credit.** Lost: most unmatched
  settlements on the adversarial fixture are `refunded` or `disputed` and produced no credit on
  purpose. Filing them would flood the queue with non-problems, which is the opposite of the
  diagnostic instrument §8 describes.

**Consequences:** The distinction between the two codes is drawn only from evidence the matcher
actually holds, and that limit is worth stating: **the matcher cannot know a credit is an orphan.**
Only ground truth knows that. An empty candidate window is what an out-of-band transfer looks like
from the inside, and the code is an inference from absence rather than a claim of fact.

One correction was needed mid-implementation and it is instructive. The first version built the pool
from *unclaimed* settlements, so a credit whose candidates had been consumed by other credits was
labelled an orphan. That is wrong — the counterpart existed, we simply spent it elsewhere — and it
would have reported ordinary contention as an out-of-band transfer. The pool is now built over every
settlement.

The ablation's exception distribution changes as a result, and `results/metrics.md` was regenerated.
Auto-match rates and false-match rates are untouched; the sweep matches nothing. What changed is that
each arm now accounts for all 165 credits instead of 23 of them.

---

## ADR-028 — Approved rules reach into Tier 0, which changes what "deterministic" means

**Date:** 2026-09-01
**Status:** Accepted

**Context:** The rule promotion loop is only worth building if a promoted rule changes what the
cascade does on the next run — §9.3 asks for a measured before-and-after delta, and a loop that
cannot move a number is a UI flourish. That means the rule store has to be consulted by a tier. The
natural place for a learned instrument prefix is `tier0_exact.normalise_utr`, which is also the
function feeding the tier that posts at **confidence 1.0**.

**Decision:** `normalise_utr` takes an optional `RuleStore`, defaulting to empty. Approved prefixes
are appended to the built-in list and are subject to exactly the same `MIN_REFERENCE_LENGTH` floor.

**Alternatives considered:**
- **Apply rules only in Tier 1.** Safer — Tier 1 posts at 0.99 and corroborates with a fee-model
  recomputation — but a learned prefix genuinely belongs to normalisation, and duplicating the
  normaliser so one copy could learn would be worse than the risk it avoids.
- **Let rules bypass the length floor**, on the grounds that a human approved them. Refused. A rule
  approved once fires forever, on batches nobody reviewed. The floor exists because Tier 0 asserts
  certainty, and human approval of a *rule* is not human approval of every future *match* it makes.

**Consequences:** This is the point where a claim in the write-up has to become more careful. Tiers
0–2 are still deterministic, but they are deterministic **given a rule store** rather than
deterministic as fixed code. A run is still reproducible from the repository alone, because
`store.yaml` is committed — that is why it ships as `rules: []`, so a reviewer can diff it after a
demo and see exactly what was learned. But "the same code always produces the same matches" is no
longer the right sentence; "the same code and the same approved rules" is.

Two rule kinds ship, not twelve: an unrecognised instrument prefix and a counterparty spelling. §9.3
asks for a measured delta, not coverage, and two kinds that visibly work demonstrate the loop better
than a dozen that each fire once. The cost is that a resolution whose lesson is neither of those
yields no rule — correctly, since inventing one from a single example is how a store fills with
overfitted guesses.

---

## ADR-029 — The rule-promotion lift is zero, and the reason is architectural

**Date:** 2026-09-01
**Status:** Accepted

**Context:** §9.3 asks for the auto-match rate before and after promoting the rules generated by
resolving five exceptions, as "the evidence that the agentic loop does something rather than being a
UI flourish". The loop was built on day 10 and measured on day 11. **The delta is 0.00%.**

Getting to that number took three attempts, and each attempt is part of the finding.

The first measurement showed zero because no fixture contained a class a rule could repair. §5.5
defines `NARRATION_NOISE` as "UTR **prefixed**, truncated, case-varied, delimiter-varied", and the
generator rendered "prefixed" as a separate delimited field that the tokeniser splits off for free —
so the phenomenon never reached the matcher and the `NARRATION_PREFIX` rule kind had nothing to learn
from. That was a generator defect and it was fixed: prefixes are now glued to the token, as banks
actually write them.

It still measured zero. Every credit a prefix rule could rescue was already being rescued by Tier 1's
amount, date and name signals, so recovering the reference changed nothing.

So a `FEE_DRIFT` injector was added — one merchant whose real MDR sits 100 bps from our configured
rate — together with a `FEE_OVERRIDE` rule kind. This is the failure §8 describes and the only one in
the project that makes the system's own *model* wrong rather than corrupting how a fact was written
down. It still measures zero, and this time the reason is structural.

**Decision:** Report the zero, with the explanation, and change §9.3's assertion from "promotion
raises the match rate" to "promotion never lowers it".

**Why the lift is structurally zero.** Tier 1 is the only tier that *recomputes* the fee model. Tier 0
compares the credit against the settlement's reported net; Tier 2 sums reported nets. So when our
pricing model is wrong for a merchant, Tier 0 still matches, Tier 2 still matches, and the single tier
that declines has its work picked up by the tier beneath it. A wrong fee model is very nearly
unobservable in this cascade.

That is a **robustness property, not a bug**: the matcher does not depend on our model of the world
being right, because two of its three deterministic tiers reconcile against what the gateway actually
reported. It is also the honest reason §8's claim — "fee model wrong for a merchant shows up as an
exception cluster, and rule promotion fixes the whole class" — does not hold as written. The claim
assumes the fee model is load-bearing for matching. It is load-bearing for *prediction* only.

**Alternatives considered:**
- **Make Tier 1's recomputation authoritative** so a wrong fee model genuinely blocks matches. Lost:
  it is a cascade redesign three days before submission, and it would trade away the robustness above
  to manufacture a number.
- **Tune the fixture until a delta appears.** Refused. That is fitting the data to the claim, which is
  the one thing this project refuses everywhere else — and every measured figure in `results/metrics.md`
  would become suspect by association.
- **Report the loop without measuring it.** Refused. §9.3 exists precisely to stop that.

**Consequences:** The loop is real and fully exercised — five resolutions produce five approved rules,
the store persists them, the tiers consume them, a re-run applies them, and every step is tested. What
it does not do is raise the match rate on these fixtures, and the write-up must say so plainly rather
than describe the mechanism and let a reader infer a result.

The measurement was not wasted. It caught a genuine defect that would otherwise have shipped: an early
inference learned an **absolute rate** from one settlement and applied it across payment methods, so a
rate learned from a debit-card row (140 bps) was applied to the same merchant's UPI rows (should be
100). It promoted five rules and moved the adversarial fixture from 59.4% to **56.4%** — five human
approvals, and the system got worse. The inference now learns a *margin* rather than a rate, refuses
anything but a clean `captured` settlement, and bounds what it will believe.
`test_promotion_never_reduces_the_auto_match_rate` is what caught it, and is now the most valuable
test in that file.

For the pitch: "we built the loop, measured it, and the lift was zero — because the architecture is
insensitive to the error class the loop repairs" is a stronger answer than a manufactured delta, and it
is the only one that stays true under questioning.

---

## ADR-030 — Rule approval is a CLI step, and the UI is cut

**Date:** 2026-09-01
**Status:** Accepted. Enacts §12's buffer policy.

**Context:** §12 scheduled a FastAPI backend and React UI for day 12, and §4.3's demo flow put two
steps inside it: the exception queue, and resolving an exception to approve a promoted rule. The build
started two days later than §12 assumes, and `api/main.py` carries the instruction in its own
docstring: "if day 12 slips, cut this and generate a static HTML report from the CLI instead."

**Decision:** The UI is cut. Rule approval happens through `ledgerloop resolve`, and the queue through
`ledgerloop exceptions`, both of which already exist.

**Alternatives considered:**
- **Build a minimal UI anyway.** Lost: it would consume the last clear day before the write-up, and the
  bar §12 states is "throughput, measured accuracy, and an honest exception list, not visual polish".
- **Cut approval along with the UI.** Refused. Approval is not a UI feature — it is the gate ADR-004
  names as the only thing standing between a single resolution and a rule that fires forever.

**Consequences:** The demo stays in one window. §4.3's steps 4 and 7 become `ledgerloop exceptions` and
`ledgerloop report`, and step 5 becomes `ledgerloop resolve`, which prints the proposed rule in prose
and does nothing until `--approve` is passed. On a five-minute recording that is arguably better than
switching to a browser mid-flow.

What is lost is the provenance drill-down: "click any matched row and see tier, rule, evidence and
source hashes" becomes a report rather than an interaction. The data is all there — `match_records`
carries every field — so this is a presentation cut, not a capability one. `api/main.py` stays in the
tree as a stub with its buffer-policy note intact, so the cut is visible in the repository rather than
silently absent.

---

## ADR-031 — The provider is Gemini, and the swap is the evidence for ADR-024

**Date:** 2026-09-02
**Status:** Accepted

**Context:** Tier 3 had never run against a real model, leaving the Full cascade and
LLM-only rows of §9.2 unmeasured — and the LLM-only row is the control arm that turns the
cascade from an assertion into a result. Closing that gap needed an API key. Anthropic's
API is paid, and paying for it was not an option, so the project moved to Google AI Studio's
free tier.

**Decision:** `GeminiAdapter` via the official `google-genai` SDK, pinned to
`gemini-3.5-flash-lite`. `AnthropicAdapter` stays in the tree as the second implementation
of the Protocol rather than being deleted.

**This is the first real test of ADR-024, and it passed.** ADR-024 claimed the
`LLMAdapter` Protocol made the provider a config change rather than a refactor. Changing
providers touched exactly one file. The tiers did not move. The gates did not move. The
cache did not move. The orchestrator did not move. `tier3_llm.py` did not move. "No vendor
lock-in" stopped being a design intention and became an observed property, which is a much
better answer under questioning than an architecture diagram.

**Alternatives considered:**
- **Keep Anthropic and skip the measurement.** Lost: it leaves the control arm unmeasured,
  and §9.2 exists precisely to stop the cascade being an opinion.
- **Delete `AnthropicAdapter` now that it is unused.** Lost: two implementations of one
  Protocol is the evidence for the claim above. One implementation proves nothing.
- **Abstract over both providers with a framework.** Refused — see the exclusion list. The
  entire LLM surface is one schema-constrained call; a framework here would undercut the
  project's own argument.

**Consequences.** Four things were found by doing this rather than by reasoning about it:

1. **`gemini-2.5-flash-lite` is retired for new keys** and returns a 404 naming its
   successor. The originally planned pin would have killed the sweep on request one. Found
   by making a single two-token validation call before committing to 677 of them.
2. **`temperature` had to move, not stay.** Anthropic removed sampling parameters on
   current models and rejects them with a 400, so the `TEMPERATURE = 0.0` in the old
   adapter was already broken by inspection. Gemini accepts it, and `seed` too. Either way
   the determinism claim now rests where it always really rested: on the response cache.
   Temperature and seed only stabilise the *first* call on a new prompt, which is a cache
   miss by construction.
3. **The retry predicate was wrong, and it cost 58 calls to learn.** It recognised only
   429. The first real sweep died on a `ServerError: 503` — a transient outage that says
   nothing about the credit being adjudicated — because a 5xx propagated straight out of
   the adapter. Now `RETRYABLE_STATUS` covers 429 and 5xx, matched by status code rather
   than exception class so an SDK rename cannot silently reintroduce it. Every one of the
   58 answers survived in the cache, which is the first time that design was tested by an
   actual failure rather than by a test.
4. **`SweepInterruptedError` is now the base and `DailyQuotaExhaustedError` a subclass.**
   Reporting a provider outage as "daily quota exhausted" would have been a false statement
   in a generated metrics file. Both mean *stop and keep what you bought*; only one clears
   by waiting a minute.

The free tier's measured ceilings for this model are 15 RPM, 250K TPM and **500 requests
per day**, read off the AI Studio dashboard because the docs no longer publish them. A full
sweep needs 677, which is what forced ADR-032.

---

## ADR-032 — A model arm reports no number at all rather than a partial one

**Date:** 2026-09-02
**Status:** Accepted

**Context:** §9.2's two model arms need 677 requests across the three fixtures. The free
tier allows 500 a day. An interrupted sweep is therefore the normal case, not the
exceptional one — and that is before counting outages, one of which had already stopped a
run at 58 calls.

**Decision:** An arm that could not ask about every credit it needed to reports
*not yet measured*, exactly as it did while Tier 3 was unimplemented. No arm is ever scored
over the portion of a fixture that fit inside a quota window.

**Alternatives considered:**
- **Score what came back and footnote the shortfall.** This is the tempting one, and it is
  wrong. An auto-match rate over "the 312 credits we got to before the quota died" is not
  reproducible by anyone, depends on when the run started, and would be quoted without its
  footnote by the first person who read it. Rule 7 is as much about not implying a number
  as about not typing one.
- **Shrink the fixtures until a sweep fits in a day.** Refused: every arm must score the
  same fixture at the same seed or the table compares datasets rather than tiers.
- **Cut the `easy` LLM-only arm** (250 of the 677 calls, on the fixture the deterministic
  tiers already solve at 100%). Kept for now — omitting a cell because we assumed we knew
  its value is the exact habit this project is built against.

**Consequences:** Interruption is cheap. The cache is written per response, so answers
already paid for survive a crash and a resumed run re-buys nothing. That turns the
677-versus-500 problem into a scheduling detail: run the fixtures that carry the argument
first, run the rest tomorrow, then assemble the full table with a final pass that makes
**zero** calls. That last pass is also a live demonstration of §7.4.

`evaluate` grew a `--fixture` option for this reason. Running fixtures in file order would
spend half a day's quota on `easy` before reaching `adversarial`, and an interruption would
then cost the rows that matter most.

`--estimate-only` exists for the same reason: it reports what a sweep *would* send, per
arm, without sending it, so a call count is approved before quota is spent.

---

## ADR-033 — Report adjudications, not API calls

**Date:** 2026-09-02
**Status:** Accepted. Corrects a defect in the first published Tier 3 table.

**Context:** §9.2 uses model-call count as a headline comparison: the cascade should reach
a higher match rate *using fewer model calls* than the LLM-only baseline. The first real
adversarial run reported the full cascade making **3** calls against the baseline's 162.

That number was wrong in the flattering direction. A cold cache would have made 67. The 3
was an artefact of an earlier crashed sweep having already paid for 64 of them.

**Decision:** Report **adjudications** — how many credits the configuration put to the
model — as the §9.2 figure, and report **new API calls** separately as what this particular
run paid for.

**Why:** adjudications is a property of the architecture and is reproducible. New API calls
is a property of the response cache, varies with what happened to be on disk, and falls to
zero on a re-run. Publishing only the second understates model usage by whatever the cache
happened to hold, and the understatement always favours the cascade.

**Alternatives considered:**
- **Always run against a cold cache when publishing.** Lost: it would re-buy every answer
  on every publication, which the free tier makes impossible and which the committed cache
  exists to avoid.
- **Report only new calls and note the caveat in prose.** Lost for the same reason ADR-032
  rejects footnoted partial numbers: the caveat does not travel with the number.

**Consequences:** `RunMetrics` gains `cache_hits` and an `adjudications` property. The
report carries both columns and says in the table which is architectural and which is
bookkeeping.

Worth recording how close this came to shipping. "Full cascade: 3 calls versus LLM-only:
162" is a far better-sounding line than the truth, and it survived a green suite of 404
tests. It was caught only because the figure looked implausible against a call estimate
measured an hour earlier. ADR-018 and ADR-027 record defects that measurement caught; this
one adds an edge to the lesson. **The defects that survive longest are the ones whose
output you are pleased with.**

---

## ADR-034 — The gates reduce false matches; they do not eliminate them

**Date:** 2026-09-02
**Status:** Accepted

**Context:** Rule 2 says the LLM never overrides arithmetic, and every Tier 3 proposal is
re-verified in Python. It is easy to read that — and the project has come close to saying
it — as "a false match cannot get through Tier 3". The first measurement against a real
model says otherwise.

On `adversarial` at 250 records, the **LLM-only baseline posted one wrong match**:
precision 98.9%, false-match rate 1.1%. It passed the schema gate, the membership gate, the
arithmetic gate and the confidence threshold, and it was still wrong.

**Decision:** State the gates as risk-reducing rather than airtight, in the README and in
the pitch. The claim is "every proposal is re-verified against the fee model and the date
window, and the numbers win", which is true. The claim is *not* "verification makes a wrong
match impossible", which is not.

**Why a proposal can be wrong and still reconcile:** the arithmetic gate asks whether a
proposed settlement set explains the credit within tolerance. On a fixture containing
similar amounts on nearby dates, a *different* settlement set can also reconcile. The gate
catches proposals that do not add up. It cannot catch a proposal that adds up and is still
not what happened.

**Consequences.** The measured result makes the cascade's case more strongly than an
airtight claim would have:

| Configuration | Auto-match | Precision | False-match | Wrong |
|---|---|---|---|---|
| T0 + T1 + T2 | 59.4% | 100.0% | 0.0% | 0 |
| Full cascade | 67.9% | 100.0% | 0.0% | 0 |
| LLM-only baseline | 53.9% | 98.9% | 1.1% | 1 |

Same model, same fixture, same prompts, same gates. The full cascade matched 14 points more
and got none wrong; the baseline matched less and got one wrong. **The difference is not
the gates — both arms have them. The difference is how many questions the model was
asked.** In the full cascade the deterministic tiers claim everything they can first, so
the model sees only the residual; in the baseline it sees every credit. Every easy credit
the model might have fumbled was never put to it.

That is the actual argument for deterministic-first, and it is stronger than "our gates
catch everything": **the cheapest way to avoid a wrong answer from a model is not to ask it
the question.** Sections 7.3's gates then reduce what remains — visibly, in the same run:
19 `LLM_INVALID_OUTPUT` and 19 `AMOUNT_BEYOND_TOLERANCE` exceptions in the full cascade, 27
and 40 in the baseline, all of them responses the model produced and Python refused. Zero
hallucinated identifiers were seen in either arm, so the membership gate went unexercised
on this fixture — its coverage remains scripted (§7.3), which is the honest statement.

---

## ADR-035 — The cache is keyed on a model of record, so a keyless run reproduces Tier 3

**Date:** 2026-09-02
**Status:** Accepted. Fixes a defect that made ADR-026's central promise false.

**Context:** ADR-026 committed the Tier 3 response cache to the repository so that "CI runs
Tier 3 without an API key and without cost", and §7.4 promises a re-run performs zero calls
and produces a byte-identical match set. §12's bar for day 13 is *fresh clone → `make demo`
works*.

None of that was true. `tier3_llm.py` computed the cache key from `adapter.name`, falling
back to `""` when there was no adapter. A run without a key therefore looked up every
response under the empty string and **missed all 559 committed answers**. Measured on the
demo fixture: 4/10 prompts hit with the model name, **0/10 with no adapter**.

The failure is silent. §8 requires the batch to complete when no model is reachable, so
Tier 3 would raise `MODEL_UNAVAILABLE` per credit, the run would succeed, the totals would
look plausible, and the tier the whole pitch is about would have done nothing — on a
judge's laptop, during the demo.

**Decision:** `match_tier3` takes a `model_name` parameter, defaulting to the pinned model.
The rule is:

```python
model_name = adapter.name if adapter is not None else model_name
```

Whoever actually answered is the model of record; when nobody answered live, the configured
model is.

**Why not simply always use the configured name.** That was the first attempt and it broke
provenance: a scripted response recorded as having come from Gemini is a false statement in
the one table the entire audit argument rests on. `tests/test_tier3.py::
test_every_tier_three_match_names_the_model_and_prompt_version` caught it. The test was
right; the code changed. In production the two values agree anyway, because `build_adapter`
is handed the same pinned model.

**Alternatives considered:**
- **Drop the model from the cache key entirely.** Lost: the same prompt answered by a
  different model is a different answer, and serving one from the other's cache would make
  the provenance record's `model_name` a lie in a subtler way.
- **Run the demo with `--no-llm`.** Lost: it needs no code change but shows less. The whole
  point of committing the cache is that the full cascade is demonstrable for free.

**Consequences:** `reconcile` grew `--cache-dir` and `--model`, so `make demo` runs the full
cascade from the committed cache with no key. Verified end to end with the environment
variables cleared: 685 rows ingested, tiers 0-3 executed, **no `MODEL_UNAVAILABLE`**, and
`AMOUNT_BEYOND_TOLERANCE 2` / `LLM_INVALID_OUTPUT 2` in the exception spread — the
arithmetic and schema gates rejecting real cached model responses, visible in the demo
output.

CI now runs `make demo` on every push with no key configured, so the claim is checked
continuously rather than asserted once — and it earned its keep within a minute of existing.
The first CI run after this landed failed the drift check, because `run_ablation` returned
*not yet measured* the moment `adapter is None`, before trying the cache at all. A keyless
regeneration therefore blanked every model row the committed cache could perfectly well have
answered. The rule is now narrower and correct: **an arm is unmeasured when credits went
unanswered, not when a key was absent.** A `Full cascade` arm whose deterministic tiers leave
no residual needs no model and is fully measurable without one.

This is the fifth defect in this project that a green test suite did not see, and it has a
distinguishing feature worth naming: **it was invisible because the fallback path was
correct**. Graceful degradation did exactly what §8 asked, and in doing so it concealed that
the primary path never ran.

---

## ADR-036 — The README's results table is generated, and the build checks it

**Date:** 2026-09-02
**Status:** Accepted

**Context:** Rule 7 — *never hand-write a metric into README.md or results/* — was the only
one of the seven non-negotiable rules with no mechanical guard. Rule 6 has
`tests/test_no_truth_leak.py`, which has caught a real violation.

Intention lost. The README's Results table sat full of placeholder em-dashes for twelve
days, under a banner reading "PLACEHOLDER — do not fill by hand", while `results/metrics.md`
beside it filled with real measured figures. A reader skimming the repository would have
concluded the project had measured nothing.

**Decision:** `evaluate` renders the summary table, writes it to `results/summary.md`, and
splices it into README.md between `<!-- RESULTS:START -->` / `<!-- RESULTS:END -->` markers.
`tests/test_readme_results.py` asserts the two are byte-identical, and CI regenerates and
runs `git diff --exit-code`.

**Alternatives considered:**
- **Delete the table and link to `results/metrics.md`.** Honest, zero risk, and worse: §9.2
  is the centrepiece of the pitch and a judge should meet it on the page rather than one
  click away.
- **Fill it in by hand from the generated file, carefully.** Refused. "I copied carefully"
  is what everyone says before a transcription bug, and a hand-typed metric is
  indistinguishable from a generated one by eye — which is the entire reason rule 7 exists.

**Consequences:** editing either side by hand now fails the suite. `splice` raises rather
than repairing a README with no markers, because appending the table or silently doing
nothing both end with stale numbers whose staleness is invisible.

The ADRs remain the exception: figures quoted in `DECISIONS.md` and `CLAUDE.md` are
transcribed by hand, because prose about a measurement needs the number inline to make
sense. That is an accepted drift risk, and day 13 ran a verification pass — all fourteen
figures quoted across ADR-029, ADR-033 and ADR-034 and the two status blocks were confirmed
present in the generated output.

---

## ADR-037 — Tier 3 provenance was never written, and the test that should have caught it passed

**Date:** 2026-09-02
**Status:** Accepted. Fixes a defect present since Tier 3 landed on day 9.

**Context:** `CLAUDE.md` states the convention outright — every posted match writes a
`match_record` carrying tier, rule, evidence, source fingerprints, and *(tier 3 only)*
model name and prompt version. §7.4 needs the prompt version in particular, so that a
prompt change is visible in the trail rather than inferred from whatever the code says
later.

`Tier3Result` carried both fields. `record_match` accepted both as keyword arguments. The
orchestrator called `record_match(conn, run_id, match)` and passed **neither**. Every Tier
3 match in every run since day 9 recorded `model_name = NULL` and `prompt_version = NULL`.

**The test written on day 13 to prevent exactly this passed anyway.** It read:

```sql
SELECT model_name FROM match_records WHERE ... AND model_name IS NOT NULL
```

and then asserted `"" not in names`. With the column entirely NULL the filter returned an
empty set, and the assertion held over nothing at all. It was green for the same reason
the bug existed.

**Decision:** the orchestrator passes the model and prompt version for tier 3 and `None`
for tiers 0-2. The identity comes from `effective_model_name(adapter, configured)`,
extracted into one function so the cache key and the provenance record cannot drift apart
— they are the same question, *which model produced this answer*, and answering it in two
places is precisely how the cache became unreadable without an API key (ADR-035).

**The test now asserts the row count before the contents**, and separately asserts that
tiers 0-2 record *no* model, because claiming one would be worse than recording none.

**Consequences:** a Tier 3 match now reads `gemini-3.5-flash-lite · v1` in the audit trail.
Tiers 0-2 read nothing, which is itself the point — the provenance record is where rule 1
becomes checkable rather than stated.

Found while building the HTML report, because the provenance panel had nothing to show.
That is worth recording on its own: **the defect had survived 419 tests, CI, a clean-clone
check and a written convention, and what exposed it was trying to display the data to a
human.** A field nothing reads is a field nobody notices is empty.

**A filter that can empty the set is a filter that can hide the bug.** Assert the shape of
the data before asserting anything about its contents.

---

## ADR-038 — The report replays Tier 3 rather than summarising it

**Date:** 2026-09-02
**Status:** Accepted. Delivers §12's buffer-policy fallback.

**Context:** §12 cut the FastAPI + React UI and named the replacement outright — *a static
HTML report generated by the CLI*. The first draft of that report was a summary: metrics,
the cascade, the exception queue, a provenance table. It answered "what happened".

It did not answer the question the pitch actually turns on, which is **"how do you know the
model didn't just decide?"** A summary asks the viewer to take the gates on trust, which is
the opposite of the argument.

**Decision:** the report replays real Tier 3 adjudications end to end. For each case it
shows the candidate pool the model was given, the exact prompt, the raw text it returned,
and every gate running on that text in order, with the reason each gate gave.

**Replayed from the committed cache, never re-requested.** A live call during a recorded
demo is a risk with no upside: the cached answer *is* the answer, and both failure modes
have already happened on this project — a 503 killed a sweep 58 calls in, and a daily quota
ran out mid-fixture (ADR-031, ADR-032). The replay is byte-identical on every machine and
needs no API key.

**Alternatives considered:**
- **Call the model live in the demo.** Lost. Non-deterministic, rate-limited, and it fails
  in front of an audience rather than in CI.
- **Show the stored exception rather than re-running the gates.** Lost: a recorded outcome
  cannot show *which* gate stopped a proposal or what it said, and that is the entire
  content of the demonstration.
- **Summary only, as first drafted.** Lost for the reason above.

**What the real cases turned out to be** is better than anything that could have been
constructed:

* **BNK00128** — the model proposed two settlements and wrote in its own evidence
  `"5313950 paise (1587791 + 4132045)"`. Those addends sum to **5,719,836**. It asserted an
  arithmetic identity that is false, confidently, in the field meant to justify it. Schema
  passed, membership passed, and the arithmetic gate refused it: *"proposed settlements sum
  to 5719836 paise against a credit of 5313950; the numbers decide, not the model."* That is
  rule 2 catching a real model doing the exact thing the rule exists to prevent.
* **BNK00109** — the model began reasoning *inside* a JSON string field (`"Wait, checking
  sums... "`), overrunning the 200-character contract. Pydantic rejected the whole response
  at gate one. No retry, no repair.
* **BNK00137** — the clean path, all four checks green.

**One case is scripted, and the page says so.** §7.3 requires demonstrating that a
fabricated identifier discards the *whole* response, and across every measured arm and both
fixtures the model never fabricated one — zero hallucinated ids. So that case is
constructed, reusing a real prompt and a real candidate pool and scripting only the
response, and it carries a `scripted` label. Presenting it as a real rejection would be
exactly the kind of dishonesty the rest of the project is built to avoid.

**Consequences:** `ledgerloop/report/` holds a pure renderer — data in, string out, no
database, no filesystem, no ground truth. `eval/` scores the run and hands the numbers over,
because precision needs truth and `ledgerloop/` may never read it (rule 6). Nothing is
computed in the template: a renderer that derived a percentage could disagree with
`results/metrics.md`, and two sources for one number is how a wrong number ships (ADR-036).

The page loads **nothing** from the network — no CDN, no webfont, no script. The type is a
system stack rather than an embedded webfont, because a font that resolves on the machine
that built the page and fails on a locked-down network is invisible until the moment it is
being demonstrated.

`make demo` writes it, and CI regenerates it on every push with no key configured and
diffs the result, so "reproducible from the cache without credentials" is checked rather
than claimed.

**A test that measured the wrong thing, recorded because it nearly stuck.** The
self-containment check originally asserted `"https://" not in html`. On real data it would
have failed — Pydantic appends a documentation URL to its validation errors, and that
message is *displayed* as the schema gate's reason. A URL in text fetches nothing. Left
alone, the assertion would have pressured someone into stripping a genuine diagnostic to
satisfy a test that was measuring a substring instead of a behaviour. It now checks for
`<link>`, `<script src>`, `<img src>`, `@import` and `url(http...)`, and a second test pins
the distinction so nobody re-tightens it.
