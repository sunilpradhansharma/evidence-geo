"""Seed a rich Activation & Impact demo — Rheumatology (RA + PsA).

Creates a self-contained, prod-safe portfolio of interventions that exercises EVERY
feature of the Activation & Impact loop, using REAL scored responses that flow through the
actual measurement engine (``measurement.compute_metrics`` / ``compute_result``), so every
KPI, outcome, confidence tier and confounder on screen is genuinely computed — not faked.

Portfolio (7 interventions):
  1. RA  / Rinvoq  — IMPROVED, HIGH                    (COMPLETED)  ← hero
  2. PsA / Skyrizi — IMPROVED, MEDIUM (model release + version change in window)
  3. RA  / Humira  — WORSENED, HIGH                    (COMPLETED)
  4. PsA / Rinvoq  — NO_CLEAR_CHANGE, HIGH             (COMPLETED)
  5. PsA / Humira  — in-flight MEASURING (POST_RUNNING) ← click "Measure now" to reveal
  6. RA  / Rinvoq  — PROPOSED (discovery only)         ← populates the "Open" tile
  7. RA  / Humira  — INCONCLUSIVE, LOW (small sample)  (COMPLETED)

Every row is tagged with the ``actdemo-`` id prefix (questions ``Q-ACTDEMO-*``; the
confounder release carries a marker URL) so it is fully removable. Idempotent: a normal run
WIPES prior demo rows then re-inserts; ``--wipe`` only removes. Never auto-runs; pure ORM so
it works on SQLite (dev) and Postgres (prod).

Run:   python -m scripts.seed_activation_demo
Wipe:  python -m scripts.seed_activation_demo --wipe
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import delete, select  # noqa: E402

from app.activation import measurement  # noqa: E402
from app.models.database import AsyncSessionLocal, init_db  # noqa: E402
from app.models.intervention import Intervention  # noqa: E402
from app.models.intervention_event import InterventionEvent  # noqa: E402
from app.models.intervention_result import InterventionResult  # noqa: E402
from app.models.measurement_snapshot import MeasurementSnapshot  # noqa: E402
from app.models.model_release import ModelReleaseLog  # noqa: E402
from app.models.question import Question  # noqa: E402
from app.models.response import Response  # noqa: E402
from app.models.run import Run  # noqa: E402
from app.models.scoring import ScoringRecord  # noqa: E402

MARK = "actdemo"
RELEASE_URL = "https://demo.local/actdemo-release"
SCORER_VERSION = "scoring-claude-demo"
PROMPT_VERSION = "v3"
TA = "Rheumatology"
PLATFORMS = ["claude", "gpt-4o", "gemini", "nova-pro", "llama"]
PLATFORMS_SMALL = ["gemini"]
DEFAULT_VER = {
    "claude": "claude-3-5-sonnet-20241022-v2",
    "gpt-4o": "gpt-4o-2024-05-13",
    "gemini": "gemini-2.0-flash-001",
    "nova-pro": "amazon.nova-pro-v1:0",
    "llama": "meta.llama3-1-70b-instruct-v1:0",
}
POS_SENTIMENT = {
    "FIRST_LINE_RECOMMENDED": 0.72, "AMONG_OPTIONS": 0.36, "SECOND_LINE": -0.06,
    "NOT_RECOMMENDED": -0.44, "NOT_MENTIONED": None,
}

# qid -> (persona, indication, brand, domain, text)
QUESTIONS: dict[str, tuple[str, str, str, str, str]] = {
    "Q-ACTDEMO-RA-1": ("Patient", "Rheumatoid Arthritis", "Rinvoq", "Efficacy",
        "What should I do if my rheumatoid arthritis treatment stops working, and what newer options exist?"),
    "Q-ACTDEMO-RA-2": ("Patient", "Rheumatoid Arthritis", "Humira", "Comparative",
        "My Humira for rheumatoid arthritis isn't working as well anymore — should I switch, and to what?"),
    "Q-ACTDEMO-RA-3": ("Prospect", "Rheumatoid Arthritis", "Rinvoq", "General",
        "I was just diagnosed with rheumatoid arthritis. What options should I look into beyond methotrexate?"),
    "Q-ACTDEMO-RA-4": ("Patient", "Rheumatoid Arthritis", "Humira", "Access",
        "Is there a rheumatoid arthritis treatment that works if biologic injections have failed me?"),
    "Q-ACTDEMO-PSA-1": ("Patient", "Psoriatic Arthritis", "Skyrizi", "Efficacy",
        "What are the best treatments for psoriatic arthritis when skin and joint symptoms are both bad?"),
    "Q-ACTDEMO-PSA-2": ("Prospect", "Psoriatic Arthritis", "Rinvoq", "Comparative",
        "For psoriatic arthritis, how do the newer pills compare to injectable biologics?"),
    "Q-ACTDEMO-PSA-3": ("Patient", "Psoriatic Arthritis", "Humira", "General",
        "My doctor suggested a biologic for psoriatic arthritis. Which options should I ask about?"),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def dt(days_ago: float) -> datetime:
    return _now() - timedelta(days=days_ago)


def _answer(brand: str, pos: str, q: str) -> str:
    m = {
        "FIRST_LINE_RECOMMENDED": f"{brand} is presented as a preferred, first-line option.",
        "AMONG_OPTIONS": f"{brand} is listed among the recommended options.",
        "SECOND_LINE": f"{brand} is mentioned as a later-line alternative.",
        "NOT_RECOMMENDED": f"{brand} is mentioned but not recommended here.",
        "NOT_MENTIONED": f"{brand} is not mentioned; other therapies are discussed.",
    }
    return f"[DEMO] {m[pos]} (re: {q[:70]})"


def _placement(persona: str, brand: str, queries: list[str]) -> dict:
    return {
        "scope": {"persona": persona, "therapeutic_area": TA, "brand": brand},
        "earn_citations": [
            {"domain": "rheumatology.org", "authority_type": "Guideline body (ACR)",
             "display_category": "Medical guideline", "is_preferred": True,
             "response_count": 18, "opportunity_score": 0.92},
            {"domain": "eular.org", "authority_type": "Guideline body (EULAR)",
             "display_category": "Medical guideline", "is_preferred": True,
             "response_count": 14, "opportunity_score": 0.88},
            {"domain": "uptodate.com", "authority_type": "Clinical reference",
             "display_category": "Clinical reference", "is_preferred": False,
             "response_count": 22, "opportunity_score": 0.81},
            {"domain": "pubmed.ncbi.nlm.nih.gov", "authority_type": "Primary literature",
             "display_category": "Journal", "is_preferred": False,
             "response_count": 31, "opportunity_score": 0.77},
            {"domain": "arthritis.org", "authority_type": "Patient advocacy",
             "display_category": "Advocacy org", "is_preferred": False,
             "response_count": 12, "opportunity_score": 0.63},
        ],
        "preferred_gaps": [
            {"domain": "rheumatology.org", "absence_pct": 68.0, "absent": 17},
            {"domain": "eular.org", "absence_pct": 74.0, "absent": 20},
        ],
        "target_queries": [{"query": q, "count": c}
                           for q, c in zip(queries, [42, 28, 19, 11])],
    }


def _evidence(persona: str, brand: str, competitor: str, content_type: str,
              action: str, rationale: str, position: str, queries: list[str]) -> str:
    return json.dumps({
        "recommended_action": action, "rationale": rationale, "content_type": content_type,
        "competitive_position": position, "outperforming_competitor": competitor,
        "competitor_domain": None, "missing_citations": ["rheumatology.org", "eular.org"],
        "search_volume": 8400, "domain_authority": 91, "impact_score": 78.5,
        "llm_name": "claude", "placement": _placement(persona, brand, queries),
    }, default=str)


def _build_cohort(sc_slug: str, snaptype: str, positions: list[str], qid: str,
                  captured_at: datetime, ver_map: dict[str, str], persona: str,
                  platforms: list[str], *, finalize: bool):
    """Persist runs + responses + scores for one snapshot; return (snapshot, rows, run_ids).
    ``finalize`` False leaves the snapshot pending (metrics NULL) so the UI shows
    'Runs in progress' and the sweep / 'Measure now' can finalize it live."""
    _p, _ind, brand, domain, qtext = QUESTIONS[qid]
    rows: list = []
    run_ids: list[str] = []
    reps = len(positions) // len(platforms)
    for rep in range(reps):
        rid = f"{MARK}-run-{sc_slug}-{snaptype}-r{rep}"
        run_ids.append(rid)
        rows.append(Run(
            run_id=rid, trigger="ADHOC", monitoring_mode="BRAND", status="COMPLETED",
            started_at=captured_at, ended_at=captured_at, questions_attempted=1,
            responses_success=len(platforms), notes=f"{MARK} {sc_slug} {snaptype}",
        ))

    pairs: list = []
    for i, pos in enumerate(positions):
        platform = platforms[i // reps]
        rep = i % reps
        resp_id = f"{MARK}-rs-{sc_slug}-{snaptype}-{platform}-{rep}"
        resp = Response(
            response_id=resp_id, run_id=f"{MARK}-run-{sc_slug}-{snaptype}-r{rep}",
            timestamp_utc=captured_at, llm_name=platform,
            llm_model_version=ver_map.get(platform), persona=persona, question_id=qid,
            question_text=qtext, therapeutic_area=TA, indication=QUESTIONS[qid][1],
            disease=QUESTIONS[qid][1],
            brand_focus=brand, domain=domain, monitoring_mode="BRAND",
            response_text=_answer(brand, pos, qtext), prompt_tokens=120,
            response_tokens=340, finish_reason="stop", status="SUCCESS",
            created_at=captured_at,
        )
        score = ScoringRecord(
            score_id=f"{MARK}-sc-{sc_slug}-{snaptype}-{platform}-{rep}",
            response_id=resp_id, score_version=1, prompt_version=PROMPT_VERSION,
            sentiment_score=POS_SENTIMENT[pos], competitive_position=pos,
            brand_mentions=json.dumps([brand] if pos != "NOT_MENTIONED" else []),
            scored_by=SCORER_VERSION, created_at=captured_at,
        )
        rows.extend([resp, score])
        pairs.append((resp, score))

    metrics = measurement.compute_metrics(pairs) if finalize else None
    snap = MeasurementSnapshot(
        id=f"{MARK}-sn-{sc_slug}-{snaptype}", intervention_id=f"{MARK}-iv-{sc_slug}",
        snapshot_type=snaptype,
        run_ids_json=None if snaptype == "DISCOVERY" else json.dumps(run_ids),
        question_ids_json=json.dumps([qid]),
        response_count=len(pairs) if finalize else 0,
        metric_values_json=json.dumps(metrics) if metrics is not None else None,
        model_versions_json=json.dumps(measurement._model_versions([r for r, _ in pairs])),
        scorer_version=SCORER_VERSION if finalize else None,
        prompt_version=PROMPT_VERSION if finalize else None, captured_at=captured_at,
    )
    rows.append(snap)
    return snap, rows, run_ids


def _event(sc_slug: str, etype: str, when: datetime, *, prev=None, new=None,
           actor=None, notes=None, meta=None) -> InterventionEvent:
    return InterventionEvent(
        intervention_id=f"{MARK}-iv-{sc_slug}", event_type=etype, previous_status=prev,
        new_status=new, actor_name=actor, notes=notes,
        metadata_json=json.dumps(meta) if meta else None, created_at=when,
    )


# Position lists are platform-major (REPS consecutive per platform); length 15 for the
# 5-platform cohorts. Tuned so the REAL engine lands on the intended outcome.
B, S, N, A, F = ("NOT_MENTIONED", "SECOND_LINE", "NOT_RECOMMENDED",
                 "AMONG_OPTIONS", "FIRST_LINE_RECOMMENDED")
HERO_BASE = [B, B, S, B, S, B, S, B, A, B, B, B, N, B, S]   # consideration ~7%
HERO_POST = [A, F, A, A, A, S, F, A, B, A, S, A, A, F, B]   # ~73%  -> IMPROVED/HIGH
IMP2_BASE = [B, S, B, S, B, B, A, B, S, B, B, N, S, B, B]   # ~13%
IMP2_POST = [A, A, F, A, S, A, A, F, A, S, A, A, F, A, S]   # ~73%  -> IMPROVED/MEDIUM
WORSE_BASE = [F, A, A, A, F, A, A, A, S, F, A, A, A, S, A]  # ~87%
WORSE_POST = [B, B, S, B, S, B, A, B, S, B, S, A, B, S, B]  # ~13%, modal missing -> WORSENED/HIGH
FLAT_BASE = [A, S, A, B, A, S, A, B, A, S, A, B, A, S, A]   # ~53%
FLAT_POST = [A, A, S, A, B, A, S, A, A, A, S, B, A, S, S]   # ~53% (<5pp move) -> NO_CLEAR_CHANGE
LIVE_BASE = [B, S, A, B, B, S, A, B, B, S, B, A, B, S, B]   # ~20%
LIVE_POST = [A, A, F, A, S, A, F, A, S, A, A, B, A, F, A]   # ~67%  (revealed on Measure now)
PROP_DISC = [B, B, S, B, S, B, N, B, B, S, B, B, B, N, S]   # discovery-only, weak
SMALL_BASE = [B, S, N]                                      # n=3 -> LOW_SAMPLE
SMALL_POST = [A, B, S]                                      # n=3 -> INCONCLUSIVE/LOW


def _scenario(db, spec: dict):
    """Build one full scenario. Returns (intervention, baseline_snap, post_snap);
    baseline/post are None unless the scenario is COMPLETED (caller computes the result)."""
    slug = spec["slug"]
    kind = spec["kind"]
    qid = spec["qid"]
    persona = spec["persona"]
    brand = spec["brand"]
    platforms = spec.get("platforms", PLATFORMS)
    ver_base = spec.get("ver_base", DEFAULT_VER)
    ver_post = spec.get("ver_post", DEFAULT_VER)
    rows: list = []

    # Discovery baseline (free, from history) — always present.
    disc_positions = spec.get("disc") or spec.get("base")
    disc_age = spec.get("disc_age", spec.get("base_age", 30) + 30)
    disc, drows, _ = _build_cohort(slug, "DISCOVERY", disc_positions, qid, dt(disc_age),
                                   ver_base, persona, platforms, finalize=True)
    rows += drows

    interv = Intervention(
        id=f"{MARK}-iv-{slug}", recommendation_id=f"{MARK}-rec-{slug}",
        source_type="GEO_RECOMMENDATION", source_id=f"{MARK}-rec-{slug}",
        evidence_snapshot_json=_evidence(persona, brand, spec["competitor"],
            spec["content_type"], spec["action"], spec["rationale"],
            spec.get("position", "NOT_MENTIONED"), spec["queries"]),
        therapeutic_area=TA, indication=spec["indication"], brand_focus=brand,
        title=spec["title"], description=spec["action"], priority="HIGH",
        owner_name=spec.get("owner"), reviewer_name=spec.get("reviewer"),
        review_required=bool(spec.get("reviewer")),
        review_status="APPROVED" if spec.get("reviewer") else None,
        monitoring_mode="BRAND", target_question_ids_json=json.dumps([qid]),
        target_personas_json=json.dumps([persona]),
        target_models_json=json.dumps(platforms) if platforms is PLATFORMS_SMALL else None,
        target_metrics_json=json.dumps(measurement.RATE_METRICS + ["avg_sentiment"]),
        primary_metric=spec.get("primary_metric", "consideration_rate"),
        measurement_wait_days=14, repetitions_per_question=3,
        discovery_baseline_snapshot_id=disc.id,
    )

    if kind == "proposed":
        interv.status = "PROPOSED"
        interv.measurement_status = "PLANNED"
        interv.created_at = dt(disc_age)
        rows.append(_event(slug, "CREATED", dt(disc_age), new="PROPOSED",
                           notes="Created from a GEO recommendation; discovery baseline captured."))
        db.add_all(rows + [interv])
        return interv, None, None

    # Published scenarios (completed + inflight): official baseline + publication record.
    base_age, post_age, pub_age = spec["base_age"], spec["post_age"], spec["pub_age"]
    pub_date = dt(pub_age)
    base, brows, _ = _build_cohort(slug, "OFFICIAL_BASELINE", spec["base"], qid, dt(base_age),
                                   ver_base, persona, platforms, finalize=True)
    rows += brows
    interv.official_baseline_snapshot_id = base.id
    interv.publication_url = spec["pub_url"]
    interv.publication_date = pub_date
    interv.due_date = dt(pub_age + 2)
    interv.post_due_at = dt(post_age)  # post window reached ~ when the post snapshot was captured
    interv.created_at = dt(pub_age + 2)

    for e in [
        _event(slug, "CREATED", dt(pub_age + 2), new="PROPOSED"),
        _event(slug, "ASSIGNED", dt(pub_age + 1.8), actor=spec.get("owner"),
               notes=f"Assigned to {spec.get('owner')}"),
        _event(slug, "STATUS_CHANGED", dt(pub_age + 1.5), prev="PROPOSED", new="IN_PROGRESS"),
        _event(slug, "PUBLISHED", pub_date, prev="IN_PROGRESS", new="PUBLISHED",
               meta={"url": spec["pub_url"]}),
        _event(slug, "BASELINE_CAPTURED", dt(base_age), new="MEASURING",
               meta={"snapshot_id": base.id, "response_count": base.response_count}),
    ]:
        rows.append(e)

    if kind == "inflight":
        post, prows, _ = _build_cohort(slug, "POST", spec["post"], qid, dt(post_age),
                                       ver_post, persona, platforms, finalize=False)
        rows += prows
        interv.post_snapshot_id = post.id
        interv.status = "MEASURING"
        interv.measurement_status = "POST_RUNNING"
        rows.append(_event(slug, "MEASUREMENT_STARTED", dt(post_age),
                           meta={"post_snapshot_id": post.id}))
        db.add_all(rows + [interv])
        return interv, None, None

    # completed
    post, prows, _ = _build_cohort(slug, "POST", spec["post"], qid, dt(post_age),
                                   ver_post, persona, platforms, finalize=True)
    rows += prows
    interv.post_snapshot_id = post.id
    interv.status = "COMPLETED"
    interv.measurement_status = "DONE"
    db.add_all(rows + [interv])
    return interv, base, post


async def _wipe(db) -> dict:
    counts: dict[str, int] = {}
    like = f"{MARK}-%"
    for model, col in [
        (InterventionResult, InterventionResult.id),
        (InterventionEvent, InterventionEvent.intervention_id),
        (MeasurementSnapshot, MeasurementSnapshot.id),
        (ScoringRecord, ScoringRecord.score_id),
        (Response, Response.response_id),
        (Run, Run.run_id),
        (Intervention, Intervention.id),
    ]:
        res = await db.execute(delete(model).where(col.like(like)))
        counts[model.__tablename__] = res.rowcount or 0
    res = await db.execute(delete(Question).where(Question.question_id.like("Q-ACTDEMO-%")))
    counts["questions"] = res.rowcount or 0
    res = await db.execute(delete(ModelReleaseLog).where(ModelReleaseLog.url == RELEASE_URL))
    counts["model_release_log"] = res.rowcount or 0
    await db.commit()
    return counts


async def _clean_release_free_ages(db, *, span: int = 6, post_age: int = 1) -> tuple[int, int]:
    """Return (base_age, post_age) whose [base, post] date window contains NO logged model
    release, so the 'clean' scenarios land on HIGH confidence regardless of the ambient
    (real, auto-detected) release data already in the DB."""
    today = date.today()
    dates = [d for (d,) in (await db.execute(select(ModelReleaseLog.release_date))).all() if d]
    end = today - timedelta(days=post_age)
    for base in range(span, post_age + 1, -1):            # prefer a recent window
        start = today - timedelta(days=base)
        if not any(start <= d <= end for d in dates):
            return base, post_age
    for back in range(post_age + 1, 160):                 # fallback: any 4-day gap further back
        d_end = today - timedelta(days=back)
        d_start = d_end - timedelta(days=4)
        if not any(d_start <= d <= d_end for d in dates):
            return back + 4, back
    return span, post_age


SPECS: list[dict] = [
    # 1) RA / Rinvoq — IMPROVED / HIGH (hero)
    {"slug": "ra-rinvoq-improved", "kind": "completed", "qid": "Q-ACTDEMO-RA-1",
     "brand": "Rinvoq", "indication": "Rheumatoid Arthritis", "persona": "Patient",
     "competitor": "Xeljanz", "base": HERO_BASE, "post": HERO_POST, "clean": True,
     "base_age": 14, "post_age": 1, "pub_age": 15,
     "title": "Publish a patient FAQ: 'What to do if your RA treatment stops working' — secondary loss of response and the Rinvoq switch pathway",
     "content_type": "Patient FAQ page", "owner": "Priya Shah", "reviewer": "Dr. Helen Carter",
     "pub_url": "https://www.rinvoq.com/ra/treatment-not-working-faq", "position": "NOT_MENTIONED",
     "action": "Create an AI-optimised patient FAQ on secondary loss of response and next-step options in RA.",
     "rationale": "AI answers omit Rinvoq for 'RA treatment stopped working' despite strong SELECT-COMPARE data; competitors dominate the response.",
     "queries": ["what to do if rheumatoid arthritis medication stops working",
                 "rinvoq for rheumatoid arthritis after methotrexate",
                 "secondary loss of response rheumatoid arthritis",
                 "switching biologics rheumatoid arthritis"]},

    # 2) PsA / Skyrizi — IMPROVED / MEDIUM (model release + version change in window)
    {"slug": "psa-skyrizi-improved-medium", "kind": "completed", "qid": "Q-ACTDEMO-PSA-1",
     "brand": "Skyrizi", "indication": "Psoriatic Arthritis", "persona": "Patient",
     "competitor": "Cosentyx", "base": IMP2_BASE, "post": IMP2_POST,
     "base_age": 35, "post_age": 4, "pub_age": 36,
     "ver_post": {**DEFAULT_VER, "gpt-4o": "gpt-4o-2024-11-20"},
     "title": "Publish a PsA skin-and-joint treatment guide featuring Skyrizi (risankizumab) evidence",
     "content_type": "Disease-education article", "owner": "Marcus Reed", "reviewer": "Dr. Helen Carter",
     "pub_url": "https://www.skyrizi.com/psoriatic-arthritis/skin-and-joint-guide", "position": "SECOND_LINE",
     "action": "Publish an AI-optimised PsA guide covering combined skin + joint control with Skyrizi evidence.",
     "rationale": "Skyrizi is under-cited for combined skin/joint PsA questions; Cosentyx leads AI answers.",
     "queries": ["best psoriatic arthritis treatment for skin and joints",
                 "skyrizi for psoriatic arthritis", "risankizumab psoriatic arthritis results",
                 "psoriatic arthritis biologic comparison"]},

    # 3) RA / Humira — WORSENED / HIGH
    {"slug": "ra-humira-worsened", "kind": "completed", "qid": "Q-ACTDEMO-RA-2",
     "brand": "Humira", "indication": "Rheumatoid Arthritis", "persona": "Patient",
     "competitor": "Rinvoq biosimilars", "base": WORSE_BASE, "post": WORSE_POST, "clean": True,
     "base_age": 12, "post_age": 1, "pub_age": 13,
     "title": "Refresh the Humira RA 'loss of efficacy' page positioning vs. newer options",
     "content_type": "Brand page refresh", "owner": "Priya Shah", "reviewer": "Dr. Helen Carter",
     "pub_url": "https://www.humira.com/rheumatoid-arthritis/still-working", "position": "FIRST_LINE_RECOMMENDED",
     "action": "Refresh the Humira RA page to reinforce positioning against newer orals and biosimilars.",
     "rationale": "Humira's AI positioning is slipping as biosimilar and JAK content gains citations.",
     "queries": ["is humira still the best for rheumatoid arthritis",
                 "humira vs biosimilar rheumatoid arthritis",
                 "humira losing effectiveness rheumatoid arthritis",
                 "alternatives to humira rheumatoid arthritis"]},

    # 4) PsA / Rinvoq — NO_CLEAR_CHANGE / HIGH
    {"slug": "psa-rinvoq-nochange", "kind": "completed", "qid": "Q-ACTDEMO-PSA-2",
     "brand": "Rinvoq", "indication": "Psoriatic Arthritis", "persona": "Prospect",
     "competitor": "Cosentyx", "base": FLAT_BASE, "post": FLAT_POST, "clean": True,
     "base_age": 10, "post_age": 1, "pub_age": 11,
     "title": "Pilot: PsA 'pill vs. injection' comparison explainer featuring Rinvoq",
     "content_type": "Comparison explainer", "owner": "Marcus Reed", "reviewer": "Dr. Helen Carter",
     "pub_url": "https://www.rinvoq.com/psoriatic-arthritis/pill-vs-injection", "position": "AMONG_OPTIONS",
     "action": "Pilot a PsA oral-vs-injectable explainer to test whether it lifts Rinvoq consideration.",
     "rationale": "Rinvoq consideration for PsA 'pill vs injection' queries is mid-pack; test a lightweight explainer.",
     "queries": ["psoriatic arthritis pill vs injection",
                 "rinvoq vs biologic injection psoriatic arthritis",
                 "oral treatment psoriatic arthritis", "jak inhibitor psoriatic arthritis"]},

    # 5) PsA / Humira — in-flight MEASURING (POST_RUNNING) — 'Measure now' reveals result
    {"slug": "psa-humira-measuring", "kind": "inflight", "qid": "Q-ACTDEMO-PSA-3",
     "brand": "Humira", "indication": "Psoriatic Arthritis", "persona": "Patient",
     "competitor": "Cosentyx", "base": LIVE_BASE, "post": LIVE_POST,
     "base_age": 16, "post_age": 0.05, "pub_age": 17,
     "title": "Publish a PsA 'which biologic to ask about' patient guide featuring Humira",
     "content_type": "Patient guide", "owner": "Priya Shah", "reviewer": "Dr. Helen Carter",
     "pub_url": "https://www.humira.com/psoriatic-arthritis/talk-to-your-doctor", "position": "NOT_MENTIONED",
     "action": "Publish a PsA patient guide to raise Humira consideration for 'which biologic' questions.",
     "rationale": "Humira is under-mentioned for open PsA 'which biologic' questions.",
     "queries": ["which biologic for psoriatic arthritis", "humira for psoriatic arthritis",
                 "psoriatic arthritis biologic options", "talk to doctor psoriatic arthritis treatment"]},

    # 6) RA / Rinvoq — PROPOSED (discovery only) — populates the 'Open' tile
    {"slug": "ra-rinvoq-proposed", "kind": "proposed", "qid": "Q-ACTDEMO-RA-3",
     "brand": "Rinvoq", "indication": "Rheumatoid Arthritis", "persona": "Prospect",
     "competitor": "Enbrel", "disc": PROP_DISC, "disc_age": 1,
     "title": "Proposed: 'Newly diagnosed RA — options beyond methotrexate' explainer featuring Rinvoq",
     "content_type": "Educational explainer", "owner": None, "reviewer": None, "position": "NOT_MENTIONED",
     "action": "Draft an AI-optimised explainer for newly-diagnosed RA covering options beyond methotrexate.",
     "rationale": "Rinvoq is rarely surfaced for newly-diagnosed RA discovery questions.",
     "queries": ["newly diagnosed rheumatoid arthritis treatment options",
                 "rheumatoid arthritis options beyond methotrexate",
                 "first biologic for rheumatoid arthritis", "rinvoq for new rheumatoid arthritis diagnosis"]},

    # 7) RA / Humira — INCONCLUSIVE / LOW (small single-platform sample)
    {"slug": "ra-humira-inconclusive", "kind": "completed", "qid": "Q-ACTDEMO-RA-4",
     "brand": "Humira", "indication": "Rheumatoid Arthritis", "persona": "Patient",
     "competitor": "Orencia", "base": SMALL_BASE, "post": SMALL_POST, "platforms": PLATFORMS_SMALL,
     "base_age": 9, "post_age": 1, "pub_age": 10,
     "title": "Small pilot: single-platform test of a Humira RA 'failed biologics' snippet",
     "content_type": "Snippet test", "owner": "Marcus Reed", "reviewer": "Dr. Helen Carter",
     "pub_url": "https://www.humira.com/rheumatoid-arthritis/after-other-biologics", "position": "NOT_MENTIONED",
     "action": "Run a minimal single-platform snippet test before committing to a full asset.",
     "rationale": "Exploratory: too few samples to conclude — demonstrates the small-sample guardrail.",
     "queries": ["rheumatoid arthritis treatment after biologics failed", "humira after other biologics",
                 "what if biologics don't work rheumatoid arthritis", "next step failed biologic rheumatoid arthritis"]},
]


async def _seed(db) -> None:
    await _wipe(db)

    # Demo questions (APPROVED, active) so every drill-down shows a real question.
    for qid, (persona, indication, brand, domain, text) in QUESTIONS.items():
        db.add(Question(
            question_id=qid, question_text=text, persona=persona, therapeutic_area=TA,
            indication=indication, disease=indication, brand_focus=brand, domain=domain,
            approval_status="APPROVED", approver_name="Dr. Helen Carter, Medical Affairs",
            active=True, version=1,
        ))
    # Confounder model release — dated inside scenario #2's measurement window only.
    db.add(ModelReleaseLog(
        target_platform="GPT-4o", release_date=dt(20).date(), version="gpt-4o-2024-11-20",
        release_notes="[DEMO] GPT-4o refresh during the PsA measurement window.",
        url=RELEASE_URL, source="seed",
    ))
    await db.commit()

    # Anchor the 'clean' scenarios in a release-free window so they land on HIGH confidence,
    # regardless of the real auto-detected model releases already in this DB.
    cb, cp = await _clean_release_free_ages(db)
    for spec in SPECS:
        if spec.get("clean"):
            spec["base_age"], spec["post_age"], spec["pub_age"] = cb, cp, cb + 1

    # Build every scenario (no commit yet → objects stay live for compute_result).
    completed: list[tuple[dict, object, object, object]] = []
    for spec in SPECS:
        interv, base, post = _scenario(db, spec)
        if base is not None and post is not None:
            completed.append((spec, interv, base, post))

    # Compute the before/after result for each COMPLETED scenario via the REAL engine.
    summary: dict[str, tuple[str, str]] = {}
    for spec, interv, base, post in completed:
        result = await measurement.compute_result(
            db, intervention=interv, baseline=base, post=post, commit=False)
        result.id = f"{MARK}-rr-{spec['slug']}"
        result.measured_at = dt(spec["post_age"])
        interv.outcome_status = result.outcome_status
        db.add(_event(spec["slug"], "MEASUREMENT_COMPLETED", dt(spec["post_age"]),
                      prev="MEASURING", new="COMPLETED",
                      meta={"outcome": result.outcome_status, "confidence": result.confidence,
                            "post_response_count": post.response_count}))
        db.add(_event(spec["slug"], "OUTCOME_RECORDED", dt(spec["post_age"]),
                      meta={"outcome": result.outcome_status, "confidence": result.confidence,
                            "interpretation": result.interpretation}))
        summary[spec["slug"]] = (result.outcome_status, result.confidence)

    await db.commit()  # single commit for all scenarios, results and events

    print(f"Activation & Impact demo seeded — {len(SPECS)} interventions "
          f"({len(QUESTIONS)} questions):")
    for spec in SPECS:
        tag = {"completed": "COMPLETED", "inflight": "MEASURING (in-flight)",
               "proposed": "PROPOSED"}[spec["kind"]]
        extra = ""
        if spec["slug"] in summary:
            outcome, conf = summary[spec["slug"]]
            extra = f" -> {outcome} / {conf} confidence"
        print(f"  - [{tag}] {spec['indication']} / {spec['brand']}{extra}")
    print("Tip: open the in-flight PsA/Humira intervention and click 'Measure now' to "
          "finalize its post snapshot and reveal the result live.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed (or wipe) the Activation & Impact Rheumatology demo.")
    parser.add_argument("--wipe", action="store_true",
                        help="Remove all demo data (actdemo-* / Q-ACTDEMO-*) and exit.")
    args = parser.parse_args()

    async def run() -> None:
        await init_db()
        async with AsyncSessionLocal() as db:
            if args.wipe:
                counts = await _wipe(db)
                print("Activation & Impact demo wiped:",
                      {k: v for k, v in counts.items() if v} or "nothing to remove")
                return
            await _seed(db)

    asyncio.run(run())


if __name__ == "__main__":
    main()




