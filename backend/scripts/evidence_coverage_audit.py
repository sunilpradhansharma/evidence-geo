"""Phase 0 — coverage and feasibility audit. READ-ONLY; writes nothing to the database.

Produces the per-indication matrix that sets the delivery tier order. Feasibility depends
on far more than "do trial results exist", so every field below can independently change
scope:

    direct evidence available          may remove the need for any indirect comparison
    suitable published NMA             may remove the need for internal computation
    arm-level results posted           required for Level-3 computation
    network connected                  required for any indirect estimate
    shared comparator per pair         placebo/Stelara/Humira must connect the classes
    placebo response by route          measures the oral-vs-injectable transitivity risk
    induction/maintenance separable    gates Tier 3 (IBD) entirely
    regulatory drug facts obtainable   gates the drug-facts deliverable independently
    source licensing                   automatic vs governed manual ingestion

**This is a scoping gate, not a platform gate.** A disconnected or non-comparable network
disables Level-3 computation *for that indication only*. Drug facts, published evidence,
multi-TA resolution, question generation, AI evaluation and structured evidence-gap
reporting all still ship — and "this comparison is not estimable, here is why" is a
legitimate product output.

The placebo-response-by-route field is a **measurement, not an assumption**. If oral and
subcutaneous placebo rates turn out close in a given indication, route-mixing is a
non-issue there and the analysis protocol can say so with evidence behind it.

Requires network access; every call degrades rather than raising, so a partial run still
produces a usable matrix with the gaps marked.

    python -m scripts.evidence_coverage_audit --indication "Psoriatic Arthritis"
    python -m scripts.evidence_coverage_audit --all --out ../docs/PHASE0_COVERAGE.md
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import taxonomy  # noqa: E402
from app.evidence import licensing, topology  # noqa: E402
from app.evidence.sources import clinicaltrials as ctg  # noqa: E402
from app.evidence.sources import pubmed  # noqa: E402

# Trials are searched per (indication, drug) because a registry condition search alone
# returns thousands of records, most of them irrelevant to this programme.
AUDIT_DRUGS = ("Rinvoq", "Skyrizi", "Tremfya", "Humira")


@dataclass
class IndicationAudit:
    """One row of the Phase 0 matrix."""

    indication: str
    canonical_outcomes: tuple[str, ...] = ()
    studies_found: int = 0
    studies_screened_out: int = 0
    studies_with_results: int = 0
    arm_level_available: int = 0
    network: topology.Topology | None = None
    published_nmas: list[pubmed.Citation] = field(default_factory=list)
    # {(canonical_outcome_id, route): [rate, ...]} — keyed by outcome so the route
    # comparison is like-for-like. A PASI75 placebo rate and an ACR20 placebo rate are
    # not comparable, and averaging across them would fabricate a route difference.
    placebo_rates: dict[tuple[str, str], list[float]] = field(default_factory=dict)
    pair_connectivity: dict[str, str] = field(default_factory=dict)
    phase_counts: dict[str, int] = field(default_factory=dict)
    phase_ambiguous: int = 0
    curated_nodes: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def focus_drugs_present(self) -> tuple[str, ...]:
        nodes = set(self.network.nodes) if self.network else set()
        return tuple(d for d in AUDIT_DRUGS if d in nodes)

    @property
    def focus_network_connected(self) -> bool:
        """Are the drugs we actually care about in ONE component?

        This replaces whole-graph connectivity, which asked the wrong question. A search
        sweeps in trials of unrelated agents that form their own isolated islands; those
        islands make `Topology.is_connected` false while saying nothing about whether
        Rinvoq can be compared with Tremfya.
        """
        present = self.focus_drugs_present
        if not self.network or len(present) < 2:
            return False
        first = present[0]
        return all(self.network.are_connected(first, other) for other in present[1:])

    @property
    def level3_feasible(self) -> bool:
        """Whether an internal synthesis is possible for the drugs in scope."""
        return bool(self.focus_network_connected and self.arm_level_available >= 2)

    @property
    def catalog_coverage(self) -> float | None:
        """Share of network nodes that resolved to a curated catalog entry.

        Low coverage means the node count is inflated by uncurated labels, so treat the
        topology numbers as an upper bound rather than a measurement.
        """
        if not self.network or not self.network.nodes:
            return None
        return 100.0 * self.curated_nodes / len(self.network.nodes)

    @property
    def phase_separable(self) -> bool:
        """True when induction and maintenance studies are both identifiable."""
        return self.phase_counts.get("INDUCTION", 0) > 0 and self.phase_counts.get("MAINTENANCE", 0) > 0

    def placebo_route_spread(self) -> tuple[str, float] | None:
        """Largest between-route spread WITHIN a single canonical outcome.

        The empirical input to ``placebo_response_policy``. Returns the outcome it was
        measured on, because a spread is only interpretable alongside the endpoint it
        was measured for.
        """
        worst: tuple[str, float] | None = None
        by_outcome: dict[str, list[float]] = {}
        for (outcome_id, _route), rates in self.placebo_rates.items():
            if rates:
                by_outcome.setdefault(outcome_id, []).append(statistics.mean(rates))
        for outcome_id, means in by_outcome.items():
            if len(means) >= 2:
                spread = max(means) - min(means)
                if worst is None or spread > worst[1]:
                    worst = (outcome_id, spread)
        return worst


async def audit_indication(indication: str, *, page_size: int = 40) -> IndicationAudit:
    """Audit one indication. Never raises; failures land in ``errors``."""
    audit = IndicationAudit(
        indication=indication,
        canonical_outcomes=taxonomy.canonical_outcomes_for_disease(indication),
    )

    study_arms: dict[str, list[str]] = {}
    seen: set[str] = set()

    for drug in AUDIT_DRUGS:
        result = await ctg.search(condition=indication, intervention=drug, page_size=page_size)
        if not result.ok:
            audit.errors.append(f"ClinicalTrials.gov search failed for {drug}: {result.reason}")
            continue

        for record in (result.payload or {}).get("studies") or []:
            parsed = ctg.parse(
                ctg.FetchResult(
                    ok=True,
                    source_type=ctg.SOURCE_TYPE,
                    source_identifier="",
                    payload=record,
                ),
                indication=indication,
            )
            if parsed is None or parsed.study.study_id in seen:
                continue
            seen.add(parsed.study.study_id)

            # Interventional does not imply randomised. A single-arm or open-label
            # extension study contributes no randomised comparison, and admitting one
            # would invent network edges that no randomisation supports.
            if not parsed.study.is_randomised:
                audit.studies_screened_out += 1
                continue

            audit.studies_found += 1
            treatments = [a.treatment for a in parsed.arms if a.treatment]
            if len(treatments) >= 2:
                study_arms[parsed.study.study_id] = treatments

            if parsed.has_results:
                audit.studies_with_results += 1
                if any(o.events is not None and o.sample_size for o in parsed.outcomes):
                    audit.arm_level_available += 1

            phase = parsed.study.treatment_phase
            audit.phase_counts[phase] = audit.phase_counts.get(phase, 0) + 1
            if any("induction and maintenance" in w for w in parsed.warnings):
                audit.phase_ambiguous += 1

            _collect_placebo_rates(parsed, audit)

    audit.network = topology.build(study_arms)
    audit.curated_nodes = sum(
        1 for node in audit.network.nodes if taxonomy.drug_class_for(node) or node == "Placebo"
    )
    audit.pair_connectivity = _pair_connectivity(audit.network)

    await _audit_published_syntheses(indication, audit)
    return audit


def _collect_placebo_rates(parsed: ctg.ParsedStudy, audit: IndicationAudit) -> None:
    """Record placebo response rates keyed by the route of the ACTIVE comparator.

    An oral trial's placebo is a tablet; a biologic trial's placebo is an injection. The
    route that matters for the transitivity threat is therefore the one the active arms
    used, not the placebo arm's own (unrecorded) route.
    """
    active_routes = {
        a.administration_route for a in parsed.arms if not a.is_placebo and a.administration_route
    }
    if len(active_routes) != 1:
        return  # a route-mixed trial cannot attribute its placebo rate to one route
    route = active_routes.pop()

    placebo_arm_ids = {a.arm_id for a in parsed.arms if a.is_placebo}
    for outcome in parsed.outcomes:
        if (
            outcome.arm_id in placebo_arm_ids
            and outcome.canonical_outcome_id
            and outcome.events is not None
            and outcome.sample_size
        ):
            rate = 100.0 * outcome.events / outcome.sample_size
            audit.placebo_rates.setdefault((outcome.canonical_outcome_id, route), []).append(rate)


def _pair_connectivity(network: topology.Topology) -> dict[str, str]:
    """How each focus/full-depth pair is connected: direct, via a comparator, or not."""
    findings: dict[str, str] = {}
    for i, a in enumerate(AUDIT_DRUGS):
        for b in AUDIT_DRUGS[i + 1:]:
            key = f"{a} vs {b}"
            if a not in network.nodes or b not in network.nodes:
                findings[key] = "absent from network"
            elif network.has_direct_evidence(a, b):
                findings[key] = "DIRECT"
            elif shared := network.shared_comparators(a, b):
                findings[key] = f"via {', '.join(shared)}"
            elif network.are_connected(a, b):
                findings[key] = f"indirect, {len(network.path(a, b)) - 1} hops"
            else:
                findings[key] = "NOT CONNECTED"
    return findings


async def _audit_published_syntheses(indication: str, audit: IndicationAudit) -> None:
    query = f'("{indication}"[Title/Abstract]) AND ("network meta-analysis"[Title/Abstract])'
    search = await pubmed.search(query, retmax=30)
    if not search.ok:
        audit.errors.append(f"PubMed search failed: {search.reason}")
        return
    pmids = pubmed.parse_search(search)
    if not pmids:
        return
    summary = await pubmed.summaries(pmids)
    if not summary.ok:
        audit.errors.append(f"PubMed summary failed: {summary.reason}")
        return
    audit.published_nmas = pubmed.synthesis_candidates(pubmed.parse_summaries(summary))


def render(audits: list[IndicationAudit]) -> str:
    """Markdown matrix. Tier order should follow the Level-3 feasibility column."""
    lines = [
        "# Phase 0 — coverage and feasibility audit",
        "",
        "Read-only snapshot. A `no` in **L3 feasible** disables internal synthesis for that",
        "indication only — drug facts, published evidence, question generation and",
        "structured evidence gaps all still ship.",
        "",
        "**Focus connected** asks whether Rinvoq/Skyrizi/Tremfya/Humira sit in ONE component,",
        "not whether the whole swept graph is connected. Unrelated agents pulled in by the",
        "search form their own islands and say nothing about the comparisons in scope.",
        "",
        "**Catalog %** is the share of nodes resolving to a curated drug. Where it is low the",
        "node count is inflated by uncurated labels — treat topology figures as an upper bound.",
        "",
        "| Indication | RCTs | Screened out | With results | Arm-level | Nodes | Catalog % | Focus connected | Indep. loops | NMAs | L3 feasible |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | :---: | ---: | ---: | :---: |",
    ]
    for a in audits:
        n = a.network
        coverage = f"{a.catalog_coverage:.0f}%" if a.catalog_coverage is not None else "n/a"
        lines.append(
            f"| {a.indication} | {a.studies_found} | {a.studies_screened_out} | "
            f"{a.studies_with_results} | {a.arm_level_available} | {len(n.nodes) if n else 0} | "
            f"{coverage} | {'yes' if a.focus_network_connected else 'no'} | "
            f"{n.independent_loop_count if n else 0} | {len(a.published_nmas)} | "
            f"{'yes' if a.level3_feasible else 'NO'} |"
        )

    for a in audits:
        lines += ["", f"## {a.indication}", ""]
        lines.append(f"- **Canonical outcomes**: {', '.join(a.canonical_outcomes) or 'none declared'}")
        lines.append(
            f"- **Focus drugs in network**: {', '.join(a.focus_drugs_present) or 'none'}"
        )

        phases = ", ".join(f"{k} {v}" for k, v in sorted(a.phase_counts.items())) or "none"
        ambiguous = (
            f" ({a.phase_ambiguous} trial(s) name both phases and were left PRIMARY — their "
            "substudies must be split before either can enter a network)"
            if a.phase_ambiguous else ""
        )
        lines.append(
            f"- **Induction/maintenance**: {phases}{ambiguous}. "
            f"Separable: {'yes' if a.phase_separable else 'no'}"
        )

        spread = a.placebo_route_spread()
        if spread is None:
            measured = sorted({oid for oid, _ in a.placebo_rates})
            reason = (
                f"only one route observed for {', '.join(measured)}"
                if measured else "no placebo arm had a mapped endpoint with events and N"
            )
            lines.append(f"- **Placebo response by route**: not measurable — {reason}")
        else:
            outcome_id, points = spread
            detail = ", ".join(
                f"{route} {statistics.mean(rates):.1f}% (n={len(rates)})"
                for (oid, route), rates in sorted(a.placebo_rates.items())
                if oid == outcome_id
            )
            verdict = (
                "route-mixing is a non-issue here; CONTRAST_ONLY is defensible"
                if points < 5
                else "material difference; SENSITIVITY_REQUIRED or SUBGROUP_BY_ROUTE"
            )
            lines.append(
                f"- **Placebo response by route** (on {outcome_id}): {detail} — "
                f"spread {points:.1f}pp. {verdict}"
            )

        lines.append("- **Pair connectivity**:")
        for pair, finding in sorted(a.pair_connectivity.items()):
            lines.append(f"  - {pair}: {finding}")

        if a.published_nmas:
            lines.append("- **Candidate published NMAs** (Level-2 input):")
            for citation in a.published_nmas[:10]:
                lines.append(f"  - {citation.formatted()}")
        else:
            lines.append("- **Candidate published NMAs**: none found")

        lines.append(
            f"- **Source licensing**: ClinicalTrials.gov "
            f"{licensing.license_for_source('CLINICALTRIALS_GOV')}, "
            f"PubMed metadata {licensing.license_for_source('PUBMED')}, "
            f"full text per-article (see licence_for_pmc_record)"
        )
        for error in a.errors:
            lines.append(f"- **Degraded**: {error}")

    return "\n".join(lines) + "\n"


async def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 0 coverage + feasibility audit (read-only).")
    ap.add_argument("--indication", action="append", help="Repeatable. Defaults to --all.")
    ap.add_argument("--all", action="store_true", help="Audit every declared indication.")
    ap.add_argument("--page-size", type=int, default=40)
    ap.add_argument("--out", help="Write markdown here instead of stdout.")
    args = ap.parse_args()

    indications = list(args.indication or ())
    if args.all or not indications:
        indications = list(taxonomy.diseases())

    unknown = [i for i in indications if not taxonomy.canonical_disease(i)]
    if unknown:
        ap.error(
            f"unknown indication(s): {', '.join(unknown)}. "
            f"Declared: {', '.join(taxonomy.diseases())}"
        )

    print(f"Auditing {len(indications)} indication(s). This makes live API calls.\n", file=sys.stderr)
    audits = []
    for indication in indications:
        print(f"  {indication} …", file=sys.stderr)
        audits.append(await audit_indication(indication, page_size=args.page_size))

    report = render(audits)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"\nWrote {args.out}", file=sys.stderr)
    else:
        print(report)


if __name__ == "__main__":
    asyncio.run(main())
