"""Backfill the Question Repository with the Dermatology therapeutic area.

Adds an APPROVED, active question bank (~40 questions) for Dermatology so the area
is monitorable end-to-end (Pipeline -> Results -> Analytics), not just harvestable.

  - Dermatology  (focus brands: Skyrizi [risankizumab], Humira [adalimumab],
                  Rinvoq [upadacitinib])

Across 3 personas (Prospect, Patient, Provider) x 5 domains, spanning the three
Dermatology indications in brands.yaml: Plaque Psoriasis, Atopic Dermatitis and
Hidradenitis Suppurativa. Real brand names, contains NO PII (SE-001).

Every row carries an explicit ``disease``. This is deliberate and is the difference
between this loader and the older ones: Humira, Skyrizi and Rinvoq each span three
specialties, so without a disease a row cannot be re-derived or verified later — it
is exactly the gap ``scripts/backfill_question_disease.py`` had to close after the
fact. The disease is also what scopes scoring to the right competitor set (an Atopic
Dermatitis question against Dupixent/Cibinqo, not against Entyvio).

Questions use deterministic ids (Q-DERM-###) so this loader is IDEMPOTENT: it inserts
only the rows whose id is not already present and is safe to re-run against an existing
prod database (the main ``seed_questions.py`` skips a non-empty DB, so this separate
loader is required, mirroring ``seed_rheumatology_questions.py``).

Run:  python -m scripts.seed_dermatology_questions
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
THERAPEUTIC_AREA = "Dermatology"

PSO = "Plaque Psoriasis"
AD = "Atopic Dermatitis"
HS = "Hidradenitis Suppurativa"

# (persona, domain, brand_focus, disease, question_text)
DERM_QUESTIONS: list[tuple[str, str, str, str, str]] = [
    # ============ PROSPECT (exploring / newly diagnosed / choosing therapy) ============
    ("Prospect", "General", "Skyrizi", PSO, "I was just diagnosed with plaque psoriasis. What are my options beyond topical creams?"),
    ("Prospect", "Efficacy", "Skyrizi", PSO, "How much skin clearance can I realistically expect from Skyrizi for plaque psoriasis?"),
    ("Prospect", "Comparative", "Skyrizi", PSO, "For plaque psoriasis, how does Skyrizi compare to Cosentyx?"),
    ("Prospect", "Safety", "Skyrizi", PSO, "What side effects should I know about before starting Skyrizi for psoriasis?"),
    ("Prospect", "Access", "Skyrizi", PSO, "Is Skyrizi usually covered by insurance for moderate plaque psoriasis?"),
    ("Prospect", "General", "Rinvoq", AD, "My eczema is not responding to steroid creams. Is a pill like Rinvoq an option?"),
    ("Prospect", "Comparative", "Rinvoq", AD, "For atopic dermatitis, is Rinvoq or Dupixent likely to work faster?"),
    ("Prospect", "Safety", "Rinvoq", AD, "I read about JAK inhibitor warnings. How risky is Rinvoq for eczema?"),
    ("Prospect", "Efficacy", "Rinvoq", AD, "How much itch relief do people get from Rinvoq for atopic dermatitis?"),
    ("Prospect", "General", "Humira", HS, "I was diagnosed with hidradenitis suppurativa. What treatments actually help?"),
    ("Prospect", "Efficacy", "Humira", HS, "How well does Humira reduce flare-ups in hidradenitis suppurativa?"),
    ("Prospect", "Comparative", "Humira", HS, "For hidradenitis suppurativa, how does Humira compare to Cosentyx?"),
    ("Prospect", "Access", "Humira", PSO, "What does Humira cost for psoriasis if my insurance has a high deductible?"),

    # ============ PATIENT (already on therapy: experiential, practical) ============
    ("Patient", "Efficacy", "Skyrizi", PSO, "My psoriasis is creeping back in the last few weeks before my Skyrizi dose. Is that expected?"),
    ("Patient", "Safety", "Skyrizi", PSO, "Do I need to pause Skyrizi before a scheduled surgery?"),
    ("Patient", "General", "Skyrizi", PSO, "How do I store my Skyrizi pen when I travel for work?"),
    ("Patient", "Comparative", "Skyrizi", PSO, "My psoriasis is only partly clear on Skyrizi. Would Tremfya do better?"),
    ("Patient", "Access", "Skyrizi", PSO, "My plan moved Skyrizi to a specialty tier. What assistance is available?"),
    ("Patient", "Safety", "Rinvoq", AD, "Since starting Rinvoq for eczema I keep getting cold sores. Should I be concerned?"),
    ("Patient", "Efficacy", "Rinvoq", AD, "How long before Rinvoq calms the itching from my atopic dermatitis?"),
    ("Patient", "General", "Rinvoq", AD, "Can I use my usual moisturizers and steroid creams while taking Rinvoq?"),
    ("Patient", "Safety", "Rinvoq", AD, "Do I need regular lab work while on Rinvoq for eczema?"),
    ("Patient", "Efficacy", "Humira", HS, "I have been on Humira for hidradenitis suppurativa for six months with only mild improvement. What now?"),
    ("Patient", "Safety", "Humira", HS, "My HS lesions are draining and I am due for my Humira injection. Should I still take it?"),
    ("Patient", "General", "Humira", PSO, "Can I get my psoriasis biologic and a flu shot in the same week?"),
    ("Patient", "Access", "Humira", PSO, "Would switching to an adalimumab biosimilar for my psoriasis save me money?"),

    # ============ PROVIDER (clinician / technical) ============
    ("Provider", "Efficacy", "Skyrizi", PSO, "What PASI 90 and PASI 100 rates support risankizumab at week 16 in moderate-to-severe plaque psoriasis?"),
    ("Provider", "Comparative", "Skyrizi", PSO, "How does risankizumab compare with guselkumab on durability of PASI 90 response in plaque psoriasis?"),
    ("Provider", "Safety", "Skyrizi", PSO, "What is the serious infection and malignancy signal for IL-23 inhibitors in long-term psoriasis extension data?"),
    ("Provider", "General", "Skyrizi", PSO, "What is the induction and maintenance dosing interval for risankizumab in plaque psoriasis?"),
    ("Provider", "Access", "Skyrizi", PSO, "What step therapy do commercial plans typically require before approving an IL-23 inhibitor for psoriasis?"),
    ("Provider", "Efficacy", "Rinvoq", AD, "What EASI 75 and IGA 0/1 rates support upadacitinib at week 16 in moderate-to-severe atopic dermatitis?"),
    ("Provider", "Comparative", "Rinvoq", AD, "In atopic dermatitis, how does upadacitinib compare with abrocitinib and dupilumab on speed of itch response?"),
    ("Provider", "Safety", "Rinvoq", AD, "What baseline screening and ongoing monitoring is recommended before starting upadacitinib for atopic dermatitis?"),
    ("Provider", "General", "Rinvoq", AD, "When is the 30 mg rather than 15 mg upadacitinib dose appropriate in atopic dermatitis?"),
    ("Provider", "Efficacy", "Humira", HS, "What HiSCR response rates support adalimumab at week 12 in moderate-to-severe hidradenitis suppurativa?"),
    ("Provider", "Comparative", "Humira", HS, "How do adalimumab and secukinumab compare for hidradenitis suppurativa in the absence of head-to-head trials?"),
    ("Provider", "Safety", "Humira", HS, "What tuberculosis screening is required before initiating adalimumab in a patient with hidradenitis suppurativa?"),
    ("Provider", "General", "Humira", HS, "What is the loading and maintenance schedule for adalimumab in hidradenitis suppurativa?"),
    ("Provider", "Comparative", "Humira", PSO, "For a biologic-naive plaque psoriasis patient, what favours a TNF inhibitor over an IL-23 inhibitor?"),
]


async def seed() -> None:
    await init_db()
    async with AsyncSessionLocal() as db:
        existing = set((await db.execute(select(Question.question_id))).scalars().all())

        inserted = 0
        skipped = 0
        for i, (persona, domain, brand, disease, text) in enumerate(DERM_QUESTIONS, start=1):
            qid = f"Q-DERM-{i:03d}"
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

        total = len(DERM_QUESTIONS)
        print(f"Dermatology backfill complete: {inserted} inserted, {skipped} already present (of {total} defined).")


if __name__ == "__main__":
    asyncio.run(seed())
