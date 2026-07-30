"""MeasurementSnapshot model — a frozen KPI reading for an intervention cohort.

A snapshot captures the AI-answer KPIs (derived from the latest ScoringRecords) for a fixed
set of responses at a point in time. Three kinds exist:

- DISCOVERY: computed at creation from the cohort's most-recent *existing* scored responses
  (free; explains "why the intervention was approved"). Not the comparison point.
- OFFICIAL_BASELINE: fresh targeted reruns launched at publish (the true pre-publication
  comparison point, using the same method as POST).
- POST: fresh targeted reruns launched after the adoption waiting period.

`metric_values_json` is NULL while the underlying runs are still executing/scoring; the sweep
finalizes it once every run is terminal and every response is scored.
"""
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base

SNAPSHOT_TYPES = ("DISCOVERY", "OFFICIAL_BASELINE", "POST")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MeasurementSnapshot(Base):
    """A point-in-time KPI reading for an intervention's frozen cohort."""

    __tablename__ = "measurement_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # UUID
    intervention_id: Mapped[str] = mapped_column(String(64), index=True)
    snapshot_type: Mapped[str] = mapped_column(String(24), index=True)

    # Fresh-run snapshots record the run_ids they aggregate; discovery snapshots leave this
    # NULL (they read historical responses directly). NULL metric_values_json => still pending.
    run_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)       # JSON list[str]
    question_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list[str]

    response_count: Mapped[int] = mapped_column(Integer, default=0)
    metric_values_json: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON KPI bundle
    model_versions_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON {llm: version}
    scorer_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
