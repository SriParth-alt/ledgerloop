"""Exception reason codes.

The track bar asks for "an honest exception list". This enum is that honesty made
machine-readable: every record the cascade declines to match carries one of these,
and the metrics report breaks exceptions down by code.

Never write a bare string at a call site. If a new situation arises that none of
these describe, add a member here with a docstring entry below — an unclassified
exception is indistinguishable from a bug.
"""

from __future__ import annotations

from enum import StrEnum


class ExceptionCode(StrEnum):
    NO_CANDIDATE = "NO_CANDIDATE"
    """Nothing in the settlement window plausibly explains this bank credit."""

    AMBIGUOUS_SUBSET = "AMBIGUOUS_SUBSET"
    """Two or more distinct subsets of settlements sum to this credit within
    tolerance. Both explanations are arithmetically perfect, so there is no
    principled tiebreak. By policy this is a human decision, never a guess."""

    AMOUNT_BEYOND_TOLERANCE = "AMOUNT_BEYOND_TOLERANCE"
    """The best candidate is outside the fee-model tolerance band. Often a signal
    that the fee model is wrong for this merchant rather than that the row is bad —
    check for clustering."""

    DATE_OUT_OF_WINDOW = "DATE_OUT_OF_WINDOW"
    """Amount matches but the settlement timing is implausible."""

    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    """The Tier 3 adjudicator proposed a match below the acceptance threshold."""

    ORPHAN_CREDIT = "ORPHAN_CREDIT"
    """A bank credit with no gateway counterpart at all — typically an out-of-band
    transfer, a refund reversal, or interest."""

    DUPLICATE_SUSPECTED = "DUPLICATE_SUSPECTED"
    """Row fingerprint collides with something already posted. The bank re-posted,
    or the same file was ingested twice."""

    LLM_INVALID_OUTPUT = "LLM_INVALID_OUTPUT"
    """The model returned malformed JSON, or referenced an identifier that was not
    in the candidate set it was given. The whole response is discarded. There is
    deliberately no retry loop here — retrying until the output parses is how you
    coax a guess out of a model that had nothing to say."""

    POOL_TOO_LARGE = "POOL_TOO_LARGE"
    """The subset-sum candidate pool exceeded the complexity cap after pruning.
    Declining is correct: bounded compute matters more than one extra match."""

    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    """Tier 3 could not run (API down, rate limited, or disabled). The run is marked
    degraded. Auto-match rate falls; correctness does not."""


#: Codes a human can plausibly clear from the UI without engineering help.
HUMAN_RESOLVABLE: frozenset[ExceptionCode] = frozenset(
    {
        ExceptionCode.AMBIGUOUS_SUBSET,
        ExceptionCode.NO_CANDIDATE,
        ExceptionCode.AMOUNT_BEYOND_TOLERANCE,
        ExceptionCode.DATE_OUT_OF_WINDOW,
        ExceptionCode.LOW_CONFIDENCE,
        ExceptionCode.ORPHAN_CREDIT,
    }
)

#: Codes that end a record's journey through the cascade.
#:
#: Only ambiguity is terminal, and it is terminal by *policy* rather than by capability:
#: section 7.5 forbids the model from resolving AMBIGUOUS_SUBSET, so letting such a
#: record reach Tier 3 would hand it exactly the decision reserved for a human.
#:
#: Everything else is a capability limit and must fall through. POOL_TOO_LARGE in
#: particular says only that a deterministic search declined on complexity grounds —
#: Tier 3 sees at most eight pre-filtered candidates and may well resolve it.
TERMINAL: frozenset[ExceptionCode] = frozenset({ExceptionCode.AMBIGUOUS_SUBSET})

#: Codes that indicate a system problem rather than a data problem. A run producing
#: many of these should be investigated before its metrics are trusted.
SYSTEMIC: frozenset[ExceptionCode] = frozenset(
    {
        ExceptionCode.LLM_INVALID_OUTPUT,
        ExceptionCode.POOL_TOO_LARGE,
        ExceptionCode.MODEL_UNAVAILABLE,
    }
)
