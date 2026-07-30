"""Verified vendor version sources (FR-707a).

Maps each monitored target platform (Response.llm_name / targets.yaml `name`) to the
vendor whose public changelog / release-notes / What's-New feed we capture. URLs are
config-driven (settings.model_update_*_url) so an operator can blank one to skip a
vendor or point it at a mirror. OpenEvidence / EvidenceMD have no public changelog, so
they are intentionally absent here and keep the drift-inferred fallback.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.config.settings import get_settings


@dataclass(frozen=True)
class VendorSource:
    """One vendor changelog source and the target platforms it covers."""

    vendor: str                       # display label, e.g. "OpenAI"
    platforms: tuple[str, ...]        # target names it applies to (lowercase, e.g. "gpt-4o")
    fmt: str                          # "html" | "rss"
    settings_attr: str                # settings attribute holding the source URL
    # Extra hint fed to the extractor so an aggregated feed (AWS What's New) can be
    # filtered down to only the entries relevant to these platforms.
    focus: str = ""

    def url(self) -> str:
        return (getattr(get_settings(), self.settings_attr, "") or "").strip()

    def enabled(self) -> bool:
        return bool(self.url())


# Ordered most- to least-structured. Platform names are matched case-insensitively.
VENDOR_SOURCES: list[VendorSource] = [
    VendorSource(
        vendor="OpenAI",
        platforms=("gpt-4o",),
        fmt="html",
        settings_attr="model_update_openai_changelog_url",
        focus="OpenAI GPT-4o and gpt-4o dated snapshot models",
    ),
    VendorSource(
        vendor="Anthropic",
        platforms=("claude",),
        fmt="html",
        settings_attr="model_update_anthropic_changelog_url",
        focus="Anthropic Claude models (Claude 3.x / 3.5 / 4)",
    ),
    VendorSource(
        vendor="Google",
        platforms=("gemini",),
        fmt="html",
        settings_attr="model_update_google_changelog_url",
        focus="Google Gemini API models (gemini-1.5 / 2.x)",
    ),
    VendorSource(
        vendor="AWS Bedrock",
        platforms=("nova-pro", "llama"),
        fmt="rss",
        settings_attr="model_update_aws_whatsnew_rss_url",
        focus="Amazon Bedrock foundation models — Amazon Nova (Nova Pro) and Meta Llama",
    ),
]


def enabled_sources() -> list[VendorSource]:
    """Vendor sources with a configured, non-blank URL."""
    return [s for s in VENDOR_SOURCES if s.enabled()]


# Platforms that have NO public changelog and therefore keep the drift-inferred fallback.
INFERRED_ONLY_PLATFORMS: frozenset[str] = frozenset({"evidencemd", "open-evidence", "open-evidence"})
