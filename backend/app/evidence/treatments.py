"""Map a source's own treatment wording onto a canonical network node (Phase 3A/4).

Shared by every adapter for exactly the reason ``endpoints.py`` is shared: two adapters
that resolve "Upadacitinib 15 mg QD" to different node names would build two incompatible
networks out of the same evidence. That mattered less while only ClinicalTrials.gov
produced nodes; it becomes load-bearing in Phase 4, where a **published** NMA's league
table has to be overlap-checked against an **internal** network. If the registry adapter
calls the node ``Rinvoq`` and the published adapter calls it ``Upadacitinib``, the two
networks appear disjoint and the overlap check silently passes when it should fail.

Extracted from ``sources/clinicaltrials.py``, which now imports from here. The regexes and
their reasoning are unchanged — the registry's own regression guards still cover them.

Four properties worth not breaking:

* **The curated drug table wins.** Aliases are matched longest-first and on word
  boundaries, because the catalog holds aliases as short as ``alli`` that would otherwise
  fire inside unrelated molecule names.
* **Dose is stripped from the node name, not discarded.** Whether two doses are one node
  or two is a ``dose_policy`` decision made under an approved protocol, never here.
* **An unrecognised agent keeps its label.** Dropping it would silently shrink the network.
* **Placebo does not win a label just by appearing in it.** An add-on arm names one too, and
  folding it into the placebo node contaminates the anchor every indirect estimate runs
  through. See ``_placebo_is_the_allocation``.
"""
from __future__ import annotations

import re
from functools import lru_cache

from app.config import taxonomy

PLACEBO = "Placebo"

# ``PBO`` is how registries abbreviate placebo in arm titles, and it is an abbreviation of
# the word rather than a different control, so it resolves to the same node. Placebo is the
# anchor for every indirect comparison: a missed placebo arm does not cost one node, it
# disconnects the network and takes Bucher's whole common comparator with it.
#
# ``sham`` is deliberately NOT matched here even though ``is_placebo``'s docstring mentions
# it. Merging a sham procedure into the placebo node is a claim about comparability, not an
# expansion of an abbreviation, and it belongs to curation under a protocol.
_PLACEBO_RE = re.compile(r"\b(?:placebo|pbo)\b", re.IGNORECASE)

# Noise stripped from an UNCURATED arm title to recover the molecule name.
#
# Without this, a curated drug collapses to a dose-free node ("Rinvoq") while an
# uncurated one keeps its full title, so "Secukinumab 150 mg", "Secukinumab 300 mg" and
# "Secukinumab 150 mg with loading dose" become THREE nodes. That inconsistency inflates
# node counts several-fold and fragments the network graph into unconnectable slivers.
_LABEL_NOISE = (
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:mg|g|mcg|µg|ug|ml|iu|units?)\b"
               r"(?:\s*/\s*\d*\.?\d*\s*(?:ml|kg|m2|day|week))?", re.IGNORECASE),
    re.compile(r"\b(?:QD|BID|TID|QID|QW|Q2W|Q4W|Q8W|Q12W|EOW|EW|BIW|PRN)\b", re.IGNORECASE),
    re.compile(r"\b(?:once|twice|thrice|three times)\s+(?:a\s+|per\s+)?"
               r"(?:daily|weekly|monthly|day|week|month)\b", re.IGNORECASE),
    re.compile(r"\bevery\s+(?:other\s+)?(?:\d+\s+)?(?:day|week|month)s?\b", re.IGNORECASE),
    re.compile(r"\b(?:oral|orally|per\s+os|subcutaneous(?:ly)?|intravenous(?:ly)?|"
               r"injection|infusion|tablet|capsule|solution|syringe|"
               # Hyphenated because hyphens are not separators here (see below), so a bare
               # \bautoinjector\b never matched the "Auto-injector" registries actually write.
               r"auto[-\s]?injector|pre[-\s]?filled)\b", re.IGNORECASE),
    # Route abbreviations, case-SENSITIVE on purpose: registries write them uppercase, and
    # matching case-insensitively would strip these letter pairs out of molecule names.
    re.compile(r"\b(?:SC|IV|IM|PO|SQ)\b"),
    re.compile(r"\b(?:with|without|no|plus|following|after)?\s*\bloading\s+dose\b", re.IGNORECASE),
    re.compile(r"\b(?:high|low|medium)[- ]dose\b", re.IGNORECASE),
    # A scaffolding word together with its enumerator, consumed as ONE unit and before the
    # bare-word rule below. Removing only the word leaves the ordinal stranded, and a
    # stranded ordinal is worse than the word was: "Sonelokimab Dose 1/2/3" became three
    # nodes for one molecule, and "Group A" became "A".
    re.compile(r"\b(?:dose\s+level|dose|doses|dosage|level|regimen|group|arm|cohort|"
               r"treatment|part|period|stage|step|sequence)\s+(?:\d+|[ivx]+|[a-z])\b",
               re.IGNORECASE),
    re.compile(r"\b(?:dose|doses|dosing|regimen|group|arm|cohort|treatment)\b", re.IGNORECASE),
    # Trial-design scaffolding: where an arm sits in a study's design, not what was given.
    # "Part 2 ADA" and "ADA" are one treatment node. Left in, a single drug splits across
    # several labels and none of them match the catalog.
    re.compile(r"\binadequate\s+responders?\b", re.IGNORECASE),
    # Case-SENSITIVE for the same reason as the route abbreviations above: "IR" is the
    # registry's inadequate-responder marker, and matching it case-insensitively would
    # strip those two letters out of molecule names.
    re.compile(r"\bIR\b"),
    re.compile(r"\bwash[-\s]?out\b", re.IGNORECASE),
    re.compile(r"\binterruption\b", re.IGNORECASE),
    re.compile(r"\bescalat(?:ed|ion|ing)\b", re.IGNORECASE),
    re.compile(r"\bcontinuation\b", re.IGNORECASE),
    re.compile(r"\bvia\b", re.IGNORECASE),
    # Who was enrolled, not what they were given. A registry arm titled "Healthy" or
    # "Patients" names a population; "Control" names a role. None of them name a treatment,
    # so they must not become nodes that look comparable to a drug.
    re.compile(r"\b(?:healthy|patients?|participants?|subjects?|volunteers?)\b", re.IGNORECASE),
    re.compile(r"\b(?:control|comparator)\b", re.IGNORECASE),
    re.compile(r"\([^)]*\)"),
)

# Hyphens are NOT treated as separators. Development codes carry them as part of the
# molecule's identity — ABT-494 is upadacitinib, BI-655066 is risankizumab — so collapsing
# them would split one node in two. Only a dash standing alone between spaces is noise.
_LABEL_SEPARATORS = re.compile(r"[,;:/+]+")
_LABEL_DANGLING_DASH = re.compile(r"(?:(?<=\s)|^)[-–]+(?=\s|$)")
_LABEL_WHITESPACE = re.compile(r"\s+")

# A dose index left dangling on the end of a molecule name, for the cases the paired rule
# above cannot see because the registry never wrote the word: "sonelokimab 1".
#
# Capped at two digits so a development code written with a space instead of a hyphen keeps
# its number — "LY 3074828" is one molecule, not molecule "LY" at dose 3074828. Roman
# numerals and single letters are deliberately excluded: "Vitamin D" and "Factor VIII" would
# lose the half that identifies them.
_TRAILING_ORDINAL = re.compile(r"\s+\d{1,2}$")

# A label that reduces to a bare enumerator, or to nothing at all, names no molecule.
_ENUMERATOR_ONLY = re.compile(r"^(?:[a-z]|\d+|[ivx]+)$", re.IGNORECASE)

# A results group that sums the arms instead of being one. Anchored at the start so a real
# arm is never caught by a trailing word — "Placebo Total Daily Dose" is an arm, "Total" is
# not. Kept as a prefix match rather than an exact one because registries qualify these
# ("Total of all reporting groups", "All Participants Randomized").
_AGGREGATE_TERMS = re.compile(
    r"^(?:total|overall|combined|entire\s+cohort"
    r"|all\s+(?:participants?|patients?|subjects?|groups?|arms?))\b",
    re.IGNORECASE,
)

# Arm labels naming a drug CLASS or a care STRATEGY rather than a molecule.
#
# A trial randomising "TNFi" against "Standard Care" is real randomised evidence, but it is
# not evidence about a molecule. Putting that arm in a network beside Humira asserts that
# every TNF inhibitor is interchangeable, which is the assumption the network was supposed
# to test rather than assume.
#
# The sharper problem is the common comparator. Two trials' "Standard Care" arms are two
# different interventions that happen to share a label, so pooling them on label identity
# manufactures a shared node that never existed — and a fabricated common comparator is the
# one transitivity failure indirect comparison cannot survive, because every effect estimate
# in the network is chained through it.
#
# Bounded and hand-authored for the same reason as the drug catalog: a curated term is a
# reviewable artefact, an inferred one is an unreviewed assertion. Registry typos are
# deliberately not enumerated ("Prescreen Based bDMARD Stategic" is caught by the DMARD
# rule), because chasing spelling is unmaintainable.
_CLASS_LEVEL_TERMS = (
    re.compile(r"\b(?:b|cs|ts|b/ts)?DMARDs?\b", re.IGNORECASE),
    re.compile(r"\bTNF\s*i\b|\banti[-\s]?TNF\b|\bTNF\s+inhibit(?:or|ion)s?\b", re.IGNORECASE),
    # Case-sensitive: these are uppercase target names, and lowercasing them invites matches
    # inside ordinary words.
    re.compile(r"\b(?:JAK|IL[-\s]?\d+[A-Za-z]?)\s*i(?:nhibitors?|nhibition)?\b"),
    re.compile(r"\bbiologics?\b", re.IGNORECASE),
    re.compile(r"\b(?:standard|usual|routine|conventional)\s+(?:of\s+)?"
               r"(?:care|therapy|treatment)\b", re.IGNORECASE),
    re.compile(r"\bstrateg(?:y|ic|ies)\b", re.IGNORECASE),
    re.compile(r"\btreat[-\s]?to[-\s]?target\b", re.IGNORECASE),
    re.compile(r"\btight\s+control\b", re.IGNORECASE),
    re.compile(r"\bstep[-\s]?(?:up|down)\b", re.IGNORECASE),
    # "Standard hold" / "Shorter hold": a strategy for withholding a drug, not an agent.
    re.compile(r"\bhold(?:ing)?\b", re.IGNORECASE),
)


@lru_cache(maxsize=512)
def alias_pattern(alias: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])")


def _collapse_repeats(text: str) -> str:
    """Collapse a token repeated back to back, case-insensitively.

    A dose-switch arm is written "IXE Q2W/IXE Q4W". Stripping the doses and turning the
    slash into a space leaves the molecule named twice, which is one molecule.
    """
    tokens: list[str] = []
    for token in text.split():
        if not tokens or token.lower() != tokens[-1].lower():
            tokens.append(token)
    return " ".join(tokens)


def molecule_label(title: str) -> str:
    """Strip dose, frequency, route, formulation and design noise from an arm title."""
    text = title
    for pattern in _LABEL_NOISE:
        text = pattern.sub(" ", text)
    text = _LABEL_SEPARATORS.sub(" ", text)
    text = _LABEL_DANGLING_DASH.sub(" ", text)
    text = _LABEL_WHITESPACE.sub(" ", text).strip(" ,;:/-–+")
    text = _TRAILING_ORDINAL.sub("", text)
    return _collapse_repeats(text)


def canonical_treatment(title: str | None) -> tuple[str, bool]:
    """``(node name, is_placebo)`` for a results-group, arm, or league-table label.

    Resolves through the curated drug table so "Upadacitinib 15 mg QD" and "RINVOQ" land
    on the same network node. An uncurated agent keeps its raw title — an unrecognised
    drug is still a real arm, and dropping it would silently shrink the network.
    """
    raw = (title or "").strip()
    if not raw:
        return "", False
    if _placebo_is_the_allocation(raw):
        return PLACEBO, True

    index = taxonomy.drug_index()
    lowered = raw.lower()
    # Longest alias first so "upadacitinib" wins over a shorter incidental substring, and
    # word-boundary matched because the catalog contains aliases as short as "alli"
    # (orlistat) that would otherwise fire inside unrelated molecule names.
    for alias in sorted(index, key=len, reverse=True):
        if alias_pattern(alias).search(lowered):
            return index[alias]["canonical"], False

    # Uncurated: reduce to the molecule so it behaves like a curated node. Falls back to
    # the raw title if stripping leaves nothing, because an unrecognised arm is still a
    # real arm and dropping it would silently shrink the network.
    return molecule_label(raw) or raw, False


def is_placebo(title: str | None) -> bool:
    """True when placebo is what this arm was randomised to receive.

    Deliberately not "the label mentions placebo" — an add-on arm mentions one and is not a
    placebo arm. See ``_placebo_is_the_allocation`` for why order is the discriminator.
    """
    return _placebo_is_the_allocation((title or "").strip())


def _agent_positions(lowered: str) -> dict[str, int]:
    """``{canonical name: first character offset}`` for every curated agent in *lowered*.

    Longest alias first, and each match CLAIMS its span so a shorter alias cannot fire
    inside a longer one already matched. Without this, "adalimumab-aacf" would report both
    the biosimilar and Humira, and a biosimilar arm would masquerade as a combination.

    Offsets are returned rather than discarded because label ORDER is what separates an
    add-on arm from a crossover one — see ``_placebo_is_the_allocation``.
    """
    index = taxonomy.drug_index()
    claimed: list[tuple[int, int]] = []
    first_seen: dict[str, int] = {}
    for alias in sorted(index, key=len, reverse=True):
        for match in alias_pattern(alias).finditer(lowered):
            if any(match.start() < end and start < match.end() for start, end in claimed):
                continue
            claimed.append((match.start(), match.end()))
            canonical = index[alias]["canonical"]
            if canonical not in first_seen or match.start() < first_seen[canonical]:
                first_seen[canonical] = match.start()
    return first_seen


def _placebo_is_the_allocation(raw: str) -> bool:
    """True when the placebo named in *raw* is what this arm was randomised to receive.

    A label naming both placebo and an active agent is one of two quite different arms, and
    resolving both to the placebo node corrupts the single node every indirect comparison is
    chained through: an active arm's response rate gets banked as a placebo response rate,
    which inflates the anchor rather than merely mislabelling one node.

    **Order settles it, because registries name the randomised allocation first.**

    * ``"Placebo / Upadacitinib 15 mg"`` — placebo through the primary window, then a
      crossover. The arm received no active drug while the endpoint was measured, so it is
      the placebo node and must stay one.
    * ``"Guselkumab 100 mg q4w Plus Placebo"`` — guselkumab from randomisation, with a
      placebo standing in for the comparator in a double-dummy design. Guselkumab's node.
    * ``"Placebo Plus MTX"`` — placebo against a methotrexate background every arm shares.
      Still the placebo node, and the reason this cannot simply prefer a curated agent:
      methotrexate is curated precisely because it is the usual background arm.

    Nothing here decides whether an add-on arm deserves a node of its own. That is the same
    kind of question as dose pooling and belongs to ``dose_policy`` under an approved
    protocol. This only decides which node the arm is not silently pooled into.
    """
    lowered = raw.lower()
    placebo = _PLACEBO_RE.search(lowered)
    if placebo is None:
        return False
    positions = _agent_positions(lowered)
    if not positions:
        return True
    return placebo.start() < min(positions.values())


def agents_in(title: str | None) -> tuple[str, ...]:
    """Every curated agent named in *title*, ordered as they appear.

    ``canonical_treatment`` answers "which node is this arm", and for a combination it can
    only answer with one of them: "Part 1 ADA MTX" resolves to Humira and the methotrexate
    is gone. This reports the whole arm instead, so the combination survives to be decided
    on rather than being silently discarded at parse time.

    That separation is deliberate and mirrors dose. Dose is kept structured on the arm
    record precisely so ``dose_policy`` can pool or separate later; a combination is the
    same kind of decision and needs the same kind of surviving evidence. Nothing here
    decides whether "Humira + MTX" is its own node.

    Placebo is not an agent and is excluded; ``canonical_treatment`` reports that separately.
    """
    raw = (title or "").strip()
    if not raw:
        return ()
    positions = _agent_positions(raw.lower())
    return tuple(sorted(positions, key=lambda name: positions[name]))


def is_combination(title: str | None) -> bool:
    """True when a label names more than one curated agent."""
    return len(agents_in(title)) > 1


def is_uninformative_label(label: str | None) -> bool:
    """True when a resolved node name carries no treatment identity of its own.

    Reported for curation rather than dropped at parse time. The arm is real even when its
    title is useless, and silently discarding it would shrink the network — the same reason
    an uncurated agent keeps its label. The difference is that an uncurated agent can be
    fixed by adding a catalog entry, whereas "A" cannot be fixed at all: the study record
    simply never said what that arm received.

    Runs the label back through the stripper rather than inspecting it directly, because
    ``canonical_treatment`` falls back to the raw title when stripping empties it. "Group A"
    therefore arrives here intact, and only re-stripping reveals that nothing is left.
    """
    text = molecule_label(label or "")
    return not text or bool(_ENUMERATOR_ONLY.match(text))


def is_aggregate_label(label: str | None) -> bool:
    """True when a results group reports a total across arms rather than one arm.

    None of the other predicates reject ``"Total"``. It is not an enumerator, names no
    class, and ``canonical_treatment`` hands it back unchanged as a perfectly plausible node
    name — so it would pool across studies into a single fabricated comparator and close
    loops the evidence never contained. That is the ``Standard Care`` problem wearing a
    friendlier label, and it is worth its own check because registries post these rows
    beside the real arms rather than in a separate section.

    Naming a treatment settles it. The aggregate word alone is too blunt a signal — "Overall
    Survival Cohort Rinvoq" opens with one and is still Rinvoq's row — so a label that
    resolves to an agent or to placebo is an arm no matter how it begins. What remains is the
    genuine case: an opening aggregate word and no treatment anywhere in the label.
    """
    text = (label or "").strip()
    if not _AGGREGATE_TERMS.match(text):
        return False
    return not agents_in(text) and not is_placebo(text)


def is_class_level_label(label: str | None) -> bool:
    """True when a label names a drug class or a care strategy instead of a molecule.

    Callers use this to keep the study out of a molecule-level network entirely, rather than
    to rewrite the label. There is no molecule to recover: "TNFi" is not a bad spelling of a
    drug name, it is a different kind of claim.
    """
    text = label or ""
    return any(pattern.search(text) for pattern in _CLASS_LEVEL_TERMS)


def is_class_level_node(name: str | None) -> bool:
    """True when a RESOLVED node names a class or strategy rather than a molecule.

    Curated agents are exempt, so methotrexate stays a molecule even though it is itself a
    csDMARD — the drug is not its class.

    Shared by ingestion screening and the network builder on purpose. The builder cannot
    rely on ingestion having screened: it reads every study for the indication, including
    ones ingested before this rule existed, so both sides have to apply the same test and
    they must not be able to drift apart.
    """
    if not name:
        return False
    if taxonomy.drug_class_for(name):
        return False
    return is_class_level_label(name)
