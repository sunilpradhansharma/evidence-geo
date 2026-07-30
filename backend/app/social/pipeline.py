"""Social-listening ingestion pipeline (separate from the Discovery harvest pipeline).

Per ingest: for each enabled Apify channel (run CONCURRENTLY), fetch public posts for the
seed terms, then scrub PII/PHI, screen for prompt-injection, apply the deterministic AE
backstop, LLM-tag (brand/TA/domain/topic/sentiment/AE), de-duplicate, and persist as
SocialPost rows. A second pass then scrapes comments/replies for the top-engagement posts
per channel, runs the SAME guardrails, scores their sentiment as a SEPARATE dimension, and
stores them as SocialComment rows (rolling up an avg comment sentiment onto each post).
Non-English posts/comments are translated to English during tagging (always AFTER redaction).
Short-circuits when Apify is disabled or no token is set.

No author identity is persisted. Engagement is a raw per-channel metric (never summed
across channels). Internal demo — Legal/Privacy/PV sign-off required before production.
"""
import asyncio
import hashlib
import json
import re
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import taxonomy
from app.config.settings import get_settings, load_yaml_config
from app.guardrails import adverse_event, injection
from app.harvest import scrub
from app.harvest.sources.apify import ApifySource
from app.harvest.sources.base import RawComment, RawItem
from app.models.social_comment import SocialComment
from app.models.social_post import SocialPost
from app.social import classify, community
from app.utils.logging import get_logger

logger = get_logger("social.pipeline")

_WS = re.compile(r"\s+")
_NONWORD = re.compile(r"[^a-z0-9 ]+")


def _dedupe_hash(channel: str, text: str) -> str:
    norm = _NONWORD.sub("", _WS.sub(" ", text.lower()).strip())
    return hashlib.sha1(f"{channel}:{norm}".encode("utf-8")).hexdigest()


def _comment_hash(channel: str, post_id: int, text: str) -> str:
    """Per-(channel, parent post) hash so identical replies under DIFFERENT posts are kept."""
    norm = _NONWORD.sub("", _WS.sub(" ", text.lower()).strip())
    return hashlib.sha1(f"{channel}:{post_id}:{norm}".encode("utf-8")).hexdigest()


def is_configured() -> bool:
    s = get_settings()
    return bool(s.apify_enabled and s.apify_api_token)


def _build_sources(cfg: dict, channels: list[str] | None, *,
                   ta: str | None = None) -> tuple[list[ApifySource], set[str]]:
    """Build the ApifySource list for an ingest.

    Two per-channel config keys shape membership:
      - ``only_areas``: a HARD therapeutic-area gate. A channel with this key runs ONLY
        for the listed areas (these are single-condition community sites), even when it
        is explicitly requested. Such channels are also "site-scoped" — their captured
        posts get force-tagged with the ingest scope regardless of relevance.
      - ``opt_in``: excluded from a default "all enabled" run (``channels`` is None); the
        analyst must select it by name.

    Returns ``(sources, force_scope_channels)`` where ``force_scope_channels`` is the set
    of channel names carrying ``only_areas``.
    """
    apify_cfg = cfg.get("apify") or {}
    want = {c.strip().lower() for c in channels} if channels else None
    ta_l = (ta or "").strip().lower()
    sources: list[ApifySource] = []
    force_scope: set[str] = set()
    for channel, ch in apify_cfg.items():
        if not isinstance(ch, dict) or not ch.get("enabled", True):
            continue
        only_areas = ch.get("only_areas")
        if only_areas:  # hard TA gate — skip even if explicitly requested
            allowed = {str(a).strip().lower() for a in only_areas}
            if ta_l not in allowed:
                continue
        if want is not None:
            if channel.lower() not in want:
                continue
        elif ch.get("opt_in", False):  # default run excludes opt-in channels
            continue
        comments = ch.get("comments") or {}
        sources.append(ApifySource(
            channel=channel,
            actor_id=ch.get("actor_id", ""),
            input_template=ch.get("input_template", {}),
            results_cap=ch.get("results_cap", 30),
            enabled=ch.get("enabled", True),
            mode=ch.get("mode", "search"),
            max_posts=ch.get("max_posts"),
            comments_actor_id=comments.get("actor_id", ""),
            comments_input_template=comments.get("input_template", {}),
            comments_enabled=comments.get("enabled", False),
            max_comments_per_post=comments.get("max_comments_per_post", 20),
        ))
        if only_areas:
            force_scope.add(channel)
    return sources, force_scope


def _derive_seed_terms(ta: str) -> list[str]:
    """Defensive fallback: build seed terms for a therapeutic area from brands.yaml.

    Used only when an area has no curated list in social_sources.yaml. Pulls the
    focus-brand names, the area/indication label, and a few competitor names so the
    ingest still has something relevant to search (content-agnostic, SE-007).
    """
    try:
        brands = taxonomy.config()
    except Exception:  # noqa: BLE001 — no config, no derivation
        return []
    block = (brands.get("therapeutic_areas") or {}).get(ta)
    if not isinstance(block, dict):
        return []
    terms: list[str] = [str(b["name"]) for b in block.get("focus_brands", []) if b.get("name")]
    terms.append(ta)  # the indication / area label itself
    terms.extend(str(c["name"]) for c in (block.get("competitors") or [])[:3] if c.get("name"))
    return terms


def _seed_terms(cfg: dict, max_terms: int, *, ta: str,
                custom_terms: list[str] | None = None) -> list[str]:
    """Resolve the seed terms for an ingest.

    Precedence: explicit ad-hoc ``custom_terms`` -> the curated ``seed_terms_by_area``
    entry for ``ta`` -> terms derived from brands.yaml -> the legacy flat ``seed_terms``.
    """
    if custom_terms:
        candidates: list = list(custom_terms)
    else:
        by_area = cfg.get("seed_terms_by_area") or {}
        candidates = by_area.get(ta) or _derive_seed_terms(ta) or cfg.get("seed_terms") or []
    seen: set[str] = set()
    out: list[str] = []
    for t in candidates:
        t = str(t).strip()
        if not t or t.lower() in seen:
            continue
        seen.add(t.lower())
        out.append(t)
        if len(out) >= max_terms:
            break
    return out


async def _fetch_channel(source: ApifySource, terms: list[str], cap_posts: int,
                         progress: dict | None) -> list[RawItem]:
    """Fetch posts for one channel across its seed terms (terms run sequentially).

    A ``mode='crawl'`` channel has fixed startUrls (no ``{term}``), so it is run EXACTLY
    once with an empty term — avoiding paying for N identical site crawls.
    """
    collected: list[RawItem] = []
    run_terms = [""] if source.mode == "crawl" else terms
    for term in run_terms:
        if len(collected) >= cap_posts:
            break
        remaining = cap_posts - len(collected)
        try:
            items = await source.search(term, max_results=min(source.results_cap, remaining))
        except Exception as e:  # noqa: BLE001 — never break the whole run on one channel
            logger.warning("social fetch %s %r failed: %s", source.channel, term, e)
            items = []
        collected.extend(items)
        if progress is not None:
            progress["raw_posts"] = progress.get("raw_posts", 0) + len(items)
    if progress is not None:
        progress["channels_done"] = progress.get("channels_done", 0) + 1
    return collected[:cap_posts]


async def _fetch_community_tavily(cfg: dict, scfg: dict, sources: list[ApifySource],
                                  force_scope_channels: set[str], *, ta: str,
                                  seed: list[str], crawl_counts: dict[str, int],
                                  progress: dict | None) -> list[RawItem]:
    """Optional domain-constrained Tavily supplement for the community channels.

    The Apify crawl is always the primary source; this pulls extra PUBLIC article text from each
    selected community channel's own site (via its per-channel ``tavily`` block). Governed by
    ``social.community_tavily_mode``:
      - ``adaptive`` (default): supplement ONLY a channel whose crawl came back thin (fewer than
        ``community_tavily_min_posts`` items) — rich backfill exactly when the crawl underdelivers,
        no spend/redundancy when it does not.
      - ``always``: supplement every selected community channel regardless of crawl yield.
      - ``off``: never run.
    Every mode still requires a Tavily key; without one it silently no-ops. Items are force-scoped
    onto their community channel so they flow through the SAME scrub -> classify -> community-
    enrichment path as the crawl. Best-effort: returns [] on no ``tavily`` block or any failure.
    """
    mode = str(scfg.get("community_tavily_mode", "adaptive")).strip().lower()
    if mode not in ("adaptive", "always"):
        return []
    selected = {s.channel for s in sources} & force_scope_channels
    if mode == "adaptive":
        min_posts = int(scfg.get("community_tavily_min_posts", 8))
        selected = {c for c in selected if crawl_counts.get(c, 0) < min_posts}
    if not selected:
        return []
    from app.harvest.sources.tavily import TavilySource  # optional path — imported only when needed
    source = TavilySource(search_depth="advanced", include_raw_content=True)
    if not source.is_configured():
        logger.info("community Tavily supplement requested (%s) but TAVILY_API_KEY missing; skipping", mode)
        return []
    apify_cfg = cfg.get("apify") or {}
    per = int(scfg.get("community_tavily_max_results", 8))
    max_q = int(scfg.get("community_tavily_max_queries", 3))
    out: list[RawItem] = []
    for channel in sorted(selected):
        tv = (apify_cfg.get(channel) or {}).get("tavily") or {}
        domains = tv.get("domains") or []
        if not domains:
            continue
        queries = [str(q) for q in (tv.get("queries") or seed or [ta]) if str(q).strip()][:max_q]
        for q in queries:
            try:
                items = await source.search(q, max_results=per, include_domains=domains)
            except Exception as e:  # noqa: BLE001 — one query must not break the ingest
                logger.warning("community Tavily %s %r failed: %s", channel, q, e)
                items = []
            for it in items:
                it.channel = channel               # force-scope onto the community channel
                it.source = f"tavily:{channel}"
            out.extend(items)
    if progress is not None and out:
        progress["raw_posts"] = progress.get("raw_posts", 0) + len(out)
    logger.info("community Tavily supplement (%s): %d item(s) across %d channel(s)",
                mode, len(out), len(selected))
    return out


async def _fetch_for_post(source: ApifySource, rec: dict, sem: asyncio.Semaphore,
                         cap: int) -> tuple[dict, list[RawComment]]:
    """Bounded-concurrency comment fetch for one parent post (never raises)."""
    async with sem:
        try:
            comments = await source.fetch_comments_for_post(rec["url"], max_results=cap)
        except Exception as e:  # noqa: BLE001 — one post must not break the comments phase
            logger.warning("social comments fetch failed for post %s: %s", rec.get("id"), e)
            comments = []
    return rec, comments


async def _ingest_comments_for_post(db: AsyncSession, post_id: int, channel: str,
                                   raw_comments: list[RawComment], *, vocab: str,
                                   valid_tas: set[str], min_len: int, batch_size: int,
                                   comment_seen: set[str]) -> tuple[int, int]:
    """Scrub/screen/translate/score + persist a post's comments; roll up comment sentiment.

    Comments get the SAME guardrails as posts: G1 redaction, G3 injection screen, and the
    G4 fail-closed AE backstop (run on the English text). Returns (ingested, ae_count).
    """
    prepared: list[tuple[RawComment, str, str, list[str]]] = []
    for rc in raw_comments:
        text = (rc.text or "").strip()
        if len(text) < min_len:
            continue
        clean, flags = await scrub.redact_async(text)          # G1: never persist raw text
        if len(clean.strip()) < min_len:
            continue
        h = _comment_hash(channel, post_id, clean)
        if h in comment_seen:
            continue
        comment_seen.add(h)
        inj = injection.scan_injection(clean)                  # G3: prompt-injection screen
        all_flags = sorted(set(flags) | {f"Injection:{label}" for label in inj})
        prepared.append((rc, clean, h, all_flags))
    if not prepared:
        return 0, 0

    ingested = ae_count = 0
    sent_sum = 0.0
    sent_n = 0
    for start in range(0, len(prepared), batch_size):
        chunk = prepared[start:start + batch_size]
        tags = await classify.classify_posts([c[1] for c in chunk], vocab)
        for (rc, clean, h, flags), raw_tag in zip(chunk, tags):
            norm = classify.normalize_tags(raw_tag or {}, valid_tas)
            translated = norm["is_translated"]
            english = norm["text_en"] or clean
            ae = norm["ae_flag"] or adverse_event.looks_like_ae(english)  # G4: fail-closed AE
            db.add(SocialComment(
                post_id=post_id,
                channel=channel,
                text=english if translated else clean,
                text_original=clean if translated else None,
                language=norm["language"],
                is_translated=translated,
                dedupe_hash=h,
                sentiment=norm["sentiment"],
                sentiment_label=norm["sentiment_label"],
                topic=norm["topic"],
                engagement_score=rc.engagement_score,
                posted_at=rc.posted_at,
                ae_flag=ae,
                pii_flags=json.dumps(flags) if flags else None,
            ))
            ingested += 1
            if ae:
                ae_count += 1
            if norm["sentiment"] is not None:
                sent_sum += norm["sentiment"]
                sent_n += 1

    # Roll up the comment-sentiment dimension onto the parent post (each post handled once).
    post = await db.get(SocialPost, post_id)
    if post is not None:
        post.comments_captured = ingested
        post.comment_sentiment = round(sent_sum / sent_n, 3) if sent_n else None
    await db.commit()
    return ingested, ae_count


async def ingest(db: AsyncSession, *, channels: list[str] | None = None,
                 therapeutic_area: str | None = None, terms: list[str] | None = None,
                 scope_label: str | None = None, progress: dict | None = None) -> dict:
    s = get_settings()
    if not s.apify_enabled:
        return {"status": "disabled", "reason": "APIFY_ENABLED is false.", "ingested": 0}
    if not s.apify_api_token:
        return {"status": "not_configured",
                "reason": "APIFY_API_TOKEN is not set. Add it to .env to enable social listening.",
                "ingested": 0}

    cfg = load_yaml_config("social_sources.yaml")
    scfg = cfg.get("social") or {}
    max_terms = int(scfg.get("max_terms_per_channel", 2))
    cap_posts = int(scfg.get("max_posts_per_channel", 40))
    batch_size = int(scfg.get("classify_batch_size", 10))
    min_len = int(scfg.get("min_text_len", 12))
    ta = therapeutic_area or scfg.get("therapeutic_area", "Obesity")
    # ``scope`` is the label stamped on captured posts and used to scope the brief/insights.
    # For an ad-hoc free-text search it is the analyst's typed query; capped to the DB column
    # width (64). ``adhoc`` is true when explicit ``terms`` were supplied.
    scope = ((scope_label or ta) or "").strip()[:64] or ta
    adhoc = bool(terms)
    # Comment-pass caps (separate sentiment dimension; bound the all-channels cost).
    comments_enabled = bool(scfg.get("comments_enabled", True))
    max_comments_per_post = int(scfg.get("max_comments_per_post", 20))
    max_posts_with_comments = int(scfg.get("max_posts_with_comments_per_channel", 12))
    comment_concurrency = max(1, int(scfg.get("comment_run_concurrency", 4)))
    comment_batch = int(scfg.get("comment_classify_batch_size", 12))
    min_comment_len = int(scfg.get("min_comment_len", 6))
    # Community-enrichment pass (patient-community crawls only). A smaller batch than the
    # classifier since each crawled page is far longer than a single social post.
    community_batch = int(scfg.get("community_classify_batch_size", 5))

    sources, force_scope_channels = _build_sources(cfg, channels, ta=ta)
    seed = _seed_terms(cfg, max_terms, ta=ta, custom_terms=terms)

    if progress is not None:
        progress.update(phase="fetching", channels_total=len(sources), channels_done=0,
                        raw_posts=0, ingested=0, duplicates=0, ae=0,
                        comments_ingested=0, comment_ae=0)

    # Crawl channels have fixed startUrls and need no seed terms, so only bail on an empty
    # seed when EVERY selected source is term-seeded.
    if not sources or (not seed and not any(s.mode == "crawl" for s in sources)):
        if progress is not None:
            progress["phase"] = "done"
        return {"status": "ok", "therapeutic_area": scope,
                "channels": [src.channel for src in sources],
                "raw_posts": 0, "ingested": 0, "duplicates": 0, "ae": 0}

    # Fetch every channel CONCURRENTLY (each channel's terms run sequentially within it).
    results = await asyncio.gather(
        *[_fetch_channel(src, seed, src.max_posts or cap_posts, progress) for src in sources]
    )
    crawl_counts = {src.channel: len(sub) for src, sub in zip(sources, results)}
    raw_items: list[RawItem] = [it for sub in results for it in sub]

    # Optional domain-constrained Tavily supplement for the community channels (adaptive by
    # default: fires only for a channel whose crawl came back thin). Force-scoped onto the channel
    # so supplemental items enrich through the same path as the crawl.
    if force_scope_channels:
        raw_items.extend(await _fetch_community_tavily(
            cfg, scfg, sources, force_scope_channels, ta=ta, seed=seed,
            crawl_counts=crawl_counts, progress=progress))

    if progress is not None:
        progress.update(phase="processing", raw_posts=len(raw_items))

    vocab, valid_tas = classify.build_vocab()
    existing = set((await db.execute(select(SocialPost.dedupe_hash))).scalars().all())
    batch_seen: set[str] = set()

    ingested = duplicates = ae_count = 0
    pending: list[tuple[RawItem, str, str, list[str]]] = []  # (item, clean_text, hash, flags)
    post_records: list[dict] = []  # persisted posts (id/channel/url/engagement) for the comments phase
    community_posts: list[SocialPost] = []  # force-scoped community posts for the enrichment pass

    async def _flush() -> None:
        nonlocal ingested, duplicates, ae_count, pending
        if not pending:
            return
        tags = await classify.classify_posts([p[1] for p in pending], vocab)
        created: list[tuple[SocialPost, RawItem]] = []
        for (item, clean, h, flags), raw_tag in zip(pending, tags):
            norm = classify.normalize_tags(raw_tag or {}, valid_tas)
            # Translate AFTER redaction: ``clean`` is the scrubbed source text; ``text`` stores
            # the English canonical copy and ``text_original`` the source when translated.
            translated = norm["is_translated"]
            english = norm["text_en"] or clean
            # G4: deterministic AE backstop OR'd with the LLM verdict (fail-closed for PV),
            # run on the ENGLISH text so non-English posts are still screened.
            ae = norm["ae_flag"] or adverse_event.looks_like_ae(english)
            sp = SocialPost(
                channel=item.channel or "",
                source=item.source,
                post_url=scrub.scrub_source_url(item.url),
                source_domain=item.domain,
                search_term=item.query,
                text=english if translated else clean,
                text_original=clean if translated else None,
                language=norm["language"],
                is_translated=translated,
                dedupe_hash=h,
                brand_focus=norm["brand_focus"],
                # Scope isolation: tag every relevant post with the area/label THIS ingest was
                # run for. Ad-hoc searches force the label even for borderline posts (the analyst
                # explicitly asked for those terms); dropdown areas keep the relevance gate so
                # off-topic posts stay TA=null and fall out of the scoped dashboard. We do NOT
                # reroute to the classifier's therapeutic_area, so a shared-brand ingest (e.g.
                # Lupron Depot across Endometriosis / Uterine Fibroids / CPP) never bleeds into a
                # sibling indication the analyst did not ingest.
                therapeutic_area=(scope if (adhoc or norm["relevant"]
                                            or item.channel in force_scope_channels) else None),
                domain=norm["domain"],
                topic=norm["topic"],
                sentiment=norm["sentiment"],
                sentiment_label=norm["sentiment_label"],
                engagement_score=item.engagement_score,
                comment_count=item.comment_count,
                posted_at=item.posted_at,
                ae_flag=ae,
                pii_flags=json.dumps(flags) if flags else None,
            )
            db.add(sp)
            created.append((sp, item))
            ingested += 1
            if ae:
                ae_count += 1
        await db.commit()
        # Record persisted posts (with RAW url + engagement) for the comments phase.
        for sp, item in created:
            post_records.append({"id": sp.id, "channel": sp.channel, "url": item.url,
                                 "engagement": item.engagement_score,
                                 "comment_count": item.comment_count})
        # Force-scoped community posts (myRAteam/Bezzy) get the richer enrichment pass below.
        community_posts.extend(sp for sp, item in created
                               if (item.channel or "") in force_scope_channels)
        pending = []
        if progress is not None:
            progress.update(ingested=ingested, duplicates=duplicates, ae=ae_count)

    for item in raw_items:
        text = (item.content or "").strip()
        if len(text) < min_len:
            continue
        # G1: never persist unredacted third-party free text.
        clean, flags = await scrub.redact_async(text)
        if len(clean.strip()) < min_len:
            continue
        h = _dedupe_hash(item.channel or "", clean)
        if h in existing or h in batch_seen:
            duplicates += 1
            continue
        batch_seen.add(h)
        # G3: screen for prompt-injection/jailbreak payloads in scraped text.
        inj = injection.scan_injection(clean)
        all_flags = sorted(set(flags) | {f"Injection:{label}" for label in inj})
        pending.append((item, clean, h, all_flags))
        if len(pending) >= batch_size:
            await _flush()
    await _flush()

    # ---- Community enrichment pass (patient-community crawls only) ----
    # myRAteam / Bezzy pages name many treatments and carry rich patient-experience context
    # the single-brand classifier drops. Enrich only the force-scoped community posts; a
    # failure here must never fail the ingest (best-effort, like the brief synthesis).
    community_enriched = 0
    if community_posts:
        if progress is not None:
            progress["phase"] = "enriching"
        try:
            enrich = await community.extract_and_apply(
                db, community_posts, vocab=vocab, batch_size=community_batch)
            community_enriched = enrich.get("enriched", 0)
            if progress is not None:
                progress["community_enriched"] = community_enriched
        except Exception as e:  # noqa: BLE001 — enrichment must never fail the ingest
            logger.warning("community enrichment skipped: %s", e)

    # ---- Comments phase (SEPARATE sentiment dimension) ----
    # Only the top-engagement posts per channel get a comments pass, bounded by the caps, so
    # the all-channels path stays demo-sized. Channels without a comments actor are skipped.
    comments_ingested = comment_ae = 0
    if comments_enabled and post_records:
        if progress is not None:
            progress.update(phase="comments", comments_ingested=0, comment_ae=0)
        source_by_channel = {src.channel: src for src in sources}
        by_channel: dict[str, list[dict]] = defaultdict(list)
        for rec in post_records:
            by_channel[rec["channel"]].append(rec)
        comment_seen: set[str] = set()
        for ch, recs in by_channel.items():
            src = source_by_channel.get(ch)
            if not src or not src.comments_configured():
                continue
            candidates = [r for r in recs
                          if r["url"] and (r["comment_count"] is None or r["comment_count"] > 0)]
            candidates.sort(key=lambda r: (r["engagement"] or 0), reverse=True)
            targets = candidates[:max_posts_with_comments]
            if not targets:
                continue
            sem = asyncio.Semaphore(comment_concurrency)
            fetched = await asyncio.gather(
                *[_fetch_for_post(src, r, sem, max_comments_per_post) for r in targets]
            )
            for rec, raw_comments in fetched:
                ci, cae = await _ingest_comments_for_post(
                    db, rec["id"], ch, raw_comments,
                    vocab=vocab, valid_tas=valid_tas, min_len=min_comment_len,
                    batch_size=comment_batch, comment_seen=comment_seen,
                )
                comments_ingested += ci
                comment_ae += cae
                if progress is not None:
                    progress.update(comments_ingested=comments_ingested, comment_ae=comment_ae)

    if progress is not None:
        progress.update(phase="done", duplicates=duplicates)

    result = {
        "status": "ok",
        "therapeutic_area": scope,
        "channels": [src.channel for src in sources],
        "raw_posts": len(raw_items),
        "ingested": ingested,
        "duplicates": duplicates,
        "ae": ae_count,
        "comments_ingested": comments_ingested,
        "comment_ae": comment_ae,
        "community_enriched": community_enriched,
    }
    logger.info("social ingest complete: %s", result)
    return result


async def stats(db: AsyncSession) -> dict:
    s = get_settings()
    rows = (await db.execute(
        select(SocialPost.channel, func.count()).group_by(SocialPost.channel)
    )).all()
    by_channel = {c: int(n) for c, n in rows}
    return {
        "total": sum(by_channel.values()),
        "by_channel": by_channel,
        "enabled": bool(s.apify_enabled),
        "configured": is_configured(),
    }
