"""Pydantic request/response schemas."""
import json
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator, model_validator

MONITORING_MODE_PATTERN = "^(BRAND|DISEASE_STATE)$"


def _parse_competitor_focus(v):
    """Coerce the DB's JSON-string competitor_focus into a list for API output."""
    if v is None or isinstance(v, list):
        return v
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return None
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, list) else [str(parsed)]
        except (ValueError, TypeError):
            return [v]
    return v


# ---------- Questions ----------
class QuestionCreate(BaseModel):
    question_text: str
    persona: str = Field(..., pattern="^(Prospect|Provider|Patient)$")
    therapeutic_area: str
    indication: str | None = None
    disease: str | None = None
    # FR-108a: brand_focus is optional; required only in BRAND mode (see validator).
    brand_focus: str | None = None
    monitoring_mode: str = Field("BRAND", pattern=MONITORING_MODE_PATTERN)
    competitor_focus: list[str] | None = None
    domain: str = Field(..., pattern="^(Efficacy|Safety|Access|Comparative|General)$")
    approval_status: str = Field("PENDING", pattern="^(PENDING|APPROVED|REJECTED)$")
    approver_name: str | None = None
    active: bool = True
    priority_weight: float = Field(1.0, ge=0.0)  # FR-116.4 demand-ranking weight
    # FR-116 provenance when created from a Prompt Volume gap; NULL for manual questions.
    demand_origin: str | None = Field(None, pattern="^(PROMPT|SYNTHESIZED|KEYWORD)$")

    @model_validator(mode="after")
    def _check_mode(self):
        # FR-108a.1/2: BRAND runs need a focus brand; DISEASE_STATE runs must be
        # brand-less and tagged with at least one competitor instead.
        if self.monitoring_mode == "BRAND":
            if not self.brand_focus:
                raise ValueError("brand_focus is required when monitoring_mode is BRAND")
        else:  # DISEASE_STATE
            self.brand_focus = None
            if not self.competitor_focus:
                raise ValueError(
                    "competitor_focus (>=1 competitor) is required in DISEASE_STATE mode"
                )
        return self


class QuestionUpdate(BaseModel):
    question_text: str | None = None
    persona: str | None = None
    therapeutic_area: str | None = None
    indication: str | None = None
    disease: str | None = None
    brand_focus: str | None = None
    monitoring_mode: str | None = Field(None, pattern=MONITORING_MODE_PATTERN)
    competitor_focus: list[str] | None = None
    domain: str | None = None
    approval_status: str | None = None
    approver_name: str | None = None
    active: bool | None = None
    priority_weight: float | None = Field(None, ge=0.0)  # FR-116.4 demand-ranking weight


class QuestionOut(BaseModel):
    id: int
    question_id: str
    question_text: str
    persona: str
    therapeutic_area: str
    indication: str | None = None
    disease: str | None = None
    brand_focus: str | None = None
    monitoring_mode: str = "BRAND"
    competitor_focus: list[str] | None = None
    domain: str
    intent_type: str | None = None
    active: bool
    priority_weight: float = 1.0
    demand_origin: str | None = None
    approval_status: str
    approver_name: str | None
    version: int
    superseded_by: int | None
    # Question Variations grouping (phrasing-robustness feature)
    variation_group_id: str | None = None
    variation_of: str | None = None
    is_variation: bool = False
    generation_method: str | None = None
    # Bidirectional lineage (computed, not stored):
    #   variation_of_text : for a variation, the CURRENT text of its source question
    #   variation_count   : for an original, how many variations were created from it
    #                       (all staged statuses: draft + approved + rejected)
    variation_of_text: str | None = None
    variation_count: int = 0
    # Derived provenance bucket (computed, not stored): MANUAL | PROMPT_VOLUME | DISCOVER | VARIATION
    source: str | None = None
    # Workshop designation (computed, not stored): Persona + indication from Rhem.csv,
    # e.g. "Patient RA" / "HCP PsA" / "HCP RA & PsA". None for non-workshop questions.
    designation: str | None = None
    created_at: datetime
    updated_at: datetime

    _parse_cf = field_validator("competitor_focus", mode="before")(_parse_competitor_focus)

    class Config:
        from_attributes = True


class SoftDelete(BaseModel):
    reason: str


# FR-116 — commit step of the bulk prompt importer: the analyst-approved subset of
# questions extracted from a CSV preview, plus the shared metadata applied to all of them.
class PromptImportCommit(BaseModel):
    questions: list[str] = Field(..., min_length=1)
    persona: str = Field(..., pattern="^(Prospect|Provider|Patient)$")
    brand_focus: str = Field(..., min_length=1)
    domain: str = "General"
    therapeutic_area: str | None = None
    demand_origin: str = Field("PROMPT", pattern="^(PROMPT|KEYWORD)$")


# FR-116 — in-app SEMrush fetch: pull questions + related keywords straight from the
# Analytics API for a scope, preview them, then ingest as a Prompt Volume dataset.
class SemrushFetchPreviewRequest(BaseModel):
    therapeutic_area: str = Field(..., min_length=1)  # area display name OR a stored TA key
    brand: str | None = None                          # narrow focus brands to one (optional)
    include_generics: bool = True
    include_indications: bool = True
    include_competitors: bool = True
    per_seed_limit: int | None = Field(None, ge=1, le=100)  # clamps SEMrush display_limit
    # Which SEMrush reports to pull: questions (natural-language), related (keywords), or both.
    reports: str | None = Field(None, pattern="^(questions|related|both)$")


class SemrushFetchIngestRequest(BaseModel):
    fetch_id: str = Field(..., min_length=1)          # from a prior /semrush/preview
    source_label: str = Field(..., min_length=1)
    dataset_date: str = Field(..., min_length=1)
    synthesize: bool = True
    only_new: bool = False                            # keep only net-new rows (skip seen/tracked)
    limit: int | None = Field(None, ge=1)             # keep only the top-N by demand


# ---------- Runs ----------
class RunCreate(BaseModel):
    trigger: str = Field("ADHOC", pattern="^(SCHEDULED|ADHOC)$")
    # FR-108a: which slice of the bank to run. BRAND = focus-brand questions,
    # DISEASE_STATE = brand-less landscape/pre-launch questions.
    monitoring_mode: str = Field("BRAND", pattern=MONITORING_MODE_PATTERN)
    persona: str | None = None
    therapeutic_area: str | None = None
    domain: str | None = None
    question_ids: list[str] | None = None
    dry_run: bool = False


class RunOut(BaseModel):
    run_id: str
    trigger: str
    monitoring_mode: str = "BRAND"
    status: str
    started_at: datetime
    ended_at: datetime | None
    questions_attempted: int
    responses_success: int
    responses_failed: int
    responses_truncated: int
    responses_blocked: int
    total_tokens: int
    estimated_cost_usd: float
    alerts_triggered: int
    consensus_full: int = 0
    consensus_partial: int = 0
    consensus_missing: int = 0
    # Why the run ended the way it did (failure reason, budget pause, resume marker).
    # Without this a FAILED run is an unexplained red chip in the UI and the reason is
    # only reachable by querying the database on the server.
    notes: str | None = None

    class Config:
        from_attributes = True


# ---------- Schedule ----------
class ScheduleOut(BaseModel):
    enabled: bool
    cron: str
    timezone: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run_id: str | None

    class Config:
        from_attributes = True


class ScheduleUpdate(BaseModel):
    enabled: bool | None = None
    cron: str | None = None
    timezone: str | None = None


# ---------- Model Release Log (FR-707a) ----------
class ModelReleaseCreate(BaseModel):
    target_platform: str = Field(..., min_length=1)
    release_date: date
    version: str | None = None
    release_notes: str | None = None
    url: str | None = None


class ModelReleaseOut(BaseModel):
    id: int
    target_platform: str
    release_date: date
    version: str | None
    release_notes: str | None
    url: str | None
    source: str = "api"
    event_type: str = "release"
    summary: str | None = None
    effective_date: date | None = None
    first_seen_at: datetime | None = None
    confidence: float | None = None
    created_at: datetime

    class Config:
        from_attributes = True


# ---------- Vendor version capture + per-version impact (FR-707a) ----------
class LiveVersionItem(BaseModel):
    """Current live vendor version for one target, from our own traffic."""
    target_platform: str
    current_version: str | None = None
    current_since: datetime | None = None
    last_seen_at: datetime | None = None
    versions_observed: int = 0
    total_responses: int = 0


class VersionImpactItem(BaseModel):
    """Product impact of one model-update event on our tracked answers."""
    release_id: int
    target_platform: str
    version: str | None = None
    release_date: date
    effective_date: date | None = None
    source: str
    event_type: str
    summary: str | None = None
    confidence: float | None = None
    url: str | None = None
    questions_changed: int = 0
    drift_count: int = 0
    sentiment_before: float | None = None
    sentiment_after: float | None = None
    sentiment_delta: float | None = None
    position_changes: int = 0
    is_high_impact: bool = False


# ---------- Response drift before/after (FR-707a auto-detect UI) ----------
class ResponseDriftItem(BaseModel):
    """One material response change, for the AI Update Impact drift list."""
    id: int
    question_id: str
    question_text: str | None = None
    llm_name: str
    observed_date: date | None = None
    similarity_ratio: float | None = None
    correlated_release_id: int | None = None
    correlated_release_platform: str | None = None
    correlated_release_date: date | None = None
    previous_snippet: str | None = None
    current_snippet: str | None = None


class ResponseDriftDetail(BaseModel):
    """Full before/after for a single drift (the 'View responses' drawer)."""
    id: int
    question_id: str
    question_text: str | None = None
    llm_name: str
    observed_date: date | None = None
    similarity_ratio: float | None = None
    material_change: bool
    diff_text: str | None = None
    previous_response_id: str | None = None
    previous_response_text: str | None = None
    current_response_id: str | None = None
    current_response_text: str | None = None
    correlated_release_id: int | None = None
    correlated_release_platform: str | None = None
    correlated_release_date: date | None = None
    correlated_release_notes: str | None = None


# ---------- Stakeholder Digests (BR-008a) ----------
def _parse_json_list(v):
    """Coerce a DB JSON-string column into a list for API output."""
    if v is None or isinstance(v, list):
        return v
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return None
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, list) else [str(parsed)]
        except (ValueError, TypeError):
            return [v]
    return v


class DigestRuleIn(BaseModel):
    alert_categories: list[str] | None = None
    domains: list[str] | None = None
    therapeutic_areas: list[str] | None = None
    personas: list[str] | None = None
    llm_names: list[str] | None = None


class DigestRuleOut(DigestRuleIn):
    id: int

    _p1 = field_validator("alert_categories", "domains", "therapeutic_areas",
                          "personas", "llm_names", mode="before")(_parse_json_list)

    class Config:
        from_attributes = True


class DigestProfileCreate(BaseModel):
    role: str = Field(..., min_length=1)
    description: str | None = None
    enabled: bool = True
    cron: str = "0 8 * * 1"          # Monday 08:00
    timezone: str = "America/Chicago"
    recipients: list[str] | None = None
    delivery_methods: list[str] | None = Field(default_factory=lambda: ["in_app"])
    webhook_url: str | None = None
    rules: list[DigestRuleIn] = Field(default_factory=list)


class DigestProfileUpdate(BaseModel):
    role: str | None = None
    description: str | None = None
    enabled: bool | None = None
    cron: str | None = None
    timezone: str | None = None
    recipients: list[str] | None = None
    delivery_methods: list[str] | None = None
    webhook_url: str | None = None
    rules: list[DigestRuleIn] | None = None


class DigestProfileOut(BaseModel):
    id: int
    role: str
    description: str | None
    enabled: bool
    cron: str
    timezone: str
    recipients: list[str] | None = None
    delivery_methods: list[str] | None = None
    webhook_url: str | None = None
    rules: list[DigestRuleOut] = []
    created_at: datetime
    updated_at: datetime
    # Next scheduled fire time (UTC), computed from the live scheduler; None when paused/unscheduled.
    next_run_at: datetime | None = None

    _p1 = field_validator("recipients", "delivery_methods", mode="before")(_parse_json_list)

    class Config:
        from_attributes = True


class DigestRunOut(BaseModel):
    id: int
    profile_id: int
    role: str
    generated_at: datetime
    period_start: datetime | None
    period_end: datetime | None
    findings_count: int
    findings: list | None = None
    summary: str | None
    delivered_email: bool
    delivered_webhook: bool
    # Per-method delivery outcome (e.g. {"email": "Sent via SES to 1 recipient(s)."}) so the UI
    # can show WHY an email did or didn't go out instead of failing silently.
    delivery_detail: dict | None = None

    _p1 = field_validator("findings", mode="before")(_parse_json_list)

    @field_validator("delivery_detail", mode="before")
    @classmethod
    def _parse_delivery_detail(cls, v):
        if v is None or isinstance(v, dict):
            return v
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return None
            try:
                parsed = json.loads(v)
                return parsed if isinstance(parsed, dict) else None
            except (ValueError, TypeError):
                return None
        return None

    class Config:
        from_attributes = True


# ---------- Scores ----------
class ScoreOverride(BaseModel):
    sentiment_score: float = Field(..., ge=-1.0, le=1.0)
    competitive_position: str
    rationale: str
    reviewer_name: str


# ---------- Harvest (Discovery) ----------
class HarvestPromote(BaseModel):
    persona: str | None = None
    therapeutic_area: str | None = None
    brand_focus: str | None = None
    domain: str | None = None
    reviewer_name: str | None = None
    override_ae: bool = False  # promote an adverse-event item only after PV sign-off


class HarvestReject(BaseModel):
    reason: str = ""


# ---------- Social Listening: promote a community unmet-need question to Discovery ----------
class SocialUnmetPromote(BaseModel):
    """Stage a voice-of-patient question (from myRAteam/Bezzy) into the Discovery queue.

    Mirrors the double-gate governance: real patient questions never land directly in the
    approved Question Repository — this creates a Discovery staging row that a reviewer then
    promotes to a PENDING Question.
    """
    question: str = Field(..., min_length=8, max_length=400)
    therapeutic_area: str = Field(..., min_length=1)
    brand: str | None = None
    theme: str | None = None
    domain: str = Field("General", pattern="^(Efficacy|Safety|Access|Comparative|General)$")
    persona: str = Field("Patient", pattern="^(Prospect|Provider|Patient)$")


class HarvestRunToPipeline(BaseModel):
    """Discover-page one-click: promote + approve the selected items, then run them."""
    item_ids: list[int] = Field(..., min_length=1)
    reviewer_name: str | None = None
    monitoring_mode: str = Field("BRAND", pattern=MONITORING_MODE_PATTERN)


# ---------- OpenEvidence (manual capture bridge) ----------
# OpenEvidence has no public API and is HCP-gated, so provider-persona answers are
# captured manually: a human runs the question in the OpenEvidence web app and pastes
# the answer back here, where it becomes a normal `open-evidence` Response (scored +
# folded into Chairman consensus exactly like any other target).
class OpenEvidenceSource(BaseModel):
    url: str
    title: str | None = None


class OpenEvidenceCapture(BaseModel):
    run_id: str
    question_id: str
    answer_text: str = Field(..., min_length=1)
    model_version: str | None = None  # defaults to "open-evidence-web"
    sources: list[OpenEvidenceSource] = []


# ---------- Exports (Pinpoint) ----------
class PinpointExportRequest(BaseModel):
    label: str = ""
    llm_name: str | None = None
    persona: str | None = None
    therapeutic_area: str | None = None
    brand_focus: str | None = None
    domain: str | None = None
    status: str | None = None
    run_id: str | None = None
    alert_only: bool = False
    sentiment_min: float | None = None
    sentiment_max: float | None = None
    include_themes: bool = True
    limit: int = Field(5000, ge=1, le=20000)


# ---------- GEO Intervention Recommendations (BR-012) ----------
class GenerateRecommendationsRequest(BaseModel):
    """Filters that scope which competitive-position gaps a generation batch covers.

    ``response_ids`` scopes generation to a specific cohort of answers (e.g. the
    "not mentioned" answers behind one source in the Influence Graph); when present the
    batch covers exactly those responses' gaps and the limit widens to fit them."""
    persona: str | None = None
    therapeutic_area: str | None = None
    indication: str | None = None
    brand: str | None = None
    llm_name: str | None = None
    response_ids: list[str] | None = None
    limit: int = Field(25, ge=1, le=200)


class RecommendationReviewUpdate(BaseModel):
    """Analyst triage update for a recommendation (persisted + audited, BR-010)."""
    status: str = Field(..., pattern="^(NEW|REVIEWING|ACTIONED|DISMISSED)$")
    owner: str | None = None
    note: str | None = None
    updated_by: str | None = None


# ---------- Activation & Impact (Interventions, thin v1) ----------
class InterventionCreate(BaseModel):
    """Create an owned intervention from a GEO recommendation. Most fields default server-side
    from the source recommendation; only overrides need be sent."""
    title: str | None = None
    description: str | None = None
    owner_name: str | None = None
    reviewer_name: str | None = None
    review_required: bool = False
    priority: str | None = Field(None, pattern="^(LOW|MEDIUM|HIGH)$")
    due_date: datetime | None = None
    # Widen the measured cohort beyond the recommendation's own question, if desired.
    extra_question_ids: list[str] | None = None
    target_models: list[str] | None = None          # null => all enabled targets
    primary_metric: str | None = None
    measurement_wait_days: int | None = Field(None, ge=0, le=180)
    repetitions_per_question: int | None = Field(None, ge=1, le=5)


class InterventionUpdate(BaseModel):
    """Patch mutable fields. Cohort/measurement fields are only editable before publish."""
    title: str | None = None
    description: str | None = None
    owner_name: str | None = None
    reviewer_name: str | None = None
    review_required: bool | None = None
    review_status: str | None = None
    priority: str | None = Field(None, pattern="^(LOW|MEDIUM|HIGH)$")
    due_date: datetime | None = None
    target_question_ids: list[str] | None = None
    target_models: list[str] | None = None
    target_metrics: list[str] | None = None
    primary_metric: str | None = None
    measurement_wait_days: int | None = Field(None, ge=0, le=180)
    repetitions_per_question: int | None = Field(None, ge=1, le=5)


class InterventionTransition(BaseModel):
    """A manual workflow move. PUBLISHED/MEASURING/COMPLETED are driven by publish + the sweep."""
    to_status: str = Field(..., pattern="^(PROPOSED|IN_PROGRESS|DEFERRED|CANCELLED)$")
    actor_name: str | None = None
    notes: str | None = None


class InterventionPublish(BaseModel):
    """Record publication + launch the official pre-publication baseline runs."""
    publication_url: str = Field(..., min_length=1)
    publication_date: datetime | None = None
    actor_name: str | None = None


# ---------- Source Authority Mapping (FR-706a.7) ----------
class PreferredSourceCreate(BaseModel):
    """Medical Affairs designating a preferred authority domain for a therapeutic area."""
    therapeutic_area: str = Field(..., min_length=1)
    domain: str = Field(..., min_length=1)  # any URL/host — server normalises to a root domain
    note: str | None = None
    created_by: str = "Medical Affairs"
    change_reason: str | None = None


# ---------- Question Variations (phrasing-robustness grouping) ----------
class VariationGenerateRequest(BaseModel):
    """Ask Claude for N intent-preserving paraphrases of a base question (staged for review)."""
    n: int = Field(4, ge=1, le=6)
    reviewer_name: str | None = None


class VariationEdit(BaseModel):
    variation_text: str = Field(..., min_length=1)


class VariationReview(BaseModel):
    reviewer_name: str | None = None
    note: str | None = None


class VariationGroupRunRequest(BaseModel):
    include_base: bool = True   # include the base question alongside approved variations
    dry_run: bool = False


class VariationExpandRequest(BaseModel):
    """Read-only preview: what a bank selection becomes once approved variations are added."""
    question_ids: list[str] = Field(..., min_length=1)


class VariationOut(BaseModel):
    id: int
    variation_group_id: str
    base_question_id: str
    variation_text: str
    dedupe_hash: str
    generation_method: str
    generation_model: str | None = None
    pii_flags: list[str] | None = None
    status: str
    promoted_question_id: str | None = None
    reviewer_name: str | None = None
    review_note: str | None = None
    edited: bool = False
    created_at: datetime
    updated_at: datetime

    _p_pii = field_validator("pii_flags", mode="before")(_parse_json_list)

    class Config:
        from_attributes = True
