import { useEffect, useState } from "react";
import {
  ArrowRight,
  Award,
  BarChart3,
  Globe,
  Link2,
  ListChecks,
  Search,
  Star,
  Target,
  TrendingUp,
} from "lucide-react";
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend as ChartLegend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  api,
  type CitationOpportunity,
  type CitationTrend,
  type PreferredSourceGap,
  type QueryFanout,
  type RecFilters,
  type ShareOfCitation,
} from "../api/client";
import { Card, EmptyState, InfoTooltip, Spinner } from "./ui";

/* Citation-gap analytics fused with the classified Source Authority graph (BR-005 / FR-706a).
   Five transparent, plain-math views:
     - Share of AI citations (control-based — the SAME number as the Source Authority page).
     - Citation opportunities: authoritative NON-AbbVie domains to earn a citation on.
     - Preferred-source gaps: Medical-Affairs preferred domains AI keeps omitting.
     - Query fanouts: the real search terms grounded models ran.
     - Citation trend: AbbVie vs competitor vs independent share over time. */

function pctWidth(n: number): string {
  return `${Math.max(0, Math.min(100, n))}%`;
}

const CONTROL_BADGE: Record<string, { label: string; cls: string }> = {
  ABBVIE: { label: "AbbVie", cls: "bg-teal-100 text-teal-800" },
  COMPETITOR: { label: "Competitor", cls: "bg-red-100 text-red-700" },
  INDEPENDENT: { label: "Independent", cls: "bg-sky-100 text-sky-800" },
  UNKNOWN: { label: "Unclassified", cls: "bg-slate-100 text-ink-light" },
};

function titleize(v: string | null | undefined): string {
  if (!v) return "";
  return v
    .toLowerCase()
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

function ShareMetric({
  label,
  pct,
  citations,
  answers,
  dotClass,
  valueClass,
  surfaceClass,
}: {
  label: string;
  pct: number;
  citations: number;
  answers: number;
  dotClass: string;
  valueClass: string;
  surfaceClass: string;
}) {
  return (
    <div className={`rounded-xl border border-line p-3 ${surfaceClass}`}>
      <div className="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wide text-ink-light">
        <span className={`h-2 w-2 rounded-full ${dotClass}`} />
        {label}
      </div>
      <p className={`mt-1 text-2xl font-display font-bold tabular-nums ${valueClass}`}>{pct}%</p>
      <p className="mt-0.5 text-[11px] font-medium text-ink-muted">
        {citations.toLocaleString()} citation{citations !== 1 ? "s" : ""} · {answers} answer{answers !== 1 ? "s" : ""}
      </p>
    </div>
  );
}

function ShareVoice({
  share,
  topOpportunity,
}: {
  share: ShareOfCitation;
  topOpportunity?: CitationOpportunity;
}) {
  const voice = Object.fromEntries(share.voice.map((v) => [v.control_type, v]));
  const knownCount = ["ABBVIE", "INDEPENDENT", "COMPETITOR"].reduce(
    (sum, control) => sum + (voice[control]?.citation_count ?? 0),
    0,
  );
  const unknownPct = voice.UNKNOWN?.share_pct
    ?? Math.max(0, +(100 - share.abbvie_share_pct - share.independent_share_pct - share.competitor_share_pct).toFixed(1));
  const slices = [
    {
      key: "ABBVIE",
      label: "AbbVie",
      pct: share.abbvie_share_pct,
      citations: voice.ABBVIE?.citation_count ?? 0,
      answers: voice.ABBVIE?.response_count ?? 0,
      dotClass: "bg-brand-dark",
      valueClass: "text-brand-dark",
      surfaceClass: "bg-brand-surface/50",
    },
    {
      key: "INDEPENDENT",
      label: "Independent",
      pct: share.independent_share_pct,
      citations: voice.INDEPENDENT?.citation_count ?? 0,
      answers: voice.INDEPENDENT?.response_count ?? 0,
      dotClass: "bg-sky-400",
      valueClass: "text-sky-700",
      surfaceClass: "bg-sky-50/70",
    },
    {
      key: "COMPETITOR",
      label: "Competitor",
      pct: share.competitor_share_pct,
      citations: voice.COMPETITOR?.citation_count ?? 0,
      answers: voice.COMPETITOR?.response_count ?? 0,
      dotClass: "bg-red-400",
      valueClass: "text-red-600",
      surfaceClass: "bg-red-50/70",
    },
    {
      key: "UNKNOWN",
      label: "Unclassified",
      pct: unknownPct,
      citations: voice.UNKNOWN?.citation_count ?? Math.max(0, share.total_citations - knownCount),
      answers: voice.UNKNOWN?.response_count ?? 0,
      dotClass: "bg-slate-400",
      valueClass: "text-ink-light",
      surfaceClass: "bg-slate-50",
    },
  ];
  const topCompetitor = share.competitors[0];
  const independentLead = +(share.independent_share_pct - share.abbvie_share_pct).toFixed(1);

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 xl:grid-cols-4 gap-3">
        {slices.map((slice) => (
          <ShareMetric
            key={slice.key}
            label={slice.label}
            pct={slice.pct}
            citations={slice.citations}
            answers={slice.answers}
            dotClass={slice.dotClass}
            valueClass={slice.valueClass}
            surfaceClass={slice.surfaceClass}
          />
        ))}
      </div>

      <div
        className="flex h-3.5 w-full overflow-hidden rounded-full bg-slate-100 ring-1 ring-inset ring-slate-200"
        role="img"
        aria-label={slices.map((slice) => `${slice.label} ${slice.pct}%`).join(", ")}
      >
        {slices.filter((slice) => slice.pct > 0).map((slice) => (
          <div
            key={slice.key}
            className={`h-full ${slice.dotClass}`}
            style={{ width: pctWidth(slice.pct) }}
            title={`${slice.label}: ${slice.pct}% (${slice.citations} citations)`}
          />
        ))}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <div className="rounded-xl border border-brand-light/20 bg-brand-surface/45 p-3.5">
          <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wide text-brand-dark">
            <BarChart3 size={14} /> Key signal
          </div>
          <p className="mt-2 text-xs leading-relaxed text-ink-light">
            Independent publishers shape <span className="font-bold text-ink">{share.independent_share_pct}%</span> of cited evidence
            {independentLead > 0 && <> · <span className="font-bold text-ink">{independentLead} points</span> above AbbVie-controlled sources</>}.
            {unknownPct > 0 && <> <span className="font-bold text-ink">{unknownPct}%</span> remains unclassified.</>}
          </p>
        </div>

        <div className="rounded-xl border border-amber-200 bg-amber-50/70 p-3.5">
          <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wide text-amber-800">
            <Target size={14} /> Next intervention
          </div>
          {topOpportunity ? (
            <>
              <p className="mt-2 truncate text-sm font-bold text-ink" title={topOpportunity.domain}>{topOpportunity.domain}</p>
              <p className="mt-0.5 text-xs leading-relaxed text-ink-light">
                Reaches {topOpportunity.response_count} answer{topOpportunity.response_count !== 1 ? "s" : ""}
                {topOpportunity.weak_position_count > 0 && ` · ${topOpportunity.weak_position_count} weak-position`}
              </p>
              <a href="#citation-interventions" className="mt-2 inline-flex items-center gap-1 text-[11px] font-bold text-brand-dark hover:text-brand">
                Review intervention <ArrowRight size={12} />
              </a>
            </>
          ) : (
            <p className="mt-2 text-xs leading-relaxed text-ink-light">No independent citation opportunity is present for these filters.</p>
          )}
        </div>

        <div className="rounded-xl border border-red-100 bg-red-50/60 p-3.5">
          <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wide text-red-700">
            <Award size={14} /> Competitor watch
          </div>
          <p className="mt-2 text-sm font-bold tabular-nums text-ink">
            {share.competitor_total_citations.toLocaleString()} competitor citation{share.competitor_total_citations !== 1 ? "s" : ""}
          </p>
          <p className="mt-0.5 text-xs leading-relaxed text-ink-light">
            {topCompetitor
              ? `${topCompetitor.authority_domain} leads with ${topCompetitor.citation_count} across ${topCompetitor.response_count} answer${topCompetitor.response_count !== 1 ? "s" : ""}.`
              : "No competitor-controlled sources were cited in this scope."}
          </p>
        </div>
      </div>
    </div>
  );
}

function opportunityAction(o: CitationOpportunity): string {
  if (o.is_preferred) return "Close this preferred-source citation gap";
  if (o.control_type === "UNKNOWN") return "Classify the source, then assess evidence inclusion";
  if (o.weak_position_count > 0) return "Strengthen evidence for weak-position answers";
  return "Pursue authoritative coverage on this source";
}

function OpportunityRow({ o, rank }: { o: CitationOpportunity; rank: number }) {
  const badge = CONTROL_BADGE[o.control_type] || CONTROL_BADGE.UNKNOWN;
  const authority = titleize(o.display_category || o.authority_type);
  return (
    <tr className="border-b border-line last:border-0 hover:bg-slate-50/70 transition-colors">
      <td className="py-3 pr-5 align-top">
        <div className="flex min-w-[260px] items-start gap-2.5">
          <span className="mt-0.5 inline-flex h-6 min-w-6 items-center justify-center rounded-full bg-slate-200 px-1.5 text-xs font-bold text-ink-light">
            {rank}
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-1.5">
              {o.is_preferred ? <Star size={13} className="shrink-0 text-amber-500" /> : <Globe size={13} className="shrink-0 text-brand-light" />}
              <span className="font-semibold text-ink">{o.domain}</span>
              {o.is_preferred && (
                <span className="rounded-md bg-amber-100 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-amber-800">Preferred</span>
              )}
              <span className={`rounded-md px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide ${badge.cls}`}>{badge.label}</span>
            </div>
            {o.publisher_name && o.publisher_name !== o.domain && (
              <p className="mt-0.5 max-w-[300px] truncate text-[11px] text-ink-muted">{o.publisher_name}</p>
            )}
            {authority && authority !== "Other" && (
              <p className="mt-1 text-[11px] font-medium text-ink-light">{authority}</p>
            )}
          </div>
        </div>
      </td>
      <td className="whitespace-nowrap py-3 pr-5 align-top">
        <p className="text-sm font-bold tabular-nums text-ink">{o.citation_count} citation{o.citation_count !== 1 ? "s" : ""}</p>
        <p className="mt-0.5 text-[11px] text-ink-muted">across {o.response_count} answer{o.response_count !== 1 ? "s" : ""}</p>
      </td>
      <td className="py-3 pr-5 align-top">
        {o.weak_position_count > 0 ? (
          <span className="inline-flex whitespace-nowrap rounded-md bg-red-100 px-2 py-1 text-[11px] font-semibold text-red-700">
            {o.weak_position_count} weak-position answer{o.weak_position_count !== 1 ? "s" : ""}
          </span>
        ) : (
          <span className="text-[11px] font-medium text-ink-muted">No weak-position link</span>
        )}
        {o.brands.length > 0 && (
          <p className="mt-1.5 max-w-[180px] truncate text-[11px] text-ink-light" title={o.brands.join(", ")}>{o.brands.join(", ")}</p>
        )}
      </td>
      <td className="min-w-[210px] py-3 pr-5 align-top text-xs font-medium leading-relaxed text-ink-light">
        {opportunityAction(o)}
      </td>
      <td className="py-3 text-right align-top">
        <span
          className="inline-flex min-w-12 flex-col items-center rounded-lg bg-brand-surface px-2.5 py-1 text-brand-dark"
          title="Distinct answer reach plus competitive-gap weight"
        >
          <span className="text-sm font-display font-bold tabular-nums">{o.opportunity_score}</span>
          <span className="text-[9px] font-bold uppercase tracking-wide text-ink-muted">score</span>
        </span>
      </td>
    </tr>
  );
}

function PreferredGapRow({ g }: { g: PreferredSourceGap }) {
  const pct = g.absence_pct ?? 0;
  return (
    <div className="py-2.5 border-b border-line last:border-0">
      <div className="flex items-center gap-2">
        <Star size={13} className="text-amber-500 shrink-0" />
        <span className="font-semibold text-ink truncate">{g.authority_domain}</span>
        <span className="ml-auto text-xs font-bold tabular-nums text-red-600">
          {g.absence_pct === null ? "N/A" : `${pct}%`}
        </span>
      </div>
      <div className="mt-1 h-2 w-full rounded-full bg-slate-100 overflow-hidden">
        <div className="h-full bg-red-400" style={{ width: pctWidth(pct) }} />
      </div>
      <p className="mt-1 text-[11px] text-ink-muted">
        {g.observations === 0
          ? "Not yet observed in a grounded answer"
          : `AI omitted this preferred source in ${g.absent} of ${g.observations} answers`}
      </p>
    </div>
  );
}

function FanoutRow({ q, max }: { q: QueryFanout; max: number }) {
  return (
    <div className="py-2 border-b border-line last:border-0">
      <div className="flex items-center gap-2">
        <Search size={12} className="text-brand-light shrink-0" />
        <span className="text-sm text-ink truncate flex-1" title={q.query}>{q.query}</span>
        <span className="text-xs font-bold tabular-nums text-ink-light">{q.count}</span>
      </div>
      <div className="mt-1 h-1.5 w-full rounded-full bg-slate-100 overflow-hidden">
        <div
          className="h-full bg-brand-light"
          style={{ width: pctWidth(max ? (q.count / max) * 100 : 0) }}
        />
      </div>
    </div>
  );
}

const MIN_TREND_PERIODS = 3;

function shortDate(value: string): string {
  const date = new Date(`${value}T00:00:00`);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function signedPoints(value: number): string {
  return `${value > 0 ? "+" : ""}${value.toFixed(1)} pts`;
}

function CitationTrendTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="max-w-[260px] rounded-xl border border-line bg-canvas-card p-3 text-xs shadow-lg">
      <p className="mb-1.5 font-bold text-ink">{shortDate(String(label))}</p>
      {payload.map((item: any) => (
        <div key={item.dataKey} className="flex items-center gap-2 py-0.5">
          <span className="h-2 w-2 rounded-full" style={{ backgroundColor: item.color || item.stroke || item.fill }} />
          <span className="flex-1 font-medium text-ink-light">{item.name}</span>
          <span className="font-bold tabular-nums text-ink">
            {item.dataKey === "total" ? Number(item.value).toLocaleString() : `${item.value}%`}
          </span>
        </div>
      ))}
    </div>
  );
}

function TrendBars({ trend }: { trend: CitationTrend }) {
  const periods = trend.periods.map((period) => ({
    ...period,
    unknown_share_pct: period.unknown_share_pct
      ?? Math.max(0, +(100 - period.abbvie_share_pct - period.independent_share_pct - period.competitor_share_pct).toFixed(1)),
  }));
  const first = periods[0];
  const last = periods[periods.length - 1];
  const metrics = [
    { label: "AbbVie", value: last.abbvie_share_pct, delta: last.abbvie_share_pct - first.abbvie_share_pct, cls: "text-brand-dark", dot: "bg-brand-dark", goodDirection: "up" },
    { label: "Independent", value: last.independent_share_pct, delta: last.independent_share_pct - first.independent_share_pct, cls: "text-sky-700", dot: "bg-sky-400", goodDirection: "neutral" },
    { label: "Competitor", value: last.competitor_share_pct, delta: last.competitor_share_pct - first.competitor_share_pct, cls: "text-red-600", dot: "bg-red-400", goodDirection: "down" },
    { label: "Unclassified", value: last.unknown_share_pct, delta: last.unknown_share_pct - first.unknown_share_pct, cls: "text-ink-light", dot: "bg-slate-400", goodDirection: "down" },
  ];

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {metrics.map((metric) => {
          const delta = +metric.delta.toFixed(1);
          const deltaClass = delta === 0 || metric.goodDirection === "neutral"
            ? "text-ink-muted"
            : (metric.goodDirection === "up" ? delta > 0 : delta < 0)
              ? "text-teal-700"
              : "text-red-600";
          return (
            <div key={metric.label} className="rounded-xl border border-line bg-surface-0/60 px-3 py-2.5">
              <div className="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wide text-ink-muted">
                <span className={`h-2 w-2 rounded-full ${metric.dot}`} /> {metric.label}
              </div>
              <div className="mt-1 flex items-baseline gap-2">
                <span className={`text-xl font-display font-bold tabular-nums ${metric.cls}`}>{metric.value}%</span>
                <span className={`text-[11px] font-bold tabular-nums ${deltaClass}`}>
                  {signedPoints(delta)}
                </span>
              </div>
            </div>
          );
        })}
      </div>

      <div className="h-[270px] w-full">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={periods} margin={{ top: 8, right: 10, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#E2E8F0" />
            <XAxis dataKey="period" tickFormatter={shortDate} fontSize={11} tick={{ fill: "#64748B" }} tickLine={false} axisLine={false} minTickGap={24} />
            <YAxis yAxisId="share" domain={[0, 100]} tickFormatter={(v) => `${v}%`} fontSize={11} tick={{ fill: "#64748B" }} tickLine={false} axisLine={false} width={42} />
            <YAxis yAxisId="volume" orientation="right" allowDecimals={false} fontSize={11} tick={{ fill: "#94A3B8" }} tickLine={false} axisLine={false} width={36} />
            <Tooltip content={<CitationTrendTooltip />} />
            <ChartLegend wrapperStyle={{ fontSize: 11, fontWeight: 600, paddingTop: 8 }} />
            <Bar yAxisId="volume" dataKey="total" name="Daily citations" fill="#CBD5E1" fillOpacity={0.55} radius={[4, 4, 0, 0]} maxBarSize={28} />
            <Line yAxisId="share" type="monotone" dataKey="abbvie_share_pct" name="AbbVie" stroke="#0D4F4F" strokeWidth={2.5} dot={{ r: 3 }} activeDot={{ r: 5 }} />
            <Line yAxisId="share" type="monotone" dataKey="independent_share_pct" name="Independent" stroke="#38BDF8" strokeWidth={2.5} dot={{ r: 3 }} activeDot={{ r: 5 }} />
            <Line yAxisId="share" type="monotone" dataKey="competitor_share_pct" name="Competitor" stroke="#F87171" strokeWidth={2.5} dot={{ r: 3 }} activeDot={{ r: 5 }} />
            <Line yAxisId="share" type="monotone" dataKey="unknown_share_pct" name="Unclassified" stroke="#94A3B8" strokeWidth={2} strokeDasharray="5 4" dot={{ r: 2 }} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <p className="text-[11px] font-medium text-ink-muted">
        {shortDate(first.period)} to {shortDate(last.period)} · latest day contains {last.total.toLocaleString()} classified citations
      </p>
    </div>
  );
}

function TrendPending({ trend }: { trend: CitationTrend }) {
  const count = trend.periods.length;
  const latest = trend.periods[count - 1];
  const remaining = Math.max(0, MIN_TREND_PERIODS - count);
  return (
    <div className="flex flex-col gap-3 rounded-2xl border border-dashed border-line bg-surface-0/60 px-5 py-4 sm:flex-row sm:items-center">
      <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-brand-surface text-brand-dark">
        <TrendingUp size={19} />
      </div>
      <div className="min-w-0 flex-1">
        <h3 className="text-sm font-bold text-ink">Citation momentum needs more history</h3>
        <p className="mt-0.5 text-xs leading-relaxed text-ink-light">
          {latest
            ? `Baseline set on ${shortDate(latest.period)} at ${latest.abbvie_share_pct}% AbbVie share from ${latest.total.toLocaleString()} citations. ${remaining} more classified day${remaining !== 1 ? "s" : ""} needed before showing a trend.`
            : "A trend appears after citations have been classified on at least three different days."}
        </p>
      </div>
      <span className="shrink-0 rounded-full bg-slate-100 px-3 py-1 text-[11px] font-bold tabular-nums text-ink-light">
        {count} / {MIN_TREND_PERIODS} daily snapshots
      </span>
    </div>
  );
}

export default function CitationInsights({ filters }: { filters: RecFilters }) {
  const [share, setShare] = useState<ShareOfCitation | null>(null);
  const [opps, setOpps] = useState<CitationOpportunity[]>([]);
  const [opportunityCount, setOpportunityCount] = useState(0);
  const [preferred, setPreferred] = useState<PreferredSourceGap[]>([]);
  const [preferredConfigured, setPreferredConfigured] = useState(0);
  const [fanouts, setFanouts] = useState<QueryFanout[]>([]);
  const [fanoutResponses, setFanoutResponses] = useState(0);
  const [trend, setTrend] = useState<CitationTrend | null>(null);
  const [withCites, setWithCites] = useState(0);
  const [loading, setLoading] = useState(false);

  const key = JSON.stringify(filters);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    Promise.all([
      api.shareOfCitation(filters),
      api.citationOpportunities(filters, 12),
      api.preferredSourceGaps(filters),
      api.queryFanouts(filters, 12),
      api.citationTrend(filters),
    ])
      .then(([s, o, p, f, t]) => {
        if (!alive) return;
        setShare(s);
        setOpps(o.items);
        setOpportunityCount(o.count);
        setWithCites(o.responses_with_citations);
        setPreferred(p.items);
        setPreferredConfigured(p.configured);
        setFanouts(f.items);
        setFanoutResponses(f.responses_with_queries);
        setTrend(t);
      })
      .catch(() => {
        if (!alive) return;
        setShare(null); setOpps([]); setOpportunityCount(0); setWithCites(0);
        setPreferred([]); setPreferredConfigured(0);
        setFanouts([]); setFanoutResponses(0); setTrend(null);
      })
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const totalCitations = share?.total_citations ?? 0;
  const hasCitations = totalCitations > 0;
  const hasTrend = (trend?.periods.length ?? 0) >= MIN_TREND_PERIODS;
  const hasFanouts = fanoutResponses > 0 && fanouts.length > 0;
  const hasPreferred = preferredConfigured > 0;
  const fanoutMax = fanouts.length ? fanouts[0].count : 0;
  const nothing = !hasCitations && !hasFanouts && !hasPreferred;

  return (
    <section className="space-y-4">
      <div>
        <h2 className="flex items-center gap-2 text-lg font-extrabold text-ink">
          <Link2 size={18} className="text-brand-light" />
          Citation Gap Analysis
          <InfoTooltip content="Where AI gets its answers, from the classified Source Authority citation graph (BR-005 / FR-706a): share of citations, the trusted domains where your brand is missing, preferred sources AI omits, the search terms it ran, and the trend over time." />
        </h2>
        <p className="text-sm text-ink-light font-medium mt-0.5">
          Where credible sources for your brand are missing in AI answers, and the places to earn a citation.
        </p>
      </div>

      {loading ? (
        <div className="flex justify-center py-10">
          <Spinner size={24} />
        </div>
      ) : nothing ? (
        <Card>
          <EmptyState
            icon={<Link2 size={32} />}
            message={
              "No grounded citations or search queries for the current filters yet. This view uses the " +
              "real sources and queries search-enabled models (e.g. Gemini, GPT-4o) return, so run those " +
              "targets to populate it."
            }
          />
        </Card>
      ) : (
        <div className="space-y-4">
          {hasCitations && share && (
            <Card>
              <div className="mb-4 flex flex-wrap items-center gap-2">
                <Award size={16} className="text-brand-light" />
                <h3 className="text-sm font-bold text-ink">Citation position</h3>
                <InfoTooltip content="The complete control split for every classified citation: AbbVie, independent publishers, competitors, and unclassified sources. Answer counts show distinct AI responses touched by each source type." />
                <span className="ml-auto text-[11px] font-medium text-ink-muted">
                  {totalCitations.toLocaleString()} citations · {(share.response_count ?? withCites).toLocaleString()} answers
                </span>
              </div>
              <ShareVoice share={share} topOpportunity={opps[0]} />
            </Card>
          )}

          {hasCitations && (
            <div id="citation-interventions" className="scroll-mt-6">
              <Card>
                <div className="mb-3 flex flex-wrap items-start gap-2">
                  <Globe size={16} className="mt-0.5 text-brand-light" />
                  <div>
                    <div className="flex items-center gap-2">
                      <h3 className="text-sm font-bold text-ink">Where to intervene</h3>
                      <InfoTooltip content="Non-AbbVie domains ranked by distinct answer reach plus extra weight when the cited answer positions the brand weakly. Preferred sources rank first." />
                    </div>
                    <p className="mt-0.5 text-[11px] font-medium text-ink-muted">Prioritized sources, their AI reach, brand exposure, and the recommended next move.</p>
                  </div>
                  <span className="ml-auto text-[11px] font-medium text-ink-muted">
                    {withCites} affected answers · top {opps.length} of {opportunityCount} domains
                  </span>
                </div>
                {opps.length === 0 ? (
                  <p className="py-4 text-sm text-ink-muted">No independent or unclassified citation opportunities for these filters.</p>
                ) : (
                  <div className="max-h-[470px] overflow-auto">
                    <table className="w-full min-w-[900px] border-collapse text-left text-xs">
                      <thead className="sticky top-0 z-10 bg-canvas-card">
                        <tr className="border-y border-line text-[10px] font-bold uppercase tracking-wide text-ink-muted">
                          <th className="py-2.5 pr-5">Priority source</th>
                          <th className="py-2.5 pr-5">AI reach</th>
                          <th className="py-2.5 pr-5">Brand exposure</th>
                          <th className="py-2.5 pr-5">Recommended action</th>
                          <th className="py-2.5 text-right">Priority</th>
                        </tr>
                      </thead>
                      <tbody>
                        {opps.map((o, i) => <OpportunityRow key={o.domain} o={o} rank={i + 1} />)}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>
            </div>
          )}

          {(hasPreferred || hasFanouts) && (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              {hasPreferred && (
                <Card>
                  <div className="flex items-center gap-2 mb-1">
                    <ListChecks size={16} className="text-brand-light" />
                    <h3 className="text-sm font-bold text-ink">Preferred sources AI omits</h3>
                    <InfoTooltip content="Medical-Affairs-designated preferred domains and how often AI leaves them out of grounded answers. The most-omitted rank first: the highest-priority citation gaps." />
                    <span className="ml-auto text-[11px] font-medium text-ink-muted">{preferredConfigured} configured</span>
                  </div>
                  {preferred.length === 0 ? (
                    <p className="text-sm text-ink-muted py-4">No preferred sources for these filters.</p>
                  ) : (
                    <div className="max-h-[360px] overflow-y-auto pr-1">
                      {preferred.map((g) => (
                        <PreferredGapRow key={g.authority_domain} g={g} />
                      ))}
                    </div>
                  )}
                </Card>
              )}

              {hasFanouts && (
                <Card>
                  <div className="flex items-center gap-2 mb-1">
                    <Search size={16} className="text-brand-light" />
                    <h3 className="text-sm font-bold text-ink">Search terms AI ran</h3>
                    <InfoTooltip content="The actual search queries grounded models issued while answering (query fanouts). Target these phrasings in your content so your assets get retrieved and cited." />
                    <span className="ml-auto text-[11px] font-medium text-ink-muted">{fanoutResponses} answers</span>
                  </div>
                  <div className="max-h-[360px] overflow-y-auto pr-1">
                    {fanouts.map((q) => (
                      <FanoutRow key={q.query} q={q} max={fanoutMax} />
                    ))}
                  </div>
                </Card>
              )}
            </div>
          )}

          {hasCitations && trend && (
            hasTrend ? (
              <Card>
                <div className="mb-4 flex flex-wrap items-center gap-2">
                  <TrendingUp size={16} className="text-brand-light" />
                  <h3 className="text-sm font-bold text-ink">Citation momentum</h3>
                  <InfoTooltip content="Daily AbbVie, independent, competitor, and unclassified citation share, with citation volume and percentage-point movement from the first day in view." />
                  <span className="ml-auto text-[11px] font-medium text-ink-muted">{trend.periods.length} daily snapshots</span>
                </div>
                <TrendBars trend={trend} />
              </Card>
            ) : (
              <TrendPending trend={trend} />
            )
          )}
        </div>
      )}
    </section>
  );
}
