"""PII heuristic lint for CSV import (SE-001, BR-012).

Backward-compatible shim — the detection logic now lives in the central
``app.compliance.phi`` module (G2) so every inbound path shares one, stronger
detector. Existing callers keep importing ``scan_for_pii`` from here unchanged.
"""
from app.compliance import phi


def scan_for_pii(text: str) -> list[str]:
    """Return a list of PII/PHI category names found in the text. Empty list = clean.

    Delegates to the central regex detector (synchronous, network-free). For the
    optional NLP layer (AWS Comprehend Medical) use ``phi.scan_async``.
    """
    return phi.scan(text)
