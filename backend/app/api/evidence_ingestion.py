"""Evidence ingestion API — the corpus-growing operations, off the shell.

Everything here already existed as a CLI script (``scripts/ingest_evidence.py``,
``scripts/ingest_drug_facts.py``, ``scripts/reparse_stored_payloads.py``). Growing the
evidence corpus therefore required a shell inside the production container, which is why
the corpus has grown once. This router is the same three routines, backgrounded, with the
report the scripts print returned as JSON.

**A third prefix, on purpose.** ``/evidence`` commits itself to read-only in its own
docstring, and ``/evidence-review`` carries CURATOR and REVIEWER decisions. Ingestion is
neither: its audit entries are written as ``OPERATOR``, and keeping one prefix per audit
role means a route's URL says which kind of act it performs.

**Nothing here verifies anything.** No request model accepts a ``verified_by`` (they forbid
unknown fields, so passing one is a 422), and no route can reach ``verify_study``. The
CLI's ``--verify-as`` is deliberately not ported: bulk-stamping one name across studies
nobody opened manufactures an audit trail that looks real. Verification stays one study at
a time on ``POST /evidence-review/studies/{id}/curator-check``.

**Preview is the default.** ``commit`` defaults false on all three routes, and a preview
writes no rows *and no audit entry* — a run that promises to write nothing must not write a
row saying so. A preview still queries the live source, because it has to in order to
report what it would store, so a later commit re-harvests and can legitimately differ.

**One job at a time**, across all three kinds. A re-parse racing an ingest fights over the
same rows. Job state is a module-level dict, exactly as ``/harvest/status`` does it — the
same single-process honesty, and the same limitation: a restart loses the report and more
than one uvicorn worker would show per-worker state.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.config import outcomes, taxonomy
from app.config.settings import get_settings
from app.evidence import protocols
from app.models.database import AsyncSessionLocal
from app.services import evidence_ingestion_service as ingest
from app.services import network_builder_service as builder
from app.utils.audit import write_audit
from app.utils.logging import get_logger

logger = get_logger("api.evidence_ingestion")

router = APIRouter(prefix="/evidence-ingestion", tags=["evidence-ingestion"])

# In-memory job state (single-process POC), mirroring _HARVEST in api/harvest.py.
_JOB: dict = {
    "running": False,
    "kind": None,          # trials | drug-facts | reparse
    "mode": None,          # PREVIEW | COMMIT
    "scope": None,         # what was asked for, echoed back
    "started_at": None,
    "finished_at": None,
    "progress": None,
    "report": None,
    "error": None,
}

PREVIEW, COMMIT = "PREVIEW", "COMMIT"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guard_enabled() -> None:
    """403 when the surface is switched off.

    There is no RBAC in this tree, so this flag is the only gate on a surface that spends
    an external API budget and writes to the corpus.
    """
    if not get_settings().evidence_ingestion_api_enabled:
        raise HTTPException(
            403,
            "Evidence ingestion from the UI is disabled "
            "(EVIDENCE_INGESTION_API_ENABLED=false). Use scripts/ingest_evidence.py.",
        )


def _claim(kind: str, *, commit: bool, scope: dict) -> None:
    """Take the single job slot, or 409. Check-and-set with no ``await`` between the two.

    Claimed in the REQUEST, not in the background task. A ``BackgroundTasks`` callable runs
    after the response is sent, so a guard that only flipped the flag there would let two
    submissions a second apart both pass — and a re-parse racing an ingest fights over the
    same rows, which is the thing the guard exists to prevent.
    """
    if _JOB["running"]:
        raise HTTPException(
            409,
            f"An evidence ingestion job ({_JOB['kind']}) is already running. "
            "Only one runs at a time — a re-parse racing an ingest would fight over the "
            "same rows.",
        )
    _JOB.update(
        running=True, kind=kind, mode=COMMIT if commit else PREVIEW, scope=scope,
        started_at=_now(), finished_at=None, progress=None, report=None, error=None,
    )


def _finish() -> None:
    _JOB.update(running=False, finished_at=_now())


# =====================================================================================
# Request models. `extra="forbid"` is load-bearing: it is what makes `verified_by`
# unreachable from this surface rather than merely unused by it.
# =====================================================================================
class TrialsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    indication: str
    drugs: list[str] | None = Field(
        default=None,
        description="Interventions to search for. Defaults to the full-depth drugs.",
    )
    outcome: str | None = Field(
        default=None,
        description="Canonical outcome id to build a network for. Omit to ingest only.",
    )
    protocol: str | None = Field(
        default=None,
        description=(
            "Analysis protocol for the network. Its approved window is REPORTED against "
            "the built topology, never applied to it."
        ),
    )
    phase: str = "PRIMARY"
    stratum: str | None = None
    limit: int | None = Field(default=None, ge=1, le=500)
    commit: bool = False


class DrugFactsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brands: list[str] | None = Field(
        default=None, description="Defaults to every full-depth drug in brands.yaml."
    )
    commit: bool = False


class ReparseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    indication: str | None = None
    study_ids: list[str] | None = None
    commit: bool = False


# =====================================================================================
# Validation happens in the ROUTE, not the task
# =====================================================================================
def _validate_indication(indication: str) -> str:
    """The stored indication key for *indication*, or a 422 naming the known set.

    Resolved through the taxonomy rather than accepted as free text, because the indication
    is what the network id is derived from and what every later read filters on: two
    spellings of one disease are two corpora that never join up.
    """
    resolved = taxonomy.canonical_disease(indication)
    if resolved is None:
        raise HTTPException(
            422,
            f"{indication!r} is not a declared indication. "
            f"Known: {', '.join(taxonomy.diseases()) or '(none configured)'}",
        )
    return resolved


def _validate_outcome(indication: str, outcome: str | None) -> None:
    """Reject an outcome that is not this indication's, naming the set — as the CLI's exit 2 does."""
    if not outcome:
        return
    known = taxonomy.canonical_outcomes_for_disease(indication)
    if known and outcome not in known:
        raise HTTPException(
            422,
            f"{outcome!r} is not a canonical outcome for {indication!r}. "
            f"Known: {', '.join(known) or '(none configured)'}",
        )


def _validate_protocol(protocol: str | None) -> None:
    if protocol and not protocols.is_defined(protocol):
        raise HTTPException(
            422,
            f"protocol {protocol!r} is not defined in analysis_protocols.yaml. "
            f"Known: {', '.join(protocols.protocol_ids()) or '(none configured)'}",
        )


def _validate_phase(phase: str) -> None:
    if phase not in outcomes.TREATMENT_PHASES:
        raise HTTPException(
            422,
            f"treatment phase {phase!r} is not one of "
            f"{', '.join(outcomes.TREATMENT_PHASES)} — induction and maintenance results "
            "are never poolable, so an unrecognised phase cannot be defaulted.",
        )


# =====================================================================================
# Background tasks. Each owns its session: a request-scoped one is already closed by the
# time a BackgroundTasks callable runs.
# =====================================================================================
async def _audit(db, *, kind: str, commit: bool, scope: dict, summary: dict) -> None:
    """Record the request — **only on a commit**.

    A preview writes nothing, and that has to include the audit table. An entry saying
    "nothing was written" is itself a write, and it would make the log unable to answer
    "what changed the corpus?" without reading a mode field on every row.
    """
    if not commit:
        return
    await write_audit(
        db, role="OPERATOR", event="EVIDENCE_INGESTION_REQUESTED",
        context={"kind": kind, "scope": scope, "summary": summary, "surface": "UI"},
        commit=False,
    )


async def _trials_task(data: TrialsIn, indication: str, drugs: list[str], scope: dict) -> None:
    progress: dict = {
        "phase": "starting", "drugs_total": len(drugs), "drugs_done": 0, "discovered": 0,
        "studies_total": 0, "studies_done": 0, "ingested": 0, "screened_out": 0,
    }
    _JOB["progress"] = progress
    try:
        # The task owns its session: a request-scoped one is already closed by the time a
        # BackgroundTasks callable runs.
        async with AsyncSessionLocal() as db:
            try:
                report = await ingest.ingest_indication(
                    db, indication, drugs=drugs, limit=data.limit,
                    commit=False, progress=progress,
                )
                build = None
                if data.outcome:
                    build = await builder.build_network(
                        db,
                        indication=indication,
                        canonical_outcome_id=data.outcome,
                        treatment_phase=data.phase,
                        population_stratum=data.stratum,
                        protocol_id=data.protocol,
                        commit=False,
                        progress=progress,
                    )
                await _audit(
                    db, kind="trials", commit=data.commit, scope=scope,
                    summary={
                        "discovered": report.discovered,
                        "ingested": report.ingested,
                        "updated": report.updated,
                        "skipped": report.skipped,
                        "screened_out": report.screened_out,
                        "network_id": build.network_id if build else None,
                    },
                )
                _JOB["report"] = {
                    "kind": "trials",
                    "committed": data.commit,
                    "ingestion": report.as_dict(),
                    "network": build.as_dict() if build else None,
                }
                # One commit for the whole run. Committing per study would leave a
                # half-ingested indication behind on a failure, which is why every service
                # here takes commit=False and the caller decides.
                if data.commit:
                    await db.commit()
            finally:
                if not data.commit:
                    await db.rollback()
    except builder.NetworkBuildError as e:
        # A RATIFIED network is the expected case: rebuilding it would change the evidence
        # set a reviewer approved. That is a refusal with a reason, not a server fault.
        _JOB["error"] = str(e)
        logger.warning("network build refused: %s", e)
    except Exception as e:  # noqa: BLE001 — a failed job must not take the process down
        _JOB["error"] = str(e)
        logger.exception("evidence trials ingestion failed: %s", e)
    finally:
        progress["phase"] = "done"
        _finish()


async def _drug_facts_task(data: DrugFactsIn, brands: list[str], scope: dict) -> None:
    progress: dict = {
        "phase": "starting", "brands_total": len(brands), "brands_done": 0,
    }
    _JOB["progress"] = progress
    try:
        async with AsyncSessionLocal() as db:
            try:
                report = await ingest.ingest_drug_facts(
                    db, brands, commit=False, progress=progress
                )
                facts = report.as_dict()
                await _audit(
                    db, kind="drug-facts", commit=data.commit, scope=scope,
                    summary={
                        k: facts[k] for k in
                        ("requested", "ingested", "updated", "superseded", "skipped",
                         "not_found")
                    },
                )
                _JOB["report"] = {
                    "kind": "drug-facts",
                    "committed": data.commit,
                    "drug_facts": facts,
                }
                if data.commit:
                    await db.commit()
            finally:
                if not data.commit:
                    await db.rollback()
    except Exception as e:  # noqa: BLE001
        _JOB["error"] = str(e)
        logger.exception("drug-fact ingestion failed: %s", e)
    finally:
        progress["phase"] = "done"
        _finish()


async def _reparse_task(data: ReparseIn, scope: dict) -> None:
    progress: dict = {"phase": "starting", "studies_total": 0, "studies_done": 0}
    _JOB["progress"] = progress
    try:
        async with AsyncSessionLocal() as db:
            try:
                results = await ingest.reparse_studies(
                    db, indication=data.indication, study_ids=data.study_ids,
                    commit=False, progress=progress,
                )
                rows = [r.as_dict() for r in results]
                counts: dict[str, int] = {}
                for row in rows:
                    counts[row["action"]] = counts.get(row["action"], 0) + 1
                await _audit(
                    db, kind="reparse", commit=data.commit, scope=scope,
                    summary={"studies": len(rows), "by_action": counts},
                )
                _JOB["report"] = {
                    "kind": "reparse",
                    "committed": data.commit,
                    "reparse": {
                        "studies": len(rows),
                        "by_action": counts,
                        # Stated as a first-class figure, not left to be inferred from the
                        # per-study reasons: a VERIFIED row is SKIPPED by design, and a
                        # re-parse that "did nothing" on a curated corpus is the expected
                        # outcome rather than a failure.
                        "skipped_because_decided": sum(
                            1 for r in rows
                            if r["action"] == "SKIPPED" and "already" in (r["reason"] or "")
                        ),
                        "results": rows,
                    },
                }
                if data.commit:
                    await db.commit()
            finally:
                if not data.commit:
                    await db.rollback()
    except Exception as e:  # noqa: BLE001
        _JOB["error"] = str(e)
        logger.exception("re-parse failed: %s", e)
    finally:
        progress["phase"] = "done"
        _finish()


# =====================================================================================
# Routes
# =====================================================================================
@router.get("/options")
async def options():
    """The vocabulary the form needs, derived from config rather than hardcoded client-side.

    Served from one place so adding an indication or a protocol to YAML widens the UI
    without a frontend change — and so a form cannot offer an outcome the validator will
    then reject.
    """
    _guard_enabled()
    return {
        "indications": list(taxonomy.diseases()),
        "outcomes_by_indication": {
            disease: list(taxonomy.canonical_outcomes_for_disease(disease))
            for disease in taxonomy.diseases()
        },
        "protocols": list(protocols.protocol_ids()),
        "treatment_phases": list(outcomes.TREATMENT_PHASES),
        "full_depth_drugs": list(taxonomy.full_depth_drugs()),
        # Said out loud in the payload, because it is the property most likely to be
        # assumed away by whoever wires the next surface onto this router.
        "verification": (
            "This surface cannot verify anything. Ingested rows land EXTRACTED or MAPPED, "
            "and verification is one study at a time on "
            "/evidence-review/studies/{study_id}/curator-check."
        ),
    }


@router.post("/trials", status_code=202)
async def ingest_trials(data: TrialsIn, background_tasks: BackgroundTasks):
    """Discover, fetch and persist the randomised trials for one indication; optionally
    build the network for one canonical outcome.

    Preview by default. A preview still queries ClinicalTrials.gov — it has to, to report
    what it would store — so a later commit re-harvests and can differ from what you read.
    """
    _guard_enabled()
    indication = _validate_indication(data.indication)
    _validate_outcome(indication, data.outcome)
    _validate_protocol(data.protocol)
    _validate_phase(data.phase)

    drugs = [d.strip() for d in (data.drugs or []) if d and d.strip()]
    if not drugs:
        drugs = list(taxonomy.full_depth_drugs())
    if not drugs:
        raise HTTPException(
            422,
            "No drugs to search for. Pass drugs, or set evidence_depth: full on at least "
            "one entry in brands.yaml.",
        )

    scope = {
        "indication": indication, "drugs": drugs, "outcome": data.outcome,
        "protocol": data.protocol, "phase": data.phase, "stratum": data.stratum,
        "limit": data.limit,
    }
    # Claimed only after validation passes, so a rejected form does not occupy the slot.
    _claim("trials", commit=data.commit, scope=scope)
    background_tasks.add_task(_trials_task, data, indication, drugs, scope)
    return {"status": "started", "kind": "trials", "mode": COMMIT if data.commit else PREVIEW}


@router.post("/drug-facts", status_code=202)
async def ingest_drug_facts(data: DrugFactsIn, background_tasks: BackgroundTasks):
    """Fetch openFDA labels for the given brands and persist them as ``DrugFact`` rows.

    Independent of the NMA stack: these stay valuable for an indication whose network turns
    out to be disconnected.
    """
    _guard_enabled()
    brands = [b.strip() for b in (data.brands or []) if b and b.strip()]
    if not brands:
        brands = list(taxonomy.full_depth_drugs())
    if not brands:
        raise HTTPException(
            422,
            "No brands to fetch. Pass brands, or set evidence_depth: full on at least one "
            "entry in brands.yaml.",
        )

    scope = {"brands": brands}
    _claim("drug-facts", commit=data.commit, scope=scope)
    background_tasks.add_task(_drug_facts_task, data, brands, scope)
    return {
        "status": "started", "kind": "drug-facts",
        "mode": COMMIT if data.commit else PREVIEW,
    }


@router.post("/reparse", status_code=202)
async def reparse(data: ReparseIn, background_tasks: BackgroundTasks):
    """Re-extract stored studies from their own retained payloads. No network call.

    A stale parse is a defect in our code, not in the source, and the two need different
    remedies — re-harvesting to fix a parser bug moves two variables at once. **VERIFIED
    and REJECTED rows are SKIPPED by design**: a maintenance routine does not step around
    the verification lifecycle.
    """
    _guard_enabled()
    indication = (
        _validate_indication(data.indication) if data.indication is not None else None
    )
    study_ids = [s.strip() for s in (data.study_ids or []) if s and s.strip()]
    scope = {"indication": indication, "study_ids": study_ids or None}
    _claim("reparse", commit=data.commit, scope=scope)
    # Normalised so the task filters on the same values the scope reports.
    background_tasks.add_task(
        _reparse_task,
        ReparseIn(indication=indication, study_ids=study_ids or None, commit=data.commit),
        scope,
    )
    return {"status": "started", "kind": "reparse", "mode": COMMIT if data.commit else PREVIEW}


@router.get("/status")
async def status():
    """Live job state: phase, counters, then the full report once it finishes.

    In-memory and single-process, like ``/harvest/status``. A restart loses the report, and
    more than one uvicorn worker would show per-worker state.
    """
    _guard_enabled()
    return dict(_JOB)
