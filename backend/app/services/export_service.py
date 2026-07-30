"""Export query results to CSV / JSON (FR-305)."""
import csv
import io
import json

from app.config.labels import PRELAUNCH_LABEL

_EXPORT_FIELDS = [
    "response_id", "run_id", "timestamp_utc", "llm_name", "llm_model_version",
    "persona", "question_id", "question_text", "therapeutic_area", "brand_focus",
    "monitoring_mode", "competitor_focus",
    "domain", "status",
    "sentiment_score", "competitive_position", "alert_triggered",
    "response_text", "sources", "key_claims",
]


def _has_disease_state(items: list[dict]) -> bool:
    # FR-108a: exports that include ANY brand-less row must carry the pre-launch label.
    return any(i.get("monitoring_mode") == "DISEASE_STATE" for i in items)


def _format_sources(sources) -> str:
    if not isinstance(sources, list):
        return ""
    lines = []
    for s in sources:
        if not isinstance(s, dict):
            continue
        url = (s.get("url") or "").strip()
        title = (s.get("title") or "").strip()
        if url and title:
            lines.append(f"{title} - {url}")
        elif url:
            lines.append(url)
        elif title:
            lines.append(title)
    return "\n".join(f"{i}. {line}" for i, line in enumerate(lines, 1))


def _format_key_claims(claims) -> str:
    if not isinstance(claims, list):
        return ""
    lines = [str(c).strip() for c in claims if str(c).strip()]
    return "\n".join(f"{i}. {line}" for i, line in enumerate(lines, 1))


def to_csv(items: list[dict], *, include_designation: bool = False) -> str:
    buf = io.StringIO()
    # Prepend the mandated label as a comment line when landscape rows are present.
    if _has_disease_state(items):
        buf.write(f"# {PRELAUNCH_LABEL}\n")
    fields = list(_EXPORT_FIELDS)
    if include_designation:
        # Workshop Questions export: surface the Persona+indication designation
        # (e.g. "Patient RA") in its own column, immediately after persona.
        fields.insert(fields.index("persona") + 1, "designation")
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for item in items:
        row = {k: item.get(k) for k in fields}
        if isinstance(row.get("competitor_focus"), (list, dict)):
            row["competitor_focus"] = json.dumps(row["competitor_focus"])
        row["sources"] = _format_sources(row.get("sources"))
        row["key_claims"] = _format_key_claims(row.get("key_claims"))
        writer.writerow(row)
    return buf.getvalue()


def to_json(items: list[dict]) -> str:
    if _has_disease_state(items):
        return json.dumps(
            {"pre_launch_notice": PRELAUNCH_LABEL, "items": items},
            default=str, indent=2,
        )
    return json.dumps(items, default=str, indent=2)
