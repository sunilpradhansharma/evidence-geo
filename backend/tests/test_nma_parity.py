"""The Phase 6 acceptance gate: does the sidecar agree with what we compute in-process?

The plan makes this **stop-and-review gate #4** — *"netmeta golden-fixture parity and the
bounded Bucher/netmeta star-network agreement test must both pass, or the engine does not
ship."* The engine shipped past it, because the gate had never been run: there was no
sidecar to run it against.

**These require a live sidecar and are skipped without one.** That is not a softening. The
claim the gate makes is about R arithmetic, and a fixture recorded by hand cannot test R
arithmetic — a committed "expected" response would only assert that our parser reads a file
we wrote, which is precisely the "a clean diff proves reproducibility, not correctness"
error this codebase already guards against elsewhere. Run them with:

    docker build -f Dockerfile.nma -t evidence-nma-sidecar .
    docker run -d -p 8100:8000 --name nma evidence-nma-sidecar
    NMA_SIDECAR_URL=http://127.0.0.1:8100 python -m pytest tests/test_nma_parity.py -q

The offline half of the contract — request shape, multi-arm grouping, response parsing,
reversal, rescaling — is already covered without a sidecar in ``test_evidence_engines.py``
and ``test_evidence_resolver.py``.
"""
from __future__ import annotations

import math
import os

import pytest

from app.evidence.engines import bucher, netmeta
from app.evidence.engines.pairwise import StudyContrast, binary_contrast
from app.evidence.engines.pairwise import BinaryArm

SIDECAR = os.environ.get("NMA_SIDECAR_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not SIDECAR,
    reason="needs a live netmeta sidecar; set NMA_SIDECAR_URL to run the Phase 6 gate",
)

# Tolerance on the log scale. Bucher and netmeta are the same arithmetic on a star network
# under matched assumptions, so this is a floating-point allowance, not a fudge factor: a
# real disagreement is orders of magnitude larger than this.
LOG_TOLERANCE = 1e-6


def _arm(treatment: str, events: int, n: int) -> netmeta.ArmPayload:
    return netmeta.ArmPayload(treatment=treatment, events=events, sample_size=n)


# A controlled simple star: two active treatments, one common comparator, no closed loop
# and no multi-arm trial. This is the exact configuration under which the two methods are
# claimed to coincide, and the claim is conditional on all of it.
STAR = {
    "S1": [_arm("Rinvoq", 120, 200), _arm("Placebo", 60, 200)],
    "S2": [_arm("Humira", 100, 200), _arm("Placebo", 55, 200)],
}


async def _run(studies, *, model="fixed", measure="risk_ratio"):
    request = netmeta.build_request(
        studies, outcome_type="binary", effect_measure=measure, model=model,
        reference_treatment="Placebo",
    )
    response = await netmeta.run(request, base_url=SIDECAR)
    assert response.ok, f"sidecar was not available: {response.reason}"
    return response


# =====================================================================================
# The bounded parity fixture
# =====================================================================================
async def test_the_sidecar_answers_healthz_and_reports_its_package_version():
    """A result a reviewer cannot trace to a package version is not reproducible."""
    response = await _run(STAR)
    assert response.package_version
    assert response.package_version.startswith("netmeta")


async def test_bucher_and_netmeta_agree_on_a_star_network_under_matched_assumptions():
    """The plan's bounded parity gate, stated exactly as narrowly as it is true.

    Agreement holds **only** given: a simple common-comparator network, the same effect
    scale, the same input contrasts, no multi-arm correlation, the same continuity
    correction, and fixed effects. Under random effects they may legitimately diverge,
    because netmeta estimates one tau-squared across the whole network while Bucher works
    from per-comparison variances — so this fixture pins the fixed-effect case and does not
    claim more.
    """
    contrasts = []
    for study, arms in STAR.items():
        active, placebo = arms
        contrast, _reason = binary_contrast(
            study,
            BinaryArm(active.treatment, active.events, active.sample_size),
            BinaryArm(placebo.treatment, placebo.events, placebo.sample_size),
            measure="risk_ratio",
        )
        contrasts.append(contrast)

    ours = bucher.compare(
        contrasts, treatment="Rinvoq", comparator="Humira", anchors=["Placebo"],
    )
    assert ours.estimable
    mine = ours.estimates[0]

    response = await _run(STAR)
    theirs = response.contrast_for("Rinvoq", "Humira")
    assert theirs is not None

    assert math.log(theirs.estimate) == pytest.approx(
        math.log(mine.estimate_reported), abs=LOG_TOLERANCE
    )
    assert math.log(theirs.ci_lower) == pytest.approx(
        math.log(mine.ci_lower_reported), abs=1e-4
    )
    assert math.log(theirs.ci_upper) == pytest.approx(
        math.log(mine.ci_upper_reported), abs=1e-4
    )


# =====================================================================================
# Multi-arm correctness — the reason the wire is arm-level
# =====================================================================================
async def test_a_three_arm_trial_has_wider_standard_errors_than_the_same_data_flattened():
    """The plan's multi-arm correctness test, and the whole argument for the arm-level wire.

    Flattening a three-arm trial into independent pairwise rows discards the within-study
    correlation induced by the shared control group. The pairwise rows are not independent,
    so treating them as independent understates the variance — the estimate can look almost
    identical while its interval is wrong, which is the failure mode that survives review.
    """
    three_arm = {
        "MULTI": [
            _arm("Rinvoq", 120, 200),
            _arm("Humira", 100, 200),
            _arm("Placebo", 60, 200),
        ],
    }
    # The same numbers, but each pairwise comparison presented as its own trial, so the
    # control group is counted three times over.
    flattened = {
        "MULTI_A": [_arm("Rinvoq", 120, 200), _arm("Placebo", 60, 200)],
        "MULTI_B": [_arm("Humira", 100, 200), _arm("Placebo", 60, 200)],
        "MULTI_C": [_arm("Rinvoq", 120, 200), _arm("Humira", 100, 200)],
    }

    whole = (await _run(three_arm)).contrast_for("Rinvoq", "Humira")
    split = (await _run(flattened)).contrast_for("Rinvoq", "Humira")
    assert whole is not None and split is not None
    assert whole.standard_error is not None and split.standard_error is not None

    assert whole.standard_error > split.standard_error, (
        "flattening a multi-arm trial must understate the standard error; if these are "
        "equal the sidecar is not preserving study grouping through pairwise()"
    )


async def test_a_multi_arm_network_routes_to_the_sidecar_and_returns_a_league_table():
    """Bucher cannot represent within-study correlation, so this must not fall back to it."""
    response = await _run({
        "MULTI": [
            _arm("Rinvoq", 120, 200), _arm("Humira", 100, 200), _arm("Placebo", 60, 200),
        ],
    })
    pairs = {(c.treatment, c.comparator) for c in response.contrasts}
    assert len(pairs) == 3


# =====================================================================================
# The sidecar decides nothing for itself
# =====================================================================================
async def test_a_request_with_no_effect_measure_is_refused_rather_than_defaulted():
    """A default applied on the far side of a wire is methodology nobody approved."""
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            SIDECAR.rstrip("/") + "/nma",
            json={
                "contract_version": netmeta.CONTRACT_VERSION,
                "outcome_type": "binary",
                "model": "fixed",
                "reference_treatment": "Placebo",
                "studies": [{
                    "study_id": "S1",
                    "arms": [
                        {"treatment": "Rinvoq", "events": 120, "sample_size": 200},
                        {"treatment": "Placebo", "events": 60, "sample_size": 200},
                    ],
                }],
            },
        )
    assert response.status_code == 400
    assert "effect_measure" in response.json()["error"]


async def test_a_contract_version_mismatch_fails_loudly():
    """A sidecar built against an older contract must not silently misread fields."""
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            SIDECAR.rstrip("/") + "/nma",
            json={"contract_version": "0", "outcome_type": "binary"},
        )
    assert response.status_code == 409


async def test_an_outage_is_a_service_status_not_an_evidence_gap():
    """Asserted against a real dead port rather than a mock, since that is the failure mode."""
    request = netmeta.build_request(
        STAR, outcome_type="binary", effect_measure="risk_ratio",
        reference_treatment="Placebo",
    )
    response = await netmeta.run(request, base_url="http://127.0.0.1:1", timeout=2.0)
    assert not response.ok
    assert response.status == "NMA_SERVICE_UNAVAILABLE"
