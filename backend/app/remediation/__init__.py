"""GEO Intervention Recommendation engine (BR-012).

Turns scored competitive-position gaps into ranked, plain-language, evidence-backed
content recommendations, enriched with SEMrush SEO metrics. See:
  - gaps.py     — extract SECOND_LINE / NOT_RECOMMENDED gaps + supporting evidence
  - semrush.py  — SEO enrichment (live REST, deterministic stub fallback)
  - prompts.py  — reasoning prompt + approved content-type enum
  - engine.py   — orchestrates find -> enrich -> reason -> score -> persist
"""
