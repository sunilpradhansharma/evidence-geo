"""Numbered-zone (PepsiCo-style) end-to-end architecture SVG.

    python scripts/generate_zone_architecture_svg.py [out.svg]
Render with scripts/render_svg_to_png.py.
"""
from __future__ import annotations
import pathlib, sys

W = 1860
FONT = "'Segoe UI', Roboto, Arial, sans-serif"
ZY, ZH = 100, 700
PAL = {  # header, card fill, stroke, zone bg, text
    "ui":   ("#7C3AED", "#FFFFFF", "#C4B5FD", "#F5F3FF", "#3B0A6B"),
    "plat": ("#EA580C", "#FFFFFF", "#FDBA74", "#FFF7ED", "#7C2D12"),
    "ext":  ("#2563EB", "#FFFFFF", "#93C5FD", "#EFF6FF", "#1E3A8A"),
    "ai":   ("#0891B2", "#FFFFFF", "#67E8F9", "#ECFEFF", "#155E63"),
    "store":("#16A34A", "#FFFFFF", "#86EFAC", "#F0FDF4", "#14532D"),
    "out":  ("#9333EA", "#FFFFFF", "#D8B4FE", "#FAF5FF", "#4A1078"),
    "mon":  ("#475569", "#FFFFFF", "#CBD5E1", "#F8FAFC", "#1E293B"),
}
ZONES = [
    ("1", "Users & UI", "ui", [
        ("Internal users / Client team", ["Medical Affairs · Commercial"]),
        ("React + Vite SPA", ["Discover · Question Bank · Run", "Clinician · Results · Dashboard"]),
        ("Cortex Chat Widget", ["Ask your data"]),
    ]),
    ("2", "Backend Platform / Orchestration", "plat", [
        ("nginx + FastAPI API", ["routers + App Events"]),
        ("Run Engine (orchestrator)", ["dispatch · retry", "rate-limit · resume"]),
        ("Guards", ["budget · cancel · validator", "intent classifier"]),
        ("Scheduler (APScheduler)", ["daily run · mirror job"]),
    ]),
    ("3", "External APIs", "ext", [
        ("Tavily", ["web search", "question discovery"]),
        ("OpenEvidence", ["HCP-gated web UI", "manual + Playwright bot"]),
    ]),
    ("4", "AI / Multi-Model Extraction", "ai", [
        ("AWS Bedrock", ["Claude · Nova Pro · Llama"]),
        ("OpenAI GPT-4o · Google Gemini", ["web search + grounding"]),
        ("Chairman consensus", ["arbitrate + final answer"]),
        ("Scoring + Insights", ["sentiment · position", "themes · signals"]),
    ]),
    ("5", "Storage & Outputs", "store", [
        ("SQLite (operational)", ["immutable responses", "scores · consensus"]),
        ("Audit log", ["append-only"]),
        ("Snowflake + Cortex", ["mirror · warehouse", "LLM · Analyst · Agent"]),
    ]),
    ("6", "Consumption & Delivery", "out", [
        ("Dashboard visualization", ["overview · insights · trends"]),
        ("Results review", ["responses + sources"]),
        ("Export", ["JSON / CSV · Pinpoint"]),
    ]),
    ("7", "Monitoring / Security", "mon", [
        ("JSON logging + redaction", ["credential-safe"]),
        ("PII lint", ["no PII stored"]),
        ("App Events capture", ["every request"]),
        ("Secrets (.env)", ["AWS · API keys"]),
    ]),
]
FUTURE = ["Phase B ready hooks", "Scheduled daily crawl", "Change detection (diff)",
          "Historical versioning (SCD)", "GEO findings"]


def esc(s): return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
def rr(x, y, w, h, r, f, s, sw=1.4, e=""):
    return f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{r}" ry="{r}" fill="{f}" stroke="{s}" stroke-width="{sw}" {e}/>'
def tx(x, y, s, sz, c, w="normal", a="middle"):
    return f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{sz}" font-weight="{w}" fill="{c}" text-anchor="{a}">{esc(s)}</text>'


def build():
    n = len(ZONES); gap = 18; x0 = 40
    cw = (W - 2 * x0 - (n - 1) * gap) / n
    xs = [x0 + i * (cw + gap) for i in range(n)]
    H = 1010
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="{FONT}">']
    o.append('<defs><marker id="a" markerWidth="11" markerHeight="11" refX="8" refY="3.2" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L9,3.2 L0,6.4 Z" fill="#64748B"/></marker>'
             '<filter id="sh" x="-6%" y="-6%" width="112%" height="120%"><feDropShadow dx="0" dy="1" stdDeviation="1.3" flood-color="#0F172A" flood-opacity="0.16"/></filter></defs>')
    o.append(rr(0, 0, W, H, 0, "#FFFFFF", "#FFFFFF", 0))
    o.append(tx(W / 2, 50, "Evidence Monitoring Agent  ·  End-to-End Architecture", 27, "#0F172A", "800"))
    o.append(tx(W / 2, 74, "What LLMs say about pharmaceutical therapies: discover, dispatch, score, and surface.", 13, "#64748B", "500"))

    for (num, label, key, cards), x in zip(ZONES, xs):
        hdr, cf, st, bg, tc = PAL[key]
        o.append(rr(x, ZY, cw, ZH, 12, bg, "#E2E8F0", 1.2))
        o.append(rr(x, ZY, cw, 30, 12, hdr, hdr, 0))
        o.append(rr(x, ZY + 16, cw, 14, 0, hdr, hdr, 0))
        o.append(tx(x + cw / 2, ZY + 20, f"{num} · {label}", 12, "#FFFFFF", "700"))
        m = len(cards); top = ZY + 42; area = ZH - 54
        ch = (area - (m - 1) * 12) / m
        for i, (t, lines) in enumerate(cards):
            cy = top + i * (ch + 12)
            o.append(rr(x + 12, cy, cw - 24, ch, 8, cf, st, 1.4, 'filter="url(#sh)"'))
            blk = 16 + len(lines) * 13.5; ty = cy + (ch - blk) / 2 + 13
            o.append(tx(x + cw / 2, ty, t, 11.6, tc, "700"))
            for j, ln in enumerate(lines):
                o.append(tx(x + cw / 2, ty + 16 + j * 13.5, ln, 10, "#475569"))

    # left-to-right flow arrows between zone mid-rights
    my = ZY + ZH / 2
    for i in range(n - 2):  # 1..6 (skip into monitoring)
        a = xs[i] + cw + 2; b = xs[i + 1] - 4
        o.append(f'<line x1="{a:.1f}" y1="{my:.1f}" x2="{b:.1f}" y2="{my:.1f}" stroke="#64748B" stroke-width="2.4" marker-end="url(#a)"/>')
    # monitoring dashed back-link
    mx = xs[-1]
    o.append(f'<path d="M{mx:.1f},{my:.1f} H{xs[-2]+cw+6:.1f}" fill="none" stroke="#94A3B8" stroke-width="1.8" stroke-dasharray="5 4" marker-end="url(#a)"/>')

    # future / Phase B dotted band
    fy = ZY + ZH + 24
    o.append(rr(40, fy, W - 80, 60, 12, "#F8FAFC", "#94A3B8", 1.6, 'stroke-dasharray="6 5"'))
    o.append(tx(70, fy + 35, "Phase B / Future", 13, "#475569", "800", "start"))
    fx = 230; step = (W - 80 - fx) / len(FUTURE)
    for i, f in enumerate(FUTURE):
        o.append(tx(fx + step * (i + 0.5), fy + 35, f, 11.5, "#64748B", "600"))

    # legend
    ly = fy + 84
    o.append(tx(40, ly + 4, "Legend", 12.5, "#334155", "800", "start"))
    items = [("Users / Outputs", "#9333EA"), ("Backend / Orchestration", "#EA580C"),
             ("External APIs", "#2563EB"), ("AI / Extraction", "#0891B2"),
             ("Storage", "#16A34A"), ("Monitoring / Security", "#475569")]
    lx = 120
    for lab, col in items:
        o.append(rr(lx, ly - 9, 16, 14, 3, col, col, 0))
        o.append(tx(lx + 22, ly + 4, lab, 11, "#475569", "600", "start"))
        lx += 36 + len(lab) * 6.6
    o.append(f'<line x1="{lx:.1f}" y1="{ly-2:.1f}" x2="{lx+26:.1f}" y2="{ly-2:.1f}" stroke="#64748B" stroke-width="2.4" marker-end="url(#a)"/>')
    o.append(tx(lx + 34, ly + 4, "Data flow", 11, "#475569", "600", "start"))
    o.append("</svg>")
    return "".join(o)


def main():
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else (
        pathlib.Path(__file__).resolve().parents[1] / "Evidence_Monitoring_Agent_Zone_Architecture.svg")
    out.write_text(build(), encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
