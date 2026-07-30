"""Horizontal component flowchart of the agentic orchestrator run (left to right).

Mixed shapes (process rect, decision diamond, agent hexagon), a multi-LLM fan-out,
and up/down branch paths off each decision.

    python scripts/generate_orchestrator_flowchart_svg.py [out.svg]
Render with scripts/render_svg_to_png.py.
"""
from __future__ import annotations
import pathlib, sys

FONT = "'Segoe UI', Roboto, Arial, sans-serif"
CY, TOPY, BOTY = 360, 150, 560
X0 = 70
SIZE = {"start": (128, 46), "end": (128, 46), "rect": (152, 56),
        "hex": (156, 62), "diamond": (140, 92)}
FW, FH = 168, 42          # fan-out box
BW, BH = 156, 54          # branch node
GAP = 52
CAT = {  # fill, stroke, text
    "term":  ("#ECFDF5", "#10B981", "#065F46"),
    "proc":  ("#EFF6FF", "#3B82F6", "#1E3A8A"),
    "dec":   ("#FFFBEB", "#F59E0B", "#92400E"),
    "agent": ("#FFF7ED", "#EA580C", "#7C2D12"),
    "llm":   ("#F5F3FF", "#8B5CF6", "#4A1078"),
    "stop":  ("#FEF2F2", "#EF4444", "#991B1B"),
    "rejoin":("#ECFEFF", "#06B6D4", "#155E63"),
    "data":  ("#F1F5F9", "#475569", "#1E293B"),
}

# Each spine item: dict(shape, label, cat, [branch])
# branch: dict(dir=up|down, kind=stop|rejoin, nodes=[(label,cat)...])
SPINE = [
    dict(shape="start", label="POST /runs", cat="term"),
    dict(shape="rect", label="Create Run|(RUNNING)", cat="proc"),
    dict(shape="rect", label="Load targets,|limiter, budget", cat="proc"),
    dict(shape="rect", label="Fetch approved|questions", cat="proc"),
    dict(shape="diamond", label="Dry run?", cat="dec",
         branch=dict(dir="up", kind="stop", nodes=[("Health check|providers", "proc"), ("COMPLETED|(no writes)", "stop")])),
    dict(shape="hex", label="Per question|(concurrent)", cat="agent"),
    dict(shape="hex", label="Classify intent|(triage gate)", cat="agent"),
    dict(shape="rect", label="Persona|routing", cat="proc"),
    dict(shape="diamond", label="Cancel|requested?", cat="dec",
         branch=dict(dir="down", kind="stop", nodes=[("Abort in-flight|CANCELLED", "stop")])),
    dict(shape="fanout", cat="llm", nodes=[
        "Claude · Bedrock", "Nova Pro · Bedrock", "Llama · Bedrock",
        "GPT-4o · OpenAI", "Gemini · Google", "EvidenceMD · clinical"]),
    dict(shape="diamond", label="Truncated?", cat="dec",
         branch=dict(dir="up", kind="rejoin", nodes=[("Retry boosted|tokens", "rejoin")])),
    dict(shape="rect", label="Build response|rows (validate)", cat="proc"),
    dict(shape="hex", label="Chairman|consensus", cat="agent"),
    dict(shape="rect", label="DB commit|(SQLite)", cat="data"),
    dict(shape="diamond", label="Budget|exceeded?", cat="dec",
         branch=dict(dir="up", kind="stop", nodes=[("PAUSED|BUDGET", "stop")])),
    dict(shape="rect", label="Finalize|COMPLETED", cat="proc"),
    dict(shape="rect", label="Score run|(sentiment)", cat="proc"),
    dict(shape="rect", label="Alerts|+ diff", cat="proc"),
    dict(shape="rect", label="Insights|tag_new", cat="proc"),
    dict(shape="rect", label="Snowflake|mirror", cat="data"),
    dict(shape="end", label="Done", cat="term"),
]


def esc(s): return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tlines(cx, cy, label, color, size=10.5, weight="600"):
    parts = label.split("|"); n = len(parts)
    y0 = cy - (n - 1) * 6.5
    return "".join(
        f'<text x="{cx:.1f}" y="{y0 + i*13:.1f}" font-family="{FONT}" font-size="{size}" '
        f'font-weight="{weight}" fill="{color}" text-anchor="middle" dominant-baseline="middle">{esc(p)}</text>'
        for i, p in enumerate(parts))


def shape_svg(cx, cy, shape, label, cat):
    f, s, tc = CAT[cat]
    w, h = SIZE.get(shape, (BW, BH))
    o = ""
    if shape in ("start", "end"):
        o = f'<rect x="{cx-w/2:.1f}" y="{cy-h/2:.1f}" width="{w}" height="{h}" rx="{h/2}" ry="{h/2}" fill="{f}" stroke="{s}" stroke-width="1.8"/>'
    elif shape == "diamond":
        o = f'<polygon points="{cx:.1f},{cy-h/2:.1f} {cx+w/2:.1f},{cy:.1f} {cx:.1f},{cy+h/2:.1f} {cx-w/2:.1f},{cy:.1f}" fill="{f}" stroke="{s}" stroke-width="1.8"/>'
    elif shape == "hex":
        i = 16
        o = f'<polygon points="{cx-w/2+i:.1f},{cy-h/2:.1f} {cx+w/2-i:.1f},{cy-h/2:.1f} {cx+w/2:.1f},{cy:.1f} {cx+w/2-i:.1f},{cy+h/2:.1f} {cx-w/2+i:.1f},{cy+h/2:.1f} {cx-w/2:.1f},{cy:.1f}" fill="{f}" stroke="{s}" stroke-width="1.8"/>'
    else:
        o = f'<rect x="{cx-w/2:.1f}" y="{cy-h/2:.1f}" width="{w}" height="{h}" rx="8" ry="8" fill="{f}" stroke="{s}" stroke-width="1.8" filter="url(#sh)"/>'
    return o + tlines(cx, cy, label, tc)


def conn(d, color="#64748B", dash=False, marker="a"):
    da = ' stroke-dasharray="5 4"' if dash else ""
    return f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.8"{da} marker-end="url(#{marker})"/>'


def build():
    nodes, edges, extras = [], [], []
    x = X0; prev = None

    def hwidth(shape): return SIZE.get(shape, (BW, BH))[0]

    for item in SPINE:
        shape = item["shape"]
        if shape == "fanout":
            busL = x + 26
            bx = busL + 30 + FW / 2
            total = len(item["nodes"]) * FH + (len(item["nodes"]) - 1) * 12
            cys = [CY - total / 2 + FH / 2 + i * (FH + 12) for i in range(len(item["nodes"]))]
            f, s, tc = CAT["llm"]
            busR = bx + FW / 2 + 30
            # left bus in from prev (solid main-flow line)
            if prev:
                px = prev["cx"] + prev["w"] / 2
                edges.append(f'<path d="M{px:.1f},{CY:.1f} H{busL:.1f}" fill="none" stroke="#64748B" stroke-width="1.8"/>')
                if prev.get("shape") == "diamond":
                    extras.append(f'<text x="{px+10:.1f}" y="{CY-7:.1f}" font-family="{FONT}" font-size="9" fill="#64748B" font-weight="700">no</text>')
            for lbl, cy in zip(item["nodes"], cys):
                edges.append(f'<path d="M{busL:.1f},{CY:.1f} V{cy:.1f} H{bx-FW/2:.1f}" fill="none" stroke="#64748B" stroke-width="1.6"/>')
                edges.append(f'<path d="M{bx+FW/2:.1f},{cy:.1f} H{busR:.1f} V{CY:.1f}" fill="none" stroke="#64748B" stroke-width="1.6"/>')
                extras.append(f'<rect x="{bx-FW/2:.1f}" y="{cy-FH/2:.1f}" width="{FW}" height="{FH}" rx="7" fill="{f}" stroke="{s}" stroke-width="1.6" filter="url(#sh)"/>')
                extras.append(tlines(bx, cy, lbl, tc, 10))
            extras.append(f'<text x="{bx:.1f}" y="{CY-total/2-12:.1f}" font-family="{FONT}" font-size="9.5" fill="#8B5CF6" text-anchor="middle" font-weight="700">fan-out · rate-limit + retry</text>')
            prev = {"cx": busR, "cy": CY, "w": 0, "h": 0, "shape": "point"}
            x = busR + GAP
            continue

        w, h = SIZE[shape]
        cx = x + w / 2
        node = {"cx": cx, "cy": CY, "w": w, "h": h, "shape": shape}
        nodes.append((node, item))
        if prev:
            px = prev["cx"] + prev["w"] / 2
            lx = cx - w / 2
            edges.append(conn(f"M{px:.1f},{CY:.1f} H{lx:.1f}"))
            if prev.get("shape") == "diamond":
                extras.append(f'<text x="{px+10:.1f}" y="{CY-7:.1f}" font-family="{FONT}" font-size="9" fill="#64748B" font-weight="700">no</text>')

        reserve = GAP
        if "branch" in item:
            br = item["branch"]; up = br["dir"] == "up"
            laneY = TOPY if up else BOTY
            k = len(br["nodes"])
            span = k * BW + (k - 1) * 26
            start = cx + w / 2 + 40
            bcxs = [start + BW / 2 + i * (BW + 26) for i in range(k)]
            # decision -> first branch (elbow)
            dy = CY - h / 2 if up else CY + h / 2
            edges.append(conn(f"M{cx:.1f},{dy:.1f} V{laneY:.1f} H{bcxs[0]-BW/2:.1f}", color="#94A3B8"))
            extras.append(f'<text x="{cx+8:.1f}" y="{dy + (-8 if up else 14):.1f}" font-family="{FONT}" font-size="9" fill="#64748B" font-weight="700">yes</text>')
            for (lbl, bcat), bx in zip(br["nodes"], bcxs):
                extras.append(shape_svg(bx, laneY, "rect", lbl, bcat))
            for i in range(k - 1):
                edges.append(conn(f"M{bcxs[i]+BW/2:.1f},{laneY:.1f} H{bcxs[i+1]-BW/2:.1f}", color="#94A3B8"))
            reserve = max(GAP, span + 80)
            node["_rejoin"] = (br, bcxs[-1], laneY, up)
        x = cx + w / 2 + reserve
        prev = node

    # second pass: rejoin branches into the following spine node
    for idx, (node, item) in enumerate(nodes):
        if "_rejoin" in node:
            br, lastbx, laneY, up = node["_rejoin"]
            if br["kind"] == "rejoin" and idx + 1 < len(nodes):
                nxt = nodes[idx + 1][0]
                ny = nxt["cy"] - nxt["h"] / 2 if up else nxt["cy"] + nxt["h"] / 2
                edges.append(conn(f"M{lastbx+BW/2:.1f},{laneY:.1f} H{nxt['cx']:.1f} V{ny:.1f}", color="#06B6D4"))

    W = int(x + 40)
    H = 720
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="{FONT}">']
    o.append('<defs><marker id="a" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L8,3 L0,6 Z" fill="#64748B"/></marker>'
             '<filter id="sh" x="-6%" y="-12%" width="112%" height="130%"><feDropShadow dx="0" dy="1" stdDeviation="1.2" flood-color="#0F172A" flood-opacity="0.13"/></filter></defs>')
    o.append(f'<rect width="{W}" height="{H}" fill="#FFFFFF"/>')
    o.append(f'<text x="{W/2:.1f}" y="46" font-family="{FONT}" font-size="26" font-weight="800" fill="#0F172A" text-anchor="middle">Agentic Orchestrator</text>')
    o.append(f'<text x="{W/2:.1f}" y="70" font-family="{FONT}" font-size="13" font-weight="500" fill="#64748B" text-anchor="middle">Evidence Monitoring Agent · backend run flow (left to right)</text>')
    o += edges
    o += extras
    for node, item in nodes:
        o.append(shape_svg(node["cx"], node["cy"], item["shape"], item["label"], item["cat"]))

    # legend
    leg = [("Start / End", "term"), ("Process", "proc"), ("Decision", "dec"),
           ("Agent step", "agent"), ("LLM target", "llm"), ("Persist", "data"),
           ("Stop state", "stop"), ("Rejoin path", "rejoin")]
    lx = 70; ly = H - 28
    o.append(f'<text x="70" y="{ly-22:.1f}" font-family="{FONT}" font-size="12" font-weight="800" fill="#334155">Legend</text>')
    for lab, cat in leg:
        f, s, _ = CAT[cat]
        o.append(f'<rect x="{lx}" y="{ly-12}" width="16" height="14" rx="3" fill="{f}" stroke="{s}" stroke-width="1.6"/>')
        o.append(f'<text x="{lx+22}" y="{ly}" font-family="{FONT}" font-size="11" fill="#475569" font-weight="600">{esc(lab)}</text>')
        lx += 40 + len(lab) * 7.0
    o.append("</svg>")
    return "".join(o)


def main():
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else (
        pathlib.Path(__file__).resolve().parents[1] / "Evidence_Monitoring_Agent_Orchestrator_Flowchart.svg")
    out.write_text(build(), encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
