"""Backfill the Question Repository with the Lupron therapeutic areas.

Adds an APPROVED, active question bank (~40 questions each) for the three Lupron
therapies introduced in the Harvest tab so they are monitorable end-to-end
(Pipeline -> Results -> Analytics), not just harvestable:

  - Central Precocious Puberty  (Lupron Depot-Ped)
  - Endometriosis               (Lupron Depot)
  - Uterine Fibroids            (Lupron Depot)

Across 3 personas (Prospect, Patient, Provider) x 5 domains. Real brand names,
contains NO PII (SE-001). Questions use deterministic ids (Q-CPP-###, Q-ENDO-###,
Q-UF-###) so this loader is IDEMPOTENT — it inserts only the rows whose id is not
already present and is safe to re-run against an existing prod database (the main
``seed_questions.py`` skips a non-empty DB, so this separate loader is required).

Run:  python -m scripts.seed_lupron_questions
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

# Each entry: (persona, domain, question_text). therapeutic_area + brand_focus are
# applied per block (see TA_BLOCKS) so they stay consistent and easy to audit.

# ============ CENTRAL PRECOCIOUS PUBERTY — Lupron Depot-Ped ============
# Patient/Prospect personas are voiced by the parent/caregiver (pediatric indication).
CPP_QUESTIONS: list[tuple[str, str, str]] = [
    # PROSPECT (parent exploring / newly diagnosed)
    ("Prospect", "General", "My 6-year-old daughter was just diagnosed with central precocious puberty. What are the main treatment options we should consider?"),
    ("Prospect", "Efficacy", "How effective is Lupron Depot-Ped at stopping the progression of early puberty in young children?"),
    ("Prospect", "Safety", "What are the most common side effects of Lupron Depot-Ped that parents should watch for?"),
    ("Prospect", "Comparative", "How does Lupron Depot-Ped compare to the Supprelin LA implant for treating central precocious puberty?"),
    ("Prospect", "Access", "How much does Lupron Depot-Ped cost, and is treatment for precocious puberty usually covered by insurance?"),
    ("Prospect", "Efficacy", "Will treating my child's precocious puberty with Lupron Depot-Ped help them reach a normal adult height?"),
    ("Prospect", "General", "What is leuprolide acetate and why is it used for central precocious puberty?"),
    ("Prospect", "Comparative", "Is a monthly injection like Lupron Depot-Ped better than a once-a-year implant for precocious puberty?"),
    ("Prospect", "Safety", "Are there long-term risks to my child's bone health from using Lupron Depot-Ped?"),
    ("Prospect", "General", "How is Lupron Depot-Ped given, and how often will my child need injections?"),
    ("Prospect", "Comparative", "For central precocious puberty, how does Lupron Depot-Ped compare to Triptodur or Fensolvi?"),
    ("Prospect", "Access", "Are there patient assistance programs to help pay for Lupron Depot-Ped?"),
    ("Prospect", "Safety", "Is it safe to start Lupron Depot-Ped in a 7-year-old, and are the effects reversible after stopping?"),
    # PATIENT (child on therapy — parent voice)
    ("Patient", "Efficacy", "My daughter has been on Lupron Depot-Ped for six months. How will the doctor know it's working?"),
    ("Patient", "Safety", "My child gets redness at the Lupron Depot-Ped injection site. Is this normal and what can we do?"),
    ("Patient", "Safety", "Since starting Lupron Depot-Ped my son has had headaches. Should I be concerned?"),
    ("Patient", "General", "Can my child still get regular vaccines while on Lupron Depot-Ped?"),
    ("Patient", "Efficacy", "My daughter's periods started before treatment. Will they stop on Lupron Depot-Ped?"),
    ("Patient", "Access", "Our copay for Lupron Depot-Ped went up. What assistance options are available?"),
    ("Patient", "Safety", "Are mood swings or emotional changes expected in children taking Lupron Depot-Ped?"),
    ("Patient", "Comparative", "My child dislikes the monthly Lupron Depot-Ped shots. Would switching to a Supprelin LA implant be easier?"),
    ("Patient", "General", "If we miss a scheduled Lupron Depot-Ped injection, what should we do?"),
    ("Patient", "Efficacy", "How long will my child need to stay on Lupron Depot-Ped before treatment is stopped?"),
    ("Patient", "Safety", "After my child stops Lupron Depot-Ped, how soon will normal puberty resume?"),
    ("Patient", "General", "Is there a 3-month version of Lupron Depot-Ped so my child needs fewer injections?"),
    ("Patient", "Safety", "My child gained some weight after starting Lupron Depot-Ped. Is that a known effect?"),
    # PROVIDER (pediatric endocrinology)
    ("Provider", "General", "What is the recommended dosing and administration schedule for Lupron Depot-Ped in central precocious puberty?"),
    ("Provider", "Efficacy", "What evidence supports Lupron Depot-Ped for improving predicted adult height in children with CPP?"),
    ("Provider", "Safety", "What is the recommended monitoring for LH and sex-steroid suppression during Lupron Depot-Ped therapy?"),
    ("Provider", "Safety", "What is the guidance on pseudotumor cerebri and seizure risk with GnRH agonists like leuprolide in pediatric patients?"),
    ("Provider", "Comparative", "How does Lupron Depot-Ped compare to histrelin (Supprelin LA) implant for hypothalamic-pituitary suppression in CPP?"),
    ("Provider", "Comparative", "For CPP, how do the 3-month and 6-month leuprolide formulations compare to triptorelin (Triptodur)?"),
    ("Provider", "Efficacy", "How should treatment response to Lupron Depot-Ped be assessed — GnRH stimulation testing or basal LH?"),
    ("Provider", "Safety", "What bone mineral density considerations apply to long-term Lupron Depot-Ped use in children?"),
    ("Provider", "General", "At what bone age or chronological age is discontinuation of Lupron Depot-Ped typically recommended?"),
    ("Provider", "Safety", "What is the expected timeline for return of the hypothalamic-pituitary-gonadal axis after stopping Lupron Depot-Ped?"),
    ("Provider", "Comparative", "When would you choose Fensolvi (6-month subcutaneous leuprolide) over Lupron Depot-Ped for a child with CPP?"),
    ("Provider", "Access", "What documentation and prior authorization are typically required to initiate Lupron Depot-Ped?"),
    ("Provider", "Efficacy", "What are the data on height outcomes when initiating Lupron Depot-Ped before age 6 versus later?"),
    ("Provider", "Safety", "How should an initial gonadotropin flare after the first Lupron Depot-Ped dose be managed?"),
]

# ============ ENDOMETRIOSIS — Lupron Depot ============
ENDO_QUESTIONS: list[tuple[str, str, str]] = [
    # PROSPECT
    ("Prospect", "General", "I was just diagnosed with endometriosis. What are my treatment options beyond pain pills?"),
    ("Prospect", "Efficacy", "How well does Lupron Depot relieve endometriosis pain?"),
    ("Prospect", "Safety", "What side effects should I expect when starting Lupron Depot for endometriosis?"),
    ("Prospect", "Comparative", "For endometriosis, how does Lupron Depot compare to the oral medication Orilissa?"),
    ("Prospect", "Comparative", "Is Lupron Depot or Myfembree a better choice for managing endometriosis pain?"),
    ("Prospect", "Access", "How much does Lupron Depot cost for endometriosis, and is it covered by insurance?"),
    ("Prospect", "General", "What is leuprolide and how does it treat endometriosis?"),
    ("Prospect", "Safety", "Will Lupron Depot put me into temporary menopause, and what does that feel like?"),
    ("Prospect", "Efficacy", "How long until Lupron Depot starts reducing my endometriosis symptoms?"),
    ("Prospect", "Safety", "I want children eventually. Does Lupron Depot for endometriosis affect future fertility?"),
    ("Prospect", "Comparative", "How does an injection like Lupron Depot compare to Zoladex for endometriosis?"),
    ("Prospect", "General", "How is Lupron Depot given for endometriosis and how often?"),
    ("Prospect", "Access", "Are there copay cards or assistance programs for Lupron Depot?"),
    # PATIENT
    ("Patient", "Safety", "I'm on Lupron Depot for endometriosis and having a lot of hot flashes. Is add-back therapy with norethindrone an option?"),
    ("Patient", "Efficacy", "I've had two Lupron Depot injections but still have pelvic pain. How long should I give it?"),
    ("Patient", "Safety", "I'm worried about bone loss from Lupron Depot. How long is it safe to stay on it?"),
    ("Patient", "General", "Does Lupron Depot for endometriosis cause weight gain?"),
    ("Patient", "Safety", "Since starting Lupron Depot my mood has been low. Is depression a known side effect?"),
    ("Patient", "Comparative", "My Lupron Depot side effects are tough. Would switching to oral Orilissa be gentler?"),
    ("Patient", "Efficacy", "Will my endometriosis pain come back after I finish my course of Lupron Depot?"),
    ("Patient", "Access", "My insurance wants a prior authorization for Lupron Depot. How do I get it approved?"),
    ("Patient", "General", "Can I use add-back therapy with Lupron Depot to reduce side effects without losing pain relief?"),
    ("Patient", "Safety", "Is it safe to take Lupron Depot for endometriosis longer than 6 months?"),
    ("Patient", "Efficacy", "How will my doctor know if Lupron Depot is reducing my endometriosis lesions?"),
    ("Patient", "General", "I missed my Lupron Depot injection by two weeks. Will my symptoms return?"),
    ("Patient", "Comparative", "Should I switch from Lupron Depot injections to Myfembree pills for convenience?"),
    # PROVIDER
    ("Provider", "Efficacy", "What is the evidence for Lupron Depot in reducing endometriosis-associated pelvic pain?"),
    ("Provider", "Safety", "What add-back regimen is recommended to mitigate bone loss during Lupron Depot therapy for endometriosis?"),
    ("Provider", "General", "What is the approved duration of Lupron Depot for endometriosis with and without add-back therapy?"),
    ("Provider", "Comparative", "How does depot leuprolide compare to oral elagolix (Orilissa) for endometriosis pain management?"),
    ("Provider", "Comparative", "For endometriosis, how does Lupron Depot compare to relugolix combination therapy (Myfembree)?"),
    ("Provider", "Safety", "What bone mineral density monitoring is advised for patients on long-term Lupron Depot for endometriosis?"),
    ("Provider", "Efficacy", "What recurrence rates of endometriosis pain are seen after discontinuing a course of Lupron Depot?"),
    ("Provider", "Safety", "How should the initial estrogen flare after the first Lupron Depot dose be managed in endometriosis?"),
    ("Provider", "General", "What is the recommended dosing of Lupron Depot (3.75 mg monthly vs 11.25 mg every 3 months) for endometriosis?"),
    ("Provider", "Comparative", "When would you select goserelin (Zoladex) over leuprolide (Lupron Depot) for endometriosis?"),
    ("Provider", "Access", "What prior-authorization criteria typically gate Lupron Depot for endometriosis?"),
    ("Provider", "Safety", "Is Lupron Depot appropriate for an adolescent with endometriosis, and what are the bone-health considerations?"),
    ("Provider", "Efficacy", "Does add-back therapy reduce the efficacy of Lupron Depot for endometriosis pain?"),
    ("Provider", "Safety", "What contraindications and pregnancy considerations apply before initiating Lupron Depot for endometriosis?"),
]

# ============ UTERINE FIBROIDS — Lupron Depot (with iron, preoperative) ============
UF_QUESTIONS: list[tuple[str, str, str]] = [
    # PROSPECT
    ("Prospect", "General", "I have uterine fibroids causing heavy bleeding. What medical options can help before surgery?"),
    ("Prospect", "Efficacy", "How effective is Lupron Depot at shrinking fibroids and reducing heavy periods?"),
    ("Prospect", "Safety", "What side effects does Lupron Depot cause when used for fibroids?"),
    ("Prospect", "Comparative", "For fibroid heavy bleeding, how does Lupron Depot compare to Oriahnn?"),
    ("Prospect", "Comparative", "Is Lupron Depot or Myfembree better for managing fibroid symptoms?"),
    ("Prospect", "Access", "Is Lupron Depot for uterine fibroids usually covered by insurance?"),
    ("Prospect", "General", "Why is Lupron Depot given together with iron before fibroid surgery?"),
    ("Prospect", "Efficacy", "Can Lupron Depot help me avoid a hysterectomy for my fibroids?"),
    ("Prospect", "Safety", "Will the fibroids grow back after I stop Lupron Depot?"),
    ("Prospect", "General", "How long do women usually take Lupron Depot before fibroid surgery?"),
    ("Prospect", "Comparative", "How does Lupron Depot compare to a non-hormonal option like tranexamic acid for fibroid bleeding?"),
    ("Prospect", "Safety", "Will Lupron Depot for fibroids cause menopause-like symptoms?"),
    ("Prospect", "Access", "Are there assistance programs to help with the cost of Lupron Depot?"),
    # PATIENT
    ("Patient", "General", "My doctor prescribed Lupron Depot with iron before my fibroid surgery. Why only for a few months?"),
    ("Patient", "Efficacy", "I've had one Lupron Depot injection for fibroids. How soon will my bleeding improve?"),
    ("Patient", "Safety", "I'm getting hot flashes on Lupron Depot for fibroids. Is there anything that helps?"),
    ("Patient", "Efficacy", "Will Lupron Depot raise my blood count enough to avoid a transfusion before surgery?"),
    ("Patient", "Safety", "Is short-term Lupron Depot for fibroids risky for my bones?"),
    ("Patient", "Comparative", "My Lupron Depot side effects are hard. Would Oriahnn pills control my fibroid bleeding instead?"),
    ("Patient", "General", "If my surgery is delayed, can I stay on Lupron Depot for my fibroids longer?"),
    ("Patient", "Access", "My insurer requires prior authorization for Lupron Depot. How do I get it approved for fibroids?"),
    ("Patient", "Efficacy", "How much do fibroids typically shrink on Lupron Depot before surgery?"),
    ("Patient", "Safety", "Can I get pregnant while on Lupron Depot for fibroids?"),
    ("Patient", "General", "What happens to my fibroid symptoms after I stop Lupron Depot?"),
    ("Patient", "Comparative", "Should I ask about Myfembree instead of Lupron Depot injections for my fibroids?"),
    ("Patient", "Safety", "I felt a symptom flare in the first weeks of Lupron Depot for fibroids. Is that expected?"),
    # PROVIDER
    ("Provider", "Efficacy", "What is the evidence for Lupron Depot plus iron for preoperative anemia in uterine fibroids?"),
    ("Provider", "General", "What is the approved indication and duration for Lupron Depot in uterine fibroids?"),
    ("Provider", "Safety", "What hypoestrogenic effects should be monitored during short-term Lupron Depot for fibroids?"),
    ("Provider", "Comparative", "How does Lupron Depot compare to elagolix combination therapy (Oriahnn) for fibroid-associated heavy menstrual bleeding?"),
    ("Provider", "Comparative", "For fibroid heavy menstrual bleeding, how does leuprolide depot compare to relugolix combination (Myfembree)?"),
    ("Provider", "Efficacy", "What degree of fibroid volume and uterine size reduction can be expected with preoperative Lupron Depot?"),
    ("Provider", "Access", "What prior-authorization documentation supports Lupron Depot for preoperative fibroid management?"),
    ("Provider", "Safety", "Is add-back therapy appropriate when extending Lupron Depot beyond 3 months for fibroids?"),
    ("Provider", "Efficacy", "Does preoperative Lupron Depot reduce intraoperative blood loss in myomectomy or hysterectomy?"),
    ("Provider", "Comparative", "When would you choose goserelin (Zoladex) over Lupron Depot for uterine fibroids?"),
    ("Provider", "General", "What is the recommended dosing of Lupron Depot for uterine fibroids preoperatively?"),
    ("Provider", "Safety", "What is the concern with fibroid regrowth and symptom return after discontinuing Lupron Depot?"),
    ("Provider", "Efficacy", "How does Lupron Depot affect hemoglobin and hematocrit in anemic fibroid patients before surgery?"),
    ("Provider", "Safety", "What are the contraindications and pregnancy warnings before starting Lupron Depot for fibroids?"),
]

# (id_prefix, therapeutic_area, brand_focus, questions)
TA_BLOCKS: list[tuple[str, str, str, list[tuple[str, str, str]]]] = [
    ("CPP", "Central Precocious Puberty", "Lupron Depot-Ped", CPP_QUESTIONS),
    ("ENDO", "Endometriosis", "Lupron Depot", ENDO_QUESTIONS),
    ("UF", "Uterine Fibroids", "Lupron Depot", UF_QUESTIONS),
]


async def seed() -> None:
    await init_db()
    async with AsyncSessionLocal() as db:
        existing = set((await db.execute(select(Question.question_id))).scalars().all())

        inserted = 0
        skipped = 0
        per_ta: dict[str, int] = {}
        for prefix, ta, brand, questions in TA_BLOCKS:
            for i, (persona, domain, text) in enumerate(questions, start=1):
                qid = f"Q-{prefix}-{i:03d}"
                if qid in existing:
                    skipped += 1
                    continue
                intent_result = classify_by_rules(persona, domain, text)
                intent_type = intent_result.intent if intent_result else None
                db.add(Question(
                    question_id=qid,
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
                ))
                inserted += 1
                per_ta[ta] = per_ta.get(ta, 0) + 1
        await db.commit()

        total = sum(len(q) for _, _, _, q in TA_BLOCKS)
        print(f"Lupron backfill complete: {inserted} inserted, {skipped} already present (of {total} defined).")
        if per_ta:
            print("Inserted per therapeutic area:")
            for ta, n in per_ta.items():
                print(f"  - {ta}: {n}")


if __name__ == "__main__":
    asyncio.run(seed())
