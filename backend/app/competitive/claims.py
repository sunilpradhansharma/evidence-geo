"""What models keep asserting in one head-to-head. Pure — no DB, no network, no model call.

Clusters the ``key_claims`` the scorer already extracted, so recurrence across models and
runs becomes visible: a claim three platforms repeat is a market narrative, one claim in one
answer is an anecdote, and a marketer needs to be able to tell them apart at a glance.

Clustering is keyword-free and deterministic — normalise, then group identical normalised
text. Deliberately NOT fuzzy: a near-match threshold silently merges two different clinical
statements, and a claim ledger that merges "works faster" with "works better" is worse than
one that lists both. Normalisation reuses ``coverage.normalize``, the same key the harvest
dedupe and the coverage matrix already agree on.

Claims are attributed to the verdict of the answer they came from, which is the whole point:
the reader wants the claims that show up when we LOSE, not the most common claim overall.
"""
from __future__ import annotations

import collections
from dataclasses import dataclass, field

from app.competitive import verdict as verdict_mod
from app.curation.coverage import normalize
from app.prompt_volume import mapping

# A claim shorter than this is a fragment, not an assertion ("efficacy", "cost").
MIN_CLAIM_CHARS = 12
MAX_CLAIMS = 25


@dataclass
class ClaimCluster:
    text: str
    answers: int = 0
    models: set[str] = field(default_factory=set)
    verdicts: collections.Counter = field(default_factory=collections.Counter)
    names_competitor: bool = False

    @property
    def losing_answers(self) -> int:
        return self.verdicts.get(verdict_mod.LOSING, 0)

    @property
    def against_us(self) -> bool:
        """Names the rival AND showed up in an answer we lost — the claim to counter."""
        return self.names_competitor and self.losing_answers > 0

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "answers": self.answers,
            "models": sorted(self.models),
            "model_count": len(self.models),
            "losing_answers": self.losing_answers,
            "verdicts": dict(self.verdicts),
            # Repeated by several platforms rather than one: the difference between a market
            # narrative and one model's phrasing.
            "cross_model": len(self.models) > 1,
            "names_competitor": self.names_competitor,
            "against_us": self.against_us,
        }


def cluster(
    entries: list[tuple[str, str, list]], *, competitor: str | None = None
) -> dict:
    """Cluster claims from ``[(llm_name, verdict, key_claims), ...]``.

    Returns the ledger plus the counts a reader needs to size it: how many answers actually
    carried claims, because an empty ledger over 25 answers means the scorer extracted
    nothing, not that the models said nothing.

    **Ranking is not volume-first, on purpose.** Measured on the real corpus, 124 of 125
    extracted claims were textually unique — models rarely repeat a phrasing — so ordering by
    recurrence leaves almost every claim tied and the truncated list degenerates into
    alphabetical order, hiding the few that matter behind whichever happen to start with "A".
    So the informative class is identified FIRST: a claim that names the rival and appeared in
    an answer we lost is the market narrative to counter, and it sorts above everything else.
    """
    clusters: dict[str, ClaimCluster] = {}
    answers_with_claims = 0
    claims_seen = 0

    for llm_name, result, key_claims in entries:
        if not isinstance(key_claims, list):
            continue
        # One answer repeating a claim counts once for that answer, so a model that restates
        # itself cannot inflate the recurrence figure.
        local: dict[str, str] = {}
        for raw in key_claims:
            text = str(raw or "").strip()
            if len(text) < MIN_CLAIM_CHARS:
                continue
            key = normalize(text)
            if key:
                local.setdefault(key, text)
        if not local:
            continue
        answers_with_claims += 1
        claims_seen += len(local)
        for key, text in local.items():
            entry = clusters.setdefault(key, ClaimCluster(text=text))
            entry.answers += 1
            entry.models.add(llm_name)
            entry.verdicts[result] += 1
            if competitor and mapping.mentions(text, competitor):
                entry.names_competitor = True

    ranked = sorted(
        clusters.values(),
        key=lambda c: (
            not c.against_us,       # the claims to counter, first
            not c.names_competitor,  # then anything else about the rival
            -c.losing_answers,       # then claims recurring in answers we lost
            -len(c.models),          # then narratives more than one platform repeats
            -c.answers,
            c.text,
        ),
    )
    against_us = sum(1 for c in ranked if c.against_us)
    return {
        "answers_with_claims": answers_with_claims,
        "claims_extracted": claims_seen,
        "distinct_claims": len(ranked),
        "claims_against_us": against_us,
        "claims": [c.as_dict() for c in ranked[:MAX_CLAIMS]],
        "claims_truncated": max(0, len(ranked) - MAX_CLAIMS),
        "note": "Claims are what the scoring model pulled out of each answer, grouped by "
                "identical wording — nothing is paraphrased or merged by similarity. Sorted "
                "so claims about the rival that appear in answers we lose come first.",
    }
