"""CSV parsing for the Prompt Volume uploader (FR-116). Uses Pandas per the spec toolset.

Reads flat demand exports and resolves their differing header names to a canonical schema:

  * SEO keyword exports (Semrush/Ahrefs): a ``keyword`` column of bare terms.
  * QUESTION / PROMPT exports (Profound, AlsoAsked, AnswerThePublic, Semrush "Questions"):
    a ``prompt``/``question`` column of full natural-language questions.

Either a keyword column OR a prompt column satisfies the upload; when both are present the
keyword drives taxonomy mapping / dedupe while the prompt carries the real question the
audience asks. Volume strings ("1,200", "1.2K") are coerced to ints. ALL Pandas usage is
isolated here so the rest of the pipeline stays dependency-light.
"""
from __future__ import annotations

import io
import re

import pandas as pd

# Canonical column -> accepted header aliases (compared lowercased + stripped).
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "query": ("keyword", "query", "search term", "term", "queries", "keywords", "seed keyword"),
    # The full natural-language question / AI prompt (Profound, AlsoAsked, AnswerThePublic,
    # Semrush "Questions" report). Distinct from the SEO keyword so we keep both when present.
    "prompt": (
        "prompt", "prompts", "question", "questions", "ai prompt", "conversation",
        "conversation topic", "natural language question", "nlq", "query text",
    ),
    "search_volume": (
        "search volume", "volume", "avg. search volume", "search_volume", "vol",
        "monthly searches", "avg. monthly searches", "avg monthly searches",
    ),
    "keyword_difficulty": ("keyword difficulty", "difficulty", "kd", "keyword_difficulty"),
    "cpc": ("cpc", "cpc (usd)", "cost per click"),
}

_NUM = re.compile(r"[0-9]*\.?[0-9]+")


class CsvValidationError(ValueError):
    """Raised when the uploaded file can't be parsed into the canonical schema."""


def _resolve_columns(columns: list) -> dict[str, str]:
    lookup = {str(c).strip().lower(): c for c in columns}
    resolved: dict[str, str] = {}
    for canonical, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lookup:
                resolved[canonical] = lookup[alias]
                break
    return resolved


def resolve_text_column(columns) -> tuple[str | None, str | None]:
    """Locate the free-text column to import as questions, ignoring volume/metrics.

    Returns ``(column_name, kind)`` where *kind* is ``"prompt"`` (a real question / AI-prompt
    column such as a Profound / AlsoAsked / "People Also Ask" export — preferred) or
    ``"query"`` (a bare SEO keyword column), or ``(None, None)`` when neither is present.
    Unlike :func:`read_csv`, this does NOT require a search-volume column, so prompt exports
    that carry no demand metric can still be imported straight into the Question Bank.
    """
    resolved = _resolve_columns(list(columns))
    if "prompt" in resolved:
        return resolved["prompt"], "prompt"
    if "query" in resolved:
        return resolved["query"], "query"
    return None, None


def parse_volume(raw) -> int | None:
    """Coerce a messy volume cell to a non-negative int, or None when unparseable/invalid.

    Handles "1,200", "1.2K", "3.4M", "<10", blanks. Negative values -> None (invalid).
    """
    if raw is None:
        return None
    s = str(raw).strip().lower().replace(",", "").replace("<", "").replace(">", "")
    if not s or s in ("n/a", "na", "-", "—"):
        return None
    if s.startswith("-"):
        return None
    mult = 1
    if s.endswith("k"):
        mult, s = 1000, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000, s[:-1]
    m = _NUM.search(s)
    if not m:
        return None
    try:
        return int(round(float(m.group()) * mult))
    except (ValueError, OverflowError):
        return None


def _parse_float(raw) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace("$", "")
    m = _NUM.search(s)
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None


def read_csv(content: bytes) -> tuple[pd.DataFrame, list[dict], bool]:
    """Parse raw CSV bytes into ``(raw_df, rows, volume_present)``.

    ``raw_df`` keeps every original cell (str) so the PII linter can scan the WHOLE file;
    ``rows`` is the coerced canonical data ready for ingestion; ``volume_present`` is True when
    the file carried a search-volume column.

    A text column (keyword/query OR prompt/question) is REQUIRED. A search-volume column is
    OPTIONAL: a bare prompt log (Profound / AlsoAsked) with no volume is accepted, and each
    row's ``search_volume`` is left ``None`` — the engine then derives demand from how often
    each prompt recurs. Raises CsvValidationError for empty/malformed files or a missing text
    column.
    """
    try:
        df = pd.read_csv(
            io.BytesIO(content), dtype=str, keep_default_na=False, encoding="utf-8-sig"
        )
    except Exception as e:  # noqa: BLE001 — surface a clean 4xx to the caller
        raise CsvValidationError(f"Could not parse CSV: {e}") from e

    if df.empty or len(df.columns) == 0:
        raise CsvValidationError("The uploaded CSV has no data rows.")

    resolved = _resolve_columns(list(df.columns))
    has_text = "query" in resolved or "prompt" in resolved
    if not has_text:
        raise CsvValidationError(
            "CSV must contain a keyword/query OR a prompt/question column (for example, a "
            "keyword export from an SEO tool such as Semrush, or a question/prompt export "
            "from a 'people also ask' or AI-prompt research tool). A search-volume column is "
            "optional — without one, demand is estimated from how often each prompt recurs."
        )
    volume_present = "search_volume" in resolved

    rows: list[dict] = []
    for _, r in df.iterrows():
        # The prompt is the full natural-language question when present; the keyword is the
        # term used for taxonomy mapping + dedupe. If only one column exists it fills both.
        prompt = str(r[resolved["prompt"]]).strip() if "prompt" in resolved else ""
        query = str(r[resolved["query"]]).strip() if "query" in resolved else ""
        if not query:
            query = prompt  # prompt-only export (AlsoAsked / AnswerThePublic / Profound)
        if not query:
            continue
        rows.append({
            "query_text": query,
            "prompt_text": prompt or None,
            # None when the file has no volume column -> engine derives it from recurrence.
            "search_volume": parse_volume(r[resolved["search_volume"]]) if volume_present else None,
            "keyword_difficulty": (
                _parse_float(r[resolved["keyword_difficulty"]])
                if "keyword_difficulty" in resolved else None
            ),
            "cpc": _parse_float(r[resolved["cpc"]]) if "cpc" in resolved else None,
        })

    if not rows:
        raise CsvValidationError("No valid keyword/prompt rows found in the CSV.")
    return df, rows, volume_present
