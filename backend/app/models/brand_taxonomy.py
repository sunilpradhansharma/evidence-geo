"""The brand taxonomy, stored in SQLite instead of brands.yaml.

These four tables hold what ``backend/app/config/seed/brands_seed.yaml`` used to be the
runtime source of: the ``therapeutic_areas:`` blocks, the ``indications:`` disease overlay,
and the ``drug_catalog:`` annotations. ``app.config.taxonomy`` reads them through an
in-process snapshot and keeps every one of its accessors unchanged, so nothing downstream
knows the source moved.

Three properties this schema exists to hold:

* **Declaration order is data, not an accident.** ``curation.coverage.rank()`` orders the gap
  queue by ``diseases().index(...)`` and ``competitors_for_disease().index(...)`` — the YAML's
  curated tiering was expressed purely by line order. A row without an explicit
  ``display_order`` would let SQLite's row order silently rerank the queue, so every table
  carries one and every read sorts by it.
* **An absent field is not a null field.** The YAML omits keys rather than nulling them, and
  ``drug_index()`` distinguishes the two via ``.get(key, default)``. ``as_entry()`` therefore
  omits anything unset, so a snapshot rebuilt from these rows compares *equal* to the parsed
  YAML rather than merely equivalent. ``tests/test_brand_taxonomy_store.py`` pins that.
* **Curation reasoning survives.** The competitive field was curated per disease with the
  reasoning in YAML comments — "Calquence is DROPPED here although it is a BTK inhibitor: it
  holds no Waldenstrom indication", "Seroquel is dropped as the ARGUABLE case". A comment
  cannot survive a migration into rows, so ``TaxonomyIndicationDrug.note`` carries it. It is
  also where an LLM-suggested competitor's stated reason lands.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base

# Which YAML block a drug row came from. ``CATALOG`` rows have no ``ta_key`` — they are the
# ``drug_catalog:`` annotations for agents that appear only as disease-overlay comparators.
SIDE_FOCUS = "FOCUS"
SIDE_COMPETITOR = "COMPETITOR"
SIDE_CATALOG = "CATALOG"
DRUG_SIDES = (SIDE_FOCUS, SIDE_COMPETITOR, SIDE_CATALOG)

# Role of a drug within one indication's overlay entry.
ROLE_BRAND = "BRAND"
ROLE_COMPETITOR = "COMPETITOR"
INDICATION_ROLES = (ROLE_BRAND, ROLE_COMPETITOR)

# An indication seeded from the reviewed baseline is VERIFIED. One created through the UI
# with model-drafted canonical outcomes is DRAFT, and the evidence programme refuses it —
# see ``app.config.taxonomy.draft_diseases``.
STATUS_VERIFIED = "VERIFIED"
STATUS_DRAFT = "DRAFT"
VERIFICATION_STATUSES = (STATUS_VERIFIED, STATUS_DRAFT)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _loads(raw: str | None) -> list:
    """Parse a JSON list column, tolerating null and malformed content."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _dumps(values: list | tuple | None) -> str | None:
    """Serialise a list column, storing ``None`` for empty so absence round-trips."""
    clean = [v for v in (values or []) if v not in (None, "")]
    return json.dumps(clean) if clean else None


class TaxonomyAreaBlock(Base):
    """One ``therapeutic_areas:`` key — the stored ``therapeutic_area`` value on a question."""

    __tablename__ = "taxonomy_area_blocks"

    ta_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    # The broad display rollup. Several keys can share one area (Endometriosis and Uterine
    # Fibroids are both Women's Health).
    area: Mapped[str] = mapped_column(String(128))
    display_order: Mapped[int] = mapped_column(Integer, default=0, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TaxonomyDrug(Base):
    """One curated drug entry, in a therapeutic-area block or the standalone catalog.

    A drug legitimately appears several times: Rinvoq is declared under Dermatology,
    Gastroenterology and Rheumatology, each with its own ``indications`` list. The surrogate
    key exists because ``ta_key`` is NULL for catalog rows, and SQLite will not enforce
    uniqueness over a composite key containing NULLs.
    """

    __tablename__ = "taxonomy_drugs"
    __table_args__ = (
        UniqueConstraint("ta_key", "side", "name", name="uq_taxonomy_drug_slot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # NULL == a ``drug_catalog:`` entry with no therapeutic-area block of its own.
    ta_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    side: Mapped[str] = mapped_column(String(16), index=True)

    name: Mapped[str] = mapped_column(String(128), index=True)
    generic: Mapped[str | None] = mapped_column(String(128), nullable=True)
    company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    drug_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    administration_route: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Left NULL when the YAML omitted it, so ``_DEFAULT_DEPTH`` still applies on read rather
    # than being baked in here — scope stays a decision, not a storage side effect.
    evidence_depth: Mapped[str | None] = mapped_column(String(16), nullable=True)
    biosimilar_of: Mapped[str | None] = mapped_column(String(128), nullable=True)
    background_therapy: Mapped[bool] = mapped_column(Boolean, default=False)

    aliases_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Only meaningful for focus brands; the YAML declares it on those alone.
    indications_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Fields the source document declared as an explicit null rather than omitting.
    # ABT-122 and MSB11022 are keyed by development code and state ``generic: null``
    # deliberately — "this molecule has no generic name", which is a different claim from
    # "nobody recorded one". Behaviourally the two are identical, so the only thing that
    # notices is the round-trip check; preserving it keeps the store faithful to the
    # document instead of reshaping the document to suit the store.
    null_fields_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    display_order: Mapped[int] = mapped_column(Integer, default=0, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    @property
    def aliases(self) -> list[str]:
        return _loads(self.aliases_json)

    @property
    def indications(self) -> list[str]:
        return _loads(self.indications_json)

    @property
    def null_fields(self) -> list[str]:
        return _loads(self.null_fields_json)

    def as_entry(self) -> dict:
        """The drug dict as the source document expressed it.

        An unset field is OMITTED rather than nulled, except where the document itself wrote
        an explicit null (see ``null_fields``). ``drug_index()`` reads
        ``entry.get("background_therapy", False)`` and ``entry.get("evidence_depth") or
        _DEFAULT_DEPTH``, so the two forms behave identically — which is exactly why the
        distinction has to be preserved deliberately rather than left to chance.
        """
        entry: dict = {"name": self.name}
        for key, value in (
            ("generic", self.generic),
            ("company", self.company),
            ("drug_class", self.drug_class),
            ("administration_route", self.administration_route),
            ("evidence_depth", self.evidence_depth),
            ("biosimilar_of", self.biosimilar_of),
        ):
            if value:
                entry[key] = value
        if self.background_therapy:
            entry["background_therapy"] = True
        if self.indications:
            entry["indications"] = self.indications
        if self.aliases:
            entry["aliases"] = self.aliases
        for field in self.null_fields:
            entry.setdefault(field, None)
        return entry

    def as_catalog_entry(self) -> dict:
        """The ``drug_catalog:`` value shape, which is keyed by name and so omits it."""
        entry = self.as_entry()
        entry.pop("name", None)
        return entry


class TaxonomyIndication(Base):
    """One ``indications:`` disease overlay entry — the unit the coverage matrix is built from."""

    __tablename__ = "taxonomy_indications"

    disease: Mapped[str] = mapped_column(String(128), primary_key=True)
    area: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # The stored ``therapeutic_area`` a question in this disease carries. Must be a key of
    # ``taxonomy_area_blocks`` or questions would store an area no filter can select.
    therapeutic_area_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    aliases_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # REFERENCES into canonical_outcomes.yaml. Never definitions.
    canonical_outcomes_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # DRAFT keeps a UI-created indication out of the evidence programme while leaving it
    # usable for comparison coverage and question generation.
    verification_status: Mapped[str] = mapped_column(
        String(16), default=STATUS_VERIFIED, index=True
    )
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    display_order: Mapped[int] = mapped_column(Integer, default=0, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    @property
    def aliases(self) -> list[str]:
        return _loads(self.aliases_json)

    @property
    def canonical_outcomes(self) -> list[str]:
        return _loads(self.canonical_outcomes_json)


class TaxonomyIndicationDrug(Base):
    """One drug's membership of one indication, as a brand or as a competitor.

    ``note`` is the curation reasoning that used to live in a YAML comment. It is recorded
    and never read by any rule — the presence or absence of the row is the whole assertion —
    but a competitive field curated by exclusion is unreviewable without it.
    """

    __tablename__ = "taxonomy_indication_drugs"
    __table_args__ = (
        UniqueConstraint("disease", "role", "drug_name", name="uq_taxonomy_indication_drug"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    disease: Mapped[str] = mapped_column(String(128), index=True)
    drug_name: Mapped[str] = mapped_column(String(128), index=True)
    role: Mapped[str] = mapped_column(String(16), index=True)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
