import React, { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Activity,
  AlertTriangle,
  Crown,
  FileText,
  Hash,
  Layers,
  Minus,
  Network,
  RefreshCw,
  ScanEye,
  Snowflake,
  Tags,
  TrendingDown,
  TrendingUp,
  X,
} from "lucide-react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, type TaFilters } from "../api/client";
import { TaHierarchyFilter, TaFilterBadge, type TaSelection } from "../components/TaHierarchyFilter";
import {
  AnimatedCard,
  Card,
  EmptyState,
  PageHeader,
  SentimentBadge,
  Spinner,
  Stat,
  InfoTooltip,
} from "../components/ui";

/* ------------------------------------------------------------------ */
/*  Palette + helpers                                                  */
/* ------------------------------------------------------------------ */
const THEME_COLORS = [
  "#0F766E", "#0284C7", "#EA580C", "#7C3AED", "#DC2626",
  "#059669", "#DB2777", "#2563EB", "#CA8A04", "#0D9488",
];

const CATEGORY_COLORS: Record<string, string> = {
  Efficacy: "bg-teal-100 text-teal-800",
  Safety: "bg-red-100 text-red-700",
  Access: "bg-sky-100 text-sky-800",
  Comparative: "bg-amber-100 text-amber-800",
  Experience: "bg-indigo-100 text-indigo-700",
  Other: "bg-slate-100 text-ink-light",
};

function sentColor(s: number | null | undefined): string {
  if (s === null || s === undefined) return "#94A3B8";
  return s > 0.2 ? "#0F766E" : s < -0.2 ? "#DC2626" : "#EA580C";
}

function CategoryBadge({ category }: { category?: string | null }) {
  if (!category) return null;
  return (
    <span className={`px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wide ${CATEGORY_COLORS[category] || CATEGORY_COLORS.Other}`}>
      {category}
    </span>
  );
}

function TrendArrow({ trend }: { trend?: string }) {
  if (trend === "up" || trend === "new")
    return <TrendingUp size={14} className="text-teal-600" />;
  if (trend === "down") return <TrendingDown size={14} className="text-red-600" />;
  return <Minus size={14} className="text-slate-400" />;
}

function ChartTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="bg-white rounded-xl shadow-lg border border-slate-200 p-3 text-xs max-w-[240px]">
      <p className="font-bold text-ink mb-1">{label}</p>
      {payload.map((p: any, i: number) => (
        <div key={i} className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full" style={{ backgroundColor: p.color || p.stroke }} />
          <span className="text-ink-light font-medium truncate">
            {p.name}: <span className="text-ink font-bold">{p.value}</span>
          </span>
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Signal panel                                                       */
/* ------------------------------------------------------------------ */
function SignalPanel({
  title, subtitle, icon, items, accent, onPick, metric, tooltip,
}: {
  title: string;
  subtitle: string;
  icon: React.ReactNode;
  items: any[];
  accent: string;
  onPick: (id: string) => void;
  metric: (t: any) => React.ReactNode;
  tooltip?: string;
}) {
  return (
    <Card className="h-full">
      <div className="flex items-start gap-3 mb-3">
        <div className={`w-9 h-9 rounded-xl flex items-center justify-center shrink-0 ${accent}`}>{icon}</div>
        <div>
          <h3 className="flex items-center gap-1.5 text-sm font-extrabold text-ink leading-tight">{tooltip && <InfoTooltip content={tooltip} />}{title}</h3>
          <p className="text-[11px] text-ink-light font-medium">{subtitle}</p>
        </div>
      </div>
      {items && items.length ? (
        <div className="space-y-1.5">
          {items.slice(0, 5).map((t) => (
            <button
              key={t.theme_id}
              onClick={() => onPick(t.theme_id)}
              className="w-full flex items-center justify-between gap-2 p-2 rounded-lg hover:bg-slate-100 transition-colors text-left group"
            >
              <span className="text-xs font-semibold text-ink truncate group-hover:text-brand">{t.label}</span>
              <span className="shrink-0">{metric(t)}</span>
            </button>
          ))}
        </div>
      ) : (
        <p className="text-xs text-ink-light italic py-4 text-center">No signals in this category yet.</p>
      )}
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/*  Theme detail drawer                                                */
/* ------------------------------------------------------------------ */
function ThemeDrawer({ detail, loading, onClose }: { detail: any; loading: boolean; onClose: () => void }) {
  return (
    <AnimatePresence>
      <motion.div
        className="fixed inset-0 z-50 bg-black/30"
        initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
        onClick={onClose}
      >
        <motion.div
          className="absolute right-0 top-0 h-full w-full max-w-[560px] bg-slate-50 shadow-2xl overflow-y-auto"
          initial={{ x: 560 }} animate={{ x: 0 }} exit={{ x: 560 }}
          transition={{ type: "spring", damping: 30, stiffness: 300 }}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="sticky top-0 z-10 bg-brand-dark px-6 py-4 flex items-center justify-between">
            <div className="flex items-center gap-2 text-white">
              <Layers size={18} className="text-brand-light" />
              <span className="font-bold text-sm">Theme detail</span>
            </div>
            <button onClick={onClose} className="text-white/70 hover:text-white"><X size={20} /></button>
          </div>

          {loading || !detail ? (
            <div className="flex justify-center py-20"><Spinner size={28} /></div>
          ) : (
            <div className="p-6 space-y-5">
              <div>
                <div className="flex items-center gap-2 mb-1">
                  <h2 className="text-xl font-extrabold text-ink">{detail.theme.label}</h2>
                  <CategoryBadge category={detail.theme.category} />
                </div>
                <p className="text-sm text-ink-light">{detail.theme.description}</p>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <Stat label="Mentions" value={detail.count} icon={<Hash size={14} />} />
                <Stat
                  label="Avg sentiment"
                  value={detail.avg_sentiment !== null ? detail.avg_sentiment.toFixed(2) : "N/A"}
                  icon={<Activity size={14} />}
                />
                <Stat label="Scored" value={detail.scored_count} icon={<FileText size={14} />} />
              </div>

              {detail.timeseries?.length > 1 && (
                <Card title="Volume & sentiment over time">
                  <ResponsiveContainer width="100%" height={200}>
                    <ComposedChart data={detail.timeseries}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#E2E8F0" />
                      <XAxis dataKey="period" fontSize={11} tick={{ fill: "#64748B" }} />
                      <YAxis yAxisId="l" fontSize={11} tick={{ fill: "#64748B" }} allowDecimals={false} />
                      <YAxis yAxisId="r" orientation="right" domain={[-1, 1]} fontSize={11} tick={{ fill: "#64748B" }} />
                      <Tooltip content={<ChartTooltip />} />
                      <Area yAxisId="l" type="monotone" dataKey="count" name="Mentions" stroke="#0F766E" fill="#0F766E" fillOpacity={0.15} strokeWidth={2} />
                      <Line yAxisId="r" type="monotone" dataKey="avg_sentiment" name="Sentiment" stroke="#EA580C" strokeWidth={2} dot={false} connectNulls />
                    </ComposedChart>
                  </ResponsiveContainer>
                </Card>
              )}

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <Breakdown title="By model" data={detail.by_llm} />
                <Breakdown title="By persona" data={detail.by_persona} />
              </div>

              {detail.theme.keywords?.length > 0 && (
                <Card title="Matched keywords">
                  <div className="flex flex-wrap gap-1.5">
                    {detail.theme.keywords.map((k: string) => (
                      <span key={k} className="px-2 py-0.5 rounded-md bg-brand-surface text-[11px] font-medium text-ink">{k}</span>
                    ))}
                  </div>
                </Card>
              )}

              <Card title={`Representative responses (${detail.samples?.length || 0})`}>
                <div className="space-y-3">
                  {(detail.samples || []).map((s: any) => (
                    <div key={s.response_id} className="p-3 rounded-xl bg-white border border-slate-200">
                      <div className="flex items-center justify-between mb-1.5">
                        <span className="text-[11px] font-bold text-ink uppercase tracking-wide">{s.llm_name} · {s.persona}</span>
                        <SentimentBadge score={s.sentiment_score} />
                      </div>
                      {s.question_text && <p className="text-[11px] text-ink-light italic mb-1">Q: {s.question_text}</p>}
                      <p className="text-xs text-ink leading-relaxed">{s.snippet}</p>
                      {s.matched_keywords?.length > 0 && (
                        <div className="flex flex-wrap gap-1 mt-2">
                          {s.matched_keywords.map((k: string) => (
                            <span key={k} className="px-1.5 py-0.5 rounded bg-teal-50 text-[10px] font-medium text-teal-700">{k}</span>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </Card>
            </div>
          )}
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}

function Breakdown({ title, data }: { title: string; data: Record<string, number> }) {
  const entries = Object.entries(data || {}).sort((a, b) => b[1] - a[1]);
  const max = entries.length ? entries[0][1] : 1;
  return (
    <Card title={title}>
      <div className="space-y-2">
        {entries.map(([k, v]) => (
          <div key={k}>
            <div className="flex justify-between text-[11px] font-medium text-ink-light mb-0.5">
              <span className="truncate">{k}</span><span>{v}</span>
            </div>
            <div className="h-1.5 rounded-full bg-slate-100 overflow-hidden">
              <div className="h-full rounded-full bg-brand-light" style={{ width: `${(v / max) * 100}%` }} />
            </div>
          </div>
        ))}
        {!entries.length && <p className="text-xs text-ink-light italic">No data.</p>}
      </div>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/*  Insights page                                                      */
/* ------------------------------------------------------------------ */
const PERSONA_OPTS = ["All Personas", "Prospect", "Patient", "Provider"] as const;
const PERSONA_COLORS: Record<string, string> = {
  Prospect: "#0284C7",
  Patient: "#0F766E",
  Provider: "#7C3AED",
};

const EMPTY_TA: TaSelection = { area: "", indication: "", brand: "", disease: "" };

export default function Insights() {
  const [status, setStatus] = useState<any>(null);
  const [themes, setThemes] = useState<any[]>([]);
  const [signals, setSignals] = useState<any>(null);
  const [trends, setTrends] = useState<any>(null);
  const [rebuilding, setRebuilding] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<any>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [personaFilter, setPersonaFilter] = useState<string>("All Personas");
  const [taSelection, setTaSelection] = useState<TaSelection>(EMPTY_TA);
  const [taFilters, setTaFilters] = useState<TaFilters>({});
  const [runsCount, setRunsCount] = useState<number>(0);
  const [positioning, setPositioning] = useState<any>(null);
  const pollRef = useRef<any>(null);

  const activePersona = personaFilter === "All Personas" ? undefined : personaFilter;

  function refreshAll(persona?: string, filters?: TaFilters) {
    const f = filters ?? taFilters;
    api.insightsStatus().then(setStatus).catch(() => {});
    api.insightsThemes(persona, f).then((r) => setThemes(r.themes || [])).catch(() => {});
    api.insightsSignals(persona, f).then(setSignals).catch(() => {});
    api.insightsTrends(8, persona, f).then(setTrends).catch(() => {});
    // Global context for the headline narrative (run count + brand-absence rate).
    api.runs().then((r) => setRunsCount(r.length)).catch(() => {});
    api.positioning(f).then(setPositioning).catch(() => {});
  }

  function handleTaChange(next: TaSelection, filters: TaFilters) {
    setTaSelection(next);
    setTaFilters(filters);
    refreshAll(activePersona, filters);
  }

  useEffect(() => {
    refreshAll(activePersona);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [personaFilter]);

  useEffect(() => {
    if (!selected) { setDetail(null); return; }
    setDetailLoading(true);
    api.insightsThemeDetail(selected)
      .then(setDetail)
      .catch(() => setDetail(null))
      .finally(() => setDetailLoading(false));
  }, [selected]);

  async function rebuild() {
    const prevFinished = status?.rebuild?.finished_at ?? null;
    setRebuilding(true);
    try { await api.insightsRebuild(12); } catch { /* surfaced via status */ }
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const s = await api.insightsStatus();
        setStatus(s);
        if (!s?.rebuild?.running && s?.rebuild?.finished_at !== prevFinished) {
          clearInterval(pollRef.current); pollRef.current = null;
          setRebuilding(false);
          refreshAll(activePersona);
        }
      } catch { /* keep polling */ }
    }, 2500);
  }

  const hasThemes = (status?.themes ?? 0) > 0 && themes.length > 0;
  const rebuildError = status?.rebuild?.error;
  const coverage = status?.responses_total
    ? Math.round((status.responses_tagged / status.responses_total) * 100)
    : 0;

  return (
    <div className="space-y-8">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <PageHeader
          title="Insights"
          subtitle="Theme discovery, trend detection & signal extraction across every monitored response: the needles in the haystack."
          badge={
            <span className="inline-flex items-center gap-1 rounded-full bg-sky-50 border border-sky-200 px-2.5 py-0.5 text-xs font-semibold text-sky-700">
              <Snowflake size={12} /> Snowflake Views
            </span>
          }
        />
        <div className="flex flex-col items-end gap-3">
          {/* Persona tabs */}
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-1 p-1 rounded-xl bg-slate-100">
              {PERSONA_OPTS.map((opt) => (
                <button
                  key={opt}
                  onClick={() => setPersonaFilter(opt)}
                  className={`px-3 py-1.5 rounded-lg text-xs font-bold transition-all ${
                    personaFilter === opt
                      ? "bg-white shadow text-brand-dark"
                      : "text-ink-light hover:text-ink"
                  }`}
                  style={
                    personaFilter === opt && opt !== "All Personas"
                      ? { color: PERSONA_COLORS[opt] }
                      : undefined
                  }
                >
                  {opt === "All Personas" ? "All" : opt}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-1.5">
            <InfoTooltip content="Re-analyzes all stored AI responses to discover updated themes, trends, and signals. Takes about 1 minute. Run this after new monitoring runs complete." side="bottom" />
            <button
              onClick={rebuild}
              disabled={rebuilding}
              className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl bg-brand-dark text-white text-sm font-bold hover:bg-brand transition-colors disabled:opacity-60 shadow-sm"
            >
              {rebuilding ? <Spinner size={16} /> : <RefreshCw size={16} />}
              {rebuilding ? "Discovering themes…" : "Rebuild insights"}
            </button>
            </div>
          </div>
          {/* TA / Indication / Brand filter row */}
          <TaHierarchyFilter value={taSelection} onChange={handleTaChange} />
        </div>
      </div>

      {(activePersona || taSelection.area) && (
        <div className="flex flex-wrap items-center gap-2">
          {activePersona && (
            <div
              className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-bold border"
              style={{
                backgroundColor: `${PERSONA_COLORS[activePersona]}15`,
                borderColor: `${PERSONA_COLORS[activePersona]}40`,
                color: PERSONA_COLORS[activePersona],
              }}
            >
              <span className="w-2 h-2 rounded-full" style={{ backgroundColor: PERSONA_COLORS[activePersona] }} />
              {activePersona} persona
            </div>
          )}
          <TaFilterBadge value={taSelection} />
          <span className="text-xs text-ink-muted">· {themes.length} theme{themes.length !== 1 ? "s" : ""}</span>
        </div>
      )}

      {rebuildError && !rebuilding && (
        <div className="flex items-center gap-2 p-3 rounded-xl bg-red-50 border border-red-100 text-sm text-red-700 font-medium">
          <AlertTriangle size={16} /> Last rebuild failed: {rebuildError}
        </div>
      )}

      {/* Headline narrative — one plain-English takeaway across all runs */}
      {hasThemes && (() => {
        const dividedTopic = signals?.model_skew?.[0]?.label || signals?.dominant?.[0]?.label;
        const posData: Record<string, Record<string, number>> = positioning || {};
        let totalPos = 0, notMentioned = 0;
        for (const llmCounts of Object.values(posData)) {
          for (const [k, n] of Object.entries(llmCounts)) {
            totalPos += n;
            if (k === "NOT_MENTIONED") notMentioned += n;
          }
        }
        const absentPct = totalPos ? Math.round((notMentioned / totalPos) * 100) : 0;
        const parts: string[] = [];
        parts.push(`Across **${runsCount}** monitoring run${runsCount !== 1 ? "s" : ""}`);
        if (dividedTopic) parts.push(`AI platforms are most divided on **${dividedTopic}**`);
        if (totalPos > 0) parts.push(`and your brand is absent in **${absentPct}%** of all AI responses`);
        const takeaway = parts.join(", ") + ".";
        return (
          <div className="rounded-2xl border-2 border-brand/30 bg-brand-surface/50 p-5">
            <div className="flex items-start gap-3">
              <ScanEye size={18} className="text-brand-light shrink-0 mt-0.5" />
              <div>
                <p className="text-[11px] font-bold uppercase tracking-widest text-ink-light mb-1">The big picture</p>
                <p className="text-sm font-semibold text-ink leading-snug">
                  {takeaway.split("**").map((seg, i) =>
                    i % 2 === 1 ? <span key={i} className="text-brand-dark font-extrabold">{seg}</span> : <span key={i}>{seg}</span>
                  )}
                </p>
              </div>
            </div>
          </div>
        );
      })()}

      {/* Stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <AnimatedCard delay={0}><Stat label="Themes discovered" value={status?.themes ?? 0} icon={<Tags size={16} />} /></AnimatedCard>
        <AnimatedCard delay={0.05}><Stat label="Responses tagged" value={status?.responses_tagged ?? 0} sub={`${coverage}% of ${status?.responses_total ?? 0}`} icon={<FileText size={16} />} /></AnimatedCard>
        <AnimatedCard delay={0.1}><Stat label="Risk themes" value={signals?.risks?.length ?? 0} sub="negative sentiment" icon={<AlertTriangle size={16} />} /></AnimatedCard>
        <AnimatedCard delay={0.15}><Stat label="Taxonomy version" value={status?.taxonomy_version ?? 0} icon={<Layers size={16} />} tooltip="The version number of the theme classification system. A higher number means insights have been rebuilt more recently." /></AnimatedCard>
      </div>

      {!hasThemes ? (
        <Card>
          <EmptyState
            icon={<Tags size={36} />}
            message={
              rebuilding
                ? "Discovering themes across your responses… this runs in the background."
                : "No themes yet. Click 'Rebuild insights' to discover themes, trends, and signals across your response repository."
            }
          />
        </Card>
      ) : (
        <>
          {/* Signals */}
          <div>
            <h2 className="text-xs font-bold text-ink-light uppercase tracking-widest mb-3">
              {activePersona ? `${activePersona} signals` : "Extracted signals"}
            </h2>
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
              <AnimatedCard delay={0.05}>
                <SignalPanel
                  title="Risk themes" subtitle="Most negative sentiment"
                  icon={<AlertTriangle size={18} className="text-red-700" />} accent="bg-red-100"
                  items={signals?.risks || []} onPick={setSelected}
                  metric={(t) => <SentimentBadge score={t.avg_sentiment} />}
                  tooltip="Recurring topics where AI consistently speaks negatively about your brand. These require the most urgent attention."
                />
              </AnimatedCard>
              <AnimatedCard delay={0.1}>
                <SignalPanel
                  title="Emerging" subtitle="Rising in the last 7 days"
                  icon={<TrendingUp size={18} className="text-teal-700" />} accent="bg-teal-100"
                  items={signals?.emerging || []} onPick={setSelected}
                  metric={(t) => <span className="text-xs font-bold text-teal-700">+{t.recent_count}</span>}
                  tooltip="Topics that have appeared more frequently in AI responses over the last 7 days. Potential early signals to watch."
                />
              </AnimatedCard>
              <AnimatedCard delay={0.15}>
                <SignalPanel
                  title="Dominant" subtitle="Highest volume themes"
                  icon={<Crown size={18} className="text-amber-700" />} accent="bg-amber-100"
                  items={signals?.dominant || []} onPick={setSelected}
                  metric={(t) => <span className="text-xs font-bold text-ink">{t.count}</span>}
                  tooltip="Topics that appear most frequently across all AI responses. High volume means this is a key narrative AI is building around your brand."
                />
              </AnimatedCard>
              <AnimatedCard delay={0.2}>
                <SignalPanel
                  title="Model skew" subtitle="Driven by one model"
                  icon={<Network size={18} className="text-sky-700" />} accent="bg-sky-100"
                  items={signals?.model_skew || []} onPick={setSelected}
                  metric={(t) => <span className="text-xs font-bold text-sky-700">{Math.round(t.top_llm_share * 100)}%</span>}
                  tooltip="Topics where a single AI platform accounts for most of the responses. A high % means only one model is driving this narrative, not all of them."
                />
              </AnimatedCard>
            </div>
          </div>

          {/* Trend chart */}
          <AnimatedCard delay={0.1}>
            <Card title="Theme trends over time">
              {trends?.rows?.length > 1 ? (
                <ResponsiveContainer width="100%" height={320}>
                  <LineChart data={trends.rows}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#E2E8F0" />
                    <XAxis dataKey="period" fontSize={12} tick={{ fill: "#64748B" }} />
                    <YAxis fontSize={12} tick={{ fill: "#64748B" }} allowDecimals={false} />
                    <Tooltip content={<ChartTooltip />} />
                    <Legend wrapperStyle={{ fontSize: 11, fontWeight: 600 }} />
                    {(trends.themes || []).map((t: any, i: number) => (
                      <Line
                        key={t.theme_id}
                        type="monotone"
                        dataKey={t.theme_id}
                        name={t.label}
                        stroke={THEME_COLORS[i % THEME_COLORS.length]}
                        strokeWidth={2}
                        dot={{ r: 2 }}
                      />
                    ))}
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <EmptyState
                  icon={<Activity size={32} />}
                  message="Not enough history yet. Trends appear once responses span multiple days. Use the theme cards below for current totals."
                />
              )}
            </Card>
          </AnimatedCard>

          {/* Theme grid */}
          <div>
            <h2 className="text-xs font-bold text-ink-light uppercase tracking-widest mb-3">
              {activePersona ? `${activePersona} themes` : "All themes"} ({themes.length})
            </h2>
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
              {themes.map((t, i) => (
                <AnimatedCard key={t.theme_id} delay={Math.min(i * 0.03, 0.3)}>
                  <button
                    onClick={() => setSelected(t.theme_id)}
                    className="w-full text-left bg-canvas-card rounded-2xl border border-slate-200/80 shadow-sm p-5 hover:border-brand-light/50 hover:shadow-md transition-all h-full"
                  >
                    <div className="flex items-start justify-between gap-2 mb-2">
                      <h3 className="text-sm font-extrabold text-ink leading-tight">{t.label}</h3>
                      <CategoryBadge category={t.category} />
                    </div>
                    <p className="text-xs text-ink-light leading-relaxed line-clamp-2 mb-3">{t.description}</p>
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-3">
                        <span className="inline-flex items-center gap-1 text-xs font-bold text-ink">
                          <Hash size={12} className="text-ink-light" />{t.count}
                        </span>
                        <span className="inline-flex items-center gap-1">
                          <TrendArrow trend={t.trend} />
                        </span>
                      </div>
                      <div
                        className="text-xs font-bold px-2 py-1 rounded-full"
                        style={{ backgroundColor: `${sentColor(t.avg_sentiment)}1A`, color: sentColor(t.avg_sentiment) }}
                      >
                        {t.avg_sentiment !== null ? t.avg_sentiment.toFixed(2) : "N/A"}
                      </div>
                    </div>
                  </button>
                </AnimatedCard>
              ))}
            </div>
          </div>
        </>
      )}

      {/* Drawer */}
      {selected && <ThemeDrawer detail={detail} loading={detailLoading} onClose={() => setSelected(null)} />}
    </div>
  );
}
