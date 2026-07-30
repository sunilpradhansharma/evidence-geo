"""Light-touch PII/PHI redaction — preserve the real phrasing, strip only identifiers.

We deliberately do NOT reword questions (we want them as real people ask). Detection
and redaction now delegate to the central ``app.compliance.phi`` module (G2), which
covers the direct identifiers (email/phone/SSN/MRN/date/age/@handles) plus, when the
NLP backend is enabled, the harder HIPAA Safe-Harbor identifiers (names, geography).
The set of PII/PHI *types* found is returned for transparency and surfaced to the
human reviewer.
"""
import re

from app.compliance import phi

# G5: usernames embedded in source URLs re-identify a poster even after the text is
# scrubbed. Strip the username segment from the common patient-community URL shapes
# (e.g. reddit.com/user/<name>, /u/<name>, healthunlocked.com/<community>/posts is fine
# but profile links are not). We keep the host + path-to-thread so reviewers can still
# locate context, while removing the direct author handle.
_URL_USER_PATTERNS = [
    re.compile(r"(?i)(reddit\.com)/u(?:ser)?/[^/?#]+", ),
    re.compile(r"(?i)(/user/)[^/?#]+"),
    re.compile(r"(?i)(/u/)[^/?#]+"),
    re.compile(r"(?i)(/profile/)[^/?#]+"),
    re.compile(r"(?i)(/members?/)[^/?#]+"),
    re.compile(r"(?i)(/people/)[^/?#]+"),
]


def scrub_source_url(url: str | None) -> str | None:
    """Remove author/username segments from a source URL (re-identification control)."""
    if not url:
        return url
    out = url
    out = _URL_USER_PATTERNS[0].sub(r"\1/user/[redacted]", out)
    for pat in _URL_USER_PATTERNS[1:]:
        out = pat.sub(r"\1[redacted]", out)
    return out


def redact(text: str) -> tuple[str, list[str]]:
    """Return (clean_text, flags). Regex layer only — synchronous, network-free."""
    return phi.redact(text)


async def redact_async(text: str) -> tuple[str, list[str]]:
    """Return (clean_text, flags) including the optional NLP layer (Comprehend Medical).

    Use from async paths (the harvest pipeline) so the NLP network call runs off the
    event loop. Falls back to the regex layer when the NLP backend is disabled/erroring.
    """
    return await phi.redact_async(text)
