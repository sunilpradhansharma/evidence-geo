"""Tests for the central PHI/PII detector (app.compliance.phi) and the re-redaction backfill.

Covers the strengthened heuristic layer (self-disclosed names, US locations, ZIPs),
guards against over-redacting clinical text (drug/brand names), verifies idempotency, and
exercises the backfill sweep that cleans already-stored rows in place.
"""
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.compliance import backfill, phi
from app.models.database import Base
from app.models.harvested_question import HarvestedQuestion
from app.models.social_post import SocialPost


# --- Detector: direct identifiers (regression) -----------------------------------
def test_redacts_direct_identifiers():
    clean, flags = phi.redact("email me at jane.doe@example.com or 555-123-4567")
    assert "[email]" in clean and "[phone]" in clean
    assert "Email" in flags and "Phone" in flags
    assert "jane.doe@example.com" not in clean


def test_redacts_age():
    clean, flags = phi.redact("I am 52 years old and just started treatment")
    assert "[age]" in clean
    assert "Age" in flags


# --- Detector: strengthened heuristic layer --------------------------------------
def test_redacts_self_disclosed_name():
    clean, flags = phi.redact("Hi, my name is Sarah and I have a question")
    assert "Sarah" not in clean
    assert "[name]" in clean
    assert "Name" in flags


def test_redacts_relationship_name():
    clean, flags = phi.redact("my son Jake was prescribed this last month")
    assert "Jake" not in clean
    assert "my son [name]" in clean
    assert "Name" in flags


def test_redacts_clinician_name():
    clean, flags = phi.redact("Dr. Smith said it was safe")
    assert "Smith" not in clean
    assert "[name]" in clean
    assert "Name" in flags


def test_redacts_location():
    clean, flags = phi.redact("I live in Austin, TX and can't find it")
    assert "Austin" not in clean
    assert "[location]" in clean
    assert "Location" in flags


def test_redacts_zip():
    clean1, flags1 = phi.redact("mail it to TX 78701 please")
    assert "78701" not in clean1 and "[zip]" in clean1 and "Zip" in flags1
    clean2, flags2 = phi.redact("my zip code: 90210")
    assert "90210" not in clean2 and "[zip]" in clean2 and "Zip" in flags2


# --- Detector: does NOT over-redact clinical text --------------------------------
@pytest.mark.parametrize("text", [
    "I take Wegovy and Ozempic for weight loss",
    "Is Mounjaro better than Ozempic for diabetes?",
    "In my experience the side effects faded after a week",
])
def test_preserves_clinical_terms(text):
    clean, flags = phi.redact(text)
    assert clean == text
    assert flags == []


# --- Detector: consistency + idempotency -----------------------------------------
def test_scan_matches_redact_flags():
    text = "my name is Sarah, I live in Austin, TX, call 555-123-4567"
    _clean, redact_flags = phi.redact(text)
    scan_flags = phi.scan(text)
    assert set(scan_flags) == set(redact_flags)


def test_redaction_is_idempotent():
    text = "my name is Sarah and I live in Austin, TX"
    once, _ = phi.redact(text)
    twice, _ = phi.redact(once)
    assert twice == once


# --- Backfill: re-redacts already-stored rows in place ---------------------------
@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def test_backfill_reredacts_social_post(session):
    post = SocialPost(
        channel="reddit",
        text="Hi my name is Sarah and I live in Austin, TX. Ozempic helped a lot.",
        dedupe_hash="h1",
        therapeutic_area="Obesity",
        pii_flags=None,
    )
    session.add(post)
    await session.commit()

    summary = await backfill.redact_backfill(session)

    assert "Sarah" not in post.text and "[name]" in post.text
    assert "Austin" not in post.text and "[location]" in post.text
    assert "Ozempic" in post.text  # drug name preserved
    assert post.pii_flags and "Name" in post.pii_flags and "Location" in post.pii_flags
    assert summary["posts_updated"] == 1


async def test_backfill_reredacts_harvested_question(session):
    hq = HarvestedQuestion(
        source="tavily",
        question_text="Does Dr. Smith recommend this for my son Jake?",
        raw_excerpt="Originally posted by someone in Dallas, TX",
        dedupe_hash="hq1",
        pii_flags=None,
    )
    session.add(hq)
    await session.commit()

    await backfill.redact_backfill(session)

    assert "Smith" not in hq.question_text and "Jake" not in hq.question_text
    assert "[name]" in hq.question_text
    assert "Dallas" not in hq.raw_excerpt and "[location]" in hq.raw_excerpt
    assert hq.pii_flags and "Name" in hq.pii_flags
