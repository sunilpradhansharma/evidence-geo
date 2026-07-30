import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  X,
  AlertTriangle,
  Shield,
  Download,
  Filter,
  Search,
  ChevronRight,
  MessageSquare,
  CheckCircle2,
  XCircle,
  ShieldCheck,
  TrendingUp,
  DollarSign,
  ExternalLink,
} from "lucide-react";
import { api, PRELAUNCH_LABEL, ResponseItem, Question, Run } from "../api/client";
import { TA_GROUPS, BRAND_OPTIONS, DISEASE_OPTIONS, DISEASE_BRAND_MAP, DESIGNATION_OPTIONS, brandsForIndication, diseasesForIndication } from "../lib/taxonomy";
import {
  Card,
  PageHeader,
  Select,
  MultiSelect,
  Markdown,
  SentimentBadge,
  PositionBadge,
  IntentBadge,
  ConsensusBadge,
  CONSENSUS_LABELS,
  EmptyState,
  AnimatedCard,
  Stat,
  InfoTooltip,
  ThemeBadge,
} from "../components/ui";
import { ResponseDetailDrawer } from "../components/ResponseDetailDrawer";

const INTENTS = ["", "CLINICAL", "EXPERIENTIAL", "SCREENING", "SHORTHAND"];
const CONSENSUS_LEVELS = ["", "FULL", "PARTIAL", "MISSING"];

const POSITIONS = ["FIRST_LINE_RECOMMENDED", "AMONG_OPTIONS", "SECOND_LINE", "NOT_RECOMMENDED", "NOT_MENTIONED"];
const POS_COLORS: Record<string, string> = { FIRST_LINE_RECOMMENDED: "#0F766E", AMONG_OPTIONS: "#0284C7", SECOND_LINE: "#EA580C", NOT_RECOMMENDED: "#DC2626", NOT_MENTIONED: "#94A3B8" };
const INTENT_COLORS: Record<string, string> = { CLINICAL: "#0D4F4F", EXPERIENTIAL: "#0284C7", SCREENING: "#EA580C", SHORTHAND: "#64748B" };
const CONSENSUS_COLORS: Record<string, string> = { FULL: "#0F766E", PARTIAL: "#EA580C", MISSING: "#DC2626" };

// Demo-only quick filters (Workshop Questions + Rheumatology Only) share a subtle
// amber treatment (matching the Approved Question Bank) so it's clear at a glance
// that these two toggles are curated shortcuts kept for the demo.
const demoFilterCls = (active: boolean) =>
  `flex items-center gap-2 px-4 py-2.5 rounded-xl text-xs font-bold transition-colors ${
    active ? "bg-amber-500 text-white" : "text-amber-800 bg-amber-100 hover:bg-amber-200"
  }`;

const RUN_STATUS_LABELS: Record<string, string> = {
  PENDING: "Pending",
  RUNNING: "Running",
  COMPLETED: "Completed",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
  PAUSED_BUDGET: "Paused: budget limit reached",
  AWAITING_OPENEVIDENCE: "Paused: waiting for clinician input",
};
const runStatusLabel = (s: string) =>
  RUN_STATUS_LABELS[s] ?? s.replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());

const RUN_TRIGGER_LABELS: Record<string, string> = {
  ADHOC: "On-demand run",
  SCHEDULED: "Scheduled run",
  SCHEDULE: "Scheduled run",
  CSV: "CSV upload run",
};
const runTriggerLabel = (t: string) =>
  RUN_TRIGGER_LABELS[t] ?? t.replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());

/* ------------------------------------------------------------------ */
/*  Chart tooltip                                                      */
/* ------------------------------------------------------------------ */
function ChartTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="bg-white rounded-xl shadow-lg border border-slate-200 p-3 text-xs">
      <p className="font-bold text-ink mb-1">{label}</p>
      {payload.map((p: any, i: number) => (
        <div key={i} className="flex items-center gap-2"><div className="w-2 h-2 rounded-full" style={{ backgroundColor: p.color }} /><span className="text-ink-light font-medium">{p.name}: <span className="text-ink font-bold">{typeof p.value === "number" ? (Number.isInteger(p.value) ? p.value : p.value.toFixed(3)) : p.value}</span></span></div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Run Summary Mini-Dashboard                                         */
/* ------------------------------------------------------------------ */
function RunSummary({ summary }: { summary: any }) {
  const run = summary.run;
  const sentimentByLlm = summary.sentiment_by_llm || [];
  const positioningData = Object.entries(summary.positioning_by_llm || {}).map(([llm, counts]: any) => ({ llm, ...counts }));
  const consensusData = Object.entries(summary.consensus_by_level || {}).map(([level, count]: any) => ({ level, count }));
  const intentData = Object.entries(summary.intent_by_type || {}).map(([name, value]: any) => ({ name, value }));

  return (
    <div className="space-y-6">
      {/* Run metadata banner */}
      <div className="bg-brand-surface rounded-2xl p-5 border border-brand-light/20">
        <div className="flex items-center justify-between mb-1">
          <div className="flex items-center gap-3">
            <span className={`flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-bold ${run.status === "COMPLETED" ? "bg-teal-100 text-teal-800" : run.status === "CANCELLED" ? "bg-slate-200 text-ink-light" : "bg-amber-100 text-amber-800"}`}>
              {run.status === "COMPLETED" ? <CheckCircle2 size={12} /> : <XCircle size={12} />}
              {runStatusLabel(run.status)}
            </span>
            <span className="text-sm font-bold text-ink">{runTriggerLabel(run.trigger)}</span>
            <span className="text-xs text-ink-light font-medium">{new Date(run.started_at).toLocaleString()}</span>
            {run.ended_at && <span className="text-xs text-ink-muted font-medium">→ {new Date(run.ended_at).toLocaleString()}</span>}
          </div>
          <span className="text-xs font-mono text-ink-muted">{run.run_id.slice(0, 12)}...</span>
        </div>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-4">
        <AnimatedCard delay={0}><Stat label="Questions" value={run.questions_attempted} icon={<MessageSquare size={16} />} /></AnimatedCard>
        <AnimatedCard delay={0.05}><Stat label="Successful" value={run.responses_success} icon={<CheckCircle2 size={16} />} /></AnimatedCard>
        <AnimatedCard delay={0.1}><Stat label="Failed" value={run.responses_failed} icon={<XCircle size={16} />} /></AnimatedCard>
        <AnimatedCard delay={0.15}><Stat label="Cost" value={`$${run.estimated_cost_usd.toFixed(4)}`} icon={<DollarSign size={16} />} /></AnimatedCard>
        <AnimatedCard delay={0.2}><Stat label="Alerts" value={summary.alerts?.total ?? 0} icon={<AlertTriangle size={16} />} /></AnimatedCard>
      </div>

      {/* Consensus breakdown bar */}
      {consensusData.length > 0 && (
        <Card title="Consensus Breakdown">
          <div className="flex items-center gap-2 mb-3">
            {consensusData.map((c) => {
              const total = consensusData.reduce((a, x) => a + x.count, 0);
              const pct = total > 0 ? Math.round((c.count / total) * 100) : 0;
              return (
                <div key={c.level} className="flex items-center gap-2">
                  <div className="h-8 rounded-lg" style={{ width: `${Math.max(pct * 3, 30)}px`, backgroundColor: CONSENSUS_COLORS[c.level] || "#94A3B8" }} />
                  <div className="text-xs"><span className="font-bold text-ink">{c.count}</span> <span className="text-ink-light font-medium">{c.level} ({pct}%)</span></div>
                </div>
              );
            })}
          </div>
          {summary.geo_fallback_count > 0 && (
            <div className="flex items-center gap-2 text-teal-700 text-xs p-3 bg-teal-50 rounded-xl font-semibold mt-2">
              <ShieldCheck size={14} /> GEO fallback used for {summary.geo_fallback_count} evaluation(s)
            </div>
          )}
        </Card>
      )}

      {/* Charts row */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6">
        {/* Sentiment by LLM */}
        <AnimatedCard delay={0.1}>
          <Card title="Sentiment by LLM">
            {sentimentByLlm.length > 0 ? (
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={sentimentByLlm}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#E2E8F0" />
                  <XAxis dataKey="key" fontSize={11} fontWeight={600} tick={{ fill: "#1A1A2E" }} />
                  <YAxis domain={[-1, 1]} fontSize={11} tick={{ fill: "#64748B" }} />
                  <Tooltip content={<ChartTooltip />} />
                  <Bar dataKey="avg_sentiment" radius={[6, 6, 0, 0]} name="Avg Sentiment">
                    {sentimentByLlm.map((d: any, i: number) => <Cell key={i} fill={d.avg_sentiment >= 0 ? "#0F766E" : "#DC2626"} />)}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            ) : <EmptyState message="No sentiment data." />}
          </Card>
        </AnimatedCard>

        {/* Positioning */}
        <AnimatedCard delay={0.15}>
          <Card title="Competitive Positioning">
            {positioningData.length > 0 ? (
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={positioningData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#E2E8F0" />
                  <XAxis dataKey="llm" fontSize={11} fontWeight={600} tick={{ fill: "#1A1A2E" }} />
                  <YAxis fontSize={11} tick={{ fill: "#64748B" }} />
                  <Tooltip content={<ChartTooltip />} />
                  {POSITIONS.map((p) => <Bar key={p} dataKey={p} stackId="a" fill={POS_COLORS[p]} />)}
                </BarChart>
              </ResponsiveContainer>
            ) : <EmptyState message="No positioning data." />}
          </Card>
        </AnimatedCard>

        {/* Intent donut */}
        <AnimatedCard delay={0.2}>
          <Card title="Intent Distribution">
            {intentData.length > 0 ? (
              <ResponsiveContainer width="100%" height={200}>
                <PieChart>
                  <Pie data={intentData} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={75} innerRadius={40} label={({ name, percent }: any) => `${name} ${(percent * 100).toFixed(0)}%`} labelLine={{ stroke: "#64748B" }}>
                    {intentData.map((d: any, i: number) => <Cell key={i} fill={INTENT_COLORS[d.name] || "#94A3B8"} />)}
                  </Pie>
                  <Tooltip content={<ChartTooltip />} />
                </PieChart>
              </ResponsiveContainer>
            ) : <EmptyState message="No intent data." />}
          </Card>
        </AnimatedCard>
      </div>
    </div>
  );
}

/* Response detail drawer + tooltip builder live in components/ResponseDetailDrawer.tsx (shared with Phrasing Variation Testing). */


/* ------------------------------------------------------------------ */
/*  Side-by-side comparison view                                       */
/* ------------------------------------------------------------------ */
function buildCompareQuestionTooltip(a0: any): string {
  const lines: string[] = [];
  const intentMap: Record<string, string> = {
    CLINICAL:     "\u2022 Clinical: treatment, dosing, outcomes",
    EXPERIENTIAL: "\u2022 Experiential: side effects, quality of life",
    SCREENING:    "\u2022 Screening: eligibility, diagnosis",
    SHORTHAND:    "\u2022 Shorthand: quick brand lookup",
  };
  const intentKey = (a0?.intent_type || "").toUpperCase();
  if (intentKey && intentMap[intentKey]) {
    lines.push("Question Type:", intentMap[intentKey]);
  }
  const consensusMap: Record<string, string> = {
    FULL:    "\u2022 Full: all 5 platforms agreed",
    PARTIAL: "\u2022 Partial: some platforms diverged",
    MISSING: "\u2022 Missing: significant disagreement, GEO fallback may apply",
  };
  if (a0?.consensus_level && consensusMap[a0.consensus_level]) {
    if (lines.length) lines.push("");
    lines.push("Models in Agreement:", consensusMap[a0.consensus_level]);
  }
  return lines.join("\n");
}

function buildCompareCardTooltip(a: any): string {
  const lines: string[] = [];
  const s = a.sentiment_score;
  if (s != null) {
    const desc =
      s > 0 ? "\u2022 Positive (> 0): AI speaks favorably about this brand"
      : s < 0 ? "\u2022 Negative (< 0): AI discourages or downplays this brand"
      : "\u2022 Neutral (\u22480): balanced or neutral coverage";
    lines.push("Sentiment: \u22121.0 to +1.0", desc);
  } else {
    lines.push("Sentiment: unscored", "\u2022 Score pending: scoring runs asynchronously after the LLM responds and fills in automatically within a few minutes.");
  }
  const posMap: Record<string, string> = {
    FIRST_LINE_RECOMMENDED: "\u2022 Leading: first-choice recommendation",
    AMONG_OPTIONS:          "\u2022 Present: mentioned among options",
    SECOND_LINE:            "\u2022 Backup: secondary option",
    NOT_RECOMMENDED:        "\u2022 Not Endorsed: actively avoided",
    NOT_MENTIONED:          "\u2022 Absent: not mentioned",
  };
  if (a.competitive_position && posMap[a.competitive_position]) {
    lines.push("", "Position: brand\u2019s rank in treatment options", posMap[a.competitive_position]);
  }
  return lines.join("\n");
}

function CompareView({ questionId }: { questionId: string }) {
  const [comparison, setComparison] = useState<any>(null);
  useEffect(() => { if (questionId) api.compare(questionId).then(setComparison).catch(() => setComparison(null)); }, [questionId]);
  if (!comparison || !comparison.answers?.length) return <Card><EmptyState message="Select a question to compare model responses." /></Card>;
  return (
    <div className="space-y-4">
      <Card accent>
        <p className="text-sm font-bold text-ink mb-3">{comparison.question_text}</p>
        <div className="flex items-center gap-2 flex-wrap">
          <InfoTooltip content={buildCompareQuestionTooltip(comparison.answers[0])} />
          {comparison.answers[0]?.intent_type && <IntentBadge intent={comparison.answers[0].intent_type} />}
          {comparison.answers[0]?.consensus_level && <ConsensusBadge level={comparison.answers[0].consensus_level} />}
        </div>
      </Card>
      {comparison.answers[0]?.consensus_level && comparison.answers[0].consensus_level !== "FULL" && (
        <div className="flex items-center gap-3 p-4 rounded-2xl bg-amber-50 border border-amber-200"><Shield size={18} className="text-amber-600 flex-shrink-0" /><div><span className="text-sm font-bold text-amber-800">Consensus: {comparison.answers[0].consensus_level}</span><p className="text-xs text-amber-700 font-medium mt-0.5">Models diverged on this question. GEO verified schema data may have been used as fallback.</p></div></div>
      )}
      <div className="grid gap-4 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3">
        {comparison.answers.map((a: any) => (
          <Card key={a.response_id}>
            <div className="flex items-center gap-2 mb-3">
              <div className="w-2.5 h-2.5 rounded-full bg-brand-light" />
              <h4 className="text-sm font-extrabold text-ink">{a.llm_name}</h4>
            </div>
            <div className="flex items-center gap-2 mb-4 flex-wrap">
              <InfoTooltip content={buildCompareCardTooltip(a)} />
              <SentimentBadge score={a.sentiment_score} />
              <PositionBadge position={a.competitive_position} />
            </div>
            <div className="max-h-96 overflow-y-auto pr-2"><Markdown>{a.response_text}</Markdown></div>
          </Card>
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Main Results page                                                  */
/* ------------------------------------------------------------------ */
export default function Results() {
  const [searchParams, setSearchParams] = useSearchParams();
  const urlRunId = searchParams.get("run_id") || "";
  const urlMode = (searchParams.get("mode") === "compare" ? "compare" : "browse") as "browse" | "compare";
  const urlQuestionId = searchParams.get("question_id") || "";
  const urlAlertOnly = searchParams.get("alert_only") === "true";
  const urlSearch = searchParams.get("q") || "";

  const [allRuns, setAllRuns] = useState<Run[]>([]);
  const [selectedRunId, setSelectedRunId] = useState(urlRunId);
  const [runSummary, setRunSummary] = useState<any>(null);
  const [summaryLoading, setSummaryLoading] = useState(false);

  const [mode, setMode] = useState<"browse" | "compare">(urlMode);
  const [items, setItems] = useState<ResponseItem[]>([]);
  const [total, setTotal] = useState(0);
  const [llms, setLlms] = useState<string[]>([]);
  const [questions, setQuestions] = useState<Question[]>([]);
  const [selectedQid, setSelectedQid] = useState(urlQuestionId);
  const [filters, setFilters] = useState({ llm_name: "", persona: "", therapeutic_area: [] as string[], brand_focus: "", disease: "", intent_type: "", consensus_level: "", designation: [] as string[], alert_only: urlAlertOnly });
  const [selected, setSelected] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [searchText, setSearchText] = useState(urlSearch);
  const [compareFilters, setCompareFilters] = useState({ persona: "", therapeutic_area: "", intent: "", search: "" });
  // "Workshop Questions" filter: scope responses (and the compare question list) to the
  // curated workshop set (Rhem.csv) + their variations, matched server-side by question_id.
  const [analystOnly, setAnalystOnly] = useState(false);

  // Sync URL param
  useEffect(() => {
    if (urlRunId && urlRunId !== selectedRunId) setSelectedRunId(urlRunId);
  }, [urlRunId]);

  // Load runs list
  useEffect(() => { api.runs().then(setAllRuns).catch(() => {}); }, []);

  // Load run summary when run selected
  useEffect(() => {
    if (!selectedRunId) { setRunSummary(null); return; }
    setSummaryLoading(true);
    api.runSummary(selectedRunId).then(setRunSummary).catch(() => setRunSummary(null)).finally(() => setSummaryLoading(false));
  }, [selectedRunId]);

  // Build query string for responses
  const buildQs = () => {
    const p = new URLSearchParams();
    if (selectedRunId) p.set("run_id", selectedRunId);
    if (filters.llm_name) p.set("llm_name", filters.llm_name);
    if (filters.persona) p.set("persona", filters.persona);
    // Multi-select: repeat the param once per selected value so the list + download
    // narrow to the union of the chosen therapeutic areas / designations.
    filters.therapeutic_area.forEach((ta) => p.append("therapeutic_area", ta));
    if (filters.brand_focus) p.set("brand_focus", filters.brand_focus);
    if (filters.intent_type) p.set("intent_type", filters.intent_type);
    if (filters.consensus_level) p.set("consensus_level", filters.consensus_level);
    filters.designation.forEach((d) => p.append("designation", d));
    if (filters.alert_only) p.set("alert_only", "true");
    if (analystOnly) p.set("analyst", "1");
    p.set("limit", "500");
    return `?${p.toString()}`;
  };

  const loadResponses = (quiet = false) => {
    if (!quiet) setLoading(true);
    api.responses(buildQs())
      .then((r) => { setItems(r.items); setTotal(r.total); })
      .catch(() => {})
      .finally(() => { if (!quiet) setLoading(false); });
  };

  useEffect(() => {
    api.llmComparison().then((d) => setLlms(d.map((x: any) => x.llm_name))).catch(() => {});
  }, []);

  useEffect(() => {
    api.questions(`?limit=500${analystOnly ? "&analyst=1" : ""}`).then(setQuestions).catch(() => {});
  }, [analystOnly]);

  useEffect(() => { loadResponses(); }, [filters, selectedRunId, analystOnly]);

  // Post-run scoring is asynchronous, so a just-finished run shows rows as
  // "unscored" for a short while. Poll quietly until every SUCCESS/TRUNCATED row
  // has a score (or we hit the safety cap) so sentiment/position fill in live
  // without the user needing a manual hard refresh.
  const pendingScores = items.some(
    (i) => (i.status === "SUCCESS" || i.status === "TRUNCATED") && i.sentiment_score == null
  );

  useEffect(() => {
    if (!pendingScores) return;
    const intervalMs = 5000;
    const maxMs = 3 * 60 * 1000; // stop after 3 min to avoid endless polling
    let elapsed = 0;
    const id = window.setInterval(() => {
      elapsed += intervalMs;
      if (elapsed >= maxMs) { window.clearInterval(id); return; }
      loadResponses(true);
      if (selectedRunId) api.runSummary(selectedRunId).then(setRunSummary).catch(() => {});
    }, intervalMs);
    return () => window.clearInterval(id);
  }, [pendingScores, selectedRunId, filters]);

  const handleRunChange = (runId: string) => {
    setSelectedRunId(runId);
    const p = new URLSearchParams(searchParams);
    if (runId) p.set("run_id", runId); else p.delete("run_id");
    setSearchParams(p, { replace: true });
  };

  const openDetail = (id: string) => api.responseDetail(id).then(setSelected).catch(() => {});

  const filteredItems = items
    .filter((i) => !searchText || i.question_text.toLowerCase().includes(searchText.toLowerCase()))
    .filter((i) => !filters.disease || (DISEASE_BRAND_MAP[filters.disease] ?? []).includes(i.brand_focus ?? ""));

  // Demo quick-filter: "Rheumatology Only" pins the (multi-select) therapeutic-area
  // filter to just Rheumatology; toggling off clears it. Composes with Workshop Questions.
  const rheumOnly = filters.therapeutic_area.length === 1 && filters.therapeutic_area[0] === "Rheumatology";

  // Brand + Disease dropdowns scope to the selected therapeutic area(s) — union across the
  // multi-select. Areas with no AbbVie focus brand (e.g. Obesity) or an empty selection fall back
  // to the full lists so the field is never a dead end. "" keeps "All" selectable.
  const taScopedBrands = [...new Set(filters.therapeutic_area.flatMap((ta) => brandsForIndication(ta)))];
  const brandFilterOptions = taScopedBrands.length ? ["", ...taScopedBrands] : BRAND_OPTIONS;
  const taScopedDiseases = [...new Set(filters.therapeutic_area.flatMap((ta) => diseasesForIndication(ta)))];
  const diseaseFilterOptions = taScopedDiseases.length ? ["", ...taScopedDiseases] : DISEASE_OPTIONS;

  // For compare mode: derive unique questions from loaded responses
  const runQuestions = selectedRunId
    ? Array.from(new Map(items.map((i) => [i.question_id, { question_id: i.question_id, question_text: i.question_text, persona: i.persona, brand_focus: i.brand_focus, therapeutic_area: i.therapeutic_area, domain: i.domain, intent_type: i.intent_type } as any])).values())
    : questions;

  return (
    <div className="space-y-6">
      {/* Header + controls */}
      <div className="flex items-center justify-between">
        <PageHeader title="AI Response Review" subtitle={`${selectedRunId ? `Run-scoped view: ${total} responses` : `${total} responses across all runs`}${pendingScores ? " · scoring in progress, auto-refreshing…" : ""}`} />
        <div className="flex items-center gap-3">
          <div className="flex bg-slate-100 rounded-xl p-1">
            <button onClick={() => setMode("browse")} className={`px-4 py-2 rounded-lg text-xs font-bold transition-colors ${mode === "browse" ? "bg-white text-ink shadow-sm" : "text-ink-light"}`}>Review</button>
            <button onClick={() => setMode("compare")} className={`px-4 py-2 rounded-lg text-xs font-bold transition-colors ${mode === "compare" ? "bg-white text-ink shadow-sm" : "text-ink-light"}`}>Compare Platforms</button>
          </div>
          <button
            onClick={() => setAnalystOnly((s) => !s)}
            title="Demo shortcut: show only responses to the workshop question set (from Rhem.csv) and their variations"
            className={demoFilterCls(analystOnly)}
          >
            <Filter size={14} /> Workshop Questions
          </button>
          <button
            onClick={() => setFilters({ ...filters, therapeutic_area: rheumOnly ? [] : ["Rheumatology"] })}
            title="Demo shortcut: show only Rheumatology responses"
            className={demoFilterCls(rheumOnly)}
          >
            <Filter size={14} /> Rheumatology Only
          </button>
          <a href={api.exportUrl("csv", buildQs().replace("?", "&"))} className="flex items-center gap-2 px-4 py-2.5 bg-brand text-white rounded-xl text-xs font-bold hover:bg-brand-dark transition-colors"><Download size={14} /> Download</a>
        </div>
      </div>

      {/* Run selector */}
      <Card>
        <label className="text-xs font-bold text-ink-light uppercase tracking-wide mb-2 block">Select Run</label>
        <select value={selectedRunId} onChange={(e) => handleRunChange(e.target.value)} className="w-full border border-slate-200 rounded-xl px-4 py-3 text-sm bg-white text-ink font-medium focus:outline-none focus:ring-2 focus:ring-brand-light/40">
          <option value="">All Runs (global view)</option>
          {allRuns.map((r) => (
            <option key={r.run_id} value={r.run_id}>
              {runStatusLabel(r.status)} · {runTriggerLabel(r.trigger)} · {new Date(r.started_at).toLocaleString()} · {r.questions_attempted}Q · {r.responses_success} ok
            </option>
          ))}
        </select>
      </Card>

      {/* Per-run summary dashboard */}
      {selectedRunId && (
        summaryLoading ? (
          <div className="py-12 text-center text-ink-light font-medium">Loading run summary...</div>
        ) : runSummary ? (
          <RunSummary summary={runSummary} />
        ) : null
      )}

      {/* Browse / Compare */}
      {mode === "compare" ? (
        <div className="space-y-4">
          {/* Compare filters */}
          <Card>
            <div className="flex gap-4 flex-wrap items-end mb-4">
              <Select label="Persona" value={compareFilters.persona} options={["", "Patient", "Prospect", "Provider"]} onChange={(v) => setCompareFilters({ ...compareFilters, persona: v })} tooltip={"Who is asking the question:\n• Prospect: exploring treatment options\n• Patient: currently on treatment\n• Provider: a clinician"} />
              <Select label="Therapeutic Area & Indication" value={compareFilters.therapeutic_area} groups={TA_GROUPS} onChange={(v) => setCompareFilters({ ...compareFilters, therapeutic_area: v })} tooltip={"The disease area or specific indication this question targets.\nExamples: Dermatology, Gastroenterology, Oncology, Endometriosis."} />
              <Select label="Intent" value={compareFilters.intent} options={INTENTS} onChange={(v) => setCompareFilters({ ...compareFilters, intent: v })} tooltip={"The question category assigned by the AI classifier:\n• Clinical: treatment, dosing, outcomes\n• Experiential: side effects, quality of life\n• Screening: eligibility, diagnosis\n• Shorthand: quick brand lookup"} />
              <div className="flex flex-col">
                <label className="text-xs font-semibold text-ink-light mb-1.5 uppercase tracking-wide">Search</label>
                <div className="relative"><Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-ink-muted" /><input type="text" value={compareFilters.search} onChange={(e) => setCompareFilters({ ...compareFilters, search: e.target.value })} placeholder="Filter questions..." className="border border-slate-200 rounded-xl pl-9 pr-3 py-2 text-sm bg-white text-ink font-medium focus:outline-none focus:ring-2 focus:ring-brand-light/40 w-52" /></div>
              </div>
            </div>
            <label className="text-xs font-bold text-ink-light uppercase tracking-wide mb-2 block">Select a Question</label>
            <select value={selectedQid} onChange={(e) => setSelectedQid(e.target.value)} className="w-full border border-slate-200 rounded-xl px-4 py-3 text-sm bg-white text-ink font-medium focus:outline-none focus:ring-2 focus:ring-brand-light/40">
              <option value="">Select a question</option>
              {runQuestions
                .filter((q: any) => {
                  if (compareFilters.persona && q.persona !== compareFilters.persona) return false;
                  if (compareFilters.therapeutic_area && q.therapeutic_area !== compareFilters.therapeutic_area) return false;
                  if (compareFilters.intent && q.intent_type !== compareFilters.intent) return false;
                  if (compareFilters.search && !q.question_text.toLowerCase().includes(compareFilters.search.toLowerCase())) return false;
                  return true;
                })
                .map((q: any) => <option key={q.question_id} value={q.question_id}>{q.question_text.slice(0, 90)}</option>)}
            </select>
          </Card>
          {selectedQid && <CompareView questionId={selectedQid} />}
        </div>
      ) : (
        <>
          <Card>
            <div className="flex gap-4 flex-wrap items-end">
              <Select label="Persona" value={filters.persona} options={["", "Patient", "Prospect", "Provider"]} onChange={(v) => setFilters({ ...filters, persona: v })} tooltip={"Who is asking the question:\n• Prospect: exploring treatment options\n• Patient: currently on treatment\n• Provider: a clinician"} />
              <Select label="AI Platform" value={filters.llm_name} options={["", ...llms]} onChange={(v) => setFilters({ ...filters, llm_name: v })} tooltip={"Filter responses by a specific AI platform.\nOptions: Claude, Nova-Pro, Llama, Gemini, GPT-4o, EvidenceMD."} />
              <MultiSelect label="Therapeutic Area & Indication" values={filters.therapeutic_area} groups={TA_GROUPS} onChange={(v) => {
                // Reset a now-invalid brand/disease when the area selection changes so they don't
                // silently combine into an empty result (e.g. Humira + Neuroscience).
                const brands = v.flatMap((ta) => brandsForIndication(ta));
                const diseases = v.flatMap((ta) => diseasesForIndication(ta));
                setFilters({
                  ...filters,
                  therapeutic_area: v,
                  brand_focus: brands.length && filters.brand_focus && !brands.includes(filters.brand_focus) ? "" : filters.brand_focus,
                  disease: diseases.length && filters.disease && !diseases.includes(filters.disease) ? "" : filters.disease,
                });
              }} tooltip={"Filter by one or more disease areas / indications.\nThe on-screen table and the CSV download both narrow to your selection."} />
              <Select label="Brand" value={filters.brand_focus} options={brandFilterOptions} onChange={(v) => setFilters({ ...filters, brand_focus: v })} tooltip={"Filter responses by the AbbVie brand being monitored. Scoped to the selected therapeutic area."} />
              <Select label="Disease" value={filters.disease} options={diseaseFilterOptions} onChange={(v) => setFilters({ ...filters, disease: v })} tooltip={"Filter by specific disease or indication.\nNarrows results to brands that treat the selected condition."} />
              <Select label="Intent" value={filters.intent_type} options={INTENTS} onChange={(v) => setFilters({ ...filters, intent_type: v })} tooltip={"The question category assigned by the AI classifier:\n• Clinical: treatment, dosing, outcomes\n• Experiential: side effects, quality of life\n• Screening: eligibility, diagnosis\n• Shorthand: quick brand lookup"} />
              <Select label="Consensus" value={filters.consensus_level} options={CONSENSUS_LEVELS} optionLabels={CONSENSUS_LABELS} onChange={(v) => setFilters({ ...filters, consensus_level: v })} tooltip={"How much the 5 AI platforms agreed on their response:\n• All platforms agree: every platform agreed\n• Partial agreement: some platforms diverged\n• No consensus: significant disagreement"} />
              <MultiSelect label="Designation" values={filters.designation} options={DESIGNATION_OPTIONS} onChange={(v) => setFilters({ ...filters, designation: v })} tooltip={"Workshop designation (Persona + indication) from the curated workshop set:\nPatient RA, Patient PsA, HCP RA, HCP PsA, HCP RA & PsA.\nSelecting one or more scopes the table and adds a Designation column to the CSV download."} />
              <div className="flex flex-col">
                <label className="text-xs font-semibold text-ink-light mb-1.5 uppercase tracking-wide">Search</label>
                <div className="relative"><Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-ink-muted" /><input type="text" value={searchText} onChange={(e) => setSearchText(e.target.value)} placeholder="Search questions..." className="border border-slate-200 rounded-xl pl-9 pr-3 py-2 text-sm bg-white text-ink font-medium focus:outline-none focus:ring-2 focus:ring-brand-light/40 w-48" /></div>
              </div>
              <label className="flex items-center gap-2 text-sm text-ink font-semibold pb-1"><input type="checkbox" checked={filters.alert_only} onChange={(e) => setFilters({ ...filters, alert_only: e.target.checked })} className="rounded border-slate-300" />Alerts only</label>
            </div>
          </Card>
          <Card>
            {loading ? (
              <div className="py-16 text-center text-ink-light font-medium">Loading…</div>
            ) : filteredItems.length === 0 ? (
              <EmptyState message="No responses found. Trigger a run from the Pipeline page." />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[960px] text-sm">
                  <thead>
                    <tr className="text-left border-b-2 border-slate-200">
                      <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">AI Platform</th>
                      <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">Focus</th>
                      <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">Question</th>
                      <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">
                        <span className="inline-flex items-center gap-1"><InfoTooltip content={"The category of the question:\n\n• Clinical: treatment / dosing outcomes\n• Experiential: side effects / quality of life\n• Screening: eligibility / diagnosis\n• Shorthand: quick brand lookup"} /> Question Type</span>
                      </th>
                      <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">
                        <span className="inline-flex items-center gap-1"><InfoTooltip content={"How many of the 5 AI platforms reached the same conclusion:\n\n• Full: all agreed\n• Partial: some disagreed\n• Missing: significant disagreement, no consensus reached"} /> Models in Agreement</span>
                      </th>
                      <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">
                        <span className="inline-flex items-center gap-1"><InfoTooltip content={"How positively or negatively AI speaks about this brand.\n\nScore range:\n• +1.0: very positive\n• 0: neutral\n• −1.0: very negative"} /> Sentiment</span>
                      </th>
                      <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">
                        <span className="inline-flex items-center gap-1"><InfoTooltip content={"Where AI places this brand in the treatment hierarchy:\n\n• Leading: first choice\n• Among options: mentioned as a viable option\n• Mentioned, not recommended first: secondary option\n• Not Endorsed: actively avoided\n• Not appearing in AI answers: not mentioned"} /> How Brand Is Positioned</span>
                      </th>
                      <th className="pb-3"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredItems.map((r, i) => (
                      <motion.tr key={r.response_id} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: Math.min(i * 0.02, 0.5) }} onClick={() => openDetail(r.response_id)} className="border-b border-slate-100 hover:bg-brand-surface/50 cursor-pointer transition-colors group">
                        <td className="py-3 font-bold text-ink">{r.llm_name}</td>
                        <td className="py-3 font-medium text-ink">{r.monitoring_mode === "DISEASE_STATE" ? <span className="px-2 py-0.5 rounded-full bg-violet-100 text-violet-700 text-[11px] font-bold">Disease state</span> : r.brand_focus}</td>
                        <td className="py-3 max-w-[160px] lg:max-w-xs xl:max-w-md truncate text-ink-light font-medium">{r.question_text}</td>
                        <td className="py-3"><IntentBadge intent={r.intent_type} /></td>
                        <td className="py-3"><ConsensusBadge level={r.consensus_level} /></td>
                        <td className="py-3"><SentimentBadge score={r.sentiment_score} /></td>
                        <td className="py-3"><PositionBadge position={r.competitive_position} /></td>
                        <td className="py-3"><div className="flex items-center gap-2">{r.alert_triggered && <AlertTriangle size={14} className="text-red-500" />}<ChevronRight size={14} className="text-ink-muted opacity-0 group-hover:opacity-100 transition-opacity" /></div></td>
                      </motion.tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}

      <AnimatePresence>{selected && <ResponseDetailDrawer detail={selected} onClose={() => setSelected(null)} />}</AnimatePresence>
    </div>
  );
}
