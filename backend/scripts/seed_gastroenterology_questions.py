"""Backfill the Question Repository with the Gastroenterology therapeutic area.

Adds an APPROVED, active question bank (~40 questions) for Gastroenterology so the
area is monitorable end-to-end (Pipeline -> Results -> Analytics), not just harvestable.

  - Gastroenterology  (focus brands: Skyrizi [risankizumab], Rinvoq [upadacitinib],
                       Humira [adalimumab])

Across 3 personas (Prospect, Patient, Provider) x 5 domains, spanning the two
Gastroenterology indications in brands.yaml: Crohn's Disease and Ulcerative Colitis.
Real brand names, contains NO PII (SE-001).

Every row carries an explicit ``disease`` — see the note in
``scripts/seed_dermatology_questions.py`` for why. It matters more here than anywhere
else: induction and maintenance are separate canonical outcomes in IBD, so a row that
cannot be resolved to UC or CD cannot be compared against the right evidence at all.

Questions use deterministic ids (Q-GI-###) so this loader is IDEMPOTENT: it inserts
only the rows whose id is not already present and is safe to re-run against an existing
prod database (the main ``seed_questions.py`` skips a non-empty DB, so this separate
loader is required, mirroring ``seed_rheumatology_questions.py``).

Run:  python -m scripts.seed_gastroenterology_questions
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.agent.intent_classifier import classify_by_rules  # noqa: E402
from app.models.database import AsyncSessionLocal, init_db  # noqa: E402
from app.models.question import Question  # noqa: E402
from sqlalchemy import select  # noqa: E402

APPROVER = "Dr. Helen Carter, Medical Affairs"
THERAPEUTIC_AREA = "Gastroenterology"

CD = "Crohn's Disease"
UC = "Ulcerative Colitis"

# (persona, domain, brand_focus, disease, question_text)
GI_QUESTIONS: list[tuple[str, str, str, str, str]] = [
    # ============ PROSPECT (exploring / newly diagnosed / choosing therapy) ============
    ("Prospect", "General", "Skyrizi", CD, "I was just diagnosed with Crohn's disease. What treatments come after steroids and immunomodulators?"),
    ("Prospect", "Efficacy", "Skyrizi", CD, "How likely is Skyrizi to get Crohn's disease into remission?"),
    ("Prospect", "Comparative", "Skyrizi", CD, "For Crohn's disease, how does Skyrizi compare to Stelara?"),
    ("Prospect", "Safety", "Skyrizi", CD, "What are the main risks of starting Skyrizi for Crohn's?"),
    ("Prospect", "Access", "Skyrizi", CD, "Does insurance usually require a prior authorization for Skyrizi in Crohn's disease?"),
    ("Prospect", "General", "Rinvoq", UC, "My ulcerative colitis is not controlled on mesalamine. Is a pill like Rinvoq an option?"),
    ("Prospect", "Efficacy", "Rinvoq", UC, "How quickly does Rinvoq work for ulcerative colitis symptoms?"),
    ("Prospect", "Comparative", "Rinvoq", UC, "For ulcerative colitis, is an oral like Rinvoq better than an infusion like Entyvio?"),
    ("Prospect", "Safety", "Rinvoq", UC, "Should the JAK inhibitor warnings worry me if I take Rinvoq for ulcerative colitis?"),
    ("Prospect", "General", "Humira", CD, "My doctor suggested Humira for Crohn's disease. How does a TNF blocker work in the gut?"),
    ("Prospect", "Comparative", "Humira", CD, "Is Humira or Skyrizi generally tried first for Crohn's disease?"),
    ("Prospect", "Efficacy", "Humira", UC, "What are the chances Humira puts my ulcerative colitis into remission?"),
    ("Prospect", "Access", "Humira", CD, "Are adalimumab biosimilars cheaper for Crohn's disease, and do they work as well?"),

    # ============ PATIENT (already on therapy: experiential, practical) ============
    ("Patient", "Efficacy", "Skyrizi", CD, "I have been on Skyrizi for Crohn's for eight months and symptoms are creeping back. What should I ask my GI?"),
    ("Patient", "General", "Skyrizi", CD, "What is the difference between my Skyrizi infusions and the on-body injector I switch to later?"),
    ("Patient", "Safety", "Skyrizi", CD, "Is it safe to stay on Skyrizi for Crohn's if I have a stomach bug?"),
    ("Patient", "Access", "Skyrizi", CD, "My infusion copay for Skyrizi changed this year. What help is available?"),
    ("Patient", "Efficacy", "Rinvoq", UC, "How long should I give Rinvoq before deciding it is not controlling my ulcerative colitis?"),
    ("Patient", "Safety", "Rinvoq", UC, "Do I need regular blood tests while taking Rinvoq for ulcerative colitis?"),
    ("Patient", "General", "Rinvoq", UC, "What happens when I step down from the induction dose of Rinvoq for UC?"),
    ("Patient", "Comparative", "Rinvoq", UC, "My UC is flaring on Rinvoq. Would Entyvio or Omvoh be a better fit?"),
    ("Patient", "Efficacy", "Humira", CD, "My Humira used to control my Crohn's but is not working as well. Is that antibodies?"),
    ("Patient", "Safety", "Humira", CD, "Can I get a colonoscopy while I am on Humira for Crohn's?"),
    ("Patient", "General", "Humira", UC, "Can I travel with my Humira pens if I have ulcerative colitis and flare unpredictably?"),
    ("Patient", "Access", "Humira", UC, "My plan is forcing a biosimilar switch for my ulcerative colitis. What should I expect?"),
    ("Patient", "Safety", "Humira", CD, "Is it safe to get live vaccines while on Humira for Crohn's disease?"),

    # ============ PROVIDER (clinician / technical) ============
    ("Provider", "Efficacy", "Skyrizi", CD, "What clinical remission and endoscopic response rates support risankizumab at week 12 induction in Crohn's disease?"),
    ("Provider", "Comparative", "Skyrizi", CD, "How does risankizumab compare with ustekinumab for endoscopic outcomes in biologic-experienced Crohn's disease?"),
    ("Provider", "General", "Skyrizi", CD, "What is the IV induction and subcutaneous maintenance regimen for risankizumab in Crohn's disease?"),
    ("Provider", "Efficacy", "Skyrizi", UC, "What week 52 maintenance remission data support risankizumab in ulcerative colitis?"),
    ("Provider", "Efficacy", "Rinvoq", UC, "What clinical remission rates support upadacitinib at week 8 induction in moderate-to-severe ulcerative colitis?"),
    ("Provider", "Safety", "Rinvoq", UC, "What VTE and MACE monitoring is recommended for upadacitinib in an ulcerative colitis population?"),
    ("Provider", "General", "Rinvoq", UC, "How should upadacitinib be stepped from 45 mg induction to maintenance dosing in ulcerative colitis?"),
    ("Provider", "Comparative", "Rinvoq", CD, "In Crohn's disease, how does upadacitinib compare with risankizumab for endoscopic response?"),
    ("Provider", "Efficacy", "Humira", CD, "What induction and maintenance remission rates support adalimumab in Crohn's disease?"),
    ("Provider", "General", "Humira", CD, "What is the recommended adalimumab loading regimen for Crohn's disease?"),
    ("Provider", "Safety", "Humira", UC, "How should loss of response and anti-drug antibodies to adalimumab be evaluated in ulcerative colitis?"),
    ("Provider", "Comparative", "Humira", UC, "For a biologic-naive ulcerative colitis patient, what favours vedolizumab over a TNF inhibitor?"),
    ("Provider", "Access", "Humira", CD, "What documentation typically supports prior authorization for a biologic in Crohn's disease?"),
    ("Provider", "Comparative", "Skyrizi", UC, "How do IL-23 inhibitors compare with S1P modulators for maintenance of remission in ulcerative colitis?"),
]


async def seed() -> None:
    await init_db()
    async with AsyncSessionLocal() as db:
        existing = set((await db.execute(select(Question.question_id))).scalars().all())

        inserted = 0
        skipped = 0
        for i, (persona, domain, brand, disease, text) in enumerate(GI_QUESTIONS, start=1):
            qid = f"Q-GI-{i:03d}"
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
                disease=disease,
                brand_focus=brand,
                domain=domain,
                intent_type=intent_type,
                approval_status="APPROVED",
                approver_name=APPROVER,
                active=True,
                version=1,
            ))
            inserted += 1
        await db.commit()

        total = len(GI_QUESTIONS)
        print(f"Gastroenterology backfill complete: {inserted} inserted, {skipped} already present (of {total} defined).")


if __name__ == "__main__":
    asyncio.run(seed())
