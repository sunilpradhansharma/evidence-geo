"""Re-run feature: build_rerun_data rebuilds a RunCreate from a prior run's config_snapshot,
so an operator can re-run the same question set without reselecting it. Pure/synchronous —
no DB or network involved (the Run model instance is constructed in-memory)."""
import json

from app.models.run import Run
from app.services.run_service import build_rerun_data


def _run(monitoring_mode: str = "BRAND", config_snapshot: str | None = None) -> Run:
    return Run(run_id="r1", monitoring_mode=monitoring_mode, config_snapshot=config_snapshot)


def test_rerun_reuses_snapshot_question_ids_and_filters():
    snap = {
        "monitoring_mode": "DISEASE_STATE",
        "filters": {
            "persona": "Patient",
            "therapeutic_area": "Immunology",
            "domain": "Safety",
            "question_ids": ["q1", "q2", "q3"],
        },
        "dry_run": True,  # original was a dry-run; the re-run must still be real
    }
    data = build_rerun_data(_run(monitoring_mode="BRAND", config_snapshot=json.dumps(snap)))

    assert data.trigger == "ADHOC"
    assert data.monitoring_mode == "DISEASE_STATE"  # snapshot wins over the row's mode
    assert data.persona == "Patient"
    assert data.therapeutic_area == "Immunology"
    assert data.domain == "Safety"
    assert data.question_ids == ["q1", "q2", "q3"]
    assert data.dry_run is False


def test_rerun_without_snapshot_falls_back_to_run_mode():
    data = build_rerun_data(_run(monitoring_mode="DISEASE_STATE", config_snapshot=None))

    assert data.monitoring_mode == "DISEASE_STATE"
    assert data.question_ids is None
    assert data.persona is None
    assert data.dry_run is False


def test_rerun_tolerates_malformed_snapshot():
    data = build_rerun_data(_run(monitoring_mode="BRAND", config_snapshot="{not valid json"))

    assert data.monitoring_mode == "BRAND"
    assert data.question_ids is None
