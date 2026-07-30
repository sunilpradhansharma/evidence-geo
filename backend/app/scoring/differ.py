"""Response diff & material-change detection (FR-306, BR-004)."""
import difflib

MATERIAL_CHANGE_THRESHOLD = 0.85  # similarity below this = flagged as material change


def compute_diff(previous: str, current: str) -> tuple[float, str]:
    """Return (similarity_ratio, unified_diff_text)."""
    ratio = difflib.SequenceMatcher(None, previous or "", current or "").ratio()
    diff = "\n".join(
        difflib.unified_diff(
            (previous or "").splitlines(),
            (current or "").splitlines(),
            fromfile="previous",
            tofile="current",
            lineterm="",
        )
    )
    return round(ratio, 4), diff


def is_material_change(ratio: float) -> bool:
    return ratio < MATERIAL_CHANGE_THRESHOLD
