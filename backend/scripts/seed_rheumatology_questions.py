"""Backfill the Question Repository with the Rheumatology therapeutic area.

Adds an APPROVED, active question bank (~40 questions) for the Rheumatology area
so it is monitorable end-to-end (Pipeline -> Results -> Analytics), not just
harvestable.

  - Rheumatology  (focus brands: Rinvoq [upadacitinib], Humira [adalimumab])

Across 3 personas (Prospect, Patient, Provider) x 5 domains. Real brand names,
contains NO PII (SE-001). Questions use deterministic ids (Q-RHEUM-###) so this
loader is IDEMPOTENT: it inserts only the rows whose id is not already present
and is safe to re-run against an existing prod database (the main
``seed_questions.py`` skips a non-empty DB, so this separate loader is required,
mirroring ``seed_lupron_questions.py``).

Run:  python -m scripts.seed_rheumatology_questions
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
THERAPEUTIC_AREA = "Rheumatology"

# (persona, domain, brand_focus, question_text). therapeutic_area is constant
# (Rheumatology); brand_focus is per-question because this area has two focus
# brands (Rinvoq and Humira), matching their names in brands.yaml.
RHEUM_QUESTIONS: list[tuple[str, str, str, str]] = [
    # ============ PROSPECT (exploring / newly diagnosed / choosing therapy) ============
    ("Prospect", "General", "Rinvoq", "I was just diagnosed with rheumatoid arthritis. What treatment options should I look into beyond methotrexate?"),
    ("Prospect", "Efficacy", "Rinvoq", "How effective is Rinvoq at controlling rheumatoid arthritis symptoms?"),
    ("Prospect", "Safety", "Rinvoq", "What are the most common side effects of Rinvoq I should know about before starting?"),
    ("Prospect", "Comparative", "Rinvoq", "For rheumatoid arthritis, how does Rinvoq compare to Xeljanz as a JAK inhibitor?"),
    ("Prospect", "Access", "Humira", "How much does Humira cost without good insurance, and are the biosimilars cheaper?"),
    ("Prospect", "General", "Humira", "My doctor suggested Humira for psoriatic arthritis. How does a TNF blocker actually work?"),
    ("Prospect", "Comparative", "Humira", "Is Humira or Enbrel generally considered better for ankylosing spondylitis?"),
    ("Prospect", "Efficacy", "Rinvoq", "How quickly does Rinvoq start working for joint pain and stiffness?"),
    ("Prospect", "Safety", "Rinvoq", "I read about blood clot and heart warnings with JAK inhibitors. Should that worry me about Rinvoq?"),
    ("Prospect", "Comparative", "Rinvoq", "For rheumatoid arthritis, is a daily pill like Rinvoq better than an injectable biologic like Humira?"),
    ("Prospect", "Access", "Rinvoq", "Are there patient assistance or copay programs to help pay for Rinvoq?"),
    ("Prospect", "General", "Humira", "What conditions is Humira approved to treat in rheumatology?"),
    ("Prospect", "Safety", "Humira", "Does starting Humira increase my risk of serious infections?"),

    # ============ PATIENT (already on therapy: experiential, practical) ============
    ("Patient", "Efficacy", "Humira", "I've been on Humira for rheumatoid arthritis for a year and it seems to be losing effect. What should I do?"),
    ("Patient", "Safety", "Rinvoq", "I take Rinvoq for RA. Do I need regular blood tests while on it?"),
    ("Patient", "Comparative", "Humira", "My Humira isn't controlling my psoriatic arthritis well anymore. Would switching to Rinvoq help?"),
    ("Patient", "Safety", "Humira", "Is it safe to get my flu and shingles vaccines while taking Humira?"),
    ("Patient", "Access", "Rinvoq", "My insurance is requiring a prior authorization for Rinvoq. How do I get it approved?"),
    ("Patient", "General", "Humira", "Can I travel with my Humira pens, and how do I keep them refrigerated?"),
    ("Patient", "Efficacy", "Rinvoq", "How long should I give Rinvoq before deciding whether it is working for my ankylosing spondylitis?"),
    ("Patient", "Safety", "Rinvoq", "Since starting Rinvoq I keep getting cold sores and minor infections. Is that expected?"),
    ("Patient", "Comparative", "Humira", "Should I switch from Humira to a biosimilar to save money, and will it work the same?"),
    ("Patient", "Safety", "Humira", "I have a cold and I'm due for my Humira injection. Should I skip this dose?"),
    ("Patient", "General", "Rinvoq", "If I miss a dose of Rinvoq, what should I do?"),
    ("Patient", "Access", "Humira", "My copay for Humira jumped this year. What assistance options are available?"),
    ("Patient", "Efficacy", "Rinvoq", "My joint swelling improved on Rinvoq but my morning stiffness remains. Is that normal?"),

    # ============ PROVIDER (clinician / technical) ============
    ("Provider", "Efficacy", "Rinvoq", "What is the evidence for upadacitinib (Rinvoq) in patients with rheumatoid arthritis who have failed methotrexate?"),
    ("Provider", "Safety", "Rinvoq", "What is the current boxed warning guidance for upadacitinib regarding MACE and VTE in patients over 65?"),
    ("Provider", "Comparative", "Rinvoq", "How does upadacitinib compare to adalimumab in head-to-head data for rheumatoid arthritis (SELECT-COMPARE)?"),
    ("Provider", "General", "Humira", "What is the recommended first-line biologic for an adult with active ankylosing spondylitis who has failed NSAIDs?"),
    ("Provider", "Safety", "Humira", "What screening for latent TB and hepatitis B is required before initiating adalimumab?"),
    ("Provider", "Comparative", "Humira", "For psoriatic arthritis, how does adalimumab compare to secukinumab (Cosentyx) on axial and skin outcomes?"),
    ("Provider", "Efficacy", "Rinvoq", "What ACR20/50/70 response rates were observed with upadacitinib in the SELECT trial program?"),
    ("Provider", "Safety", "Rinvoq", "What laboratory monitoring (lipids, CBC, liver function) is recommended during upadacitinib therapy?"),
    ("Provider", "Comparative", "Rinvoq", "When would you choose a JAK inhibitor such as upadacitinib over a TNF inhibitor such as etanercept (Enbrel)?"),
    ("Provider", "Access", "Humira", "What prior-authorization criteria typically gate adalimumab versus its biosimilars?"),
    ("Provider", "General", "Rinvoq", "What is the approved dosing of upadacitinib across rheumatoid arthritis, psoriatic arthritis, and ankylosing spondylitis?"),
    ("Provider", "Safety", "Humira", "How should anti-drug antibody formation and loss of response to adalimumab be evaluated and managed?"),
    ("Provider", "Comparative", "Humira", "For a patient with RA and recurrent infections, how do abatacept (Orencia) and adalimumab compare on infection risk?"),
    ("Provider", "Efficacy", "Rinvoq", "What is the evidence for upadacitinib in non-radiographic axial spondyloarthritis?"),
]


async def seed() -> None:
    await init_db()
    async with AsyncSessionLocal() as db:
        existing = set((await db.execute(select(Question.question_id))).scalars().all())

        inserted = 0
        skipped = 0
        for i, (persona, domain, brand, text) in enumerate(RHEUM_QUESTIONS, start=1):
            qid = f"Q-RHEUM-{i:03d}"
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

        total = len(RHEUM_QUESTIONS)
        print(f"Rheumatology backfill complete: {inserted} inserted, {skipped} already present (of {total} defined).")


if __name__ == "__main__":
    asyncio.run(seed())
