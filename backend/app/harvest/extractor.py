"""Pull verbatim candidate questions out of fetched page content.

Real questions are kept as-asked (the whole point of harvesting). We detect them by
the '?' terminator, plus forum/Reddit/Quora titles that begin with an interrogative
even when the title omits the question mark.
"""
import re

_INTERROGATIVE = re.compile(
    r"^(how|what|why|when|where|which|who|whom|is|are|am|can|could|should|"
    r"would|will|do|does|did|has|have|had|may|might)\b",
    re.IGNORECASE,
)
_SPLIT = re.compile(r"(?<=[.!?])\s+|[\r\n]+")
_WS = re.compile(r"\s+")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")


def _clean(text: str) -> str:
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = text.strip().strip("\"'“”*>#-• ").strip()
    return _WS.sub(" ", text)


def extract_questions(item, *, min_len: int = 15, max_len: int = 320, cap: int = 5) -> list[str]:
    """Return up to `cap` distinct verbatim questions from a RawItem (title + content)."""
    out: list[str] = []
    seen: set[str] = set()

    def consider(raw: str, *, allow_no_qmark: bool = False) -> None:
        c = _clean(raw)
        if not (min_len <= len(c) <= max_len):
            return
        is_question = c.endswith("?") or (allow_no_qmark and bool(_INTERROGATIVE.match(c)))
        if not is_question:
            return
        if not c.endswith("?"):
            c = c + "?"
        key = c.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(c)

    # Forum/Reddit/Quora titles are very often the question itself.
    if getattr(item, "title", None):
        consider(item.title, allow_no_qmark=True)

    for chunk in _SPLIT.split(getattr(item, "content", "") or ""):
        if "?" not in chunk:
            continue
        # keep text up to and including the first '?' in the chunk
        consider(chunk.split("?", 1)[0] + "?")
        if len(out) >= cap:
            break

    return out[:cap]
