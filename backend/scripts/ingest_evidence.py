"""Ingest clinical evidence for one indication and assemble its network.

This is the runner that turns the Phase 3B adapters and the Phase 6 resolver into a working
pipeline. Before it, ``GET /comparisons/resolve`` had nothing to resolve against.

What it does, in order:

  1. discover randomised trials for the indication (a search per focus drug)
  2. fetch each record in full and persist study / arms / outcome rows
  3. optionally mark them VERIFIED, recording who said so
  4. build the ``EvidenceNetwork`` for one canonical outcome and propose its memberships
  5. report its topology **twice** — endpoint-level, then re-read through the governing
     protocol's approved time window, which is what a resolve will actually see

**DRY RUN by default.** A dry run still queries ClinicalTrials.gov — it has to, to report
what it would ingest — but writes nothing.

**On ``--verify-as``.** Verification asserts that a person checked the extraction against
the source. This flag records a name; it does **not** authenticate one, because there is no
RBAC in this tree. Use it to get a pilot moving, and understand that the resulting rows
carry an audit trail rather than an authorisation. Without it, studies land MAPPED or
EXTRACTED and the resolver will correctly refuse to compute on them.

Run locally (repo-root .venv, cwd backend):
    python -m scripts.ingest_evidence --indication "Psoriatic Arthritis"
    python -m scripts.ingest_evidence --indication "Psoriatic Arthritis" \
        --outcome PSA_ACR50_W16 --protocol PSA_ACR50_W16_PRIMARY \
        --verify-as "Dr Helen Carter" --commit

Inside the prod container:
    docker exec -i evidence-monitoring-agent python -m scripts.ingest_evidence \
        --indication "Psoriatic Arthritis" --outcome PSA_ACR50_W16
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import taxonomy  # noqa: E402
from app.models.database import AsyncSessionLocal, init_db  # noqa: E402
from app.services import evidence_ingestion_service as ingest  # noqa: E402
from app.services import network_builder_service as builder  # noqa: E402

DEFAULT_DRUGS = ("Rinvoq", "Skyrizi", "Tremfya", "Humira")


def _clip(text: str, n: int = 72) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "\u2026"


def _bucket(
    title: str, counts: dict[str, int], sources: dict[str, list[str]],
    *advice: str, show: int = 40,
) -> None:
    """Print one frequency-sorted label bucket, or nothing when it is empty."""
    if not counts:
        return
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    arms = sum(counts.values())
    print(f"\n--- {title}: {len(ranked)} distinct, {arms} arms ---")
    for line in advice:
        print(f"    {line}")
    for label, count in ranked[:show]:
        studies = sources.get(label) or []
        where = ", ".join(studies[:2]) + (f" +{len(studies) - 2}" if len(studies) > 2 else "")
        print(f"  {count:4}  {_clip(label, 44):44}  {where}")
    if len(ranked) > show:
        print(f"  ... and {len(ranked) - show} more")


def _print_protocol_scope(build: builder.BuildReport) -> None:
    """Print the same network re-read through the governing protocol's approved window.

    The block above it is endpoint-level, so on its own it announces nodes an approved
    protocol may exclude — the live PsA run reported Rinvoq connected while every
    protocol-scoped resolve dropped it. The builder does not apply the window (that is the
    protocol's judgement and the resolver's job); this is where the difference is disclosed.
    """
    scope = build.protocol_scope
    if scope is None:
        print(
            "\n--- protocol-scoped topology: none ---\n"
            "No protocol governs this network, so nothing narrows the window above and a\n"
            "resolve will see the same graph. Pass --protocol to check what an approved\n"
            "window would actually leave answerable."
        )
        return

    scoped = scope.topology_summary
    lo, hi = scope.approved_time_window
    print(
        f"\n--- protocol-scoped topology: {scope.protocol_id} "
        f"(weeks {lo:g}-{hi:g}) — what a resolve sees ---"
    )
    print(f"nodes                : {scoped.get('node_count')}  {scoped.get('nodes')}")
    print(f"edges                : {scoped.get('edge_count')}")
    print(f"connected            : {scoped.get('is_connected')}")
    print(f"components           : {scoped.get('component_count')}")
    print(f"loops (independent)  : {scoped.get('independent_loop_count')}")
    print(f"studies in window    : {scoped.get('study_count')} of {len(build.proposed_studies)}")

    if not scope.narrows:
        print("This protocol's window costs the network nothing. The figures agree.")
        return

    if scope.nodes_lost:
        print(f"NODES LOST TO WINDOW : {', '.join(scope.nodes_lost)}")
        print(
            "  ^ present in the endpoint-level graph above and NOT answerable under this\n"
            "    protocol. A comparison naming one of these resolves to an evidence gap."
        )
    if scope.studies_out_of_window:
        listed = ", ".join(scope.studies_out_of_window[:8])
        more = len(scope.studies_out_of_window) - 8
        print(
            f"studies out of window: {len(scope.studies_out_of_window)}  "
            f"{listed}{f' +{more}' if more > 0 else ''}"
        )
    print(
        "  These stay PROPOSED members on purpose. The window belongs to the protocol and\n"
        "  can be re-approved without re-harvesting, so the builder discloses it rather\n"
        "  than enforcing a second copy of the same judgement."
    )


async def main() -> None:
    ap = argparse.ArgumentParser(
        description="Ingest clinical evidence for one indication and build its network."
    )
    ap.add_argument("--indication", required=True, help='e.g. "Psoriatic Arthritis"')
    ap.add_argument(
        "--drugs", nargs="*", default=list(DEFAULT_DRUGS),
        help="Interventions to search for (default: the four full-depth drugs).",
    )
    ap.add_argument(
        "--outcome", default=None,
        help="Canonical outcome id to build a network for, e.g. PSA_ACR50_W16.",
    )
    ap.add_argument(
        "--protocol", default=None,
        help=("Analysis protocol id for the network. Its approved time window is "
              "REPORTED against the built topology, never applied to it."),
    )
    ap.add_argument("--phase", default="PRIMARY", help="PRIMARY | INDUCTION | MAINTENANCE.")
    ap.add_argument("--stratum", default=None, help="Population stratum, e.g. BIO_NAIVE.")
    ap.add_argument("--limit", type=int, default=None, help="Cap studies ingested (for a smoke run).")
    ap.add_argument(
        "--verify-as", default=None, metavar="NAME",
        help="Mark ingested studies VERIFIED, recording NAME. Recorded, NOT authenticated.",
    )
    ap.add_argument("--commit", action="store_true", help="Write changes (default: dry run).")
    args = ap.parse_args()

    known = taxonomy.canonical_outcomes_for_disease(args.indication)
    if args.outcome and known and args.outcome not in known:
        print(
            f"ERROR: {args.outcome!r} is not a canonical outcome for {args.indication!r}.\n"
            f"       Known: {', '.join(known) or '(none configured)'}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    mode = "COMMIT" if args.commit else "DRY RUN"
    print(f"Evidence ingestion  [{mode}]")
    print(f"indication : {args.indication}")
    print(f"drugs      : {', '.join(args.drugs)}")
    print(f"outcome    : {args.outcome or '(none — ingest only, no network)'}")
    if known:
        print(f"canonical  : {', '.join(known)}")
    print()

    await init_db()
    async with AsyncSessionLocal() as db:
        print("Querying ClinicalTrials.gov (throttled to ~50 req/min)...")
        # commit is passed through, not left to the rollback below: the service used to
        # commit unconditionally, which made that rollback a no-op and meant a dry run
        # wrote everything while announcing it had written nothing.
        report = await ingest.ingest_indication(
            db, args.indication, drugs=args.drugs, limit=args.limit,
            commit=bool(args.commit),
        )

        print(f"\ndiscovered           : {report.discovered}")
        print(f"screened out         : {report.screened_out}")
        # Broken down by reason: screening is the only step that removes real randomised
        # evidence, so a bare total is not something a reviewer can check.
        by_reason: dict[str, int] = {}
        for _study_id, reason in report.screened_out_detail:
            key = reason.split(":")[0]
            by_reason[key] = by_reason.get(key, 0) + 1
        for reason, count in sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"                       {count:4}  {reason}")
        print(f"ingested             : {report.ingested}")
        print(f"updated              : {report.updated}")
        print(f"skipped              : {report.skipped}")

        if report.fetch_failures:
            print(f"\n--- fetch failures ({len(report.fetch_failures)}) ---")
            for identifier, reason in report.fetch_failures[:20]:
                print(f"  {identifier:24} {_clip(reason)}")

        # Named per study, not just counted. A drug that appears only in screened studies
        # is absent from every label bucket, so without this the census would quietly stop
        # mentioning it and nobody would learn the catalog is missing it.
        if report.screened_out_detail:
            print(f"\n--- studies screened out ({len(report.screened_out_detail)}) ---")
            for identifier, reason in report.screened_out_detail[:25]:
                print(f"  {identifier:16} {_clip(reason, 86)}")
            if len(report.screened_out_detail) > 25:
                print(f"  ... and {len(report.screened_out_detail) - 25} more")

        # Phase 0 measured 12-20% catalog coverage, so a run that surfaces NO uncurated
        # labels is the surprising outcome. These become junk nodes in every network built
        # from this indication until they are added to the drug catalog.
        #
        # Sorted by frequency before truncating: an arbitrary slice of an insertion-ordered
        # dict hides the labels that cost the most nodes, which is the whole point of the
        # figure. Both counts are printed so a truncated list is visibly truncated.
        _bucket(
            "uncurated treatment labels", report.unmapped_treatments, report.label_studies,
            "Real agents the catalog does not know. Add an entry (or an alias) to",
            "brands.yaml — each one is currently its own junk node.",
        )
        _bucket(
            "arms whose label names no treatment", report.uninformative_arms,
            report.label_studies,
            "Not fixable in config: the registry record never said what these arms",
            "received. Curation has to read the named study. Kept on record, but the",
            "builder refuses them a network — two trials' 'A' arms are not one node.",
        )
        _bucket(
            "class-level / strategy arms (studies SCREENED OUT)", report.class_level_arms,
            report.label_studies,
            "These name a drug class or a care strategy, not a molecule. Their studies",
            "are excluded: comparing them to a drug node would assume class equivalence,",
            "and pooling two trials' 'Standard Care' invents a common comparator.",
        )

        flagged = [s for s in report.studies if s.warnings]
        if flagged:
            print(f"\n--- studies with extraction warnings ({len(flagged)}) ---")
            for study in flagged[:15]:
                print(f"  {study.study_id}")
                for warning in study.warnings[:3]:
                    print(f"      {_clip(warning, 88)}")

        # A dry run continues THROUGH network construction and rolls back at the very end.
        # Returning here instead meant the one thing a reviewer most needs to see before
        # committing — the graph this data actually produces — was only visible after
        # writing it. Nothing below commits unless --commit was passed.
        try:
            if args.verify_as and not args.commit:
                print(
                    f"\nDRY RUN: not verifying as {args.verify_as!r}. Recording a reviewer's "
                    "name is an assertion, not a simulation."
                )
            elif args.verify_as:
                ingested = [s for s in report.studies if s.action in ("INGESTED", "UPDATED")]
                print(f"\nVerifying {len(ingested)} study/studies as {args.verify_as!r}.")
                print(
                    "NOTE: this name is RECORDED, not authenticated — there is no RBAC here."
                )
                for study in ingested:
                    try:
                        await ingest.verify_study(
                            db, study.study_id, verified_by=args.verify_as
                        )
                    except ingest.IngestionError as e:
                        print(f"  warn: {study.study_id} not verified - {e}")
            else:
                print(
                    "\nNo --verify-as given, so studies remain unverified and the resolver "
                    "will refuse to compute on them. That is the correct default."
                )

            if not args.outcome:
                print("\nDone (ingest only). Pass --outcome to build a network.")
                return

            build = await builder.build_network(
                db,
                indication=args.indication,
                canonical_outcome_id=args.outcome,
                treatment_phase=args.phase,
                population_stratum=args.stratum,
                protocol_id=args.protocol,
                commit=bool(args.commit),
            )
            graph = build.topology_summary
            print(f"\n=== network {build.network_id} ===")
            print(f"created              : {build.created}")
            print(f"studies proposed     : {len(build.proposed_studies)}")
            # Labelled, because these figures are PRE-PROTOCOL. The first live PsA run
            # printed 8 connected nodes including Rinvoq while every protocol-scoped resolve
            # saw 6 without it, and nothing here said the numbers were the wider window.
            print("\n--- endpoint-level topology (the whole outcome window, pre-protocol) ---")
            print(f"nodes                : {graph.get('node_count')}  {graph.get('nodes')}")
            print(f"edges                : {graph.get('edge_count')}")
            print(f"connected            : {graph.get('is_connected')}")
            print(f"components           : {graph.get('component_count')}")
            print(f"loops (all)          : {graph.get('loop_count')}")
            print(f"loops (independent)  : {graph.get('independent_loop_count')}")
            print(f"multi-arm studies    : {graph.get('has_multi_arm_studies')}")
            print(f"simple star          : {graph.get('is_simple_star')}")
            _print_protocol_scope(build)

            if build.excluded:
                print(f"\n--- excluded from this network ({len(build.excluded)}) ---")
                for study_id, reason in build.excluded[:20]:
                    print(f"  {study_id:16} {_clip(reason, 84)}")
                if len(build.excluded) > 20:
                    print(f"  ... and {len(build.excluded) - 20} more")

            print(
                "\nMemberships are PROPOSED and the network is DRAFT. Both are deliberate: "
                "inclusion and ratification are human decisions.\nResolve a pair with:\n"
                f"  GET /comparisons/resolve?network_id={build.network_id}"
                "&treatment_a=Rinvoq&treatment_b=Humira"
            )
        finally:
            if not args.commit:
                await db.rollback()
                print(
                    "\nDRY RUN - roll back complete, no rows written. Re-run with --commit "
                    "to apply.\n(init_db did create any missing tables; that is schema, "
                    "not data, and is not rolled back.)"
                )


if __name__ == "__main__":
    asyncio.run(main())
