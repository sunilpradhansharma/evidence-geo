"""``QuestionEvidence`` (Phase 7) — what a question rests on, and how.

A synthesised question routinely needs several evidence rows: two trials plus a label,
a synthesis plus the limitation that qualifies it, or an evidence gap with more than one
missing link. A single foreign key on ``Question`` cannot express that, so this is a
many-to-many association with a role on the edge.

**One required, single-valued ``relationship_role``.** Separate ``supports`` /
``contradicts`` / ``context_only`` booleans permit nonsense — all three true at once — and
would need a validator whose absence nobody notices. A single value makes the
contradictory combination *unrepresentable* rather than merely invalid.

**The reference is ``(evidence_type, evidence_id)``, not six nullable foreign keys.**
Evidence lives in ``clinical_studies``, ``outcome_results``, ``drug_facts``,
``nma_results``, ``competitor_candidates`` and ``evidence_networks``, and one FK cannot
span them. Six nullable FKs would reintroduce exactly the nonsense states the single role
enum was chosen to forbid, one level down. The cost is honest and stated: the database
cannot enforce that the referenced row exists, so ``evidence_question_service`` resolves
every reference before writing and re-reads it when the approval invariant is checked.

**The approval invariant is a service guarantee, not a schema one.** ``NOT NULL`` gives
"a role is always present" for free; *"at least one **verified** association"* is a count
query over rows in other tables whose verification state changes independently, so it is
enforced at the approval gate and tested there. Worth stating plainly rather than
overselling: nothing here stops a direct ``INSERT`` from creating an unbacked question.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.evidence.question_generation import (
    CATEGORIES,
    EVIDENCE_PRIORITIES,
    EVIDENCE_TYPES,
    RELATIONSHIP_ROLES,
)
from app.models.database import Base

__all__ = [
    "CATEGORIES",
    "EVIDENCE_PRIORITIES",
    "EVIDENCE_TYPES",
    "RELATIONSHIP_ROLES",
    "QuestionEvidence",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class QuestionEvidence(Base):
    """One evidence row, in one role, behind one question."""

    __tablename__ = "question_evidence"
    __table_args__ = (
        # The same row may back a question in only one role. Two rows asserting that a
        # study both supports and contradicts the expected answer is the state the single
        # role enum exists to prevent, and without this constraint it is reachable by
        # inserting twice instead of by setting two flags.
        UniqueConstraint(
            "question_id", "evidence_type", "evidence_id", name="uq_question_evidence_ref"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # The logical Question.question_id, not the surrogate row id — a question is versioned
    # and its associations belong to the logical question, not to one version of its text.
    question_id: Mapped[str] = mapped_column(String(64), index=True)

    evidence_type: Mapped[str] = mapped_column(String(32), index=True)  # see EVIDENCE_TYPES
    evidence_id: Mapped[str] = mapped_column(String(128), index=True)

    relationship_role: Mapped[str] = mapped_column(String(32), index=True)  # REQUIRED
    evidence_priority: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Which generated category proposed this association, so a reviewer can see why a
    # label ended up behind a comparative question.
    category: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    # Snapshotted at association time. The live row is re-read for the approval check, but
    # a stored copy is what lets an audit answer "was it verified when this was approved?"
    # after the underlying row has moved on.
    verification_state_at_link: Mapped[str | None] = mapped_column(String(24), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
