"""Curated analyst question set (Rhem.csv) for the Questions-tab "Analyst" filter.

These prompts were bulk-ingested into PROD ahead of a review and carry no stored
marker, so the "Analyst" filter identifies them by matching the question TEXT
(normalized via ``app.prompt_volume.gap.normalize`` — case / punctuation / apostrophe /
whitespace insensitive). Their AI phrasing variations are surfaced through each base
question's existing expand-dropdown, so only the base prompts need to be listed here.

Each prompt also carries a DESIGNATION derived from the CSV's ``Persona`` + ``TA``
columns (Patient/HCP × RA/PsA). This surfaces as a "Designation" column on the
Approved Question Bank and — when the Workshop Questions filter is on — in the
AI Response Review CSV export. The one HCP row tagged ``Both`` (RA and PsA) is
labelled "HCP RA & PsA".

To adjust which questions the filter shows (and their designation), edit the pairs
below (data only). The match set + designation map are rebuilt from it (see
``question_service._analyst_norms`` / ``_analyst_designations``).
"""

# Verbatim "Prompt" column of C:\\Users\\SushantBandgar\\Documents\\Rhem.csv (21 rows)
# paired with its designation (Persona + indication). Order matches the CSV rows.
ANALYST_QUESTION_DESIGNATIONS: list[tuple[str, str]] = [
    ("How do I know if my RA treatment is working? What do doctors look for when deciding if a treatment is working?", "Patient RA"),
    ("Who is a good candidate for RINVOQ?", "Patient RA"),
    ("What happens if rheumatoid arthritis is not well controlled?", "Patient RA"),
    ("What is the best treatment for rheumatoid arthritis?", "Patient RA"),
    ("What should I know about the risks and side effects of RINVOQ?", "Patient RA"),
    ("Based on the black box warning for JAK inhibitors, how should I weigh cardiovascular risk in a 55-year-old RA patient versus switching to a second anti-TNF?", "HCP RA"),
    ("Can I take RINVOQ if I am currently being treated for high blood pressure? / Can I take RINVOQ with controlled hypertension?", "HCP RA"),
    ("Does upadacitinib have data similar to the ORAL Surveillance trial?; Does upadacitinib have the same level of risk of safety as other JAKs?", "HCP RA"),
    ("How should I counsel patients on the boxed warning of upadacitinib? How can I articulate RINVOQ's safety risks to a nervous patient?", "HCP RA"),
    ("What is the absolute risk of DVT or PE with RINVOQ 15mg in an RA patient with a history of controlled hypertension?", "HCP RA"),
    ("Are there particular patient sub-types or biomarkers at higher risk for VTE, CV events, etc.? Beyond known risk factors (diabetes, smoking), are there biomarkers we could measure to identify risk?", "HCP RA & PsA"),
    ("Which PsA drug is best by disease domain?", "HCP PsA"),
    ("When should I start a patient on a biologic in PsA, and which one do I start them on?", "HCP PsA"),
    ("Which IL-23 is better for PsA, Skyrizi or Tremfya?", "HCP PsA"),
    ("How does Skyrizi compare to TNFs and IL-17s in the joints for PsA?", "HCP PsA"),
    ("What is the best PsA treatment after a TNF fails?", "HCP PsA"),
    ("What's the difference between RINVOQ and XELJANZ?", "Patient PsA"),
    ("What's the difference between SKYRIZI and TREMFYA?", "Patient PsA"),
    ("Why does RINVOQ have a boxed warning?", "Patient PsA"),
    ("What are RINVOQ's/SKYRIZI's side effects?", "Patient PsA"),
    ("How do I know when to change PsA medications? or How do I know my PsA medication isn't working?", "Patient PsA"),
]

# Back-compat: the flat prompt list used by the Workshop Questions text-match filter.
ANALYST_QUESTIONS: list[str] = [prompt for prompt, _ in ANALYST_QUESTION_DESIGNATIONS]
