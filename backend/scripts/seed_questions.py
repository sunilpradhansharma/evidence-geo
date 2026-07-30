"""Seed the Question Repository with a realistic synthetic question bank.

Generates 120+ Medical-Affairs-style questions across 3 personas (Prospect, Patient,
Provider) x 4 therapeutic areas (Dermatology, Gastroenterology, Rheumatology, Oncology)
x 5 domains. Uses real brand names. Contains NO PII (SE-001). Satisfies AC-02
(>=30/persona, >=2 TAs) and FR-104.

`therapeutic_area` is tagged per row from the indication the question NAMES, not from
its brand: Humira, Skyrizi and Rinvoq each span all three specialties, so a brand-level
tag would be a coin toss. The handful of rows that name no indication (cost, biosimilar,
injection-technique questions) are filed under that brand's anchor indication.

The Lupron therapies (Central Precocious Puberty, Endometriosis, Uterine Fibroids) are
loaded separately and idempotently by scripts/seed_lupron_questions.py. Dermatology and
Gastroenterology also have dedicated top-up banks in scripts/seed_dermatology_questions.py
and scripts/seed_gastroenterology_questions.py.

Run:  python -m scripts.seed_questions
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

# Each entry: (persona, therapeutic_area, brand_focus, domain, question_text)
QUESTIONS: list[tuple[str, str, str, str, str]] = [
    # ============ DERM / GI / RHEUM — PROSPECT ============
    ("Prospect", "Rheumatology", "Humira", "General", "I was just diagnosed with moderate-to-severe rheumatoid arthritis. What treatment options should I be looking into?"),
    ("Prospect", "Dermatology", "Skyrizi", "Efficacy", "I have plaque psoriasis covering a lot of my body. Which biologic clears skin the fastest?"),
    ("Prospect", "Rheumatology", "Rinvoq", "General", "My doctor mentioned a pill called a JAK inhibitor for my arthritis. How does it differ from injections?"),
    ("Prospect", "Gastroenterology", "Humira", "Safety", "I'm nervous about starting a biologic for Crohn's disease. What are the most common side effects I should know about?"),
    ("Prospect", "Dermatology", "Skyrizi", "Comparative", "How does Skyrizi compare to Stelara for treating plaque psoriasis?"),
    ("Prospect", "Dermatology", "Rinvoq", "Comparative", "For atopic dermatitis, is Rinvoq or Dupixent considered more effective?"),
    ("Prospect", "Rheumatology", "Humira", "Access", "How much does Humira typically cost without good insurance, and are there cheaper alternatives?"),
    ("Prospect", "Dermatology", "Skyrizi", "Safety", "Does Skyrizi increase the risk of infections like other psoriasis medications?"),
    ("Prospect", "Gastroenterology", "Rinvoq", "Efficacy", "I have ulcerative colitis. How likely is Rinvoq to put me into remission?"),
    ("Prospect", "Rheumatology", "Humira", "Comparative", "Is the original Humira better than the biosimilar versions that are now available?"),
    ("Prospect", "Dermatology", "Skyrizi", "General", "What is risankizumab and what conditions is it approved to treat?"),
    ("Prospect", "Rheumatology", "Rinvoq", "Access", "Are there patient assistance programs to help pay for Rinvoq?"),
    ("Prospect", "Rheumatology", "Humira", "Efficacy", "How quickly does Humira start working for psoriatic arthritis symptoms?"),

    # ============ DERM / GI / RHEUM — PATIENT ============
    ("Patient", "Rheumatology", "Humira", "Efficacy", "I've been on Humira for rheumatoid arthritis for two years and it seems to be losing effectiveness. What should I do?"),
    ("Patient", "Dermatology", "Skyrizi", "Safety", "I'm on Skyrizi for psoriasis and getting a cold. Is it safe to keep taking my doses?"),
    ("Patient", "Dermatology", "Rinvoq", "Safety", "I take Rinvoq for my eczema. Should I be worried about blood clot warnings I read about?"),
    ("Patient", "Gastroenterology", "Humira", "Comparative", "My Humira isn't controlling my Crohn's well anymore. Would switching to Skyrizi help?"),
    ("Patient", "Dermatology", "Skyrizi", "Access", "My insurance is making me switch off Skyrizi. What are my options to stay on it?"),
    ("Patient", "Gastroenterology", "Rinvoq", "Efficacy", "How long should I expect to wait before Rinvoq controls my ulcerative colitis flares?"),
    ("Patient", "Rheumatology", "Humira", "General", "Can I travel internationally with my Humira pens, and how do I keep them refrigerated?"),
    ("Patient", "Dermatology", "Skyrizi", "Comparative", "I'm choosing between staying on Skyrizi or trying Tremfya. What are the differences?"),
    ("Patient", "Rheumatology", "Rinvoq", "Safety", "Do I need regular blood tests while taking Rinvoq?"),
    ("Patient", "Rheumatology", "Humira", "Safety", "Is it safe to get my flu and COVID vaccines while on Humira?"),
    ("Patient", "Dermatology", "Skyrizi", "Efficacy", "My psoriasis came back between Skyrizi doses. Is that normal?"),
    ("Patient", "Rheumatology", "Rinvoq", "Access", "Is there a copay card to lower my monthly cost for Rinvoq?"),
    ("Patient", "Rheumatology", "Humira", "Comparative", "Should I switch from Humira to a biosimilar to save money, and will it work the same?"),

    # ============ DERM / GI / RHEUM — PROVIDER ============
    ("Provider", "Gastroenterology", "Humira", "Efficacy", "What is the recommended first-line biologic for an adult with moderate-to-severe Crohn's disease who has failed conventional therapy?"),
    ("Provider", "Dermatology", "Skyrizi", "Comparative", "How does risankizumab compare to ustekinumab in head-to-head data for plaque psoriasis?"),
    ("Provider", "Rheumatology", "Rinvoq", "Safety", "What is the current boxed warning guidance for upadacitinib in patients over 65 with cardiovascular risk factors?"),
    ("Provider", "Rheumatology", "Humira", "Comparative", "In a TNF-naive RA patient, what is the rationale for choosing adalimumab over a JAK inhibitor?"),
    ("Provider", "Dermatology", "Skyrizi", "Efficacy", "What PASI 90 response rates were observed for Skyrizi in the pivotal psoriasis trials?"),
    ("Provider", "Dermatology", "Rinvoq", "Comparative", "For atopic dermatitis, how does upadacitinib's efficacy compare to dupilumab in clinical trials?"),
    ("Provider", "Rheumatology", "Humira", "Access", "What are the formulary considerations when prescribing reference adalimumab versus available biosimilars?"),
    ("Provider", "Dermatology", "Skyrizi", "Safety", "What is the infection risk profile of risankizumab compared to other IL-23 inhibitors?"),
    ("Provider", "Gastroenterology", "Rinvoq", "Efficacy", "What clinical remission rates support upadacitinib for moderate-to-severe ulcerative colitis?"),
    ("Provider", "Rheumatology", "Humira", "Safety", "What screening is required before initiating adalimumab in a patient with latent tuberculosis risk?"),
    ("Provider", "Gastroenterology", "Skyrizi", "General", "What is the maintenance dosing schedule for Skyrizi in Crohn's disease?"),
    ("Provider", "Rheumatology", "Rinvoq", "Comparative", "When would you sequence upadacitinib after a TNF inhibitor failure in psoriatic arthritis?"),
    ("Provider", "Rheumatology", "Humira", "Comparative", "How should I counsel a patient comparing adalimumab to Enbrel for ankylosing spondylitis?"),
    ("Provider", "Dermatology", "Skyrizi", "Comparative", "Is there evidence supporting Skyrizi over Cosentyx for biologic-experienced psoriasis patients?"),

    # ============ ONCOLOGY — PROSPECT ============
    ("Prospect", "Oncology", "Imbruvica", "General", "I was just diagnosed with chronic lymphocytic leukemia. What are the main treatment approaches today?"),
    ("Prospect", "Oncology", "Venclexta", "Efficacy", "How effective is Venclexta for getting CLL into remission without chemotherapy?"),
    ("Prospect", "Oncology", "Imbruvica", "Comparative", "How does Imbruvica compare to Calquence for treating CLL?"),
    ("Prospect", "Oncology", "Venclexta", "Safety", "I've heard Venclexta can cause something called tumor lysis syndrome. What is that risk?"),
    ("Prospect", "Oncology", "Imbruvica", "Safety", "What are the heart-related side effects I should ask about before starting Imbruvica?"),
    ("Prospect", "Oncology", "Venclexta", "Comparative", "For AML, is Venclexta combined with other drugs better than standard chemotherapy?"),
    ("Prospect", "Oncology", "Imbruvica", "Access", "Imbruvica looks very expensive. Are there programs to help cover the cost?"),
    ("Prospect", "Oncology", "Venclexta", "General", "Is Venclexta a pill or an infusion, and how long would I need to take it?"),
    ("Prospect", "Oncology", "Imbruvica", "Efficacy", "What are the long-term survival outcomes for patients on Imbruvica for CLL?"),
    ("Prospect", "Oncology", "Venclexta", "Comparative", "How does Venclexta compare to Imbruvica for newly diagnosed CLL?"),
    ("Prospect", "Oncology", "Imbruvica", "General", "What is ibrutinib and which blood cancers is it used for?"),
    ("Prospect", "Oncology", "Venclexta", "Access", "Does Medicare typically cover Venclexta for leukemia?"),
    ("Prospect", "Oncology", "Imbruvica", "Comparative", "Is a newer drug like Brukinsa safer than Imbruvica?"),

    # ============ ONCOLOGY — PATIENT ============
    ("Patient", "Oncology", "Imbruvica", "Safety", "I'm on Imbruvica for CLL and noticing irregular heartbeats. Is this related to my medication?"),
    ("Patient", "Oncology", "Venclexta", "Safety", "I'm starting Venclexta and my doctor mentioned a ramp-up schedule. Why is the dose increased slowly?"),
    ("Patient", "Oncology", "Imbruvica", "Comparative", "My Imbruvica is causing side effects. Would switching to Calquence reduce them?"),
    ("Patient", "Oncology", "Venclexta", "Efficacy", "How will my doctor know if Venclexta is working for my leukemia?"),
    ("Patient", "Oncology", "Imbruvica", "Access", "My out-of-pocket cost for Imbruvica is very high. What financial assistance exists?"),
    ("Patient", "Oncology", "Venclexta", "General", "Can I take Venclexta with my other medications and supplements?"),
    ("Patient", "Oncology", "Imbruvica", "Efficacy", "I've been on Imbruvica for a year. How do I know if my CLL is responding well?"),
    ("Patient", "Oncology", "Venclexta", "Safety", "What symptoms of tumor lysis syndrome should I watch for at home?"),
    ("Patient", "Oncology", "Imbruvica", "Safety", "Is it safe to take Imbruvica before a planned surgery, or do I need to stop it?"),
    ("Patient", "Oncology", "Venclexta", "Comparative", "Should I consider switching from Venclexta to a BTK inhibitor like Imbruvica?"),
    ("Patient", "Oncology", "Imbruvica", "General", "Do I need to avoid grapefruit while taking Imbruvica?"),
    ("Patient", "Oncology", "Venclexta", "Access", "Is there a copay assistance program for Venclexta?"),
    ("Patient", "Oncology", "Imbruvica", "Safety", "I'm bruising easily on Imbruvica. Should I be concerned about bleeding risk?"),

    # ============ ONCOLOGY — PROVIDER ============
    ("Provider", "Oncology", "Imbruvica", "Comparative", "In treatment-naive CLL, what is the evidence comparing ibrutinib to acalabrutinib regarding cardiovascular tolerability?"),
    ("Provider", "Oncology", "Venclexta", "Efficacy", "What undetectable MRD rates support fixed-duration venetoclax plus obinutuzumab in frontline CLL?"),
    ("Provider", "Oncology", "Imbruvica", "Safety", "What is the recommended management of ibrutinib-associated atrial fibrillation in an elderly CLL patient?"),
    ("Provider", "Oncology", "Venclexta", "Safety", "What is the tumor lysis syndrome prophylaxis and ramp-up protocol for initiating venetoclax in high-risk CLL?"),
    ("Provider", "Oncology", "Imbruvica", "Comparative", "How does ibrutinib sequence relative to venetoclax-based regimens in relapsed/refractory CLL?"),
    ("Provider", "Oncology", "Venclexta", "Efficacy", "What response rates support venetoclax plus azacitidine in older adults with newly diagnosed AML unfit for intensive chemotherapy?"),
    ("Provider", "Oncology", "Imbruvica", "Efficacy", "What are the long-term progression-free survival data for ibrutinib in mantle cell lymphoma?"),
    ("Provider", "Oncology", "Venclexta", "Comparative", "When would you choose a fixed-duration venetoclax regimen over continuous BTK inhibitor therapy in CLL?"),
    ("Provider", "Oncology", "Imbruvica", "Safety", "What drug interactions with strong CYP3A inhibitors require ibrutinib dose adjustment?"),
    ("Provider", "Oncology", "Venclexta", "General", "What is the recommended dosing ramp-up schedule for venetoclax in CLL over the first five weeks?"),
    ("Provider", "Oncology", "Imbruvica", "Comparative", "How does ibrutinib compare to zanubrutinib in terms of selectivity and adverse event profile?"),
    ("Provider", "Oncology", "Venclexta", "Safety", "What monitoring is required for neutropenia in patients on venetoclax combination therapy?"),
    ("Provider", "Oncology", "Imbruvica", "Access", "What are the considerations for oral oncolytic access and prior authorization when prescribing ibrutinib?"),
    ("Provider", "Oncology", "Venclexta", "Efficacy", "What is the durability of response after completing fixed-duration venetoclax in frontline CLL?"),

    # ============ EXTENSION — PROSPECT (to reach 40) ============
    ("Prospect", "Rheumatology", "Rinvoq", "Safety", "Are there long-term risks like cancer associated with taking Rinvoq?"),
    ("Prospect", "Rheumatology", "Humira", "General", "Do I have to inject Humira myself, and how often?"),
    ("Prospect", "Dermatology", "Skyrizi", "Access", "Is Skyrizi usually covered by commercial insurance for psoriasis?"),
    ("Prospect", "Rheumatology", "Rinvoq", "Comparative", "For rheumatoid arthritis, should I consider Rinvoq or a biologic injection first?"),
    ("Prospect", "Gastroenterology", "Humira", "Efficacy", "What percentage of Crohn's patients achieve remission on Humira?"),
    ("Prospect", "Dermatology", "Skyrizi", "Safety", "Do I need any vaccinations before starting Skyrizi?"),
    ("Prospect", "Rheumatology", "Rinvoq", "General", "What conditions is Rinvoq FDA-approved to treat?"),
    ("Prospect", "Oncology", "Imbruvica", "Safety", "Does Imbruvica increase bleeding risk if I'm on blood thinners?"),
    ("Prospect", "Oncology", "Venclexta", "Efficacy", "Can Venclexta cure my leukemia or just control it?"),
    ("Prospect", "Oncology", "Imbruvica", "General", "Will I need to take Imbruvica for the rest of my life?"),
    ("Prospect", "Oncology", "Venclexta", "Comparative", "Is fixed-duration Venclexta therapy better than taking a daily pill indefinitely?"),
    ("Prospect", "Oncology", "Imbruvica", "Efficacy", "How well does Imbruvica work for mantle cell lymphoma?"),
    ("Prospect", "Oncology", "Venclexta", "Safety", "What blood tests will I need while taking Venclexta?"),
    ("Prospect", "Oncology", "Imbruvica", "Access", "Does insurance usually require prior authorization for Imbruvica?"),

    # ============ EXTENSION — PATIENT (to reach 40) ============
    ("Patient", "Rheumatology", "Rinvoq", "General", "Can I drink alcohol while taking Rinvoq?"),
    ("Patient", "Rheumatology", "Humira", "Efficacy", "My psoriatic arthritis pain is back even on Humira. Is it time to change treatment?"),
    ("Patient", "Dermatology", "Skyrizi", "Safety", "I'm planning to get pregnant. Is Skyrizi safe during pregnancy?"),
    ("Patient", "Dermatology", "Rinvoq", "Comparative", "Would going back to a biologic injection work better than Rinvoq for my eczema?"),
    ("Patient", "Rheumatology", "Humira", "Access", "My copay for Humira just went up a lot. What assistance is available?"),
    ("Patient", "Dermatology", "Skyrizi", "Efficacy", "How long do the effects of each Skyrizi dose last?"),
    ("Patient", "Rheumatology", "Rinvoq", "Safety", "Should I stop Rinvoq if I get an infection?"),
    ("Patient", "Oncology", "Imbruvica", "General", "Can I take Imbruvica with food or should it be on an empty stomach?"),
    ("Patient", "Oncology", "Venclexta", "Efficacy", "My doctor said I reached undetectable MRD on Venclexta. What does that mean for me?"),
    ("Patient", "Oncology", "Imbruvica", "Comparative", "Is it worth switching from Imbruvica to Brukinsa to reduce side effects?"),
    ("Patient", "Oncology", "Venclexta", "Safety", "I feel very tired on Venclexta. Is fatigue a known side effect?"),
    ("Patient", "Oncology", "Imbruvica", "Access", "Are there foundations that help with Imbruvica costs for Medicare patients?"),
    ("Patient", "Oncology", "Venclexta", "General", "What happens after I finish my fixed course of Venclexta?"),
    ("Patient", "Oncology", "Imbruvica", "Efficacy", "How often will I have scans or blood work to check if Imbruvica is still working?"),

    # ============ EXTENSION — PROVIDER (to reach 40) ============
    ("Provider", "Gastroenterology", "Humira", "Efficacy", "What maintenance dosing optimizes adalimumab durability in ulcerative colitis?"),
    ("Provider", "Dermatology", "Skyrizi", "Safety", "What is the pregnancy safety data for risankizumab?"),
    ("Provider", "Rheumatology", "Rinvoq", "Access", "What step-therapy requirements typically gate upadacitinib in commercial formularies?"),
    ("Provider", "Gastroenterology", "Humira", "General", "What is the recommended induction and maintenance regimen for adalimumab in Crohn's disease?"),
    ("Provider", "Gastroenterology", "Skyrizi", "Efficacy", "What endoscopic remission data support risankizumab in Crohn's disease?"),
    ("Provider", "Rheumatology", "Rinvoq", "Safety", "What VTE and MACE monitoring is recommended during upadacitinib therapy?"),
    ("Provider", "Oncology", "Imbruvica", "General", "What is the standard once-daily dose of ibrutinib for CLL and when is dose reduction indicated?"),
    ("Provider", "Oncology", "Venclexta", "Comparative", "How does venetoclax plus obinutuzumab compare to venetoclax plus rituximab in CLL outcomes?"),
    ("Provider", "Oncology", "Imbruvica", "Efficacy", "What overall response rates support ibrutinib in Waldenstrom's macroglobulinemia?"),
    ("Provider", "Oncology", "Venclexta", "Safety", "What is the recommended approach to dose interruption for venetoclax-associated neutropenia?"),
    ("Provider", "Oncology", "Imbruvica", "Comparative", "In relapsed CLL, how do you weigh continuous ibrutinib against fixed-duration venetoclax-based therapy?"),
    ("Provider", "Oncology", "Venclexta", "Access", "What prior authorization documentation supports venetoclax for frontline CLL?"),

    # ============ DIVERGENCE-TRIGGERING QUESTIONS (for Chairman consensus testing) ============
    ("Provider", "Dermatology", "Rinvoq", "Comparative", "Compared to Dupixent, is Rinvoq a better first-line choice for moderate-to-severe atopic dermatitis in adults?"),
    ("Provider", "Oncology", "Imbruvica", "Comparative", "Should ibrutinib or acalabrutinib be preferred as first-line BTK inhibitor therapy in treatment-naive CLL?"),
    ("Provider", "Dermatology", "Skyrizi", "Comparative", "Is risankizumab superior to secukinumab for long-term PASI 90 maintenance in plaque psoriasis?"),
    ("Patient", "Oncology", "Venclexta", "Safety", "I read online that venetoclax can cause tumor lysis syndrome. How dangerous is this really?"),
    ("Prospect", "Rheumatology", "Humira", "Comparative", "With all the biosimilars available, is there any reason to still use brand-name Humira?"),
    # Shorthand queries (for SHORTHAND intent bucket testing)
    ("Provider", "Oncology", "Imbruvica", "General", "ibrutinib MOA"),
    ("Provider", "Oncology", "Venclexta", "General", "venetoclax TLS"),
    ("Provider", "Dermatology", "Rinvoq", "Efficacy", "upadacitinib PASI 90"),
]


async def seed() -> None:
    await init_db()
    async with AsyncSessionLocal() as db:
        existing = await db.execute(select(Question))
        if existing.scalars().first() is not None:
            print("Questions already exist — skipping seed. (Delete the DB to re-seed.)")
            return

        for i, (persona, ta, brand, domain, text) in enumerate(QUESTIONS, start=1):
            # Classify intent using Layer 1 rules
            intent_result = classify_by_rules(persona, domain, text)
            intent_type = intent_result.intent if intent_result else None
            q = Question(
                question_id=f"Q-{i:04d}",
                question_text=text,
                persona=persona,
                therapeutic_area=ta,
                brand_focus=brand,
                domain=domain,
                intent_type=intent_type,
                approval_status="APPROVED",
                approver_name=APPROVER,
                active=True,
                version=1,
            )
            db.add(q)
        await db.commit()

        counts: dict[str, int] = {}
        for persona, *_ in QUESTIONS:
            counts[persona] = counts.get(persona, 0) + 1
        print(f"Seeded {len(QUESTIONS)} approved questions.")
        print(f"Per persona: {counts}")


if __name__ == "__main__":
    asyncio.run(seed())
