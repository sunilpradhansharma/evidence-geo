"""FR-108a Discover-Questions monitoring-mode tests.

Verifies that the harvest query builder scopes to AbbVie focus brands in BRAND mode and
widens to the whole landscape (focus + competitors) in DISEASE_STATE / "All Brands" mode.
"""
from app.harvest.pipeline import _expand

_BRANDS = {
    "therapeutic_areas": {
        "Immunology": {
            "focus_brands": [{"name": "Rinvoq", "indications": ["Rheumatoid Arthritis"]}],
            "competitors": [{"name": "Dupixent"}, {"name": "Cosentyx"}],
        }
    }
}


def test_brand_mode_scopes_to_focus_brands_only():
    qs = _expand(["what do people say about {brand}"], _BRANDS, landscape=False)
    joined = " ".join(qs)
    assert "Rinvoq" in joined
    assert "Dupixent" not in joined
    assert "Cosentyx" not in joined


def test_landscape_mode_includes_all_brands():
    qs = _expand(["what do people say about {brand}"], _BRANDS, landscape=True)
    joined = " ".join(qs)
    # All Brands mode covers the AbbVie focus brand AND every competitor in the space.
    assert "Rinvoq" in joined
    assert "Dupixent" in joined
    assert "Cosentyx" in joined


def test_landscape_ignores_brand_filter():
    # A brand_filter must NOT narrow a landscape run back down to a single AbbVie asset.
    qs = _expand(["about {brand}"], _BRANDS, brand_filter="Rinvoq", landscape=True)
    joined = " ".join(qs)
    assert "Dupixent" in joined and "Cosentyx" in joined
