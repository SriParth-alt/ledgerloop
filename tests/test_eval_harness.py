"""The harness: loading ground truth, and scoring a real run against it.

`eval/` is the only package permitted to read `truth_links.csv`. That boundary has been
enforced since day 1 by `tests/test_no_truth_leak.py` with nothing on the other side of
it; from today it has a legitimate consumer, which makes the guard more important rather
than less — it is now the only thing stopping the capability leaking sideways into the
matcher.

Scoring reads what was **persisted**, not what a function returned. A match that never
reached `match_records` did not happen, and the provenance trail is part of what is being
measured.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.harness import load_truth, score_run
from ledgerloop.cascade.orchestrator import reconcile
from ledgerloop.generate.synth import generate_fixture
from ledgerloop.ingest.loader import load_batch
from ledgerloop.store.db import connect, initialise, start_run

RUN_ID = "scored"


@pytest.fixture
def scored(tmp_path: Path):
    paths = generate_fixture(fixture="adversarial", settlements=60, seed=42, out_dir=tmp_path)
    with connect(tmp_path / "ll.db") as conn:
        initialise(conn)
        start_run(
            conn, run_id=RUN_ID, fixture="adversarial", tiers_enabled="", config_json="{}"
        )
        load_batch(
            conn,
            RUN_ID,
            invoices=paths["invoices"],
            settlements=paths["settlements"],
            bank_statement=paths["bank_statement"],
        )
        reconcile(conn, RUN_ID, tiers=frozenset({0, 1, 2}))
        yield conn, paths


# --- loading truth --------------------------------------------------------------


def test_truth_groups_batch_members_into_one_explanation(tmp_path: Path) -> None:
    """A three-member batch is three rows in the file and one explanation for one
    credit. Scoring against the rows rather than the sets would make a partially
    matched batch look partially correct."""
    paths = generate_fixture(fixture="adversarial", settlements=60, seed=42, out_dir=tmp_path)
    truth = load_truth(paths["truth"])

    assert truth.explanations
    assert any(len(members) > 1 for members in truth.explanations.values()), (
        "no batched credit in the fixture; this test would prove nothing"
    )


def test_orphan_credits_carry_an_empty_explanation(tmp_path: Path) -> None:
    """An orphan is not missing from truth — it is present, and explained by nothing.
    Absent would mean 'we do not know'; empty means 'we know there is nothing'."""
    paths = generate_fixture(fixture="adversarial", settlements=60, seed=42, out_dir=tmp_path)
    truth = load_truth(paths["truth"])

    assert any(members == frozenset() for members in truth.explanations.values())


def test_truth_records_the_chaos_that_produced_each_credit(tmp_path: Path) -> None:
    """Per-phenomenon attribution needs the tags, not just the links. §9.2 asks which
    injector each failure is attributable to."""
    paths = generate_fixture(fixture="adversarial", settlements=60, seed=42, out_dir=tmp_path)
    truth = load_truth(paths["truth"])

    tagged = [tags for tags in truth.chaos_tags.values() if tags]
    assert tagged, "no credit carries a chaos tag"


# --- scoring a real run ---------------------------------------------------------


def test_scoring_a_real_run_produces_every_headline_metric(scored) -> None:
    conn, paths = scored
    result = score_run(conn, RUN_ID, load_truth(paths["truth"]), seconds=0.1)

    assert result.credits_total > 0
    assert result.matches_posted > 0
    assert 0.0 <= result.precision <= 1.0
    assert 0.0 <= result.recall <= 1.0
    assert 0.0 <= result.false_match_rate <= 1.0
    assert result.precision + result.false_match_rate == 1.0


def test_scoring_reads_what_was_persisted_not_what_was_returned(scored) -> None:
    """A match that never reached the store did not happen."""
    from sqlalchemy import text

    conn, paths = scored
    posted = conn.execute(text("SELECT COUNT(*) FROM match_records")).scalar_one()
    result = score_run(conn, RUN_ID, load_truth(paths["truth"]), seconds=0.1)

    assert result.matches_posted == posted


def test_the_exception_distribution_survives_into_the_metrics(scored) -> None:
    conn, paths = scored
    result = score_run(conn, RUN_ID, load_truth(paths["truth"]), seconds=0.1)

    assert result.exceptions_by_code
    assert sum(result.exceptions_by_code.values()) > 0


def test_every_posted_match_is_judged_against_truth(scored) -> None:
    """No match may be silently excluded from scoring. Dropping the ones we cannot
    classify is how a false-match rate quietly becomes flattering."""
    conn, paths = scored
    result = score_run(conn, RUN_ID, load_truth(paths["truth"]), seconds=0.1)

    assert result.matches_correct + result.matches_incorrect == result.matches_posted


def test_a_deliberately_wrong_match_is_caught(scored) -> None:
    """The harness must be able to fail. Inject a match that ground truth contradicts
    and confirm the false-match rate moves — otherwise a scorer that returned a
    constant would pass every test above.
    """
    import json
    import uuid

    from sqlalchemy import text

    conn, paths = scored
    truth = load_truth(paths["truth"])
    before = score_run(conn, RUN_ID, truth, seconds=0.1)

    conn.execute(
        text(
            "INSERT INTO match_records (match_id, run_id, bank_txn_id, settlement_ids_json, "
            "tier, rule_id, confidence, evidence_json, source_fingerprints, operator, "
            "created_at) VALUES (:m, :r, 'BNKORPH001', :s, 0, 'INJECTED', 1.0, '[]', '[]', "
            "'system', '2026-08-26')"
        ),
        {"m": str(uuid.uuid4()), "r": RUN_ID, "s": json.dumps(["STL00001"])},
    )
    after = score_run(conn, RUN_ID, truth, seconds=0.1)

    assert after.matches_posted == before.matches_posted + 1
    assert after.matches_incorrect == before.matches_incorrect + 1
    assert after.false_match_rate > before.false_match_rate
