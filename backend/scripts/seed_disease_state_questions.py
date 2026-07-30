"""Seed a DISEASE-STATE / PRE-LAUNCH question bank (FR-108a).

Adds an APPROVED, active, brand-less landscape question set so the new
Disease-State Monitoring mode is demonstrable end-to-end (Run Analysis ->
Results -> Analytics) with NO primary AbbVie brand asset.

Scenario: the **Obesity / GLP-1 landscape** — a realistic pre-launch intelligence
use case (a team monitoring the competitive field before entering it). All brand
names are REAL, marketed anti-obesity therapies used purely as landscape
competitors (SE-007: content lives in data, not code); contains NO PII (SE-001):

  - semaglutide: Wegovy, Ozempic (Novo Nordisk)
  - tirzepatide: Zepbound, Mounjaro (Eli Lilly)
  - liraglutide: Saxenda (Novo Nordisk)
  - Contrave (naltrexone/bupropion), Qsymia (phentermine/topiramate)

Each question carries monitoring_mode=DISEASE_STATE, brand_focus=None, and a
competitor_focus tag list. Deterministic ids (Q-DS-OBESITY-###) make this loader
IDEMPOTENT and safe to re-run.

Run:  python -m scripts.seed_disease_state_questions
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.agent.intent_classifier import classify_by_rules  # noqa: E402
from app.models.database import AsyncSessionLocal, init_db  # noqa: E402
from app.models.question import Question  # noqa: E402
from sqlalchemy import select  # noqa: E402

APPROVER = "Dr. Priya Nair, Medical Affairs (Pre-Launch)"
THERAPEUTIC_AREA = "Obesity"

# The competitive field this pre-launch landscape monitors (real marketed agents).
COMPETITOR_FOCUS = [
    "Wegovy", "Ozempic", "Zepbound", "Mounjaro", "Saxenda", "Contrave", "Qsymia",
]

# (persona, domain, question_text) — NO brand_focus (landscape / pre-launch).
DISEASE_STATE_QUESTIONS: list[tuple[str, str, str]] = [
    # ============ PROSPECT (exploring the treatment landscape) ============
    ("Prospect", "General", "What are the main prescription medications available today for weight loss?"),
    ("Prospect", "Comparative", "How do the newer GLP-1 weight-loss drugs compare to each other for people with obesity?"),
    ("Prospect", "Efficacy", "Which obesity medications produce the most weight loss on average in clinical trials?"),
    ("Prospect", "Safety", "What are the most common side effects across the GLP-1 weight-loss medications?"),
    ("Prospect", "Comparative", "Is a weekly injection or a daily pill generally preferred for treating obesity?"),
    ("Prospect", "Access", "Roughly how much do the leading weight-loss drugs cost per month without insurance?"),
    ("Prospect", "General", "How do doctors decide which weight-loss medication to start someone on?"),
    ("Prospect", "Comparative", "For someone with obesity and type 2 diabetes, which classes of weight-loss drugs are usually considered first?"),

    # ============ PATIENT (navigating options within the landscape) ============
    ("Patient", "Efficacy", "I'm on a GLP-1 medication for weight loss but my results have plateaued. What other options exist?"),
    ("Patient", "Safety", "Which weight-loss medications are least likely to cause nausea?"),
    ("Patient", "Comparative", "If I can't tolerate one GLP-1 drug, is switching to a different one in the same class worth trying?"),
    ("Patient", "Access", "My insurance won't cover the injectable weight-loss drugs. What alternatives should I ask about?"),
    ("Patient", "General", "Do I have to stay on obesity medication for life, or can I stop once I reach my goal weight?"),
    ("Patient", "Safety", "Are there weight-loss medications I should avoid if I have a history of pancreatitis?"),
    ("Patient", "Comparative", "How do the non-GLP-1 weight-loss pills compare to the injectable options for someone like me?"),

    # ============ PROVIDER (clinical landscape assessment) ============
    ("Provider", "Efficacy", "What percentage total body weight reduction is reported across the leading anti-obesity pharmacotherapies?"),
    ("Provider", "Comparative", "How does tirzepatide compare to semaglutide on weight-loss efficacy in head-to-head or indirect data?"),
    ("Provider", "Safety", "What is the current guidance on GLP-1 and GIP/GLP-1 agents regarding thyroid C-cell tumor and pancreatitis risk?"),
    ("Provider", "General", "What does current guideline positioning look like for pharmacotherapy in the management of obesity?"),
    ("Provider", "Comparative", "When would you select a combination agent such as naltrexone/bupropion over an incretin-based therapy?"),
    ("Provider", "Efficacy", "What cardiovascular outcome data currently exist for the anti-obesity medication class?"),
    ("Provider", "Safety", "What are the key contraindications and monitoring requirements across the incretin-based anti-obesity agents?"),
    ("Provider", "Access", "What prior-authorization criteria typically gate coverage of anti-obesity pharmacotherapy?"),
    ("Provider", "Comparative", "For a treatment-naive patient with obesity, how would you sequence the available pharmacologic options?"),
]


async def seed() -> None:
    await init_db()
    competitor_json = json.dumps(COMPETITOR_FOCUS)
    async with AsyncSessionLocal() as db:
        existing = set((await db.execute(select(Question.question_id))).scalars().all())

        inserted = 0
        skipped = 0
        for i, (persona, domain, text) in enumerate(DISEASE_STATE_QUESTIONS, start=1):
            qid = f"Q-DS-OBESITY-{i:03d}"
            if qid in existing:
                skipped += 1
                continue
            intent_result = classify_by_rules(persona, domain, text)
            intent_type = intent_result.intent if intent_result else None
            db.add(Question(
                question_id=qid,
                question_text=text,
                persona=persona,
                therapeutic_area=THERAPEUTIC_AREA,
                brand_focus=None,                     # FR-108a: no primary brand asset
                monitoring_mode="DISEASE_STATE",
                competitor_focus=competitor_json,
                domain=domain,
                intent_type=intent_type,
                approval_status="APPROVED",
                approver_name=APPROVER,
                active=True,
                version=1,
            ))
            inserted += 1
        await db.commit()

        total = len(DISEASE_STATE_QUESTIONS)
        print(f"Disease-state (Obesity landscape) seed complete: {inserted} inserted, "
              f"{skipped} already present (of {total} defined).")


if __name__ == "__main__":
    asyncio.run(seed())
