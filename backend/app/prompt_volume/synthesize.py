"""Turn a bare SEO keyword into a natural, monitorable question (FR-116.3 enhancement).

Prompt Volume ingests third-party demand data. SEO keyword exports are bare terms
("skyrizi", "skyrizi side effects", "skyrizi vs humira") rather than the full questions an
audience actually asks an AI engine, so dropping the raw keyword into the Question Bank
yields a lone word instead of something a medical/marketing reviewer can act on.

When an upload carries a real prompt/question column we use it verbatim; otherwise this
module deterministically rewrites the keyword into a natural question. It is pure,
dependency-free, and rule-based (NO LLM) so it is cheap, offline, and unit-testable. Rules
are ordered and the FIRST matching intent wins. The question subject is the mapped focus
brand or competitor when known (already nicely cased), else the keyword phrase itself.
"""
from __future__ import annotations

import re

# Leading words that mean the text is already phrased as a question / natural-language prompt
# (Semrush "Questions" report, AlsoAsked, AnswerThePublic) — keep the author's phrasing.
_INTERROGATIVES = frozenset({
    "how", "what", "whats", "why", "when", "where", "which", "who", "whom", "is", "are",
    "am", "can", "could", "will", "would", "should", "shall", "does", "do", "did", "has",
    "have", "was", "were", "may", "might",
})

# Symptom-ish words that read naturally as "Does {brand} cause {concern}?".
_CONCERN_HINTS = frozenset({
    "weight", "gain", "loss", "hair", "nausea", "fatigue", "headache", "headaches",
    "rash", "itching", "acne", "swelling", "depression", "anxiety", "diarrhea",
    "constipation", "insomnia", "pain", "cramps", "bloating", "dizziness", "cough",
    "hairloss", "tiredness", "cancer",
})

_WORD = re.compile(r"[a-z0-9']+")


def _first_word(low: str) -> str:
    m = _WORD.search(low)
    return m.group() if m else ""


def _strip_subject(low: str, subject: str | None) -> str:
    """Remove the subject (drug name) from the keyword, leaving the concern/qualifier phrase."""
    if not subject:
        return low.strip()
    pat = re.compile(r"(?<![a-z0-9])" + re.escape(subject.lower()) + r"(?![a-z0-9])")
    return re.sub(r"\s+", " ", pat.sub(" ", low)).strip(" -,")


def _looks_like_concern(leftover: str) -> bool:
    return any(tok in _CONCERN_HINTS for tok in _WORD.findall(leftover))


def _safety_qualifier(low: str) -> str:
    if re.search(r"\bpregnan", low):
        return " during pregnancy"
    if re.search(r"\bbreastfeed", low):
        return " while breastfeeding"
    if re.search(r"\balcohol\b", low):
        return " to take with alcohol"
    if re.search(r"long[\s-]?term", low):
        return " to use long term"
    return ""


def _polish(text: str, brand: str | None, competitor: str | None) -> str:
    """Normalize an already-question string: fix casing of known drugs + trailing '?'."""
    out = text.strip()
    for name in (brand, competitor):
        if name:
            out = re.sub(
                r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", name, out, flags=re.I
            )
    out = out[:1].upper() + out[1:]
    if not out.endswith("?"):
        out += "?"
    return out


def to_question(query: str, *, brand: str | None = None, competitor: str | None = None) -> str:
    """Rewrite a keyword/topic into a natural question. Always returns a non-empty string
    (unless ``query`` is blank). Deterministic — no network, no LLM."""
    raw = (query or "").strip()
    if not raw:
        return ""
    low = raw.lower()

    # 0) Already a question / prompt -> keep the audience's own phrasing.
    if raw.endswith("?") or _first_word(low) in _INTERROGATIVES:
        return _polish(raw, brand, competitor)

    subject = brand or competitor
    subj = subject if subject else raw
    leftover = _strip_subject(low, subject) if subject else ""

    # 1) Ordered intent rules — first match wins.
    if competitor and re.search(
        r"\b(vs|versus|compared to|compare|comparison|difference|better than)\b", low
    ):
        return f"How does {subj} compare to {competitor}?"
    if re.search(
        r"\b(cost|costs|price|prices|pricing|copay|co-pay|coupon|savings|save|insurance|"
        r"covered|cover|afford|expensive|cheap|discount|assistance)\b", low
    ):
        return f"How much does {subj} cost, and is it covered by insurance?"
    if re.search(
        r"\b(dose|doses|dosing|dosage|mg|ml|injection|inject|how often|frequency|"
        r"administration|administer|titration)\b", low
    ) or "how much" in low:
        return f"What is the recommended dosing and administration for {subj}?"
    if re.search(
        r"\b(pregnan\w*|breastfeed\w*|alcohol|safe|safety|risk|risks|warning|warnings|"
        r"contraindicat\w*|interaction|interactions)\b", low
    ) or re.search(r"long[\s-]?term", low):
        return f"Is {subj} safe{_safety_qualifier(low)}?"
    if re.search(
        r"\b(work|works|working|effective|effectiveness|efficacy|results|success)\b", low
    ) or "how long" in low:
        return f"How well does {subj} work, and how long does it take?"
    if re.search(r"\b(review|reviews|experience|experiences|forum|stories|testimonial\w*)\b", low):
        return f"What do patients say about their experience with {subj}?"
    if re.search(r"\b(alternative|alternatives|substitute|substitutes|generic|generics|biosimilar\w*)\b", low) \
            or "instead of" in low or "other options" in low:
        return f"What are the alternatives to {subj}?"
    if re.search(r"\b(side effect|side effects|adverse|reaction|reactions)\b", low):
        return f"What are the side effects of {subj}?"

    # 2) Known drug + a symptom-like tail -> "Does {drug} cause {concern}?".
    if subject and leftover and _looks_like_concern(leftover):
        return f"Does {subj} cause {leftover}?"

    # 3) Grammatical fallbacks that preserve the topic.
    if subject and leftover:
        return f"What should patients know about {subj} {leftover}?"
    if subject:
        return f"What is {subj} and what is it used for?"
    return f"What should patients know about {raw}?"
