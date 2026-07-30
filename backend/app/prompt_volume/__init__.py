"""AI Prompt Volume Intelligence pipeline (FR-116).

Manual CSV ingestion of third-party search-demand exports (Semrush/Ahrefs), used as a
proxy for AI-inquiry demand. Submodules:

  parser   — Pandas CSV read + column-alias resolution + volume coercion
  linter   — whole-file PII pre-flight (rejects the entire upload on any hit)
  mapping  — map each query to the brand taxonomy (via config.taxonomy)
  gap      — normalized token-overlap matching, topic grouping, demand ranking
  engine   — orchestration (parse -> lint -> map -> analyze -> persist, atomic)
"""
