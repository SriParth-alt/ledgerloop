"""Row schemas for the three input files.

Pydantic guards both boundaries of this system: file ingest here, LLM output in
``ledgerloop/llm/contract.py``. Same tool, both edges.

A row that fails validation is quarantined with its raw text and the error. The batch
continues. Losing one malformed row must never cost you the other 249.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from ledgerloop.generate.fee_model import PaymentMethod


class InvoiceRow(BaseModel):
    """Source A — the internal ledger. What the business thinks it is owed."""

    invoice_id: str
    merchant_id: str
    customer_id: str
    customer_name: str
    invoice_amount_paise: int = Field(gt=0)
    currency: str = "INR"
    issue_date: date
    due_date: date
    status: str


class SettlementRow(BaseModel):
    """Source B — the gateway payout report. Structured, with a real UTR field."""

    settlement_id: str
    payment_id: str
    order_id: str
    invoice_ref: str | None
    customer_name: str
    method: PaymentMethod
    gross_amount_paise: int = Field(gt=0)
    fee_paise: int = Field(ge=0)
    gst_on_fee_paise: int = Field(ge=0)
    tds_paise: int = Field(ge=0)
    net_amount_paise: int
    captured_at: date
    settled_on: date
    utr: str | None
    status: str


class BankRow(BaseModel):
    """Source C — the bank statement.

    Note what is absent: there is no reference column. The UTR, if it survived at
    all, is somewhere inside ``narration`` as free text. That absence is the whole
    problem this project exists to solve — do not add a structured ref field to make
    the generator easier.
    """

    bank_txn_id: str
    value_date: date
    narration: str
    credit_paise: int = Field(ge=0)
    debit_paise: int = Field(ge=0)
    balance_paise: int
