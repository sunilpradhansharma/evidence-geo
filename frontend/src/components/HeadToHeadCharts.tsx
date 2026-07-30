import { Fragment, type ReactNode } from "react";
import { BarChart3, CalendarRange, Grid3x3, Scale, Swords, Users } from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  LabelList,
  Line,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";
import type { HeadToHeadBoard, HeadToHeadPair, HeadToHeadTimeline } from "../api/client";
import { AnimatedCard, Card, InfoTooltip } from "./ui";

/* Analyst views over the head-to-head board (FR-616).
   Every chart reads the SAME board object the matchup list reads, so the pictures and the
   rows can never report different numbers, and one filter change moves both at once. No
   chart makes its own request. Where the data cannot honestly carry a view — one AI
   platform, two run days, a tone gap the scorer never recorded — the chart says so rather
   than drawing a shape that implies evidence we do not have. */

/** Marketer-facing platform names, mirroring ``labels.AI_PLATFORM_LABELS`` on the server. */
export const MODEL_LABELS: Record<string, string> = {
  claude: "Claude",
  evidencemd: "EvidenceMD",
  gemini: "Gemini",
  "gpt-4o": "GPT-4o",
  llama: "Llama",
  "nova-pro": "Nova Pro",
};

/**
 * Chart fills for the three verdicts. Hex rather than Tailwind classes because an SVG
 * `fill` is not a class name — the semantics still match `VERDICT_META` on the page.
 */
const FILL: Record<string, string> = {
  LOSING: "#DC2626",
  EVEN: "#FCD34D",
  WINNING: "#10B981",
  held: "#CBD5E1",
  volume: "#CBD5E1",
};

const AXIS = {
  fontSize: 11,
  tick: { fill: "#64748B" },
  tickLine: false,
  axisLine: false,
} as const;

const GRID = { strokeDasharray: "3 3", stroke: "#E2E8F0" } as const;

const CURSOR = { fill: "#0D4F4F", fillOpacity: 0.04 };

/** How many rows the ranked charts show before they stop. */
const TOP_N = 8;

function platformLabel(name: string): string {
  return MODEL_LABELS[name] ?? name;
}

function matchup(pair: HeadToHeadPair): string {
  return `${pair.brand} vs ${pair.competitor}`;
}

function shortDate(value: string): string {
  const date = new Date(`${value}T00:00:00`);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/* ------------------------------------------------------------------ */
/*  Shared shells                                                      */
/* ------------------------------------------------------------------ */
function ChartFrame({
  icon, title, blurb, tooltip, delay = 0, className = "", children,
}: {
  icon: ReactNode;
  title: string;
  blurb: string;
  tooltip?: string;
  delay?: number;
  className?: string;
  children: ReactNode;
}) {
  return (
    <AnimatedCard delay={delay} className={className}>
      <Card className="h-full">
        <div className="mb-3 flex items-start gap-2">
          <span className="mt-0.5 shrink-0 text-brand">{icon}</span>
          <div className="min-w-0">
            <h3 className="flex items-center gap-1 text-sm font-bold text-ink">
              {title}
              {tooltip && <InfoTooltip content={tooltip} />}
            </h3>
            <p className="mt-0.5 text-xs font-medium text-ink-light">{blurb}</p>
          </div>
        </div>
        {children}
      </Card>
    </AnimatedCard>
  );
}

/**
 * What a chart shows when the corpus cannot support it yet. States what exists AND what is
 * missing: a blank space reads as a bug, and a line over two points reads as a finding.
 */
function NotEnough({ message, badge }: { message: string; badge?: string }) {
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-dashed border-line bg-surface-0/60 px-4 py-5 sm:flex-row sm:items-center">
      <p className="min-w-0 flex-1 text-xs font-medium leading-relaxed text-ink-light">
        {message}
      </p>
      {badge && (
        <span className="shrink-0 self-start rounded-full bg-slate-100 px-3 py-1 text-[11px] font-bold tabular-nums text-ink-light sm:self-auto">
          {badge}
        </span>
      )}
    </div>
  );
}

function TipShell({ title, sub, children }: { title: string; sub?: string; children: ReactNode }) {
  return (
    <div className="max-w-[280px] rounded-xl border border-line bg-canvas-card p-3 text-xs shadow-lg">
      <p className="font-bold text-ink">{title}</p>
      {sub && <p className="mt-0.5 text-[11px] font-medium text-ink-muted">{sub}</p>}
      <div className="mt-1.5">{children}</div>
    </div>
  );
}

function TipRow({ colour, label, value }: { colour?: string; label: string; value: string }) {
  return (
    <div className="flex items-center gap-2 py-0.5">
      {colour && (
        <span className="h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: colour }} />
      )}
      <span className="flex-1 font-medium text-ink-light">{label}</span>
      <span className="font-bold tabular-nums text-ink">{value}</span>
    </div>
  );
}

function Swatches({ items }: { items: { label: string; colour: string; dashed?: boolean }[] }) {
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
      {items.map((item) => (
        <span key={item.label} className="flex items-center gap-1.5 text-[11px] font-semibold text-ink-light">
          <span
            className={`h-2.5 w-2.5 rounded-sm ${item.dashed ? "border border-dashed border-line" : ""}`}
            style={item.dashed ? undefined : { backgroundColor: item.colour }}
          />
          {item.label}
        </span>
      ))}
    </div>
  );
}

const VERDICT_SWATCHES = [
  { label: "We lose", colour: FILL.LOSING },
  { label: "Too close to call", colour: FILL.EVEN },
  { label: "We win", colour: FILL.WINNING },
];

/* ------------------------------------------------------------------ */
/*  1. Where we are most exposed                                       */
/* ------------------------------------------------------------------ */
function ExposureTooltip({ active, payload }: any) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload;
  return (
    <TipShell
      title={row.label}
      sub={`${row.disease ?? "No indication recorded"} · ${plural(row.platforms, "platform")}`}
    >
      <TipRow colour={FILL.LOSING} label="We lose" value={String(row.LOSING)} />
      <TipRow colour={FILL.EVEN} label="Too close to call" value={String(row.EVEN)} />
      <TipRow colour={FILL.WINNING} label="We win" value={String(row.WINNING)} />
      <p className="mt-1.5 border-t border-line pt-1.5 text-[11px] font-bold text-ink">
        {row.LOSING} of {row.answers} answers lost ({row.lossPct}%)
      </p>
    </TipShell>
  );
}

function ExposureRanking({ pairs }: { pairs: HeadToHeadPair[] }) {
  // Keyed on `key`, never on the label: two comparisons between the same two drugs in
  // different indications share a label, and a duplicate category silently merges the rows.
  const rows = pairs.slice(0, TOP_N).map((p) => ({
    key: p.key,
    label: matchup(p),
    disease: p.disease,
    platforms: p.models.length,
    answers: p.answers,
    lossPct: Math.round(p.loss_rate * 100),
    LOSING: p.verdict_counts.LOSING ?? 0,
    EVEN: p.verdict_counts.EVEN ?? 0,
    WINNING: p.verdict_counts.WINNING ?? 0,
  }));
  const labels = Object.fromEntries(rows.map((r) => [r.key, r.label]));

  return (
    <>
      <div className="w-full" style={{ height: rows.length * 34 + 34 }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={rows}
            layout="vertical"
            margin={{ top: 4, right: 12, bottom: 0, left: 0 }}
            barCategoryGap="24%"
          >
            <CartesianGrid {...GRID} horizontal={false} />
            <XAxis type="number" allowDecimals={false} {...AXIS} />
            <YAxis
              type="category"
              dataKey="key"
              tickFormatter={(key: string) => labels[key] ?? key}
              width={132}
              interval={0}
              {...AXIS}
            />
            <Tooltip content={<ExposureTooltip />} cursor={CURSOR} />
            <Bar dataKey="LOSING" stackId="a" fill={FILL.LOSING} radius={[4, 0, 0, 4]} maxBarSize={22} />
            <Bar dataKey="EVEN" stackId="a" fill={FILL.EVEN} maxBarSize={22} />
            <Bar dataKey="WINNING" stackId="a" fill={FILL.WINNING} radius={[0, 4, 4, 0]} maxBarSize={22} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <Swatches items={VERDICT_SWATCHES} />
    </>
  );
}

/* ------------------------------------------------------------------ */
/*  2. Which AI platform is costing us                                 */
/* ------------------------------------------------------------------ */
// Banded rather than a smooth ramp: a reader has to be able to name the band a cell sits in,
// which a continuous gradient makes impossible at a glance.
const HEAT_STEPS = [
  { max: 0, fill: "#D1FAE5", text: "#065F46", label: "None lost" },
  { max: 0.25, fill: "#FEF3C7", text: "#92400E", label: "Up to 25%" },
  { max: 0.5, fill: "#FDBA74", text: "#7C2D12", label: "26-50%" },
  { max: 0.75, fill: "#F87171", text: "#7F1D1D", label: "51-75%" },
  { max: 1.01, fill: "#B91C1C", text: "#FFFFFF", label: "Over 75%" },
];

function heat(rate: number) {
  return HEAT_STEPS.find((step) => rate <= step.max) ?? HEAT_STEPS[HEAT_STEPS.length - 1];
}

function PlatformHeatmap({ pairs }: { pairs: HeadToHeadPair[] }) {
  const rows = pairs.slice(0, TOP_N);
  const lostByPlatform = new Map<string, number>();
  rows.forEach((pair) =>
    pair.by_model.forEach((m) =>
      lostByPlatform.set(m.llm_name, (lostByPlatform.get(m.llm_name) ?? 0) + m.losing),
    ),
  );
  const platforms = [...lostByPlatform.keys()].sort(
    (a, b) => (lostByPlatform.get(b) ?? 0) - (lostByPlatform.get(a) ?? 0) || a.localeCompare(b),
  );

  if (platforms.length < 2 || rows.length < 2) {
    return (
      <NotEnough
        message={
          `A heatmap needs at least two comparisons across at least two AI platforms to say ` +
          `anything a list cannot. This board holds ${plural(rows.length, "comparison")} across ` +
          `${plural(platforms.length, "platform")} — the per-platform split is on each row below.`
        }
        badge={`${rows.length} x ${platforms.length}`}
      />
    );
  }

  const template = `minmax(112px, 1.4fr) repeat(${platforms.length}, minmax(46px, 1fr))`;
  return (
    <>
      <div className="overflow-x-auto">
        <div className="min-w-[460px]">
          <div className="grid gap-1" style={{ gridTemplateColumns: template }}>
            <span />
            {platforms.map((name) => (
              <span
                key={name}
                title={platformLabel(name)}
                className="truncate pb-1 text-center text-[10px] font-bold uppercase tracking-wide text-ink-light"
              >
                {platformLabel(name)}
              </span>
            ))}
            {rows.map((pair) => {
              const seen = new Map(pair.by_model.map((m) => [m.llm_name, m]));
              return (
                <Fragment key={pair.key}>
                  <span
                    title={pair.label}
                    className="flex items-center truncate pr-2 text-[11px] font-semibold text-ink"
                  >
                    {matchup(pair)}
                  </span>
                  {platforms.map((name) => {
                    const cell = seen.get(name);
                    // An empty cell is "never asked here", NOT a zero. Colouring it green
                    // would turn a coverage gap into a win we never earned.
                    if (!cell) {
                      return (
                        <span
                          key={name}
                          title={`${platformLabel(name)} was never asked this comparison`}
                          className="rounded-md border border-dashed border-line bg-surface-0/60 py-2 text-center text-[10px] font-bold text-ink-muted"
                        >
                          &ndash;
                        </span>
                      );
                    }
                    const tone = heat(cell.loss_rate);
                    return (
                      <span
                        key={name}
                        title={
                          `${platformLabel(name)} lost ${cell.losing} of ${cell.answers} answers on ` +
                          `${matchup(pair)} (${Math.round(cell.loss_rate * 100)}%)`
                        }
                        className="rounded-md py-2 text-center text-[11px] font-bold tabular-nums"
                        style={{ backgroundColor: tone.fill, color: tone.text }}
                      >
                        {cell.losing}/{cell.answers}
                      </span>
                    );
                  })}
                </Fragment>
              );
            })}
          </div>
        </div>
      </div>
      <Swatches
        items={[
          ...HEAT_STEPS.map((step) => ({ label: step.label, colour: step.fill })),
          { label: "Never asked here", colour: "transparent", dashed: true },
        ]}
      />
    </>
  );
}

/* ------------------------------------------------------------------ */
/*  3. Loss rate over time                                             */
/* ------------------------------------------------------------------ */
function TimelineTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload;
  return (
    <TipShell title={shortDate(String(label))} sub={`${plural(row.runs, "run")} on this date`}>
      <TipRow colour={FILL.LOSING} label="Answers we lose" value={`${row.lossPct}%`} />
      <TipRow colour={FILL.volume} label="Comparison answers" value={String(row.answers)} />
      <p className="mt-1.5 border-t border-line pt-1.5 text-[11px] font-medium text-ink-muted">
        {row.losing} lost · {row.even} too close · {row.winning} won
      </p>
    </TipShell>
  );
}

function LossOverTime({ timeline }: { timeline?: HeadToHeadTimeline }) {
  const periods = (timeline?.periods ?? []).map((p) => ({
    ...p,
    lossPct: Math.round(p.loss_rate * 100),
  }));
  const minPeriods = timeline?.min_periods ?? 3;

  if (!timeline?.available) {
    const latest = periods[periods.length - 1];
    const remaining = Math.max(0, minPeriods - periods.length);
    return (
      <NotEnough
        message={
          latest
            ? `Baseline set on ${shortDate(latest.period)}: ${latest.lossPct}% of ` +
              `${plural(latest.answers, "comparison answer")} went against us. ` +
              `${plural(remaining, "more run day")} needed before a direction can be drawn — ` +
              `two points joined up read as a trend when they are one observation each.`
            : `A line appears once these comparisons have been run on at least ` +
              `${minPeriods} different days. Nothing on the board so far carries a usable date.`
        }
        badge={`${periods.length} / ${minPeriods} run days`}
      />
    );
  }

  const first = periods[0];
  const last = periods[periods.length - 1];
  const delta = last.lossPct - first.lossPct;
  const graded = periods.reduce((n, p) => n + p.answers, 0);
  const metrics = [
    { label: "Latest loss rate", value: `${last.lossPct}%`, cls: "text-red-600" },
    {
      // Losing MORE of the same comparisons is the bad direction, so a rising number is red.
      label: "Since first run day",
      value: `${delta > 0 ? "+" : ""}${delta} pts`,
      cls: delta > 0 ? "text-red-600" : delta < 0 ? "text-teal-700" : "text-ink-muted",
    },
    { label: "Run days", value: String(periods.length), cls: "text-ink" },
    { label: "Answers graded", value: String(graded), cls: "text-ink" },
  ];

  return (
    <>
      <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
        {metrics.map((metric) => (
          <div key={metric.label} className="rounded-xl border border-line bg-surface-0/60 px-3 py-2.5">
            <p className="text-[11px] font-bold uppercase tracking-wide text-ink-muted">
              {metric.label}
            </p>
            <p className={`mt-1 font-display text-xl font-bold tabular-nums ${metric.cls}`}>
              {metric.value}
            </p>
          </div>
        ))}
      </div>
      <div className="h-[250px] w-full">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={periods} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid {...GRID} vertical={false} />
            <XAxis dataKey="period" tickFormatter={shortDate} minTickGap={20} {...AXIS} />
            <YAxis
              yAxisId="rate"
              domain={[0, 100]}
              tickFormatter={(v: number) => `${v}%`}
              width={42}
              {...AXIS}
            />
            <YAxis
              yAxisId="volume"
              orientation="right"
              allowDecimals={false}
              width={36}
              {...AXIS}
              tick={{ fill: "#94A3B8" }}
            />
            <Tooltip content={<TimelineTooltip />} cursor={CURSOR} />
            <Bar
              yAxisId="volume"
              dataKey="answers"
              fill={FILL.volume}
              fillOpacity={0.55}
              radius={[4, 4, 0, 0]}
              maxBarSize={30}
            />
            <Line
              yAxisId="rate"
              type="monotone"
              dataKey="lossPct"
              stroke={FILL.LOSING}
              strokeWidth={2.5}
              dot={{ r: 3.5 }}
              activeDot={{ r: 5.5 }}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <Swatches
        items={[
          { label: "Share of answers we lose", colour: FILL.LOSING },
          { label: "Comparison answers that day", colour: FILL.volume },
        ]}
      />
      <p className="mt-2 text-[11px] font-medium leading-relaxed text-ink-muted">
        {shortDate(first.period)} to {shortDate(last.period)} across {plural(timeline.runs, "run")}.{" "}
        {timeline.note}
        {timeline.undated > 0 &&
          ` ${plural(timeline.undated, "answer")} carried no usable date and sit on no point.`}
      </p>
    </>
  );
}

/* ------------------------------------------------------------------ */
/*  4. Tone gap against exposure                                       */
/* ------------------------------------------------------------------ */
function ToneTooltip({ active, payload }: any) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload;
  return (
    <TipShell title={row.name} sub={row.disease ?? "No indication recorded"}>
      <TipRow
        colour={FILL[row.verdict] ?? FILL.EVEN}
        label="Answers we lose"
        value={`${row.losing} of ${row.z}`}
      />
      <TipRow label="Share lost" value={`${row.x}%`} />
      <TipRow label="Tone gap" value={`${row.y > 0 ? "+" : ""}${row.y}`} />
      <p className="mt-1.5 border-t border-line pt-1.5 text-[11px] font-medium text-ink-muted">
        {row.y < 0
          ? `AI speaks more warmly about ${row.rival}.`
          : `AI speaks more warmly about ${row.brand}.`}
      </p>
    </TipShell>
  );
}

function ToneQuadrant({ pairs }: { pairs: HeadToHeadPair[] }) {
  // A null gap means one side was never scored. Plotting it at zero would invent a neutral
  // tone nobody measured, so those comparisons are dropped and the count is reported.
  const plotted = pairs.filter((p) => p.sentiment_gap !== null);
  const dropped = pairs.length - plotted.length;

  if (plotted.length < 2) {
    return (
      <NotEnough
        message={
          `A tone gap only exists where the scorer recorded a sentiment for BOTH sides, and ` +
          `that holds for ${plural(plotted.length, "comparison")} here. The other ${dropped} ` +
          `named a rival the scorer never scored, and placing them at zero would invent a ` +
          `neutral tone nobody measured.`
        }
        badge={`${plotted.length} / 2 plottable`}
      />
    );
  }

  const points = plotted.map((p) => ({
    x: Math.round(p.loss_rate * 100),
    y: p.sentiment_gap as number,
    z: p.answers,
    name: matchup(p),
    brand: p.brand,
    rival: p.competitor,
    disease: p.disease,
    verdict: p.verdict,
    losing: p.losing_answers,
  }));
  // Symmetric, so the zero line sits in the middle and "warmer about them" is always down.
  const limit = Math.ceil(Math.max(0.5, ...points.map((pt) => Math.abs(pt.y))) * 10) / 10;

  return (
    <>
      <div className="h-[280px] w-full">
        <ResponsiveContainer width="100%" height="100%">
          <ScatterChart margin={{ top: 10, right: 16, bottom: 4, left: 0 }}>
            <CartesianGrid {...GRID} />
            <XAxis
              type="number"
              dataKey="x"
              domain={[0, 100]}
              tickFormatter={(v: number) => `${v}%`}
              {...AXIS}
            />
            <YAxis
              type="number"
              dataKey="y"
              domain={[-limit, limit]}
              tickFormatter={(v: number) => v.toFixed(1)}
              width={44}
              {...AXIS}
            />
            <ZAxis type="number" dataKey="z" range={[80, 520]} />
            {/* Most answers going against us AND a warmer tone for the rival: act here first. */}
            <ReferenceArea x1={50} x2={100} y1={-limit} y2={0} fill={FILL.LOSING} fillOpacity={0.06} />
            <ReferenceLine y={0} stroke="#94A3B8" strokeDasharray="4 4" />
            <ReferenceLine x={50} stroke="#94A3B8" strokeDasharray="4 4" />
            <Tooltip content={<ToneTooltip />} cursor={{ strokeDasharray: "3 3" }} />
            <Scatter data={points}>
              {points.map((pt, i) => (
                <Cell
                  key={`${pt.name}-${i}`}
                  fill={FILL[pt.verdict] ?? FILL.EVEN}
                  fillOpacity={0.72}
                  stroke="#FFFFFF"
                  strokeWidth={1.5}
                />
              ))}
            </Scatter>
          </ScatterChart>
        </ResponsiveContainer>
      </div>
      <Swatches items={VERDICT_SWATCHES} />
      <p className="mt-2 text-[11px] font-medium leading-relaxed text-ink-muted">
        Left to right: the share of answers going against us. Up and down: how much warmer AI
        is about our brand than the rival, so anything below the dashed line is a tone
        deficit. Bubble size is the number of answers behind the comparison, and the tinted
        corner is the one to act on first.
        {dropped > 0 &&
          ` ${plural(dropped, "comparison")} not shown: the scorer recorded no sentiment for the rival.`}
      </p>
    </>
  );
}

/* ------------------------------------------------------------------ */
/*  5. Which rival is taking the most ground                           */
/* ------------------------------------------------------------------ */
function RivalTooltip({ active, payload }: any) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload;
  return (
    <TipShell title={row.competitor} sub={plural(row.matchups, "monitored comparison")}>
      <TipRow colour={FILL.LOSING} label="Answers we lose" value={String(row.losing)} />
      <TipRow colour={FILL.held} label="Answers we hold" value={String(row.held)} />
      <p className="mt-1.5 border-t border-line pt-1.5 text-[11px] font-bold text-ink">
        {row.lossPct}% of the {row.answers} answers naming them
      </p>
    </TipShell>
  );
}

function RivalThreat({ pairs }: { pairs: HeadToHeadPair[] }) {
  const byRival = new Map<
    string,
    { competitor: string; answers: number; losing: number; matchups: number }
  >();
  pairs.forEach((pair) => {
    const row = byRival.get(pair.competitor)
      ?? { competitor: pair.competitor, answers: 0, losing: 0, matchups: 0 };
    row.answers += pair.answers;
    row.losing += pair.losing_answers;
    row.matchups += 1;
    byRival.set(pair.competitor, row);
  });

  const rows = [...byRival.values()]
    .sort((a, b) =>
      b.losing - a.losing || b.answers - a.answers || a.competitor.localeCompare(b.competitor))
    .slice(0, TOP_N)
    .map((r) => ({
      ...r,
      held: r.answers - r.losing,
      lossPct: r.answers ? Math.round((100 * r.losing) / r.answers) : 0,
    }));

  if (rows.length < 2) {
    return (
      <NotEnough
        message={
          `Ranking rivals needs at least two of them on the board, and this selection holds ` +
          `${plural(rows.length, "rival")}. Widen the Rival filter, or run more comparison ` +
          `questions, to see who is taking the most ground.`
        }
        badge={`${rows.length} / 2 rivals`}
      />
    );
  }

  return (
    <>
      <div className="w-full" style={{ height: rows.length * 34 + 34 }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={rows}
            layout="vertical"
            margin={{ top: 4, right: 12, bottom: 0, left: 0 }}
            barCategoryGap="24%"
          >
            <CartesianGrid {...GRID} horizontal={false} />
            <XAxis type="number" allowDecimals={false} {...AXIS} />
            <YAxis type="category" dataKey="competitor" width={104} interval={0} {...AXIS} />
            <Tooltip content={<RivalTooltip />} cursor={CURSOR} />
            <Bar dataKey="losing" stackId="a" fill={FILL.LOSING} radius={[4, 0, 0, 4]} maxBarSize={22}>
              <LabelList
                dataKey="lossPct"
                position="insideLeft"
                formatter={(v: any) => (Number(v) >= 20 ? `${v}%` : "")}
                fontSize={10}
                fontWeight={700}
                fill="#FFFFFF"
              />
            </Bar>
            <Bar dataKey="held" stackId="a" fill={FILL.held} radius={[0, 4, 4, 0]} maxBarSize={22} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <Swatches
        items={[
          { label: "Answers we lose", colour: FILL.LOSING },
          { label: "Answers we hold", colour: FILL.held },
        ]}
      />
    </>
  );
}

/* ------------------------------------------------------------------ */
/*  6. Which audience gets the worst answer                            */
/* ------------------------------------------------------------------ */
function AudienceTooltip({ active, payload }: any) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload;
  return (
    <TipShell title={row.persona} sub={plural(row.matchups, "comparison")}>
      <TipRow
        colour={FILL.LOSING}
        label="Answers we lose"
        value={`${row.losing} of ${row.answers}`}
      />
      <TipRow label="Share lost" value={`${row.lossPct}%`} />
    </TipShell>
  );
}

function AudienceSplit({ pairs }: { pairs: HeadToHeadPair[] }) {
  const byPersona = new Map<
    string,
    { persona: string; answers: number; losing: number; matchups: number }
  >();
  pairs.forEach((pair) =>
    pair.by_persona.forEach((slice) => {
      const row = byPersona.get(slice.persona)
        ?? { persona: slice.persona, answers: 0, losing: 0, matchups: 0 };
      row.answers += slice.answers;
      row.losing += slice.losing;
      row.matchups += 1;
      byPersona.set(slice.persona, row);
    }),
  );

  const rows = [...byPersona.values()]
    .sort((a, b) => b.losing - a.losing || a.persona.localeCompare(b.persona))
    .map((r) => ({
      ...r,
      lossPct: r.answers ? Math.round((100 * r.losing) / r.answers) : 0,
    }));

  if (rows.length < 2) {
    return (
      <NotEnough
        message={
          `Comparing audiences needs at least two of them asking these questions, and only ` +
          `${rows.length === 1 ? rows[0].persona : "none"} appears on this board. Approve the ` +
          `same comparison questions for another persona to see whether a patient is told ` +
          `something a prescriber is not.`
        }
        badge={`${rows.length} / 2 audiences`}
      />
    );
  }

  return (
    <>
      <div className="h-[220px] w-full">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={rows} margin={{ top: 16, right: 8, bottom: 0, left: 0 }} barCategoryGap="32%">
            <CartesianGrid {...GRID} vertical={false} />
            <XAxis dataKey="persona" {...AXIS} />
            <YAxis
              domain={[0, 100]}
              tickFormatter={(v: number) => `${v}%`}
              width={42}
              {...AXIS}
            />
            <Tooltip content={<AudienceTooltip />} cursor={CURSOR} />
            <Bar dataKey="lossPct" radius={[6, 6, 0, 0]} maxBarSize={68}>
              {rows.map((row) => (
                <Cell
                  key={row.persona}
                  fill={row.lossPct >= 50 ? FILL.LOSING : row.lossPct > 0 ? FILL.EVEN : FILL.WINNING}
                />
              ))}
              <LabelList
                dataKey="lossPct"
                position="top"
                formatter={(v: any) => `${v}%`}
                fontSize={11}
                fontWeight={700}
                fill="#334155"
              />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <p className="mt-2 text-[11px] font-medium leading-relaxed text-ink-muted">
        {rows
          .map((r) => `${r.persona}: ${r.losing} of ${r.answers} lost`)
          .join(" · ")}
        . A platform losing the comparison is a content-placement problem; an audience losing
        it is a messaging one.
      </p>
    </>
  );
}

/* ------------------------------------------------------------------ */
/*  Composed board                                                     */
/* ------------------------------------------------------------------ */
/**
 * Six analyst views of the board above the matchup list. Renders nothing at all when there
 * is no comparison to chart — the page's own empty state already explains why.
 */
export default function HeadToHeadCharts({ board }: { board: HeadToHeadBoard }) {
  const pairs = board.pairs;
  if (!pairs.length) return null;
  const capped = pairs.length > TOP_N ? ` Showing the ${TOP_N} most exposed of ${board.pairs_total}.` : "";

  return (
    <div className="mb-5 grid gap-4 lg:grid-cols-2">
      <ChartFrame
        icon={<BarChart3 size={18} />}
        title="Where we are most exposed"
        blurb={`Every answer in a comparison, split by who AI sided with.${capped}`}
        tooltip={board.verdict_rule}
        delay={0}
      >
        <ExposureRanking pairs={pairs} />
      </ChartFrame>

      <ChartFrame
        icon={<Grid3x3 size={18} />}
        title="Which AI platform is costing us"
        blurb="Answers lost out of answers asked, per comparison and per platform."
        tooltip={
          "A dashed cell means that platform was never asked that comparison. It is a gap " +
          "in coverage, not a comparison we won, and is deliberately not coloured as one."
        }
        delay={0.05}
      >
        <PlatformHeatmap pairs={pairs} />
      </ChartFrame>

      <ChartFrame
        icon={<CalendarRange size={18} />}
        title="Is the comparison front moving?"
        blurb="Share of comparison answers going against us, per day the questions were run."
        tooltip={
          board.timeline?.note ??
          "Aggregated across the whole board: per-comparison history is usually one or two " +
          "runs deep, which is too thin to draw a line through on its own."
        }
        delay={0.1}
        className="lg:col-span-2"
      >
        <LossOverTime timeline={board.timeline} />
      </ChartFrame>

      <ChartFrame
        icon={<Swords size={18} />}
        title="Which rival is taking the most ground"
        blurb="Answers lost to each rival, across every comparison they appear in."
        tooltip={
          "Summed over comparisons, so a rival we meet in three indications is counted in " +
          "all three. The bar length is the total answers naming them, not a percentage."
        }
        delay={0.15}
      >
        <RivalThreat pairs={pairs} />
      </ChartFrame>

      <ChartFrame
        icon={<Users size={18} />}
        title="Which audience gets the worst answer"
        blurb="Share of comparison answers going against us, per person asking."
        tooltip={
          "The same graded answers as the platform view, cut by who asked instead of by " +
          "which model answered — so the two totals always agree."
        }
        delay={0.2}
      >
        <AudienceSplit pairs={pairs} />
      </ChartFrame>

      <ChartFrame
        icon={<Scale size={18} />}
        title="Tone gap against exposure"
        blurb="How often we lose a comparison, against how warmly AI speaks about each side."
        tooltip={
          "The tone gap is our sentiment minus the rival's, both recorded by the scorer. " +
          "A comparison where either side was never scored is left off rather than plotted " +
          "at zero, and the count of those is stated under the chart."
        }
        delay={0.25}
        className="lg:col-span-2"
      >
        <ToneQuadrant pairs={pairs} />
      </ChartFrame>
    </div>
  );
}
