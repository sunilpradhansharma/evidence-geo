"""Backend flow SVG: what happens in which component during a monitoring run.

    python scripts/generate_backend_flow_svg.py [out.svg]
Render with scripts/render_svg_to_png.py.
"""
from __future__ import annotations
import pathlib, sys

W = 1480
FONT = "'Segoe UI', Roboto, Arial, sans-serif"
CX, CW = 300, 760          # central step column
BX, BW = 1112, 332         # branch callout column
TOP, CH, GAP = 110, 80, 26

GROUPS = {  # component group -> accent color
    "api":  "#2E7D32", "svc": "#558B2F", "eng": "#E65100",
    "prov": "#6A1B9A", "data": "#334155", "post": "#4527A0",
}
LEGEND = [("API router", "api"), ("Service", "svc"), ("Agent / Run Engine", "eng"),
          ("Providers", "prov"), ("Persistence", "data"), ("Post-run", "post")]

STEPS = [
    ("api", "API · runs.py  →  POST /runs", [
        "Create Run (status=RUNNING) and enqueue a background task (202 Accepted).",
        "AppEventsMiddleware mirrors the request to the event log."]),
    ("svc", "Service · run_service.run_in_background", [
        "Opens an async DB session and calls execute_run.",
        "On exception → Run FAILED (already-captured responses preserved)."]),
    ("eng", "Engine · orchestrator.execute_run  (setup)", [
        "Load enabled targets, system prompts, RateLimiter, BudgetGuard (resume-aware).",
        "Fetch APPROVED questions; compute resume pairs; write RUN_START audit."]),
    ("eng", "Engine · intent_classifier.classify_intent  (Triage Gate)", [
        "Classify each question's intent before dispatch (network, off the DB lock).",
        "Questions run concurrently, bounded by a semaphore (NF-003)."]),
    ("eng", "Engine · targets_for_persona  +  _dispatch_targets", [
        "Persona routing picks targets; dispatch all of them concurrently.",
        "Preemptive cancel aborts in-flight calls immediately (NF-005)."]),
    ("prov", "Providers · provider client.chat  (registry + targets.yaml)", [
        "Bedrock (Claude/Nova/Llama), OpenAI GPT-4o, Google Gemini, EvidenceMD (clinical · Provider).",
        "Rate-limit bucket + retry/backoff; truncation → boosted retry; safety → BLOCKED."]),
    ("eng", "Engine · validator.looks_truncated  +  _build_row", [
        "Set per-call status; build immutable Response rows.",
        "Capture text, tokens, sources, grounding, finish_reason."]),
    ("eng", "Engine · chairman.evaluate_consensus  (Claude)", [
        "Arbitrate consensus FULL / PARTIAL / MISSING + synthesized final answer.",
        "Runs off the DB lock; every persona arbitrates inline (Provider adds EvidenceMD)."]),
    ("data", "Persistence · db_lock critical section  →  SQLite", [
        "Commit all targets per question together: responses + consensus + run counters.",
        "budget.add; write append-only LLM_CALL audit; db.commit (FR-204)."]),
    ("eng", "Engine · finalize run status", [
        "COMPLETED, or CANCELLED / PAUSED_BUDGET.",
        "Write RUN_COMPLETE (or branch) audit; clear cancel registration."]),
    ("post", "Post-run · run_service passes  (fresh sessions, best-effort)", [
        "scorer.score_run → ScoringRecord (sentiment + position) + alerts + diff.",
        "insights.tag_new (themes); snowflake.mirror (no-op when disabled)."]),
]

PHASES = [("Request & setup", 0, 2), ("Per-question dispatch", 3, 7),
          ("Persist & finalize", 8, 9), ("Post-run analytics", 10, 10)]

BRANCHES = [  # (step_idx, title, lines)
    (2, "Dry run", ["health_check only,", "no writes → COMPLETED"]),
    (4, "Cancel (NF-005)", ["abort in-flight calls,", "→ CANCELLED,", "partials preserved"]),
    (8, "Budget exceeded", ["→ PAUSED_BUDGET,", "resume later (idempotent)"]),
]


def esc(s): return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
def rr(x, y, w, h, r, f, s, sw=1.4, e=""):
    return f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{r}" ry="{r}" fill="{f}" stroke="{s}" stroke-width="{sw}" {e}/>'
def tx(x, y, s, sz, c, w="normal", a="start"):
    return f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{sz}" font-weight="{w}" fill="{c}" text-anchor="{a}">{esc(s)}</text>'


def build():
    ys = [TOP + i * (CH + GAP) for i in range(len(STEPS))]
    H = ys[-1] + CH + 96
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="{FONT}">']
    o.append('<defs><marker id="a" markerWidth="11" markerHeight="11" refX="8" refY="3.2" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L9,3.2 L0,6.4 Z" fill="#64748B"/></marker>'
             '<marker id="ab" markerWidth="11" markerHeight="11" refX="8" refY="3.2" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L9,3.2 L0,6.4 Z" fill="#D97706"/></marker>'
             '<filter id="sh" x="-3%" y="-8%" width="106%" height="120%"><feDropShadow dx="0" dy="1" stdDeviation="1.3" flood-color="#0F172A" flood-opacity="0.14"/></filter></defs>')
    o.append(rr(0, 0, W, H, 0, "#FFFFFF", "#FFFFFF", 0))
    o.append(tx(CX, 48, "Evidence Monitoring Agent  ·  Backend Flow (a monitoring run)", 25, "#0F172A", "800"))
    o.append(tx(CX, 74, "What happens in which backend component, from API request to scored, mirrored results.", 13, "#64748B", "500"))

    # phase rail
    for label, a, b in PHASES:
        y1, y2 = ys[a] - 2, ys[b] + CH + 2
        o.append(f'<path d="M150,{y1:.1f} H40 V{y2:.1f} H150" fill="none" stroke="#CBD5E1" stroke-width="1.6"/>')
        my = (y1 + y2) / 2
        o.append(f'<text transform="translate(30,{my:.1f}) rotate(-90)" font-family="{FONT}" font-size="13" font-weight="800" fill="#64748B" text-anchor="middle">{esc(label)}</text>')

    cxmid = CX + CW / 2
    # down arrows between steps
    for i in range(len(STEPS) - 1):
        o.append(f'<line x1="{cxmid:.1f}" y1="{ys[i]+CH+3:.1f}" x2="{cxmid:.1f}" y2="{ys[i+1]-4:.1f}" stroke="#64748B" stroke-width="2.4" marker-end="url(#a)"/>')

    # branch callouts (dashed)
    for idx, title, lines in BRANCHES:
        y = ys[idx]
        bh = 30 + len(lines) * 15
        by = y + (CH - bh) / 2
        o.append(f'<line x1="{CX+CW+2:.1f}" y1="{y+CH/2:.1f}" x2="{BX-5:.1f}" y2="{by+bh/2:.1f}" stroke="#D97706" stroke-width="1.8" stroke-dasharray="5 4" marker-end="url(#ab)"/>')
        o.append(rr(BX, by, BW, bh, 9, "#FFFBEB", "#F59E0B", 1.5, 'stroke-dasharray="5 4"'))
        o.append(tx(BX + 14, by + 20, title, 12, "#B45309", "800"))
        for j, ln in enumerate(lines):
            o.append(tx(BX + 14, by + 36 + j * 15, ln, 10.5, "#92400E"))

    # step cards
    for i, ((grp, comp, lines), y) in enumerate(zip(STEPS, ys)):
        col = GROUPS[grp]
        o.append(rr(CX, y, CW, CH, 11, "#FFFFFF", "#E2E8F0", 1.4, 'filter="url(#sh)"'))
        o.append(rr(CX, y, 7, CH, 0, col, col, 0))   # accent
        o.append(f'<circle cx="{CX+34:.1f}" cy="{y+CH/2:.1f}" r="15" fill="{col}"/>')
        o.append(tx(CX + 34, y + CH / 2 + 5, str(i + 1), 14, "#FFFFFF", "800", "middle"))
        o.append(tx(CX + 62, y + 26, comp, 13.5, col, "800"))
        for j, ln in enumerate(lines):
            o.append(tx(CX + 62, y + 45 + j * 16, ln, 11, "#475569"))

    # legend
    ly = ys[-1] + CH + 46
    o.append(tx(CX, ly, "Component groups", 12, "#334155", "800"))
    lx = CX + 160
    for lab, key in LEGEND:
        o.append(rr(lx, ly - 11, 15, 14, 3, GROUPS[key], GROUPS[key], 0))
        o.append(tx(lx + 21, ly, lab, 11, "#475569", "600"))
        lx += 32 + len(lab) * 7.0
    o.append("</svg>")
    return "".join(o)


def main():
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else (
        pathlib.Path(__file__).resolve().parents[1] / "Evidence_Monitoring_Agent_Backend_Flow.svg")
    out.write_text(build(), encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
