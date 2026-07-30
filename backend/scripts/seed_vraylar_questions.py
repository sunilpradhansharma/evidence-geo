"""Backfill the Question Repository with the Neuroscience therapeutic area.

Adds an APPROVED, active question bank (~40 questions) for the Neuroscience area
so it is monitorable end-to-end (Pipeline -> Results -> Analytics), not just
harvestable.

  - Neuroscience  (focus brand: Vraylar [cariprazine], AbbVie)

Across 3 personas (Prospect, Patient, Provider) x 5 domains. Real brand names,
contains NO PII (SE-001). Questions use deterministic ids (Q-VRAY-###) so this
loader is IDEMPOTENT: it inserts only the rows whose id is not already present
and is safe to re-run against an existing prod database (the main
``seed_questions.py`` skips a non-empty DB, so this separate loader is required,
mirroring ``seed_rheumatology_questions.py``).

Run:  python -m scripts.seed_vraylar_questions
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
THERAPEUTIC_AREA = "Neuroscience"
BRAND = "Vraylar"  # single focus brand for this area (cariprazine, AbbVie)

# (persona, domain, question_text). therapeutic_area (Neuroscience) and brand_focus
# (Vraylar) are constant for this area. Covers schizophrenia, bipolar I disorder
# (mania + bipolar depression), and adjunctive major depressive disorder.
VRAYLAR_QUESTIONS: list[tuple[str, str, str]] = [
    # ============ PROSPECT (exploring / newly diagnosed / choosing therapy) ============
    ("Prospect", "General", "I was just diagnosed with bipolar I disorder. Is Vraylar something I should ask my psychiatrist about?"),
    ("Prospect", "Efficacy", "How well does Vraylar work for bipolar depression, not just the manic episodes?"),
    ("Prospect", "Safety", "What are the most common side effects of Vraylar I should know about before starting it?"),
    ("Prospect", "Comparative", "For schizophrenia, how does Vraylar compare to Abilify since both are partial agonists?"),
    ("Prospect", "Access", "How much does Vraylar cost per month without insurance, and is there a savings card?"),
    ("Prospect", "General", "My doctor suggested adding Vraylar to my antidepressant. Why would I add an antipsychotic for depression?"),
    ("Prospect", "Comparative", "Is Vraylar or Latuda generally considered better for bipolar depression?"),
    ("Prospect", "Efficacy", "How long does Vraylar usually take to start improving manic symptoms?"),
    ("Prospect", "Safety", "I've heard antipsychotics can cause restlessness (akathisia). How likely is that with Vraylar?"),
    ("Prospect", "Comparative", "Does Vraylar cause less weight gain than Zyprexa or Seroquel?"),
    ("Prospect", "Access", "Are there patient assistance programs to help pay for Vraylar if I'm uninsured?"),
    ("Prospect", "General", "What conditions is Vraylar actually FDA-approved to treat?"),
    ("Prospect", "Safety", "Is it safe to take Vraylar long term for schizophrenia?"),

    # ============ PATIENT (already on therapy: experiential, practical) ============
    ("Patient", "Efficacy", "I've been on Vraylar for my bipolar disorder for two months and still feel low. Should I give it more time?"),
    ("Patient", "Safety", "Since starting Vraylar I feel restless and can't sit still. Is that a side effect, and what can I do about it?"),
    ("Patient", "Comparative", "My Vraylar isn't controlling my symptoms well anymore. Would switching to Rexulti help?"),
    ("Patient", "Safety", "Can I drink alcohol occasionally while taking Vraylar?"),
    ("Patient", "Access", "My insurance is requiring a prior authorization for Vraylar. How do I get it approved?"),
    ("Patient", "General", "Do I need to take Vraylar with food, and does the time of day matter?"),
    ("Patient", "Efficacy", "My Vraylar dose was just increased. Why does it take so long to feel the full effect?"),
    ("Patient", "Safety", "I've gained a little weight on Vraylar. Should I be getting my blood sugar and cholesterol checked?"),
    ("Patient", "Comparative", "I was switched from Seroquel to Vraylar. What differences should I expect?"),
    ("Patient", "General", "If I miss a dose of Vraylar, what should I do?"),
    ("Patient", "Access", "My copay for Vraylar jumped this year. What assistance options are available?"),
    ("Patient", "Safety", "Can I stop Vraylar suddenly once I feel better, or do I need to taper off it?"),
    ("Patient", "Safety", "I'm pregnant and taking Vraylar for bipolar disorder. Is it safe to continue?"),

    # ============ PROVIDER (clinician / technical) ============
    ("Provider", "Efficacy", "What is the evidence for cariprazine (Vraylar) as adjunctive therapy in major depressive disorder?"),
    ("Provider", "Safety", "What is the current guidance on akathisia and extrapyramidal symptoms with cariprazine, and how should they be managed?"),
    ("Provider", "Comparative", "How does cariprazine compare to brexpiprazole (Rexulti) for adjunctive treatment of major depressive disorder?"),
    ("Provider", "General", "What is the recommended starting dose and titration of cariprazine across schizophrenia, bipolar mania, and bipolar depression?"),
    ("Provider", "Safety", "How does cariprazine's long half-life and its active metabolite (DDCAR) affect dose changes and washout?"),
    ("Provider", "Comparative", "For bipolar depression, how does cariprazine compare to lurasidone (Latuda) and lumateperone (Caplyta) on efficacy and tolerability?"),
    ("Provider", "Efficacy", "What response and remission rates were observed with cariprazine in the pivotal bipolar depression trials?"),
    ("Provider", "Safety", "What metabolic monitoring (weight, glucose, lipids) is recommended for patients on cariprazine?"),
    ("Provider", "Comparative", "When would you choose cariprazine over aripiprazole (Abilify) given both are dopamine partial agonists?"),
    ("Provider", "Access", "What prior-authorization criteria typically gate cariprazine versus generic atypical antipsychotics?"),
    ("Provider", "General", "What is cariprazine's receptor pharmacology, and how does its D3 preference distinguish it from other partial agonists?"),
    ("Provider", "Safety", "What is the boxed warning regarding increased mortality in elderly patients with dementia-related psychosis for cariprazine?"),
    ("Provider", "Comparative", "For a patient with predominant negative symptoms of schizophrenia, what evidence supports cariprazine versus risperidone?"),
    ("Provider", "Safety", "What is the risk of tardive dyskinesia with cariprazine, and how should patients be monitored?"),
]


async def seed() -> None:
    await init_db()
    async with AsyncSessionLocal() as db:
        existing = set((await db.execute(select(Question.question_id))).scalars().all())

        inserted = 0
        skipped = 0
        for i, (persona, domain, text) in enumerate(VRAYLAR_QUESTIONS, start=1):
            qid = f"Q-VRAY-{i:03d}"
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
                brand_focus=BRAND,
                domain=domain,
                intent_type=intent_type,
                approval_status="APPROVED",
                approver_name=APPROVER,
                active=True,
                version=1,
            ))
            inserted += 1
        await db.commit()

        total = len(VRAYLAR_QUESTIONS)
        print(f"Neuroscience (Vraylar) backfill complete: {inserted} inserted, {skipped} already present (of {total} defined).")


if __name__ == "__main__":
    asyncio.run(seed())
