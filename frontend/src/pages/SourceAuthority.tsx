import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Area, AreaChart, CartesianGrid, Cell, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  AlertTriangle,
  ArrowRight,
  ArrowUpDown,
  Check,
  ChevronDown,
  Database,
  ExternalLink,
  ListPlus,
  Plus,
  RefreshCw,
  Scale,
  ScanEye,
  ShieldCheck,
  ShieldAlert,
  Swords,
  Trash2,
  TrendingUp,
  X,
} from "lucide-react";
import {
  api,
  type PreferredObservation,
  type PreferredSource,
  type ResponseProvenance,
  type SaFilters,
  type SentimentBySource,
  type ShareOfVoice,
  type SourceAuthorityDistribution,
  type SourceDomainCitation,
  type SourceDomainDetail,
  type SourceDomainRank,
  type SourcePages,
  type SourceTopDomains,
  type SourceTrends,
} from "../api/client";
import { TaHierarchyFilter, type TaSelection } from "../components/TaHierarchyFilter";
import type { TaFilters } from "../api/client";
import { AnimatedCard, Card, EmptyState, InfoTooltip, PageHeader, POSITION_LABELS, PositionBadge, SentimentBadge, Spinner, Stat } from "../components/ui";

/* Trust grouping (client-side) — buckets the 8 display_category enums into a plain-language
   spectrum so users read "healthy vs risky" at a glance. No backend change. */
type TrustBucket = "TRUSTED" | "NEUTRAL" | "RISK";
const TRUST_OF: Record<string, TrustBucket> = {
  ABBVIE_CONTROLLED: "TRUSTED",
  REGULATORY: "TRUSTED",
  GUIDELINE: "TRUSTED",
  PEER_REVIEWED: "TRUSTED",
  MEDICAL_REFERENCE: "TRUSTED",
  HEALTH_MEDIA: "NEUTRAL",
  COMPETITOR_CONTROLLED: "RISK",
  SOCIAL_UGC: "RISK",
  OTHER: "RISK",
};
const TRUST_META: Record<TrustBucket, { label: string; color: string }> = {
  TRUSTED: { label: "Trusted", color: "#0D9488" },
  NEUTRAL: { label: "Neutral", color: "#F59E0B" },
  RISK: { label: "Risk", color: "#DC2626" },
};

/* Category → colour + human label (display_category from the backend). */
const CATEGORY_META: Record<string, { label: string; color: string }> = {
  ABBVIE_CONTROLLED: { label: "AbbVie-controlled", color: "#0D9488" },
  COMPETITOR_CONTROLLED: { label: "Competitor-controlled", color: "#DC2626" },
  REGULATORY: { label: "Regulatory", color: "#1D4ED8" },
  GUIDELINE: { label: "Guideline / HTA", color: "#059669" },
  PEER_REVIEWED: { label: "Peer-reviewed", color: "#0EA5E9" },
  MEDICAL_REFERENCE: { label: "Medical reference", color: "#14B8A6" },
  HEALTH_MEDIA: { label: "Health media", color: "#F59E0B" },
  SOCIAL_UGC: { label: "Social / UGC", color: "#EC4899" },
  OTHER: { label: "Other / unverified", color: "#94A3B8" },
};

const catLabel = (k: string) => CATEGORY_META[k]?.label ?? k;
const catColor = (k: string) => CATEGORY_META[k]?.color ?? "#94A3B8";

const COVERAGE_STATE_META: Record<string, { label: string; color: string; tip: string }> = {
  CLASSIFIED: { label: "Cited & classified", color: "#0D9488", tip: "Grounded answers that cited at least one source we could classify." },
  GROUNDED_ZERO_CITATIONS: { label: "Grounded, no citations", color: "#F59E0B", tip: "The model can cite sources but returned none for these answers." },
  CLASSIFICATION_FAILED: { label: "Pending / failed", color: "#94A3B8", tip: "Classification hasn't completed yet. Run Backfill historical." },
  NO_CITATION_CAPABILITY: { label: "No citation capability", color: "#CBD5E1", tip: "Parametric models that never return citations (excluded from coverage %)." },
};
const covLabel = (k: string) => COVERAGE_STATE_META[k]?.label ?? k;
const titleCase = (s: string) => (s ? s.charAt(0) + s.slice(1).toLowerCase().replace(/_/g, " ") : "N/A");

/* Segmented control — same idiom as AI Response Review's Review/Compare toggle. */
function Segmented<T extends string>({ value, onChange, options }: { value: T; onChange: (v: T) => void; options: { value: T; label: string }[] }) {
  return (
    <div className="flex rounded-xl bg-slate-100 p-1">
      {options.map((o) => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          className={`rounded-lg px-4 py-2 text-xs font-bold transition-colors ${value === o.value ? "bg-white text-ink shadow-sm" : "text-ink-light hover:text-ink"}`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function CoverageBar({ states }: { states: Record<string, number> }) {
  const order = ["CLASSIFIED", "GROUNDED_ZERO_CITATIONS", "CLASSIFICATION_FAILED", "NO_CITATION_CAPABILITY"];
  const entries = order.filter((k) => (states[k] ?? 0) > 0).map((k) => ({ k, v: states[k] }));
  const total = entries.reduce((s, e) => s + e.v, 0) || 1;
  if (!entries.length) return null;
  return (
    <div>
      <div className="flex h-3 w-full overflow-hidden rounded-full bg-slate-100">
        {entries.map((e) => (
          <div key={e.k} style={{ width: `${(e.v / total) * 100}%`, background: COVERAGE_STATE_META[e.k]?.color }} title={`${covLabel(e.k)}: ${e.v}`} />
        ))}
      </div>
      <div className="mt-2.5 flex flex-wrap gap-x-4 gap-y-1.5">
        {entries.map((e) => (
          <span key={e.k} className="inline-flex items-center gap-1.5 text-[11px] font-medium text-ink-light">
            <span className="h-2.5 w-2.5 rounded-sm" style={{ background: COVERAGE_STATE_META[e.k]?.color }} />
            {covLabel(e.k)} <span className="tabular-nums font-bold text-ink">{e.v.toLocaleString()}</span>
            <InfoTooltip content={COVERAGE_STATE_META[e.k]?.tip ?? ""} />
          </span>
        ))}
      </div>
    </div>
  );
}

function ChartTooltip({ active, payload }: any) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  return (
    <div className="rounded-lg border border-line bg-canvas-card px-3 py-2 text-xs shadow-lg">
      <div className="font-bold text-ink">{d.name}</div>
      <div className="text-ink-light">
        {d.value.toLocaleString()} citations · {d.pct}%
      </div>
    </div>
  );
}

function ControlBadge({ control }: { control: string }) {
  if (control === "ABBVIE")
    return <span className="rounded-full bg-teal-100 px-2 py-0.5 text-[10px] font-bold text-teal-800">AbbVie</span>;
  if (control === "COMPETITOR")
    return <span className="rounded-full bg-red-100 px-2 py-0.5 text-[10px] font-bold text-red-700">Competitor</span>;
  if (control === "INDEPENDENT")
    return <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-bold text-ink-light">Independent</span>;
  return <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-bold text-ink-muted">Unknown</span>;
}

function VerificationBadge({ verification }: { verification: string }) {
  if (verification === "VERIFIED")
    return <span className="inline-flex items-center gap-0.5 text-[10px] font-bold text-teal-700"><Check size={10} /> Verified</span>;
  if (verification === "UNVERIFIED")
    return <span className="inline-flex items-center gap-0.5 text-[10px] font-bold text-amber-600"><AlertTriangle size={10} /> Unverified</span>;
  return <span className="text-[10px] font-medium text-ink-muted">N/A</span>;
}

type DomainMetric = "citation_count" | "response_count";

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-2">
      <span className="text-ink-muted">{label}</span>
      <span className="truncate font-semibold text-ink" title={value}>{value}</span>
    </div>
  );
}

function DomainRow({ d, rank, metric, max, onDrill }: { d: SourceDomainRank; rank: number; metric: DomainMetric; max: number; onDrill?: (domain: string) => void }) {
  const [open, setOpen] = useState(false);
  const val = d[metric];
  return (
    <div className="rounded-lg transition-colors hover:bg-slate-50">
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-center gap-3 px-1 py-1 text-left">
        <span className="w-5 shrink-0 text-right text-xs font-bold tabular-nums text-ink-muted">{rank}</span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-semibold text-ink" title={d.authority_domain}>{d.authority_domain}</span>
            <ControlBadge control={d.control_type} />
            <VerificationBadge verification={d.verification} />
          </div>
          <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
            <div className="h-full rounded-full" style={{ width: `${(val / max) * 100}%`, background: catColor(d.display_category) }} />
          </div>
        </div>
        <span className="w-12 shrink-0 text-right text-xs font-bold tabular-nums text-ink">{val.toLocaleString()}</span>
        <ChevronDown size={13} className={`shrink-0 text-ink-muted transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <div className="mb-1.5 ml-8 grid grid-cols-2 gap-x-4 gap-y-1 rounded-lg bg-slate-50 px-3 py-2 text-[11px]">
          <Detail label="Publisher" value={d.publisher_name ?? "N/A"} />
          <Detail label="Category" value={catLabel(d.display_category)} />
          <Detail label="Control" value={titleCase(d.control_type)} />
          <Detail label="Authority" value={titleCase(d.authority_type)} />
          <Detail label="Citations" value={d.citation_count.toLocaleString()} />
          <Detail label="Responses" value={d.response_count.toLocaleString()} />
          {onDrill && (
            <button onClick={() => onDrill(d.authority_domain)} className="col-span-2 mt-1 inline-flex items-center gap-1 font-bold text-brand hover:text-brand-dark">
              View citing answers <ArrowRight size={11} />
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function DomainList({ items, metric, onDrill }: { items: SourceDomainRank[]; metric: DomainMetric; onDrill?: (domain: string) => void }) {
  if (!items.length) return <EmptyState message="No cited domains yet." icon={<Database size={26} />} />;
  const sorted = [...items].sort((a, b) => b[metric] - a[metric]);
  const max = Math.max(...sorted.map((d) => d[metric]), 1);
  return (
    <div className="space-y-0.5">
      {sorted.map((d, i) => <DomainRow key={d.authority_domain} d={d} rank={i + 1} metric={metric} max={max} onDrill={onDrill} />)}
    </div>
  );
}

const TREND_COLORS: Record<"Trusted" | "Neutral" | "Risk", string> = {
  Trusted: TRUST_META.TRUSTED.color,
  Neutral: TRUST_META.NEUTRAL.color,
  Risk: TRUST_META.RISK.color,
};

const CONTROL_COLOR: Record<string, string> = {
  ABBVIE: "#0D9488",
  COMPETITOR: "#DC2626",
  INDEPENDENT: "#94A3B8",
  UNKNOWN: "#CBD5E1",
};

// Friendly platform labels + accent colours for the per-model breakdown cards.
const MODEL_META: Record<string, { label: string; color: string }> = {
  claude: { label: "Claude", color: "#D97706" },
  evidencemd: { label: "EvidenceMD", color: "#0891B2" },
  gemini: { label: "Gemini", color: "#2563EB" },
  "gpt-4o": { label: "GPT-4o", color: "#16A34A" },
  "nova-pro": { label: "Nova Pro", color: "#0D9488" },
  llama: { label: "Llama", color: "#DB2777" },
};
const modelLabel = (n: string) => MODEL_META[n]?.label ?? n.toUpperCase();
const modelColor = (n: string) => MODEL_META[n]?.color ?? "#64748B";

const CLAIM_BUCKET_COLOR: Record<string, string> = {
  TRUSTED: "#0D9488",
  NEUTRAL: "#F59E0B",
  RISK: "#DC2626",
  UNSOURCED: "#CBD5E1",
};

const POSITION_COLORS: Record<string, string> = {
  FIRST_LINE_RECOMMENDED: "#0D9488",
  AMONG_OPTIONS: "#38BDF8",
  SECOND_LINE: "#F59E0B",
  NOT_RECOMMENDED: "#DC2626",
  NOT_MENTIONED: "#CBD5E1",
};
const POSITION_ORDER = ["FIRST_LINE_RECOMMENDED", "AMONG_OPTIONS", "SECOND_LINE", "NOT_RECOMMENDED", "NOT_MENTIONED"];

function TrendTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-line bg-canvas-card px-3 py-2 text-xs shadow-lg">
      <div className="mb-1 font-bold text-ink">{label}</div>
      {payload.map((e: any) => (
        <div key={e.dataKey} className="flex items-center gap-1.5 text-ink-light">
          <span className="h-2 w-2 rounded-sm" style={{ background: e.color }} />
          {e.dataKey} <span className="tabular-nums font-bold text-ink">{e.value}%</span>
        </div>
      ))}
      <div className="mt-1 border-t border-line pt-1 text-[10px] text-ink-muted">
        {(payload[0]?.payload?.total ?? 0).toLocaleString()} citations
      </div>
    </div>
  );
}

function PositionMiniBar({ dist, total }: { dist: Record<string, number>; total: number }) {
  const t = total || Object.values(dist).reduce((s, n) => s + n, 0) || 1;
  const parts = POSITION_ORDER.filter((k) => (dist[k] ?? 0) > 0);
  if (!parts.length) return <div className="text-[11px] text-ink-muted">no positioning scored</div>;
  return (
    <div className="flex h-2 w-full overflow-hidden rounded-full bg-slate-100">
      {parts.map((k) => (
        <div key={k} style={{ width: `${((dist[k] ?? 0) / t) * 100}%`, background: POSITION_COLORS[k] }} title={`${POSITION_LABELS[k] ?? k}: ${dist[k]}`} />
      ))}
    </div>
  );
}

function DrillRow({ it }: { it: SourceDomainCitation }) {
  const [open, setOpen] = useState(false);
  const [provOpen, setProvOpen] = useState(false);
  const [prov, setProv] = useState<ResponseProvenance | null>(null);
  const [provLoading, setProvLoading] = useState(false);
  async function toggleProv() {
    if (provOpen) { setProvOpen(false); return; }
    setProvOpen(true);
    if (!prov && !provLoading) {
      setProvLoading(true);
      try { setProv(await api.sourceAuthorityProvenance(it.response_id)); }
      catch { /* ignore */ }
      finally { setProvLoading(false); }
    }
  }
  return (
    <div className="rounded-xl border border-line p-3">
      <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
        <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-bold text-ink-light">{it.llm_name}</span>
        <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-bold text-ink-light">{it.persona}</span>
        {it.brand_focus && <span className="rounded-full bg-brand-surface px-2 py-0.5 text-[10px] font-bold text-brand-dark">{it.brand_focus}</span>}
        <span className="ml-auto text-[10px] tabular-nums text-ink-muted">{it.citation_count} citation{it.citation_count === 1 ? "" : "s"}</span>
      </div>
      <p className="line-clamp-2 text-sm font-medium text-ink" title={it.question_text}>{it.question_text}</p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <SentimentBadge score={it.sentiment_score} />
        <PositionBadge position={it.competitive_position} />
        <div className="ml-auto flex items-center gap-3">
          <button onClick={toggleProv} className="inline-flex items-center gap-1 text-[11px] font-bold text-brand hover:text-brand-dark">
            Claim sourcing <ChevronDown size={12} className={`transition-transform ${provOpen ? "rotate-180" : ""}`} />
          </button>
          {it.urls.length > 0 && (
            <button onClick={() => setOpen((o) => !o)} className="inline-flex items-center gap-1 text-[11px] font-bold text-brand hover:text-brand-dark">
              {it.urls.length} URL{it.urls.length === 1 ? "" : "s"} <ChevronDown size={12} className={`transition-transform ${open ? "rotate-180" : ""}`} />
            </button>
          )}
        </div>
      </div>
      {provOpen && (
        <div className="mt-2 border-t border-line pt-2">
          {provLoading ? (
            <div className="flex justify-center py-2"><Spinner size={16} /></div>
          ) : prov && prov.claims.length ? (
            <>
              <div className="mb-1.5 text-[10px] font-bold uppercase tracking-widest text-ink-muted">
                Claim sourcing · <span className="text-red-600">{prov.summary.RISK ?? 0} risky</span> / {prov.claims_total} claims
              </div>
              <ul className="space-y-1.5">
                {prov.claims.slice(0, 8).map((cl, i) => (
                  <li key={i} className="flex gap-2">
                    <span className="mt-1 h-2 w-2 shrink-0 rounded-full" style={{ background: CLAIM_BUCKET_COLOR[cl.bucket] ?? "#CBD5E1" }} title={cl.bucket} />
                    <div className="min-w-0">
                      <p className="line-clamp-2 text-[11px] text-ink">{cl.text}</p>
                      {cl.sources.length > 0 && (
                        <div className="truncate text-[10px] text-ink-muted">{cl.sources.map((s) => s.authority_domain).filter(Boolean).join(", ")}</div>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <div className="py-1 text-[11px] text-ink-muted">No claim-level grounding recorded for this answer.</div>
          )}
        </div>
      )}
      {open && (
        <ul className="mt-2 space-y-1 border-t border-line pt-2">
          {it.urls.map((u) => (
            <li key={u}>
              <a href={u} target="_blank" rel="noreferrer" className="inline-flex items-start gap-1 break-all text-[11px] text-brand hover:underline">
                <ExternalLink size={11} className="mt-0.5 shrink-0" /> {u}
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function DomainDrawer({ detail, loading, onClose }: { detail: SourceDomainDetail | null; loading: boolean; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/30" onClick={onClose} />
      <div className="animate-fade-up relative flex h-full w-full max-w-xl flex-col bg-canvas-card shadow-2xl">
        <div className="flex items-start justify-between gap-3 border-b border-line px-5 py-4">
          <div className="min-w-0">
            <div className="text-[11px] font-bold uppercase tracking-widest text-ink-muted">Citing answers</div>
            <div className="truncate text-lg font-bold text-ink" title={detail?.authority_domain}>{detail?.authority_domain ?? "\u2026"}</div>
            {detail?.classification && (
              <div className="mt-1 flex flex-wrap items-center gap-2">
                <ControlBadge control={detail.classification.control_type} />
                <VerificationBadge verification={detail.classification.verification} />
                <span className="text-[11px] text-ink-light">
                  {catLabel(detail.classification.display_category)}
                  {detail.classification.publisher_name ? ` \u00b7 ${detail.classification.publisher_name}` : ""}
                </span>
              </div>
            )}
          </div>
          <button onClick={onClose} className="shrink-0 rounded-lg p-1 text-ink-muted hover:bg-slate-100 hover:text-ink"><X size={18} /></button>
        </div>
        {detail && !loading && (
          <div className="flex items-center gap-4 border-b border-line px-5 py-2.5 text-[11px] text-ink-light">
            <span><span className="font-bold text-ink">{detail.total_citations.toLocaleString()}</span> citations</span>
            <span><span className="font-bold text-ink">{detail.response_count.toLocaleString()}</span> answers</span>
          </div>
        )}
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {loading ? (
            <div className="flex justify-center py-16"><Spinner size={24} /></div>
          ) : detail && detail.items.length ? (
            <div className="space-y-3">
              {detail.items.map((it) => <DrillRow key={it.response_id} it={it} />)}
            </div>
          ) : (
            <EmptyState message="No citing answers for this filter." icon={<Database size={26} />} />
          )}
        </div>
      </div>
    </div>
  );
}

export default function SourceAuthority() {
  const [taSel, setTaSel] = useState<TaSelection>({ area: "", indication: "", brand: "", disease: "" });
  const [taFilters, setTaFilters] = useState<TaFilters>({});
  const [model, setModel] = useState("");
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [tab, setTab] = useState<"overview" | "competition" | "models" | "trends" | "sentiment" | "preferred">("overview");
  const [metric, setMetric] = useState<DomainMetric>("citation_count");
  const navigate = useNavigate();

  const [dist, setDist] = useState<SourceAuthorityDistribution | null>(null);
  const [top, setTop] = useState<SourceTopDomains | null>(null);
  const [prefs, setPrefs] = useState<PreferredSource[]>([]);
  const [obs, setObs] = useState<PreferredObservation[]>([]);
  const [loading, setLoading] = useState(true);
  const [sweeping, setSweeping] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [trends, setTrends] = useState<SourceTrends | null>(null);
  const [sentiment, setSentiment] = useState<SentimentBySource | null>(null);
  const [sov, setSov] = useState<ShareOfVoice | null>(null);
  const [compPages, setCompPages] = useState<SourcePages | null>(null);
  const [drill, setDrill] = useState<string | null>(null);
  const [drillData, setDrillData] = useState<SourceDomainDetail | null>(null);
  const [drillLoading, setDrillLoading] = useState(false);
  const [generatingFix, setGeneratingFix] = useState(false);

  // Preferred-source management form
  const [prefDomain, setPrefDomain] = useState("");
  const [prefNote, setPrefNote] = useState("");

  const saFilters: SaFilters = useMemo(
    () => ({
      therapeutic_area: taFilters.therapeutic_area,
      indication: taFilters.indication,
      brand: taFilters.brand,
      llm_name: model || undefined,
    }),
    [taFilters, model],
  );

  // The TA used for preferred-source management: the most specific one selected.
  const prefTa = taSel.indication || taSel.area || "";

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const [d, t, p, o, tr, se, sv, pg] = await Promise.all([
        api.sourceAuthorityDistribution(saFilters),
        api.sourceAuthorityTopDomains(saFilters, "llm_name", 10),
        api.preferredSources(prefTa || undefined),
        api.preferredObservations(prefTa || undefined, model || undefined),
        api.sourceAuthorityTrends(saFilters),
        api.sourceAuthoritySentiment(saFilters),
        api.sourceAuthorityShareOfVoice(saFilters),
        api.sourceAuthorityPages(saFilters, "COMPETITOR", 15),
      ]);
      setDist(d);
      setTop(t);
      setPrefs(p.items);
      setObs(o.items);
      setTrends(tr);
      setSentiment(se);
      setSov(sv);
      setCompPages(pg);
      if (!model && t.groups) setModelOptions(t.groups.map((g) => g.llm_name));
    } finally {
      setLoading(false);
    }
  }, [saFilters, prefTa, model]);

  useEffect(() => {
    loadData().catch(() =>
      setBanner("Couldn't load Source Authority data — check the backend is running, then reload."),
    );
  }, [loadData]);

  async function runSweep() {
    setSweeping(true);
    setBanner(null);
    let processed = 0;
    let failed = 0;
    let remaining = 0;
    try {
      // Each pass is pure-DB now (backfill does no live enrichment), so drain the backlog in
      // bounded passes — every call picks up the next page of still-unclassified responses.
      for (let i = 0; i < 50; i++) {
        const res = await api.classifySourcesSweep();
        processed += res.processed;
        failed += res.failed;
        remaining = res.remaining;
        if (res.processed === 0 || res.remaining === 0) break;
      }
    } catch (e) {
      setBanner(`Backfill failed: ${e instanceof Error ? e.message : "see server logs."}`);
      setSweeping(false);
      return;
    }
    const failedNote = failed ? `, ${failed} failed` : "";
    const doneNote =
      processed === 0 && remaining === 0
        ? "Backfill complete — no historical responses needed classifying."
        : `Backfill classified ${processed} response(s)${failedNote}${remaining ? `; ${remaining} still pending (run again)` : ""}.`;
    setBanner(doneNote);
    // Refresh the dashboard SEPARATELY: a reload error here must not read as a backfill failure.
    try {
      await loadData();
    } catch {
      setBanner(`${doneNote} (Couldn't refresh the dashboard — reload the page.)`);
    } finally {
      setSweeping(false);
    }
  }

  async function addPreferred() {
    if (!prefTa || !prefDomain.trim()) return;
    await api.addPreferredSource({ therapeutic_area: prefTa, domain: prefDomain.trim(), note: prefNote.trim() || undefined });
    setPrefDomain("");
    setPrefNote("");
    await loadData();
  }

  async function removePreferred(id: string) {
    await api.deletePreferredSource(id);
    await loadData();
  }

  async function openDrill(domain: string) {
    setDrill(domain);
    setDrillData(null);
    setDrillLoading(true);
    try {
      setDrillData(await api.sourceAuthorityDomain(domain, saFilters, 50));
    } finally {
      setDrillLoading(false);
    }
  }

  async function generateFix() {
    if (generatingFix) return;
    const ok = window.confirm(
      "Generate GEO intervention recommendations for the current filters?\n\nThis runs the recommendation engine (SEMrush + LLM) and may take up to a minute.",
    );
    if (!ok) return;
    setGeneratingFix(true);
    setBanner(null);
    try {
      await api.generateRecommendations({
        therapeutic_area: saFilters.therapeutic_area,
        indication: saFilters.indication,
        brand: saFilters.brand,
        llm_name: saFilters.llm_name,
        limit: 25,
      });
      navigate("/dashboard/recommendations");
    } catch {
      setBanner("Could not generate recommendations: see server logs.");
      setGeneratingFix(false);
    }
  }

  const pieData = (dist?.categories ?? []).map((c) => ({
    name: catLabel(c.display_category),
    value: c.citation_count,
    pct: c.citation_share_pct,
    key: c.display_category,
  }));

  const cov = dist?.coverage;
  const groups = top?.groups ?? [];

  // Risk signals derived from the ranked domains (authoritative per-response alerts live in AI Response Review).
  const riskDomains = useMemo(() => {
    const seen = new Map<string, SourceDomainRank>();
    for (const g of groups) for (const d of g.items) {
      if (d.control_type === "COMPETITOR" || d.verification === "UNVERIFIED") seen.set(d.authority_domain, d);
    }
    return [...seen.values()].sort((a, b) => b.citation_count - a.citation_count);
  }, [groups]);

  // Headline KPIs + trust spectrum, all derived from the already-fetched distribution.
  const totalCitations = dist?.total_citations ?? 0;
  const trust = useMemo(() => {
    const acc: Record<TrustBucket, number> = { TRUSTED: 0, NEUTRAL: 0, RISK: 0 };
    for (const c of dist?.categories ?? []) acc[TRUST_OF[c.display_category] ?? "RISK"] += c.citation_share_pct;
    return acc;
  }, [dist]);
  const competitorPct = (dist?.categories ?? []).find((c) => c.display_category === "COMPETITOR_CONTROLLED")?.citation_share_pct ?? 0;
  const riskCounts = useMemo(() => {
    let comp = 0, unver = 0;
    for (const d of riskDomains) { if (d.control_type === "COMPETITOR") comp++; if (d.verification === "UNVERIFIED") unver++; }
    return { comp, unver };
  }, [riskDomains]);

  // Preferred sources ranked worst-presence-first (the absence story is the point of FR-706a.7).
  const prefRows = useMemo(
    () => prefs
      .map((p) => ({ p, o: obs.find((x) => x.pref_id === p.pref_id) }))
      .sort((a, b) => (a.o?.presence_pct ?? 999) - (b.o?.presence_pct ?? 999)),
    [prefs, obs],
  );

  // Trust-mix over time — client-side bucketing of the per-day category counts.
  const trendData = useMemo(
    () => (trends?.periods ?? []).map((p) => {
      const acc: Record<TrustBucket, number> = { TRUSTED: 0, NEUTRAL: 0, RISK: 0 };
      for (const [cat, n] of Object.entries(p.categories)) acc[TRUST_OF[cat] ?? "RISK"] += n;
      const total = p.total_citations || 1;
      return {
        period: p.period,
        Trusted: Math.round((acc.TRUSTED / total) * 1000) / 10,
        Neutral: Math.round((acc.NEUTRAL / total) * 1000) / 10,
        Risk: Math.round((acc.RISK / total) * 1000) / 10,
        total: p.total_citations,
      };
    }),
    [trends],
  );

  const competitorBucket = useMemo(
    () => sentiment?.buckets.find((b) => b.control_type === "COMPETITOR"),
    [sentiment],
  );
  const sentimentHeadline = useMemo(() => {
    const comp = sentiment?.buckets.find((b) => b.control_type === "COMPETITOR");
    const ab = sentiment?.buckets.find((b) => b.control_type === "ABBVIE");
    if (comp && ab && comp.response_count >= 3 && ab.response_count >= 3) {
      const diff = Math.round(comp.weak_position_pct - ab.weak_position_pct);
      if (diff > 0)
        return `Answers built on competitor-controlled sources land in weak brand positioning ${diff} points more often than those built on AbbVie-controlled sources (${comp.weak_position_pct}% vs ${ab.weak_position_pct}%).`;
    }
    if (comp && comp.response_count > 0)
      return `${comp.weak_position_pct}% of answers whose top source is competitor-controlled land in weak brand positioning.`;
    return null;
  }, [sentiment]);

  // Action-first plain-language headline for the IBT / marketer audience (FR-611/612).
  const bigPicture = useMemo(() => {
    if (!dist || totalCitations === 0) return null;
    const trusted = Math.round(trust.TRUSTED);
    const comp = Math.round(competitorPct);
    let lead: string;
    if (comp >= 15)
      lead = `Competitors are winning ${comp}% of the AI citations in this view. That shapes how AI answers about your brand.`;
    else if (trusted >= 60)
      lead = `AI mostly leans on trusted medical sources here (${trusted}% of citations), a healthy footprint to defend.`;
    else
      lead = `AI's sourcing here is mixed: ${trusted}% trusted vs ${comp}% competitor-controlled.`;
    const action = (comp > 0 || riskCounts.comp > 0)
      ? "Publish authoritative content on your weak topics to shift AI toward citing you."
      : "Keep reinforcing authoritative content so AI keeps citing trusted sources.";
    return { lead, action };
  }, [dist, totalCitations, trust, competitorPct, riskCounts]);

  return (
    <div>
      <PageHeader
        title="Source Authority"
        subtitle="Which publishers AI models cite for our brands, classified by ownership and authority."
        tooltip="Every cited URL is reduced to a clean root domain and classified on two axes: who controls it (AbbVie / competitor / independent) and what kind of source it is (regulatory, peer-reviewed, media, social). Only grounded models return citations."
      />

      {/* Filters */}
      <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div className="flex flex-wrap items-end gap-2">
          <TaHierarchyFilter value={taSel} onChange={(sel, f) => { setTaSel(sel); setTaFilters(f); }} />
          <div className="flex flex-col gap-0.5">
            <label className="pl-0.5 text-xs font-bold uppercase tracking-widest text-ink">Model</label>
            <select
              value={model}
              onChange={(e) => setModel(e.target.value)}
              className="cursor-pointer appearance-none rounded-lg border border-slate-200 bg-white py-2 pl-3 pr-8 text-sm font-medium text-ink shadow-sm focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand/30"
            >
              <option value="">All models</option>
              {modelOptions.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </div>
        </div>
        <button
          onClick={runSweep}
          disabled={sweeping}
          className="inline-flex items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs font-bold text-ink-light shadow-sm transition-colors hover:border-brand-light/40 hover:text-ink disabled:opacity-50"
          title="Classify sources for historical responses that have none yet"
        >
          <RefreshCw size={13} className={sweeping ? "animate-spin" : ""} /> Backfill historical
        </button>
      </div>

      {banner && (
        <div className="mb-4 rounded-xl border border-brand-light/30 bg-brand-surface/50 px-4 py-2.5 text-sm font-medium text-brand-dark">
          {banner}
        </div>
      )}

      {loading && !dist ? (
        <div className="flex justify-center py-24"><Spinner size={28} /></div>
      ) : (
        <div className="space-y-5">
          {bigPicture && (
            <div className="rounded-2xl border border-brand-light/30 bg-brand-surface/50 px-5 py-4">
              <div className="mb-1 inline-flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-widest text-brand">
                <ScanEye size={12} /> The big picture
              </div>
              <p className="text-sm font-medium text-ink">
                {bigPicture.lead} <span className="text-ink-light">{bigPicture.action}</span>
              </p>
            </div>
          )}
          {/* Headline KPIs — derived entirely from the already-fetched distribution + coverage. */}
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <Stat label="Classified citations" value={totalCitations.toLocaleString()} icon={<Database size={16} />} sub={groups.length ? `across ${groups.length} model${groups.length > 1 ? "s" : ""}` : "no models in scope"} tooltip="Total cited URLs (reduced to root domains) classified for the current filter." />
            <Stat label="Trusted-source share" value={`${Math.round(trust.TRUSTED)}%`} icon={<ShieldCheck size={16} />} sub="regulatory · peer-reviewed · AbbVie" tooltip="Share of classified citations from regulatory bodies, peer-reviewed journals, medical references, or AbbVie-controlled sites." />
            <Stat label="Competitor-controlled" value={`${Math.round(competitorPct)}%`} icon={<ShieldAlert size={16} />} sub="of classified citations" tooltip="Share of classified citations from competitor-controlled domains: the primary risk signal." />
            <Stat label="Citation coverage" value={cov ? `${cov.coverage_pct}%` : "N/A"} icon={<Check size={16} />} sub={cov ? `${cov.with_citations.toLocaleString()} of ${cov.citation_capable.toLocaleString()} grounded` : ""} tooltip="Grounded answers that cited at least one classifiable source. Parametric models are excluded from the denominator." />
          </div>

          {/* Section nav */}
          <div className="flex flex-wrap items-center justify-between gap-3">
            <Segmented value={tab} onChange={setTab} options={[{ value: "overview", label: "Overview" }, { value: "competition", label: "Competition" }, { value: "models", label: "By Model" }, { value: "trends", label: "Trends" }, { value: "sentiment", label: "Sentiment" }, { value: "preferred", label: "Preferred Sources" }]} />
            {tab === "models" && (
              <div className="flex items-center gap-1.5 text-ink-light">
                <ArrowUpDown size={13} />
                <Segmented value={metric} onChange={setMetric} options={[{ value: "citation_count", label: "By citations" }, { value: "response_count", label: "By responses" }]} />
              </div>
            )}
          </div>

          {/* ---- Overview tab ---- */}
          {tab === "overview" && (
            <div className="space-y-5">
              {cov && (
                <AnimatedCard>
                  <Card title="Citation coverage">
                    <div className="mb-3 text-sm font-medium text-ink">
                      <span className="font-bold text-brand">{cov.coverage_pct}%</span> of grounded answers cited a source we could classify
                      <span className="ml-1 text-ink-muted">({cov.with_citations.toLocaleString()} of {cov.citation_capable.toLocaleString()} citation-capable · {cov.total_responses.toLocaleString()} total in scope)</span>
                    </div>
                    <CoverageBar states={cov.states} />
                  </Card>
                </AnimatedCard>
              )}

              <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
                {/* Distribution donut + trust spectrum */}
                <AnimatedCard>
                  <Card title="Source authority distribution">
                    {pieData.length ? (
                      <>
                        <div className="relative">
                          <ResponsiveContainer width="100%" height={280}>
                            <PieChart>
                              <Pie data={pieData} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={100} innerRadius={62} paddingAngle={1}>
                                {pieData.map((d) => <Cell key={d.key} fill={catColor(d.key)} />)}
                              </Pie>
                              <Tooltip content={<ChartTooltip />} />
                            </PieChart>
                          </ResponsiveContainer>
                          <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
                            <span className="font-display text-3xl font-bold tabular-nums text-ink">{Math.round(trust.TRUSTED)}%</span>
                            <span className="text-[11px] font-semibold uppercase tracking-wide text-ink-light">trusted</span>
                          </div>
                        </div>
                        {/* Trust spectrum — buckets the 8 categories into Trusted / Neutral / Risk. */}
                        <div className="mt-3 flex h-2.5 w-full overflow-hidden rounded-full bg-slate-100">
                          {(["TRUSTED", "NEUTRAL", "RISK"] as TrustBucket[]).map((b) => (trust[b] > 0 ? (
                            <div key={b} style={{ width: `${trust[b]}%`, background: TRUST_META[b].color }} title={`${TRUST_META[b].label}: ${Math.round(trust[b])}%`} />
                          ) : null))}
                        </div>
                        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
                          {(["TRUSTED", "NEUTRAL", "RISK"] as TrustBucket[]).map((b) => (
                            <span key={b} className="inline-flex items-center gap-1.5 text-[11px] font-medium text-ink-light">
                              <span className="h-2.5 w-2.5 rounded-sm" style={{ background: TRUST_META[b].color }} />
                              {TRUST_META[b].label} <span className="tabular-nums font-bold text-ink">{Math.round(trust[b])}%</span>
                            </span>
                          ))}
                        </div>
                        <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 border-t border-line pt-3">
                          {pieData.map((d) => (
                            <span key={d.key} className="inline-flex items-center gap-1.5 text-xs font-medium text-ink-light">
                              <span className="h-2.5 w-2.5 rounded-sm" style={{ background: catColor(d.key) }} />
                              {d.name} <span className="tabular-nums text-ink-muted">{d.pct}%</span>
                            </span>
                          ))}
                        </div>
                      </>
                    ) : (
                      <EmptyState message="No classified citations yet. Run an analysis or use Backfill historical." icon={<Database size={28} />} />
                    )}
                  </Card>
                </AnimatedCard>

                {/* Risk panel — actionable */}
                <AnimatedCard>
                  <Card title="Risk: competitor & unverified top sources" accent={riskDomains.length > 0}>
                    {riskDomains.length ? (
                      <div className="space-y-2">
                        <div className="-mt-2 mb-1 flex flex-wrap items-center justify-between gap-2">
                          <p className="text-[11px] text-ink-muted">
                            <span className="font-bold text-red-600">{riskCounts.comp}</span> competitor-controlled · <span className="font-bold text-amber-600">{riskCounts.unver}</span> unverified in top sources
                          </p>
                          <div className="flex items-center gap-2">
                            <button onClick={generateFix} disabled={generatingFix} className="inline-flex items-center gap-1 rounded-lg border border-brand-light/40 bg-white px-2.5 py-1 text-[11px] font-bold text-brand transition-colors hover:bg-brand-surface disabled:opacity-50" title="Generate GEO intervention recommendations for the current filters">
                              {generatingFix ? <Spinner size={11} /> : <ListPlus size={11} />} Generate fixes
                            </button>
                            <Link to="/results?alert_only=true" className="inline-flex items-center gap-1 rounded-lg bg-brand px-2.5 py-1 text-[11px] font-bold text-white transition-colors hover:bg-brand-dark">
                              Review flagged <ExternalLink size={11} />
                            </Link>
                          </div>
                        </div>
                        {riskDomains.slice(0, 8).map((d) => (
                          <button key={d.authority_domain} onClick={() => openDrill(d.authority_domain)} className="flex w-full items-center justify-between gap-2 rounded-lg border border-line px-3 py-2 text-left transition-colors hover:border-brand-light/40 hover:bg-slate-50">
                            <span className="truncate text-sm font-semibold text-ink" title={d.authority_domain}>{d.authority_domain}</span>
                            <div className="flex items-center gap-2">
                              <ControlBadge control={d.control_type} />
                              <VerificationBadge verification={d.verification} />
                              <span className="text-xs font-bold tabular-nums text-ink-light">{d.citation_count}</span>
                            </div>
                          </button>
                        ))}
                      </div>
                    ) : (
                      <EmptyState message="No competitor-controlled or unverified sources in the top citations." icon={<ShieldCheck size={28} />} />
                    )}
                  </Card>
                </AnimatedCard>
              </div>
            </div>
          )}

          {/* ---- Competition tab (share of voice) ---- */}
          {tab === "competition" && (
            <AnimatedCard>
              <Card title="Share of voice: who AI cites for your brand">
                {sov && sov.total_citations > 0 ? (
                  <div className="space-y-5">
                    <div className="rounded-xl border border-line bg-brand-surface/40 px-4 py-3 text-sm text-ink">
                      AbbVie-controlled sources earn <span className="font-bold text-brand">{Math.round(sov.abbvie_share_pct)}%</span> of AI citations here; competitors earn <span className="font-bold text-red-600">{Math.round(sov.competitor_share_pct)}%</span>, and independent sources the rest.
                    </div>
                    <div>
                      <div className="flex h-4 w-full overflow-hidden rounded-full bg-slate-100">
                        {sov.voice.map((v) => (v.share_pct > 0 ? (
                          <div key={v.control_type} style={{ width: `${v.share_pct}%`, background: CONTROL_COLOR[v.control_type] ?? "#CBD5E1" }} title={`${v.label}: ${v.share_pct}%`} />
                        ) : null))}
                      </div>
                      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
                        {sov.voice.map((v) => (
                          <span key={v.control_type} className="inline-flex items-center gap-1.5 text-[11px] font-medium text-ink-light">
                            <span className="h-2.5 w-2.5 rounded-sm" style={{ background: CONTROL_COLOR[v.control_type] ?? "#CBD5E1" }} />
                            {v.label} <span className="tabular-nums font-bold text-ink">{v.share_pct}%</span> <span className="text-ink-muted">({v.citation_count.toLocaleString()})</span>
                          </span>
                        ))}
                      </div>
                    </div>
                    <div>
                      <div className="mb-2 flex items-center justify-between gap-2">
                        <h4 className="text-xs font-bold uppercase tracking-widest text-brand-dark">Competitor sources winning citations</h4>
                        {sov.competitor_total_citations > 0 && (
                          <button onClick={generateFix} disabled={generatingFix} className="inline-flex items-center gap-1 rounded-lg border border-brand-light/40 bg-white px-2.5 py-1 text-[11px] font-bold text-brand transition-colors hover:bg-brand-surface disabled:opacity-50">
                            {generatingFix ? <Spinner size={11} /> : <ListPlus size={11} />} Generate fixes
                          </button>
                        )}
                      </div>
                      {sov.competitors.length ? (
                        <div className="space-y-1.5">
                          {sov.competitors.map((c) => (
                            <button key={c.authority_domain} onClick={() => openDrill(c.authority_domain)} className="flex w-full items-center gap-3 rounded-lg border border-line px-3 py-2 text-left transition-colors hover:border-brand-light/40 hover:bg-slate-50">
                              <div className="min-w-0 flex-1">
                                <div className="flex items-center gap-2">
                                  <span className="truncate text-sm font-semibold text-ink" title={c.authority_domain}>{c.authority_domain}</span>
                                  {c.publisher_name && <span className="truncate text-[11px] text-ink-muted">{c.publisher_name}</span>}
                                </div>
                                <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
                                  <div className="h-full rounded-full bg-red-500" style={{ width: `${c.share_pct}%` }} />
                                </div>
                              </div>
                              <span className="w-24 shrink-0 text-right text-[11px] tabular-nums text-ink-light">{c.share_pct}% · {c.citation_count.toLocaleString()}</span>
                            </button>
                          ))}
                        </div>
                      ) : (
                        <EmptyState message="No competitor-controlled sources cited for this filter. Good news." icon={<ShieldCheck size={26} />} />
                      )}
                    </div>
                    {compPages && compPages.items.length > 0 && (
                      <div>
                        <h4 className="mb-2 text-xs font-bold uppercase tracking-widest text-brand-dark">The exact competitor pages AI cites</h4>
                        <div className="space-y-1.5">
                          {compPages.items.map((pg) => (
                            <div key={pg.url} className="flex items-center gap-3 rounded-lg border border-line px-3 py-2">
                              <div className="min-w-0 flex-1">
                                <a href={pg.url} target="_blank" rel="noreferrer" className="flex items-center gap-1 text-sm font-medium text-brand hover:underline" title={pg.url}>
                                  <ExternalLink size={11} className="shrink-0" /> <span className="truncate">{pg.url}</span>
                                </a>
                                <div className="text-[10px] text-ink-muted">{pg.authority_domain}</div>
                              </div>
                              <span className="shrink-0 text-right text-[11px] tabular-nums text-ink-light">{pg.response_count} answer{pg.response_count === 1 ? "" : "s"} · {pg.citation_count}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                ) : (
                  <EmptyState message="No classified citations yet for this filter." icon={<Swords size={28} />} />
                )}
              </Card>
            </AnimatedCard>
          )}

          {/* ---- By Model tab ---- */}
          {tab === "models" && (
            <AnimatedCard>
              <Card title={`Top 10 cited domains per model · ${metric === "citation_count" ? "by citations" : "by responses"}`}>
                {groups.length ? (
                  <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                    {groups.map((g) => {
                      const total = g.items.reduce((s, d) => s + d[metric], 0);
                      return (
                        <div key={g.llm_name} className="flex flex-col rounded-xl border border-line p-4">
                          <div className="mb-3 flex items-center justify-between gap-2 border-b border-line pb-2.5">
                            <span className="flex items-center gap-2 text-sm font-bold text-ink">
                              <span className="h-2.5 w-2.5 rounded-full" style={{ background: modelColor(g.llm_name) }} />
                              {modelLabel(g.llm_name)}
                            </span>
                            <span className="tabular-nums text-[11px] font-medium text-ink-muted">
                              {total.toLocaleString()} {metric === "citation_count" ? "citations" : "responses"} · {g.items.length} domain{g.items.length === 1 ? "" : "s"}
                            </span>
                          </div>
                          <DomainList items={g.items} metric={metric} onDrill={openDrill} />
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <EmptyState message="No cited domains for this filter." icon={<Database size={28} />} />
                )}
              </Card>
            </AnimatedCard>
          )}

          {/* ---- Trends tab ---- */}
          {tab === "trends" && (
            <AnimatedCard>
              <Card title="Trust mix of AI citations over time">
                <p className="-mt-2 mb-3 text-[11px] text-ink-muted">
                  Share of classified citations from Trusted vs. Neutral vs. Risk sources, per day. A rising red band means AI answers are increasingly leaning on competitor or unverified sources.
                </p>
                {trendData.length ? (
                  <>
                    <ResponsiveContainer width="100%" height={300}>
                      <AreaChart data={trendData} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke="#E2E8F0" vertical={false} />
                        <XAxis dataKey="period" tick={{ fontSize: 11 }} stroke="#94A3B8" />
                        <YAxis domain={[0, 100]} tickFormatter={(v) => `${v}%`} tick={{ fontSize: 11 }} stroke="#94A3B8" />
                        <Tooltip content={<TrendTooltip />} />
                        {(["Trusted", "Neutral", "Risk"] as const).map((k) => (
                          <Area key={k} type="monotone" dataKey={k} stackId="1" stroke={TREND_COLORS[k]} fill={TREND_COLORS[k]} fillOpacity={0.8} />
                        ))}
                      </AreaChart>
                    </ResponsiveContainer>
                    <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
                      {(["Trusted", "Neutral", "Risk"] as const).map((k) => (
                        <span key={k} className="inline-flex items-center gap-1.5 text-[11px] font-medium text-ink-light">
                          <span className="h-2.5 w-2.5 rounded-sm" style={{ background: TREND_COLORS[k] }} /> {k}
                        </span>
                      ))}
                    </div>
                    {trendData.length === 1 && (
                      <p className="mt-2 text-[11px] text-ink-muted">Only one day of data so far: run analyses over time to watch the trend build.</p>
                    )}
                  </>
                ) : (
                  <EmptyState message="No citation history yet for this filter. Run an analysis or use Backfill historical." icon={<TrendingUp size={28} />} />
                )}
              </Card>
            </AnimatedCard>
          )}

          {/* ---- Sentiment tab ---- */}
          {tab === "sentiment" && (
            <AnimatedCard>
              <Card title="Brand outcome by source control">
                <p className="-mt-2 mb-4 text-[11px] text-ink-muted">
                  Every scored answer is bucketed by the ownership of its <span className="font-semibold text-ink">top-cited</span> source, then we compare the brand positioning &amp; sentiment those answers earned. Do answers built on competitor sources fare worse?
                </p>
                {sentiment && sentiment.buckets.length ? (
                  <div className="space-y-4">
                    {sentimentHeadline && (
                      <div className="rounded-xl border border-line bg-brand-surface/40 px-4 py-3 text-sm text-ink">{sentimentHeadline}</div>
                    )}
                    <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
                      {sentiment.buckets.map((b) => (
                        <div key={b.control_type} className="rounded-xl border border-line p-4">
                          <div className="mb-2 flex items-center justify-between gap-2">
                            <ControlBadge control={b.control_type} />
                            <span className="text-[11px] font-medium text-ink-muted">{b.response_count} answer{b.response_count === 1 ? "" : "s"}</span>
                          </div>
                          <div className="mb-1 flex items-baseline gap-2">
                            <span className="font-display text-2xl font-bold tabular-nums text-ink">{b.weak_position_pct}%</span>
                            <span className="text-[11px] font-medium text-ink-light">weak positioning</span>
                            <InfoTooltip content="Share of these answers where the focus brand landed in SECOND_LINE or NOT_RECOMMENDED positioning." />
                          </div>
                          <div className="mb-3 flex items-center gap-1.5 text-[11px] text-ink-light">avg sentiment <SentimentBadge score={b.avg_sentiment} /></div>
                          <PositionMiniBar dist={b.position_distribution} total={b.response_count} />
                        </div>
                      ))}
                    </div>
                    {competitorBucket && competitorBucket.response_count > 0 && (
                      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3">
                        <p className="text-[11px] text-amber-800">
                          {competitorBucket.response_count} answer{competitorBucket.response_count === 1 ? "" : "s"} leaned on competitor-controlled sources. Publish authoritative content to shift these.
                        </p>
                        <button onClick={generateFix} disabled={generatingFix} className="inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-brand px-3 py-1.5 text-[11px] font-bold text-white transition-colors hover:bg-brand-dark disabled:opacity-50">
                          {generatingFix ? <Spinner size={12} /> : <ListPlus size={12} />} Generate fixes
                        </button>
                      </div>
                    )}
                  </div>
                ) : (
                  <EmptyState message="No scored answers with citations yet for this filter." icon={<Scale size={28} />} />
                )}
              </Card>
            </AnimatedCard>
          )}

          {/* ---- Preferred Sources tab ---- */}
          {tab === "preferred" && (
            <AnimatedCard>
              <Card title="Medical Affairs: Preferred sources">
                <div className="-mt-2 mb-3 flex flex-wrap items-center justify-between gap-2">
                  <p className="text-[11px] text-ink-muted">
                    Designate the authority domains AI models should cite for a therapeutic area. Presence/absence is recorded on every run.
                  </p>
                  <span className="inline-flex items-center gap-1 rounded-full bg-slate-100 px-2.5 py-0.5 text-[11px] font-bold text-ink-light">
                    Scoped to:&nbsp;<span className="text-ink">{prefTa || "no TA selected"}</span>
                  </span>
                </div>

                <div className="mb-1 flex flex-wrap items-end gap-2">
                  <div className="flex flex-col gap-0.5">
                    <label className="pl-0.5 text-[11px] font-bold uppercase tracking-widest text-ink-light">Therapeutic area</label>
                    <input
                      value={prefTa}
                      readOnly
                      placeholder="Select a TA in the filter above"
                      className="w-56 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-sm font-medium text-ink"
                    />
                  </div>
                  <div className="flex flex-col gap-0.5">
                    <label className="pl-0.5 text-[11px] font-bold uppercase tracking-widest text-ink-light">Domain</label>
                    <input
                      value={prefDomain}
                      onChange={(e) => setPrefDomain(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter") addPreferred(); }}
                      placeholder="e.g. fda.gov"
                      className="w-48 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-ink focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand/30"
                    />
                  </div>
                  <div className="flex flex-col gap-0.5">
                    <label className="pl-0.5 text-[11px] font-bold uppercase tracking-widest text-ink-light">Note (optional)</label>
                    <input
                      value={prefNote}
                      onChange={(e) => setPrefNote(e.target.value)}
                      placeholder="Rationale"
                      className="w-48 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-ink focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand/30"
                    />
                  </div>
                  <button
                    onClick={addPreferred}
                    disabled={!prefTa || !prefDomain.trim()}
                    className="inline-flex items-center gap-1.5 rounded-xl bg-brand px-3 py-2 text-xs font-bold text-white shadow-sm transition-colors hover:bg-brand-dark disabled:opacity-40"
                  >
                    <Plus size={13} /> Add
                  </button>
                </div>
                <p className="mb-4 pl-0.5 text-[11px] text-ink-muted">Normalizes to the registrable root domain (e.g. <span className="font-mono">www.fda.gov/news</span> → <span className="font-mono">fda.gov</span>).</p>

                {prefs.length ? (
                  <div className="overflow-hidden rounded-xl border border-line">
                    <table className="w-full text-sm">
                      <thead className="bg-slate-50 text-[11px] uppercase tracking-wide text-ink-light">
                        <tr>
                          <th className="px-3 py-2 text-left font-bold">Domain</th>
                          <th className="px-3 py-2 text-left font-bold">Therapeutic area</th>
                          <th className="px-3 py-2 text-left font-bold">
                            <span className="inline-flex items-center gap-1">Presence in AI citations
                            <InfoTooltip content="How often this preferred domain appeared in AI-cited sources for the scoped responses (recorded during run processing). Worst presence is listed first." /></span>
                          </th>
                          <th className="px-3 py-2" />
                        </tr>
                      </thead>
                      <tbody>
                        {prefRows.map(({ p, o }) => (
                          <tr key={p.pref_id} className="border-t border-line">
                            <td className="px-3 py-2 font-semibold text-ink">
                              {p.authority_domain}
                              {p.note && <span className="ml-2 text-[11px] font-normal text-ink-muted">{p.note}</span>}
                            </td>
                            <td className="px-3 py-2 text-ink-light">{p.therapeutic_area}</td>
                            <td className="px-3 py-2">
                              {o && o.observations > 0 ? (
                                <div className="flex items-center gap-2">
                                  <div className="h-2 w-24 shrink-0 overflow-hidden rounded-full bg-slate-100">
                                    <div className="h-full rounded-full" style={{ width: `${o.presence_pct ?? 0}%`, background: (o.presence_pct ?? 0) > 0 ? "#0D9488" : "#DC2626" }} />
                                  </div>
                                  <span className="text-[11px] tabular-nums text-ink-light">{o.present}/{o.observations}{o.presence_pct !== null ? ` · ${o.presence_pct}%` : ""}</span>
                                  {(o.presence_pct ?? 0) === 0 && <span className="inline-flex items-center gap-0.5 rounded-full bg-red-100 px-2 py-0.5 text-[10px] font-bold text-red-700"><AlertTriangle size={9} /> Never cited</span>}
                                </div>
                              ) : (
                                <span className="text-[11px] text-ink-muted">no data yet</span>
                              )}
                            </td>
                            <td className="px-3 py-2 text-right">
                              <button onClick={() => removePreferred(p.pref_id)} className="text-ink-muted transition-colors hover:text-red-500" title="Remove">
                                <Trash2 size={14} />
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <EmptyState message={prefTa ? "No preferred sources for this TA yet." : "Select a therapeutic area to manage preferred sources."} icon={<X size={26} />} />
                )}
              </Card>
            </AnimatedCard>
          )}
        </div>
      )}

      {drill && <DomainDrawer detail={drillData} loading={drillLoading} onClose={() => setDrill(null)} />}
    </div>
  );
}
