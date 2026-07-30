"""FR-707a vendor version + changelog capture.

Anchors AI Update Impact to REAL vendor model versions: it verifies vendor version
sources (public changelogs / release notes / What's-New feeds) and captures the
"what changed" text, upserting each into the model_release_log as an enriched update
event. Opt-in (like Snowflake/SES) via settings.model_update_sync_enabled — with it
off, every network call is a safe no-op and the tab still works from our own traffic
signal (Response.llm_model_version).
"""
from app.model_updates.sync import is_enabled, sync_model_updates, sync_status

__all__ = ["is_enabled", "sync_model_updates", "sync_status"]
