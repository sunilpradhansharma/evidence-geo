"""Config tests for the Discover Questions (Harvest) allowlist additions.

The two RA community sites are integrated into Harvest purely via the Tavily domain
allowlist (no code change), so these assertions guard the config: both domains are
present and the Balanced-coverage knobs are set. The harvest pipeline itself is
source-agnostic and unchanged.
"""
from app.config.settings import load_yaml_config


def _cfg() -> dict:
    return load_yaml_config("harvest_sources.yaml")


def test_ra_community_domains_in_allowlist():
    domains = _cfg()["tavily"]["include_domains"]
    assert "myrateam.com" in domains
    assert "bezzyra.com" in domains


def test_balanced_coverage_settings_applied():
    cfg = _cfg()
    tavily = cfg["tavily"]
    # Full page text = many more mined questions (the Balanced trade-off).
    assert tavily["include_raw_content"] is True
    assert tavily["max_results_per_query"] == 12
    assert cfg["harvest"]["max_questions_per_item"] == 8
