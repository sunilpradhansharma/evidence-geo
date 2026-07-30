"""Snowflake integration package.

The backend mirrors all operational data (questions, responses, scores, insights,
audit log, and raw API input/output) into Snowflake and uses Cortex for additional
insights plus a natural-language Q&A surfaced in the UI. SQLite remains the fast
operational store; Snowflake is the warehouse + Cortex layer.

Dashboard analytics endpoints query Snowflake views first (see ``analytics.py``),
falling back to SQLite via ``fallback.py`` when Snowflake is disabled or errors.

Everything degrades gracefully: when SNOWFLAKE_ENABLED is false or credentials are
missing, every call here is a no-op so the app runs unchanged.
"""
