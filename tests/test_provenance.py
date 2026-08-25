"""Provenance records for posted matches.

The question this has to answer, months later and in front of an auditor, is: **did a
model touch this rupee?** A provenance record that cannot answer that is decoration.

So the tier-3 columns are tested from the negative side too — a deterministic match
must leave `model_name` and `prompt_version` NULL, not empty strings, because a query
looking for model-assisted matches would silently miss an empty string.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import text

from ledgerloop.audit.provenance import (
    MatchEvidence,
    ProposedMatch,
    matches_for_run,
    record_match,
)
from ledgerloop.store.db import connect, initialise, start_run

RUN_ID = "prov-run"


def _match(**overrides: object) -> ProposedMatch:
    defaults: dict[str, object] = {
        "bank_txn_id": "BNK1",
        "settlement_ids": ("STL1",),
        "tier": 0,
        "rule_id": "T0-UTR-EXACT",
        "confidence": 1.0,
        "evidence": (
            MatchEvidence(
                field="narration_token",
                bank_value="RZRPY1234567",
                settlement_value="RZRPY1234567",
                note="exact normalised reference",
            ),
        ),
    }
    defaults.update(overrides)
    return ProposedMatch(**defaults)  # type: ignore[arg-type]


def _seed(conn) -> None:
    """Two source rows with known fingerprints, so provenance has something to cite."""
    start_run(conn, run_id=RUN_ID, fixture="easy", tiers_enabled="0", config_json="{}")
    conn.execute(
        text(
            "INSERT INTO bank_txns (bank_txn_id, run_id, row_sha256, value_date, narration, "
            "credit_paise, debit_paise, balance_paise) "
            "VALUES ('BNK1', :run, 'bankhash', '2026-08-10', 'N', 10000, 0, 10000)"
        ),
        {"run": RUN_ID},
    )
    conn.execute(
        text(
            "INSERT INTO settlements (settlement_id, run_id, row_sha256, payment_id, order_id, "
            "invoice_ref, customer_name, method, gross_amount_paise, fee_paise, gst_on_fee_paise, "
            "tds_paise, net_amount_paise, captured_at, settled_on, utr, status) "
            "VALUES ('STL1', :run, 'stlhash', 'P', 'O', 'I', 'ACME', 'upi', 10000, 0, 0, 0, "
            "10000, '2026-08-08', '2026-08-10', 'RZRPY1234567', 'captured')"
        ),
        {"run": RUN_ID},
    )


def test_recorded_match_carries_tier_rule_and_confidence(tmp_path: Path) -> None:
    with connect(tmp_path / "p.db") as conn:
        initialise(conn)
        _seed(conn)
        record_match(conn, RUN_ID, _match())
        row = conn.execute(
            text("SELECT tier, rule_id, confidence, operator FROM match_records")
        ).one()

    assert row.tier == 0
    assert row.rule_id == "T0-UTR-EXACT"
    assert row.confidence == 1.0
    assert row.operator == "system"


def test_recorded_match_cites_every_source_row(tmp_path: Path) -> None:
    """`source_fingerprints` is what makes a match reproducible after the CSVs are
    gone. Citing only the bank row would leave the other half of the decision
    unaccounted for."""
    with connect(tmp_path / "p.db") as conn:
        initialise(conn)
        _seed(conn)
        record_match(conn, RUN_ID, _match())
        stored = conn.execute(text("SELECT source_fingerprints FROM match_records")).scalar_one()

    assert set(json.loads(stored)) == {"bankhash", "stlhash"}


def test_deterministic_match_leaves_the_model_columns_null(tmp_path: Path) -> None:
    """"Did a model touch this rupee?" must be answerable with a WHERE clause. An
    empty string would pass `IS NOT NULL` and quietly corrupt that answer."""
    with connect(tmp_path / "p.db") as conn:
        initialise(conn)
        _seed(conn)
        record_match(conn, RUN_ID, _match())
        row = conn.execute(
            text("SELECT model_name, prompt_version FROM match_records")
        ).one()

    assert row.model_name is None
    assert row.prompt_version is None


def test_model_assisted_match_records_the_model_and_prompt_version(tmp_path: Path) -> None:
    """§7.4: a prompt change must be visible in the trail, so the version is stored per
    match rather than looked up from whatever the code says today."""
    with connect(tmp_path / "p.db") as conn:
        initialise(conn)
        _seed(conn)
        record_match(
            conn,
            RUN_ID,
            _match(tier=3, rule_id="T3-ADJUDICATED", confidence=0.91),
            model_name="gemini-flash",
            prompt_version="v1",
        )
        row = conn.execute(
            text("SELECT tier, model_name, prompt_version FROM match_records")
        ).one()

    assert row.tier == 3
    assert row.model_name == "gemini-flash"
    assert row.prompt_version == "v1"


def test_evidence_round_trips(tmp_path: Path) -> None:
    """Evidence is what a human reads when overturning a match. Losing its structure
    on the way to storage would leave them a blob to squint at."""
    with connect(tmp_path / "p.db") as conn:
        initialise(conn)
        _seed(conn)
        record_match(conn, RUN_ID, _match())
        posted = matches_for_run(conn, RUN_ID)

    assert len(posted) == 1
    assert posted[0].settlement_ids == ("STL1",)
    assert posted[0].evidence[0].field == "narration_token"
    assert posted[0].evidence[0].bank_value == "RZRPY1234567"


def test_batched_match_records_every_settlement(tmp_path: Path) -> None:
    """N:1 batches are the reason `settlement_ids_json` is an array. A record naming
    only the first member would understate what the credit actually covers."""
    with connect(tmp_path / "p.db") as conn:
        initialise(conn)
        _seed(conn)
        conn.execute(
            text(
                "INSERT INTO settlements (settlement_id, run_id, row_sha256, payment_id, "
                "order_id, invoice_ref, customer_name, method, gross_amount_paise, fee_paise, "
                "gst_on_fee_paise, tds_paise, net_amount_paise, captured_at, settled_on, utr, "
                "status) VALUES ('STL2', :run, 'stlhash2', 'P', 'O', 'I', 'ACME', 'upi', 5000, "
                "0, 0, 0, 5000, '2026-08-08', '2026-08-10', 'RZRPY9', 'captured')"
            ),
            {"run": RUN_ID},
        )
        record_match(conn, RUN_ID, _match(settlement_ids=("STL1", "STL2"), tier=2))
        posted = matches_for_run(conn, RUN_ID)
        stored = conn.execute(text("SELECT source_fingerprints FROM match_records")).scalar_one()

    assert posted[0].settlement_ids == ("STL1", "STL2")
    assert set(json.loads(stored)) == {"bankhash", "stlhash", "stlhash2"}
