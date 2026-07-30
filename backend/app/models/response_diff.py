"""Response diff model (FR-306, BR-004) — change detection between runs."""
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ResponseDiff(Base):
    __tablename__ = "response_diffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    question_id: Mapped[str] = mapped_column(String(64), index=True)
    llm_name: Mapped[str] = mapped_column(String(64), index=True)

    current_response_id: Mapped[str] = mapped_column(String(64), index=True)
    previous_response_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    similarity_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    material_change: Mapped[bool] = mapped_column(default=False)
    diff_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # unified diff

    # FR-707a: when material drift correlates with a logged vendor model release within
    # the lookback window, we annotate the diff with the release id ("Possible model update").
    correlated_release_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
