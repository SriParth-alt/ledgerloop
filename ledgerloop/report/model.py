"""What the HTML report renders. Data only — no queries, no truth, no formatting.

These types exist so the renderer can be a pure function. That is not tidiness: the
renderer shows precision and false-match rate, and computing those needs ground truth,
which `ledgerloop/` may never read (rule 6). Keeping the shapes here and the scoring in
`eval/` means the report can live beside the matcher without the matcher gaining a
capability it must not have.

`tests/test_no_truth_leak.py` enforces that boundary, and has caught a real violation.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RunHeader:
    """Which batch this is, and under what configuration."""

    run_id: str
    fixture: str
    records: int
    seed: int
    tiers: str
    rows_ingested: int
    rows_quarantined: int
    #: True when the run reached Tier 3 without an API key — every answer came from the
    #: committed cache. Stated on the page because it is the reproducibility claim.
    keyless: bool = True


@dataclass(frozen=True)
class TierStep:
    """What one tier resolved out of the residual it inherited."""

    tier: int
    credits: int
    settlements: int


@dataclass(frozen=True)
class EvidenceItem:
    field_name: str
    bank_value: str
    settlement_value: str
    note: str


@dataclass(frozen=True)
class ProvenanceRow:
    """One posted match, with everything needed to audit it.

    ``model_name`` and ``prompt_version`` are None for tiers 0-2 by construction — they
    never call a model. The page renders that absence as "arithmetic decided" rather than
    a blank, because rule 1 is easier to believe when the trail says it out loud.
    """

    bank_txn_id: str
    settlement_ids: tuple[str, ...]
    tier: int
    rule_id: str
    confidence: float
    evidence: list[EvidenceItem]
    fingerprints: tuple[str, ...]
    model_name: str | None
    prompt_version: str | None


@dataclass(frozen=True)
class ClusterRow:
    """A group of exceptions sharing a reason code, with what to do about them."""

    code: str
    count: int
    value_at_risk_paise: int
    diagnosis: str


@dataclass(frozen=True)
class CandidateRow:
    settlement_id: str
    customer_name: str
    gross_amount_paise: int


@dataclass(frozen=True)
class GateStage:
    """One gate's verdict on one proposal, with the reason it gave."""

    gate: str
    passed: bool
    detail: str = ""
    fabricated_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Adjudication:
    """One credit's journey through Tier 3, start to finish.

    The demo surface. Showing a verdict without the prompt and the raw response would ask
    a viewer to take the gates on trust, which is the opposite of the point.

    ``scripted`` marks a case no measured run produced. §7.3 requires demonstrating that a
    fabricated identifier discards the whole response, and across every arm and both
    fixtures the model never fabricated one — so that case is constructed, and the page
    labels it rather than implying a real model did it.
    """

    bank_txn_id: str
    credit_paise: int
    value_date: str
    narration: str
    candidates: list[CandidateRow]
    candidates_total: int
    prompt: str
    raw_response: str
    stages: list[GateStage]
    verdict: str
    scripted: bool = False


@dataclass(frozen=True)
class AblationRow:
    """One row of the §9.2 table, already scored."""

    label: str
    auto_match_rate: float
    precision: float
    false_match_rate: float
    adjudications: int
    matches_incorrect: int
    measured: bool = True


@dataclass(frozen=True)
class ReportData:
    """Everything the page shows. Assembled by the caller; the renderer computes nothing.

    ``metrics`` is optional because `reconcile` can run where ground truth is not
    available. The page then shows the cascade, the queue and the provenance — which need
    no truth — and says plainly that accuracy was not scored, rather than failing to
    generate.
    """

    run: RunHeader
    metrics: object | None
    tiers: list[TierStep]
    residual: int
    clusters: list[ClusterRow] = field(default_factory=list)
    provenance: list[ProvenanceRow] = field(default_factory=list)
    adjudications: list[Adjudication] = field(default_factory=list)
    ablation_rows: list[AblationRow] = field(default_factory=list)
