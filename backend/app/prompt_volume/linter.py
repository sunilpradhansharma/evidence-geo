"""Pre-flight PII linting for Prompt Volume uploads (FR-116.5).

Scans EVERY non-empty raw cell BEFORE anything is written to the database. Any hit rejects
the ENTIRE upload (not per-row skipping, unlike the legacy /questions/import-csv). Uses the
central ``compliance.phi`` detector (email/phone/dashed-SSN/MRN/IP/dates/...), plus an
undashed 9-digit-SSN rule (phi only catches the dashed form) applied to every column EXCEPT
known numeric-metric columns — so a 9-digit search volume is never misread as an SSN.
"""
from __future__ import annotations

import re

import pandas as pd

from app.compliance import phi

# Header names whose values are numeric metrics — exempt from the undashed-SSN numeric rule.
# (Direct-identifier regexes like email/phone still run on these columns.)
_NUMERIC_HEADER = re.compile(
    r"(?i)\b(volume|search|cpc|kd|difficulty|clicks|traffic|position|competition|results|vol)\b"
)
_UNDASHED_SSN = re.compile(r"\b\d{9}\b")


class PiiRejection(Exception):
    """Raised when the pre-flight scan finds PII — the whole upload is rejected."""

    def __init__(self, hits: list[dict]):
        self.hits = hits
        categories = sorted({c for h in hits for c in h["categories"]})
        super().__init__(
            f"Upload rejected: PII detected in {len(hits)} cell(s) "
            f"({', '.join(categories)}). No data was stored."
        )


def lint(df: pd.DataFrame) -> None:
    """Raise :class:`PiiRejection` if ANY cell contains PII. No-op when the file is clean."""
    hits: list[dict] = []
    numeric_cols = {c for c in df.columns if _NUMERIC_HEADER.search(str(c))}
    # +2: the header is row 1, so the first data row is row 2 (matches import-csv numbering).
    for pos, (_, row) in enumerate(df.iterrows(), start=2):
        for col in df.columns:
            value = str(row[col]).strip()
            if not value:
                continue
            categories = phi.scan(value)
            if col not in numeric_cols and _UNDASHED_SSN.search(value):
                categories = sorted(set(categories) | {"SSN"})
            if categories:
                hits.append({"row": pos, "column": str(col), "categories": categories})
    if hits:
        raise PiiRejection(hits)
