"""Frontend page flow of the AI Response Review tab (frontend/src/pages/Results.tsx).

A vertical flowchart: shared load + run selection, then a fork into the two view
modes (Review browse table and Compare Platforms side-by-side), with the backend
API call each step makes shown inline.

    python scripts/generate_response_review_flow_svg.py [out.svg]
Render with scripts/render_svg_to_png.py.
"""
from __future__ import annotations
import pathlib, sys

FONT = "'Segoe UI', Roboto, Arial, sans-serif"

# fill, stroke, text
CAT = {
    "term":  ("#ECFDF5", "#10B981", "#065F46"),
    "ui":    ("#EFF6FF", "#3B82F6", "#1E3A8A"),
    "proc":  ("#F0FDFA", "#14B8A6", "#0F5F5A"),
    "dec":   ("#FFFBEB", "#F59E0B", "#92400E"),
    "api":   ("#F5F3FF", "#8B5CF6", "#4A1078"),
    "panel": ("#EEF2FF", "#6366F1", "#312E81"),
    "poll":  ("#ECFEFF", "#06B6D4", "#155E63"),
}

LX, RX, CX = 330, 1010, 670        # review lane, compare lane, shared spine centers
W = 1300

# id -> node.  x,y is the CENTER of the node.
NODES = {
    # ---- shared spine ----
    "start": dict(x=CX, y=130, w=300, h=44, shape="pill", cat="term",
                  title="Open  ·  AI Response Review"),
    "load":  dict(x=CX, y=214, w=372, h=66, shape="rect", cat="proc",
                  title="On mount — load context", ts=12.5, ss=9.6,
                  subs=["GET /runs   ·   GET /questions",
                        "GET /analytics/llm-comparison"]),
    "run":   dict(x=CX, y=308, w=300, h=56, shape="rect", cat="ui",
                  title="Select Run", subs=["All runs (global)  ·  or one run"]),
    "summary": dict(x=CX, y=410, w=384, h=74, shape="rect", cat="proc",
                    title="Run Summary dashboard", ts=12.5, ss=9.4,
                    subs=["when a run is selected · GET /analytics/run-summary",
                          "KPIs · consensus · sentiment/LLM · positioning · intent"]),
    "mode":  dict(x=CX, y=528, w=172, h=96, shape="diamond", cat="dec",
                  title="View mode?"),

    # ---- Review (browse) lane ----
    "lfilter": dict(x=LX, y=666, w=304, h=72, shape="rect", cat="ui",
                    title="Set review filters", ss=9.3,
                    subs=["persona · platform · TA · brand · disease",
                          "intent · consensus · search · alerts-only"]),
    "lget":  dict(x=LX, y=772, w=264, h=48, shape="pill", cat="api",
                  title="GET /responses", subs=["current run + filters"], ss=9.2),
    "ltable": dict(x=LX, y=866, w=304, h=72, shape="rect", cat="proc",
                   title="Response table", ss=9.3,
                   subs=["platform · focus · question · type",
                         "agreement · sentiment · position"]),
    "lclick": dict(x=LX, y=970, w=240, h=48, shape="rect", cat="ui",
                   title="Click a response row"),
    "ldget": dict(x=LX, y=1060, w=240, h=48, shape="pill", cat="api",
                  title="GET /responses/{id}"),
    "ldrawer": dict(x=LX, y=1174, w=344, h=98, shape="rect", cat="panel",
                    title="Response Detail Drawer", ts=12.5, ss=9.1,
                    subs=["question · response · sources · grounded claims",
                          "scoring rationale · key claims · alerts",
                          "consensus (final answer + overall) · GEO · diff"]),

    # ---- Compare Platforms lane ----
    "rfilter": dict(x=RX, y=666, w=300, h=64, shape="rect", cat="ui",
                    title="Set compare filters",
                    subs=["persona · TA · intent · search"]),
    "rqsel": dict(x=RX, y=764, w=240, h=48, shape="rect", cat="ui",
                  title="Select a question"),
    "rget":  dict(x=RX, y=858, w=270, h=48, shape="pill", cat="api",
                  title="GET /responses/compare", subs=["question_id"], ss=9.2),
    "rcards": dict(x=RX, y=966, w=324, h=96, shape="rect", cat="panel",
                   title="Side-by-side platform cards", ts=12.5, ss=9.2,
                   subs=["per-LLM sentiment + position",
                         "consensus banner + synthesized answer"]),

    # ---- converge ----
    "end":   dict(x=CX, y=1300, w=440, h=48, shape="pill", cat="term",
                  title="Analyst reads sentiment, position & consensus"),

    # ---- side / always-available ----
    "export": dict(x=1052, y=308, w=272, h=58, shape="pill", cat="api",
                   title="Header: Download CSV", ss=9.2,
                   subs=["GET /responses/export (run + filters)"]),
    "poll":  dict(x=628, y=866, w=252, h=72, shape="rect", cat="poll",
                  title="Auto-refresh while scoring", ss=9.2,
                  subs=["unscored rows → re-GET /responses",
                        "every 5s · up to 3 min"]),
}


def esc(s): return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def stack(cx, cy, lines):
    lh = [ln["s"] * 1.5 for ln in lines]
    y = cy - sum(lh) / 2
    out = []
    for ln, h in zip(lines, lh):
        out.append(
            f'<text x="{cx:.1f}" y="{y + h/2:.1f}" font-family="{FONT}" font-size="{ln["s"]}" '
            f'font-weight="{ln["w"]}" fill="{ln["c"]}" text-anchor="middle" '
            f'dominant-baseline="middle">{esc(ln["t"])}</text>')
        y += h
    return "".join(out)


def draw_node(n):
    f, s, tc = CAT[n["cat"]]
    x, y, w, h = n["x"], n["y"], n["w"], n["h"]
    shape = n["shape"]
    if shape == "diamond":
        o = (f'<polygon points="{x:.1f},{y-h/2:.1f} {x+w/2:.1f},{y:.1f} '
             f'{x:.1f},{y+h/2:.1f} {x-w/2:.1f},{y:.1f}" fill="{f}" stroke="{s}" '
             f'stroke-width="1.8" filter="url(#sh)"/>')
    elif shape == "pill":
        o = (f'<rect x="{x-w/2:.1f}" y="{y-h/2:.1f}" width="{w}" height="{h}" '
             f'rx="{h/2}" ry="{h/2}" fill="{f}" stroke="{s}" stroke-width="1.8" filter="url(#sh)"/>')
    else:
        o = (f'<rect x="{x-w/2:.1f}" y="{y-h/2:.1f}" width="{w}" height="{h}" '
             f'rx="11" ry="11" fill="{f}" stroke="{s}" stroke-width="1.8" filter="url(#sh)"/>')
    lines = [dict(t=n["title"], s=n.get("ts", 12.5), w="700", c=tc)]
    for sub in n.get("subs", []):
        lines.append(dict(t=sub, s=n.get("ss", 9.8), w="500", c=tc))
    return o + stack(x, y, lines)


def A(nid, side):
    n = NODES[nid]
    x, y, w, h = n["x"], n["y"], n["w"], n["h"]
    return {"top": (x, y - h / 2), "bottom": (x, y + h / 2),
            "left": (x - w / 2, y), "right": (x + w / 2, y)}[side]


def edge(d, color="#64748B", dash=False, wdt=1.9, marker="a"):
    da = ' stroke-dasharray="6 5"' if dash else ""
    return f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{wdt}"{da} marker-end="url(#{marker})"/>'


def vstraight(a_id, b_id, **kw):
    (x1, y1), (x2, y2) = A(a_id, "bottom"), A(b_id, "top")
    return edge(f"M{x1:.1f},{y1:.1f} V{y2:.1f}", **kw)


def velbow(a_id, b_id, busy, **kw):
    (x1, y1), (x2, y2) = A(a_id, "bottom"), A(b_id, "top")
    return edge(f"M{x1:.1f},{y1:.1f} V{busy:.1f} H{x2:.1f} V{y2:.1f}", **kw)


def build():
    H = 1408
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="{FONT}">']
    o.append('<defs>'
             '<marker id="a" markerWidth="11" markerHeight="11" refX="8" refY="3.2" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L9,3.2 L0,6.4 Z" fill="#64748B"/></marker>'
             '<marker id="v" markerWidth="11" markerHeight="11" refX="8" refY="3.2" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L9,3.2 L0,6.4 Z" fill="#8B5CF6"/></marker>'
             '<marker id="c" markerWidth="11" markerHeight="11" refX="8" refY="3.2" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L9,3.2 L0,6.4 Z" fill="#06B6D4"/></marker>'
             '<filter id="sh" x="-6%" y="-14%" width="112%" height="132%"><feDropShadow dx="0" dy="1" stdDeviation="1.3" flood-color="#0F172A" flood-opacity="0.14"/></filter>'
             '</defs>')
    o.append(f'<rect width="{W}" height="{H}" fill="#FFFFFF"/>')
    o.append(f'<text x="{CX}" y="50" font-family="{FONT}" font-size="26" font-weight="800" fill="#0F172A" text-anchor="middle">AI Response Review</text>')
    o.append(f'<text x="{CX}" y="76" font-family="{FONT}" font-size="13" font-weight="500" fill="#64748B" text-anchor="middle">Frontend page flow · Results.tsx — browse &amp; compare AI answers, with the backend API each step calls</text>')

    edges = []
    # shared spine
    edges.append(vstraight("start", "load"))
    edges.append(vstraight("load", "run"))
    edges.append(vstraight("run", "summary"))
    edges.append(vstraight("summary", "mode"))
    # fork
    edges.append(velbow("mode", "lfilter", 612))
    edges.append(velbow("mode", "rfilter", 612))
    # review lane
    edges.append(vstraight("lfilter", "lget"))
    edges.append(vstraight("lget", "ltable"))
    edges.append(vstraight("ltable", "lclick"))
    edges.append(vstraight("lclick", "ldget"))
    edges.append(vstraight("ldget", "ldrawer"))
    # compare lane
    edges.append(vstraight("rfilter", "rqsel"))
    edges.append(vstraight("rqsel", "rget"))
    edges.append(vstraight("rget", "rcards"))
    # converge into end
    (x1, y1) = A("ldrawer", "bottom"); (ex, ey) = A("end", "top")
    edges.append(edge(f"M{x1:.1f},{y1:.1f} V1254 H{ex:.1f} V{ey:.1f}"))
    (x2, y2) = A("rcards", "bottom")
    edges.append(edge(f"M{x2:.1f},{y2:.1f} V1254 H{ex:.1f} V{ey:.1f}"))
    # side connectors (dashed)
    (sx, sy) = A("run", "right"); (px, py) = A("export", "left")
    edges.append(edge(f"M{sx:.1f},{sy:.1f} H{px:.1f}", color="#8B5CF6", dash=True, wdt=1.7, marker="v"))
    (qx, qy) = A("poll", "left"); (tx, ty) = A("ltable", "right")
    edges.append(edge(f"M{qx:.1f},{qy:.1f} H{tx:.1f}", color="#06B6D4", dash=True, wdt=1.7, marker="c"))

    o += edges

    # fork labels
    o.append(f'<text x="500" y="606" font-family="{FONT}" font-size="11" font-weight="700" fill="#92400E" text-anchor="middle">Review</text>')
    o.append(f'<text x="845" y="606" font-family="{FONT}" font-size="11" font-weight="700" fill="#92400E" text-anchor="middle">Compare Platforms</text>')

    for n in NODES.values():
        o.append(draw_node(n))

    # legend
    leg = [("Start / End", "term"), ("User action", "ui"), ("Render / view", "proc"),
           ("Decision", "dec"), ("Backend API call", "api"), ("Detail panel", "panel"),
           ("Async refresh", "poll")]
    ly = H - 34
    o.append(f'<text x="70" y="{ly-24:.1f}" font-family="{FONT}" font-size="12" font-weight="800" fill="#334155">Legend</text>')
    o.append(f'<text x="{W-70}" y="{ly-24:.1f}" font-family="{FONT}" font-size="10.5" fill="#94A3B8" font-weight="500" text-anchor="end">Deep-linkable via URL: /results?run_id · mode · question_id · alert_only · q</text>')
    lx = 70
    for lab, cat in leg:
        f, s, _ = CAT[cat]
        o.append(f'<rect x="{lx}" y="{ly-12}" width="16" height="14" rx="3" fill="{f}" stroke="{s}" stroke-width="1.6"/>')
        o.append(f'<text x="{lx+22}" y="{ly}" font-family="{FONT}" font-size="11" fill="#475569" font-weight="600">{esc(lab)}</text>')
        lx += 40 + len(lab) * 7.0
    o.append(f'<text x="{lx+6}" y="{ly}" font-family="{FONT}" font-size="11" fill="#94A3B8" font-weight="600">— — dashed: always-available / side action</text>')

    o.append("</svg>")
    return "".join(o)


def main():
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else (
        pathlib.Path(__file__).resolve().parents[1] / "Evidence_Monitoring_Agent_Response_Review_Flow.svg")
    out.write_text(build(), encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
