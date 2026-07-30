"""Analysis protocol approvals (X1).

The protocol **definition** lives in ``analysis_protocols.yaml`` — diffable and reviewable
in git. Only the **approval** is a database row, and it references the definition by
derived ``content_hash`` rather than containing any of it.

Keeping them apart is what makes the governance model coherent: if approval were stored
alongside the methodology, recording an approval would change the content, change the
hash, and invalidate the approval just granted. Here, recording an approval cannot touch
the definition, and editing the definition cannot silently carry an approval forward —
the hash stops matching and ``approvals.derived_status`` reports ``SUPERSEDED``.

**Rows accumulate; they are not overwritten.** A role that rejects and later approves the
same content leaves both rows behind, and ``approvals.role_status`` reads the latest by
``reviewed_at``. Revocation sets ``revoked_at`` on the row being withdrawn rather than
deleting it, so "this was approved, then withdrawn, with a reason" stays answerable. That
matches the existing evidence rule that history is versioned, never rewritten.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.evidence.approvals import MEDICAL
from app.models.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AnalysisProtocolApproval(Base):
    """One role's decision on one exact version of one protocol's methodology."""

    __tablename__ = "analysis_protocol_approvals"

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # References analysis_protocols.yaml. Deliberately NOT a foreign key: protocols are
    # config, not rows, and an approval must survive a protocol being renamed or retired
    # so the audit trail does not develop holes.
    protocol_id: Mapped[str] = mapped_column(String(64), index=True)

    # The exact content this decision was made against. An approval is meaningless without
    # it — "approved" with no record of *what* was approved is unreviewable.
    content_hash: Mapped[str] = mapped_column(String(80), index=True)

    approval_role: Mapped[str] = mapped_column(String(16), default=MEDICAL, index=True)
    decision: Mapped[str] = mapped_column(String(16), index=True)

    reviewer_id: Mapped[str] = mapped_column(String(128))
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Revocation is a withdrawal, not a deletion. A reason is required by the service
    # layer for the same reason an exclusion requires one: a silent withdrawal is
    # indistinguishable from a mistake.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    revocation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # The lookup every gate performs: "what decisions exist for this protocol at this
    # exact content, by role?"
    __table_args__ = (
        Index("ix_protocol_approval_lookup", "protocol_id", "content_hash", "approval_role"),
    )

    @property
    def is_active(self) -> bool:
        """True when this decision has not been withdrawn."""
        return self.revoked_at is None
