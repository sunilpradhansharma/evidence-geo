"""Harvest pipeline: search -> extract -> scrub -> dedupe -> classify -> stage.

Staged rows are NEVER auto-active. A human reviewer promotes them (creating a PENDING
Question that still needs Medical-Affairs approval). Adverse-event candidates are
quarantined for safety/PV review and excluded from promotion by default.
"""
import hashlib
import json
import re
from itertools import zip_longest

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.intent_classifier import classify_by_rules
from app.config import taxonomy
from app.config.settings import load_yaml_config
from app.guardrails import adverse_event, injection
from app.harvest import classify, extractor, scrub
from app.harvest.sources.tavily import TavilySource
from app.models.harvested_question import HarvestedQuestion
from app.utils.logging import get_logger

logger = get_logger("harvest.pipeline")

_WS = re.compile(r"\s+")
_NONWORD = re.compile(r"[^a-z0-9 ]+")


def _dedupe_hash(text: str) -> str:
    norm = _NONWORD.sub("", _WS.sub(" ", text.lower()).strip())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def _comparators_for(brand: str, ta_key: str, block_competitors: list[str]) -> list[str]:
    """Who this brand is actually compared against, disease overlay first.

    The area block's competitor list is a union across every indication in the area, so
    reading it alone both offers irrelevant pairs and buries relevant ones. Tremfya is
    the clearest case: it is a real comparator for Rinvoq in psoriatic arthritis,
    ulcerative colitis and Crohn's, and the overlay is the only place that says so.
    The block list is appended afterwards rather than dropped, so an area with no
    overlay entries still produces comparison queries.
    """
    names: list[str] = []
    for disease in taxonomy.diseases_for_key(ta_key):
        if brand not in taxonomy.brands_for_disease(disease):
            continue
        for competitor in taxonomy.competitors_for_disease(disease):
            if competitor not in names:
                names.append(competitor)
    for competitor in block_competitors:
        if competitor not in names:
            names.append(competitor)
    return [n for n in names if n.lower() != brand.lower()]


def _round_robin(buckets: list[list[str]]) -> list[str]:
    """Take one from each bucket in turn, so a cap truncates every bucket's tail equally."""
    out: list[str] = []
    for row in zip_longest(*buckets):
        out.extend(q for q in row if q is not None)
    return out


def _by_rank(buckets: list[list[str]]) -> list[str]:
    """Flatten rank buckets in order: every brand's best pairing before anyone's second.

    Concatenation, NOT round-robin. Bucket ``k`` already holds one entry per brand, so
    zipping across buckets would emit brand one's whole comparator list first and push
    later brands past the cap — the same starvation the old ``comps[:2]`` caused.
    """
    return [q for bucket in buckets for q in bucket]


def _expand(templates: list[str], brands_cfg: dict, *, ta_filter: str | None = None,
            brand_filter: str | None = None, landscape: bool = False) -> list[str]:
    """Substitute brand/competitor/indication names into query templates.

    Queries are built per therapeutic area and then round-robin interleaved, so a
    capped run (max_queries_per_run) gives every area fair coverage instead of
    exhausting the cap on whichever areas appear first in brands.yaml.

    ``ta_filter`` / ``brand_filter`` (case-insensitive) scope generation to a
    single therapeutic area / focus brand for targeted discovery.

    ``landscape`` (DISEASE_STATE / "All Brands" mode) widens the subject set to EVERY
    brand in the space — AbbVie focus brands AND competitors — so discovery covers the
    whole disease landscape rather than only AbbVie assets. ``brand_filter`` is ignored
    in landscape mode.

    Comparator and indication queries are interleaved BY RANK rather than truncated to
    the first two. The old cap was silently deciding the competitive scope of discovery:
    with a fixed ``comps[:2]``, no query pairing Rinvoq with Tremfya could ever be built,
    so no such question could ever be found. ``max_queries_per_run`` remains the one
    place run size is limited.
    """
    per_ta: list[list[str]] = []
    for _ta, block in (brands_cfg.get("therapeutic_areas") or {}).items():
        if ta_filter and _ta.lower() != ta_filter.lower():
            continue
        focus = [b.get("name") for b in block.get("focus_brands", []) if b.get("name")]
        if brand_filter and not landscape:
            focus = [b for b in focus if b and b.lower() == brand_filter.lower()]
        comps = [c.get("name") for c in block.get("competitors", []) if c.get("name")]
        inds = sorted({i for b in block.get("focus_brands", []) for i in b.get("indications", [])})
        # BRAND mode harvests around AbbVie focus brands; DISEASE_STATE harvests around
        # the whole landscape (focus + competitors), de-duplicated, order preserved.
        subjects = list(dict.fromkeys([*focus, *comps])) if landscape else focus

        plain: list[str] = []
        # Bucket k holds every brand's k-th ranked comparator / indication, so the cap
        # costs each brand its rarest pairings instead of costing later brands everything.
        by_comp_rank: list[list[str]] = []
        by_ind_rank: list[list[str]] = []

        def _at(buckets: list[list[str]], rank: int) -> list[str]:
            while len(buckets) <= rank:
                buckets.append([])
            return buckets[rank]

        for brand in subjects:
            brand_comps = _comparators_for(brand, _ta, comps)
            for tmpl in templates:
                if "{competitor}" in tmpl:
                    for rank, comp in enumerate(brand_comps):
                        _at(by_comp_rank, rank).append(
                            tmpl.format(brand=brand, competitor=comp, indication="")
                        )
                elif "{indication}" in tmpl:
                    for rank, ind in enumerate(inds):
                        _at(by_ind_rank, rank).append(
                            tmpl.format(brand=brand, competitor="", indication=ind)
                        )
                else:
                    plain.append(tmpl.format(brand=brand, competitor="", indication=""))

        per_ta.append([*plain, *_by_rank(by_comp_rank), *_by_rank(by_ind_rank)])

    return _round_robin(per_ta)


def build_queries(cfg: dict, cap: int, *, persona: str | None = None,
                  therapeutic_area: str | None = None,
                  brand_focus: str | None = None,
                  landscape: bool = False) -> list[tuple[str, str | None]]:
    """Build (query, persona_hint) pairs from general + per-persona templates.

    General templates carry no persona; persona_query_templates carry their persona as a
    prior. Buckets are round-robin interleaved so the cap covers every persona, then
    de-duplicated by query text.

    When ``persona`` is given, only that persona's templates are used (falling back
    to the general templates, still tagged with the persona lens) so discovery is
    scoped to that audience. ``therapeutic_area`` / ``brand_focus`` scope which
    brands the queries are built from.
    """
    brands_cfg = taxonomy.config()
    persona_cfg = cfg.get("persona_query_templates") or {}
    general = cfg.get("query_templates") or []

    def exp(tmpls: list[str]) -> list[str]:
        return _expand(tmpls, brands_cfg, ta_filter=therapeutic_area,
                       brand_filter=brand_focus, landscape=landscape)

    buckets: list[list[tuple[str, str | None]]] = []
    if persona:
        ptmpls = persona_cfg.get(persona) or []
        buckets.append([(q, persona) for q in exp(ptmpls or general)])
    else:
        if general:
            buckets.append([(q, None) for q in exp(general)])
        for p, tmpls in persona_cfg.items():
            if tmpls:
                buckets.append([(q, p) for q in exp(tmpls)])

    interleaved: list[tuple[str, str | None]] = []
    for row in zip_longest(*buckets):
        for pair in row:
            if pair is not None:
                interleaved.append(pair)

    seen: set[str] = set()
    out: list[tuple[str, str | None]] = []
    for q, hint in interleaved:
        if q in seen:
            continue
        seen.add(q)
        out.append((q, hint))
        if len(out) >= cap:
            break
    return out


async def harvest(db: AsyncSession, *, max_queries: int | None = None,
                  max_items: int | None = None, progress: dict | None = None,
                  persona: str | None = None, therapeutic_area: str | None = None,
                  brand_focus: str | None = None, monitoring_mode: str | None = None) -> dict:
    # DISEASE_STATE ("All Brands") widens discovery to the whole landscape; BRAND (default,
    # "AbbVie") keeps the focus-brand behavior. Any unrecognized value falls back to BRAND.
    landscape = (monitoring_mode or "BRAND").upper() == "DISEASE_STATE"
    cfg = load_yaml_config("harvest_sources.yaml")
    tcfg = cfg.get("tavily", {})
    hcfg = cfg.get("harvest", {})

    source = TavilySource(
        search_depth=tcfg.get("search_depth", "advanced"),
        include_domains=tcfg.get("include_domains", []),
        exclude_domains=tcfg.get("exclude_domains", []),
        include_answer=tcfg.get("include_answer", False),
        include_raw_content=tcfg.get("include_raw_content", False),
    )
    if not source.is_configured():
        return {"status": "not_configured",
                "reason": "TAVILY_API_KEY is not set. Add it to .env to enable harvesting.",
                "staged": 0}

    max_queries = max_queries or hcfg.get("max_queries_per_run", 40)
    max_items = max_items or hcfg.get("max_items_per_run", 250)
    per_query = tcfg.get("max_results_per_query", 8)
    min_len = hcfg.get("min_question_len", 15)
    max_len = hcfg.get("max_question_len", 320)
    per_item = hcfg.get("max_questions_per_item", 5)
    batch_size = hcfg.get("classify_batch_size", 12)
    min_rel = hcfg.get("min_relevance", 0.4)

    vocab, valid_tas = classify.build_vocab()
    # Resolve the requested scope to canonical values (case-insensitive). An
    # unrecognized persona/TA simply means "no scoping" rather than an error.
    want_persona = (persona or "").capitalize() or None
    if want_persona not in classify.VALID_PERSONAS:
        want_persona = None
    want_ta = next((t for t in valid_tas if therapeutic_area and t.lower() == therapeutic_area.lower()), None)
    queries = build_queries(cfg, max_queries, persona=want_persona,
                            therapeutic_area=want_ta, brand_focus=brand_focus,
                            landscape=landscape)
    persona_domains = cfg.get("persona_domains") or {}
    existing = set((await db.execute(select(HarvestedQuestion.dedupe_hash))).scalars().all())
    batch_seen: set[str] = set()

    if progress is not None:
        progress.update(phase="searching", queries_total=len(queries), queries_done=0,
                        raw_results=0, candidates=0, duplicates=0,
                        filtered_off_topic=0, staged=0, quarantined_ae=0)

    staged = quarantined = filtered = duplicates = candidate_count = raw_count = 0
    pending: list[tuple[str, list[str], object, str | None, str]] = []

    async def _flush() -> None:
        """Classify + stage the pending buffer, then COMMIT so rows surface immediately."""
        nonlocal staged, quarantined, filtered, pending
        if not pending:
            return
        tags = await classify.classify_batch(
            [b[0] for b in pending], vocab, hints=[b[3] for b in pending]
        )
        for (clean, flags, item, hint, h), raw_tag in zip(pending, tags):
            norm = classify.normalize_tags(raw_tag or {}, valid_tas, hint=hint)
            # G4: deterministic AE backstop. OR the LLM verdict with a keyword detector so
            # a missed (or un-run, on classifier error) adverse_event still trips the hold.
            ae = norm["ae_flag"] or adverse_event.looks_like_ae(clean)
            # Fail-closed: a possible AE is NEVER silently dropped by the relevance filter —
            # it is quarantined for pharmacovigilance review regardless of relevance score.
            if norm["relevance_score"] < min_rel and not ae:
                filtered += 1
                continue
            # Scope filter: when the user targeted a persona/TA, drop off-scope
            # items — but NEVER drop a possible adverse event (fail-closed for PV).
            if not ae and (
                (want_persona and norm["persona"] != want_persona)
                or (want_ta and norm["therapeutic_area"] != want_ta)
            ):
                filtered += 1
                continue
            intent = None
            if norm["persona"] and norm["domain"]:
                ir = classify_by_rules(norm["persona"], norm["domain"], clean)
                intent = ir.intent if ir else None
            # G1: NEVER persist unredacted third-party free text. The reviewer-facing
            # excerpt and the page title both routinely carry the poster's self-disclosed
            # PHI/PII, so they pass through the same redactor as question_text. PII/PHI
            # types found anywhere are merged into pii_flags for reviewer transparency.
            excerpt, excerpt_flags = await scrub.redact_async((item.content or "")[:500])
            title, title_flags = await scrub.redact_async(item.title or "")
            all_flags = set(flags) | set(excerpt_flags) | set(title_flags)
            # G3: screen the verbatim question for prompt-injection/jailbreak payloads
            # before it can ever be promoted into the LLM-bound question bank.
            inj = injection.scan_injection(clean)
            if inj:
                all_flags.update(f"Injection:{label}" for label in inj)
            all_flags = sorted(all_flags)
            db.add(HarvestedQuestion(
                source=item.source,
                source_url=scrub.scrub_source_url(item.url),
                source_domain=item.domain,
                source_title=title or None,
                search_query=item.query,
                search_persona=hint,
                raw_excerpt=excerpt or None,
                question_text=clean,
                dedupe_hash=h,
                persona=norm["persona"],
                therapeutic_area=norm["therapeutic_area"],
                brand_focus=norm["brand_focus"],
                domain=norm["domain"],
                intent_type=intent,
                relevance_score=norm["relevance_score"],
                pii_flags=json.dumps(all_flags) if all_flags else None,
                ae_flag=ae,
                status="QUARANTINED_AE" if ae else "CLASSIFIED",
            ))
            staged += 1
            if ae:
                quarantined += 1
        await db.commit()
        pending = []
        if progress is not None:
            progress.update(staged=staged, quarantined_ae=quarantined,
                            filtered_off_topic=filtered, duplicates=duplicates,
                            candidates=candidate_count, raw_results=raw_count)

    # Stream: search each query, then extract -> scrub -> dedupe -> stage in batches,
    # committing as we go so questions appear in the UI while the run is still going.
    for qi, (q, hint) in enumerate(queries, 1):
        domains = persona_domains.get(hint) if hint else None
        results = await source.search(q, max_results=per_query, include_domains=domains)
        raw_count += len(results)
        for item in results:
            for qtext in extractor.extract_questions(item, min_len=min_len, max_len=max_len, cap=per_item):
                candidate_count += 1
                clean, flags = await scrub.redact_async(qtext)
                if len(clean) < min_len:
                    continue
                h = _dedupe_hash(clean)
                if h in existing or h in batch_seen:
                    duplicates += 1
                    continue
                batch_seen.add(h)
                pending.append((clean, flags, item, hint, h))
                if len(pending) >= batch_size:
                    await _flush()
        if progress is not None:
            progress.update(queries_done=qi, raw_results=raw_count,
                            candidates=candidate_count, duplicates=duplicates)
        if raw_count >= max_items:
            break

    await _flush()  # stage whatever remains in the buffer
    if progress is not None:
        progress.update(phase="done", queries_done=len(queries))

    result = {
        "status": "ok",
        "persona": want_persona,
        "therapeutic_area": want_ta,
        "queries": len(queries),
        "raw_results": raw_count,
        "candidates": candidate_count,
        "duplicates": duplicates,
        "filtered_off_topic": filtered,
        "staged": staged,
        "quarantined_ae": quarantined,
    }
    logger.info("harvest complete: %s", result)
    return result


async def stats(db: AsyncSession) -> dict:
    rows = (await db.execute(
        select(HarvestedQuestion.status, func.count()).group_by(HarvestedQuestion.status)
    )).all()
    by_status = {s: int(c) for s, c in rows}
    return {
        "total": sum(by_status.values()),
        "by_status": by_status,
        "configured": TavilySource().is_configured(),
    }
