"""Competitive API — who wins when AI is asked "us vs them".

Every route here is a read over answers a run already produced. **Nothing on this router
calls a model or writes a row**, so opening the board costs nothing and cannot change what
the next reader sees.

The coverage-to-answer funnel deliberately lives on ``/curation/funnel`` instead: it takes
the identical scope parameters as ``/curation/coverage``, so keeping the two together lets
the frontend reuse one query builder rather than growing a second one that could drift.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.competitive import head_to_head as h2h
from app.competitive import mentions as mentions_mod
from app.models.database import get_db
from app.utils.logging import get_logger

logger = get_logger("api.competitive")

router = APIRouter(prefix="/competitive", tags=["competitive"])


@router.get("/head-to-head")
async def head_to_head(
    therapeutic_area: list[str] | None = Query(None, description="Stored TA keys or broad area names"),
    disease: list[str] | None = Query(None, description="Indications to scope to"),
    brand: list[str] | None = Query(None, description="Our brands"),
    competitor: list[str] | None = Query(None, description="Comparators to scope to"),
    persona: list[str] | None = Query(None, description="Prospect / Provider / Patient"),
    llm_name: list[str] | None = Query(None, description="AI platforms"),
    verdict: list[str] | None = Query(None, description="WINNING / EVEN / LOSING"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Ranked head-to-head board, worst-exposure first.

    Every filter is REPEATABLE (``?brand=Rinvoq&brand=Humira``), matching the convention
    ``/curation/coverage`` already uses, and omitting one means "all". Singular parameter
    names are kept so a one-value link written before the pickers went multi-select still
    resolves to the same board.
    """
    return await h2h.scoreboard(
        db, therapeutic_areas=therapeutic_area, diseases=disease, brands=brand,
        competitors=competitor, personas=persona, llm_names=llm_name, verdicts=verdict,
        limit=limit,
    )


@router.get("/head-to-head/detail")
async def head_to_head_detail(
    pair_key: str = Query(..., description="The pair key from the board"),
    persona: list[str] | None = Query(None),
    llm_name: list[str] | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """One comparison in full: claims, cited sources, absence gaps and sample answers.

    ``pair_key`` is a query parameter rather than a path segment because it contains the
    indication and both drug names separated by ``|`` — encoding that into a path invites
    double-unescaping bugs for no benefit.
    """
    detail = await h2h.pair_detail(db, pair_key, personas=persona, llm_names=llm_name)
    if detail is None:
        raise HTTPException(404, f"No answers on the board for comparison {pair_key!r}")
    return detail


@router.get("/mentions")
async def mentions(
    therapeutic_area: str | None = Query(None, description="Stored TA key or broad area name"),
    indication: str | None = Query(None),
    disease: str | None = Query(None),
    brand: str | None = Query(None, description="Scope to answers focused on one of OUR brands"),
    persona: str | None = Query(None),
    llm_name: str | None = Query(None),
    run_id: str | None = Query(None),
    monitoring_mode: str | None = Query(None, pattern="^(BRAND|DISEASE_STATE)$"),
    side: str | None = Query(None, pattern="^(OURS|COMPETITOR|UNTRACKED)$"),
    limit: int = Query(25, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Every agent the models actually named, across ALL scored answers in scope.

    Wider than the head-to-head board, which only reads the comparison questions we asked
    on purpose. This counts a rival wherever a model brought them up, which is the only way
    to answer "how is X being talked about" — a competitor is never a ``brand_focus``.
    """
    return await mentions_mod.rollup(
        db, therapeutic_area=therapeutic_area, indication=indication, disease=disease,
        brand=brand, persona=persona, llm_name=llm_name, run_id=run_id,
        monitoring_mode=monitoring_mode, side=side, limit=limit,
    )


@router.get("/mentions/agent")
async def mention_detail(
    agent: str = Query(..., description="Brand, generic or alias — resolved to one curated drug"),
    therapeutic_area: str | None = Query(None),
    indication: str | None = Query(None),
    disease: str | None = Query(None),
    brand: str | None = Query(None),
    persona: str | None = Query(None),
    llm_name: str | None = Query(None),
    run_id: str | None = Query(None),
    monitoring_mode: str | None = Query(None, pattern="^(BRAND|DISEASE_STATE)$"),
    db: AsyncSession = Depends(get_db),
):
    """One agent in full: breakdowns by model/persona/area plus the answers naming it.

    ``agent`` is a query parameter, and an agent nobody mentioned returns ``found: false``
    with a 200 rather than a 404 — "no model brought them up" is a real answer about the
    corpus, not a missing resource.
    """
    return await mentions_mod.agent_detail(
        db, agent, therapeutic_area=therapeutic_area, indication=indication,
        disease=disease, brand=brand, persona=persona, llm_name=llm_name,
        run_id=run_id, monitoring_mode=monitoring_mode,
    )
