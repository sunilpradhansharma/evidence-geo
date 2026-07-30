"""Generate a Google Pinpoint-ready document corpus from the Response Repository.

Pinpoint ingests plain text and runs OCR/entity-extraction/full-text search over it, so we emit
one .txt per response with a metadata header it can index (persona, brand, model, sentiment,
date, themes, sources), plus a manifest.csv and collection_metadata.json, and bundle the whole
folder into a single .zip for one-shot bulk upload into a collection.
"""
import csv
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from app.config.settings import PROJECT_ROOT, get_settings

_EXPORT_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def export_base_dir() -> Path:
    settings = get_settings()
    base = settings.export_dir.strip() if getattr(settings, "export_dir", "") else ""
    root = Path(base) if base else (PROJECT_ROOT / "exports")
    return (root / "pinpoint").resolve()


def _safe(value: str, fallback: str = "x") -> str:
    cleaned = _SAFE.sub("_", (value or "").strip()) or fallback
    return cleaned[:48]


def _fmt_ts(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _sentiment_label(score) -> str:
    if score is None:
        return "unscored"
    if score > 0.2:
        return "positive"
    if score < -0.2:
        return "negative"
    return "neutral"


def _document(item: dict, theme_labels: list[str]) -> str:
    lines: list[str] = []
    title = f"{item.get('persona', '')} · {item.get('brand_focus', '')} · {item.get('llm_name', '')}"
    lines.append(f"Title: {title}")
    lines.append(f"Response ID: {item.get('response_id', '')}")
    lines.append(f"Run ID: {item.get('run_id', '')}")
    lines.append(f"Date (UTC): {_fmt_ts(item.get('timestamp_utc'))}")
    lines.append(f"Model: {item.get('llm_name', '')} ({item.get('llm_model_version') or 'n/a'})")
    lines.append(f"Persona: {item.get('persona', '')}")
    lines.append(f"Therapeutic Area: {item.get('therapeutic_area', '')}")
    lines.append(f"Brand Focus: {item.get('brand_focus', '')}")
    lines.append(f"Domain: {item.get('domain', '')}")
    lines.append(f"Intent: {item.get('intent_type') or 'n/a'}")
    lines.append(f"Consensus: {item.get('consensus_level') or 'n/a'}")
    sentiment = item.get("sentiment_score")
    lines.append(f"Sentiment: {sentiment if sentiment is not None else 'n/a'} ({_sentiment_label(sentiment)})")
    lines.append(f"Competitive Position: {item.get('competitive_position') or 'n/a'}")
    lines.append(f"Status: {item.get('status', '')}")
    if theme_labels:
        lines.append(f"Themes: {', '.join(theme_labels)}")

    claims = item.get("key_claims") or []
    if claims:
        lines.append("Key Claims:")
        for c in claims:
            lines.append(f"  - {c}")

    sources = item.get("sources") or []
    if sources:
        lines.append("Sources:")
        for s in sources:
            title_s = s.get("title") or s.get("domain") or ""
            url = s.get("url") or s.get("redirect_url") or ""
            lines.append(f"  - {title_s} — {url}".rstrip(" —"))

    queries = item.get("search_queries") or []
    if queries:
        lines.append("Search Queries: " + "; ".join(str(q) for q in queries))

    lines.append("")
    lines.append("=== QUESTION ===")
    lines.append(item.get("question_text") or "")
    lines.append("")
    lines.append("=== RESPONSE ===")
    lines.append(item.get("response_text") or "")
    return "\n".join(lines)


_MANIFEST_FIELDS = [
    "filename", "response_id", "run_id", "timestamp_utc", "llm_name", "persona",
    "therapeutic_area", "brand_focus", "domain", "intent_type", "consensus_level",
    "status", "sentiment_score", "sentiment_label", "competitive_position", "themes",
    "source_count", "question_text",
]


def build_export(
    items: list[dict],
    *,
    themes_map: dict[str, list[str]] | None = None,
    label: str = "",
    filters: dict | None = None,
) -> dict:
    """Write the corpus + manifest + metadata + zip. Returns a summary dict."""
    themes_map = themes_map or {}
    export_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = export_base_dir()
    out_dir = base / export_id
    docs_dir = out_dir / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)

    manifest_rows: list[dict] = []
    by_persona: dict[str, int] = {}
    by_brand: dict[str, int] = {}
    by_llm: dict[str, int] = {}
    sentiment_buckets = {"positive": 0, "neutral": 0, "negative": 0, "unscored": 0}
    timestamps: list[str] = []

    used_names: set[str] = set()
    for item in items:
        rid = item.get("response_id", "")
        theme_labels = themes_map.get(rid, [])
        fname = (
            f"{_safe(item.get('persona', ''), 'persona')}__"
            f"{_safe(item.get('brand_focus', ''), 'brand')}__"
            f"{_safe(item.get('llm_name', ''), 'model')}__{rid[:8] or 'x'}.txt"
        )
        # de-duplicate filename collisions
        if fname in used_names:
            fname = f"{fname[:-4]}_{len(used_names)}.txt"
        used_names.add(fname)

        (docs_dir / fname).write_text(_document(item, theme_labels), encoding="utf-8")

        sentiment = item.get("sentiment_score")
        slabel = _sentiment_label(sentiment)
        sentiment_buckets[slabel] = sentiment_buckets.get(slabel, 0) + 1
        persona = item.get("persona") or "?"
        brand = item.get("brand_focus") or "?"
        llm = item.get("llm_name") or "?"
        by_persona[persona] = by_persona.get(persona, 0) + 1
        by_brand[brand] = by_brand.get(brand, 0) + 1
        by_llm[llm] = by_llm.get(llm, 0) + 1
        if item.get("timestamp_utc"):
            timestamps.append(_fmt_ts(item.get("timestamp_utc")))

        manifest_rows.append({
            "filename": f"documents/{fname}",
            "response_id": rid,
            "run_id": item.get("run_id", ""),
            "timestamp_utc": _fmt_ts(item.get("timestamp_utc")),
            "llm_name": llm,
            "persona": persona,
            "therapeutic_area": item.get("therapeutic_area", ""),
            "brand_focus": brand,
            "domain": item.get("domain", ""),
            "intent_type": item.get("intent_type") or "",
            "consensus_level": item.get("consensus_level") or "",
            "status": item.get("status", ""),
            "sentiment_score": sentiment if sentiment is not None else "",
            "sentiment_label": slabel,
            "competitive_position": item.get("competitive_position") or "",
            "themes": "; ".join(theme_labels),
            "source_count": len(item.get("sources") or []),
            "question_text": (item.get("question_text") or "").replace("\n", " "),
        })

    # manifest.csv
    with open(out_dir / "manifest.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_MANIFEST_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(manifest_rows)

    summary = {
        "export_id": export_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "filters": filters or {},
        "document_count": len(manifest_rows),
        "by_persona": by_persona,
        "by_brand": by_brand,
        "by_llm": by_llm,
        "sentiment_buckets": sentiment_buckets,
        "date_range": {
            "earliest": min(timestamps) if timestamps else None,
            "latest": max(timestamps) if timestamps else None,
        },
    }
    (out_dir / "collection_metadata.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )

    # zip the whole export folder for one-shot upload
    zip_path = shutil.make_archive(str(out_dir), "zip", root_dir=str(out_dir))

    summary["dir"] = str(out_dir)
    summary["zip_path"] = zip_path
    summary["zip_bytes"] = Path(zip_path).stat().st_size if Path(zip_path).exists() else 0
    return summary


def list_exports() -> list[dict]:
    base = export_base_dir()
    if not base.exists():
        return []
    out: list[dict] = []
    for child in sorted(base.iterdir(), reverse=True):
        if not (child.is_dir() and _EXPORT_ID_RE.match(child.name)):
            continue
        meta_path = child / "collection_metadata.json"
        zip_path = base / f"{child.name}.zip"
        meta: dict = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                meta = {}
        out.append({
            "export_id": child.name,
            "generated_at": meta.get("generated_at"),
            "label": meta.get("label", ""),
            "document_count": meta.get("document_count", 0),
            "has_zip": zip_path.exists(),
            "zip_bytes": zip_path.stat().st_size if zip_path.exists() else 0,
        })
    return out


def zip_path_for(export_id: str) -> Path | None:
    if not _EXPORT_ID_RE.match(export_id):
        return None
    candidate = export_base_dir() / f"{export_id}.zip"
    return candidate if candidate.exists() else None
