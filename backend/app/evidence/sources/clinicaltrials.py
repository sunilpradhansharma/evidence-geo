"""ClinicalTrials.gov v2 adapter (Phase 3B).

``fetch`` touches the wire and never raises; ``parse`` is pure and is what the tests
exercise against committed fixture JSON. Respects the registry's ~50 requests/minute
guidance via a shared limiter.

The interesting part is ``resultsSection.outcomeMeasuresModule``. Its shape is
``outcomeMeasures[] -> classes[] -> categories[] -> measurements[]``, with per-group
denominators held separately in ``denoms``. Four things make it awkward, and each is
handled explicitly below rather than assumed away:

* **Results groups are not protocol arms.** ``armGroups`` (protocol) and each outcome's
  own ``groups`` (results, ``OG000``…) are different objects with different IDs. They are
  reconciled on title, and an unreconciled group is reported rather than dropped.
* **Binary results are usually posted as percentages.** An NMA needs events and N. Back-
  deriving ``events = round(pct/100 * denom)`` from a value rounded to one decimal is
  lossy, so every derived count is flagged ``EVENTS_DERIVED_FROM_PERCENTAGE`` — the
  number is usable, but a reviewer can see it was not reported directly. Where a class
  posts a denominator contradicting the measure's, no count is derived at all: the source
  disputes the N, so the reconstruction would be wrong rather than merely rounded.
* **The class axis is not one thing.** ``classes`` carries the timepoint (one class per
  visit under a ``timeFrame`` listing them all), the endpoint (``PASI 75``, ``PASI 90``,
  ``PASI 100`` as siblings) or a genuine subgroup, and only the last is a stratum. Each
  yields its own row, identified from its own title — a class may not inherit an endpoint
  its siblings name more precisely, and two classes that would claim one endpoint at one
  week are both left unidentified rather than one of them being picked.
* **``paramType`` decides the shape.** Counts and percentages are binary; means are
  continuous and need ``dispersionType`` to interpret the spread. An unrecognised
  ``paramType`` produces a warning, never a guessed interpretation.

Nothing here decides whether a result is *usable* — that is the resolver's job under an
approved protocol. This module's contract is to represent faithfully what the registry
published, including the parts that are inconvenient.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime

from app.config import taxonomy
from app.evidence import treatments as treatment_labels
from app.evidence.endpoints import EndpointMatch, match_endpoint, normalise, parse_timepoint_weeks
from app.evidence.sources.base import FetchResult, get_json
# Aliased to its long-standing private name so this adapter's regression guards keep
# exercising the exact path it uses. Published NMAs call the same function — see
# app/evidence/treatments.py for why one shared normaliser is load-bearing.
from app.evidence.treatments import canonical_treatment as _canonical_treatment
from app.models.clinical_study import BINARY, CONTINUOUS, ClinicalStudy, OutcomeResult, StudyArm

logger = logging.getLogger(__name__)

SOURCE_TYPE = "CLINICALTRIALS_GOV"
BASE_URL = "https://clinicaltrials.gov/api/v2"

# The registry asks for ~50 requests/minute. One shared limiter so concurrent ingestion
# tasks cannot collectively exceed it.
_MIN_INTERVAL_SECONDS = 60.0 / 50.0
_rate_lock = asyncio.Lock()
_last_request_at = 0.0

# paramType values that describe a proportion of participants -> binary.
_BINARY_PARAM_TYPES = {"COUNT_OF_PARTICIPANTS", "NUMBER"}
# paramType values that describe a central tendency -> continuous.
_CONTINUOUS_PARAM_TYPES = {
    "MEAN", "LEAST_SQUARES_MEAN", "GEOMETRIC_MEAN", "MEDIAN", "GEOMETRIC_LEAST_SQUARES_MEAN",
}
_SD_DISPERSIONS = {"STANDARD_DEVIATION"}
_SE_DISPERSIONS = {"STANDARD_ERROR"}

_PERCENT_HINT = re.compile(r"percent", re.IGNORECASE)
_DOSE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(mg|g|mcg|µg|ug)\b", re.IGNORECASE)
_FREQ_RE = re.compile(
    r"\b(QD|BID|TID|QW|Q2W|Q4W|Q8W|Q12W|EOW|once daily|twice daily|every \d+ weeks?)\b",
    re.IGNORECASE,
)
_INDUCTION_RE = re.compile(r"\binduction\b", re.IGNORECASE)
_MAINTENANCE_RE = re.compile(r"\bmaintenance\b", re.IGNORECASE)

# Mismatch flags. Recorded on the row, surfaced to curation, never silently resolved.
FLAG_DERIVED_EVENTS = "EVENTS_DERIVED_FROM_PERCENTAGE"
FLAG_NO_DENOMINATOR = "NO_DENOMINATOR_POSTED"
FLAG_UNMAPPED_ENDPOINT = "ENDPOINT_NOT_CANONICAL"
FLAG_AMBIGUOUS_ENDPOINT = "ENDPOINT_AMBIGUOUS"
FLAG_UNKNOWN_PARAM_TYPE = "UNKNOWN_PARAM_TYPE"
FLAG_NO_TIMEPOINT = "TIMEPOINT_NOT_PARSED"
FLAG_STRATIFIED = "STRATIFIED_RESULT"
# The row's week came from its own visit class, not from the measure's timeFrame. Kept
# visible because a value from a "by visit over time" series is a repeated measure, not
# the trial's pre-specified analysis at that week.
FLAG_VISIT_TIMEPOINT = "TIMEPOINT_FROM_VISIT_CLASS"
FLAG_UNRECONCILED_GROUP = "RESULTS_GROUP_NOT_MATCHED_TO_ARM"
# A sibling class named the canonical endpoint and this one named a different member of the
# same family, so it may not inherit the measure title's identity (PASI 75 beside PASI 90).
FLAG_CLASS_NAMES_ANOTHER_ENDPOINT = "ENDPOINT_NOT_NAMED_BY_CLASS"
# Two classes would claim one endpoint at one week for the same group. The class axis is
# separating something the canonical vocabulary cannot express, so neither row may claim it.
FLAG_ENDPOINT_NOT_DISTINGUISHED = "ENDPOINT_NOT_DISTINGUISHED_BY_CLASS"
# The class posted its own denominator and it contradicts the measure's. Back-deriving a
# count would divide a percentage by an N the source itself disputes.
FLAG_DISPUTED_DENOMINATOR = "DENOMINATOR_DISPUTED_BY_CLASS"


@dataclass
class ParsedStudy:
    """Canonical rows for one registry record, unsaved and ready for curation."""

    study: ClinicalStudy
    arms: list[StudyArm] = field(default_factory=list)
    outcomes: list[OutcomeResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_results(self) -> bool:
        return bool(self.outcomes)

    @property
    def flag_counts(self) -> dict[str, int]:
        """``{flag: row count}`` for this study, most frequent first.

        A flag nobody counts is not disclosure. ``EVENTS_DERIVED_FROM_PERCENTAGE`` covered
        2468 of 6342 rows in one PsA harvest and no report said so, which is how a number
        back-derived from a rounded percentage can reach a reviewer looking exactly like a
        number the registry posted. The census travels with the parse so a caller can state
        the coverage before quoting anything.
        """
        tally: dict[str, int] = {}
        for row in self.outcomes:
            for flag in json.loads(row.mismatch_flags) if row.mismatch_flags else ():
                tally[flag] = tally.get(flag, 0) + 1
        return dict(sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])))


# --- fetch ----------------------------------------------------------------------------
async def _throttle() -> None:
    global _last_request_at
    async with _rate_lock:
        elapsed = time.monotonic() - _last_request_at
        if elapsed < _MIN_INTERVAL_SECONDS:
            await asyncio.sleep(_MIN_INTERVAL_SECONDS - elapsed)
        _last_request_at = time.monotonic()


async def fetch(nct_id: str) -> FetchResult:
    """Retrieve one study record. Never raises."""
    await _throttle()
    return await get_json(
        f"{BASE_URL}/studies/{nct_id}",
        params={"format": "json"},
        source_type=SOURCE_TYPE,
        source_identifier=nct_id,
    )


async def search(
    *, condition: str, intervention: str | None = None, page_size: int = 50
) -> FetchResult:
    """Search for studies. Never raises. Used by the Phase 0 coverage audit."""
    await _throttle()
    params: dict[str, object] = {
        "format": "json",
        "query.cond": condition,
        "pageSize": page_size,
        "filter.overallStatus": "COMPLETED|ACTIVE_NOT_RECRUITING|RECRUITING",
        # Observational records must never reach an RCT network. A registry cohort
        # listing ten drugs would otherwise become a ten-node clique of non-randomised
        # "comparisons", inventing both direct edges and closed loops. Callers should
        # ALSO check `study.is_randomised`, since interventional does not imply
        # randomised (single-arm and open-label extension studies are interventional).
        "filter.advanced": "AREA[StudyType]INTERVENTIONAL",
    }
    if intervention:
        params["query.intr"] = intervention
    return await get_json(
        f"{BASE_URL}/studies",
        params=params,
        source_type=SOURCE_TYPE,
        source_identifier=f"search:{condition}:{intervention or '*'}",
    )


# --- helpers ---------------------------------------------------------------------------
def _iso_date(value: object) -> date | None:
    """Registry dates are ``YYYY-MM-DD`` or ``YYYY-MM``; anything else is discarded."""
    if isinstance(value, dict):
        value = value.get("date")
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _infer_treatment_phase(protocol: dict) -> tuple[str, str | None]:
    """``(phase, warning)`` inferred from the trial's own titles.

    IBD protocols routinely run induction and maintenance as substudies of one
    registration. When a title names **both**, no single phase is correct for the study,
    so this returns PRIMARY plus a warning rather than picking one — assigning such a
    record to one phase would pool re-randomised maintenance responders with induction
    patients, which is a hard gate downstream (TREATMENT_PHASE_MISMATCH), not a warning.
    """
    identification = protocol.get("identificationModule") or {}
    haystack = " ".join(
        str(identification.get(key) or "")
        for key in ("officialTitle", "briefTitle", "acronym")
    )
    induction = bool(_INDUCTION_RE.search(haystack))
    maintenance = bool(_MAINTENANCE_RE.search(haystack))

    if induction and maintenance:
        return "PRIMARY", (
            "title names both induction and maintenance; treatment_phase left PRIMARY — "
            "the substudies must be separated before either can enter a network"
        )
    if induction:
        return "INDUCTION", None
    if maintenance:
        return "MAINTENANCE", None
    return "PRIMARY", None


def _doses_in(title: str | None) -> list[tuple[float, str]]:
    """Every distinct ``(value, unit)`` a label states, in order of appearance."""
    found: list[tuple[float, str]] = []
    for match in _DOSE_RE.finditer(title or ""):
        value = _to_float(match.group(1))
        if value is None:
            continue
        pair = (value, match.group(2).lower())
        if pair not in found:
            found.append(pair)
    return found


def _arm_dose(
    title: str | None, *, treatment: str, is_placebo: bool
) -> tuple[float | None, str | None, str | None, str | None]:
    """``(value, unit, frequency, warning)`` — a dose only when it belongs to *treatment*.

    Dose is kept structured because ``dose_policy`` decides whether doses may be pooled, and
    silently pooling doses is among the most common and most fatal NMA criticisms. That is
    also why reading the first strength found anywhere in the label is not good enough: on a
    crossover title it attributes the wrong half. ``'Placebo / Upadacitinib 15 mg'`` resolves
    to the **Placebo** node and was carrying ``dose_value=15.0``, so the pooling key said
    15 mg about an arm that received no active drug at all.

    Ten arms in one PsA harvest did this across four different separators — ``/``, ``to``,
    ``Followed by`` and ``Plus`` — so splitting the label on punctuation does not generalise.
    Attribution is by **exclusivity** instead: a dose is recorded only when the label
    describes one agent at one strength.

    Nothing is lost when it is withheld. ``label`` and ``dose_description`` keep the full
    title, so the strength is still on the record and legible to curation; what is withheld
    is only the claim that it is *this node's* dose.
    """
    raw = title or ""
    doses = _doses_in(raw)
    frequency = _FREQ_RE.search(raw)
    stated = frequency.group(1).upper() if frequency else None

    if not doses:
        return None, None, stated, None

    # Placebo has no strength of its own, so a strength in a placebo-resolved label always
    # describes something else: the agent this arm crossed over to, was combined with, or
    # was matched against. The frequency goes with it, for the same reason.
    if is_placebo:
        return None, None, None, (
            f"arm {raw!r} resolves to the Placebo node but states "
            f"{doses[0][0]:g} {doses[0][1]}; placebo has no dose of its own, so none is "
            "recorded"
        )
    if len(doses) > 1:
        return None, None, None, (
            f"arm {raw!r} states {len(doses)} distinct doses "
            f"({', '.join(f'{v:g} {u}' for v, u in doses)}); no single dose_value is true of "
            "it, so none is recorded"
        )
    others = [a for a in treatment_labels.agents_in(raw) if a != treatment]
    if others:
        return None, None, None, (
            f"arm {raw!r} resolves to {treatment} but also names {', '.join(others)}; the "
            "stated dose cannot be attributed to one of them"
        )
    return doses[0][0], doses[0][1], stated, None


def _results_groups(results: dict) -> dict[str, str]:
    """``{group_id: title}`` from participant flow, the most complete group listing."""
    flow = (results.get("participantFlowModule") or {}).get("groups") or []
    return {g.get("id"): (g.get("title") or "").strip() for g in flow if g.get("id")}


def _denominators_of(holder: dict) -> dict[str, float]:
    """``{group_id: N}`` from a ``denoms`` block — an outcome measure, or one of its classes.

    Prefers a participant-unit denominator; falls back to the first posted one, because
    some records label the unit differently while still counting participants.

    Classes post their own ``denoms`` in 185 of 597 posted measures in one PsA harvest, and a
    class-level figure is **not** interchangeable with the measure-level one — see
    ``FLAG_DISPUTED_DENOMINATOR``. Both are read through this one function so the two
    readings cannot drift apart.
    """
    denoms = holder.get("denoms") or []
    chosen = next(
        (d for d in denoms if "participant" in (d.get("units") or "").lower()),
        denoms[0] if denoms else None,
    )
    if not chosen:
        return {}
    return {
        c.get("groupId"): value
        for c in (chosen.get("counts") or [])
        if c.get("groupId") and (value := _to_float(c.get("value"))) is not None
    }


def _outcome_shape(measure: dict) -> tuple[str | None, bool]:
    """``(outcome_type, is_percentage)`` implied by ``paramType`` and the unit."""
    param_type = (measure.get("paramType") or "").strip().upper()
    unit = measure.get("unitOfMeasure") or ""
    is_percentage = "%" in unit or bool(_PERCENT_HINT.search(unit))

    if param_type in _BINARY_PARAM_TYPES:
        return BINARY, is_percentage
    if param_type in _CONTINUOUS_PARAM_TYPES:
        return CONTINUOUS, False
    return None, is_percentage


# --- parse -----------------------------------------------------------------------------
def parse(result: FetchResult, *, indication: str | None = None) -> ParsedStudy | None:
    """Map a successful fetch onto canonical rows. Pure; no I/O; never raises.

    *indication* overrides the registry's own condition text. Supply it when ingesting
    for a known network — registry conditions are free text ("Psoriasis Arthritis",
    "Arthritis, Psoriatic") and will not match the brands.yaml overlay reliably.
    """
    if not result.ok or not isinstance(result.payload, dict):
        return None

    payload = result.payload
    protocol = payload.get("protocolSection") or {}
    results = payload.get("resultsSection") or {}
    warnings: list[str] = []

    # An empty or protocol-less body is not a study record. Without this guard the
    # source_identifier fallback below manufactures a ClinicalStudy with no title, no
    # indication and no arms out of `{}` — a row that looks ingested but says nothing.
    if not protocol:
        return None

    identification = protocol.get("identificationModule") or {}
    # Falls back to the identifier we requested, for a real record that omits nctId.
    nct_id = (identification.get("nctId") or result.source_identifier or "").strip()
    if not nct_id:
        return None

    status = protocol.get("statusModule") or {}
    design = protocol.get("designModule") or {}
    conditions = (protocol.get("conditionsModule") or {}).get("conditions") or []
    sponsor = ((protocol.get("sponsorCollaboratorsModule") or {}).get("leadSponsor") or {}).get("name")

    resolved_indication = indication or (conditions[0] if conditions else "")
    if not indication and conditions:
        warnings.append(
            f"indication taken from registry condition text {conditions[0]!r}; "
            "supply `indication=` to bind it to the brands.yaml overlay"
        )

    treatment_phase, phase_warning = _infer_treatment_phase(protocol)
    if phase_warning:
        warnings.append(f"{nct_id}: {phase_warning}")

    phases = design.get("phases") or []
    study = ClinicalStudy(
        study_id=nct_id,  # deterministic, so re-ingestion updates rather than duplicates
        registry_id=nct_id,
        acronym=identification.get("acronym"),
        title=identification.get("officialTitle") or identification.get("briefTitle"),
        indication=resolved_indication,
        phase="/".join(phases) if phases else None,
        study_design=((design.get("designInfo") or {}).get("allocation")),
        is_randomised=((design.get("designInfo") or {}).get("allocation") == "RANDOMIZED"),
        enrollment=(design.get("enrollmentInfo") or {}).get("count"),
        treatment_phase=treatment_phase,
        sponsor=sponsor,
        start_date=_iso_date(status.get("startDateStruct")),
        completion_date=_iso_date(status.get("completionDateStruct")),
        results_first_posted=_iso_date(status.get("resultsFirstPostDateStruct")),
        source_payload_id=None,  # set by the ingestion service once the payload is stored
    )

    arms, group_to_arm, arm_warnings = _parse_arms(nct_id, protocol, results)
    warnings.extend(arm_warnings)
    outcomes, outcome_warnings = _parse_outcomes(
        nct_id, results, arms, group_to_arm, resolved_indication
    )
    warnings.extend(outcome_warnings)

    return ParsedStudy(study=study, arms=arms, outcomes=outcomes, warnings=warnings)


def _arm_sample_sizes(results: dict) -> dict[str, int]:
    """``{group_id: randomised N}`` from the first period's ``STARTED`` milestone.

    ``StudyArm.sample_size`` was never populated by this parser, which left
    ``row.sample_size or arm.sample_size`` in the comparison service as a dead fallback: a
    measure posting no denominator lost its arm N even though participant flow states it
    plainly.

    The **first** period is the randomised one. A later period counts re-randomised
    completers, which is a different denominator entirely, and using it would silently
    shrink an arm to its responders.

    A zero is dropped rather than stored. Period 1 posts ``0`` for the groups that only come
    into existence in a later period, and recording that would assert an arm randomised
    nobody — a claim, where the truth is simply that this period does not describe it.
    ``None`` leaves ``row.sample_size or arm.sample_size`` to fall through honestly.
    """
    periods = (results.get("participantFlowModule") or {}).get("periods") or []
    if not periods:
        return {}
    for milestone in periods[0].get("milestones") or []:
        if (milestone.get("type") or "").strip().upper() != "STARTED":
            continue
        return {
            group_id: int(value)
            for achievement in milestone.get("achievements") or []
            if (group_id := achievement.get("groupId"))
            and (value := _to_float(achievement.get("numSubjects"))) is not None
            and value > 0
        }
    return {}


def _period_group_ids(results: dict) -> list[set[str]]:
    """Group ids each participant-flow period actually counts, in order.

    A zero is not membership. Period 1 posts ``0`` for the groups that only come into
    existence later, which is the only thing that tells the two apart at all — the same
    reading ``_arm_sample_sizes`` already relies on.
    """
    periods = (results.get("participantFlowModule") or {}).get("periods") or []
    per_period: list[set[str]] = []
    for period in periods:
        present: set[str] = set()
        for milestone in period.get("milestones") or []:
            for achievement in milestone.get("achievements") or []:
                group_id = achievement.get("groupId")
                if group_id and _to_float(achievement.get("numSubjects")):
                    present.add(group_id)
        per_period.append(present)
    return per_period


def _later_period_groups(results: dict) -> set[str]:
    """Groups a later period counts and period 1 does not.

    Participant flow lists every period's groups together, so a registration with an
    extension posts one group per arm **per period**: NCT03104400 is a four-arm trial with
    ten flow groups, five of them ``… Period 2 (Weeks 56 to 260)``. Those are not additional
    randomisations, they are the same patients re-counted after re-randomisation, which is
    exactly why ``_arm_sample_sizes`` refuses period 2's N.

    They are kept out of ``arms`` rather than merged into them. A ``StudyArm`` row asserts a
    randomised group, and ten for a four-arm trial overstates the trial in every report that
    counts arms — and gives a later-period group its own chance to answer for the treatment
    in a network. Nothing is lost if a measure does name one: ``_measure_group_arms`` mints
    it on demand, which is what makes excluding it here safe.

    Requires positive evidence. With fewer than two periods, or nothing counted in period 1,
    there is nothing to compare against and every group is kept.
    """
    per_period = _period_group_ids(results)
    if len(per_period) < 2 or not per_period[0]:
        return set()
    later: set[str] = set()
    for present in per_period[1:]:
        later |= present
    return later - per_period[0]


def _measure_group_arms(
    nct_id: str, results: dict, taken: set[str]
) -> tuple[list[StudyArm], list[str]]:
    """Arms for analysis groups the outcome measures name but participant flow does not.

    Participant flow describes each group's whole journey — "Placebo / Upadacitinib 15 mg" —
    while an outcome measure names the group as it stood at that timepoint, "Placebo". Those
    are different partitions of the same patients, so title reconciliation fails for exactly
    the arms whose assignment changed later. That is disproportionately **placebo**, the
    common comparator a star network is anchored on: in NCT03104400 it dropped the placebo
    arm from all 18 measures, and across one PsA harvest 664 rows (10.5%) were left
    unattached and therefore invisible to every network.

    The union reading is verifiable rather than assumed. That measure's "Placebo" group
    counts 423 participants, which is exactly FG000 (211) plus FG001 (212) — so no existing
    arm can carry the row and a group of its own is the honest representation.

    Aggregate rows are skipped. "Total" is not an enumerator, names no class, and survives
    ``canonical_treatment`` as a plausible node name, so it would pool across studies into
    one fabricated comparator. A dropped row is recoverable; an invented shared comparator
    corrupts the graph.
    """
    seen: dict[str, str] = {}
    for measure in (results.get("outcomeMeasuresModule") or {}).get("outcomeMeasures") or []:
        for group in measure.get("groups") or []:
            title = (group.get("title") or "").strip()
            key = normalise(title)
            if not key or key in taken or key in seen:
                continue
            if treatment_labels.is_aggregate_label(title):
                continue
            treatment, _ = _canonical_treatment(title)
            if treatment_labels.is_uninformative_label(treatment):
                continue
            if treatment_labels.is_class_level_node(treatment):
                continue
            seen[key] = title

    arms: list[StudyArm] = []
    warnings: list[str] = []
    # Sorted so the generated ids are deterministic, like every other id this parser mints.
    for index, key in enumerate(sorted(seen)):
        title = seen[key]
        treatment, is_placebo = _canonical_treatment(title)
        dose, unit, frequency, dose_warning = _arm_dose(
            title, treatment=treatment, is_placebo=is_placebo
        )
        if dose_warning:
            warnings.append(f"{nct_id}: {dose_warning}")
        arms.append(StudyArm(
            arm_id=f"{nct_id}:MG{index:03d}",
            study_id=nct_id,
            label=title,
            treatment=treatment,
            is_placebo=is_placebo,
            drug_class=taxonomy.drug_class_for(treatment),
            administration_route=taxonomy.administration_route_for(treatment),
            dose_value=dose,
            dose_unit=unit,
            dose_frequency=frequency,
            dose_description=title,
        ))
    return arms, warnings


def _parse_arms(
    nct_id: str, protocol: dict, results: dict
) -> tuple[list[StudyArm], dict[str, StudyArm], list[str]]:
    """Build arms, preferring results groups (which carry the IDs outcomes reference).

    Falls back to protocol ``armGroups`` for a registration with no posted results — the
    arms are still worth holding, they simply have no measurements yet.

    Arm-level anomalies are returned as warnings because ``StudyArm`` has no
    ``mismatch_flags`` column: a row either states a dose or does not, and the reason it does
    not has nowhere on the row to live.
    """
    groups = _results_groups(results)
    arms: list[StudyArm] = []
    by_group: dict[str, StudyArm] = {}
    warnings: list[str] = []

    if groups:
        sizes = _arm_sample_sizes(results)
        later_period = _later_period_groups(results)
        if later_period:
            warnings.append(
                f"{nct_id}: {len(later_period)} participant-flow group(s) are counted only in "
                f"a later period and are not randomised arms "
                f"({', '.join(sorted(groups[g] for g in later_period if g in groups))}); "
                "excluded from the arm list"
            )
        for group_id, title in groups.items():
            if group_id in later_period:
                continue
            treatment, is_placebo = _canonical_treatment(title)
            dose, unit, frequency, dose_warning = _arm_dose(
                title, treatment=treatment, is_placebo=is_placebo
            )
            if dose_warning:
                warnings.append(f"{nct_id}: {dose_warning}")
            arm = StudyArm(
                arm_id=f"{nct_id}:{group_id}",
                study_id=nct_id,
                label=title,
                treatment=treatment,
                is_placebo=is_placebo,
                drug_class=taxonomy.drug_class_for(treatment),
                administration_route=taxonomy.administration_route_for(treatment),
                sample_size=sizes.get(group_id),
                dose_value=dose,
                dose_unit=unit,
                dose_frequency=frequency,
                dose_description=title,
            )
            arms.append(arm)
            by_group[group_id] = arm
        # Appended after the flow groups so an existing arm always wins the title join and
        # these only cover what participant flow left unnamed.
        minted, minted_warnings = _measure_group_arms(
            nct_id, results, {normalise(a.label) for a in arms}
        )
        arms.extend(minted)
        warnings.extend(minted_warnings)
        return arms, by_group, warnings

    for index, group in enumerate((protocol.get("armsInterventionsModule") or {}).get("armGroups") or []):
        title = (group.get("label") or "").strip()
        treatment, is_placebo = _canonical_treatment(title)
        dose, unit, frequency, dose_warning = _arm_dose(
            title, treatment=treatment, is_placebo=is_placebo
        )
        if dose_warning:
            warnings.append(f"{nct_id}: {dose_warning}")
        arms.append(StudyArm(
            arm_id=f"{nct_id}:AG{index:03d}",
            study_id=nct_id,
            label=title,
            treatment=treatment,
            is_placebo=is_placebo,
            drug_class=taxonomy.drug_class_for(treatment),
            administration_route=taxonomy.administration_route_for(treatment),
            dose_value=dose,
            dose_unit=unit,
            dose_frequency=frequency,
            dose_description=group.get("description") or title,
        ))
    return arms, by_group, warnings


def _reconcile_measure_groups(
    measure: dict, arms: list[StudyArm], by_id: dict[str, StudyArm]
) -> dict[str, StudyArm]:
    """Map THIS measure's group IDs onto study arms.

    A registry results section carries **two independent ID spaces**: participant-flow
    groups (``FG000``…) and each outcome measure's own groups (``OG000``…). They are not
    interchangeable and frequently disagree, so looking a measurement's ``groupId`` up in
    the participant-flow map silently leaves every row unattached — and a result that is
    not attached to an arm cannot enter a network at all.

    Title is the only reliable join between them. A title matching two arms is left
    unresolved rather than guessed, since attaching a number to the wrong arm is worse
    than leaving it for curation. Falls back to the ID where the spaces do coincide.
    """
    by_title: dict[str, StudyArm] = {}
    ambiguous: set[str] = set()
    for arm in arms:
        key = normalise(arm.label)
        if not key:
            continue
        if key in by_title:
            ambiguous.add(key)
        by_title[key] = arm

    resolved: dict[str, StudyArm] = {}
    for group in measure.get("groups") or []:
        group_id = group.get("id")
        if not group_id:
            continue
        key = normalise(group.get("title"))
        if key and key in by_title and key not in ambiguous:
            resolved[group_id] = by_title[key]
        elif group_id in by_id:
            resolved[group_id] = by_id[group_id]
    return resolved


def _class_endpoints(
    class_titles: list[str],
    weeks: list[float | None],
    *,
    measure_title: str,
    indication: str,
) -> tuple[list[EndpointMatch], bool, set[int]]:
    """``(identity per class, the class axis names endpoints, class indexes to withhold)``.

    The class axis carries the **endpoint** as often as it carries the timepoint, and reading
    the identity off the measure title regardless is how real trial numbers end up filed
    under the wrong endpoint. One PsA measure is titled "…Achieved a PASI 50, PASI 75,
    PASI 90 and PASI 100 Response" and posts 20 classes; only PASI 90 is a modelled endpoint,
    so the measure title matched it **unambiguously** and all 20 rows — PASI 50, 75 and 100
    among them — were stored as ``PSA_PASI90_W16``. The matcher's refusal to guess was not
    defeated, it was bypassed by asking it about the family instead of the member.

    So when any class names canonical wording, every class is identified from its **own**
    title. A sibling naming a different member (``PASI 75``) then resolves to nothing, which
    is the correct answer: it is not that endpoint and it may not inherit the family's id.
    ``candidates`` is the test rather than ``matched``, because a class whose wording is
    recognised but whose week falls outside the window has still named the endpoint.

    Two classes left claiming one id at one week are **both** withheld. Their numbers differ,
    so at most one can be that endpoint's value, and the class axis is separating something
    the canonical vocabulary cannot express — picking either would be the guess this module
    exists not to make. Withholding is not a loss: ``endpoint`` keeps the registry's wording
    and ``endpoint_definition`` keeps the class title, so curation can still read the row.
    """
    per_class = [
        match_endpoint(title, indication=indication, week=week)
        for title, week in zip(class_titles, weeks)
    ]
    names_endpoint = len(class_titles) > 1 and any(m.candidates for m in per_class)

    if not names_endpoint:
        # The measure title is the only identity on offer. Cached per week, or a 20-visit
        # series re-runs the matcher 20 times over one string.
        cache: dict[float | None, EndpointMatch] = {}
        for week in weeks:
            if week not in cache:
                cache[week] = match_endpoint(measure_title, indication=indication, week=week)
        per_class = [cache[week] for week in weeks]

    claimed: dict[tuple[str, float | None], list[int]] = {}
    for index, (match, week) in enumerate(zip(per_class, weeks)):
        if match.outcome_id:
            claimed.setdefault((match.outcome_id, week), []).append(index)
    withheld = {i for indexes in claimed.values() if len(indexes) > 1 for i in indexes}
    return per_class, names_endpoint, withheld


def _parse_outcomes(
    nct_id: str,
    results: dict,
    arms: list[StudyArm],
    group_to_arm: dict[str, StudyArm],
    indication: str,
) -> tuple[list[OutcomeResult], list[str]]:
    """Flatten ``outcomeMeasures -> classes -> categories -> measurements`` into rows."""
    measures = (results.get("outcomeMeasuresModule") or {}).get("outcomeMeasures") or []
    rows: list[OutcomeResult] = []
    warnings: list[str] = []
    # (arm, endpoint, week) -> the measures that report it. Reported at the end; see below.
    identity_measures: dict[tuple[str, str, float | None], set[int]] = {}

    for measure_index, measure in enumerate(measures):
        if (measure.get("reportingStatus") or "POSTED").upper() != "POSTED":
            continue

        title = measure.get("title") or ""
        measure_week = parse_timepoint_weeks(measure.get("timeFrame"))
        outcome_type, is_percentage = _outcome_shape(measure)
        denoms = _denominators_of(measure)
        dispersion = (measure.get("dispersionType") or "").strip().upper()

        base_flags: list[str] = []
        if outcome_type is None:
            base_flags.append(FLAG_UNKNOWN_PARAM_TYPE)
            warnings.append(
                f"{nct_id}: unrecognised paramType {measure.get('paramType')!r} for {title!r}; "
                "values retained but the shape is unresolved"
            )
        if not denoms:
            base_flags.append(FLAG_NO_DENOMINATOR)

        measure_groups = _reconcile_measure_groups(measure, arms, group_to_arm)

        classes = measure.get("classes") or []
        class_titles = [(c.get("title") or "").strip() for c in classes]
        # A class titled "Week 12" is a TIMEPOINT, not a subgroup. Registries post
        # repeated-measures outcomes as one class per visit under a single timeFrame that
        # lists them all, so treating those classes as strata both mislabels the row and
        # forces every visit to inherit one week the source never assigned it.
        class_weeks = [parse_timepoint_weeks(t) for t in class_titles]
        # The visit's own week wins over the measure's, which for a repeated-measures
        # outcome identifies nothing.
        weeks = [w if w is not None else measure_week for w in class_weeks]
        class_identities, class_names_endpoint, indistinguishable = _class_endpoints(
            class_titles, weeks, measure_title=title, indication=indication
        )
        if indistinguishable:
            warnings.append(
                f"{nct_id}: {len(indistinguishable)} classes of {title!r} would claim one "
                "canonical endpoint at one week; none of them carries it"
            )
        # Only a class axis that is neither a visit series nor an endpoint family describes a
        # subgroup. Applying the label to the other two is what made STRATIFIED_RESULT read as
        # 5528 subgroup rows when almost none of them were subgroups.
        is_stratified = not class_names_endpoint and (
            sum(1 for w in class_weeks if w is None) > 1
            or any(len(c.get("categories") or []) > 1 for c in classes)
        )

        for class_index, cls in enumerate(classes):
            class_title = class_titles[class_index]
            class_week = class_weeks[class_index]
            week = weeks[class_index]
            match = class_identities[class_index]
            class_denoms = _denominators_of(cls)

            class_flags = list(base_flags)
            outcome_id = match.outcome_id
            if week is None:
                class_flags.append(FLAG_NO_TIMEPOINT)
            if class_week is not None and class_week != measure_week:
                class_flags.append(FLAG_VISIT_TIMEPOINT)
            if not match.matched:
                class_flags.append(
                    FLAG_AMBIGUOUS_ENDPOINT if match.is_ambiguous else FLAG_UNMAPPED_ENDPOINT
                )
            if class_names_endpoint and not match.candidates:
                class_flags.append(FLAG_CLASS_NAMES_ANOTHER_ENDPOINT)
            if class_index in indistinguishable:
                class_flags.append(FLAG_ENDPOINT_NOT_DISTINGUISHED)
                outcome_id = None

            for category_index, category in enumerate(cls.get("categories") or []):
                category_title = (category.get("title") or "").strip()
                qualifier = " / ".join(p for p in (class_title, category_title) if p)

                for measurement in category.get("measurements") or []:
                    group_id = measurement.get("groupId")
                    arm = measure_groups.get(group_id) or group_to_arm.get(group_id)
                    denominator = denoms.get(group_id)
                    disputed = (
                        denominator is not None
                        and group_id in class_denoms
                        and class_denoms[group_id] != denominator
                    )
                    flags = list(class_flags)
                    if group_id and arm is None:
                        flags.append(FLAG_UNRECONCILED_GROUP)
                    if is_stratified:
                        flags.append(FLAG_STRATIFIED)
                    if disputed:
                        flags.append(FLAG_DISPUTED_DENOMINATOR)

                    row = _build_row(
                        nct_id=nct_id,
                        measure_index=measure_index,
                        class_index=class_index,
                        category_index=category_index,
                        group_id=group_id,
                        arm=arm,
                        measure=measure,
                        measurement=measurement,
                        title=title,
                        qualifier=qualifier,
                        week=week,
                        outcome_type=outcome_type,
                        is_percentage=is_percentage,
                        dispersion=dispersion,
                        denominator=denominator,
                        denominator_disputed=disputed,
                        outcome_id=outcome_id,
                        flags=flags,
                    )
                    if row is not None:
                        rows.append(row)
                        if outcome_id and row.arm_id:
                            identity_measures.setdefault(
                                (row.arm_id, outcome_id, week), set()
                            ).add(measure_index)

    # A registry posts the same result twice: once as its own measure ("ACR 20 Response at
    # Week 16") and again inside a combined one ("ACR 20, ACR 50 and ACR 70 Response" by
    # visit). Both readings are faithful — 18 of 44 such identities in one PsA harvest carry
    # identical numbers — so withholding either would discard correct evidence to solve a
    # problem this module does not have.
    #
    # It is reported because the CONSUMER has one. ``gather_evidence`` keys arm data by
    # treatment, so two rows for one identity mean whichever is read last becomes the arm's
    # number with no record that a choice was made. Choosing between two analysis populations
    # is a curation judgement, not an extraction one, so it is disclosed and left alone.
    duplicated = sum(1 for by in identity_measures.values() if len(by) > 1)
    if duplicated:
        warnings.append(
            f"{nct_id}: {duplicated} endpoint/week/arm identities are reported by more than "
            "one outcome measure; both rows are kept as posted, so a consumer expecting one "
            "row per identity will silently use whichever it reads last"
        )

    return rows, warnings


def _build_row(
    *,
    nct_id: str,
    measure_index: int,
    class_index: int,
    category_index: int,
    group_id: str | None,
    arm: StudyArm | None,
    measure: dict,
    measurement: dict,
    title: str,
    qualifier: str,
    week: float | None,
    outcome_type: str | None,
    is_percentage: bool,
    dispersion: str,
    denominator: float | None,
    denominator_disputed: bool,
    outcome_id: str | None,
    flags: list[str],
) -> OutcomeResult | None:
    """One measurement -> one canonical row, or ``None`` when there is no value."""
    value = _to_float(measurement.get("value"))
    if value is None:
        return None

    events: int | None = None
    sample_size = int(denominator) if denominator is not None else None
    mean: float | None = None
    standard_deviation: float | None = None
    standard_error: float | None = None

    if outcome_type == BINARY:
        # A disputed denominator is not derived from. The registry's own class contradicts
        # the measure-level N, so the reconstruction would divide a percentage by a number
        # the source disputes — and that is not a lossy count, it is a wrong one. Left None,
        # which the comparison service reports as "no events/denominator posted" rather than
        # feeding the engine a fabricated numerator.
        if is_percentage and denominator and not denominator_disputed:
            # Lossy by construction: the registry posted a rounded percentage, so the
            # reconstructed count carries that rounding. Flagged, not hidden.
            events = int(round(value / 100.0 * denominator))
            flags.append(FLAG_DERIVED_EVENTS)
        elif not is_percentage:
            events = int(round(value))
    elif outcome_type == CONTINUOUS:
        mean = value
        spread = _to_float(measurement.get("spread"))
        if dispersion in _SD_DISPERSIONS:
            standard_deviation = spread
        elif dispersion in _SE_DISPERSIONS:
            standard_error = spread

    return OutcomeResult(
        result_id=f"{nct_id}:OM{measure_index:03d}:{class_index}:{category_index}:{group_id or 'NA'}",
        study_id=nct_id,
        arm_id=arm.arm_id if arm else None,
        canonical_outcome_id=outcome_id,
        endpoint=title[:128],
        endpoint_definition=" | ".join(
            p for p in (measure.get("description"), qualifier, measure.get("populationDescription")) if p
        ) or None,
        timepoint_week=week,
        outcome_type=outcome_type or BINARY,
        events=events,
        sample_size=sample_size,
        mean=mean,
        standard_deviation=standard_deviation,
        standard_error=standard_error,
        ci_lower=_to_float(measurement.get("lowerLimit")),
        ci_upper=_to_float(measurement.get("upperLimit")),
        effect_measure=(measure.get("unitOfMeasure") or None),
        source_text=f"{title} | {measure.get('timeFrame') or ''}".strip(" |"),
        # Registry-posted values are read directly out of structured JSON, so extraction
        # confidence is 1.0. That says nothing about whether the value is *appropriate*
        # for a given network — that is the resolver's judgement, not the parser's.
        extraction_confidence=1.0,
        mismatch_flags=json.dumps(sorted(set(flags))) if flags else None,
    )
