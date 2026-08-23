"""Group exceptions by reason code and merchant.

TODO(day-10): this turns the queue from a to-do list into a diagnostic
instrument. Twelve exceptions sharing one code and one merchant is not twelve
problems — it is one wrong assumption, usually in the fee model.

Sort the queue by RUPEE VALUE AT RISK, never by row order. An associate with
twenty minutes should spend them on the large exception.
"""

from __future__ import annotations
