import { useCallback, useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  Area,
  AreaChart,
  LabelList,
  ReferenceLine,
} from "recharts";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  ClipboardList,
  Layers,
  Lightbulb,
  MessageSquare,
  ShieldCheck,
  Snowflake,
  TrendingDown,
  TrendingUp,
  Users,
  Zap,
} from "lucide-react";
import { Link } from "react-router-dom";
import { api, BrandMatrix, PRELAUNCH_LABEL, type LandscapeMatrix, type TaFilters } from "../api/client";
import { TaHierarchyFilter, type TaSelection } from "../components/TaHierarchyFilter";
import { Card, Stat, PageHeader, AnimatedCard, EmptyState, PositionBadge, InfoTooltip } from "../components/ui";

/* ------------------------------------------------------------------ */
/*  Color palettes                                                     */
/* ------------------------------------------------------------------ */
const POSITIONS = ["FIRST_LINE_RECOMMENDED", "AMONG_OPTIONS", "SECOND_LINE", "NOT_RECOMMENDED", "NOT_MENTIONED"];
const POS_COLORS: Record<string, string> = {
  FIRST_LINE_RECOMMENDED: "#0F766E",
  AMONG_OPTIONS: "#0284C7",
  SECOND_LINE: "#EA580C",
  NOT_RECOMMENDED: "#DC2626",
  NOT_MENTIONED: "#94A3B8",
};

/* Short, scannable chart labels (`label`) paired with a plain-English
   explanation (`desc`) shown in the prominent "What each color means" key.
   Ordered best → worst so the hierarchy reads top-to-bottom. */
const POSITION_LEGEND = [
  { key: "FIRST_LINE_RECOMMENDED", color: "#0F766E", label: "Leading",        desc: "AI recommends this first" },
  { key: "AMONG_OPTIONS",          color: "#0284C7", label: "In the mix",      desc: "Mentioned, but not featured as the top choice" },
  { key: "SECOND_LINE",            color: "#EA580C", label: "Backup",          desc: "Named only as a secondary or fallback option" },
  { key: "NOT_RECOMMENDED",        color: "#DC2626", label: "Flagged against", desc: "AI actively steers away from this brand" },
  { key: "NOT_MENTIONED",          color: "#94A3B8", label: "Missing",         desc: "Not appearing in AI answers at all" },
];
const POSITION_LEGEND_BY_KEY: Record<string, { label: string; color: string; desc: string }> =
  Object.fromEntries(POSITION_LEGEND.map((p) => [p.key, p]));

/* Keyed by the actual rule names emitted by the backend alert engine:
   LOW_SENTIMENT | NOT_RECOMMENDED | COMPETITOR_ADVANTAGE. */
const ALERT_INTERPRETATIONS: Record<string, { what: string; why: string; next: string }> = {
  LOW_SENTIMENT: {
    what: "AI is framing your brand negatively",
    why: "Responses scored below −0.3. AI is using cautious or negative language about your brand for these questions.",
    next: "Open these responses and publish clear, patient-language content that addresses the negative themes.",
  },
  NOT_RECOMMENDED: {
    what: "AI is steering away from your brand",
    why: "AI explicitly positions your brand as not recommended for these questions; the highest-priority positioning risk.",
    next: "Audit your clinical claims and GEO schema, and compare against competitor coverage in these responses.",
  },
  COMPETITOR_ADVANTAGE: {
    what: "A competitor is winning the recommendation",
    why: "AI platforms are describing a rival brand more favorably than yours for these questions.",
    next: "See which competitor leads and strengthen your GEO content and evidence claims for those topics.",
  },
};

/* Friendly title for any alert rule, even ones without a full interpretation. */
const alertTitle = (rule: string) =>
  ALERT_INTERPRETATIONS[rule]?.what ??
  rule.replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());

const WORST_ACTION_DETAIL: Record<string, { issue: string; why: string; action: string }> = {
  NOT_MENTIONED: {
    issue: "Brand is absent from AI responses for this question.",
    why: "AI platforms are routing patients and providers to other brands or generic answers. Your brand lacks structured content coverage for this topic. This is a GEO gap.",
    action: "Audit your llms.txt and JSON-LD schema to ensure this topic is covered. Create dedicated, citable content that directly addresses this question. AI models respond to structured, authoritative sources.",
  },
  NOT_RECOMMENDED: {
    issue: "AI platforms are actively steering away from this brand.",
    why: "AI is citing safety concerns, insufficient efficacy data, or stronger competitor claims for this question. This is the highest-priority positioning risk and requires immediate investigation.",
    action: "Review the full AI responses in AI Response Review to identify what's driving the negative recommendation. Check if competitor GEO coverage or clinical citations are outweighing your brand's content for this indication.",
  },
  SECOND_LINE: {
    issue: "Brand is recognized but consistently positioned as a fallback option.",
    why: "AI models see the brand as a valid alternative but not the first choice. A competitor or generic treatment is being ranked ahead for this specific indication.",
    action: "Identify which brand ranks above yours for this question and analyze its GEO advantage. Strengthen first-line clinical evidence claims in your GEO schema and update llms.txt with primary-line rationale for this indication.",
  },
  AMONG_OPTIONS: {
    issue: "Brand is present but not leading the recommendation.",
    why: "AI mentions the brand as viable but another brand captures the primary recommendation. This is a positioning opportunity. AI already has awareness of your brand, but not differentiated preference.",
    action: "Publish comparative clinical evidence that highlights differentiated value for this question type. Ensure your llms.txt includes first-line clinical rationale and head-to-head data that supports leading with your brand.",
  },
  FIRST_LINE_RECOMMENDED: {
    issue: "Brand is performing well for this question.",
    why: "AI platforms are consistently recommending your brand as the first choice. However, this position can shift across runs as AI models are updated.",
    action: "Set up run-to-run monitoring for this question. Any drop in consensus level or sentiment score here is an early warning signal. Investigate immediately before it becomes a trend.",
  },
};

const PERSONA_COLORS: Record<string, string> = {
  Prospect: "#0284C7",
  Patient: "#0F766E",
  Provider: "#7C3AED",
};
const INTENT_COLORS: Record<string, string> = {
  CLINICAL: "#0D4F4F",
  EXPERIENTIAL: "#0284C7",
  SCREENING: "#EA580C",
  SHORTHAND: "#64748B",
};

/* Plain-English "why this matters" framing for each question intent type. */
const INTENT_MEANING: Record<string, { color: string; label: string; desc: string }> = {
  CLINICAL: { color: "#0D4F4F", label: "Clinical", desc: "Treatment, dosing & outcome questions, where clinical evidence claims matter most." },
  EXPERIENTIAL: { color: "#0284C7", label: "Experiential", desc: "Side-effects & quality-of-life questions; patient-voice content drives the narrative here." },
  SCREENING: { color: "#EA580C", label: "Screening", desc: "Eligibility & diagnosis questions, early in the journey, before brand preference forms." },
  SHORTHAND: { color: "#64748B", label: "Shorthand", desc: "Quick brand look-ups; high-intent moments to ensure your brand appears first." },
};
const CONSENSUS_COLORS: Record<string, string> = {
  FULL: "#0F766E",
  PARTIAL: "#EA580C",
  MISSING: "#DC2626",
};

const CHART_TEAL = "#14B8A6";

/* ------------------------------------------------------------------ */
/*  Shared chart styling — keeps every card visually consistent and    */
/*  removes clutter: horizontal gridlines only, no axis/tick lines,    */
/*  capped bar widths, and value labels for at-a-glance reading.       */
/* ------------------------------------------------------------------ */
const GRID_PROPS = { strokeDasharray: "3 3", stroke: "#EEF2F6", vertical: false } as const;
const AXIS_PROPS = { fontSize: 12, tickLine: false, axisLine: false } as const;
const TICK_INK = { fill: "#1A1A2E", fontWeight: 600 } as const;
const TICK_MUTED = { fill: "#64748B" } as const;
const TOOLTIP_CURSOR = { fill: "rgba(15,118,110,0.06)" } as const;
const BAR_LABEL = { fill: "#475569", fontSize: 11, fontWeight: 700 } as const;
const fmt2 = (v: any) => (typeof v === "number" ? v.toFixed(2) : v);

/* ------------------------------------------------------------------ */
/*  Brand Brief (rule-based headline)                                  */
/* ------------------------------------------------------------------ */
function BrandBrief({ sentiment, positioning, alerts, consensus }: {
  sentiment: any; positioning: any; alerts: any; consensus: any;
}) {
  if (!sentiment && !positioning && !alerts) return null;

  const byLlm: any[] = sentiment?.by_llm || [];
  const allSents = byLlm.map((d: any) => d.avg_sentiment).filter((s: any) => s != null);
  const avgSent = allSents.length ? allSents.reduce((a: number, b: number) => a + b, 0) / allSents.length : null;

  const posData: Record<string, Record<string, number>> = positioning || {};
  let totalPos = 0, firstLine = 0, notMentioned = 0, notRecommended = 0;
  for (const llmCounts of Object.values(posData)) {
    for (const [key, n] of Object.entries(llmCounts as Record<string, number>)) {
      totalPos += n;
      if (key === "FIRST_LINE_RECOMMENDED") firstLine += n;
      if (key === "NOT_MENTIONED") notMentioned += n;
      if (key === "NOT_RECOMMENDED") notRecommended += n;
    }
  }
  const firstLinePct = totalPos ? Math.round((firstLine / totalPos) * 100) : 0;
  const absentPct   = totalPos ? Math.round((notMentioned / totalPos) * 100) : 0;
  const badPosPct   = totalPos ? Math.round(((notMentioned + notRecommended) / totalPos) * 100) : 0;

  const totalAlerts = alerts?.total_alerts ?? 0;
  const totalEvals  = consensus?.total_evaluations ?? 0;
  const missingCons = consensus?.by_level?.MISSING ?? 0;
  const missingPct  = totalEvals ? Math.round((missingCons / totalEvals) * 100) : 0;

  let tone: "positive" | "mixed" | "concerning";
  if (avgSent !== null && avgSent > 0.2 && firstLinePct >= 40) tone = "positive";
  else if (avgSent !== null && avgSent < 0 || badPosPct > 40) tone = "concerning";
  else tone = "mixed";

  const disagree = missingPct > 20 ? ` AI platforms reach no consensus on ${missingPct}% of questions.` : "";
  const headlines: Record<typeof tone, string> = {
    positive:    `AI platforms are representing your brand favorably. It's recommended first in ${firstLinePct}% of responses.${disagree}`,
    mixed:       `AI platforms give your brand mixed coverage: recommended first in ${firstLinePct}% of responses, yet absent in ${absentPct}%.${disagree}`,
    concerning:  `AI platforms are cautious or absent on your brand. It's missing or flagged against in ${badPosPct}% of responses${totalAlerts > 0 ? `, with ${totalAlerts} alert${totalAlerts !== 1 ? "s" : ""} active` : ""}.`,
  };

  const toneStyle: Record<typeof tone, { border: string; icon: string; badge: string }> = {
    positive:   { border: "border-teal-300 bg-teal-50",   icon: "text-teal-600",  badge: "bg-teal-100 text-teal-800" },
    mixed:      { border: "border-amber-300 bg-amber-50",  icon: "text-amber-600", badge: "bg-amber-100 text-amber-800" },
    concerning: { border: "border-red-300 bg-red-50",      icon: "text-red-600",   badge: "bg-red-100 text-red-700" },
  };
  const style = toneStyle[tone];

  const pills = [
    tone === "positive"   && `${firstLinePct}% leading`,
    tone !== "positive"  && absentPct > 10 && `${absentPct}% absent`,
    missingPct > 20      && `${missingPct}% no consensus`,
  ].filter(Boolean) as string[];

  return (
    <AnimatedCard delay={0}>
      <div className={`rounded-2xl border-2 ${style.border} p-5`}>
        <div className="flex items-start gap-3">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-1 flex-wrap">
              <span className="text-sm font-bold uppercase tracking-widest text-ink-light flex items-center gap-1">
                <InfoTooltip content="An automatically generated summary of how AI platforms are representing your brand right now, based on sentiment, positioning, and alert data." />
                AI Brand Brief
              </span>
              {pills.map((p) => (
                <span key={p} className={`px-2 py-0.5 rounded-full text-[10px] font-bold ${style.badge}`}>{p}</span>
              ))}
            </div>
            <p className="text-sm font-bold text-ink leading-snug">{headlines[tone]}</p>
            {missingPct > 20 && (
              <p className="text-xs text-ink-light mt-1.5 font-medium">Models disagree significantly: {missingPct}% of evaluations reached no consensus.</p>
            )}
          </div>
        </div>
      </div>
    </AnimatedCard>
  );
}

/* ------------------------------------------------------------------ */
/*  Needs Attention / Action Items panel                               */
/* ------------------------------------------------------------------ */
function NeedsAttention({ questions, isLoading }: { questions: any[]; isLoading: boolean }) {
  if (isLoading) {
    return (
      <AnimatedCard delay={0.05}>
        <div className="rounded-2xl border-2 border-brand/30 bg-slate-50 overflow-hidden shadow-sm">
          <div className="bg-brand-dark px-5 py-3.5 flex items-center gap-2.5 text-white">
            <ClipboardList size={17} />
            <span className="text-sm font-extrabold tracking-wide">Recommended Next Steps</span>
          </div>
          <div className="p-6 flex items-center justify-center gap-3 text-ink-muted text-sm">
            <svg className="animate-spin h-4 w-4 text-brand" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
            </svg>
            Loading recommendations…
          </div>
        </div>
      </AnimatedCard>
    );
  }

  if (!questions.length) {
    return (
      <AnimatedCard delay={0.05}>
        <div className="rounded-2xl border-2 border-slate-200 bg-slate-50 overflow-hidden shadow-sm">
          <div className="bg-slate-600 px-5 py-3.5 flex items-center gap-2.5 text-white">
            <ClipboardList size={17} />
            <span className="text-sm font-extrabold tracking-wide">Recommended Next Steps</span>
          </div>
          <div className="p-6 text-center text-sm text-ink-muted">
            <CheckCircle2 className="w-8 h-8 text-teal-400 mx-auto mb-2" />
            <p className="font-semibold text-ink">No critical questions flagged right now.</p>
            <p className="text-xs mt-1">Run an analysis to generate scored responses, or broaden your filters.</p>
          </div>
        </div>
      </AnimatedCard>
    );
  }

  const PRIORITY_COLORS = ["bg-red-600", "bg-orange-500", "bg-amber-500"];

  return (
    <AnimatedCard delay={0.05}>
      <div className="rounded-2xl border-2 border-brand/30 bg-slate-50 overflow-hidden shadow-sm">
        {/* Header bar */}
        <div className="bg-brand-dark px-5 py-3.5 flex items-center justify-between">
          <div className="flex items-center gap-2.5 text-white">
            <ClipboardList size={17} />
            <span className="text-sm font-extrabold tracking-wide">Recommended Next Steps</span>
            <span className="text-white/85 text-xs font-medium">· top {questions.length} questions that need your attention</span>
          </div>
          <span className="inline-flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-widest text-white/90 bg-white/15 px-2.5 py-1 rounded-full"><InfoTooltip content="Questions ranked #1–3 by severity. #1 is the most urgent: a combination of negative sentiment, weak AI positioning, and model disagreement." side="bottom" iconClassName="text-white/70 hover:text-white transition-colors shrink-0" /> Priority ranked</span>
        </div>

        {/* Cards */}
        <div className="p-4 grid grid-cols-1 md:grid-cols-3 gap-4">
          {questions.map((q: any, i: number) => {
            const sent = q.avg_sentiment ?? 0;
            const sentColor = sent > 0.2 ? "text-teal-700" : sent < -0.2 ? "text-red-700" : "text-amber-700";
            const sentLabel = sent > 0.2 ? "Positive" : sent < -0.2 ? "Negative" : "Mixed";
            const sentChip = sent > 0.2 ? "bg-teal-100 text-teal-700" : sent < -0.2 ? "bg-red-100 text-red-700" : "bg-amber-100 text-amber-700";
            const sentDot = sent > 0.2 ? "#0F766E" : sent < -0.2 ? "#DC2626" : "#EA580C";
            const detail = WORST_ACTION_DETAIL[q.dominant_position] ?? {
              issue: "This question has a low composite performance score across AI platforms.",
              why:   "A combination of negative sentiment, weak positioning, and model disagreement is flagging this as a gap.",
              action: "Review the full responses in AI Response Review to diagnose the root cause.",
            };
            return (
              <div key={q.question_id} className="bg-white rounded-xl border border-slate-200 shadow-sm flex flex-col overflow-hidden">
                {/* Card top bar with priority number */}
                <div className="flex items-center gap-2 px-4 pt-4 pb-2">
                  <span className={`text-[10px] font-extrabold text-white ${PRIORITY_COLORS[i] || "bg-slate-500"} w-5 h-5 rounded-full flex items-center justify-center shrink-0`}>
                    {i + 1}
                  </span>
                  <span className="text-[10px] font-bold uppercase tracking-widest text-ink-light">{q.persona}</span>
                  {q.alert_count > 0 && (
                    <Link
                      to={`/results?alert_only=true&q=${encodeURIComponent(q.question_text)}`}
                      className="ml-auto flex items-center gap-1 px-2 py-0.5 rounded-full bg-red-100 text-red-700 text-[10px] font-bold hover:bg-red-200 transition-colors"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <AlertTriangle size={9} /> {q.alert_count} alert{q.alert_count !== 1 ? "s" : ""}
                    </Link>
                  )}
                </div>

                {/* Question */}
                <p className="text-sm font-semibold text-ink leading-snug px-4 pb-3 line-clamp-3">{q.question_text}</p>

                {/* Scores */}
                <div className="flex items-center gap-2 flex-wrap px-4 pb-3">
                  <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-bold ${sentChip}`}>
                    <span className="h-2 w-2 rounded-full" style={{ backgroundColor: sentDot }} />
                    {sentLabel}
                  </span>
                  <span className={`text-xs font-bold ${sentColor}`}>{sent.toFixed(2)} sentiment</span>
                  <PositionBadge position={q.dominant_position} />
                </div>

                {/* Diagnosis */}
                <div className="mx-4 mb-3 rounded-lg bg-red-50 border border-red-100 p-4 space-y-2">
                  <p className="text-sm font-bold text-red-700 leading-relaxed">{detail.issue}</p>
                  <p className="text-[13px] text-red-700/80 leading-relaxed">{detail.why}</p>
                </div>

                {/* Action */}
                <div className="mx-4 mb-4 rounded-lg bg-brand-surface border-2 border-brand/30 p-3.5 space-y-1.5">
                  <div className="flex items-center gap-1.5 text-brand-dark">
                    <Lightbulb size={15} className="shrink-0" />
                    <span className="text-xs font-extrabold uppercase tracking-widest">Recommended action</span>
                  </div>
                  <p className="text-[13px] text-ink font-semibold leading-snug">{detail.action}</p>
                </div>

                {/* CTA */}
                <div className="mt-auto px-4 pb-4">
                  <Link
                    to={`/results?mode=compare&question_id=${q.question_id}`}
                    className="inline-flex items-center gap-1 text-[11px] font-bold text-brand hover:text-brand-dark"
                    onClick={(e) => e.stopPropagation()}
                  >
                    View full AI responses <ArrowRight size={11} />
                  </Link>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </AnimatedCard>
  );
}

/* ------------------------------------------------------------------ */
/*  Alert interpretation panel                                         */
/* ------------------------------------------------------------------ */
function AlertsPanel({ alerts }: { alerts: any }) {
  if (!alerts?.by_rule || !Object.keys(alerts.by_rule).length) {
    return <Card title="Active alerts" className="h-full"><EmptyState message="No alerts triggered yet." /></Card>;
  }
  return (
    <Card title="Active alerts" className="h-full">
      <div className="space-y-3">
        {Object.entries(alerts.by_rule).map(([rule, count]: any) => {
          const interp = ALERT_INTERPRETATIONS[rule];
          return (
            <motion.div
              key={rule}
              className="rounded-xl border border-red-100 bg-red-50 p-4 space-y-2"
              whileHover={{ scale: 1.005 }}
            >
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2 text-red-700">
                  <AlertTriangle size={15} />
                  <span className="text-sm font-bold">{alertTitle(rule)}</span>
                </div>
                <span className="text-lg font-extrabold text-red-700 shrink-0">{count}</span>
              </div>
              <p className="text-xs text-red-700 font-medium">
                {interp?.why ?? "These responses triggered a monitoring alert and need review."}
              </p>
              <div className="rounded-lg bg-white/70 border border-red-100 p-2.5">
                <p className="text-[10px] font-bold uppercase tracking-widest text-ink-light mb-0.5">What to do</p>
                <p className="text-xs text-ink font-semibold leading-snug">
                  {interp?.next ?? "Open the flagged responses in AI Response Review to diagnose the issue."}
                </p>
                <Link
                  to="/results?alert_only=true"
                  className="mt-1.5 inline-flex items-center gap-1 text-[11px] font-bold text-brand hover:text-brand-dark"
                >
                  Review flagged responses <ArrowRight size={11} />
                </Link>
              </div>
            </motion.div>
          );
        })}
      </div>
    </Card>
  );
}

const PERSONAS = ["Prospect", "Patient", "Provider"] as const;
type PersonaTab = "All" | (typeof PERSONAS)[number];

/* ------------------------------------------------------------------ */
/*  Custom tooltip                                                     */
/* ------------------------------------------------------------------ */
function CustomTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="bg-white rounded-xl shadow-xl ring-1 ring-slate-900/5 border border-slate-100 px-3.5 py-2.5 text-xs">
      {label != null && label !== "" && <p className="font-bold text-ink mb-1.5">{label}</p>}
      <div className="space-y-1">
        {payload.map((p: any, i: number) => (
          <div key={i} className="flex items-center gap-2">
            <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: p.color || p.payload?.fill }} />
            <span className="text-ink-light font-medium">{p.name}: <span className="text-ink font-bold">{typeof p.value === "number" && !Number.isInteger(p.value) ? p.value.toFixed(2) : p.value}</span></span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Persona tab bar                                                    */
/* ------------------------------------------------------------------ */
function MonitoringModeToggle({ mode, onChange }: { mode: "BRAND" | "DISEASE_STATE"; onChange: (m: "BRAND" | "DISEASE_STATE") => void }) {
  const opts: { key: "BRAND" | "DISEASE_STATE"; label: string }[] = [
    { key: "BRAND", label: "AbbVie" },
    { key: "DISEASE_STATE", label: "All Brands" },
  ];
  return (
    <div className="flex items-center gap-1 p-1 rounded-xl bg-slate-100 w-fit">
      {opts.map((o) => (
        <button
          key={o.key}
          onClick={() => onChange(o.key)}
          className={`px-4 py-1.5 rounded-lg text-xs font-bold transition-all ${
            mode === o.key ? "bg-white shadow text-brand-dark" : "text-ink-light hover:text-ink"
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function PersonaTabBar({ active, onChange }: { active: PersonaTab; onChange: (t: PersonaTab) => void }) {
  const tabs: PersonaTab[] = ["All", "Prospect", "Patient", "Provider"];
  return (
    <div className="flex items-center gap-1 p-1 rounded-xl bg-slate-100 w-fit">
      {tabs.map((t) => (
        <button
          key={t}
          onClick={() => onChange(t)}
          className={`px-4 py-1.5 rounded-lg text-xs font-bold transition-all ${
            active === t
              ? "bg-white shadow text-brand-dark"
              : "text-ink-light hover:text-ink"
          }`}
          style={active === t && t !== "All" ? { color: PERSONA_COLORS[t] } : undefined}
        >
          {t}
        </button>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Persona KPI row (shown when a specific persona is selected)       */
/* ------------------------------------------------------------------ */
function PersonaKpiRow({ personaData }: { personaData: any }) {
  const d = personaData;
  if (!d) return null;

  const sentAvg = d.sentiment?.avg;
  const sentColor = sentAvg === null || sentAvg === undefined
    ? "#94A3B8"
    : sentAvg > 0.2 ? "#0F766E" : sentAvg < -0.2 ? "#DC2626" : "#EA580C";

  const totalPos = d.response_count || 1;
  const firstLinePct = d.positioning?.FIRST_LINE_RECOMMENDED
    ? Math.round((d.positioning.FIRST_LINE_RECOMMENDED / totalPos) * 100)
    : 0;

  const fullPct = d.consensus?.full_pct ?? 0;

  const alertRate = d.alert_rate?.rate ?? 0;
  const alertPct = Math.round(alertRate * 100);

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
      <AnimatedCard delay={0}>
        <div className="h-full bg-canvas-card rounded-2xl border border-slate-200/80 shadow-sm p-5">
          <p className="flex items-center gap-1 text-[11px] font-bold text-ink-light uppercase tracking-widest mb-1"><InfoTooltip content="Average sentiment score across all AI responses. +1.0 = very positive, −1.0 = very negative, 0 = neutral." /> Avg Sentiment</p>
          <p className="text-2xl font-extrabold" style={{ color: sentColor }}>
            {sentAvg !== null && sentAvg !== undefined ? sentAvg.toFixed(2) : "N/A"}
          </p>
          <div className="flex items-center gap-3 mt-2 text-[11px] text-ink-light font-medium">
            <span className="text-teal-600 font-bold">+{d.sentiment?.positive ?? 0}</span>
            <span className="text-slate-400">·</span>
            <span className="text-slate-500">{d.sentiment?.neutral ?? 0} neutral</span>
            <span className="text-slate-400">·</span>
            <span className="text-red-600 font-bold">-{d.sentiment?.negative ?? 0}</span>
          </div>
        </div>
      </AnimatedCard>

      <AnimatedCard delay={0.05}>
        <div className="h-full bg-canvas-card rounded-2xl border border-slate-200/80 shadow-sm p-5">
          <p className="flex items-center gap-1 text-[11px] font-bold text-ink-light uppercase tracking-widest mb-1"><InfoTooltip content="% of AI responses where this brand was recommended as the first-choice option. Higher is better." /> First-Line Rate</p>
          <p className="text-2xl font-extrabold text-teal-700">{firstLinePct}%</p>
          <p className="text-[11px] text-ink-light mt-2 font-medium">
            {d.positioning?.FIRST_LINE_RECOMMENDED ?? 0} of {totalPos} responses
          </p>
          <div className="mt-2 h-1.5 rounded-full bg-slate-100 overflow-hidden">
            <div className="h-full rounded-full bg-teal-500 transition-all" style={{ width: `${firstLinePct}%` }} />
          </div>
        </div>
      </AnimatedCard>

      <AnimatedCard delay={0.1}>
        <div className="h-full bg-canvas-card rounded-2xl border border-slate-200/80 shadow-sm p-5">
          <p className="flex items-center gap-1 text-[11px] font-bold text-ink-light uppercase tracking-widest mb-1"><InfoTooltip content="% of questions where all 5 AI platforms fully agreed on their response. Below 60% signals brand messaging gaps." /> Consensus Quality</p>
          <p className="text-2xl font-extrabold text-teal-700">{fullPct}%</p>
          <p className="text-[11px] text-ink-light mt-2 font-medium">
            {d.consensus?.FULL ?? 0} full · {d.consensus?.PARTIAL ?? 0} partial · {d.consensus?.MISSING ?? 0} missing
          </p>
          <div className="mt-2 h-1.5 rounded-full bg-slate-100 overflow-hidden">
            <div className="h-full rounded-full bg-teal-500 transition-all" style={{ width: `${fullPct}%` }} />
          </div>
        </div>
      </AnimatedCard>

      <AnimatedCard delay={0.15}>
        <div className={`h-full bg-canvas-card rounded-2xl border shadow-sm p-5 ${alertPct > 10 ? "border-red-200 bg-red-50" : "border-slate-200/80"}`}>
          <p className="flex items-center gap-1 text-[11px] font-bold text-ink-light uppercase tracking-widest mb-1"><InfoTooltip content="% of AI responses that triggered a monitoring alert (e.g. negative sentiment, brand absent). Above 10% requires immediate attention." /> Alert Rate</p>
          <p className={`text-2xl font-extrabold ${alertPct > 10 ? "text-red-700" : "text-ink"}`}>{alertPct}%</p>
          <p className="text-[11px] text-ink-light mt-2 font-medium">
            {d.alert_rate?.alerts ?? 0} alerts / {d.alert_rate?.responses ?? 0} responses
          </p>
          {alertPct > 10 && (
            <p className="text-[10px] font-bold text-red-600 mt-1 flex items-center gap-1">
              <AlertTriangle size={10} /> Above 10% threshold
            </p>
          )}
        </div>
      </AnimatedCard>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Persona Comparison chart (All tab)                                 */
/* ------------------------------------------------------------------ */
function PersonaComparisonChart({ personaData, taSelection }: { personaData: any; taSelection: TaSelection }) {
  if (!personaData) return <EmptyState message="No persona data yet." />;

  const sentData = PERSONAS.map((p) => ({
    persona: p,
    "Avg Sentiment": personaData[p]?.sentiment?.avg ?? 0,
  }));

  const totalsByPersona = PERSONAS.map((p) => {
    const total = personaData[p]?.response_count || 1;
    const pos = personaData[p];
    return {
      persona: p,
      "First-Line %": pos?.positioning?.FIRST_LINE_RECOMMENDED
        ? Math.round((pos.positioning.FIRST_LINE_RECOMMENDED / total) * 100)
        : 0,
      "Consensus Full %": pos?.consensus?.full_pct ?? 0,
      "Alert Rate %": Math.round((pos?.alert_rate?.rate ?? 0) * 100),
    };
  });

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <Card title={<span className="flex items-center">Avg Sentiment by Persona<FilteredBadge selection={taSelection} /></span>}>
        <ResponsiveContainer width="100%" height={220}>
          <BarChart data={sentData} margin={{ top: 24, right: 8, left: -12, bottom: 0 }}>
            <CartesianGrid {...GRID_PROPS} />
            <XAxis dataKey="persona" {...AXIS_PROPS} tick={{ ...TICK_INK, fontWeight: 700 }} tickMargin={10} />
            <YAxis domain={[-1, 1]} ticks={[-1, -0.5, 0, 0.5, 1]} {...AXIS_PROPS} tick={TICK_MUTED} tickMargin={6} />
            <Tooltip cursor={TOOLTIP_CURSOR} content={<CustomTooltip />} />
            <ReferenceLine y={0} stroke="#CBD5E1" />
            <Bar dataKey="Avg Sentiment" radius={[6, 6, 0, 0]} maxBarSize={56}>
              <LabelList dataKey="Avg Sentiment" position="top" formatter={fmt2} style={BAR_LABEL} />
              {PERSONAS.map((p) => (
                <Cell key={p} fill={PERSONA_COLORS[p]} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </Card>

      <Card title={<span className="flex items-center">Key KPIs by Persona (%)<FilteredBadge selection={taSelection} /></span>}>
        <ResponsiveContainer width="100%" height={220}>
          <BarChart data={totalsByPersona} margin={{ top: 20, right: 8, left: -12, bottom: 0 }}>
            <CartesianGrid {...GRID_PROPS} />
            <XAxis dataKey="persona" {...AXIS_PROPS} tick={{ ...TICK_INK, fontWeight: 700 }} tickMargin={10} />
            <YAxis domain={[0, 100]} {...AXIS_PROPS} tick={TICK_MUTED} tickMargin={6} unit="%" />
            <Tooltip cursor={TOOLTIP_CURSOR} content={<CustomTooltip />} />
            <Legend wrapperStyle={{ fontSize: 11, fontWeight: 600, paddingTop: 10 }} iconType="circle" iconSize={9} />
            <Bar dataKey="First-Line %" fill="#0F766E" radius={[4, 4, 0, 0]} maxBarSize={26} />
            <Bar dataKey="Consensus Full %" fill="#0284C7" radius={[4, 4, 0, 0]} maxBarSize={26} />
            <Bar dataKey="Alert Rate %" fill="#DC2626" radius={[4, 4, 0, 0]} maxBarSize={26} />
          </BarChart>
        </ResponsiveContainer>
      </Card>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Small helper: show when a card is TA-filtered                      */
/* ------------------------------------------------------------------ */
function FilteredBadge({ selection }: { selection: TaSelection }) {
  const parts: string[] = [];
  if (selection.area) parts.push(selection.area);
  if (selection.indication) parts.push(selection.indication);
  if (selection.disease) parts.push(selection.disease);
  if (selection.brand) parts.push(selection.brand);
  if (!parts.length) return null;
  return (
    <span className="ml-1.5 inline-flex items-center gap-1 rounded-full bg-brand/10 border border-brand/20 px-2 py-0.5 text-[10px] font-semibold text-brand normal-case tracking-normal">
      {parts.join(" › ")}
    </span>
  );
}

/* ------------------------------------------------------------------ */
/*  Brand & Indication coverage matrix                                 */
/* ------------------------------------------------------------------ */
function BrandCoverageMatrix({ data, taSelection }: { data: BrandMatrix | null; taSelection: TaSelection }) {
  const allRows = data?.rows ?? [];

  // Apply frontend TA/indication/brand/disease filter
  const rows = allRows.filter((r) => {
    if (taSelection.area && r.area.toLowerCase() !== taSelection.area.toLowerCase()) return false;
    if (taSelection.indication && r.indication.toLowerCase() !== taSelection.indication.toLowerCase()) return false;
    if (taSelection.brand && !r.focus_brands.some((b) => b.brand === taSelection.brand)) return false;
    if (taSelection.disease && !r.diseases.includes(taSelection.disease)) return false;
    return true;
  });

  // Group indications under their parent therapeutic area so the area cell can
  // span all of its indications (one row per indication keeps the table compact).
  const areaMap = new Map<string, typeof rows>();
  for (const r of rows) {
    if (!areaMap.has(r.area)) areaMap.set(r.area, []);
    areaMap.get(r.area)!.push(r);
  }

  type RenderRow = Omit<(typeof rows)[number], "area"> & { area?: string; areaSpan?: number };
  const renderRows: RenderRow[] = [];
  for (const [area, inds] of areaMap) {
    inds.forEach((r, i) => {
      renderRows.push({ ...r, area: i === 0 ? area : undefined, areaSpan: i === 0 ? inds.length : undefined });
    });
  }

  return (
    <Card
      title={
        <span className="flex items-center gap-1.5 flex-wrap">
          <InfoTooltip content="The monitored brand taxonomy: each therapeutic area, its indications, the diseases treated within each indication, the AbbVie focus brands, and the competitor brands tracked for that indication." />
          Brand &amp; Indication Coverage
          <FilteredBadge selection={taSelection} />
        </span>
      }
    >
      {renderRows.length === 0 ? (
        <EmptyState message="No brand taxonomy configured." />
      ) : (
        <div className="max-h-[460px] overflow-auto rounded-xl border border-slate-200">
          <table className="w-full text-sm border-collapse">
            <thead className="sticky top-0 z-10 bg-canvas-card">
              <tr className="text-left border-b-2 border-slate-200">
                <th className="py-2.5 px-3 font-bold text-xs text-ink-light uppercase tracking-widest bg-canvas-card">Therapeutic Area</th>
                <th className="py-2.5 px-3 font-bold text-xs text-ink-light uppercase tracking-widest bg-canvas-card">Indication</th>
                <th className="py-2.5 px-3 font-bold text-xs text-ink-light uppercase tracking-widest bg-canvas-card">Disease(s) Treated</th>
                <th className="py-2.5 px-3 font-bold text-xs text-ink-light uppercase tracking-widest bg-canvas-card">AbbVie Brand</th>
                <th className="py-2.5 px-3 font-bold text-xs text-ink-light uppercase tracking-widest bg-canvas-card">Competitors</th>
              </tr>
            </thead>
            <tbody>
              {renderRows.map((r) => (
                <tr key={`${r.area ?? ""}-${r.indication}`} className="border-b border-slate-100 hover:bg-brand-surface/40 transition-colors align-top">
                  {r.area !== undefined ? (
                    <td rowSpan={r.areaSpan} className="py-2.5 px-3 font-extrabold text-ink whitespace-nowrap border-r border-slate-100">
                      {r.area}
                    </td>
                  ) : null}
                  <td className="py-2.5 px-3 font-semibold text-ink whitespace-nowrap border-r border-slate-100">{r.indication}</td>
                  <td className="py-2.5 px-3 border-r border-slate-100 max-w-[260px] w-[260px]">
                    {r.diseases.length ? (
                      <div className="flex flex-wrap gap-1">
                        {r.diseases.map((d) => (
                          <span key={d} className="px-2 py-0.5 rounded-full text-[11px] font-semibold bg-brand-surface text-brand-dark">{d}</span>
                        ))}
                      </div>
                    ) : (
                      <span className="text-ink-muted text-xs">N/A</span>
                    )}
                  </td>
                  <td className="py-2.5 px-3 border-r border-slate-100">
                    <div className="space-y-1.5">
                      {r.focus_brands.map((b) => (
                        <div key={b.brand} className="whitespace-nowrap">
                          <span className="font-bold text-ink">{b.brand}</span>
                        </div>
                      ))}
                    </div>
                  </td>
                  <td className="py-2.5 px-3">
                    {r.competitors.length ? (
                      <div className="space-y-1.5">
                        {r.competitors.map((c) => (
                          <div key={c.brand} className="whitespace-nowrap">
                            <span className="font-semibold text-ink">{c.brand}</span>
                            {c.company && <span className="block text-[11px] text-ink-light font-medium">{c.company}</span>}
                          </div>
                        ))}
                      </div>
                    ) : (
                      <span className="text-ink-muted text-xs">N/A</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/*  Disease-State / Pre-Launch landscape view (FR-108a.4/.6)           */
/* ------------------------------------------------------------------ */
function LandscapeView({ data, taSelection }: { data: LandscapeMatrix | null; taSelection: TaSelection }) {
  const matrix = data?.matrix ?? [];
  return (
    <div className="space-y-5">
      {/* Mandated pre-launch label (FR-108a.7) */}
      <div className="rounded-2xl border-2 border-violet-300 bg-violet-50 px-5 py-4">
        <p className="text-xs font-extrabold uppercase tracking-widest text-violet-800">{PRELAUNCH_LABEL}</p>
        <p className="mt-1 text-sm font-medium text-violet-700">
          Brand-less landscape view: every competitor named across AI answers for this disease state, with no AbbVie brand under evaluation.
        </p>
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <AnimatedCard delay={0}><Stat label="Competitors Tracked" value={matrix.length} icon={<Layers size={16} />} tooltip="Distinct therapies/agents named by AI across the disease-state responses." /></AnimatedCard>
        <AnimatedCard delay={0.05}><Stat label="Responses Analyzed" value={data?.responses_analyzed ?? 0} icon={<MessageSquare size={16} />} /></AnimatedCard>
        <AnimatedCard delay={0.1}><Stat label="Questions" value={data?.questions ?? 0} icon={<ClipboardList size={16} />} /></AnimatedCard>
        <AnimatedCard delay={0.15}><Stat label="AI Platforms" value={data?.llms?.length ?? 0} icon={<Zap size={16} />} /></AnimatedCard>
      </div>

      {matrix.length === 0 ? (
        <Card title="Competitive Landscape">
          <EmptyState message="No disease-state (landscape) responses scored yet. Run an All Brands analysis from Run Analysis to populate this view." />
        </Card>
      ) : (
        <>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <Card title={<span className="flex items-center gap-1"><InfoTooltip content="How often each agent appears across AI answers, as a share of all landscape responses. Higher = more visible in AI." />Share of Voice<FilteredBadge selection={taSelection} /></span>}>
              <ResponsiveContainer width="100%" height={Math.max(240, matrix.length * 34)}>
                <BarChart data={matrix.map((m) => ({ brand: m.brand, "Share of Voice %": Math.round(m.share_of_voice * 100) }))} layout="vertical" margin={{ left: 20 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#E2E8F0" />
                  <XAxis type="number" domain={[0, 100]} unit="%" fontSize={12} tick={{ fill: "#64748B" }} />
                  <YAxis type="category" dataKey="brand" width={110} fontSize={12} fontWeight={700} tick={{ fill: "#1A1A2E" }} />
                  <Tooltip content={<CustomTooltip />} />
                  <Bar dataKey="Share of Voice %" radius={[0, 6, 6, 0]} fill={CHART_TEAL} />
                </BarChart>
              </ResponsiveContainer>
            </Card>
            <Card title={<span className="flex items-center gap-1"><InfoTooltip content="Average AI sentiment toward each agent across the landscape. Green = positive, red = negative." />Sentiment by Competitor<FilteredBadge selection={taSelection} /></span>}>
              <ResponsiveContainer width="100%" height={Math.max(240, matrix.length * 34)}>
                <BarChart data={matrix.map((m) => ({ brand: m.brand, "Avg Sentiment": m.avg_sentiment ?? 0 }))} layout="vertical" margin={{ left: 20 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#E2E8F0" />
                  <XAxis type="number" domain={[-1, 1]} fontSize={12} tick={{ fill: "#64748B" }} />
                  <YAxis type="category" dataKey="brand" width={110} fontSize={12} fontWeight={700} tick={{ fill: "#1A1A2E" }} />
                  <Tooltip content={<CustomTooltip />} />
                  <Bar dataKey="Avg Sentiment" radius={[0, 6, 6, 0]}>
                    {matrix.map((m, i) => (
                      <Cell key={i} fill={(m.avg_sentiment ?? 0) >= 0 ? "#0F766E" : "#DC2626"} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </Card>
          </div>

          <Card title={<span className="flex items-center gap-1.5"><InfoTooltip content="The full competitive matrix: for each agent, its share of voice, mean sentiment, and how AI positions it across the landscape." />Competitive Landscape Matrix<FilteredBadge selection={taSelection} /></span>}>
            <div className="overflow-auto rounded-xl border border-slate-200">
              <table className="w-full text-sm border-collapse">
                <thead className="bg-canvas-card">
                  <tr className="text-left border-b-2 border-slate-200">
                    <th className="py-2.5 px-3 font-bold text-xs text-ink-light uppercase tracking-widest">Competitor</th>
                    <th className="py-2.5 px-3 font-bold text-xs text-ink-light uppercase tracking-widest text-right">Mentions</th>
                    <th className="py-2.5 px-3 font-bold text-xs text-ink-light uppercase tracking-widest text-right">Share of Voice</th>
                    <th className="py-2.5 px-3 font-bold text-xs text-ink-light uppercase tracking-widest text-right">Avg Sentiment</th>
                    <th className="py-2.5 px-3 font-bold text-xs text-ink-light uppercase tracking-widest">Dominant Position</th>
                  </tr>
                </thead>
                <tbody>
                  {matrix.map((m) => {
                    const sent = m.avg_sentiment;
                    const sentColor = sent == null ? "#94A3B8" : sent > 0.2 ? "#0F766E" : sent < -0.2 ? "#DC2626" : "#EA580C";
                    return (
                      <tr key={m.brand} className="border-b border-slate-100 hover:bg-brand-surface/40 transition-colors">
                        <td className="py-2.5 px-3 font-bold text-ink whitespace-nowrap">{m.brand}</td>
                        <td className="py-2.5 px-3 text-right tabular-nums text-ink-light">{m.mentions}</td>
                        <td className="py-2.5 px-3 text-right tabular-nums font-semibold text-ink">{Math.round(m.share_of_voice * 100)}%</td>
                        <td className="py-2.5 px-3 text-right tabular-nums font-bold" style={{ color: sentColor }}>{sent == null ? "N/A" : sent.toFixed(2)}</td>
                        <td className="py-2.5 px-3"><PositionBadge position={m.dominant_position} /></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Dashboard page                                                     */
/* ------------------------------------------------------------------ */
const EMPTY_TA: TaSelection = { area: "", indication: "", brand: "", disease: "" };

export default function Dashboard() {
  const [activePersona, setActivePersona] = useState<PersonaTab>("All");
  // FR-108a: BRAND (AbbVie) vs DISEASE_STATE (All Brands / pre-launch landscape) view.
  const [mode, setMode] = useState<"BRAND" | "DISEASE_STATE">("BRAND");
  const [landscape, setLandscape] = useState<LandscapeMatrix | null>(null);
  const [taSelection, setTaSelection] = useState<TaSelection>(EMPTY_TA);
  const [taFilters, setTaFilters] = useState<TaFilters>({});
  const [sentiment, setSentiment] = useState<any>(null);
  const [positioning, setPositioning] = useState<any>(null);
  const [volume, setVolume] = useState<any>(null);
  const [alerts, setAlerts] = useState<any>(null);
  const [llm, setLlm] = useState<any[]>([]);
  const [consensus, setConsensus] = useState<any>(null);
  const [intentDist, setIntentDist] = useState<any>(null);
  const [personaData, setPersonaData] = useState<any>(null);   // all-3 bulk (comparison chart)
  const [personaStats, setPersonaStats] = useState<any>(null); // single-persona targeted query
  const [worstQs, setWorstQs] = useState<any[]>([]);
  const [worstLoading, setWorstLoading] = useState(true);
  const [brandMatrix, setBrandMatrix] = useState<BrandMatrix | null>(null);

  const activePersonaRef = useRef(activePersona);
  activePersonaRef.current = activePersona;
  const taFiltersRef = useRef(taFilters);
  taFiltersRef.current = taFilters;
  const modeRef = useRef(mode);
  modeRef.current = mode;

  /* Refetch all aggregate analytics (the KPI cards + charts). Also re-runs the
     targeted persona query when a specific persona tab is active so its KPIs
     stay in sync after a run completes. */
  const loadAnalytics = useCallback(() => {
    const f = taFiltersRef.current;
    if (modeRef.current === "DISEASE_STATE") {
      // Landscape mode: one brand-less multi-competitor aggregation drives the whole view.
      api.landscape(f).then(setLandscape).catch(() => {});
      return;
    }
    api.sentiment(f).then(setSentiment).catch(() => {});
    api.positioning(f).then(setPositioning).catch(() => {});
    api.volume().then(setVolume).catch(() => {});
    api.alertsSummary().then(setAlerts).catch(() => {});
    api.llmComparison().then(setLlm).catch(() => {});
    api.consensusSummary().then(setConsensus).catch(() => {});
    api.intentDistribution().then(setIntentDist).catch(() => {});
    api.personaSummary(undefined, f).then(setPersonaData).catch(() => {});  // bulk for comparison chart
    api.brandMatrix().then(setBrandMatrix).catch(() => {});      // approved-question brand/indication coverage
    const persona = activePersonaRef.current;
    api.worstQuestions(3, persona === "All" ? undefined : persona, f).then(setWorstQs).catch(() => {}).finally(() => setWorstLoading(false));
    if (persona !== "All") {
      api.personaSummary(persona, f).then(setPersonaStats).catch(() => {});
    }
  }, []);

  function handleTaChange(next: TaSelection, filters: TaFilters) {
    setTaSelection(next);
    setTaFilters(filters);
    taFiltersRef.current = filters;
    // Immediately reload with new filters
    const f = filters;
    if (modeRef.current === "DISEASE_STATE") {
      api.landscape(f).then(setLandscape).catch(() => {});
      return;
    }
    api.sentiment(f).then(setSentiment).catch(() => {});
    api.positioning(f).then(setPositioning).catch(() => {});
    api.personaSummary(undefined, f).then(setPersonaData).catch(() => {});
    const persona = activePersonaRef.current;
    setWorstLoading(true);
    api.worstQuestions(3, persona === "All" ? undefined : persona, f).then(setWorstQs).catch(() => {}).finally(() => setWorstLoading(false));
    if (persona !== "All") {
      api.personaSummary(persona, f).then(setPersonaStats).catch(() => {});
    }
  }

  useEffect(() => {
    loadAnalytics();
  }, [loadAnalytics, mode]);

  /* Event-driven refresh: poll the lightweight runs list and refetch the heavy
     analytics ONLY when a run newly transitions to COMPLETED, so the KPI cards
     reflect the latest run without constant background polling of every metric. */
  const completedRunIds = useRef<Set<string> | null>(null);
  useEffect(() => {
    let cancelled = false;
    const checkRuns = () => {
      api.runs().then((runs) => {
        if (cancelled) return;
        const completed = new Set(runs.filter((r) => r.status === "COMPLETED").map((r) => r.run_id));
        if (completedRunIds.current === null) {
          // First poll establishes the baseline; don't refetch on initial load.
          completedRunIds.current = completed;
          return;
        }
        let hasNew = false;
        for (const id of completed) {
          if (!completedRunIds.current.has(id)) { hasNew = true; break; }
        }
        completedRunIds.current = completed;
        if (hasNew) loadAnalytics();
      }).catch(() => {});
    };
    checkRuns();
    const t = setInterval(checkRuns, 5000);
    return () => { cancelled = true; clearInterval(t); };
  }, [loadAnalytics]);

  /* Per-tab targeted query: fires whenever the user switches persona tabs.
     Refetches both the persona KPI summary and the persona-specific top-3
     "Recommended Next Steps" so the action items reflect the active persona. */
  useEffect(() => {
    const f = taFiltersRef.current;
    setWorstLoading(true);
    api.worstQuestions(3, activePersona === "All" ? undefined : activePersona, f)
      .then(setWorstQs).catch(() => {}).finally(() => setWorstLoading(false));
    if (activePersona === "All") { setPersonaStats(null); return; }
    setPersonaStats(null); // clear stale data while loading
    api.personaSummary(activePersona, f).then(setPersonaStats).catch(() => {});
  }, [activePersona]);

  /* ---- Derived values ---- */
  const totalResponses = llm.reduce(
    (acc, l) => acc + Object.values(l.counts || {}).reduce((a: number, b: any) => a + b, 0),
    0
  );

  const isPersonaActive = activePersona !== "All";
  // pd is the single-persona targeted data (from the per-tab useEffect)
  const pd = isPersonaActive ? personaStats : null;

  const personaTotal = pd?.response_count ?? totalResponses;
  const personaAlerts = pd ? pd.alert_rate?.alerts : (alerts?.total_alerts ?? 0);
  const personaConsensusEvals = pd
    ? (pd.consensus?.FULL ?? 0) + (pd.consensus?.PARTIAL ?? 0) + (pd.consensus?.MISSING ?? 0)
    : (consensus?.total_evaluations ?? 0);
  const personaConsensusSub = pd
    ? `${pd.consensus?.FULL ?? 0} full · ${pd.consensus?.PARTIAL ?? 0} partial · ${pd.consensus?.MISSING ?? 0} missing`
    : `${consensus?.by_level?.FULL ?? 0} full · ${consensus?.by_level?.PARTIAL ?? 0} partial · ${consensus?.by_level?.MISSING ?? 0} missing`;
  const personaSentimentLabel = pd
    ? (pd.sentiment?.avg !== null && pd.sentiment?.avg !== undefined ? pd.sentiment.avg.toFixed(2) : "N/A")
    : (sentiment?.buckets ? `${sentiment.buckets.positive}+ / ${sentiment.buckets.negative}-` : "N/A");
  const personaSentimentSub = pd ? "avg score" : "positive / negative";

  /* Persona-filtered chart data */
  const sentimentByLlm = sentiment?.by_llm || [];

  const positioningData = (() => {
    if (!isPersonaActive) {
      return positioning
        ? Object.entries(positioning).map(([llmName, counts]: any) => ({ llm: llmName, ...counts }))
        : [];
    }
    if (!pd?.positioning) return [];
    return [{ llm: activePersona, ...pd.positioning }];
  })();

  const volumeData = volume
    ? Object.entries(volume).map(([day, statuses]: any) => ({
        day,
        total: Object.values(statuses).reduce((a: number, b: any) => a + b, 0),
      }))
    : [];

  const intentSource = isPersonaActive
    ? (intentDist?.by_persona?.[activePersona] ?? {})
    : (intentDist?.by_intent ?? {});

  const consensusSource = isPersonaActive && pd?.consensus
    ? { FULL: pd.consensus.FULL, PARTIAL: pd.consensus.PARTIAL, MISSING: pd.consensus.MISSING }
    : (consensus?.by_level ?? {});

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <PageHeader
          title="Dashboard"
          subtitle="How AI platforms are representing your brand, and what to do about it."
          badge={
            <span className="inline-flex items-center gap-1 rounded-full bg-sky-50 border border-sky-200 px-2.5 py-0.5 text-xs font-semibold text-sky-700">
              <Snowflake size={12} /> Snowflake Views
            </span>
          }
        />
        <div className="flex flex-col items-end gap-2">
          <MonitoringModeToggle mode={mode} onChange={setMode} />
          {mode === "BRAND" && <PersonaTabBar active={activePersona} onChange={setActivePersona} />}
        </div>
      </div>

      {/* ── Disease-State / Pre-Launch landscape (FR-108a) ── */}
      {mode === "DISEASE_STATE" && (
        <>
          <TaHierarchyFilter value={taSelection} onChange={handleTaChange} />
          <LandscapeView data={landscape} taSelection={taSelection} />
        </>
      )}

      {/* ── Brand (AbbVie) dashboard ── */}
      {mode === "BRAND" && (
        <>
      {/* ── Needs Attention — lead with the action items ── */}
      <NeedsAttention questions={worstQs} isLoading={worstLoading} />

      {/* ── Brand Brief ── */}
      <BrandBrief sentiment={sentiment} positioning={positioning} alerts={alerts} consensus={consensus} />

      {/* ── Stats row ── */}
      <div className="space-y-3">
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-4">
        <AnimatedCard delay={0}>
          <Stat
            label={isPersonaActive ? `${activePersona} Responses` : "Total Responses"}
            value={personaTotal}
            icon={<MessageSquare size={16} />}
          />
        </AnimatedCard>
        <AnimatedCard delay={0.05}>
          <Stat
            label="Targeted AI Platforms"
            value={llm.length}
            icon={<Zap size={16} />}
            tooltip="The number of AI platforms being actively monitored (e.g. Claude, GPT-4o, Gemini, Nova-Pro, Llama)."
          />
        </AnimatedCard>
        <AnimatedCard delay={0.1}>
          <Stat
            label={isPersonaActive ? `${activePersona} Alerts` : "Total Alerts"}
            value={personaAlerts ?? 0}
            icon={<AlertTriangle size={16} />}
          />
        </AnimatedCard>
        <AnimatedCard delay={0.15}>
          <Stat
            label="Consensus Evals"
            value={personaConsensusEvals}
            sub={personaConsensusSub}
            icon={<ShieldCheck size={16} />}
            tooltip="Total number of evaluations where all 5 AI platforms were compared for agreement on their responses."
          />
        </AnimatedCard>
        <AnimatedCard delay={0.2}>
          <Stat
            label="Avg Sentiment"
            value={personaSentimentLabel}
            sub={personaSentimentSub}
            icon={<TrendingUp size={16} />}
          />
        </AnimatedCard>
      </div>

      {/* ── TA / Indication / Disease / Brand filters ── */}
      <TaHierarchyFilter value={taSelection} onChange={handleTaChange} />

      </div>{/* end stats+filter wrapper */}

      {/* ── Brand & Indication coverage matrix (approved questions) ── */}
      <BrandCoverageMatrix data={brandMatrix} taSelection={taSelection} />

      {/* ── Persona KPI cards (when a persona tab is active) ── */}
      {isPersonaActive && (
        <div>
          <h2 className="text-xs font-bold text-ink-light uppercase tracking-widest mb-3 flex items-center gap-2">
            <Users size={13} />
            <span style={{ color: PERSONA_COLORS[activePersona] }}>{activePersona}</span> KPIs
          </h2>
          <PersonaKpiRow personaData={personaStats} />
        </div>
      )}

      {/* ── Persona Comparison (All tab only) ── */}
      {!isPersonaActive && personaData && (
        <div>
          <h2 className="text-xs font-bold text-ink-light uppercase tracking-widest mb-3">Persona comparison</h2>
          <PersonaComparisonChart personaData={personaData} taSelection={taSelection} />
        </div>
      )}

      {/* ── Charts grid ── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Sentiment by LLM */}
        <AnimatedCard delay={0.1} className="h-full">
          <Card title={<span className="flex items-center">Sentiment by LLM<FilteredBadge selection={taSelection} /></span>} className="h-full">
            {sentimentByLlm.length ? (
              <ResponsiveContainer width="100%" height={280}>
                <BarChart data={sentimentByLlm} margin={{ top: 24, right: 8, left: -12, bottom: 0 }}>
                  <CartesianGrid {...GRID_PROPS} />
                  <XAxis dataKey="key" {...AXIS_PROPS} tick={TICK_INK} tickMargin={10} interval={0} />
                  <YAxis domain={[-1, 1]} ticks={[-1, -0.5, 0, 0.5, 1]} {...AXIS_PROPS} tick={TICK_MUTED} tickMargin={6} />
                  <Tooltip cursor={TOOLTIP_CURSOR} content={<CustomTooltip />} />
                  <ReferenceLine y={0} stroke="#CBD5E1" />
                  <Bar dataKey="avg_sentiment" radius={[6, 6, 0, 0]} name="Avg Sentiment" maxBarSize={52}>
                    <LabelList dataKey="avg_sentiment" position="top" formatter={fmt2} style={BAR_LABEL} />
                    {sentimentByLlm.map((d: any, i: number) => (
                      <Cell key={i} fill={d.avg_sentiment >= 0 ? "#0F766E" : "#DC2626"} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <EmptyState message="No sentiment data yet. Trigger a run." />
            )}
          </Card>
        </AnimatedCard>

        {/* Competitive Positioning */}
        <AnimatedCard delay={0.15} className="h-full">
          <Card title={<span className="flex items-center">{isPersonaActive ? `${activePersona} Positioning` : "Competitive Positioning"}<FilteredBadge selection={taSelection} /></span>} className="h-full">
            {positioningData.length ? (
              <>
                <ResponsiveContainer width="100%" height={260}>
                  <BarChart margin={{ top: 8, right: 8, left: -12, bottom: 0 }} data={positioningData.map((d: any) => {
                    const renamed: any = { llm: d.llm };
                    for (const pos of POSITIONS) {
                      if (d[pos] != null) renamed[POSITION_LEGEND_BY_KEY[pos]?.label ?? pos] = d[pos];
                    }
                    return renamed;
                  })}>
                    <CartesianGrid {...GRID_PROPS} />
                    <XAxis dataKey="llm" {...AXIS_PROPS} tick={TICK_INK} tickMargin={10} interval={0} />
                    <YAxis {...AXIS_PROPS} tick={TICK_MUTED} tickMargin={6} allowDecimals={false} />
                    <Tooltip cursor={TOOLTIP_CURSOR} content={<CustomTooltip />} />
                    <Legend wrapperStyle={{ fontSize: 11, fontWeight: 600, paddingTop: 10 }} iconType="circle" iconSize={9} />
                    {POSITION_LEGEND.map((p, idx) => (
                      <Bar key={p.key} dataKey={p.label} stackId="a" fill={p.color} maxBarSize={64}
                        radius={idx === POSITION_LEGEND.length - 1 ? [4, 4, 0, 0] : undefined} />
                    ))}
                  </BarChart>
                </ResponsiveContainer>
                <div className="mt-4 rounded-xl border border-slate-200 bg-slate-50 p-4">
                  <p className="text-xs font-extrabold text-ink uppercase tracking-widest mb-3">What each color means</p>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-5 gap-y-2.5">
                    {POSITION_LEGEND.map((p) => (
                      <div key={p.key} className="flex items-start gap-2.5">
                        <span className="mt-0.5 w-3.5 h-3.5 rounded-md shrink-0" style={{ backgroundColor: p.color }} />
                        <span className="text-[13px] leading-tight">
                          <span className="font-extrabold text-ink">{p.label}</span>
                          <span className="text-ink-light">: {p.desc}</span>
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              </>
            ) : (
              <EmptyState message="No positioning data yet." />
            )}
          </Card>
        </AnimatedCard>

        {/* Volume over time */}
        <AnimatedCard delay={0.2} className="h-full">
          <Card title="Response Volume Over Time" className="h-full">
            {volumeData.length ? (
              <ResponsiveContainer width="100%" height={280}>
                <AreaChart data={volumeData} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
                  <defs>
                    <linearGradient id="colorVolume" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor={CHART_TEAL} stopOpacity={0.3} />
                      <stop offset="95%" stopColor={CHART_TEAL} stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid {...GRID_PROPS} />
                  <XAxis dataKey="day" {...AXIS_PROPS} tick={TICK_INK} tickMargin={10} minTickGap={24} />
                  <YAxis {...AXIS_PROPS} tick={TICK_MUTED} tickMargin={6} allowDecimals={false} />
                  <Tooltip cursor={{ stroke: "#CBD5E1", strokeWidth: 1 }} content={<CustomTooltip />} />
                  <Area type="monotone" dataKey="total" stroke={CHART_TEAL} strokeWidth={2.5} fillOpacity={1} fill="url(#colorVolume)" name="Total" dot={false} activeDot={{ r: 4, strokeWidth: 2, stroke: "#fff" }} />
                </AreaChart>
              </ResponsiveContainer>
            ) : (
              <EmptyState message="No volume data yet." />
            )}
          </Card>
        </AnimatedCard>

        {/* Intent Distribution */}
        <AnimatedCard delay={0.25} className="h-full">
          <Card title={isPersonaActive ? `${activePersona} Intent Distribution` : "Intent Distribution"} className="h-full">
            {Object.keys(intentSource).length ? (
              <ResponsiveContainer width="100%" height={280}>
                <PieChart>
                  <Pie
                    data={Object.entries(intentSource).map(([name, value]: any) => ({ name, value }))}
                    dataKey="value"
                    nameKey="name"
                    cx="50%"
                    cy="50%"
                    outerRadius={100}
                    innerRadius={55}
                    paddingAngle={2}
                    stroke="#fff"
                    strokeWidth={2}
                    label={({ name, percent }: any) => `${name} ${(percent * 100).toFixed(0)}%`}
                    labelLine={{ stroke: "#CBD5E1" }}
                  >
                    {Object.entries(intentSource).map(([name]: any, i: number) => (
                      <Cell key={i} fill={INTENT_COLORS[name] || "#94A3B8"} />
                    ))}
                  </Pie>
                  <Tooltip content={<CustomTooltip />} />
                </PieChart>
              </ResponsiveContainer>
            ) : (
              <EmptyState message="No intent data yet." />
            )}
            {Object.keys(intentSource).length > 0 && (
              <div className="mt-3 pt-3 border-t border-slate-100">
                <p className="text-[10px] font-bold text-ink-light uppercase tracking-widest mb-2">Why this matters</p>
                <div className="grid grid-cols-1 gap-1.5">
                  {Object.keys(intentSource).map((k) => {
                    const m = INTENT_MEANING[k];
                    if (!m) return null;
                    return (
                      <div key={k} className="flex items-start gap-2">
                        <span className="mt-1 w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: m.color }} />
                        <span className="text-[11px] leading-snug"><span className="font-bold text-ink">{m.label}:</span> <span className="text-ink-light">{m.desc}</span></span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </Card>
        </AnimatedCard>

        {/* Consensus Breakdown */}
        <AnimatedCard delay={0.3} className="h-full">
          <Card title={isPersonaActive ? `${activePersona} Consensus` : "Consensus Breakdown"} className="h-full">
            {Object.keys(consensusSource).length ? (
              <ResponsiveContainer width="100%" height={280}>
                <BarChart
                  data={Object.entries(consensusSource).map(([level, count]: any) => ({ level, count }))}
                  layout="vertical"
                  margin={{ top: 4, right: 32, left: 8, bottom: 4 }}
                >
                  <CartesianGrid strokeDasharray="3 3" stroke="#EEF2F6" horizontal={false} />
                  <XAxis type="number" {...AXIS_PROPS} tick={TICK_MUTED} allowDecimals={false} />
                  <YAxis type="category" dataKey="level" {...AXIS_PROPS} tick={TICK_INK} width={84} />
                  <Tooltip cursor={TOOLTIP_CURSOR} content={<CustomTooltip />} />
                  <Bar dataKey="count" radius={[0, 6, 6, 0]} name="Count" maxBarSize={44}>
                    <LabelList dataKey="count" position="right" style={BAR_LABEL} />
                    {Object.entries(consensusSource).map(([level]: any, i: number) => (
                      <Cell key={i} fill={CONSENSUS_COLORS[level] || "#94A3B8"} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <EmptyState message="No consensus data yet." />
            )}
          </Card>
        </AnimatedCard>

        {/* Alerts — interpreted */}
        <AnimatedCard delay={0.35} className="h-full">
          <AlertsPanel alerts={alerts} />
        </AnimatedCard>
      </div>
        </>
      )}
    </div>
  );
}
