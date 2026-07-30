import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Building2,
  Check,
  CheckCircle2,
  ChevronDown,
  ExternalLink,
  Filter,
  Hash,
  HeartPulse,
  HelpCircle,
  Languages,
  Loader2,
  Megaphone,
  MessageCircle,
  MessageSquare,
  MessageSquareText,
  Quote,
  RefreshCw,
  Scale,
  Search,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Pill,
  Send,
  TrendingDown,
  TrendingUp,
  Users,
  X,
} from "lucide-react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  api,
  CommunityCount,
  CommunityInsights,
  PlatformComparison,
  PlatformComparisonChannel,
  PlatformSentStat,
  SocialComment,
  SocialInsights,
  SocialPost,
  SocialVerbatim,
  UnmetQuestion,
} from "../api/client";
import {
  AnimatedCard,
  Card,
  EmptyState,
  InfoTooltip,
  PageHeader,
  SentimentBadge,
  Spinner,
  ThemeBadge,
} from "../components/ui";
import { TA_GROUPS, TA_VALUES } from "../lib/taxonomy";

const ALL_CHANNELS = ["reddit", "tiktok", "instagram", "facebook", "x", "myrateam", "bezzy"] as const;

// Channels restricted to specific therapeutic areas (Rheumatology-only community sites).
// They render in the channel pickers only when the current scope matches.
const CHANNEL_AREAS: Record<string, string[]> = {
  myrateam: ["Rheumatology"],
  bezzy: ["Rheumatology"],
};

type SortKey = "recent" | "engagement" | "negative";

// On-brand channel colors drawn from the app's chart palette (cohesive with Insights).
const CHANNEL_COLOR: Record<string, string> = {
  reddit: "#EA580C",
  tiktok: "#0F766E",
  instagram: "#DB2777",
  facebook: "#2563EB",
  x: "#7C3AED",
  myrateam: "#0D9488",
  bezzy: "#9333EA",
};

// Pretty display names (falls back to titleCase for the platform channels).
const CHANNEL_LABEL: Record<string, string> = {
  myrateam: "myRAteam",
  bezzy: "Bezzy RA",
};

const SOV_PALETTE = [
  "#0F766E", "#0284C7", "#EA580C", "#7C3AED", "#DC2626",
  "#059669", "#DB2777", "#2563EB", "#CA8A04", "#0D9488",
];

function fmt(n: number | null | undefined): string {
  if (n === null || n === undefined) return "0";
  return n.toLocaleString();
}

// Matches Insights.tsx: teal (positive) / orange (neutral) / red (negative).
function sentimentColor(v: number | null | undefined): string {
  if (v === null || v === undefined) return "#94A3B8";
  return v > 0.2 ? "#0F766E" : v < -0.2 ? "#DC2626" : "#EA580C";
}

function titleCase(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function channelLabel(c: string): string {
  return CHANNEL_LABEL[c] ?? titleCase(c);
}

function ChartTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="bg-white rounded-xl shadow-lg border border-slate-200 p-3 text-xs max-w-[240px]">
      {label !== undefined && label !== "" && <p className="font-bold text-ink mb-1">{label}</p>}
      {payload.map((p: any, i: number) => (
        <div key={i} className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full" style={{ backgroundColor: p.color || p.stroke || p.fill }} />
          <span className="text-ink-light font-medium truncate">
            {p.name}: <span className="text-ink font-bold">{typeof p.value === "number" ? p.value.toLocaleString() : p.value}</span>
          </span>
        </div>
      ))}
    </div>
  );
}

function ChannelPill({ channel }: { channel: string }) {
  return (
    <span
      className="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wide text-white"
      style={{ backgroundColor: CHANNEL_COLOR[channel] ?? "#64748B" }}
    >
      {channelLabel(channel)}
    </span>
  );
}

const SENT_POS = "#0F766E";
const SENT_NEU = "#EA580C";
const SENT_NEG = "#DC2626";

function pct(n: number | null | undefined): string {
  if (n === null || n === undefined) return "0%";
  return `${Math.round(n * 100)}%`;
}

function AeChip() {
  return (
    <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-[10px] font-bold bg-red-100 text-red-700">
      <AlertTriangle size={10} /> AE
    </span>
  );
}

function SectionHeading({ icon, children, hint }: { icon?: ReactNode; children: ReactNode; hint?: string }) {
  return (
    <div className="flex items-center gap-2 mb-3">
      {icon && <span className="text-ink-light">{icon}</span>}
      <h2 className="text-xs font-bold text-ink-light uppercase tracking-widest">{children}</h2>
      {hint && <span className="text-xs text-ink-muted font-medium normal-case tracking-normal">· {hint}</span>}
    </div>
  );
}

/* Stacked positive/neutral/negative mini-bar used in KPI tiles. */
function DistributionBar({ positive, neutral, negative }: { positive: number; neutral: number; negative: number }) {
  const total = positive + neutral + negative || 1;
  return (
    <div className="flex h-2 w-full overflow-hidden rounded-full bg-surface-2">
      <div style={{ width: `${(positive / total) * 100}%`, backgroundColor: SENT_POS }} />
      <div style={{ width: `${(neutral / total) * 100}%`, backgroundColor: SENT_NEU }} />
      <div style={{ width: `${(negative / total) * 100}%`, backgroundColor: SENT_NEG }} />
    </div>
  );
}

/* Compact label + value + proportional bar list (AE breakdowns). */
function MiniBars({ title, rows, color }: { title: string; rows: { label: string; value: number }[]; color: string }) {
  const max = rows.reduce((m, r) => Math.max(m, r.value), 0) || 1;
  return (
    <div>
      <p className="text-[11px] font-bold uppercase tracking-widest text-ink-light mb-2">{title}</p>
      <div className="space-y-1.5">
        {rows.slice(0, 5).map((r) => (
          <div key={r.label} className="flex items-center gap-2">
            <span className="text-xs font-semibold text-ink w-24 truncate">{r.label}</span>
            <div className="flex-1 h-2 rounded-full bg-surface-2 overflow-hidden">
              <div className="h-full rounded-full" style={{ width: `${(r.value / max) * 100}%`, backgroundColor: color }} />
            </div>
            <span className="text-xs font-bold text-ink tabular-nums w-6 text-right">{r.value}</span>
          </div>
        ))}
        {!rows.length && <p className="text-xs text-ink-muted">None.</p>}
      </div>
    </div>
  );
}

/* Enriched KPI tile (mirrors the Dashboard persona KPI cards). */
function KpiTile({ label, tooltip, icon, value, valueColor, alert, children }: {
  label: string; tooltip?: string; icon?: ReactNode; value: ReactNode;
  valueColor?: string; alert?: boolean; children?: ReactNode;
}) {
  return (
    <div className={`h-full rounded-2xl border shadow-sm p-5 transition-colors ${alert ? "border-red-200 bg-red-50" : "border-line bg-canvas-card hover:border-brand-light/40"}`}>
      <div className="flex items-center gap-2 mb-1">
        {icon && <span className={alert ? "text-red-500" : "text-brand-light"}>{icon}</span>}
        {tooltip && <InfoTooltip content={tooltip} />}
        <span className="text-xs font-semibold text-ink-light uppercase tracking-widest">{label}</span>
      </div>
      <div className="text-3xl font-display font-bold tracking-tight tabular-nums mt-1 text-ink" style={valueColor ? { color: valueColor } : undefined}>{value}</div>
      {children}
    </div>
  );
}

/* Shared sentiment columns for a platform brand row: distribution bar + avg sentiment + share/count. */
function PlatformStatCols({ stat }: { stat: PlatformSentStat }) {
  return (
    <>
      <div className="flex-1 min-w-0">
        <DistributionBar positive={stat.positive} neutral={stat.neutral} negative={stat.negative} />
      </div>
      <span className="text-xs font-bold tabular-nums w-11 text-right" style={{ color: sentimentColor(stat.avg_sentiment) }}>
        {stat.avg_sentiment !== null ? stat.avg_sentiment.toFixed(2) : "n/a"}
      </span>
      <span className="text-[11px] text-ink-light tabular-nums w-[4.5rem] text-right">{pct(stat.post_share)} · {fmt(stat.posts)}</span>
    </>
  );
}

/* One platform's card: AI gist (when generated) plus an AbbVie-vs-each-competitor-brand
   sentiment/share breakdown. AbbVie is one aggregated bucket (its brands are named); every
   other brand is listed individually, ranked by captured post volume. */
function PlatformCard({ c, delay }: { c: PlatformComparisonChannel; delay: number }) {
  const abbvie = c.abbvie;
  const hasAbbvie = abbvie.posts > 0;
  const competitors = c.competitors.slice(0, 5);
  const moreCount = c.competitors.length - competitors.length;
  return (
    <AnimatedCard delay={delay}>
      <Card className="h-full flex flex-col">
        <div className="flex items-center gap-2 mb-3 flex-wrap">
          <ChannelPill channel={c.channel} />
          <span className="text-[11px] font-semibold text-ink-muted">{fmt(c.total_posts)} posts</span>
          {c.attributed_posts > 0 && (
            <span className="ml-auto text-[11px] font-medium text-ink-muted">{fmt(c.attributed_posts)} brand-attributed</span>
          )}
        </div>

        {c.gist ? (
          <div className="flex items-start gap-2 mb-4 rounded-xl bg-brand-surface/60 border border-brand-light/30 p-3">
            <Sparkles size={14} className="text-brand-dark shrink-0 mt-0.5" />
            <p className="text-[13px] text-ink leading-relaxed">{c.gist}</p>
          </div>
        ) : (
          <p className="text-xs text-ink-muted italic mb-4">No AI gist yet for this platform.</p>
        )}

        <div className="mt-auto space-y-2">
          {/* AbbVie (aggregated; its brands are named) */}
          <div className="flex items-center gap-2">
            <span className="w-32 shrink-0 min-w-0">
              <span className="inline-flex items-center gap-1 text-xs font-extrabold text-brand-dark">
                <Building2 size={11} /> AbbVie
              </span>
              {hasAbbvie && abbvie.brands.length > 0 && (
                <span className="block text-[10px] text-ink-muted truncate">{abbvie.brands.join(", ")}</span>
              )}
            </span>
            {hasAbbvie ? (
              <PlatformStatCols stat={abbvie} />
            ) : (
              <span className="flex-1 text-[11px] text-ink-muted italic">No marketed AbbVie asset in this category</span>
            )}
          </div>

          {/* Each competitor brand, ranked by volume */}
          {competitors.map((comp) => (
            <div key={comp.brand} className="flex items-center gap-2">
              <span className="w-32 shrink-0 min-w-0">
                <span className="block text-xs font-bold text-ink truncate">{comp.brand}</span>
                {comp.company && <span className="block text-[10px] text-ink-muted truncate">{comp.company}</span>}
              </span>
              <PlatformStatCols stat={comp} />
            </div>
          ))}

          {moreCount > 0 && (
            <p className="text-[11px] text-ink-muted pl-[8.5rem]">+{moreCount} more brand{moreCount !== 1 ? "s" : ""}</p>
          )}
          {competitors.length === 0 && (
            <p className="text-[11px] text-ink-muted italic">No competitor brands attributed on this platform.</p>
          )}
        </div>
      </Card>
    </AnimatedCard>
  );
}

/* Lead section: per-platform "AbbVie vs other brands" curated gist. Ordered by captured
   volume (most-discussed platform first). The sentiment/share numbers are deterministic;
   the per-platform gist is AI-authored and shares the AI Social Brief refresh. */
function PlatformComparisonSection({ data, onRegenerate, regenerating }: {
  data: PlatformComparison; onRegenerate: () => void; regenerating: boolean;
}) {
  const channels = data?.channels ?? [];
  if (!channels.length) return null;
  const anyGist = channels.some((c) => !!c.gist);
  return (
    <section>
      <div className="flex items-center gap-2 mb-3 flex-wrap">
        <Scale size={14} className="text-brand-light" />
        <h2 className="text-xs font-bold text-ink-light uppercase tracking-widest">
          What each platform is saying — AbbVie vs other brands
        </h2>
        <InfoTooltip content="A curated per-platform read of the captured social sample: for each channel, an AI gist plus sentiment and share of voice for AbbVie brands versus each competitor brand. Brand ownership is derived from the monitored brand list; the numbers are computed from the captured posts, not market-level data. Always verify against the source posts." />
        <button
          onClick={onRegenerate}
          disabled={regenerating}
          className="ml-auto inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-[11px] font-bold text-brand-dark bg-white/70 border border-brand-light/40 hover:bg-white transition-colors disabled:opacity-60"
        >
          {regenerating
            ? <><Loader2 size={12} className="animate-spin" /> Synthesizing…</>
            : anyGist
              ? <><RefreshCw size={12} /> Refresh gists</>
              : <><Sparkles size={12} /> Generate gists</>}
        </button>
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {channels.map((c, i) => (
          <PlatformCard key={c.channel} c={c} delay={Math.min(i * 0.04, 0.24)} />
        ))}
      </div>
      <p className="mt-2.5 text-[11px] text-ink-muted font-medium">
        Sentiment and share of voice are computed from the captured sample (brand-attributed posts).{" "}
        {anyGist
          ? "The per-platform gist is AI-synthesized; always verify against the source posts."
          : "Click \u201CGenerate gists\u201D to add the AI read of AbbVie vs each competitor brand per platform."}
      </p>
    </section>
  );
}

function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const s = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (s < 60) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

/* AI-synthesized qualitative read of the captured sample. Leads with the LLM narrative
   ("what people are actually saying") and falls back to a rule-based headline computed
   client-side from the aggregates until a brief has been generated. */
function SocialBrief({ insights, onRegenerate, regenerating }: {
  insights: SocialInsights; onRegenerate: () => void; regenerating: boolean;
}) {
  const so = insights.sentiment_overall;
  const ae = insights.adverse_events;
  const total = insights.total_posts;
  const avg = so?.avg_sentiment ?? null;
  const negShare = so && so.n ? so.negative / so.n : 0;
  const aeRate = ae?.rate ?? 0;
  const win = insights.window;
  const topBrand =
    insights.share_of_voice.by_brand.find((b) => b.brand !== "Unattributed") ??
    insights.share_of_voice.by_brand[0];

  let tone: "positive" | "mixed" | "concerning";
  if (aeRate > 0.05 || negShare > 0.35 || (avg !== null && avg < -0.1)) tone = "concerning";
  else if (avg !== null && avg > 0.15 && negShare < 0.2) tone = "positive";
  else tone = "mixed";

  const toneStyle = {
    positive: { border: "border-teal-300 bg-teal-50", icon: "text-teal-600", badge: "bg-teal-100 text-teal-800" },
    mixed: { border: "border-amber-300 bg-amber-50", icon: "text-amber-600", badge: "bg-amber-100 text-amber-800" },
    concerning: { border: "border-red-300 bg-red-50", icon: "text-red-600", badge: "bg-red-100 text-red-700" },
  }[tone];

  const toneWord = tone === "positive" ? "net positive" : tone === "concerning" ? "cautious to negative" : "mixed";
  const channelsLabel = insights.channels.map(channelLabel).join(", ") || "no channels yet";

  const B = ({ children }: { children: ReactNode }) => <b className="text-brand-dark font-extrabold">{children}</b>;

  const pills = [
    avg !== null && `avg sentiment ${avg.toFixed(2)}`,
    insights.comment_sentiment_overall?.avg_sentiment != null && `comments ${insights.comment_sentiment_overall.avg_sentiment.toFixed(2)}`,
    negShare > 0 && `${Math.round(negShare * 100)}% negative`,
    ae.total > 0 && `${ae.total} AE signal${ae.total !== 1 ? "s" : ""}`,
    win && win.delta_pct != null && `${win.delta_pct >= 0 ? "+" : ""}${Math.round(win.delta_pct * 100)}% volume (7d)`,
  ].filter(Boolean) as string[];

  const brief = insights.ai_brief;
  const paragraphs = (brief?.narrative ?? "").split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean);
  const hasNarrative = paragraphs.length > 0;

  return (
    <AnimatedCard delay={0}>
      <div className={`rounded-2xl border-2 ${toneStyle.border} p-5`}>
        <div className="flex items-start gap-3">
          <MessageSquareText size={18} className={`${toneStyle.icon} shrink-0 mt-0.5`} />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 mb-2 flex-wrap">
              <span className="text-[11px] font-bold uppercase tracking-widest text-ink-light flex items-center gap-1">
                <InfoTooltip content="An AI-synthesized read of what people are actually saying in this captured social sample: the recurring themes, the main complaints and praise, and any adverse-event signals. Generated from the de-identified post and comment text, not market-level data." />
                AI Social Brief
              </span>
              {pills.map((p) => (
                <span key={p} className={`px-2 py-0.5 rounded-full text-[10px] font-bold ${toneStyle.badge}`}>{p}</span>
              ))}
              <button
                onClick={onRegenerate}
                disabled={regenerating}
                className="ml-auto inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-[11px] font-bold text-brand-dark bg-white/70 border border-brand-light/40 hover:bg-white transition-colors disabled:opacity-60"
              >
                {regenerating
                  ? <><Loader2 size={12} className="animate-spin" /> Synthesizing…</>
                  : hasNarrative
                    ? <><RefreshCw size={12} /> Refresh</>
                    : <><MessageSquareText size={12} /> Generate AI summary</>}
              </button>
            </div>

            {hasNarrative ? (
              <>
                <div className="space-y-2">
                  {paragraphs.map((p, i) => (
                    <p key={i} className="text-[13px] text-ink leading-relaxed">{p}</p>
                  ))}
                </div>
                <p className="mt-2.5 text-[11px] text-ink-muted font-medium">
                  Synthesized by AI{brief?.posts_analyzed ? ` from ${brief.posts_analyzed} posts and comments` : ""}
                  {brief?.updated_at ? ` · updated ${relativeTime(brief.updated_at)}` : ""}. Always verify against the source posts.
                </p>
              </>
            ) : (
              <>
                <p className="text-sm font-bold text-ink leading-snug">
                  Across <B>{total.toLocaleString()}</B> captured posts on <B>{channelsLabel}</B>, overall sentiment is <B>{toneWord}</B>
                  {avg !== null && <> (<B>{avg.toFixed(2)}</B>)</>}.
                  {topBrand && <> <B>{topBrand.brand}</B> leads the sample with <B>{pct(topBrand.post_share)}</B> of posts.</>}
                  {ae.total > 0 && <> <B>{ae.total}</B> adverse-event signal{ae.total !== 1 ? "s" : ""} {ae.total !== 1 ? "are" : "is"} routed for pharmacovigilance review.</>}
                </p>
                <p className="mt-2 text-[11px] text-ink-muted font-medium">
                  This is a rule-based headline from the numbers. Click “Generate AI summary” to read what people are actually saying in their own words.
                </p>
              </>
            )}
          </div>
        </div>
      </div>
    </AnimatedCard>
  );
}

/* Representative verbatim quotes pulled from the captured sample (selected by the LLM,
   text taken verbatim from the stored, de-identified records — never model-authored). */
function VerbatimQuotes({ verbatims }: { verbatims: SocialVerbatim[] }) {
  if (!verbatims.length) return null;
  return (
    <section>
      <SectionHeading icon={<Quote size={13} />} hint="real examples from the captured sample, selected as representative">
        What people are saying
      </SectionHeading>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {verbatims.map((v, i) => (
          <AnimatedCard key={i} delay={Math.min(i * 0.04, 0.2)}>
            <Card className="h-full">
              <div className="flex items-center gap-2 mb-2.5 flex-wrap">
                <ChannelPill channel={v.channel} />
                <span className="text-[10px] font-bold uppercase tracking-wide text-ink-muted">{v.kind}</span>
                {v.ae_flag && <AeChip />}
                <span className="ml-auto"><SentimentBadge score={v.sentiment} /></span>
              </div>
              <div className="relative">
                <Quote size={16} className="absolute -left-0.5 -top-1 text-brand-light/40" />
                <blockquote className="pl-5 text-[13px] text-ink leading-relaxed italic">
                  {v.quote}
                </blockquote>
              </div>
              <div className="mt-3 flex items-center gap-2 flex-wrap">
                {v.brand && v.brand !== "Unattributed" && (
                  <span className="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-brand-surface text-brand-dark">{v.brand}</span>
                )}
                {v.topic && (
                  <span className="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-surface-2 text-ink-light">{v.topic}</span>
                )}
                {v.why && (
                  <span className="text-[11px] text-ink-muted font-medium">{v.why}</span>
                )}
              </div>
            </Card>
          </AnimatedCard>
        ))}
      </div>
    </section>
  );
}

/* Adverse-event panel: interpreted callout + breakdowns, or a calm all-clear. */
function AePanel({ ae, onReview }: { ae: SocialInsights["adverse_events"]; onReview: () => void }) {
  if (!ae || ae.total === 0) {
    return (
      <AnimatedCard delay={0.05}>
        <div className="rounded-2xl border-2 border-teal-200 bg-teal-50 p-5 flex items-start gap-3">
          <ShieldCheck className="text-teal-600 shrink-0 mt-0.5" size={18} />
          <div>
            <p className="text-sm font-bold text-teal-900">No adverse-event signals in the captured sample</p>
            <p className="text-xs text-teal-800 mt-1 font-medium leading-relaxed">
              The classifier did not flag any posts as a possible adverse event. New signals are routed here for pharmacovigilance review as they appear.
            </p>
          </div>
        </div>
      </AnimatedCard>
    );
  }
  const ratePct = Math.round((ae.rate ?? 0) * 1000) / 10;
  const many = ae.total !== 1;
  return (
    <AnimatedCard delay={0.05}>
      <div className="rounded-2xl border-2 border-red-300 bg-canvas-card overflow-hidden shadow-sm">
        <div className="bg-red-600 px-5 py-3.5 flex items-center justify-between gap-3 flex-wrap text-white">
          <div className="flex items-center gap-2.5">
            <AlertTriangle size={17} />
            <span className="text-sm font-extrabold tracking-wide">Adverse-Event Signals</span>
            <span className="text-white/85 text-xs font-medium">· routed for pharmacovigilance review</span>
          </div>
          <span className="inline-flex items-center gap-1.5 text-[11px] font-bold bg-white/15 px-2.5 py-1 rounded-full">
            {ae.total} flagged · {ratePct}% of sample
          </span>
        </div>
        <div className="p-5 grid grid-cols-1 lg:grid-cols-2 gap-5">
          <div className="rounded-xl bg-red-50 border border-red-100 p-4 space-y-2">
            <p className="text-sm font-bold text-red-700">What this means</p>
            <p className="text-[13px] text-red-700/80 leading-relaxed">
              {ae.total} signal{many ? "s" : ""} (<b>{ae.posts}</b> in posts · <b>{ae.comments}</b> in comments) mention a possible side effect or adverse reaction. In a regulated setting these must be triaged by pharmacovigilance within the required reporting window.
            </p>
            <div className="rounded-lg bg-white/70 border border-red-100 p-2.5">
              <p className="text-[10px] font-bold uppercase tracking-widest text-ink-light mb-0.5">What to do</p>
              <p className="text-xs text-ink font-semibold leading-snug">
                Open the flagged posts, confirm whether each is a reportable adverse event, and forward qualifying cases to PV intake.
              </p>
              <button onClick={onReview} className="mt-1.5 inline-flex items-center gap-1 text-[11px] font-bold text-red-700 hover:text-red-800">
                Review AE posts <ArrowRight size={11} />
              </button>
            </div>
          </div>
          <div className="space-y-4">
            <MiniBars title="By brand" rows={ae.by_brand.map((r) => ({ label: r.brand, value: r.count }))} color="#DC2626" />
            <MiniBars title="By channel" rows={ae.by_channel.map((r) => ({ label: channelLabel(r.channel), value: r.count }))} color="#DC2626" />
          </div>
        </div>
      </div>
    </AnimatedCard>
  );
}

function Meta({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-lg bg-surface-1 border border-line p-2.5">
      <p className="text-[10px] font-bold uppercase tracking-widest text-ink-muted mb-0.5">{label}</p>
      <p className="text-xs font-semibold text-ink break-words">{value}</p>
    </div>
  );
}

/* Shows English by default; when translated, offers an Instagram/X-style
   "Translated from <language> · Show original" toggle. */
function TranslatableText({ text, textOriginal, language, isTranslated, className }: {
  text: string; textOriginal: string | null; language: string | null;
  isTranslated: boolean; className?: string;
}) {
  const [showOriginal, setShowOriginal] = useState(false);
  if (!isTranslated || !textOriginal) {
    return <p className={className}>{text}</p>;
  }
  return (
    <div>
      <p className={className}>{showOriginal ? textOriginal : text}</p>
      <button
        onClick={(e) => { e.stopPropagation(); setShowOriginal((v) => !v); }}
        className="mt-1 inline-flex items-center gap-1 text-[11px] font-bold text-brand hover:text-brand-dark"
      >
        <Languages size={11} />
        {showOriginal ? "Show English translation" : `Translated from ${language || "another language"} · Show original`}
      </button>
    </div>
  );
}

/* Comment sentiment as a SEPARATE dimension from post sentiment: overall avg + distribution,
   a contrast vs the post sentiment, and a by-channel breakdown. */
function CommentSentimentPanel({ insights }: { insights: SocialInsights }) {
  const cs = insights.comment_sentiment_overall;
  const byCh = insights.comment_sentiment_by_channel ?? [];
  const postAvg = insights.sentiment_overall?.avg_sentiment ?? null;
  const avg = cs?.avg_sentiment ?? null;
  if (!cs || cs.n === 0) return null;
  const delta = avg !== null && postAvg !== null ? avg - postAvg : null;
  return (
    <Card>
      <div className="flex items-center gap-2 mb-1">
        <MessageCircle size={15} className="text-brand-light" />
        <h3 className="text-sm font-bold text-ink">Comment sentiment</h3>
        <InfoTooltip content="Average sentiment of the captured comments/replies: the crowd's reaction. Tracked as a SEPARATE dimension from the post author's sentiment." />
        <span className="ml-auto text-[11px] font-semibold text-ink-muted">{fmt(insights.total_comments)} comments captured</span>
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5 mt-3">
        <div>
          <div className="flex items-end gap-3">
            <span className="text-4xl font-display font-bold tabular-nums" style={{ color: sentimentColor(avg) }}>
              {avg !== null ? avg.toFixed(2) : "n/a"}
            </span>
            <span className="text-xs font-semibold text-ink-muted mb-1">avg comment sentiment</span>
          </div>
          <div className="flex items-center gap-3 mt-2 text-[11px] text-ink-light font-medium">
            <span className="text-teal-600 font-bold">{cs.positive} pos</span>
            <span className="text-slate-500">{cs.neutral} neu</span>
            <span className="text-red-600 font-bold">{cs.negative} neg</span>
          </div>
          <div className="mt-2"><DistributionBar positive={cs.positive} neutral={cs.neutral} negative={cs.negative} /></div>
          {delta !== null && (
            <p className="mt-3 text-xs font-semibold text-ink-light">
              Comments run{" "}
              <b style={{ color: sentimentColor(delta > 0.05 ? 1 : delta < -0.05 ? -1 : 0) }}>
                {Math.abs(delta) < 0.05 ? "about the same as" : delta > 0 ? `${delta.toFixed(2)} more positive than` : `${Math.abs(delta).toFixed(2)} more negative than`}
              </b>{" "}
              the posts ({postAvg!.toFixed(2)}).
            </p>
          )}
        </div>
        <div>
          <p className="text-[11px] text-ink-muted mb-2">Comment sentiment by channel</p>
          {byCh.length ? (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={byCh} margin={{ left: 8, right: 8 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#E2E8F0" />
                <XAxis dataKey="channel" fontSize={11} tickFormatter={channelLabel} tick={{ fill: "#64748B" }} />
                <YAxis domain={[-1, 1]} fontSize={11} tick={{ fill: "#64748B" }} />
                <Tooltip content={<ChartTooltip />} cursor={{ fill: "rgba(148,163,184,0.12)" }} />
                <ReferenceLine y={0} stroke="#94A3B8" />
                <Bar dataKey="avg_sentiment" name="avg comment sentiment" radius={[4, 4, 0, 0]}>
                  {byCh.map((row, i) => (<Cell key={i} fill={sentimentColor(row.avg_sentiment)} />))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <EmptyState message="No scored comments yet." icon={<Activity size={24} />} />
          )}
        </div>
      </div>
    </Card>
  );
}

/* Slide-in detail drawer for a single post (mirrors the Results drawer). */
function PostDrawer({ post, onClose }: { post: SocialPost; onClose: () => void }) {
  const [comments, setComments] = useState<SocialComment[]>([]);
  const [loadingComments, setLoadingComments] = useState(true);
  useEffect(() => {
    let active = true;
    setLoadingComments(true);
    api.socialComments(post.id)
      .then((cs) => { if (active) setComments(cs); })
      .catch(() => { if (active) setComments([]); })
      .finally(() => { if (active) setLoadingComments(false); });
    return () => { active = false; };
  }, [post.id]);
  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="fixed inset-0 bg-black/30 flex justify-end z-50" onClick={onClose}>
      <motion.div
        initial={{ x: "100%" }} animate={{ x: 0 }} exit={{ x: "100%" }}
        transition={{ type: "spring", damping: 30, stiffness: 300 }}
        className="w-full max-w-[560px] bg-canvas-card h-full overflow-y-auto shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="p-6">
          <div className="flex items-center justify-between mb-5">
            <div className="flex items-center gap-2">
              <ChannelPill channel={post.channel} />
              <span className="text-xs text-ink-light font-medium">{post.posted_at ? new Date(post.posted_at).toLocaleString() : "date unknown"}</span>
            </div>
            <button onClick={onClose} className="p-2 hover:bg-surface-2 rounded-xl transition-colors"><X size={18} className="text-ink-light" /></button>
          </div>
          <div className="flex flex-wrap items-center gap-2 mb-4">
            {post.ae_flag && <AeChip />}
            {post.domain && <ThemeBadge theme={post.domain} />}
            <SentimentBadge score={post.sentiment} />
            {post.brand_focus && <span className="px-2.5 py-1 rounded-full text-xs font-semibold bg-brand-surface text-brand-dark">{post.brand_focus}</span>}
          </div>
          <TranslatableText
            text={post.text}
            textOriginal={post.text_original}
            language={post.language}
            isTranslated={post.is_translated}
            className="text-sm text-ink leading-relaxed whitespace-pre-wrap"
          />
          {post.pii_flags?.length > 0 && (
            <div className="mt-4 rounded-lg bg-amber-50 border border-amber-200 p-2.5 text-xs text-amber-800 font-medium">
              Redacted before storage: {post.pii_flags.join(", ")}
            </div>
          )}
          <div className="mt-5 grid grid-cols-2 gap-3">
            <Meta label="Engagement" value={`${fmt(post.engagement_score)} ${post.engagement_metric}`} />
            <Meta label="Comments" value={fmt(post.comment_count)} />
            <Meta label="Topic" value={post.topic || "Untagged"} />
            <Meta label="Search term" value={post.search_term || "n/a"} />
            <Meta label="Source" value={post.source_domain || post.source || "n/a"} />
            <Meta label="Therapeutic area" value={post.therapeutic_area || "n/a"} />
          </div>
          {post.post_url && (
            <a href={post.post_url} target="_blank" rel="noreferrer" className="mt-5 inline-flex items-center gap-1.5 px-3 py-2 rounded-lg bg-brand-dark text-white text-xs font-bold hover:bg-brand transition-colors">
              Open original source <ExternalLink size={13} />
            </a>
          )}

          {/* What people are saying (captured comments) */}
          <div className="mt-7 pt-5 border-t border-line">
            <div className="flex items-center gap-2 mb-3 flex-wrap">
              <MessageCircle size={14} className="text-ink-light" />
              <h3 className="text-xs font-bold uppercase tracking-widest text-ink-light">What people are saying</h3>
              {post.comments_captured > 0 && (
                <span className="text-[11px] font-semibold text-ink-muted">
                  {fmt(post.comments_captured)} captured
                  {post.comment_sentiment != null && (
                    <> · avg <b style={{ color: sentimentColor(post.comment_sentiment) }}>{post.comment_sentiment.toFixed(2)}</b></>
                  )}
                </span>
              )}
            </div>
            {loadingComments ? (
              <div className="py-6 flex justify-center"><Spinner size={20} /></div>
            ) : comments.length === 0 ? (
              <p className="text-xs text-ink-muted">No comments captured for this post.</p>
            ) : (
              <ul className="space-y-2">
                {comments.map((c) => (
                  <li key={c.id} className="rounded-xl border border-line bg-surface-1 p-3">
                    <div className="flex items-center gap-2 mb-1">
                      <span className="h-1.5 w-1.5 rounded-full shrink-0" style={{ backgroundColor: sentimentColor(c.sentiment) }} title={c.sentiment_label ?? "unscored"} />
                      {c.ae_flag && <AeChip />}
                      {c.engagement_score != null && c.engagement_score > 0 && (
                        <span className="text-[10px] text-ink-muted tabular-nums">{fmt(c.engagement_score)} {c.engagement_metric}</span>
                      )}
                      <span className="ml-auto text-[10px] text-ink-muted">{c.posted_at ? new Date(c.posted_at).toLocaleDateString() : ""}</span>
                    </div>
                    <TranslatableText
                      text={c.text}
                      textOriginal={c.text_original}
                      language={c.language}
                      isTranslated={c.is_translated}
                      className="text-[13px] text-ink leading-snug whitespace-pre-wrap"
                    />
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </motion.div>
    </motion.div>
  );
}

/* ---- Patient Community Insights (myRAteam / Bezzy RA enrichment) ---- */

/* AbbVie-vs-competitor split of community drug mentions. */
function DrugSovBar({ sov }: { sov: CommunityInsights["drug_sov"] }) {
  const total = sov.total_mentions || 1;
  const abbviePct = (sov.abbvie_mentions / total) * 100;
  return (
    <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-surface-2">
      <div style={{ width: `${abbviePct}%`, backgroundColor: "#0F766E" }} />
      <div style={{ width: `${100 - abbviePct}%`, backgroundColor: "#94A3B8" }} />
    </div>
  );
}

/* Merge case/whitespace-duplicate labels (e.g. "Pain" + "pain"), summing their counts and
   ranking by frequency. Keeps acronym casing (RA, TNF, DMARDs) by preferring the variant with
   the most uppercase letters. */
function mergeCounts(rows: CommunityCount[]): CommunityCount[] {
  const ups = (s: string) => (s.match(/[A-Z]/g) || []).length;
  const map = new Map<string, CommunityCount>();
  for (const r of rows ?? []) {
    const label = (r.label ?? "").replace(/_/g, " ").trim();
    if (!label) continue;
    const key = label.toLowerCase();
    const cur = map.get(key);
    if (!cur) map.set(key, { label, count: r.count });
    else {
      cur.count += r.count;
      if (ups(label) > ups(cur.label)) cur.label = label;
    }
  }
  return [...map.values()].sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}

/* "Show all / show less" toggle shared by the experience groups. */
function ShowMore({ expanded, total, onToggle }: { expanded: boolean; total: number; onToggle: () => void }) {
  return (
    <button
      onClick={onToggle}
      className="mt-2 inline-flex items-center gap-1 text-[11px] font-bold uppercase tracking-widest text-ink-muted hover:text-ink transition-colors"
    >
      <ChevronDown size={11} className={`transition-transform ${expanded ? "rotate-180" : ""}`} />
      {expanded ? "Show less" : `Show all ${total}`}
    </button>
  );
}

/* Ranked list for the descriptive, sentence-like themes (concerns / switching / access):
   a colored dot, the full phrase (wraps naturally), and a count badge only when it recurs.
   A subtle frequency bar appears only when counts actually vary, so all-once lists stay clean. */
function ExperienceList({ title, rows, color }: { title: string; rows: CommunityCount[]; color: string }) {
  const [expanded, setExpanded] = useState(false);
  const merged = useMemo(() => mergeCounts(rows), [rows]);
  const LIMIT = 6;
  const shown = expanded ? merged : merged.slice(0, LIMIT);
  const maxCount = merged[0]?.count ?? 1;
  const showBar = maxCount > 1;
  return (
    <div>
      <p className="text-[11px] font-bold uppercase tracking-widest text-ink-light mb-2">{title}</p>
      {merged.length === 0 ? (
        <p className="text-xs text-ink-muted italic">None captured.</p>
      ) : (
        <>
          <ul className="space-y-1.5">
            {shown.map((r) => (
              <li key={r.label}>
                <div className="flex items-start gap-2">
                  <span className="mt-[7px] h-1.5 w-1.5 rounded-full shrink-0" style={{ backgroundColor: color }} />
                  <span className="flex-1 text-sm leading-snug text-ink capitalize">{r.label}</span>
                  {r.count > 1 && (
                    <span className="shrink-0 text-[11px] font-bold tabular-nums px-1.5 py-0.5 rounded-full"
                          style={{ color, backgroundColor: `${color}14` }}>{r.count}</span>
                  )}
                </div>
                {showBar && r.count > 1 && (
                  <div className="ml-3.5 mt-1 h-1 rounded-full bg-surface-2 overflow-hidden">
                    <div className="h-full rounded-full" style={{ width: `${(r.count / maxCount) * 100}%`, backgroundColor: color }} />
                  </div>
                )}
              </li>
            ))}
          </ul>
          {merged.length > LIMIT && (
            <ShowMore expanded={expanded} total={merged.length} onToggle={() => setExpanded((v) => !v)} />
          )}
        </>
      )}
    </div>
  );
}

/* Frequency tag-cloud for the short, high-recurrence themes (quality of life / journey stage):
   chips are ranked and lightly sized by count so dominant impacts (Pain, Fatigue) read at a glance. */
function ExperienceChips({ title, rows, color }: { title: string; rows: CommunityCount[]; color: string }) {
  const [expanded, setExpanded] = useState(false);
  const merged = useMemo(() => mergeCounts(rows), [rows]);
  const LIMIT = 14;
  const shown = expanded ? merged : merged.slice(0, LIMIT);
  const maxCount = merged[0]?.count ?? 1;
  const minCount = merged[merged.length - 1]?.count ?? 1;
  const fontSize = (c: number) => {
    const t = maxCount > minCount ? (c - minCount) / (maxCount - minCount) : 0;
    return 13 + t * 2.5; // 13 -> 15.5px by frequency
  };
  return (
    <div>
      <p className="text-[11px] font-bold uppercase tracking-widest text-ink-light mb-2">{title}</p>
      {merged.length === 0 ? (
        <p className="text-xs text-ink-muted italic">None captured.</p>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-1.5">
            {shown.map((r) => (
              <span key={r.label}
                    className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full font-semibold capitalize"
                    style={{ fontSize: `${fontSize(r.count)}px`, backgroundColor: `${color}14`, color }}>
                {r.label}
                <span className="tabular-nums opacity-70">{r.count}</span>
              </span>
            ))}
          </div>
          {merged.length > LIMIT && (
            <ShowMore expanded={expanded} total={merged.length} onToggle={() => setExpanded((v) => !v)} />
          )}
        </>
      )}
    </div>
  );
}

type PromoteState = "idle" | "sending" | "staged" | "exists" | "ae" | "error";

/* Community-specific patient-voice read for myRAteam / Bezzy: multi-drug share of voice,
   patient-experience themes, and the unmet questions (each promotable to Discovery). */
function PatientCommunityInsights({ data, scope }: { data: CommunityInsights; scope: string }) {
  const [promote, setPromote] = useState<Record<string, PromoteState>>({});
  const [promoteMsg, setPromoteMsg] = useState<Record<string, string>>({});

  const send = async (q: UnmetQuestion) => {
    const key = q.question;
    setPromote((s) => ({ ...s, [key]: "sending" }));
    try {
      const res = await api.socialPromoteUnmet({
        question: q.question,
        therapeutic_area: scope,
        brand: q.brand ?? undefined,
        theme: q.theme ?? undefined,
      });
      const next: PromoteState = res.status === "exists" ? "exists" : res.ae_flag ? "ae" : "staged";
      setPromote((s) => ({ ...s, [key]: next }));
    } catch (e: any) {
      setPromote((s) => ({ ...s, [key]: "error" }));
      setPromoteMsg((s) => ({ ...s, [key]: e?.message || "Could not stage this question." }));
    }
  };

  const sov = data.drug_sov;
  const drugs = data.drug_mentions.slice(0, 8);
  const maxMentions = drugs[0]?.mentions || 1;

  return (
    <section>
      <div className="flex items-center gap-2 mb-3 flex-wrap">
        <Users size={14} className="text-brand-light" />
        <h2 className="text-xs font-bold text-ink-light uppercase tracking-widest">Patient Community Insights</h2>
        <InfoTooltip content="A patient-voice read of the myRAteam / Bezzy RA community crawls. Unlike the platform comparison (one brand per post), the drug share of voice here counts every monitored treatment named on each page. Themes and questions are AI-extracted from public patient text; deeper member reviews and ratings are login-gated and not captured." />
        <span className="ml-auto text-[11px] font-medium text-ink-muted">
          {fmt(data.posts)} community page{data.posts !== 1 ? "s" : ""}
          {data.channels.length ? ` \u00b7 ${data.channels.map(channelLabel).join(", ")}` : ""}
        </span>
      </div>

      <div className="space-y-4">
        {/* Treatments discussed \u2014 multi-drug share of voice (full width for readable bars) */}
        <AnimatedCard delay={0}>
          <Card>
            <div className="flex items-center gap-2 mb-3">
              <Pill size={14} className="text-brand-dark" />
              <h3 className="text-sm font-bold text-ink">Treatments discussed</h3>
              <InfoTooltip content="Every monitored treatment named across the captured community pages, how many pages mention each, and the average patient sentiment toward it." />
              <span className="ml-auto text-[11px] text-ink-muted">{fmt(sov.total_mentions)} mentions</span>
            </div>
            {sov.total_mentions > 0 ? (
              <>
                <div className="flex items-center justify-between text-[11px] font-semibold mb-1.5">
                  <span className="text-brand-dark inline-flex items-center gap-1"><Building2 size={11} /> AbbVie {pct(sov.abbvie_share)}</span>
                  <span className="text-ink-light">Competitors {pct(1 - sov.abbvie_share)}</span>
                </div>
                <DrugSovBar sov={sov} />
                <div className="mt-3 space-y-1.5">
                  {drugs.map((d) => (
                    <div key={d.name} className="flex items-center gap-2">
                      <span className="w-28 shrink-0 min-w-0">
                        <span className="block text-xs font-bold text-ink truncate">{d.name}</span>
                        {d.company && <span className="block text-[10px] text-ink-muted truncate">{d.company}</span>}
                      </span>
                      <div className="flex-1 h-2 rounded-full bg-surface-2 overflow-hidden">
                        <div className="h-full rounded-full" style={{ width: `${(d.mentions / maxMentions) * 100}%`, backgroundColor: d.owner === "AbbVie" ? "#0F766E" : "#94A3B8" }} />
                      </div>
                      <span className="text-[11px] font-bold text-ink tabular-nums w-5 text-right">{d.mentions}</span>
                      <span className="text-xs font-bold tabular-nums w-10 text-right" style={{ color: sentimentColor(d.avg_sentiment) }}>
                        {d.avg_sentiment !== null ? d.avg_sentiment.toFixed(2) : "n/a"}
                      </span>
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <p className="text-xs text-ink-muted italic">No monitored treatments were named in the captured pages.</p>
            )}
          </Card>
        </AnimatedCard>

        {/* Patient experience \u2014 full width so the theme groups have room to breathe */}
        <AnimatedCard delay={0.04}>
          <Card>
            <div className="flex items-center gap-2 mb-4">
              <HeartPulse size={14} className="text-brand-dark" />
              <h3 className="text-sm font-bold text-ink">Patient experience</h3>
              <InfoTooltip content="What patients discuss in these communities: their top concerns, quality-of-life impacts, reasons for switching treatments, access barriers, and where they are in their treatment journey." />
            </div>
            {/* Descriptive, sentence-like themes read better as ranked lists than as chips */}
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-x-6 gap-y-5">
              <ExperienceList title="Top concerns" rows={data.concerns} color="#DC2626" />
              <ExperienceList title="Switching drivers" rows={data.switching_drivers} color="#EA580C" />
              <ExperienceList title="Access barriers" rows={data.access_barriers} color="#0284C7" />
            </div>
            {/* Short, high-recurrence themes as frequency-sized chips */}
            <div className="mt-5 pt-4 border-t border-line grid grid-cols-1 md:grid-cols-3 gap-x-6 gap-y-5">
              <div className="md:col-span-2">
                <ExperienceChips title="Quality of life" rows={data.qol_impacts} color="#7C3AED" />
              </div>
              <div>
                <ExperienceChips title="Journey stage" rows={data.journey_stages} color="#0F766E" />
              </div>
            </div>
          </Card>
        </AnimatedCard>
      </div>

      {/* Unmet questions \u2014 promotable to Discovery */}
      {data.unmet_questions.length > 0 && (
        <div className="mt-4">
          <Card>
            <div className="flex items-center gap-2 mb-3 flex-wrap">
              <HelpCircle size={14} className="text-brand-dark" />
              <h3 className="text-sm font-bold text-ink">Unmet questions patients are asking</h3>
              <InfoTooltip content="Real questions patients raise in these communities, clustered by AI. Send one to Discovery to stage it as a candidate monitoring question. It lands in the review queue (not directly in the approved Question Bank); adverse-event content is quarantined for PV review." />
              <span className="ml-auto text-[11px] text-ink-muted">{data.unmet_questions.length} question{data.unmet_questions.length !== 1 ? "s" : ""}</span>
            </div>
            <div className="space-y-2">
              {data.unmet_questions.map((q, i) => {
                const st = promote[q.question] || "idle";
                const done = st === "staged" || st === "exists" || st === "ae";
                return (
                  <div key={i} className="flex items-start gap-3 rounded-xl border border-line bg-surface-1 p-3">
                    <MessageCircle size={14} className="text-brand-light shrink-0 mt-0.5" />
                    <div className="min-w-0 flex-1">
                      <p className="text-[13px] text-ink font-medium leading-snug">{q.question}</p>
                      {(q.theme || q.brand) && (
                        <div className="flex items-center gap-1.5 mt-1.5 flex-wrap">
                          {q.theme && <span className="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-surface-2 text-ink-light capitalize">{q.theme}</span>}
                          {q.brand && <span className="px-2 py-0.5 rounded-full text-[10px] font-semibold bg-brand-surface text-brand-dark">{q.brand}</span>}
                        </div>
                      )}
                      {st === "error" && <p className="text-[11px] text-red-600 font-medium mt-1">{promoteMsg[q.question]}</p>}
                      {st === "ae" && <p className="text-[11px] text-amber-700 font-medium mt-1">Contains a possible adverse event \u2014 quarantined for pharmacovigilance review.</p>}
                    </div>
                    <button
                      onClick={() => send(q)}
                      disabled={st === "sending" || done}
                      className={`shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-[11px] font-bold border transition-colors disabled:opacity-70 ${
                        done ? "text-teal-700 bg-teal-50 border-teal-200" : "text-brand-dark bg-white/70 border-brand-light/40 hover:bg-white"
                      }`}
                    >
                      {st === "sending" ? <><Loader2 size={12} className="animate-spin" /> Sending\u2026</>
                        : st === "staged" ? <><CheckCircle2 size={12} /> Sent to Discover</>
                        : st === "exists" ? <><Check size={12} /> Already staged</>
                        : st === "ae" ? <><ShieldAlert size={12} /> Sent (PV)</>
                        : <><Send size={12} /> Send to Discover</>}
                    </button>
                  </div>
                );
              })}
            </div>
          </Card>
        </div>
      )}

      <p className="mt-2.5 text-[11px] text-ink-muted font-medium">
        Extracted by AI from public patient-community pages (myRAteam, Bezzy RA). Multi-drug share of voice counts every treatment named on each page. Always verify against the source pages; deeper member reviews and ratings are login-gated and not captured.
      </p>
    </section>
  );
}

export default function SocialListening() {
  const [insights, setInsights] = useState<SocialInsights | null>(null);
  const [posts, setPosts] = useState<SocialPost[]>([]);
  const [status, setStatus] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [ingesting, setIngesting] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [scope, setScope] = useState<string>("Rheumatology");
  const [customQuery, setCustomQuery] = useState("");
  const [searchError, setSearchError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("recent");
  const [aeOnly, setAeOnly] = useState(false);
  const [topicFilter, setTopicFilter] = useState<string | null>(null);
  const [drawerPost, setDrawerPost] = useState<SocialPost | null>(null);
  const [channelMenuOpen, setChannelMenuOpen] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const tableRef = useRef<HTMLElement | null>(null);

  const configured: boolean = !!status?.configured;
  const running: boolean = !!status?.social?.running;
  const progress = status?.social?.progress ?? null;
  // A scope that is not one of the configured monitored areas is an ad-hoc free-text search.
  const isAdhoc = !TA_VALUES.includes(scope);

  const loadData = useCallback(async () => {
    const [ins, ps] = await Promise.all([
      api.socialInsights(scope),
      api.socialPosts(`?therapeutic_area=${encodeURIComponent(scope)}&limit=500`),
    ]);
    setInsights(ins);
    setPosts(ps);
  }, [scope]);

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await api.socialStatus());
    } catch {
      /* keep last status */
    }
  }, []);

  useEffect(() => {
    (async () => {
      setLoading(true);
      await Promise.all([loadStatus(), loadData()]);
      setLoading(false);
    })();
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [loadStatus, loadData]);

  // Poll while an ingest is running; reload analytics when it finishes.
  useEffect(() => {
    if (!running) return;
    setIngesting(true);
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      const s = await api.socialStatus().catch(() => null);
      if (s) setStatus(s);
      if (s && !s.social?.running) {
        if (pollRef.current) clearInterval(pollRef.current);
        pollRef.current = null;
        setIngesting(false);
        await loadData();
      }
    }, 3000);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [running, loadData]);

  const toggleChannel = (c: string) =>
    setSelected((prev) => (prev.includes(c) ? prev.filter((x) => x !== c) : [...prev, c]));

  const startIngest = async () => {
    setSearchError(null);
    setIngesting(true);
    try {
      // For an ad-hoc scope (not a configured area), pass the scope as custom seed terms so
      // the backend searches for it; configured areas derive their terms from config.
      const terms = isAdhoc ? scope : undefined;
      await api.socialIngest(selected.length ? selected.join(",") : undefined, scope, terms);
      await loadStatus();
    } catch (e: any) {
      // An ad-hoc scope can be rejected by the relevance gate (422); surface the reason.
      setSearchError(e?.message || "Ingest could not be started.");
      setIngesting(false);
    }
  };

  // Ad-hoc free-text search: scope to the typed query and kick off a live ingest for it.
  const runCustomSearch = async () => {
    const q = customQuery.trim().slice(0, 64);
    if (!q || !configured || running || ingesting) return;
    setSearchError(null);
    setIngesting(true);
    try {
      await api.socialIngest(selected.length ? selected.join(",") : undefined, q, q);
      // Only switch the dashboard to the ad-hoc scope once the capture is accepted — the
      // relevance gate rejects an off-topic query with a 422 before anything is scraped.
      setScope(q);
      await loadStatus();
    } catch (e: any) {
      setSearchError(e?.message || "Search could not be started.");
      setIngesting(false);
    }
  };

  // Regenerate the AI narrative brief from the already-captured sample (no new ingest).
  const regenerateBrief = async () => {
    setRegenerating(true);
    try {
      await api.socialBrief(scope);
      await loadData();
    } catch {
      /* leave the previous brief in place on failure */
    } finally {
      setRegenerating(false);
    }
  };

  const onReviewAe = () => {
    setAeOnly(true);
    setSelected([]);
    setTopicFilter(null);
    setSortKey("recent");
    requestAnimationFrame(() => tableRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };

  const onPickTopic = (topic: string) => {
    setTopicFilter((prev) => (prev === topic ? null : topic));
    requestAnimationFrame(() => tableRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };

  const sovByBrand = insights?.share_of_voice.by_brand ?? [];
  const sovByChannel = insights?.share_of_voice.by_channel ?? [];
  const sentByBrand = insights?.sentiment_by_brand ?? [];
  const sentByChannel = insights?.sentiment_by_channel ?? [];
  const sentOverall = insights?.sentiment_overall;
  const topTopics = insights?.top_topics ?? [];
  const volChannels = insights?.volume_over_time.channels ?? [];
  const volRows = insights?.volume_over_time.rows ?? [];
  const leaders = insights?.engagement_leaders ?? [];
  const ae = insights?.adverse_events;
  const win = insights?.window;
  const topBrand = sovByBrand.find((b) => b.brand !== "Unattributed") ?? sovByBrand[0];
  const lastResult = status?.social?.last_result ?? null;
  const hasData = !!insights && insights.total_posts > 0;
  const busy = running || ingesting;

  // Channel-mix donut (post volume per channel from the captured sample).
  const channelMix = useMemo(
    () => sovByChannel.map((c) => ({ name: channelLabel(c.channel), channel: c.channel, value: c.posts })),
    [sovByChannel],
  );

  // Captured-post count per channel (for the channel chips).
  const channelCounts = useMemo(() => {
    const m: Record<string, number> = {};
    for (const c of sovByChannel) m[c.channel] = c.posts;
    return m;
  }, [sovByChannel]);

  // Rheumatology-only community channels (myRAteam/Bezzy) appear in the pickers only under
  // the Rheumatology scope; the 5 platform channels always show.
  const visibleChannels = useMemo(
    () => ALL_CHANNELS.filter((c) => {
      const areas = CHANNEL_AREAS[c];
      return !areas || areas.includes(scope);
    }),
    [scope],
  );

  // Sentiment distribution donut (whole captured sample).
  const sentDist = useMemo(() => {
    if (!sentOverall) return [];
    return [
      { name: "Positive", value: sentOverall.positive, color: SENT_POS },
      { name: "Neutral", value: sentOverall.neutral, color: SENT_NEU },
      { name: "Negative", value: sentOverall.negative, color: SENT_NEG },
    ].filter((d) => d.value > 0);
  }, [sentOverall]);

  const visiblePosts = useMemo(() => {
    let list = posts;
    if (selected.length) list = list.filter((p) => selected.includes(p.channel));
    if (aeOnly) list = list.filter((p) => p.ae_flag);
    if (topicFilter) list = list.filter((p) => p.topic === topicFilter);
    if (query.trim()) {
      const q = query.trim().toLowerCase();
      list = list.filter(
        (p) =>
          (p.text || "").toLowerCase().includes(q) ||
          (p.brand_focus || "").toLowerCase().includes(q) ||
          (p.topic || "").toLowerCase().includes(q),
      );
    }
    const sorted = [...list];
    if (sortKey === "recent") {
      sorted.sort((a, b) => (b.posted_at ? Date.parse(b.posted_at) : 0) - (a.posted_at ? Date.parse(a.posted_at) : 0));
    } else if (sortKey === "engagement") {
      sorted.sort((a, b) => (b.engagement_score ?? 0) - (a.engagement_score ?? 0));
    } else {
      sorted.sort((a, b) => (a.sentiment ?? 1) - (b.sentiment ?? 1));
    }
    return sorted;
  }, [posts, selected, aeOnly, topicFilter, query, sortKey]);

  const filterActive = selected.length > 0 || aeOnly || !!topicFilter || query.trim().length > 0;

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner size={28} />
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <PageHeader
          title="Social Listening"
          subtitle="What people are saying about monitored therapies across public social channels."
          tooltip={insights?.basis}
          badge={
            <span className="inline-flex items-center gap-1 rounded-full bg-sky-50 border border-sky-200 px-2.5 py-0.5 text-xs font-semibold text-sky-700">
              <Megaphone size={12} /> Captured sample
            </span>
          }
        />
      </div>

      {/* Scope selector: therapeutic-area dropdown + ad-hoc free-text search */}
      <Card>
        <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 mb-2">
              <Filter size={13} className="text-ink-light" />
              <span className="text-[11px] font-bold text-ink-light uppercase tracking-widest">Therapeutic area</span>
              <InfoTooltip content="Pick a monitored indication to view its captured social sample, or use the search box to run an ad-hoc capture for any topic or brand." />
            </div>
            <select
              value={isAdhoc ? "" : scope}
              onChange={(e) => { if (e.target.value) { setScope(e.target.value); setCustomQuery(""); setSearchError(null); } }}
              className="w-full lg:w-72 rounded-xl border border-line bg-canvas-card px-3 py-2 text-sm font-semibold text-ink focus:border-brand-light focus:outline-none focus:ring-2 focus:ring-brand-light/30"
            >
              {isAdhoc && <option value="">{scope} (custom search)</option>}
              {TA_GROUPS.map((e) =>
                e.type === "option" ? (
                  <option key={e.label} value={e.label}>{e.label}</option>
                ) : (
                  <optgroup key={e.label} label={e.label}>
                    {e.options.map((o) => (
                      <option key={o} value={o}>{o}</option>
                    ))}
                  </optgroup>
                )
              )}
            </select>
          </div>

          <div className="min-w-0 flex-1 lg:max-w-md">
            <div className="flex items-center gap-1.5 mb-2">
              <Search size={13} className="text-ink-light" />
              <span className="text-[11px] font-bold text-ink-light uppercase tracking-widest">Or search any indication / topic / brand</span>
              <InfoTooltip content="Type anything (e.g. 'Lupron endometriosis', 'psoriasis biologic') and run a live capture. This scrapes fresh public posts for your terms and shows the scoped dashboard. Uses Apify + LLM credits." />
            </div>
            <div className="flex items-center gap-2">
              <input
                value={customQuery}
                onChange={(e) => { setCustomQuery(e.target.value); if (searchError) setSearchError(null); }}
                onKeyDown={(e) => { if (e.key === "Enter") runCustomSearch(); }}
                maxLength={64}
                placeholder="e.g. Lupron endometriosis"
                className="flex-1 rounded-xl border border-line bg-canvas-card px-3 py-2 text-sm text-ink placeholder:text-ink-muted focus:border-brand-light focus:outline-none focus:ring-2 focus:ring-brand-light/30"
              />
              <button
                onClick={runCustomSearch}
                disabled={!configured || busy || !customQuery.trim()}
                className="inline-flex items-center gap-1.5 px-4 py-2 rounded-xl bg-brand-dark text-white text-sm font-bold hover:bg-brand transition-colors disabled:opacity-60 shadow-sm shrink-0"
              >
                {busy ? <Spinner size={15} /> : <Search size={15} />}
                Search
              </button>
            </div>
            {searchError && (
              <p className="mt-2 flex items-start gap-1.5 text-xs font-semibold text-rose-600">
                <ShieldAlert size={13} className="mt-0.5 shrink-0" />
                <span>{searchError}</span>
              </p>
            )}
          </div>
        </div>
        <p className="mt-3 text-[11px] text-ink-muted font-medium">
          Viewing <b className="text-ink">{scope}</b>{isAdhoc && " (custom search)"}. The dashboard below reflects the captured sample for this scope.
        </p>
      </Card>

      {/* Controls: channel filter + ingest (grouped with scope selection at the top) */}
      <Card>
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          {/* Channel multi-select */}
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 mb-2">
              <Filter size={13} className="text-ink-light" />
              <span className="text-[11px] font-bold text-ink-light uppercase tracking-widest">Channels</span>
              <InfoTooltip content="Filters the captured-posts table below and scopes the next ingest. The charts above always reflect the full captured sample." />
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <button
                onClick={() => setSelected([])}
                className={`inline-flex items-center rounded-xl border px-3 py-1.5 text-xs font-bold transition-colors ${selected.length === 0 ? "border-brand-light/50 bg-brand-surface text-brand-dark" : "border-line text-ink-light hover:text-ink hover:border-ink-muted/40"}`}
              >
                All channels
              </button>
              {visibleChannels.map((c) => {
                const on = selected.includes(c);
                const count = channelCounts[c] ?? 0;
                return (
                  <button
                    key={c}
                    onClick={() => toggleChannel(c)}
                    className={`inline-flex items-center gap-1.5 rounded-xl border px-3 py-1.5 text-xs font-bold transition-colors ${on ? "border-transparent text-white shadow-sm" : "border-line text-ink-light hover:text-ink hover:border-ink-muted/40"}`}
                    style={on ? { backgroundColor: CHANNEL_COLOR[c] } : undefined}
                  >
                    <span className="h-2 w-2 rounded-full" style={{ backgroundColor: on ? "rgba(255,255,255,0.9)" : CHANNEL_COLOR[c] }} />
                    {channelLabel(c)}
                    {count > 0 && <span className={`tabular-nums ${on ? "text-white/80" : "text-ink-muted"}`}>{count}</span>}
                  </button>
                );
              })}
            </div>
          </div>

          {/* Ingest action */}
          <div className="flex items-center gap-3 shrink-0">
            <div className="text-right leading-tight">
              <p className="text-xs font-semibold text-ink">
                {selected.length ? `Scoped to ${selected.length} channel${selected.length !== 1 ? "s" : ""}` : "All enabled channels"}
              </p>
              <p className="text-[11px] text-ink-muted">
                {selected.length ? selected.map(channelLabel).join(", ") : "fetches every configured source"}
              </p>
            </div>
            <InfoTooltip content={"Scrapes fresh public posts from the selected channels (all enabled channels if none are picked) via Apify, then classifies sentiment, theme, and brand and flags possible adverse events for review. Runs in the background and refreshes the analytics when it finishes."} side="bottom" />
            <button
              onClick={startIngest}
              disabled={!configured || busy}
              className="inline-flex items-center gap-1.5 px-4 py-2.5 rounded-xl bg-brand-dark text-white text-sm font-bold hover:bg-brand transition-colors disabled:opacity-60 shadow-sm"
            >
              {busy ? <Spinner size={15} /> : <RefreshCw size={15} />}
              {busy ? "Ingesting…" : "Ingest now"}
            </button>
          </div>
        </div>

        {/* Live progress */}
        {busy && progress && (
          <div className="mt-4 pt-4 border-t border-line">
            <div className="flex items-center justify-between gap-3 mb-1.5 flex-wrap">
              <span className="inline-flex items-center gap-2 text-xs font-bold text-brand">
                <span className="relative flex h-2 w-2">
                  <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-brand-light opacity-75" />
                  <span className="relative inline-flex rounded-full h-2 w-2 bg-brand" />
                </span>
                Ingesting live · {progress.phase}
              </span>
              <span className="text-[11px] text-ink-light font-medium tabular-nums">
                {fmt(progress.ingested)} stored · {fmt(progress.raw_posts)} captured · {fmt(progress.ae)} AE
              </span>
            </div>
            <div className="h-1.5 rounded-full bg-surface-2 overflow-hidden">
              <div className="h-full rounded-full bg-brand-light transition-all" style={{ width: `${progress.channels_total ? Math.round(((progress.channels_done ?? 0) / progress.channels_total) * 100) : 8}%` }} />
            </div>
            <p className="text-[11px] text-ink-muted mt-1">channels {progress.channels_done ?? 0}/{progress.channels_total ?? 0}</p>
          </div>
        )}

        {/* Last run */}
        {!busy && lastResult && (
          <div className="mt-4 pt-4 border-t border-line flex items-center gap-2 text-xs flex-wrap">
            {lastResult.status === "ok"
              ? <CheckCircle2 size={14} className="text-teal-600 shrink-0" />
              : <AlertTriangle size={14} className="text-amber-600 shrink-0" />}
            <span className="font-bold text-ink-light">Last run</span>
            {lastResult.status === "ok" ? (
              <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-ink-light font-medium">
                <span><b className="text-ink font-bold">{fmt(lastResult.ingested)}</b> stored</span>
                <span className="text-slate-300">·</span>
                <span>{fmt(lastResult.duplicates)} dupes</span>
                <span className="text-slate-300">·</span>
                <span>{fmt(lastResult.ae)} AE</span>
                <span className="text-ink-muted">(from {fmt(lastResult.raw_posts)} captured)</span>
              </span>
            ) : (
              <span className="text-amber-700 font-medium">{lastResult.reason || lastResult.status}</span>
            )}
          </div>
        )}
      </Card>

      {/* Not-configured banner */}
      {status && !configured && (
        <Card className="border-amber-200 bg-amber-50">
          <div className="flex items-start gap-3">
            <ShieldAlert className="text-amber-600 mt-0.5" size={18} />
            <div>
              <p className="text-sm font-bold text-amber-900">Apify API token not set</p>
              <p className="text-xs text-amber-800 mt-1 font-medium">
                Add <code className="font-mono">APIFY_API_TOKEN=…</code> to your{" "}
                <code className="font-mono">.env</code> and restart the backend to enable live ingestion.
                The dashboard below shows whatever has already been captured.
              </p>
            </div>
          </div>
        </Card>
      )}

      {/* Patient Community Insights (myRAteam / Bezzy enrichment; hidden for platform-only) */}
      {hasData && insights?.community_insights && (
        <PatientCommunityInsights data={insights.community_insights} scope={scope} />
      )}

      {/* Per-platform "AbbVie vs other brands" curated gist (lead read) */}
      {hasData && insights && (
        <PlatformComparisonSection
          data={insights.platform_comparison}
          onRegenerate={regenerateBrief}
          regenerating={regenerating}
        />
      )}

      {/* AI social brief (qualitative narrative + regenerate) */}
      {hasData && insights && (
        <SocialBrief insights={insights} onRegenerate={regenerateBrief} regenerating={regenerating} />
      )}

      {/* Representative verbatim quotes ("what people are saying") */}
      {hasData && insights?.ai_brief?.verbatims?.length ? (
        <VerbatimQuotes verbatims={insights.ai_brief.verbatims} />
      ) : null}

      {/* KPI row */}
      {hasData && (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <AnimatedCard delay={0}>
            <KpiTile
              label="Captured posts"
              icon={<MessageSquare size={16} />}
              tooltip="Total public posts captured for the selected scope in this sample."
              value={fmt(insights?.total_posts)}
            >
              <div className="flex items-center gap-2 mt-2 text-[11px] text-ink-light font-medium">
                <span>{insights?.channels.length ?? 0} channel{(insights?.channels.length ?? 0) !== 1 ? "s" : ""}</span>
                {win && win.delta_pct != null && (
                  <>
                    <span className="text-slate-400">·</span>
                    <span className={`inline-flex items-center gap-0.5 font-bold ${win.delta_pct >= 0 ? "text-teal-600" : "text-red-600"}`}>
                      {win.delta_pct >= 0 ? <TrendingUp size={11} /> : <TrendingDown size={11} />}
                      {win.delta_pct >= 0 ? "+" : ""}{Math.round(win.delta_pct * 100)}% (7d)
                    </span>
                  </>
                )}
              </div>
            </KpiTile>
          </AnimatedCard>

          <AnimatedCard delay={0.05}>
            <KpiTile
              label="Overall sentiment"
              icon={<Activity size={16} />}
              tooltip="Average sentiment across all captured posts. +1 is very positive, -1 is very negative."
              value={sentOverall?.avg_sentiment != null ? sentOverall.avg_sentiment.toFixed(2) : "n/a"}
              valueColor={sentimentColor(sentOverall?.avg_sentiment)}
            >
              {sentOverall && (
                <>
                  <div className="flex items-center gap-3 mt-2 text-[11px] text-ink-light font-medium">
                    <span className="text-teal-600 font-bold">{sentOverall.positive} pos</span>
                    <span className="text-slate-500">{sentOverall.neutral} neu</span>
                    <span className="text-red-600 font-bold">{sentOverall.negative} neg</span>
                  </div>
                  <div className="mt-2">
                    <DistributionBar positive={sentOverall.positive} neutral={sentOverall.neutral} negative={sentOverall.negative} />
                  </div>
                </>
              )}
            </KpiTile>
          </AnimatedCard>

          <AnimatedCard delay={0.1}>
            <KpiTile
              label="Top brand · captured"
              icon={<TrendingUp size={16} />}
              tooltip="Brand with the largest share of captured posts in this sample."
              value={topBrand?.brand ?? "n/a"}
            >
              {topBrand && (
                <>
                  <p className="text-[11px] text-ink-light mt-2 font-medium">{pct(topBrand.post_share)} of posts · {fmt(topBrand.posts)} total</p>
                  <div className="mt-2 h-1.5 rounded-full bg-surface-2 overflow-hidden">
                    <div className="h-full rounded-full bg-brand-light transition-all" style={{ width: `${Math.round((topBrand.post_share ?? 0) * 100)}%` }} />
                  </div>
                </>
              )}
            </KpiTile>
          </AnimatedCard>

          <AnimatedCard delay={0.15}>
            <KpiTile
              label="AE signals"
              icon={<AlertTriangle size={16} />}
              tooltip="Posts flagged as possible adverse-event content. Routed for pharmacovigilance review."
              value={fmt(ae?.total)}
              valueColor={ae && ae.total > 0 ? "#DC2626" : undefined}
              alert={!!ae && ae.total > 0}
            >
              {ae && ae.total > 0 ? (
                <button onClick={onReviewAe} className="mt-2 inline-flex items-center gap-1 text-[11px] font-bold text-red-600 hover:text-red-700">
                  <AlertTriangle size={10} /> {Math.round((ae.rate ?? 0) * 1000) / 10}% of sample · review
                </button>
              ) : (
                <p className="text-[11px] text-ink-light mt-2 font-medium">No signals flagged</p>
              )}
            </KpiTile>
          </AnimatedCard>
        </div>
      )}

      {/* Adverse-event / pharmacovigilance panel */}
      {hasData && insights && <AePanel ae={insights.adverse_events} onReview={onReviewAe} />}

      {!hasData ? (
        <Card>
          <EmptyState
            icon={<Megaphone size={36} />}
            message={
              busy
                ? "Capturing posts… this runs in the background and can take a few minutes per channel."
                : `No posts captured yet for ${scope}. Click “Ingest now” (or use the search box above) to scrape public posts for this scope.`
            }
          />
        </Card>
      ) : (
        <>
          {/* ── Share of voice ── */}
          <section>
            <SectionHeading icon={<TrendingUp size={13} />} hint="captured sample, not market-level share of voice">Share of voice</SectionHeading>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <AnimatedCard delay={0}>
                <Card title="Posts by brand" className="h-full">
                  <p className="text-[11px] text-ink-muted -mt-2 mb-3">Post count by brand across the captured sample.</p>
                  <ResponsiveContainer width="100%" height={260}>
                    <BarChart data={sovByBrand} layout="vertical" margin={{ left: 16, right: 24 }}>
                      <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="#E2E8F0" />
                      <XAxis type="number" fontSize={11} tick={{ fill: "#64748B" }} allowDecimals={false} />
                      <YAxis type="category" dataKey="brand" width={96} fontSize={11} tick={{ fill: "#64748B" }} />
                      <Tooltip content={<ChartTooltip />} cursor={{ fill: "rgba(15,118,110,0.06)" }} />
                      <Bar dataKey="posts" name="posts" radius={[0, 4, 4, 0]}>
                        {sovByBrand.map((_entry, i) => (
                          <Cell key={i} fill={SOV_PALETTE[i % SOV_PALETTE.length]} />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </Card>
              </AnimatedCard>

              <AnimatedCard delay={0.05}>
                <Card title="Channel mix" className="h-full">
                  <p className="text-[11px] text-ink-muted -mt-2 mb-3">Where the captured conversation is happening, by post volume.</p>
                  {channelMix.length ? (
                    <ResponsiveContainer width="100%" height={260}>
                      <PieChart>
                        <Pie data={channelMix} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={100} innerRadius={55}
                          label={({ name, percent }: any) => `${name} ${(percent * 100).toFixed(0)}%`} labelLine={{ stroke: "#CBD5E1" }}>
                          {channelMix.map((d, i) => (
                            <Cell key={i} fill={CHANNEL_COLOR[d.channel] ?? SOV_PALETTE[i % SOV_PALETTE.length]} />
                          ))}
                        </Pie>
                        <Tooltip content={<ChartTooltip />} />
                      </PieChart>
                    </ResponsiveContainer>
                  ) : (
                    <EmptyState message="No channel data yet." icon={<Hash size={28} />} />
                  )}
                </Card>
              </AnimatedCard>
            </div>
          </section>

          {/* ── Sentiment ── */}
          <section>
            <SectionHeading icon={<Activity size={13} />} hint="teal positive · amber neutral · red negative">Sentiment</SectionHeading>
            {insights && insights.total_comments > 0 && (
              <div className="mb-4"><CommentSentimentPanel insights={insights} /></div>
            )}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <AnimatedCard delay={0}>
                <Card title="Sentiment by brand" className="h-full">
                  <p className="text-[11px] text-ink-muted -mt-2 mb-3">Average sentiment from -1 to +1 across captured posts.</p>
                  <ResponsiveContainer width="100%" height={260}>
                    <BarChart data={sentByBrand} margin={{ left: 8, right: 8 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#E2E8F0" />
                      <XAxis dataKey="brand" fontSize={11} tick={{ fill: "#64748B" }} interval={0} angle={-20} textAnchor="end" height={64} />
                      <YAxis domain={[-1, 1]} fontSize={11} tick={{ fill: "#64748B" }} />
                      <Tooltip content={<ChartTooltip />} cursor={{ fill: "rgba(148,163,184,0.12)" }} />
                      <ReferenceLine y={0} stroke="#94A3B8" />
                      <Bar dataKey="avg_sentiment" name="avg sentiment" radius={[4, 4, 0, 0]}>
                        {sentByBrand.map((row, i) => (
                          <Cell key={i} fill={sentimentColor(row.avg_sentiment)} />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </Card>
              </AnimatedCard>

              <AnimatedCard delay={0.05}>
                <Card title="Sentiment distribution" className="h-full">
                  <p className="text-[11px] text-ink-muted -mt-2 mb-3">Positive, neutral and negative split across the whole sample.</p>
                  {sentDist.length ? (
                    <div className="relative">
                      <ResponsiveContainer width="100%" height={260}>
                        <PieChart>
                          <Pie data={sentDist} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={100} innerRadius={62}
                            label={({ name, percent }: any) => `${name} ${(percent * 100).toFixed(0)}%`} labelLine={{ stroke: "#CBD5E1" }}>
                            {sentDist.map((d, i) => (
                              <Cell key={i} fill={d.color} />
                            ))}
                          </Pie>
                          <Tooltip content={<ChartTooltip />} />
                        </PieChart>
                      </ResponsiveContainer>
                      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
                        <span className="text-2xl font-display font-bold tabular-nums" style={{ color: sentimentColor(sentOverall?.avg_sentiment) }}>
                          {sentOverall?.avg_sentiment != null ? sentOverall.avg_sentiment.toFixed(2) : "n/a"}
                        </span>
                        <span className="text-[10px] font-bold uppercase tracking-widest text-ink-muted">avg score</span>
                      </div>
                    </div>
                  ) : (
                    <EmptyState message="No scored posts yet." icon={<Activity size={28} />} />
                  )}
                </Card>
              </AnimatedCard>

            </div>

            {/* Left column stacks channel sentiment + volume so it fills the
                height of the taller Top themes card on the right (no dead space). */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-4">
              <div className="flex flex-col gap-4">
                <AnimatedCard delay={0.1}>
                  <Card title="Sentiment by channel">
                    <p className="text-[11px] text-ink-muted -mt-2 mb-3">Which platforms skew positive or negative.</p>
                    {sentByChannel.length ? (
                      <ResponsiveContainer width="100%" height={240}>
                        <BarChart data={sentByChannel} margin={{ left: 8, right: 8 }}>
                          <CartesianGrid strokeDasharray="3 3" stroke="#E2E8F0" />
                          <XAxis dataKey="channel" fontSize={11} tickFormatter={channelLabel} tick={{ fill: "#64748B" }} />
                          <YAxis domain={[-1, 1]} fontSize={11} tick={{ fill: "#64748B" }} />
                          <Tooltip content={<ChartTooltip />} cursor={{ fill: "rgba(148,163,184,0.12)" }} />
                          <ReferenceLine y={0} stroke="#94A3B8" />
                          <Bar dataKey="avg_sentiment" name="avg sentiment" radius={[4, 4, 0, 0]}>
                            {sentByChannel.map((row, i) => (
                              <Cell key={i} fill={sentimentColor(row.avg_sentiment)} />
                            ))}
                          </Bar>
                        </BarChart>
                      </ResponsiveContainer>
                    ) : (
                      <EmptyState message="No channel sentiment yet." icon={<Activity size={28} />} />
                    )}
                  </Card>
                </AnimatedCard>

                <AnimatedCard delay={0.2}>
                  <Card title="Volume over time">
                    <p className="text-[11px] text-ink-muted -mt-2 mb-3">Daily captured posts, stacked by channel.</p>
                    {volRows.length ? (
                      <ResponsiveContainer width="100%" height={240}>
                        <AreaChart data={volRows} margin={{ left: 8, right: 8 }}>
                          <defs>
                            {volChannels.map((c) => (
                              <linearGradient key={c} id={`vol-${c}`} x1="0" y1="0" x2="0" y2="1">
                                <stop offset="5%" stopColor={CHANNEL_COLOR[c] ?? "#64748B"} stopOpacity={0.5} />
                                <stop offset="95%" stopColor={CHANNEL_COLOR[c] ?? "#64748B"} stopOpacity={0.05} />
                              </linearGradient>
                            ))}
                          </defs>
                          <CartesianGrid strokeDasharray="3 3" stroke="#E2E8F0" />
                          <XAxis dataKey="date" fontSize={11} tick={{ fill: "#64748B" }} />
                          <YAxis allowDecimals={false} fontSize={11} tick={{ fill: "#64748B" }} />
                          <Tooltip content={<ChartTooltip />} />
                          <Legend wrapperStyle={{ fontSize: 11, fontWeight: 600 }} />
                          {volChannels.map((c) => (
                            <Area key={c} type="monotone" dataKey={c} name={channelLabel(c)} stackId="1"
                              stroke={CHANNEL_COLOR[c] ?? "#64748B"} fill={`url(#vol-${c})`} strokeWidth={2} />
                          ))}
                        </AreaChart>
                      </ResponsiveContainer>
                    ) : (
                      <EmptyState message="No timestamped posts to chart yet." icon={<Activity size={28} />} />
                    )}
                  </Card>
                </AnimatedCard>
              </div>

              <AnimatedCard delay={0.15}>
                <Card title="Top themes" className="h-full">
                  <p className="text-[11px] text-ink-muted -mt-2 mb-3">Most-discussed topics, colored by average sentiment. Click a theme to filter the posts below.</p>
                  {topTopics.length ? (
                    <div className="space-y-1">
                      {topTopics.map((t) => {
                        const activeTopic = topicFilter === t.topic;
                        return (
                          <button
                            key={t.topic}
                            onClick={() => onPickTopic(t.topic)}
                            className={`w-full text-left rounded-lg px-2 py-1.5 transition-colors ${activeTopic ? "bg-brand-surface ring-1 ring-brand-light/40" : "hover:bg-surface-1"}`}
                          >
                            <div className="flex items-center justify-between gap-2 mb-1">
                              <span className="text-xs font-semibold text-ink truncate" title={t.topic}>{t.topic}</span>
                              <div className="flex items-center gap-2 shrink-0">
                                <span className="text-xs font-bold text-ink tabular-nums">{t.count}</span>
                                <span className="text-[11px] font-bold px-1.5 py-0.5 rounded-full tabular-nums"
                                  style={{ backgroundColor: `${sentimentColor(t.avg_sentiment)}1A`, color: sentimentColor(t.avg_sentiment) }}>
                                  {t.avg_sentiment === null ? "n/a" : t.avg_sentiment.toFixed(2)}
                                </span>
                              </div>
                            </div>
                            <div className="h-1.5 rounded-full bg-surface-2 overflow-hidden">
                              <div className="h-full rounded-full bg-brand-light"
                                style={{ width: `${Math.max(4, (t.count / (topTopics[0]?.count || 1)) * 100)}%` }} />
                            </div>
                          </button>
                        );
                      })}
                    </div>
                  ) : (
                    <EmptyState message="No topics tagged yet." icon={<Hash size={28} />} />
                  )}
                </Card>
              </AnimatedCard>
            </div>
          </section>

          {/* ── Engagement leaders ── */}
          {leaders.length > 0 && (
            <section>
              <SectionHeading icon={<TrendingUp size={13} />} hint="raw per-channel metric, compared within a channel only">Engagement leaders</SectionHeading>
              <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
                {leaders.map((g) => {
                  const maxEng = Math.max(...g.posts.map((x) => x.engagement ?? 0), 0);
                  return (
                    <Card key={g.channel}>
                      <div className="flex items-center justify-between gap-2 mb-3 pb-3 border-b border-line">
                        <ChannelPill channel={g.channel} />
                        <span className="text-[11px] font-semibold text-ink-muted">top by {g.metric}</span>
                      </div>
                      <ul className="space-y-1">
                        {g.posts.map((p, i) => {
                          const full = p.post_url ? posts.find((x) => x.post_url === p.post_url) ?? null : null;
                          const relPct = maxEng > 0 ? Math.round(((p.engagement ?? 0) / maxEng) * 100) : 0;
                          const rankCls = ["bg-amber-400 text-amber-950", "bg-slate-300 text-slate-700", "bg-orange-300 text-orange-900"][i] ?? "bg-surface-2 text-ink-light";
                          const hasComments = p.comment_count != null && p.comment_count > 0;
                          return (
                            <li key={i}>
                              <button
                                onClick={() => full && setDrawerPost(full)}
                                disabled={!full}
                                className={`group w-full text-left rounded-xl border p-3 transition-colors ${full ? "border-transparent hover:border-line hover:bg-surface-1 cursor-pointer" : "border-transparent cursor-default"}`}
                              >
                                <div className="flex items-start justify-between gap-3">
                                  <div className="flex items-start gap-2 min-w-0">
                                    <span className={`mt-0.5 h-5 w-5 shrink-0 rounded-full flex items-center justify-center text-[10px] font-extrabold ${rankCls}`}>{i + 1}</span>
                                    <div className="min-w-0">
                                      <div className="flex items-center gap-1.5">
                                        <span className="text-xs font-bold text-ink truncate">{p.brand}</span>
                                        <span className="h-1.5 w-1.5 rounded-full shrink-0" style={{ backgroundColor: sentimentColor(p.sentiment) }} title={p.sentiment_label ?? "unscored"} />
                                        {full && <ExternalLink size={11} className="text-ink-muted opacity-0 group-hover:opacity-100 transition-opacity shrink-0" />}
                                      </div>
                                      {(p.topic || hasComments) && (
                                        <p className="text-[10px] text-ink-muted truncate">
                                          {p.topic}{p.topic && hasComments ? " · " : ""}{hasComments ? `${fmt(p.comment_count)} comments` : ""}
                                        </p>
                                      )}
                                    </div>
                                  </div>
                                  <span className="text-sm font-display font-bold text-ink tabular-nums shrink-0 leading-none mt-0.5">{fmt(p.engagement)}</span>
                                </div>
                                <p className="text-[11px] text-ink-light line-clamp-2 mt-1.5">{p.snippet}</p>
                                {maxEng > 0 && (
                                  <div className="mt-2 h-1 rounded-full bg-surface-2 overflow-hidden">
                                    <div className="h-full rounded-full" style={{ width: `${relPct}%`, backgroundColor: CHANNEL_COLOR[g.channel] ?? "#64748B" }} />
                                  </div>
                                )}
                              </button>
                            </li>
                          );
                        })}
                        {g.posts.length === 0 && <li className="text-[11px] text-ink-muted px-2">No posts.</li>}
                      </ul>
                    </Card>
                  );
                })}
              </div>
            </section>
          )}

          {/* ── Captured posts ── */}
          <section ref={tableRef}>
            <SectionHeading icon={<MessageSquare size={13} />} hint={`${fmt(visiblePosts.length)} of ${fmt(posts.length)} shown · click a row for detail`}>Captured posts</SectionHeading>
            <Card>
              {/* Toolbar */}
              <div className="flex flex-wrap items-center gap-3 mb-4">
                <div className="relative flex-1 min-w-[220px]">
                  <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-ink-muted" />
                  <input
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="Search posts, brands, themes…"
                    className="w-full pl-9 pr-3 py-2 rounded-xl border border-line bg-surface-1 text-sm text-ink placeholder:text-ink-muted focus:outline-none focus:ring-2 focus:ring-brand-light/40"
                  />
                </div>
                <div className="relative">
                  <button
                    onClick={() => setChannelMenuOpen((v) => !v)}
                    className={`inline-flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-bold border transition-colors ${selected.length ? "border-brand-light/50 bg-brand-surface text-brand-dark" : "border-line text-ink-light hover:text-ink"}`}
                  >
                    <Filter size={13} />
                    {selected.length ? `${selected.length} channel${selected.length !== 1 ? "s" : ""}` : "All channels"}
                    <ChevronDown size={13} className={`transition-transform ${channelMenuOpen ? "rotate-180" : ""}`} />
                  </button>
                  {channelMenuOpen && (
                    <>
                      <button aria-hidden tabIndex={-1} className="fixed inset-0 z-10 cursor-default" onClick={() => setChannelMenuOpen(false)} />
                      <div className="absolute left-0 top-full mt-1.5 z-20 w-52 rounded-xl border border-line bg-canvas-card shadow-xl p-1.5">
                        <button
                          onClick={() => setSelected([])}
                          className="w-full flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-xs font-bold text-ink hover:bg-surface-1 transition-colors"
                        >
                          All channels
                          {selected.length === 0 && <Check size={13} className="ml-auto text-brand" />}
                        </button>
                        <div className="my-1 h-px bg-line" />
                        {visibleChannels.map((c) => {
                          const on = selected.includes(c);
                          const count = channelCounts[c] ?? 0;
                          return (
                            <button
                              key={c}
                              onClick={() => toggleChannel(c)}
                              className="w-full flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-xs font-semibold text-ink hover:bg-surface-1 transition-colors"
                            >
                              <span className="h-2 w-2 rounded-full shrink-0" style={{ backgroundColor: CHANNEL_COLOR[c] ?? "#64748B" }} />
                              {channelLabel(c)}
                              <span className="ml-auto flex items-center gap-2">
                                {count > 0 && <span className="text-[10px] text-ink-muted tabular-nums">{count}</span>}
                                {on && <Check size={13} className="text-brand" />}
                              </span>
                            </button>
                          );
                        })}
                      </div>
                    </>
                  )}
                </div>
                <div className="flex items-center gap-1 p-1 rounded-xl bg-slate-100">
                  {(([["recent", "Recent"], ["engagement", "Top engagement"], ["negative", "Most negative"]]) as [SortKey, string][]).map(([k, lbl]) => (
                    <button
                      key={k}
                      onClick={() => setSortKey(k)}
                      className={`px-3 py-1.5 rounded-lg text-xs font-bold transition-all ${sortKey === k ? "bg-white shadow text-brand-dark" : "text-ink-light hover:text-ink"}`}
                    >
                      {lbl}
                    </button>
                  ))}
                </div>
                <button
                  onClick={() => setAeOnly((v) => !v)}
                  className={`inline-flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-bold border transition-colors ${aeOnly ? "bg-red-600 border-red-600 text-white" : "border-line text-ink-light hover:text-ink"}`}
                >
                  <AlertTriangle size={13} /> AE only
                </button>
              </div>

              {/* Active filters */}
              {filterActive && (
                <div className="flex flex-wrap items-center gap-2 mb-3">
                  {selected.map((c) => (
                    <span key={c} className="inline-flex items-center gap-1 rounded-full bg-surface-2 px-2.5 py-1 text-[11px] font-bold text-ink">
                      {channelLabel(c)} <button onClick={() => toggleChannel(c)} className="text-ink-muted hover:text-ink"><X size={11} /></button>
                    </span>
                  ))}
                  {topicFilter && (
                    <span className="inline-flex items-center gap-1 rounded-full bg-brand-surface px-2.5 py-1 text-[11px] font-bold text-brand-dark">
                      {topicFilter} <button onClick={() => setTopicFilter(null)} className="text-brand/60 hover:text-brand-dark"><X size={11} /></button>
                    </span>
                  )}
                  {aeOnly && (
                    <span className="inline-flex items-center gap-1 rounded-full bg-red-100 px-2.5 py-1 text-[11px] font-bold text-red-700">
                      AE only <button onClick={() => setAeOnly(false)} className="text-red-400 hover:text-red-700"><X size={11} /></button>
                    </span>
                  )}
                  <button
                    onClick={() => { setSelected([]); setTopicFilter(null); setAeOnly(false); setQuery(""); }}
                    className="text-[11px] font-bold text-ink-muted hover:text-ink"
                  >
                    clear all
                  </button>
                </div>
              )}

              {visiblePosts.length === 0 ? (
                <EmptyState message="No posts match the current filters." icon={<Megaphone size={28} />} />
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[860px] text-sm">
                    <thead>
                      <tr className="text-left border-b-2 border-line">
                        <th className="pb-3 pr-3 font-bold text-xs text-ink-light uppercase tracking-widest">Channel</th>
                        <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">
                          <span className="inline-flex items-center gap-1"><InfoTooltip content="Safety flags raised by the classifier. AE marks a possible adverse-event mention, routed for pharmacovigilance review." /> Flags</span>
                        </th>
                        <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">Brand</th>
                        <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">
                          <span className="inline-flex items-center gap-1"><InfoTooltip content={"The theme of the post:\n\u2022 Efficacy: outcomes & dosing\n\u2022 Safety: side effects & risks\n\u2022 Access: coverage & cost\n\u2022 Comparative: vs. competitors\n\u2022 General: broad/exploratory"} /> Theme</span>
                        </th>
                        <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">Post</th>
                        <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">Sentiment</th>
                        <th className="pb-3 text-right font-bold text-xs text-ink-light uppercase tracking-widest">
                          <span className="inline-flex items-center gap-1"><InfoTooltip content="Raw per-channel engagement (upvotes/views/likes). Not comparable across channels." /> Engagement</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {visiblePosts.slice(0, 200).map((p, i) => (
                        <motion.tr
                          key={p.id}
                          initial={{ opacity: 0, y: 6 }}
                          animate={{ opacity: 1, y: 0 }}
                          transition={{ delay: Math.min(i * 0.01, 0.4) }}
                          onClick={() => setDrawerPost(p)}
                          className="border-b border-line/60 hover:bg-brand-surface/50 transition-colors align-top cursor-pointer"
                        >
                          <td className="py-3 pr-3 whitespace-nowrap">
                            <ChannelPill channel={p.channel} />
                          </td>
                          <td className="py-3 whitespace-nowrap">
                            {p.ae_flag ? <AeChip /> : <span className="text-ink-muted">·</span>}
                          </td>
                          <td className="py-3 text-ink font-medium">{p.brand_focus || "n/a"}</td>
                          <td className="py-3"><ThemeBadge theme={p.domain} /></td>
                          <td className="py-3 max-w-[200px] lg:max-w-xs xl:max-w-md text-ink-light">
                            <p className="line-clamp-2">{p.text}</p>
                            <div className="flex flex-wrap items-center gap-2 mt-0.5">
                              {p.is_translated && (
                                <span className="inline-flex items-center gap-1 rounded-full bg-violet-50 border border-violet-200 px-1.5 py-0.5 text-[10px] font-bold text-violet-700">
                                  <Languages size={10} /> {p.language || "translated"}→EN
                                </span>
                              )}
                              {p.comments_captured > 0 && (
                                <span className="inline-flex items-center gap-1 text-[10px] font-semibold text-ink-muted">
                                  <MessageCircle size={10} /> {fmt(p.comments_captured)}
                                  {p.comment_sentiment != null && (
                                    <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: sentimentColor(p.comment_sentiment) }} title={`avg comment sentiment ${p.comment_sentiment.toFixed(2)}`} />
                                  )}
                                </span>
                              )}
                              {p.post_url && (
                                <a
                                  href={p.post_url}
                                  target="_blank"
                                  rel="noreferrer"
                                  onClick={(e) => e.stopPropagation()}
                                  className="inline-flex items-center gap-1 text-[11px] font-bold text-brand hover:text-brand-dark"
                                >
                                  <ExternalLink size={11} /> source
                                </a>
                              )}
                            </div>
                          </td>
                          <td className="py-3"><SentimentBadge score={p.sentiment} /></td>
                          <td className="py-3 text-right text-ink-light tabular-nums whitespace-nowrap">
                            {fmt(p.engagement_score)}
                            <span className="ml-1 text-[10px] text-ink-muted">{p.engagement_metric}</span>
                          </td>
                        </motion.tr>
                      ))}
                    </tbody>
                  </table>
                  {visiblePosts.length > 200 && (
                    <p className="text-xs text-ink-muted mt-3 text-center">Showing the first 200 of {fmt(visiblePosts.length)} matching posts. Refine with search or filters to narrow the list.</p>
                  )}
                </div>
              )}
            </Card>
          </section>
        </>
      )}

      <AnimatePresence>
        {drawerPost && <PostDrawer post={drawerPost} onClose={() => setDrawerPost(null)} />}
      </AnimatePresence>
    </div>
  );
}
