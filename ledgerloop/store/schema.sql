-- LedgerLoop store.
--
-- APPEND-ONLY BY DESIGN. Corrections are new rows that supersede old ones; there are
-- no UPDATEs to match or exception rows. Provenance depends on this — an UPDATE
-- destroys the audit trail that the whole submission is built around.
--
-- Every row carries run_id so that reconciliation runs are first-class, comparable
-- objects (the ablation harness diffs them).

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    fixture         TEXT NOT NULL,
    tiers_enabled   TEXT NOT NULL,   -- e.g. "0,1,2,3"
    degraded        INTEGER NOT NULL DEFAULT 0,  -- 1 when Tier 3 was unavailable
    config_json     TEXT NOT NULL,   -- the MatchConfig in force; report it with metrics
    notes           TEXT
);

-- ---------- source rows ----------
-- row_sha256 is the canonicalised fingerprint of the raw source row. It is what makes
-- re-ingesting the same file a no-op and what DUPLICATE_SUSPECTED keys off.

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id           TEXT NOT NULL,
    run_id               TEXT NOT NULL REFERENCES runs(run_id),
    row_sha256           TEXT NOT NULL,
    merchant_id          TEXT NOT NULL,
    customer_id          TEXT NOT NULL,
    customer_name        TEXT NOT NULL,
    invoice_amount_paise INTEGER NOT NULL,
    currency             TEXT NOT NULL DEFAULT 'INR',
    issue_date           TEXT NOT NULL,
    due_date             TEXT NOT NULL,
    status               TEXT NOT NULL,
    PRIMARY KEY (run_id, invoice_id)
);

CREATE TABLE IF NOT EXISTS settlements (
    settlement_id      TEXT NOT NULL,
    run_id             TEXT NOT NULL REFERENCES runs(run_id),
    row_sha256         TEXT NOT NULL,
    payment_id         TEXT NOT NULL,
    order_id           TEXT NOT NULL,
    invoice_ref        TEXT,
    customer_name      TEXT NOT NULL,
    method             TEXT NOT NULL,
    gross_amount_paise INTEGER NOT NULL,
    fee_paise          INTEGER NOT NULL,
    gst_on_fee_paise   INTEGER NOT NULL,
    tds_paise          INTEGER NOT NULL,
    net_amount_paise   INTEGER NOT NULL,
    captured_at        TEXT NOT NULL,
    settled_on         TEXT NOT NULL,
    utr                TEXT,
    status             TEXT NOT NULL,
    PRIMARY KEY (run_id, settlement_id)
);

CREATE TABLE IF NOT EXISTS bank_txns (
    bank_txn_id   TEXT NOT NULL,
    run_id        TEXT NOT NULL REFERENCES runs(run_id),
    row_sha256    TEXT NOT NULL,
    value_date    TEXT NOT NULL,
    narration     TEXT NOT NULL,   -- free text. There is deliberately no ref column.
    credit_paise  INTEGER NOT NULL,
    debit_paise   INTEGER NOT NULL,
    balance_paise INTEGER NOT NULL,
    PRIMARY KEY (run_id, bank_txn_id)
);

CREATE TABLE IF NOT EXISTS quarantine (
    quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL REFERENCES runs(run_id),
    source_file   TEXT NOT NULL,
    line_number   INTEGER NOT NULL,
    raw_row       TEXT NOT NULL,
    error         TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- ---------- decisions ----------

CREATE TABLE IF NOT EXISTS match_records (
    match_id            TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES runs(run_id),
    bank_txn_id         TEXT NOT NULL,
    settlement_ids_json TEXT NOT NULL,  -- array; N:1 batches have many
    tier                INTEGER NOT NULL CHECK (tier BETWEEN 0 AND 3),
    rule_id             TEXT,
    confidence          REAL NOT NULL,
    evidence_json       TEXT NOT NULL,
    source_fingerprints TEXT NOT NULL,  -- SHA-256 of every row that fed this decision
    model_name          TEXT,           -- tier 3 only
    prompt_version      TEXT,           -- tier 3 only; never edit a prompt in place
    operator            TEXT NOT NULL DEFAULT 'system',  -- system | human
    created_at          TEXT NOT NULL,
    superseded_by       TEXT REFERENCES match_records(match_id)
);

-- Idempotency lookups hit row_sha256 once per incoming row. Without these the check
-- is a full table scan per row, which is quadratic in batch size — survivable at 250
-- rows, not at the file sizes a reviewer might try.
CREATE INDEX IF NOT EXISTS idx_invoices_sha ON invoices(run_id, row_sha256);
CREATE INDEX IF NOT EXISTS idx_settlements_sha ON settlements(run_id, row_sha256);
CREATE INDEX IF NOT EXISTS idx_bank_txns_sha ON bank_txns(run_id, row_sha256);

CREATE INDEX IF NOT EXISTS idx_match_run_tier ON match_records(run_id, tier);
CREATE INDEX IF NOT EXISTS idx_match_bank ON match_records(run_id, bank_txn_id);

CREATE TABLE IF NOT EXISTS exceptions (
    exception_id     TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL REFERENCES runs(run_id),
    bank_txn_id      TEXT,
    settlement_id    TEXT,
    reason_code      TEXT NOT NULL,
    value_at_risk_paise INTEGER NOT NULL,  -- the queue sorts by this, not by row order
    detail_json      TEXT NOT NULL,        -- candidate explanations, gathered evidence
    created_at       TEXT NOT NULL,
    resolved_at      TEXT,
    resolved_by      TEXT,
    resolution_json  TEXT,
    promoted_rule_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_exc_run_code ON exceptions(run_id, reason_code);
CREATE INDEX IF NOT EXISTS idx_exc_value ON exceptions(run_id, value_at_risk_paise DESC);

CREATE TABLE IF NOT EXISTS rules (
    rule_id        TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL,
    origin_exception_id TEXT REFERENCES exceptions(exception_id),
    description    TEXT NOT NULL,   -- natural language, shown to the approver
    matcher_json   TEXT NOT NULL,   -- machine-readable form applied on the next run
    approved_by    TEXT,
    active         INTEGER NOT NULL DEFAULT 1
);

-- ---------- convenience view ----------

CREATE VIEW IF NOT EXISTS v_run_summary AS
SELECT
    r.run_id,
    r.fixture,
    r.degraded,
    (SELECT COUNT(*) FROM bank_txns b WHERE b.run_id = r.run_id)          AS bank_rows,
    (SELECT COUNT(*) FROM match_records m
       WHERE m.run_id = r.run_id AND m.superseded_by IS NULL)             AS matches,
    (SELECT COUNT(*) FROM match_records m
       WHERE m.run_id = r.run_id AND m.tier = 3
         AND m.superseded_by IS NULL)                                     AS llm_assisted_matches,
    (SELECT COUNT(*) FROM exceptions e
       WHERE e.run_id = r.run_id AND e.resolved_at IS NULL)               AS open_exceptions,
    (SELECT COALESCE(SUM(e.value_at_risk_paise), 0) FROM exceptions e
       WHERE e.run_id = r.run_id AND e.resolved_at IS NULL)               AS value_at_risk_paise
FROM runs r;
