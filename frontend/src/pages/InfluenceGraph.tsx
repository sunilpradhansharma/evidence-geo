import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import ForceGraph2D from "react-force-graph-2d";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  Crosshair,
  ExternalLink,
  Eye,
  EyeOff,
  Layers,
  ListPlus,
  Maximize2,
  Minimize2,
  Network,
  Play,
  Quote,
  RotateCcw,
  ScanEye,
  Search,
  Settings2,
  Sparkles,
  Target,
  X,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import {
  api,
  type InfluenceGraph as InfluenceGraphData,
  type InfluenceLink,
  type InfluenceNode,
  type InfluenceNodeType,
  type InfluenceNodeEvidence,
  type SaFilters,
  type SourceDomainCitation,
  type SourceDomainDetail,
  type TaFilters,
} from "../api/client";
import { TaHierarchyFilter, type TaSelection } from "../components/TaHierarchyFilter";
import {
  Card,
  EmptyState,
  InfoTooltip,
  PageHeader,
  POSITION_LABELS,
  PositionBadge,
  Select,
  SentimentBadge,
  Spinner,
} from "../components/ui";

/* ------------------------------------------------------------------ */
/*  Palette — muted by default on a dark canvas. Only meaningful hues   */
/*  stay saturated (AbbVie/competitor sources, brand positions); the    */
/*  high-volume claim + independent nodes recede to quiet neutrals so   */
/*  the web reads calmly at rest and lifts only on hover/selection.     */
/* ------------------------------------------------------------------ */
const CONTROL_NODE_COLOR: Record<string, string> = {
  ABBVIE: "#2DD4BF",
  COMPETITOR: "#F87171",
  INDEPENDENT: "#64748B",
  UNKNOWN: "#475569",
};
const POSITION_NODE_COLOR: Record<string, string> = {
  FIRST_LINE_RECOMMENDED: "#2DD4BF",
  AMONG_OPTIONS: "#60A5FA",
  SECOND_LINE: "#FBBF24",
  NOT_RECOMMENDED: "#F87171",
  NOT_MENTIONED: "#64748B",
};
// Brand-position nodes are coloured by standing (best -> worst), matching the
// app-wide PositionBadge scale. The legend renders this same ordered scale so
// the colours on the canvas are explained rather than implied by one dot.
const POSITION_SCALE: { key: string; label: string }[] = [
  { key: "FIRST_LINE_RECOMMENDED", label: "Leading" },
  { key: "AMONG_OPTIONS", label: "Among options" },
  { key: "SECOND_LINE", label: "Second-line" },
  { key: "NOT_RECOMMENDED", label: "Not endorsed" },
  { key: "NOT_MENTIONED", label: "Not mentioned" },
];
const POSITION_GRADIENT = `linear-gradient(90deg, ${POSITION_SCALE.map((p) => POSITION_NODE_COLOR[p.key]).join(", ")})`;
const TYPE_NODE_COLOR: Record<InfluenceNodeType, string> = {
  source: "#94A3B8",
  claim: "#5A6478",
  theme: "#8B84C4",
  position: "#FBBF24",
};

function nodeColor(n: InfluenceNode): string {
  if (n.type === "source") return CONTROL_NODE_COLOR[n.control_type ?? "UNKNOWN"] ?? "#64748B";
  if (n.type === "position") return POSITION_NODE_COLOR[n.label] ?? "#94A3B8";
  return TYPE_NODE_COLOR[n.type] ?? "#94A3B8";
}

function nodeDisplayLabel(n: InfluenceNode): string {
  if (n.type === "position") return POSITION_LABELS[n.label] ?? n.label;
  return n.display_label?.trim() || n.label;
}

function traceNodePath(ctx: CanvasRenderingContext2D, node: any, r: number) {
  ctx.beginPath();
  if (node.type === "claim") {
    ctx.moveTo(node.x ?? 0, (node.y ?? 0) - r);
    ctx.lineTo((node.x ?? 0) + r, node.y ?? 0);
    ctx.lineTo(node.x ?? 0, (node.y ?? 0) + r);
    ctx.lineTo((node.x ?? 0) - r, node.y ?? 0);
    ctx.closePath();
    return;
  }
  if (node.type === "theme") {
    const x = (node.x ?? 0) - r * 1.35;
    const y = (node.y ?? 0) - r * 0.78;
    ctx.roundRect(x, y, r * 2.7, r * 1.56, r * 0.72);
    return;
  }
  if (node.type === "position") {
    for (let i = 0; i < 6; i += 1) {
      const angle = Math.PI / 6 + (i * Math.PI) / 3;
      const x = (node.x ?? 0) + Math.cos(angle) * r;
      const y = (node.y ?? 0) + Math.sin(angle) * r;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.closePath();
    return;
  }
  ctx.arc(node.x ?? 0, node.y ?? 0, r, 0, Math.PI * 2);
}

function wrapCanvasLabel(
  ctx: CanvasRenderingContext2D,
  text: string,
  maxWidth: number,
  maxLines: number,
): string[] {
  const words = text.trim().split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let line = "";
  while (words.length && lines.length < maxLines) {
    const word = words.shift()!;
    const candidate = line ? `${line} ${word}` : word;
    if (!line || ctx.measureText(candidate).width <= maxWidth) {
      line = candidate;
      continue;
    }
    lines.push(line);
    line = word;
  }
  if (line && lines.length < maxLines) lines.push(line);
  if (words.length && lines.length) {
    let last = lines[lines.length - 1];
    while (last && ctx.measureText(`${last}…`).width > maxWidth) last = last.slice(0, -1).trimEnd();
    lines[lines.length - 1] = `${last}…`;
  }
  return lines;
}

function boxesOverlap(
  a: { left: number; top: number; right: number; bottom: number },
  b: { left: number; top: number; right: number; bottom: number },
): boolean {
  return a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
}

function nodeRadius(n: any, scale = 1): number {
  const w = n.weight || 1;
  // Anchors (narratives + brand positions) stay prominent; claims shrink and
  // grow slowly so the most numerous node type recedes into the background.
  // Degree (link count, Obsidian-style) nudges well-connected nodes larger.
  const base = n.type === "position" ? 5.5 : n.type === "theme" ? 5 : n.type === "source" ? 3 : 1.6;
  const grow = n.type === "claim" ? 0.7 : 1.6;
  const deg = n.__deg || 1;
  const size = base + grow * Math.log2(w + 1) + Math.min(2.6, Math.log2(deg + 1) * 0.65);
  return Math.min(16, size) * scale;
}

/* ------------------------------------------------------------------ */
/*  Force + display controls (Obsidian-style live tuning)              */
/* ------------------------------------------------------------------ */
type ForceSettings = {
  repel: number; // charge strength (magnitude)
  linkDistance: number;
  linkStrength: number;
  centerForce: number; // gravity toward origin
};
type DisplaySettings = {
  labelFade: number; // zoom scale at which source labels reach full opacity
  nodeSize: number; // radius multiplier
  linkThickness: number; // width multiplier
  animate: boolean; // directional particles on the active neighbourhood
};
const DEFAULT_FORCES: ForceSettings = { repel: 260, linkDistance: 42, linkStrength: 0.7, centerForce: 0.06 };
const DEFAULT_DISPLAY: DisplaySettings = { labelFade: 2.2, nodeSize: 1, linkThickness: 1, animate: false };

// Custom center gravity: pulls every node gently toward the origin (0,0). Registered
// as the 'center' force so we get an adjustable-strength pull without importing d3-force.
function makeCenterForce(strength: number) {
  let nodes: any[] = [];
  const force = (alpha: number) => {
    const k = strength * alpha;
    for (const n of nodes) {
      if (n.x != null) n.vx -= n.x * k;
      if (n.y != null) n.vy -= n.y * k;
    }
  };
  force.initialize = (n: any[]) => {
    nodes = n;
  };
  return force;
}

const TYPE_LABEL: Record<InfluenceNodeType, string> = {
  source: "Source",
  claim: "Claim",
  theme: "Narrative",
  position: "Brand position",
};

const CONTROL_LABEL: Record<string, string> = {
  ABBVIE: "AbbVie",
  COMPETITOR: "Competitor",
  INDEPENDENT: "Independent",
  UNKNOWN: "Unknown",
};

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* ------------------------------------------------------------------ */
/*  Small local badges                                                 */
/* ------------------------------------------------------------------ */
function ControlBadge({ control }: { control?: string }) {
  const c = control ?? "UNKNOWN";
  const cls =
    c === "ABBVIE"
      ? "bg-teal-100 text-teal-800"
      : c === "COMPETITOR"
      ? "bg-red-100 text-red-700"
      : "bg-slate-100 text-ink-light";
  return <span className={`rounded-full px-2 py-0.5 text-[10px] font-bold ${cls}`}>{CONTROL_LABEL[c] ?? c}</span>;
}

function TypeChip({ type, color }: { type: InfluenceNodeType; color?: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-ink-light">
      <span className="h-2 w-2 rounded-full" style={{ background: color ?? TYPE_NODE_COLOR[type] }} />
      {TYPE_LABEL[type]}
    </span>
  );
}

/* ------------------------------------------------------------------ */
/*  Canvas overlay primitives (glassy, dark-canvas friendly)           */
/* ------------------------------------------------------------------ */
function GlassButton({
  onClick,
  title,
  active,
  children,
}: {
  onClick: () => void;
  title: string;
  active?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      className={`pointer-events-auto inline-flex items-center gap-1 rounded-lg border px-2 py-1.5 text-[11px] font-bold backdrop-blur transition-colors ${
        active
          ? "border-brand-light/60 bg-brand-light/25 text-white"
          : "border-white/15 bg-white/10 text-white/90 hover:bg-white/20"
      }`}
    >
      {children}
    </button>
  );
}

function Slider({
  label,
  value,
  min,
  max,
  step,
  format,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  format?: (v: number) => string;
  onChange: (v: number) => void;
}) {
  return (
    <label className="block">
      <div className="mb-1 flex items-center justify-between">
        <span className="text-[11px] font-semibold text-white/80">{label}</span>
        <span className="tabular-nums text-[10px] font-bold text-white/55">{format ? format(value) : value}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="ema-graph-slider h-1 w-full cursor-pointer appearance-none rounded-full bg-white/15 accent-brand-light"
      />
    </label>
  );
}

function ToggleChip({
  on,
  onClick,
  color,
  label,
  title,
}: {
  on: boolean;
  onClick: () => void;
  color: string;
  label: string;
  title?: string;
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={on}
      title={title ?? (on ? `Hide ${label}` : `Show ${label}`)}
      className={`pointer-events-auto inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-semibold backdrop-blur transition-colors ${
        on ? "border-white/20 bg-white/10 text-white/90" : "border-white/10 bg-transparent text-white/35"
      }`}
    >
      <span
        className="h-2.5 w-2.5 rounded-full transition-opacity"
        style={{ background: color, opacity: on ? 1 : 0.3 }}
      />
      {label}
      {on ? <Eye size={11} className="opacity-60" /> : <EyeOff size={11} className="opacity-60" />}
    </button>
  );
}

/* ------------------------------------------------------------------ */
/*  Container size hook                                                 */
/* ------------------------------------------------------------------ */
function useSize<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  useEffect(() => {
    if (!ref.current) return;
    const el = ref.current;
    const ro = new ResizeObserver((entries) => {
      const cr = entries[0].contentRect;
      setSize({ width: Math.floor(cr.width), height: Math.floor(cr.height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return { ref, size };
}

/* ------------------------------------------------------------------ */
/*  Simple view — collapse the claim layer so the web reads as          */
/*  source -> narrative -> brand position (far fewer nodes/links).      */
/* ------------------------------------------------------------------ */
function collapseClaims(data: InfluenceGraphData): InfluenceGraphData {
  const claimIds = new Set(data.nodes.filter((n) => n.type === "claim").map((n) => n.id));
  if (!claimIds.size) return data;

  const inbound = new Map<string, { id: string; value: number }[]>(); // claim <- sources
  const outbound = new Map<string, { id: string; value: number }[]>(); // claim -> narratives
  const passthrough: InfluenceLink[] = []; // narrative -> position (kept as-is)
  const push = (
    m: Map<string, { id: string; value: number }[]>,
    k: string,
    v: { id: string; value: number },
  ) => {
    const arr = m.get(k);
    if (arr) arr.push(v);
    else m.set(k, [v]);
  };
  for (const l of data.links) {
    if (claimIds.has(l.target)) push(inbound, l.target, { id: l.source, value: l.value });
    else if (claimIds.has(l.source)) push(outbound, l.source, { id: l.target, value: l.value });
    else passthrough.push(l);
  }

  // Bridge each source directly to the narratives its claims feed, summing weights.
  const agg = new Map<string, Map<string, number>>();
  claimIds.forEach((claim) => {
    const srcs = inbound.get(claim) ?? [];
    const thms = outbound.get(claim) ?? [];
    for (const s of srcs) {
      let m = agg.get(s.id);
      if (!m) agg.set(s.id, (m = new Map()));
      for (const t of thms) m.set(t.id, (m.get(t.id) ?? 0) + s.value);
    }
  });

  const collapsed: InfluenceLink[] = [];
  agg.forEach((m, source) => m.forEach((value, target) => collapsed.push({ source, target, value })));

  const links = [...collapsed, ...passthrough];
  const connected = new Set<string>();
  links.forEach((l) => {
    connected.add(l.source);
    connected.add(l.target);
  });
  // Drop claim nodes, plus any source orphaned by the collapse.
  const nodes = data.nodes.filter(
    (n) => n.type !== "claim" && (n.type !== "source" || connected.has(n.id)),
  );
  return { ...data, nodes, links };
}

/* ------------------------------------------------------------------ */
/*  InfluenceWeb — the force-directed canvas                           */
/* ------------------------------------------------------------------ */
function InfluenceWeb({
  data,
  selectedId,
  onSelect,
  fs,
  onToggleFullscreen,
}: {
  data: InfluenceGraphData;
  selectedId: string | null;
  onSelect: (n: InfluenceNode | null) => void;
  fs: boolean;
  onToggleFullscreen: () => void;
}) {
  const FG = ForceGraph2D as any; // third-party viz: skip strict JSX prop-typing
  const fgRef = useRef<any>(null);
  const { ref, size } = useSize<HTMLDivElement>();
  const [hover, setHover] = useState<string | null>(null);
  const [simple, setSimple] = useState(true);
  const [search, setSearch] = useState("");
  const [groups, setGroups] = useState<Record<InfluenceNodeType, boolean>>({
    source: true,
    claim: true,
    theme: true,
    position: true,
  });
  const [ctrls, setCtrls] = useState<Record<string, boolean>>({
    ABBVIE: true,
    COMPETITOR: true,
    INDEPENDENT: true,
    UNKNOWN: true,
  });
  const [forces, setForces] = useState<ForceSettings>(DEFAULT_FORCES);
  const [display, setDisplay] = useState<DisplaySettings>(DEFAULT_DISPLAY);
  const [showSettings, setShowSettings] = useState(false);
  const fitted = useRef(false);
  // Cache live node positions so a group/control toggle doesn't teleport the web.
  const posRef = useRef<Map<string, { x: number; y: number; vx: number; vy: number }>>(new Map());

  // "Simple" collapses the claim layer to source -> narrative -> position.
  const view = useMemo(() => (simple ? collapseClaims(data) : data), [data, simple]);

  // Structural filter: node-group + source-control toggles drop nodes and their links.
  const filtered = useMemo(() => {
    const keep = new Set<string>();
    for (const n of view.nodes) {
      if (!groups[n.type]) continue;
      if (n.type === "source" && !ctrls[n.control_type ?? "UNKNOWN"]) continue;
      keep.add(n.id);
    }
    const links = view.links.filter((l) => keep.has(l.source) && keep.has(l.target));
    const connected = new Set<string>();
    links.forEach((l) => {
      connected.add(l.source);
      connected.add(l.target);
    });
    const nodes = view.nodes.filter((n) => keep.has(n.id) && connected.has(n.id));
    return { nodes, links };
  }, [view, groups, ctrls]);

  useEffect(() => {
    if (selectedId && !filtered.nodes.some((node) => node.id === selectedId)) onSelect(null);
  }, [filtered.nodes, onSelect, selectedId]);

  const groupCounts = useMemo(() => {
    const counts: Record<InfluenceNodeType, number> = { source: 0, claim: 0, theme: 0, position: 0 };
    view.nodes.forEach((n) => { counts[n.type] += 1; });
    return counts;
  }, [view]);
  const controlCounts = useMemo(() => {
    const counts: Record<string, number> = { ABBVIE: 0, COMPETITOR: 0, INDEPENDENT: 0, UNKNOWN: 0 };
    view.nodes.forEach((n) => {
      if (n.type === "source") counts[n.control_type ?? "UNKNOWN"] = (counts[n.control_type ?? "UNKNOWN"] ?? 0) + 1;
    });
    return counts;
  }, [view]);

  // Degree (link count) per node — feeds Obsidian-style "bigger when connected" sizing.
  const degree = useMemo(() => {
    const d = new Map<string, number>();
    filtered.links.forEach((l) => {
      d.set(l.source, (d.get(l.source) ?? 0) + 1);
      d.set(l.target, (d.get(l.target) ?? 0) + 1);
    });
    return d;
  }, [filtered]);

  // Fresh object copies (react-force-graph mutates in place); seed positions from
  // the cache so a filter toggle keeps the layout stable instead of resetting it.
  const graphData = useMemo(() => {
    const prev = posRef.current;
    return {
      nodes: filtered.nodes.map((n) => {
        const p = prev.get(n.id);
        return { ...n, __deg: degree.get(n.id) ?? 0, ...(p ? { x: p.x, y: p.y, vx: p.vx, vy: p.vy } : {}) };
      }),
      links: filtered.links.map((l, i) => ({ ...l, __i: i })),
    };
  }, [filtered, degree]);

  const adj = useMemo(() => {
    const incoming = new Map<string, { id: string; link: number }[]>();
    const outgoing = new Map<string, { id: string; link: number }[]>();
    const add = (m: Map<string, { id: string; link: number }[]>, k: string, value: { id: string; link: number }) => {
      const items = m.get(k);
      if (items) items.push(value);
      else m.set(k, [value]);
    };
    filtered.links.forEach((l, i) => {
      add(outgoing, l.source, { id: l.target, link: i });
      add(incoming, l.target, { id: l.source, link: i });
    });
    return { incoming, outgoing };
  }, [filtered]);

  const active = hover ?? selectedId;
  const { hiNodes, hiLinks } = useMemo(() => {
    if (!active) return { hiNodes: new Set<string>(), hiLinks: new Set<number>() };
    const nodes = new Set<string>([active]);
    const links = new Set<number>();
    const walk = (edges: Map<string, { id: string; link: number }[]>) => {
      const seen = new Set<string>([active]);
      const stack = [active];
      while (stack.length) {
        const id = stack.pop()!;
        for (const edge of edges.get(id) ?? []) {
          links.add(edge.link);
          nodes.add(edge.id);
          if (!seen.has(edge.id)) {
            seen.add(edge.id);
            stack.push(edge.id);
          }
        }
      }
    };
    walk(adj.incoming);
    walk(adj.outgoing);
    return { hiNodes: nodes, hiLinks: links };
  }, [active, adj]);

  // Search matches (highlight overlay only — does NOT re-run the layout).
  const searchResults = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return [];
    const rank: Record<InfluenceNodeType, number> = { theme: 0, position: 1, source: 2, claim: 3 };
    return filtered.nodes
      .filter((n) => `${nodeDisplayLabel(n)} ${n.label} ${n.authority_domain ?? ""} ${n.text ?? ""}`.toLowerCase().includes(q))
      .sort((a, b) => {
        const al = nodeDisplayLabel(a).toLowerCase();
        const bl = nodeDisplayLabel(b).toLowerCase();
        const exact = Number(bl === q) - Number(al === q);
        const starts = Number(bl.startsWith(q)) - Number(al.startsWith(q));
        return exact || starts || rank[a.type] - rank[b.type] || b.weight - a.weight || al.localeCompare(bl);
      });
  }, [search, filtered]);
  const searchIds = useMemo(() => new Set(searchResults.map((n) => n.id)), [searchResults]);
  const [searchIndex, setSearchIndex] = useState(0);
  const [searchOpen, setSearchOpen] = useState(false);
  const prefersReducedMotion = useMemo(
    () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false,
    [],
  );

  useEffect(() => {
    setSearchIndex(0);
  }, [search]);

  useEffect(() => {
    fitted.current = false;
  }, [graphData]);

  useEffect(() => {
    if (!fitted.current || !size.width || !size.height) return;
    const timer = window.setTimeout(() => {
      fgRef.current?.zoomToFit(prefersReducedMotion ? 0 : 350, fs ? 90 : 60);
    }, prefersReducedMotion ? 0 : 340);
    return () => window.clearTimeout(timer);
  }, [fs, prefersReducedMotion, size.height, size.width]);

  // Live-tune the d3-force engine from the Obsidian-style sliders + reheat on change.
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;
    const charge = fg.d3Force("charge");
    if (charge) charge.strength(-forces.repel);
    const link = fg.d3Force("link");
    if (link) {
      link.distance(forces.linkDistance);
      link.strength(forces.linkStrength);
    }
    fg.d3Force("center", makeCenterForce(forces.centerForce));
    fg.d3ReheatSimulation();
  }, [forces, graphData]);

  // Unpin every dragged node and re-run the simulation from a warm start.
  const replay = useCallback(() => {
    graphData.nodes.forEach((n: any) => {
      n.fx = undefined;
      n.fy = undefined;
    });
    posRef.current.clear();
    fitted.current = false;
    fgRef.current?.d3ReheatSimulation();
  }, [graphData]);

  const focusNode = useCallback((node: InfluenceNode) => {
    onSelect(node);
    setSearch("");
    setSearchOpen(false);
    const live = graphData.nodes.find((n) => n.id === node.id) as any;
    if (live?.x == null || live?.y == null) return;
    fgRef.current?.centerAt(live.x, live.y, prefersReducedMotion ? 0 : 500);
    fgRef.current?.zoom(Math.max(fgRef.current?.zoom?.() ?? 1, 2.2), prefersReducedMotion ? 0 : 500);
  }, [graphData, onSelect, prefersReducedMotion]);

  const focusSearch = useCallback(() => {
    const match = searchResults[searchIndex] ?? searchResults[0];
    if (match) focusNode(match);
  }, [focusNode, searchIndex, searchResults]);

  // Target opacity for a node given the current hover / search state.
  const nodeTargetOp = useCallback(
    (id: string, type: InfluenceNodeType) => {
      const anyActive = hiNodes.size > 0;
      const searching = searchIds.size > 0;
      if (searching && hover == null) return searchIds.has(id) ? 1 : 0.055;
      if (anyActive) return hiNodes.has(id) ? 1 : 0.055;
      return type === "claim" ? 0.36 : 0.94;
    },
    [hiNodes, hover, searchIds],
  );

  const paintNode = useCallback(
    (node: any, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const r = nodeRadius(node, display.nodeSize);
      const color = nodeColor(node);
      const isHi = hiNodes.has(node.id);
      const inSearch = searchIds.has(node.id);
      // Ease current opacity toward its target for a smooth fade in/out.
      const target = nodeTargetOp(node.id, node.type);
      node.__op = node.__op == null ? target : node.__op + (target - node.__op) * 0.18;
      const op = node.__op;
      ctx.save();
      ctx.globalAlpha = op;
      // Soft halo eases in only for the lifted neighbourhood / search hits.
      const wantGlow = (isHi && hiNodes.size > 0) || (inSearch && searchIds.size > 0);
      node.__glow = node.__glow == null ? 0 : node.__glow + ((wantGlow ? 11 : 0) - node.__glow) * 0.18;
      if (node.__glow > 0.4) {
        ctx.shadowColor = color;
        ctx.shadowBlur = node.__glow;
      }
      traceNodePath(ctx, node, r);
      ctx.fillStyle = node.type === "source" ? "#172033" : color;
      ctx.fill();
      ctx.shadowBlur = 0;
      ctx.lineWidth = (node.type === "source" ? 2 : 1.1) / globalScale;
      ctx.strokeStyle = node.type === "claim" ? "rgba(203,213,225,0.72)" : color;
      ctx.stroke();
      if (node.type === "source") {
        ctx.beginPath();
        ctx.arc(node.x, node.y, Math.max(1.6 / globalScale, r * 0.3), 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
      } else if (node.type === "position") {
        ctx.beginPath();
        ctx.arc(node.x, node.y, Math.max(1.4 / globalScale, r * 0.27), 0, Math.PI * 2);
        ctx.fillStyle = "rgba(11,17,32,0.82)";
        ctx.fill();
      }
      if (selectedId === node.id || inSearch) {
        traceNodePath(ctx, node, r + (selectedId === node.id ? 3.5 : 2.5) / globalScale);
        ctx.lineWidth = (selectedId === node.id ? 2 : 1.25) / globalScale;
        ctx.strokeStyle = selectedId === node.id ? "rgba(255,255,255,0.96)" : "rgba(251,191,36,0.9)";
        ctx.stroke();
      }
      ctx.restore();
    },
    [hiNodes, searchIds, selectedId, display.nodeSize, nodeTargetOp],
  );

  const paintLabels = useCallback(
    (ctx: CanvasRenderingContext2D, globalScale: number) => {
      const occupied: { left: number; top: number; right: number; bottom: number }[] = [];
      const currentSearch = searchResults[searchIndex]?.id;
      const candidates = (graphData.nodes as any[])
        .filter((node) => node.x != null && node.y != null)
        .map((node) => {
          const isCurrent = node.id === active || node.id === currentSearch;
          const isHi = hiNodes.has(node.id);
          const inSearch = searchIds.has(node.id);
          const anchor = node.type === "theme" || node.type === "position";
          let visible = anchor || isCurrent || isHi || inSearch;
          if (node.type === "source") visible ||= globalScale >= display.labelFade - 0.55 || node.weight >= 20;
          if (node.type === "claim") visible ||= globalScale >= display.labelFade + 1.15;
          const priority = isCurrent ? 1000 : inSearch ? 900 : isHi ? 800 : node.type === "position" ? 700 : node.type === "theme" ? 650 : node.type === "source" ? 300 + node.weight : node.weight;
          return { node, visible, priority, isCurrent, isHi, inSearch, anchor };
        })
        .filter((item) => item.visible && (!hiNodes.size || searchIds.size > 0 || item.isHi || item.isCurrent))
        .sort((a, b) => b.priority - a.priority || nodeDisplayLabel(a.node).localeCompare(nodeDisplayLabel(b.node)));

      for (const item of candidates) {
        const { node, isCurrent, anchor } = item;
        const screenFont = anchor ? 13 : 12;
        const fontSize = screenFont / globalScale;
        const lineHeight = (anchor ? 16 : 15) / globalScale;
        const maxWidth = (node.type === "claim" ? 230 : node.type === "theme" ? 190 : 160) / globalScale;
        ctx.font = `${anchor || isCurrent ? "700" : "600"} ${fontSize}px "DM Sans", system-ui, sans-serif`;
        const lines = wrapCanvasLabel(ctx, nodeDisplayLabel(node), maxWidth, node.type === "claim" && isCurrent ? 3 : 2);
        if (!lines.length) continue;
        const textWidth = Math.max(...lines.map((line) => ctx.measureText(line).width));
        const padX = 6 / globalScale;
        const padY = 4 / globalScale;
        const r = nodeRadius(node, display.nodeSize);
        const width = textWidth + padX * 2;
        const height = lines.length * lineHeight + padY * 2;
        const left = node.x - width / 2;
        const top = node.y + r + 4 / globalScale;
        const box = { left, top, right: left + width, bottom: top + height };
        if (!isCurrent && occupied.some((other) => boxesOverlap(box, other))) continue;
        occupied.push(box);
        ctx.save();
        ctx.globalAlpha = Math.max(0.18, node.__op ?? 1);
        ctx.beginPath();
        ctx.roundRect(left, top, width, height, 5 / globalScale);
        ctx.fillStyle = isCurrent ? "rgba(15,23,42,0.96)" : "rgba(2,6,23,0.82)";
        ctx.fill();
        ctx.lineWidth = 0.8 / globalScale;
        ctx.strokeStyle = isCurrent ? nodeColor(node) : "rgba(148,163,184,0.18)";
        ctx.stroke();
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillStyle = anchor || isCurrent ? "rgba(241,245,249,0.98)" : "rgba(203,213,225,0.94)";
        lines.forEach((line, index) => {
          ctx.fillText(line, node.x, top + padY + lineHeight * (index + 0.5));
        });
        ctx.restore();
      }
    },
    [active, display.labelFade, display.nodeSize, graphData.nodes, hiNodes, searchIds, searchIndex, searchResults],
  );

  const paintPointerArea = useCallback(
    (node: any, color: string, ctx: CanvasRenderingContext2D) => {
      ctx.fillStyle = color;
      traceNodePath(ctx, node, nodeRadius(node, display.nodeSize) + 3);
      ctx.fill();
    },
    [display.nodeSize],
  );

  const linkColor = useCallback(
    (l: any) => {
      const activeLink = hiLinks.has(l.__i);
      const anyHi = hiLinks.size > 0;
      const target = anyHi ? (activeLink ? 0.78 : 0.018) : 0.11;
      l.__op = l.__op == null ? target : l.__op + (target - l.__op) * 0.2;
      if (activeLink) return `rgba(45,212,191,${l.__op})`;
      const targetId = typeof l.target === "object" ? l.target.id : l.target;
      const rgb = String(targetId).startsWith("pos:") ? "251,191,36" : String(targetId).startsWith("theme:") ? "139,132,196" : "100,116,139";
      return `rgba(${rgb},${l.__op})`;
    },
    [hiLinks],
  );
  const linkWidth = useCallback(
    (l: any) => {
      const weight = 0.35 + Math.min(1.1, Math.log2((l.value ?? 1) + 1) * 0.2);
      return (hiLinks.has(l.__i) ? weight + 1.1 : weight) * display.linkThickness;
    },
    [hiLinks, display.linkThickness],
  );
  const linkParticles = useCallback(
    (l: any) => (display.animate && !prefersReducedMotion && hiLinks.has(l.__i) ? 2 : 0),
    [display.animate, hiLinks, prefersReducedMotion],
  );

  const nodeLabel = useCallback((n: any) => {
    const bits = [
      '<div style="font-weight:700;margin-bottom:3px">' + escapeHtml(nodeDisplayLabel(n)) + "</div>",
      '<div style="opacity:.75">' +
        TYPE_LABEL[n.type as InfluenceNodeType] +
        (n.type === "source" && n.control_type ? " · " + (CONTROL_LABEL[n.control_type] ?? n.control_type) : "") +
        "</div>",
      '<div style="opacity:.6;margin-top:3px">' + n.weight + (n.weight === 1 ? " grounded answer" : " grounded answers") + "</div>",
    ];
    return (
      '<div style="background:#0f172a;color:#fff;padding:9px 11px;border-radius:10px;font-size:12px;max-width:300px;' +
      'font-family:DM Sans,system-ui,sans-serif;box-shadow:0 10px 30px rgba(0,0,0,.48);border:1px solid rgba(148,163,184,.25)">' +
      bits.join("") +
      "</div>"
    );
  }, []);

  const nodeGroups: InfluenceNodeType[] = simple ? ["source", "theme", "position"] : ["source", "theme", "position", "claim"];
  const controlKeys = ["ABBVIE", "COMPETITOR", "INDEPENDENT", "UNKNOWN"];

  return (
    <div ref={ref} className="ema-influence-canvas relative h-full w-full overflow-hidden bg-[#0b1120]">
      {/* Top-left: search + view toggle + tune */}
      <div className="pointer-events-none absolute left-3 top-3 z-20 flex max-h-[calc(100%-24px)] w-[calc(100%-3.5rem)] flex-col gap-2 sm:w-[min(21rem,calc(100%-1.5rem))]">
        <div className="pointer-events-auto relative">
          <div className="flex items-center gap-1.5 rounded-xl border border-white/15 bg-slate-950/70 px-2.5 py-2 shadow-lg backdrop-blur-md">
            <Search size={14} className="shrink-0 text-white/60" />
            <input
              value={search}
              onFocus={() => setSearchOpen(true)}
              onChange={(e) => { setSearch(e.target.value); setSearchOpen(true); }}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  setSearchIndex((i) => Math.min(7, Math.max(0, searchResults.length - 1), i + 1));
                } else if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setSearchIndex((i) => Math.max(0, i - 1));
                } else if (e.key === "Enter") {
                  e.preventDefault();
                  focusSearch();
                } else if (e.key === "Escape") {
                  setSearch("");
                  setSearchOpen(false);
                }
              }}
              placeholder="Search sources, claims, narratives…"
              aria-label="Search graph nodes"
              aria-expanded={searchOpen && !!search}
              className="min-w-0 flex-1 bg-transparent text-[12px] font-medium text-white placeholder:text-white/40 focus:outline-none"
            />
            {search && (
              <span className="shrink-0 text-[10px] font-bold tabular-nums text-white/45">{searchResults.length}</span>
            )}
            {search && (
              <button onClick={() => { setSearch(""); setSearchOpen(false); }} title="Clear search" className="text-white/50 hover:text-white">
                <X size={13} />
              </button>
            )}
          </div>
          {searchOpen && search && (
            <div className="absolute left-0 right-0 top-[calc(100%+6px)] max-h-72 overflow-y-auto rounded-xl border border-white/15 bg-slate-950/95 p-1.5 shadow-2xl backdrop-blur-md">
              {searchResults.length ? searchResults.slice(0, 8).map((node, index) => (
                <button
                  key={node.id}
                  type="button"
                  onMouseEnter={() => setSearchIndex(index)}
                  onClick={() => focusNode(node)}
                  className={`flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left transition-colors ${index === searchIndex ? "bg-white/[0.12]" : "hover:bg-white/[0.08]"}`}
                >
                  <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: nodeColor(node) }} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[12px] font-semibold text-white">{nodeDisplayLabel(node)}</span>
                    <span className="block text-[10px] font-medium text-white/45">{TYPE_LABEL[node.type]}</span>
                  </span>
                  <span className="shrink-0 text-[10px] font-bold tabular-nums text-white/45">{node.weight}</span>
                </button>
              )) : (
                <div className="px-3 py-4 text-center text-[11px] font-medium text-white/50">No matching nodes</div>
              )}
              {searchResults.length > 8 && (
                <div className="px-2.5 py-1.5 text-[10px] font-medium text-white/40">Showing 8 of {searchResults.length} · use ↑ ↓ Enter</div>
              )}
            </div>
          )}
        </div>
        {(active || search) && (
          <div className="pointer-events-auto flex w-fit max-w-full items-center gap-1.5 rounded-lg border border-white/10 bg-slate-950/65 px-2.5 py-1 text-[10px] font-semibold text-white/65 backdrop-blur">
            {search ? `${searchResults.length} match${searchResults.length === 1 ? "" : "es"}` : `Tracing ${hiNodes.size} node${hiNodes.size === 1 ? "" : "s"}`}
            <button
              type="button"
              onClick={() => { setSearch(""); setSearchOpen(false); setHover(null); onSelect(null); }}
              className="text-white/45 hover:text-white"
              title="Clear trace"
            >
              <X size={11} />
            </button>
          </div>
        )}
        <div className="pointer-events-auto flex items-center gap-1.5">
          <div className="flex items-center gap-0.5 rounded-lg border border-white/15 bg-white/10 p-0.5 backdrop-blur">
            {([
              ["simple", "Simple"],
              ["detailed", "Detailed"],
            ] as const).map(([key, label]) => {
              const on = (key === "simple") === simple;
              return (
                <button
                  key={key}
                  onClick={() => setSimple(key === "simple")}
                  className={`rounded-md px-2.5 py-1 text-[11px] font-bold transition-colors ${
                    on ? "bg-white/85 text-slate-900" : "text-white/70 hover:text-white"
                  }`}
                  title={
                    key === "simple"
                      ? "Collapse the claim layer — source to narrative to brand position"
                      : "Show every claim between sources and narratives"
                  }
                >
                  {label}
                </button>
              );
            })}
          </div>
          <GlassButton onClick={() => setShowSettings((s) => !s)} title="Tune forces & display" active={showSettings}>
            <Settings2 size={13} /> Tune
          </GlassButton>
        </div>
        {showSettings && (
          <div className="pointer-events-auto w-60 overflow-y-auto rounded-xl border border-white/15 bg-slate-900/85 p-3 shadow-xl backdrop-blur-md">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-[10px] font-bold uppercase tracking-widest text-white/60">Forces</span>
              <button onClick={() => setForces(DEFAULT_FORCES)} className="text-[10px] font-bold text-brand-light hover:text-white">
                Reset
              </button>
            </div>
            <div className="space-y-2.5">
              <Slider label="Center force" value={forces.centerForce} min={0} max={0.3} step={0.01} format={(v) => v.toFixed(2)} onChange={(v) => setForces((f) => ({ ...f, centerForce: v }))} />
              <Slider label="Repel force" value={forces.repel} min={40} max={600} step={10} onChange={(v) => setForces((f) => ({ ...f, repel: v }))} />
              <Slider label="Link force" value={forces.linkStrength} min={0} max={2} step={0.05} format={(v) => v.toFixed(2)} onChange={(v) => setForces((f) => ({ ...f, linkStrength: v }))} />
              <Slider label="Link distance" value={forces.linkDistance} min={10} max={140} step={2} onChange={(v) => setForces((f) => ({ ...f, linkDistance: v }))} />
            </div>
            <div className="my-2.5 border-t border-white/10" />
            <div className="mb-2 flex items-center justify-between">
              <span className="text-[10px] font-bold uppercase tracking-widest text-white/60">Display</span>
              <button onClick={() => setDisplay(DEFAULT_DISPLAY)} className="text-[10px] font-bold text-brand-light hover:text-white">
                Reset
              </button>
            </div>
            <div className="space-y-2.5">
              <Slider label="Label fade" value={display.labelFade} min={0.8} max={4} step={0.1} format={(v) => v.toFixed(1) + "×"} onChange={(v) => setDisplay((d) => ({ ...d, labelFade: v }))} />
              <Slider label="Node size" value={display.nodeSize} min={0.5} max={2} step={0.05} format={(v) => v.toFixed(2) + "×"} onChange={(v) => setDisplay((d) => ({ ...d, nodeSize: v }))} />
              <Slider label="Link thickness" value={display.linkThickness} min={0.5} max={3} step={0.1} format={(v) => v.toFixed(1) + "×"} onChange={(v) => setDisplay((d) => ({ ...d, linkThickness: v }))} />
            </div>
            <button
              onClick={() => setDisplay((d) => ({ ...d, animate: !d.animate }))}
              className="mt-2.5 flex w-full items-center justify-between rounded-lg border border-white/10 bg-white/5 px-2.5 py-1.5 text-[11px] font-semibold text-white/80 transition-colors hover:bg-white/10"
            >
              <span className="flex items-center gap-1.5">
                <Play size={11} /> Animate flow
              </span>
              <span className={`rounded-full px-1.5 py-0.5 text-[9px] font-bold ${display.animate ? "bg-brand-light/30 text-white" : "bg-white/10 text-white/50"}`}>
                {display.animate ? "On" : "Off"}
              </span>
            </button>
            <button
              onClick={replay}
              className="mt-2 flex w-full items-center justify-center gap-1.5 rounded-lg border border-white/10 bg-white/5 px-2.5 py-1.5 text-[11px] font-bold text-white/80 transition-colors hover:bg-white/10"
            >
              <RotateCcw size={11} /> Replay layout
            </button>
          </div>
        )}
      </div>

      {/* Top-right: fit + fullscreen */}
      <div className="pointer-events-none absolute right-3 top-3 z-20 flex flex-col gap-1.5 sm:flex-row">
        <GlassButton onClick={() => fgRef.current?.zoom((fgRef.current?.zoom?.() ?? 1) * 1.35, prefersReducedMotion ? 0 : 250)} title="Zoom in">
          <ZoomIn size={13} />
        </GlassButton>
        <GlassButton onClick={() => fgRef.current?.zoom((fgRef.current?.zoom?.() ?? 1) / 1.35, prefersReducedMotion ? 0 : 250)} title="Zoom out">
          <ZoomOut size={13} />
        </GlassButton>
        <GlassButton onClick={() => fgRef.current?.zoomToFit(prefersReducedMotion ? 0 : 500, fs ? 90 : 60)} title="Fit graph to view">
          <Crosshair size={13} /> Fit
        </GlassButton>
        <GlassButton onClick={onToggleFullscreen} title={fs ? "Exit fullscreen" : "Fullscreen"}>
          {fs ? <Minimize2 size={13} /> : <Maximize2 size={13} />}
        </GlassButton>
      </div>

      {/* Bottom-left: legend doubles as show/hide filters */}
      <div className="pointer-events-none absolute bottom-3 left-3 z-20 flex max-w-[calc(100%-24px)] flex-col gap-1.5">
        <div className="flex flex-wrap gap-1.5">
          {nodeGroups.map((t) => (
            <ToggleChip
              key={t}
              on={groups[t]}
              color={t === "position" ? POSITION_GRADIENT : TYPE_NODE_COLOR[t]}
              label={`${TYPE_LABEL[t]} ${groupCounts[t]}`}
              onClick={() => setGroups((g) => ({ ...g, [t]: !g[t] }))}
            />
          ))}
        </div>
        <div className="flex flex-wrap gap-1.5">
          {controlKeys.map((c) => (
            <ToggleChip key={c} on={ctrls[c]} color={CONTROL_NODE_COLOR[c]} label={`${CONTROL_LABEL[c]} ${controlCounts[c] ?? 0}`} onClick={() => setCtrls((s) => ({ ...s, [c]: !s[c] }))} />
          ))}
        </div>
        {groups.position && (
          <div className="pointer-events-auto flex flex-wrap items-center gap-x-2.5 gap-y-1 self-start rounded-lg border border-white/15 bg-white/10 px-2.5 py-1.5 backdrop-blur">
            <span className="text-[9px] font-bold uppercase tracking-wider text-white/50">Brand position</span>
            {POSITION_SCALE.map((p) => (
              <span key={p.key} className="inline-flex items-center gap-1 text-[10px] font-medium text-white/80">
                <span className="h-2 w-2 rounded-full" style={{ background: POSITION_NODE_COLOR[p.key] }} />
                {p.label}
              </span>
            ))}
          </div>
        )}
      </div>

      {size.width > 0 && (
        <FG
          ref={fgRef}
          width={size.width}
          height={size.height}
          graphData={graphData as any}
          backgroundColor="rgba(0,0,0,0)"
          nodeId="id"
          nodeRelSize={4}
          nodeLabel={nodeLabel}
          nodeCanvasObject={paintNode}
          nodeCanvasObjectMode={() => "replace"}
          nodePointerAreaPaint={paintPointerArea}
          onRenderFramePost={paintLabels}
          linkColor={linkColor}
          linkWidth={linkWidth}
          linkDirectionalArrowLength={(l: any) => (hiLinks.has(l.__i) ? 4 : 0)}
          linkDirectionalArrowRelPos={0.72}
          linkDirectionalArrowColor={() => "rgba(45,212,191,0.9)"}
          linkDirectionalParticles={linkParticles}
          linkDirectionalParticleWidth={2}
          linkDirectionalParticleSpeed={0.006}
          linkDirectionalParticleColor={() => "rgba(45,212,191,0.9)"}
          autoPauseRedraw={false}
          minZoom={0.2}
          maxZoom={8}
          enableNodeDrag={true}
          d3VelocityDecay={0.42}
          d3AlphaDecay={0.035}
          onEngineTick={() => {
            const m = posRef.current;
            for (const n of graphData.nodes as any[]) {
              if (n.x != null) m.set(n.id, { x: n.x, y: n.y, vx: n.vx ?? 0, vy: n.vy ?? 0 });
            }
          }}
          onEngineStop={() => {
            // Fit once when the layout first settles — but leave nodes free (no freeze).
            if (!fitted.current) {
              fgRef.current?.zoomToFit(500, fs ? 90 : 60);
              fitted.current = true;
            }
          }}
          onNodeDragEnd={(n: any) => {
            // Pin where dropped (Obsidian-style); neighbours still respond.
            n.fx = n.x;
            n.fy = n.y;
            posRef.current.set(n.id, { x: n.x, y: n.y, vx: 0, vy: 0 });
          }}
          onNodeHover={(n: any) => setHover(n ? n.id : null)}
          onNodeClick={(n: any) => {
            setSearch("");
            setSearchOpen(false);
            onSelect(n as InfluenceNode);
          }}
          onBackgroundClick={() => {
            setSearchOpen(false);
            onSelect(null);
          }}
        />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Legend                                                             */
/* ------------------------------------------------------------------ */
function Legend() {
  return (
    <Card title="How to read the web">
      <p className="text-xs leading-relaxed text-ink-light">
        Each grounded answer becomes a chain: a <strong className="text-ink">source</strong> backs a{" "}
        <strong className="text-ink">claim</strong>, which expresses a <strong className="text-ink">narrative</strong>,
        which shapes a <strong className="text-ink">brand position</strong>.
      </p>
      <p className="mt-2 text-xs leading-relaxed text-ink-light">
        <strong className="text-ink">Hover</strong> a node to trace its full chain, <strong className="text-ink">drag</strong> to
        pin it, and <strong className="text-ink">click</strong> for detail. The colour chips at the lower-left of the
        canvas double as the legend — tap one to show or hide that group. Open <strong className="text-ink">Tune</strong> to
        adjust the physics or replay the layout and clear pins.
      </p>
      <div className="mt-3 text-[10px] font-bold uppercase tracking-widest text-ink-muted">
        Brand position (best → worst)
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1.5">
        {POSITION_SCALE.map((p) => (
          <span key={p.key} className="inline-flex items-center gap-1.5 text-[11px] font-medium text-ink-light">
            <span className="h-2.5 w-2.5 rounded-full" style={{ background: POSITION_NODE_COLOR[p.key] }} />
            {p.label}
          </span>
        ))}
      </div>
    </Card>
  );
}

function MiniStat({ icon, label, value }: { icon: ReactNode; label: string; value: number }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-brand-surface/40 px-2.5 py-1.5">
      <span className="text-brand-light">{icon}</span>
      <strong className="text-sm font-bold tabular-nums text-ink">{value.toLocaleString()}</strong>
      <span className="text-[11px] font-medium text-ink-light">{label}</span>
    </span>
  );
}

/* ------------------------------------------------------------------ */
/*  Theme drivers — the punchline ("who drives each narrative")         */
/* ------------------------------------------------------------------ */
function ThemeDrivers({ data, onFocusTheme }: { data: InfluenceGraphData; onFocusTheme: (t: string) => void }) {
  const drivers = data.meta.theme_drivers;
  if (!drivers.length)
    return (
      <Card title="What's driving each narrative">
        <EmptyState message="No narratives with grounded sources in scope yet." icon={<Layers size={26} />} />
      </Card>
    );
  return (
    <Card title="What's driving each narrative">
      <p className="mb-3 text-xs text-ink-light">The specific sources shaping each storyline in AI answers.</p>
      <div className="space-y-3">
        {drivers.slice(0, 6).map((td) => {
          const top = td.top_sources[0];
          return (
            <button
              key={td.theme}
              onClick={() => onFocusTheme(td.theme)}
              className="w-full rounded-xl border border-line p-3 text-left transition-colors hover:border-brand-light/40 hover:bg-slate-50"
            >
              <div className="mb-1.5 flex items-center justify-between gap-2">
                <span className="truncate text-sm font-bold text-ink" title={td.theme}>
                  {td.theme}
                </span>
                <span className="shrink-0 text-[10px] font-semibold tabular-nums text-ink-muted">
                  {td.theme_responses} answer{td.theme_responses === 1 ? "" : "s"}
                </span>
              </div>
              {top && (
                <div className="flex items-center gap-2">
                  <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-100">
                    <span
                      className="block h-full rounded-full"
                      style={{
                        width: `${Math.min(100, top.share_pct)}%`,
                        background: CONTROL_NODE_COLOR[top.control_type] ?? "#94A3B8",
                      }}
                    />
                  </span>
                  <span className="shrink-0 text-xs font-bold tabular-nums text-ink">{top.share_pct}%</span>
                </div>
              )}
              {top && (
                <div className="mt-1.5 flex items-center gap-1.5">
                  <ControlBadge control={top.control_type} />
                  <span className="truncate text-[11px] font-medium text-ink-light" title={top.authority_domain}>
                    {top.publisher_name || top.authority_domain}
                  </span>
                </div>
              )}
            </button>
          );
        })}
      </div>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/*  Node detail / drill-down panel                                     */
/* ------------------------------------------------------------------ */

// Roll up a source's cited answers into a compact footprint: how many
// answers/models, the brand-position mix, and the average sentiment — so the
// panel can lead with insight instead of a raw list of every citation.
type CiteRollup = {
  n: number;
  models: { name: string; count: number }[];
  positions: { key: string; count: number }[];
  avgSentiment: number | null;
};
function rollupCitations(items: SourceDomainCitation[]): CiteRollup {
  const models = new Map<string, number>();
  const positions = new Map<string, number>();
  let sSum = 0;
  let sN = 0;
  for (const it of items) {
    if (it.llm_name) models.set(it.llm_name, (models.get(it.llm_name) ?? 0) + 1);
    const p = it.competitive_position ?? "NOT_MENTIONED";
    positions.set(p, (positions.get(p) ?? 0) + 1);
    if (typeof it.sentiment_score === "number") {
      sSum += it.sentiment_score;
      sN += 1;
    }
  }
  return {
    n: items.length,
    models: [...models.entries()].map(([name, count]) => ({ name, count })).sort((a, b) => b.count - a.count),
    positions: [...positions.entries()].map(([key, count]) => ({ key, count })).sort((a, b) => b.count - a.count),
    avgSentiment: sN ? sSum / sN : null,
  };
}

// Horizontal share bar of brand positions, ordered best -> worst to match the
// canvas legend and the app-wide PositionBadge scale.
function PositionMixBar({ positions, total }: { positions: { key: string; count: number }[]; total: number }) {
  if (!total) return null;
  const ordered = POSITION_SCALE.map((p) => ({
    key: p.key,
    label: p.label,
    count: positions.find((x) => x.key === p.key)?.count ?? 0,
  })).filter((p) => p.count > 0);
  return (
    <div>
      <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-slate-100">
        {ordered.map((p) => (
          <span
            key={p.key}
            className="h-full"
            style={{ width: `${(p.count / total) * 100}%`, background: POSITION_NODE_COLOR[p.key] }}
            title={`${p.label}: ${p.count} of ${total}`}
          />
        ))}
      </div>
      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1">
        {ordered.map((p) => (
          <span key={p.key} className="inline-flex items-center gap-1 text-[10px] font-medium text-ink-light">
            <span className="h-2 w-2 rounded-full" style={{ background: POSITION_NODE_COLOR[p.key] }} />
            {p.label}
            <span className="tabular-nums text-ink-muted">{Math.round((p.count / total) * 100)}%</span>
          </span>
        ))}
      </div>
    </div>
  );
}

function NeighborList({
  title,
  items,
  onSelect,
}: {
  title: string;
  items: { node: InfluenceNode; value: number }[];
  onSelect?: (n: InfluenceNode) => void;
}) {
  if (!items.length) return null;
  const sorted = [...items].sort((a, b) => b.value - a.value).slice(0, 8);
  return (
    <div>
      <div className="mb-1.5 text-[10px] font-bold uppercase tracking-widest text-ink-muted">{title}</div>
      <div className="space-y-1.5">
        {sorted.map((it) => {
          const label = nodeDisplayLabel(it.node);
          return (
            <button
              key={it.node.id}
              type="button"
              onClick={() => onSelect?.(it.node)}
              disabled={!onSelect}
              className="flex w-full items-center gap-2 rounded-lg bg-slate-50 px-2.5 py-1.5 text-left transition-colors enabled:hover:bg-slate-100 disabled:cursor-default"
              title={onSelect ? `Open ${label}` : label}
            >
              <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: nodeColor(it.node) }} />
              <span className="min-w-0 flex-1 truncate text-[12px] font-medium text-ink">{label}</span>
              <span className="shrink-0 text-[10px] font-bold tabular-nums text-ink-muted">{it.value}</span>
              {onSelect && <ChevronRight size={12} className="shrink-0 text-ink-muted" />}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// One real answer (the receipt): model + sentiment + position + question + cited URL.
function AnswerCard({ it }: { it: SourceDomainCitation }) {
  return (
    <div className="rounded-xl border border-line p-2.5">
      <div className="mb-1 flex flex-wrap items-center gap-1.5">
        <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-bold text-ink-light">{it.llm_name}</span>
        <SentimentBadge score={it.sentiment_score} />
        <PositionBadge position={it.competitive_position} />
      </div>
      <p className="line-clamp-2 text-[12px] font-medium text-ink" title={it.question_text}>
        {it.question_text}
      </p>
      {it.urls[0] && (
        <a
          href={it.urls[0]}
          target="_blank"
          rel="noreferrer"
          className="mt-1 inline-flex items-center gap-1 truncate text-[11px] font-medium text-brand-light hover:text-brand"
          title={it.urls[0]}
        >
          <ExternalLink size={11} /> <span className="truncate">{it.urls[0]}</span>
        </a>
      )}
    </div>
  );
}

// Example answers grouped into collapsible sections by brand position (best -> worst) so you
// can jump straight to, say, the "Not endorsed" answers. Falls back to a flat list when every
// answer shares one position (e.g. a position node).
function GroupedAnswers({ items, limit = 24 }: { items: SourceDomainCitation[]; limit?: number }) {
  const shown = items.slice(0, limit);
  const byPos = new Map<string, SourceDomainCitation[]>();
  for (const it of shown) {
    const p = it.competitive_position ?? "NOT_MENTIONED";
    const arr = byPos.get(p) ?? [];
    arr.push(it);
    byPos.set(p, arr);
  }
  const groups = POSITION_SCALE.map((p) => ({
    key: p.key,
    label: p.label,
    items: byPos.get(p.key) ?? [],
  })).filter((g) => g.items.length > 0);
  const [open, setOpen] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(groups.map((g, i) => [g.key, i === 0])),
  );

  if (groups.length <= 1) {
    return (
      <div className="max-h-72 space-y-2 overflow-auto pr-1">
        {shown.map((it) => (
          <AnswerCard key={it.response_id} it={it} />
        ))}
      </div>
    );
  }
  return (
    <div className="max-h-80 space-y-1.5 overflow-auto pr-1">
      {groups.map((g) => {
        const isOpen = open[g.key] ?? false;
        return (
          <div key={g.key} className="overflow-hidden rounded-xl border border-line">
            <button
              type="button"
              onClick={() => setOpen((o) => ({ ...o, [g.key]: !isOpen }))}
              className="flex w-full items-center gap-2 bg-slate-50 px-2.5 py-2 text-left text-[11px] font-bold text-ink transition-colors hover:bg-slate-100"
            >
              <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: POSITION_NODE_COLOR[g.key] }} />
              <span className="flex-1 truncate">{g.label}</span>
              <span className="shrink-0 tabular-nums text-ink-muted">{g.items.length}</span>
              <ChevronDown size={13} className={`shrink-0 text-ink-muted transition-transform ${isOpen ? "rotate-180" : ""}`} />
            </button>
            {isOpen && (
              <div className="space-y-2 p-2">
                {g.items.map((it) => (
                  <AnswerCard key={it.response_id} it={it} />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// Plain-text/markdown-ish summary of a node's footprint for the clipboard.
function buildNodeSummary(node: InfluenceNode, roll: CiteRollup | null): string {
  const name = nodeDisplayLabel(node);
  const lines = [`${TYPE_LABEL[node.type]}: ${name}`, `Grounded answers: ${node.weight}`];
  if (roll && roll.n > 0) {
    if (roll.avgSentiment != null) lines.push(`Avg sentiment: ${roll.avgSentiment.toFixed(2)}`);
    if (roll.models.length) lines.push(`Models: ${roll.models.map((m) => `${m.name} (${m.count})`).join(", ")}`);
    if (node.type !== "position") {
      const mix = POSITION_SCALE.map((p) => ({
        label: p.label,
        count: roll.positions.find((x) => x.key === p.key)?.count ?? 0,
      }))
        .filter((p) => p.count > 0)
        .map((p) => `${p.label} ${Math.round((p.count / roll.n) * 100)}%`)
        .join(", ");
      if (mix) lines.push(`Brand position mix: ${mix}`);
    }
  }
  return lines.join("\n");
}

function NodeDetail({
  node,
  ins,
  outs,
  evidence,
  evidenceLoading,
  focusDomain,
  canDesignate,
  onFocusDomain,
  onDesignate,
  onBuildFix,
  onBuildContentActions,
  onClose,
  onSelectNode,
}: {
  node: InfluenceNode;
  ins: { node: InfluenceNode; value: number }[];
  outs: { node: InfluenceNode; value: number }[];
  evidence: { items: SourceDomainCitation[]; response_count: number } | null;
  evidenceLoading: boolean;
  focusDomain: string;
  canDesignate: boolean;
  onFocusDomain: (d: string) => void;
  onDesignate: () => void;
  onBuildFix: () => void;
  onBuildContentActions?: (responseIds: string[]) => Promise<void>;
  onClose: () => void;
  onSelectNode: (n: InfluenceNode) => void;
}) {
  const [copied, setCopied] = useState(false);
  const [building, setBuilding] = useState(false);
  // The "not mentioned" cohort behind this node — the answers where the brand is absent
  // (matches the Not-mentioned group + position mix, which treat a null position as absent).
  const notMentionedIds = useMemo(
    () =>
      Array.from(
        new Set(
          (evidence?.items ?? [])
            .filter((it) => (it.competitive_position ?? "NOT_MENTIONED") === "NOT_MENTIONED")
            .map((it) => it.response_id)
            .filter(Boolean),
        ),
      ),
    [evidence],
  );
  async function handleBuildContentActions() {
    if (!onBuildContentActions || !notMentionedIds.length || building) return;
    setBuilding(true);
    try {
      await onBuildContentActions(notMentionedIds);
    } finally {
      setBuilding(false);
    }
  }
  const isSource = node.type === "source";
  const isFocused = isSource && focusDomain === node.authority_domain;
  const hasEvidence = node.type === "source" || node.type === "theme" || node.type === "position";
  const roll = evidence ? rollupCitations(evidence.items) : null;
  const footprintHint =
    node.type === "theme"
      ? `How the brand was positioned across the ${roll?.n ?? 0} sampled answers expressing this narrative.`
      : node.type === "position"
      ? `Sentiment and models across the ${roll?.n ?? 0} sampled answers with this position.`
      : `How the brand was positioned across the ${roll?.n ?? 0} sampled answers that cited this source.`;
  const evidenceHint =
    node.type === "theme"
      ? "Live answers expressing this narrative, with model, sentiment, and brand position."
      : node.type === "position"
      ? "Live answers where the brand landed in this position, with model and sentiment."
      : "Live responses that cited this domain, with model, sentiment, and brand position.";
  function handleCopy() {
    try {
      navigator.clipboard?.writeText(buildNodeSummary(node, roll));
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable */
    }
  }
  return (
    <Card>
      <div className="mb-3 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
            <TypeChip type={node.type} color={node.type === "position" ? POSITION_NODE_COLOR[node.label] : undefined} />
            {isSource && <ControlBadge control={node.control_type} />}
          </div>
          <h3 className="break-words text-base font-bold leading-tight text-ink">
            {nodeDisplayLabel(node)}
          </h3>
          {isSource && (node.display_category || node.authority_type || node.url) && (
            <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-ink-light">
              {(node.display_category || node.authority_type) && (
                <span className="truncate" title={node.display_category ?? node.authority_type ?? ""}>
                  {node.display_category || node.authority_type}
                </span>
              )}
              {node.url && (
                <a
                  href={node.url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 font-medium text-brand-light hover:text-brand"
                >
                  <ExternalLink size={11} /> Visit
                </a>
              )}
            </div>
          )}
        </div>
        <button onClick={onClose} className="shrink-0 rounded-lg p-1 text-ink-muted hover:bg-slate-100 hover:text-ink">
          <X size={16} />
        </button>
      </div>

      {node.type === "claim" && node.text && (
        <details className="mb-3 rounded-xl border border-line bg-slate-50 p-3">
          <summary className="cursor-pointer text-[10px] font-bold uppercase tracking-wider text-ink-muted">Raw supporting text</summary>
          <p className="mt-2 break-all text-[11px] leading-relaxed text-ink-muted">{node.text}</p>
        </details>
      )}

      <div className="mb-3 flex items-center gap-2 text-xs text-ink-light">
        <Target size={13} className="text-brand-light" />
        Appears in <strong className="text-ink">{node.weight}</strong> grounded answer{node.weight === 1 ? "" : "s"}.
      </div>

      {/* Source actions */}
      {isSource && (
        <div className="mb-3 flex flex-wrap gap-2">
          <button
            onClick={() => onFocusDomain(isFocused ? "" : node.authority_domain ?? "")}
            className={`inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[11px] font-bold transition-colors ${
              isFocused
                ? "border-brand bg-brand text-white"
                : "border-line bg-white text-ink-light hover:border-brand-light/40 hover:text-ink"
            }`}
          >
            <Crosshair size={12} /> {isFocused ? "Focused" : "Focus in web"}
          </button>
          <button
            onClick={onDesignate}
            disabled={!canDesignate}
            title={canDesignate ? "Add as a preferred source for the selected therapeutic area" : "Pick a therapeutic area first"}
            className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-white px-2.5 py-1.5 text-[11px] font-bold text-ink-light transition-colors hover:border-brand-light/40 hover:text-ink disabled:cursor-not-allowed disabled:opacity-40"
          >
            <ListPlus size={12} /> Preferred source
          </button>
          <button
            onClick={onBuildFix}
            className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-white px-2.5 py-1.5 text-[11px] font-bold text-ink-light transition-colors hover:border-brand-light/40 hover:text-ink"
          >
            <Sparkles size={12} /> Build GEO fix
          </button>
        </div>
      )}

      {/* Footprint — the synthesis: how the brand shows up in these answers */}
      {hasEvidence && (evidenceLoading && !evidence ? (
        <div className="mb-3 flex justify-center py-4"><Spinner size={18} /></div>
      ) : roll && roll.n > 0 ? (
        <div className="mb-4 rounded-xl border border-line bg-slate-50/60 p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <div className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest text-ink-muted">
              {node.type === "position" ? "Sentiment & models" : "Brand position in these answers"}
              <InfoTooltip content={footprintHint} />
            </div>
            <button
              type="button"
              onClick={handleCopy}
              className="inline-flex shrink-0 items-center gap-1 rounded-md border border-line bg-white px-1.5 py-0.5 text-[10px] font-bold text-ink-light transition-colors hover:text-ink"
              title="Copy this summary to the clipboard"
            >
              {copied ? <Check size={11} className="text-emerald-500" /> : <Copy size={11} />}
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
          {node.type !== "position" && <PositionMixBar positions={roll.positions} total={roll.n} />}
          <div className={`flex flex-wrap items-center gap-x-4 gap-y-2 ${node.type !== "position" ? "mt-3" : ""}`}>
            <div className="flex items-center gap-1.5">
              <span className="text-[10px] font-bold uppercase tracking-wider text-ink-muted">Sentiment</span>
              <SentimentBadge score={roll.avgSentiment} />
            </div>
            <div className="flex min-w-0 items-center gap-1.5">
              <span className="shrink-0 text-[10px] font-bold uppercase tracking-wider text-ink-muted">Models</span>
              <span className="flex flex-wrap gap-1">
                {roll.models.map((m) => (
                  <span key={m.name} className="inline-flex items-center gap-1 rounded-full bg-white px-2 py-0.5 text-[10px] font-semibold text-ink-light ring-1 ring-line">
                    {m.name} <span className="tabular-nums text-ink-muted">{m.count}</span>
                  </span>
                ))}
              </span>
            </div>
          </div>
        </div>
      ) : null)}

      {/* Relationships from the graph — click a row to walk the web */}
      <div className="space-y-3">
        {node.type === "source" && <NeighborList title="Claims it backs" items={outs} onSelect={onSelectNode} />}
        {node.type === "claim" && (
          <>
            <NeighborList title="Backed by sources" items={ins} onSelect={onSelectNode} />
            <NeighborList title="Feeds narratives" items={outs} onSelect={onSelectNode} />
          </>
        )}
        {node.type === "theme" && (
          <>
            <NeighborList title="Built from claims" items={ins} onSelect={onSelectNode} />
            <NeighborList title="Shapes positions" items={outs} onSelect={onSelectNode} />
          </>
        )}
        {node.type === "position" && <NeighborList title="Driven by narratives" items={ins} onSelect={onSelectNode} />}
      </div>

      {/* One-click: generate a GEO content action (recommendation) for every not-mentioned answer */}
      {onBuildContentActions && notMentionedIds.length > 0 && (
        <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50/70 p-3">
          <div className="mb-2 flex items-start gap-2">
            <Sparkles size={14} className="mt-0.5 shrink-0 text-amber-600" />
            <p className="text-[12px] leading-relaxed text-ink-light">
              <strong className="text-ink">{notMentionedIds.length}</strong> answer
              {notMentionedIds.length === 1 ? "" : "s"} here don't mention the brand. Add a GEO
              content action for each in the Recommendations tab.
            </p>
          </div>
          <button
            onClick={handleBuildContentActions}
            disabled={building}
            className="inline-flex items-center gap-1.5 rounded-lg bg-brand px-3 py-1.5 text-[11px] font-bold text-white transition-colors hover:bg-brand-dark disabled:cursor-not-allowed disabled:opacity-60"
            title="Generate a GEO content action (recommendation) for each not-mentioned answer — shown in GEO Intervention Recommendations"
          >
            {building ? <Spinner size={12} /> : <Sparkles size={12} />}
            {building ? "Building…" : `Build content actions (${notMentionedIds.length})`}
          </button>
        </div>
      )}

      {/* Real-answer evidence — the receipts behind this node */}
      {hasEvidence && (
        <div className="mt-4 border-t border-line pt-3">
          <div className="mb-2 flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest text-ink-muted">
            Example answers{evidence && evidence.items.length ? ` (${Math.min(24, evidence.items.length)} of ${evidence.response_count})` : ""}
            <InfoTooltip content={evidenceHint} />
          </div>
          {evidenceLoading ? (
            <div className="flex justify-center py-6">
              <Spinner size={20} />
            </div>
          ) : evidence && evidence.items.length ? (
            <GroupedAnswers key={node.id} items={evidence.items} />
          ) : (
            <p className="py-3 text-center text-xs text-ink-muted">No answer detail in the current scope.</p>
          )}
        </div>
      )}
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/*  Page                                                               */
/* ------------------------------------------------------------------ */
const DENSITY_OPTIONS = ["25", "40", "60", "100"];

export default function InfluenceGraphPage() {
  const navigate = useNavigate();
  const [taSel, setTaSel] = useState<TaSelection>({ area: "", indication: "", brand: "", disease: "" });
  const [taFilters, setTaFilters] = useState<TaFilters>({});
  const [model, setModel] = useState("");
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [themeFocus, setThemeFocus] = useState("");
  const [focusDomain, setFocusDomain] = useState("");
  const [topN, setTopN] = useState(40);

  const [data, setData] = useState<InfluenceGraphData | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selected, setSelected] = useState<InfluenceNode | null>(null);
  const [detail, setDetail] = useState<SourceDomainDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [nodeEvidence, setNodeEvidence] = useState<InfluenceNodeEvidence | null>(null);
  const [nodeEvidenceLoading, setNodeEvidenceLoading] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [panelOpen, setPanelOpen] = useState(() => window.innerWidth >= 1024);
  // Fullscreen targets the whole graph shell (canvas + drawer) so the side
  // panel stays visible in fullscreen — not just the inner canvas.
  const graphWrapRef = useRef<HTMLDivElement>(null);
  const graphRequestRef = useRef(0);
  const detailRequestRef = useRef(0);
  const nodeEvidenceRequestRef = useRef(0);
  const [fs, setFs] = useState(false);

  // Select a node and reveal the side panel so its detail is visible.
  const selectNode = useCallback((n: InfluenceNode | null) => {
    setSelected(n);
    if (n) setPanelOpen(true);
  }, []);

  useEffect(() => {
    const onFs = () => setFs(document.fullscreenElement === graphWrapRef.current);
    document.addEventListener("fullscreenchange", onFs);
    return () => document.removeEventListener("fullscreenchange", onFs);
  }, []);
  const toggleFullscreen = useCallback(() => {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else graphWrapRef.current?.requestFullscreen?.().catch(() => {});
  }, []);

  const filters: SaFilters = useMemo(
    () => ({
      therapeutic_area: taFilters.therapeutic_area,
      indication: taFilters.indication,
      brand: taFilters.brand,
      llm_name: model || undefined,
    }),
    [taFilters, model],
  );

  const load = useCallback(async () => {
    const requestId = ++graphRequestRef.current;
    setLoading(true);
    setLoadError(null);
    try {
      const g = await api.sourceAuthorityInfluenceGraph(filters, {
        theme: themeFocus || undefined,
        focus_domain: focusDomain || undefined,
        top_n: topN,
      });
      if (requestId !== graphRequestRef.current) return;
      setData(g);
      setSelected((current) => current ? g.nodes.find((node) => node.id === current.id) ?? null : null);
    } catch {
      if (requestId === graphRequestRef.current) setLoadError("The influence graph could not be refreshed.");
    } finally {
      if (requestId === graphRequestRef.current) setLoading(false);
    }
  }, [filters, themeFocus, focusDomain, topN]);

  useEffect(() => {
    load();
    return () => {
      graphRequestRef.current += 1;
    };
  }, [load]);

  // Populate the model dropdown from the models that actually produced citations.
  useEffect(() => {
    api
      .sourceAuthorityTopDomains(undefined, "llm_name", 1)
      .then((t) => setModelOptions((t.groups ?? []).map((g) => g.llm_name)))
      .catch(() => {});
  }, []);

  // Fetch live drill-down evidence when a source node is selected.
  useEffect(() => {
    const requestId = ++detailRequestRef.current;
    if (selected?.type === "source" && selected.authority_domain) {
      setDetail(null);
      setDetailLoading(true);
      api
        .sourceAuthorityDomain(selected.authority_domain, filters, 25)
        .then((next) => {
          if (requestId === detailRequestRef.current) setDetail(next);
        })
        .catch(() => {
          if (requestId === detailRequestRef.current) setDetail(null);
        })
        .finally(() => {
          if (requestId === detailRequestRef.current) setDetailLoading(false);
        });
    } else {
      setDetail(null);
      setDetailLoading(false);
    }
    return () => {
      if (requestId === detailRequestRef.current) detailRequestRef.current += 1;
    };
  }, [selected, filters]);

  // Narrative + brand-position nodes get their own real-answer drill-down.
  useEffect(() => {
    const requestId = ++nodeEvidenceRequestRef.current;
    if (selected && (selected.type === "theme" || selected.type === "position")) {
      setNodeEvidence(null);
      setNodeEvidenceLoading(true);
      api
        .sourceAuthorityInfluenceNodeEvidence(selected.type, selected.label, filters, 25)
        .then((next) => {
          if (requestId === nodeEvidenceRequestRef.current) setNodeEvidence(next);
        })
        .catch(() => {
          if (requestId === nodeEvidenceRequestRef.current) setNodeEvidence(null);
        })
        .finally(() => {
          if (requestId === nodeEvidenceRequestRef.current) setNodeEvidenceLoading(false);
        });
    } else {
      setNodeEvidence(null);
      setNodeEvidenceLoading(false);
    }
    return () => {
      if (requestId === nodeEvidenceRequestRef.current) nodeEvidenceRequestRef.current += 1;
    };
  }, [selected, filters]);

  const counts = useMemo(() => {
    const c: Record<InfluenceNodeType, number> = { source: 0, claim: 0, theme: 0, position: 0 };
    data?.nodes.forEach((n) => (c[n.type] += 1));
    return c;
  }, [data]);

  const graphIndex = useMemo(() => {
    const byId = new Map<string, InfluenceNode>();
    data?.nodes.forEach((n) => byId.set(n.id, n));
    const outs = new Map<string, { node: InfluenceNode; value: number }[]>();
    const ins = new Map<string, { node: InfluenceNode; value: number }[]>();
    data?.links.forEach((l) => {
      const s = byId.get(l.source);
      const t = byId.get(l.target);
      if (s && t) {
        if (!outs.has(l.source)) outs.set(l.source, []);
        outs.get(l.source)!.push({ node: t, value: l.value });
        if (!ins.has(l.target)) ins.set(l.target, []);
        ins.get(l.target)!.push({ node: s, value: l.value });
      }
    });
    return { outs, ins };
  }, [data]);

  const bigPicture = useMemo(() => {
    if (!data) return null;
    // Prefer the competitor source with the largest real FOOTPRINT (answer volume),
    // not the flashiest share — a 50% share of a 2-answer narrative is noise, not a story.
    const MIN_RESPONSES = 3;
    let best: { responses: number; share_pct: number; name: string; theme: string } | null = null;
    for (const td of data.meta.theme_drivers) {
      for (const s of td.top_sources) {
        if (s.control_type !== "COMPETITOR" || s.responses < MIN_RESPONSES) continue;
        const better =
          !best || s.responses > best.responses || (s.responses === best.responses && s.share_pct > best.share_pct);
        if (better)
          best = { responses: s.responses, share_pct: s.share_pct, name: s.publisher_name || s.authority_domain, theme: td.theme };
      }
    }
    if (best)
      return {
        lead: `${best.name} shapes ${best.share_pct}% of the "${best.theme}" narrative (${best.responses} answers).`,
        action: "Trace its claims in the web, then build a GEO fix to reclaim the story.",
      };
    const td = data.meta.theme_drivers[0];
    const s = td?.top_sources[0];
    if (td && s)
      return {
        lead: `${s.publisher_name || s.authority_domain} is the top source behind the "${td.theme}" narrative (${s.share_pct}%).`,
        action: "Click any node to trace a narrative back to the exact sources feeding it.",
      };
    return null;
  }, [data]);

  const themeOptions = useMemo(() => ["", ...(data?.meta.theme_drivers.map((t) => t.theme) ?? [])], [data]);
  const activeTa = taFilters.indication || taFilters.therapeutic_area || "";

  // One-click: turn a node's "not mentioned" answers into GEO content actions (recommendations),
  // then jump to the GEO Intervention Recommendations tab. Does NOT create interventions.
  const buildContentActions = useCallback(
    async (responseIds: string[]) => {
      if (!responseIds.length) return;
      try {
        const res = await api.generateRecommendations({ response_ids: responseIds });
        if (res.generated > 0) {
          setBanner(
            `Added ${res.generated} GEO content action${res.generated === 1 ? "" : "s"} for the not-mentioned answer${responseIds.length === 1 ? "" : "s"}. Review them in GEO Intervention Recommendations.`,
          );
          navigate("/dashboard/recommendations");
        } else {
          setBanner("No scored gaps found for those answers — nothing to add.");
        }
      } catch {
        setBanner("Could not build content actions for the not-mentioned answers. Try again.");
      }
    },
    [navigate],
  );

  async function designatePreferred() {
    if (!activeTa || !selected?.authority_domain) return;
    try {
      await api.addPreferredSource({ therapeutic_area: activeTa, domain: selected.authority_domain });
      setBanner(`Added ${selected.authority_domain} as a preferred source for ${activeTa}.`);
    } catch {
      setBanner("Could not add preferred source (it may already exist).");
    }
  }

  const meta = data?.meta;

  return (
    <div>
      <PageHeader
        title="Source-to-Claim Influence Graph"
        badge={
          <span className="inline-flex items-center gap-1.5 rounded-full bg-brand-surface px-2.5 py-1 text-[11px] font-bold text-brand-dark">
            <Network size={12} /> Provenance web
          </span>
        }
        subtitle="Trace every AI narrative back to the exact sources feeding it — source to claim to narrative to brand position."
        tooltip="Built from grounded answers only (those that returned citable sources). Claims inherit their answer's discovered themes, so claim-to-narrative links are an answer-level approximation. Parametric answers carry no sources and are not shown."
      />

      {/* Filters */}
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div className="flex flex-wrap items-end gap-2">
          <TaHierarchyFilter value={taSel} onChange={(sel, f) => { setTaSel(sel); setTaFilters(f); }} />
          <Select label="Model" value={model} options={["", ...modelOptions]} onChange={setModel} optionLabels={{ "": "All models" }} />
          <Select
            label="Narrative focus"
            value={themeFocus}
            options={themeOptions}
            onChange={(v) => { setThemeFocus(v); setSelected(null); }}
            optionLabels={{ "": "All narratives" }}
            tooltip="Zoom the web into a single narrative to see the claims and sources feeding it."
          />
          <Select
            label="Sources shown"
            value={String(topN)}
            options={DENSITY_OPTIONS}
            onChange={(v) => setTopN(Number(v))}
            tooltip="How many top source domains to plot. Higher = denser web."
          />
        </div>
        {focusDomain && (
          <button
            onClick={() => setFocusDomain("")}
            className="inline-flex items-center gap-1.5 rounded-xl border border-brand-light/40 bg-brand-surface px-3 py-2 text-xs font-bold text-brand-dark transition-colors hover:bg-brand-surface/70"
          >
            <Crosshair size={13} /> Focused on {focusDomain} <X size={13} />
          </button>
        )}
      </div>

      {banner && (
        <div className="mb-4 flex items-center justify-between gap-3 rounded-xl border border-brand-light/30 bg-brand-surface/50 px-4 py-2.5 text-sm font-medium text-brand-dark">
          <span>{banner}</span>
          <button onClick={() => setBanner(null)} className="text-ink-muted hover:text-ink"><X size={15} /></button>
        </div>
      )}

      {loading && !data ? (
        <div className="flex justify-center py-24"><Spinner size={28} /></div>
      ) : loadError && !data ? (
        <div className="flex flex-col items-center justify-center gap-3 rounded-2xl border border-line bg-canvas-card py-20 text-center">
          <Network size={30} className="text-ink-muted" />
          <div>
            <p className="text-sm font-bold text-ink">Graph unavailable</p>
            <p className="mt-1 text-xs text-ink-light">{loadError}</p>
          </div>
          <button onClick={load} className="rounded-lg bg-brand px-3 py-1.5 text-xs font-bold text-white hover:bg-brand-dark">Retry</button>
        </div>
      ) : (
        <div className="space-y-4">
          {/* Insight ribbon — big picture + at-a-glance counts + honest coverage */}
          {meta && (
            <div className="rounded-2xl border border-line bg-canvas-card px-4 py-3">
              <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
                <div className="min-w-[16rem] flex-1">
                  {bigPicture ? (
                    <>
                      <div className="mb-0.5 inline-flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest text-brand">
                        <ScanEye size={11} /> The big picture
                      </div>
                      <p className="text-sm font-medium text-ink">
                        {bigPicture.lead} <span className="text-ink-light">{bigPicture.action}</span>
                      </p>
                    </>
                  ) : (
                    <p className="text-sm text-ink-light">Hover a node to trace a narrative back to its sources.</p>
                  )}
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <MiniStat icon={<Target size={13} />} label="Grounded" value={meta.grounded_responses} />
                  <MiniStat icon={<Network size={13} />} label="Sources" value={counts.source} />
                  <MiniStat icon={<Layers size={13} />} label="Narratives" value={counts.theme} />
                  <MiniStat icon={<Quote size={13} />} label="Claims" value={counts.claim} />
                </div>
              </div>
              <div className="mt-2.5 border-t border-line pt-2 text-xs text-ink-light">
                Mapped from <strong className="text-ink">{meta.grounded_responses.toLocaleString()}</strong> grounded
                answer{meta.grounded_responses === 1 ? "" : "s"} — <strong className="text-ink">{meta.coverage_pct}%</strong>{" "}
                of {meta.total_responses.toLocaleString()} in scope. Answers with no cited sources aren't shown.
                {meta.truncated && <> Showing the top {topN} sources for legibility.</>}
              </div>
            </div>
          )}

          {/* Immersive canvas + floating detail drawer */}
          <div
            ref={graphWrapRef}
            className={`relative overflow-hidden shadow-sm ${
              fs
                ? "h-screen w-screen rounded-none border-0 bg-[#0b1120]"
                : "h-[calc(100vh-19rem)] min-h-[560px] rounded-2xl border border-line"
            }`}
          >
            <div
              className={`absolute inset-y-0 left-0 right-0 transition-[right] duration-300 ${panelOpen ? "lg:right-[380px]" : "lg:right-0"}`}
            >
              {data && data.nodes.length ? (
                <InfluenceWeb
                  data={data}
                  selectedId={selected?.id ?? null}
                  onSelect={selectNode}
                  fs={fs}
                  onToggleFullscreen={toggleFullscreen}
                />
              ) : (
                <div className="flex h-full items-center justify-center bg-canvas-card">
                  <EmptyState
                    message="No grounded answers in this scope yet. Run analysis, or widen the filters."
                    icon={<Network size={30} />}
                  />
                </div>
              )}
            </div>

            {loading && data && (
              <div className="pointer-events-none absolute left-1/2 top-3 z-40 -translate-x-1/2 rounded-full border border-white/15 bg-slate-950/75 px-3 py-1.5 text-[11px] font-bold text-white/80 shadow-lg backdrop-blur-md">
                <span className="inline-flex items-center gap-1.5"><Spinner size={12} /> Updating graph</span>
              </div>
            )}
            {loadError && data && (
              <div className="absolute left-1/2 top-3 z-40 flex -translate-x-1/2 items-center gap-2 rounded-xl border border-red-300/30 bg-slate-950/90 px-3 py-1.5 text-[11px] font-semibold text-white shadow-lg backdrop-blur-md">
                <span>{loadError}</span>
                <button onClick={load} className="font-bold text-brand-light hover:text-white">Retry</button>
              </div>
            )}

            {/* Reopen handle when the drawer is hidden */}
            {data && data.nodes.length > 0 && !panelOpen && (
              <button
                onClick={() => setPanelOpen(true)}
                className="absolute right-3 top-14 z-30 inline-flex items-center gap-1 rounded-lg border border-white/15 bg-white/10 px-2.5 py-1.5 text-[11px] font-bold text-white/90 backdrop-blur transition-colors hover:bg-white/20"
                title="Show detail panel"
              >
                <Layers size={13} /> {selected ? "Details" : "Narratives"}
              </button>
            )}

            {/* Right slide-over: node detail when selected, else the narrative drivers */}
            {data && data.nodes.length > 0 && panelOpen && (
              <button
                type="button"
                onClick={() => setPanelOpen(false)}
                className="absolute inset-0 z-20 bg-slate-950/45 backdrop-blur-[1px] lg:hidden"
                aria-label="Close detail panel"
              />
            )}
            {data && data.nodes.length > 0 && (
              <div
                className={`pointer-events-none absolute bottom-2 left-2 right-2 top-20 z-30 transition-transform duration-300 lg:bottom-0 lg:left-auto lg:right-0 lg:top-14 lg:w-[380px] lg:p-3 ${
                  panelOpen ? "translate-x-0" : "translate-x-[110%]"
                }`}
              >
                <div className="pointer-events-auto flex h-full flex-col gap-3 overflow-y-auto rounded-2xl bg-canvas-card/95 p-2 shadow-2xl backdrop-blur-md lg:rounded-none lg:bg-transparent lg:p-0 lg:shadow-none">
                  <div className="flex justify-end">
                    <button
                      onClick={() => setPanelOpen(false)}
                      className="inline-flex items-center gap-1 rounded-lg border border-line bg-canvas-card px-2 py-1 text-[11px] font-bold text-ink-light shadow-sm transition-colors hover:text-ink"
                      title="Hide detail panel"
                    >
                      Hide <ChevronDown size={13} className="-rotate-90" />
                    </button>
                  </div>
                  {selected ? (
                    <NodeDetail
                      node={selected}
                      ins={graphIndex.ins.get(selected.id) ?? []}
                      outs={graphIndex.outs.get(selected.id) ?? []}
                      evidence={selected.type === "source" ? detail : nodeEvidence}
                      evidenceLoading={selected.type === "source" ? detailLoading : nodeEvidenceLoading}
                      focusDomain={focusDomain}
                      canDesignate={!!activeTa}
                      onFocusDomain={(d) => setFocusDomain(d)}
                      onDesignate={designatePreferred}
                      onBuildFix={() => navigate("/dashboard/recommendations")}
                      onBuildContentActions={buildContentActions}
                      onClose={() => setSelected(null)}
                      onSelectNode={selectNode}
                    />
                  ) : (
                    <>
                      {data && <ThemeDrivers data={data} onFocusTheme={(t) => { setThemeFocus(t); setSelected(null); }} />}
                      <Legend />
                    </>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
