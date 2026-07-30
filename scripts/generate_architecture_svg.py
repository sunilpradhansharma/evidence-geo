"""Generate the project architecture diagram as a self-contained SVG.

Hand-tuning ~80 coordinates is error prone, so the layout is computed here and the
SVG is emitted deterministically. Re-run after changing the component lists below.

    python scripts/generate_architecture_svg.py [out.svg]

Default output: Evidence_Monitoring_Agent_Architecture.svg (repo root).
Render to PNG with scripts/render_svg_to_png.py.
"""
from __future__ import annotations

import pathlib
import sys

# ---------------------------------------------------------------- canvas / layout
W = 1820
LEFT = 70
MAIN_R = 1330                 # right edge of the main (left) column
CONTENT_W = MAIN_R - LEFT     # 1260
EXT_X = 1410                  # external-services column
EXT_W = 380
GAP = 26                      # vertical gap between lanes (room for arrows)
CARD_GAP = 16
TITLE_H = 78
FONT = "'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"

# header, card fill, card stroke, lane background, card text
PAL = {
    "fe":    ("#1565C0", "#E3F2FD", "#1565C0", "#F3F8FE", "#0D2A4A"),
    "edge":  ("#455A64", "#CFD8DC", "#455A64", "#F6F8F9", "#1B262C"),
    "api":   ("#2E7D32", "#E8F5E9", "#2E7D32", "#F4FBF5", "#14361B"),
    "svc":   ("#558B2F", "#F1F8E9", "#558B2F", "#F8FBF2", "#243314"),
    "eng":   ("#E65100", "#FFF3E0", "#E65100", "#FFF9F2", "#3E2200"),
    "prov":  ("#6A1B9A", "#F3E5F5", "#6A1B9A", "#FBF5FD", "#2E0B3A"),
    "intel": ("#4527A0", "#EDE7F6", "#4527A0", "#F6F4FD", "#1B1240"),
    "data":  ("#37474F", "#ECEFF1", "#37474F", "#F7F9FB", "#1B262C"),
    "ext":   ("#BF360C", "#FBE9E7", "#BF360C", "#FEF6F4", "#3E1106"),
}

# ----------------------------------------------------------------- lane content
# Each lane: (key, label, height, [ (title, [sub-lines]) , ... ])
LANES = [
    ("fe", "FRONTEND  ·  REACT + VITE SPA (served by nginx)", 116, [
        ("Workflow pages", ["Discover · Question Bank", "Run · Clinician · Results"]),
        ("Dashboard", ["Overview · Insights · Cortex"]),
        ("Cortex Chat Widget", ["Ask your data"]),
        ("Typed API client", ["client.ts"]),
    ]),
    ("__bar__", "nginx   ·   serve SPA  +  reverse-proxy  /api", 34, []),
    ("api", "BACKEND API  ·  FASTAPI ROUTERS", 116, [
        ("Core", ["questions · runs", "responses · scores"]),
        ("Analytics", ["analytics · insights · cortex"]),
        ("Discovery", ["harvest · openevidence", "openevidence_auto · geo"]),
        ("Ops", ["schedule · exports · health"]),
    ]),
    ("svc", "SERVICE LAYER  ·  BUSINESS LOGIC", 120, [
        ("Core services", ["run · question · response"]),
        ("OpenEvidence service", ["capture bridge"]),
        ("Harvest service", ["staging + review"]),
        ("Export services", ["export · pinpoint"]),
        ("Scheduler", ["APScheduler · schedule_service"]),
    ]),
    ("eng", "AGENT  ·  RUN ENGINE", 122, [
        ("Orchestrator", ["dispatch · retry", "rate-limit · resume"]),
        ("Guards", ["rate_limiter · budget", "cancellation · validator"]),
        ("Intent classifier", ["per-question intent"]),
        ("Chairman", ["council consensus", "+ final answer"]),
    ]),
    ("prov", "PROVIDER LAYER  ·  REGISTRY (targets.yaml)", 110, [
        ("Bedrock client", ["Converse API"]),
        ("OpenAI client", ["Responses API"]),
        ("Google client", ["Gemini API / Vertex"]),
        ("OpenEvidence client", ["disabled stub"]),
    ]),
    ("intel", "INTELLIGENCE + DISCOVERY", 132, [
        ("Scoring", ["sentiment +", "competitive position"]),
        ("Alerts + Diff", ["alert engine", "change detection"]),
        ("Insights", ["themes · signals", "taxonomy · tagging"]),
        ("Harvest pipeline", ["Tavily · scrub PII", "classify"]),
        ("OpenEvidence bot", ["Playwright", "unattended"]),
    ]),
    ("data", "DATA + PERSISTENCE", 124, [
        ("SQLAlchemy models", ["+ append-only audit log"]),
        ("SQLite", ["operational store", "(immutable responses)"]),
        ("Snowflake mirror", ["incremental · every 10 min"]),
    ]),
]

EXTERNAL = [
    ("AWS Bedrock", ["Claude · Nova Pro · Llama", "(targets + scoring + orchestrator)"]),
    ("OpenAI", ["GPT-4o", "hosted web search"]),
    ("Google Gemini", ["Search grounding"]),
    ("OpenEvidence", ["HCP-gated web UI", "no public API"]),
    ("Tavily", ["web search API", "(question discovery)"]),
    ("Snowflake + Cortex", ["warehouse + views", "LLM · Analyst · Agent"]),
]


# --------------------------------------------------------------------- helpers
def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def rrect(x, y, w, h, rx, fill, stroke, sw=1.6, extra=""):
    return (f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'rx="{rx}" ry="{rx}" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="{sw}" {extra}/>')


def text(x, y, s, size, color, weight="normal", anchor="middle", spacing=""):
    sp = f' letter-spacing="{spacing}"' if spacing else ""
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" '
            f'font-size="{size}" font-weight="{weight}" fill="{color}" '
            f'text-anchor="{anchor}"{sp}>{esc(s)}</text>')


def card(x, y, w, h, title, lines, pal):
    _, fill, stroke, _, tcol = pal
    out = [rrect(x, y, w, h, 9, fill, stroke, 1.6,
                 extra='filter="url(#cardShadow)"')]
    n = len(lines)
    block = 18 + n * 15
    ty = y + (h - block) / 2 + 14
    cx = x + w / 2
    out.append(text(cx, ty, title, 14.5, tcol, "700"))
    for i, ln in enumerate(lines):
        out.append(text(cx, ty + 19 + i * 15, ln, 11.3, "#475569"))
    return "".join(out)


def row_positions(n, x0, total_w, gap):
    cw = (total_w - (n - 1) * gap) / n
    return [(x0 + i * (cw + gap), cw) for i in range(n)]


# --------------------------------------------------------------------- assemble
def build() -> str:
    s: list[str] = []
    # compute total height
    y = TITLE_H + 22
    laid: list[tuple] = []   # (key, label, h, cards, y)
    for key, label, h, cards in LANES:
        laid.append((key, label, h, cards, y))
        y += h + GAP
    bottom = y - GAP + 24
    H = max(bottom, TITLE_H + 22 + 1058)   # ensure room for external column

    s.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="{FONT}">')
    s.append('<defs>'
             '<marker id="ah" markerWidth="10" markerHeight="10" refX="8" refY="3" '
             'orient="auto" markerUnits="userSpaceOnUse">'
             '<path d="M0,0 L9,3 L0,6 Z" fill="#64748B"/></marker>'
             '<filter id="cardShadow" x="-4%" y="-4%" width="108%" height="116%">'
             '<feDropShadow dx="0" dy="1.2" stdDeviation="1.4" '
             'flood-color="#0F172A" flood-opacity="0.14"/></filter>'
             '</defs>')
    s.append(rrect(0, 0, W, H, 0, "#FFFFFF", "#FFFFFF", 0))

    # title
    s.append(text(LEFT, 44, "Evidence Monitoring Agent", 30, "#0F172A", "800",
                  anchor="start"))
    s.append(text(LEFT + 462, 44, "·  Architecture", 22, "#64748B", "600",
                  anchor="start"))
    s.append(text(LEFT, 66, "Component map and data flow: what LLMs say about "
                  "pharmaceutical therapies, scored and surfaced.",
                  13, "#64748B", "500", anchor="start"))

    cx_main = LEFT + CONTENT_W / 2

    # external column container
    ext_pal = PAL["ext"]
    ext_y = laid[0][4]
    ext_h = bottom - ext_y - 24
    s.append(rrect(EXT_X, ext_y, EXT_W, ext_h, 14, ext_pal[3], ext_pal[2], 1.8))
    s.append(rrect(EXT_X, ext_y, EXT_W, 30, 14, ext_pal[0], ext_pal[0], 0))
    s.append(rrect(EXT_X, ext_y + 16, EXT_W, 14, 0, ext_pal[0], ext_pal[0], 0))
    s.append(text(EXT_X + EXT_W / 2, ext_y + 20, "EXTERNAL SERVICES",
                  13, "#FFFFFF", "700", spacing="1.2"))
    ne = len(EXTERNAL)
    inner_top = ext_y + 44
    inner_h = ext_h - 58
    ech = (inner_h - (ne - 1) * 14) / ne
    ext_card_y = []
    for i, (title, lines) in enumerate(EXTERNAL):
        cy = inner_top + i * (ech + 14)
        ext_card_y.append(cy + ech / 2)
        s.append(card(EXT_X + 18, cy, EXT_W - 36, ech, title, lines, ext_pal))

    # lanes
    lane_centers = []   # (key, top, bottom, mid)
    for key, label, h, cards, ly in laid:
        if key == "__bar__":
            pal = PAL["edge"]
            s.append(rrect(LEFT, ly, CONTENT_W, h, 8, pal[1], pal[2], 1.6))
            s.append(text(cx_main, ly + h / 2 + 4.5, label, 12.5, pal[4], "700",
                          spacing="0.5"))
            lane_centers.append((key, ly, ly + h, ly + h / 2))
            continue
        pal = PAL[key]
        s.append(rrect(LEFT, ly, CONTENT_W, h, 12, pal[3], "#E2E8F0", 1.2))
        # label tab (left-anchored so long labels never clip)
        lw = len(label) * 7.15 + 26
        s.append(rrect(LEFT + 14, ly + 12, lw, 22, 6, pal[0], pal[0], 0))
        s.append(text(LEFT + 26, ly + 27, label, 11.5, "#FFFFFF", "700",
                      anchor="start", spacing="0.6"))
        # cards
        cards_top = ly + 44
        cards_h = h - 56
        for (cx, cw), (title, lines) in zip(
                row_positions(len(cards), LEFT + 16, CONTENT_W - 32, CARD_GAP), cards):
            s.append(card(cx, cards_top, cw, cards_h, title, lines, pal))
        lane_centers.append((key, ly, ly + h, ly + h / 2))

    # ---- main vertical spine arrows (between consecutive lanes) ----
    for i in range(len(lane_centers) - 1):
        _, _, b0, _ = lane_centers[i]
        _, t1, _, _ = lane_centers[i + 1]
        s.append(f'<line x1="{cx_main}" y1="{b0 + 3:.1f}" x2="{cx_main}" '
                 f'y2="{t1 - 5:.1f}" stroke="#64748B" stroke-width="2.4" '
                 f'marker-end="url(#ah)"/>')

    def mid_of(k):
        return next(m for (kk, _, _, m) in lane_centers if kk == k)

    # ---- right-side arrows into external services ----
    def right_arrow(lane_key, label):
        my = mid_of(lane_key)
        x0 = MAIN_R + 3
        x1 = EXT_X - 7
        s.append(f'<line x1="{x0:.1f}" y1="{my:.1f}" x2="{x1:.1f}" y2="{my:.1f}" '
                 f'stroke="#64748B" stroke-width="2.2" marker-end="url(#ah)"/>')
        s.append(text((x0 + x1) / 2, my - 9, label, 10.5, "#475569", "600"))

    right_arrow("prov", "LLM calls")
    right_arrow("intel", "web + OE")
    right_arrow("data", "mirror")

    # footnote
    s.append(text(LEFT, bottom - 2,
                  "Optional and off by default (no-op when disabled): "
                  "Snowflake + Cortex, OpenEvidence capture, and the daily scheduler. "
                  "Content is config-driven (brands.yaml, targets.yaml).",
                  11.5, "#94A3B8", "500", anchor="start"))

    s.append("</svg>")
    return "".join(s)


def main() -> None:
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else (
        pathlib.Path(__file__).resolve().parents[1]
        / "Evidence_Monitoring_Agent_Architecture.svg")
    out.write_text(build(), encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
