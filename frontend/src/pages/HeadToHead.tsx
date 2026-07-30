import { useCallback, useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import {
  AlertTriangle,
  ArrowRight,
  ChevronDown,
  Info,
  Quote,
  Sparkles,
  Swords,
  TrendingDown,
  TrendingUp,
  X,
} from "lucide-react";
import {
  api,
  H2H_FILTERED_OUT,
  HeadToHeadBoard,
  HeadToHeadClaim,
  HeadToHeadDetail,
  HeadToHeadFilterOptions,
  HeadToHeadPair,
} from "../api/client";
import {
  AnimatedCard,
  Card,
  ComingSoonBanner,
  EmptyState,
  InfoTooltip,
  MultiSelect,
  PageHeader,
  Spinner,
  Stat,
} from "../components/ui";
// The charts own the platform-label map: both they and the picker below need it, and one
// definition is what keeps a heatmap column headed "GPT-4o" from a filter chip reading "gpt-4o".
import HeadToHeadCharts, { MODEL_LABELS } from "../components/HeadToHeadCharts";

/** Worst first: the reader opens this page to find what is going against us. */
const VERDICT_ORDER = ["LOSING", "EVEN", "WINNING"];

const NO_FACETS: HeadToHeadFilterOptions = {
  areas: [], diseases: [], brands: [], competitors: [], personas: [], models: [],
};

/** Plain-language verdict labels — the number is never shown without one (FR-616). */
const VERDICT_META: Record<string, { label: string; cls: string; dot: string }> = {
  WINNING: { label: "We win", cls: "bg-emerald-50 text-emerald-800 border-emerald-200", dot: "bg-emerald-500" },
  EVEN: { label: "Too close to call", cls: "bg-amber-50 text-amber-800 border-amber-200", dot: "bg-amber-400" },
  LOSING: { label: "We lose", cls: "bg-red-50 text-red-800 border-red-200", dot: "bg-red-500" },
};

/** How much to trust the pairing itself. Shown, never hidden. */
const SOURCE_META: Record<string, { label: string; cls: string }> = {
  stored: { label: "Tagged", cls: "bg-brand-surface text-brand-dark" },
  derived: { label: "Matched", cls: "bg-slate-100 text-ink-light" },
  text_only: { label: "Indicative", cls: "bg-amber-50 text-amber-700" },
};

function VerdictPill({ verdict, size = "sm" }: { verdict: string; size?: "sm" | "lg" }) {
  const meta = VERDICT_META[verdict] ?? VERDICT_META.EVEN;
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border font-bold ${meta.cls} ${
        size === "lg" ? "px-3 py-1 text-xs" : "px-2.5 py-0.5 text-[11px]"
      }`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${meta.dot}`} />
      {meta.label}
    </span>
  );
}

function pct(n: number): string {
  return `${Math.round(n * 100)}%`;
}

/** A loss bar that always accounts for 100% of the answers, so nothing is unexplained. */
function VerdictBar({ counts, total }: { counts: Record<string, number>; total: number }) {
  if (!total) return null;
  const order: [string, string][] = [
    ["LOSING", "bg-red-500"],
    ["EVEN", "bg-amber-300"],
    ["WINNING", "bg-emerald-500"],
  ];
  return (
    <span className="flex h-1.5 w-full overflow-hidden rounded-full bg-slate-200">
      {order.map(([k, cls]) => {
        const n = counts[k] || 0;
        if (!n) return null;
        return <span key={k} className={cls} style={{ width: `${(100 * n) / total}%` }} />;
      })}
    </span>
  );
}

function ClaimRow({ claim }: { claim: HeadToHeadClaim }) {
  return (
    <div
      className={`rounded-xl border p-2.5 ${
        claim.against_us ? "border-red-200 bg-red-50/50" : "border-line bg-canvas"
      }`}
    >
      <p className="text-xs font-medium text-ink leading-relaxed">{claim.text}</p>
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        {claim.against_us && (
          <span className="rounded-full bg-red-100 px-2 py-0.5 text-[10px] font-bold text-red-700">
            Claim to counter
          </span>
        )}
        {claim.cross_model && (
          <span className="rounded-full bg-brand-surface px-2 py-0.5 text-[10px] font-bold text-brand-dark">
            {claim.model_count} platforms repeat this
          </span>
        )}
        <span className="text-[10px] font-medium text-ink-muted">
          {claim.answers} answer{claim.answers === 1 ? "" : "s"}
          {claim.losing_answers > 0 && ` · ${claim.losing_answers} we lost`}
        </span>
      </div>
    </div>
  );
}

function PairDrawer({
  pairKey, personas, models, onClose,
}: {
  pairKey: string;
  personas: string[];
  models: string[];
  onClose: () => void;
}) {
  const [data, setData] = useState<HeadToHeadDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Scoped to the same audience and platforms as the board, so the drawer cannot report
  // different numbers from the row that opened it.
  useEffect(() => {
    setData(null);
    setErr(null);
    api
      .headToHeadDetail(pairKey, { persona: personas, llm_name: models })
      .then(setData)
      .catch((e) => setErr(String(e?.message || e)));
  }, [pairKey, personas, models]);

  const s = data?.summary;
  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <div className="absolute inset-0 bg-ink/20" onClick={onClose} />
      <motion.div
        initial={{ x: 40, opacity: 0 }}
        animate={{ x: 0, opacity: 1 }}
        className="relative z-10 h-full w-full max-w-2xl overflow-y-auto bg-canvas-card shadow-2xl"
      >
        <div className="sticky top-0 z-10 flex items-start justify-between gap-4 border-b border-line bg-canvas-card px-6 py-4">
          <div>
            <h2 className="text-base font-bold text-ink">{s?.label ?? "Loading…"}</h2>
            {s && (
              <div className="mt-1 flex flex-wrap items-center gap-2">
                <VerdictPill verdict={s.verdict} />
                <span className="text-xs font-medium text-ink-light">
                  {s.losing_answers} of {s.answers} answers lost · {s.models.length} platform
                  {s.models.length === 1 ? "" : "s"}
                </span>
              </div>
            )}
          </div>
          <button onClick={onClose} className="rounded-lg p-1 text-ink-light hover:bg-slate-100">
            <X size={18} />
          </button>
        </div>

        {err && (
          <div className="m-6 flex items-start gap-2 rounded-xl border border-red-200 bg-red-50 p-3">
            <AlertTriangle className="mt-0.5 text-red-600" size={16} />
            <p className="text-xs font-medium text-red-800">{err}</p>
          </div>
        )}
        {!data && !err && (
          <div className="flex justify-center py-16">
            <Spinner />
          </div>
        )}

        {data && s && (
          <div className="space-y-5 p-6">
            {!s.indication_known && (
              <div className="flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 p-3">
                <Info className="mt-0.5 shrink-0 text-amber-600" size={15} />
                <p className="text-xs font-medium text-amber-900">
                  These answers name no indication we track, so the comparison was scored
                  against a broad competitor list rather than one indication's real field.
                  {s.pair_source === "text_only" && ` ${s.pair_source_note}`}
                </p>
              </div>
            )}

            <div>
              <h3 className="mb-2 flex items-center gap-1.5 text-xs font-bold uppercase tracking-wide text-ink-light">
                <Quote size={13} /> What AI says about this matchup
              </h3>
              {data.claims.claims.length ? (
                <>
                  <p className="mb-2 text-xs font-medium text-ink-light">
                    {data.claims.claims_against_us > 0 ? (
                      <>
                        <span className="font-bold text-red-700">
                          {data.claims.claims_against_us} claim
                          {data.claims.claims_against_us === 1 ? "" : "s"}
                        </span>{" "}
                        about {s.competitor} appear in answers we lose.
                      </>
                    ) : (
                      <>No claims about {s.competitor} appear in answers we lose.</>
                    )}{" "}
                    {data.claims.distinct_claims} distinct claims from{" "}
                    {data.claims.answers_with_claims} answers.
                  </p>
                  <div className="space-y-1.5">
                    {data.claims.claims.slice(0, 8).map((c) => (
                      <ClaimRow key={c.text} claim={c} />
                    ))}
                  </div>
                  {data.claims.claims_truncated > 0 && (
                    <p className="mt-1.5 text-[11px] font-medium text-ink-muted">
                      +{data.claims.claims_truncated} more, lower priority
                    </p>
                  )}
                </>
              ) : (
                <p className="text-xs text-ink-light">
                  The scorer extracted no claims from these answers.
                </p>
              )}
            </div>

            <div>
              <h3 className="mb-2 text-xs font-bold uppercase tracking-wide text-ink-light">
                Whose content AI read
              </h3>
              {data.sources.available ? (
                <div className="rounded-xl border border-line bg-canvas p-3">
                  <div className="flex flex-wrap gap-4 text-xs font-medium">
                    <span>
                      <span className="font-bold text-brand-dark">
                        {data.sources.abbvie_share_pct}%
                      </span>{" "}
                      ours
                    </span>
                    <span>
                      <span className="font-bold text-red-600">
                        {data.sources.competitor_share_pct}%
                      </span>{" "}
                      competitor
                    </span>
                    <span>
                      <span className="font-bold text-ink">
                        {data.sources.independent_share_pct}%
                      </span>{" "}
                      independent
                    </span>
                    <span className="text-ink-muted">
                      {data.sources.total_citations} citations
                    </span>
                  </div>
                  {!!data.sources.competitor_pages?.length && (
                    <div className="mt-3 border-t border-line pt-2">
                      <p className="mb-1.5 text-[11px] font-bold uppercase tracking-wide text-ink-light">
                        Competitor pages AI cited
                      </p>
                      <div className="space-y-1">
                        {data.sources.competitor_pages.slice(0, 5).map((p) => (
                          <a
                            key={p.url}
                            href={p.url}
                            target="_blank"
                            rel="noreferrer"
                            className="block truncate text-[11px] font-medium text-brand hover:underline"
                            title={p.url}
                          >
                            {p.url}
                          </a>
                        ))}
                      </div>
                    </div>
                  )}
                  <p className="mt-2 text-[11px] text-ink-muted">{data.sources.note}</p>
                </div>
              ) : (
                <p className="text-xs text-ink-light">
                  None of these answers returned citations — the models answered from their own
                  training, so there is no source trail to inspect.
                </p>
              )}
            </div>

            <div>
              <h3 className="mb-2 text-xs font-bold uppercase tracking-wide text-ink-light">
                Answers, worst first
              </h3>
              <div className="space-y-2">
                {data.sample_answers.map((a) => (
                  <div key={a.response_id} className="rounded-xl border border-line p-3">
                    <div className="mb-1 flex flex-wrap items-center gap-2">
                      <VerdictPill verdict={a.verdict} />
                      <span className="text-[11px] font-bold text-ink">{a.llm_name}</span>
                      <span className="text-[11px] font-medium text-ink-muted">{a.persona}</span>
                    </div>
                    <p className="text-xs font-medium text-ink">{a.question_text}</p>
                    {a.rationale && (
                      <p className="mt-1 text-[11px] leading-relaxed text-ink-light">
                        {a.rationale}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </motion.div>
    </div>
  );
}

function PairRow({ pair, onOpen }: { pair: HeadToHeadPair; onOpen: () => void }) {
  const [open, setOpen] = useState(false);
  const worstModel = pair.by_model[0];
  const src = SOURCE_META[pair.pair_source] ?? SOURCE_META.derived;
  return (
    <div className="rounded-xl border border-line bg-canvas-card">
      <div className="flex flex-wrap items-center gap-3 p-3">
        <button
          onClick={() => setOpen((s) => !s)}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          <ChevronDown
            size={14}
            className={`shrink-0 text-ink-light transition-transform ${open ? "rotate-180" : ""}`}
          />
          <div className="min-w-0">
            <p className="truncate text-sm font-bold text-ink">
              {pair.brand} <span className="font-medium text-ink-light">vs</span> {pair.competitor}
            </p>
            <p className="truncate text-[11px] font-medium text-ink-muted">
              {pair.disease ?? "No indication recorded"} · {pair.answers} answer
              {pair.answers === 1 ? "" : "s"} · {pair.models.length} platform
              {pair.models.length === 1 ? "" : "s"}
            </p>
          </div>
        </button>

        <div className="w-28 shrink-0">
          <VerdictBar counts={pair.verdict_counts} total={pair.answers} />
          <p className="mt-1 text-[10px] font-semibold text-ink-muted">
            {pair.losing_answers}/{pair.answers} lost
          </p>
        </div>

        <span
          className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-bold ${src.cls}`}
          title={pair.pair_source_note}
        >
          {src.label}
        </span>
        <VerdictPill verdict={pair.verdict} />
        <button
          onClick={onOpen}
          className="flex shrink-0 items-center gap-1 rounded-lg border border-line px-2.5 py-1 text-[11px] font-bold text-brand transition-colors hover:border-brand-light"
        >
          Why <ArrowRight size={12} />
        </button>
      </div>

      {open && (
        <motion.div
          initial={{ opacity: 0, y: -4 }}
          animate={{ opacity: 1, y: 0 }}
          className="border-t border-line px-3 py-2.5"
        >
          <div className="grid gap-3 sm:grid-cols-3">
            <div>
              <p className="mb-1 text-[10px] font-bold uppercase tracking-wide text-ink-light">
                By platform
              </p>
              {pair.by_model.map((m) => (
                <div key={m.llm_name} className="flex items-center justify-between text-[11px]">
                  <span className="font-medium text-ink">{m.llm_name}</span>
                  <span className="font-semibold text-ink-light">
                    {m.losing}/{m.answers} lost
                  </span>
                </div>
              ))}
            </div>
            <div>
              <p className="mb-1 text-[10px] font-bold uppercase tracking-wide text-ink-light">
                Tone gap
              </p>
              <p className="text-[11px] font-medium text-ink">
                {pair.sentiment_gap === null ? (
                  "Not scored on both sides"
                ) : (
                  <>
                    <span
                      className={
                        pair.sentiment_gap < 0 ? "font-bold text-red-600" : "font-bold text-emerald-700"
                      }
                    >
                      {pair.sentiment_gap > 0 ? "+" : ""}
                      {pair.sentiment_gap}
                    </span>{" "}
                    {pair.sentiment_gap < 0
                      ? `— AI speaks more warmly about ${pair.competitor}`
                      : `— AI speaks more warmly about ${pair.brand}`}
                  </>
                )}
              </p>
            </div>
            <div>
              <p className="mb-1 text-[10px] font-bold uppercase tracking-wide text-ink-light">
                Movement
              </p>
              {pair.trend.available ? (
                <p className="flex items-center gap-1 text-[11px] font-medium text-ink">
                  {pair.trend.direction === "worse" ? (
                    <TrendingDown size={12} className="text-red-600" />
                  ) : pair.trend.direction === "better" ? (
                    <TrendingUp size={12} className="text-emerald-600" />
                  ) : null}
                  {pair.trend.direction === "flat"
                    ? "Unchanged since the last run"
                    : `${pct(pair.trend.previous_loss_rate ?? 0)} → ${pct(
                        pair.trend.latest_loss_rate ?? 0,
                      )} of answers lost`}
                </p>
              ) : (
                <p className="text-[11px] font-medium text-ink-muted">{pair.trend.note}</p>
              )}
              {pair.disagreement.questions_with_disagreement > 0 && (
                <p className="mt-1 text-[11px] font-medium text-amber-700">
                  Platforms disagree on{" "}
                  {pair.disagreement.questions_with_disagreement} of{" "}
                  {pair.disagreement.questions_compared} questions
                </p>
              )}
            </div>
          </div>
        </motion.div>
      )}
    </div>
  );
}

/**
 * Head-to-head board: what AI tells buyers when it is asked to compare us with a rival.
 *
 * Every number here is aggregated from answers a run already produced, so the page costs
 * nothing to open and cannot change what the next reader sees.
 */
export default function HeadToHead() {
  const [board, setBoard] = useState<HeadToHeadBoard | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [openPair, setOpenPair] = useState<string | null>(null);
  const [showExcluded, setShowExcluded] = useState(false);

  const [areas, setAreas] = useState<string[]>([]);
  const [diseases, setDiseases] = useState<string[]>([]);
  const [brands, setBrands] = useState<string[]>([]);
  const [rivals, setRivals] = useState<string[]>([]);
  const [personas, setPersonas] = useState<string[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [verdicts, setVerdicts] = useState<string[]>([]);
  // Held separately from `board` so the pickers keep their options while the next board is
  // in flight, instead of emptying out on every change.
  const [facets, setFacets] = useState<HeadToHeadFilterOptions>(NO_FACETS);

  const load = useCallback(() => {
    setLoading(true);
    setErr(null);
    api
      .headToHead({
        therapeutic_area: areas,
        disease: diseases,
        brand: brands,
        competitor: rivals,
        persona: personas,
        llm_name: models,
        verdict: verdicts,
        limit: 100,
      })
      .then((data) => {
        setBoard(data);
        setFacets(data.filter_options ?? NO_FACETS);
      })
      .catch((e) => setErr(String(e?.message || e)))
      .finally(() => setLoading(false));
  }, [areas, diseases, brands, rivals, personas, models, verdicts]);

  useEffect(load, [load]);

  // One list drives the pickers AND the chips, so a filter can never be selectable but
  // impossible to clear — which is exactly what would happen to a value that dropped out
  // of its own option list after another filter moved.
  const filters = useMemo(
    () => [
      { key: "area", label: "Area", placeholder: "All areas",
        values: areas, set: setAreas, options: facets.areas },
      { key: "indication", label: "Indication", placeholder: "All indications",
        values: diseases, set: setDiseases, options: facets.diseases },
      { key: "brand", label: "Our brand", placeholder: "All brands",
        values: brands, set: setBrands, options: facets.brands },
      { key: "rival", label: "Rival", placeholder: "All rivals",
        values: rivals, set: setRivals, options: facets.competitors },
      { key: "persona", label: "Who is asking", placeholder: "Everyone",
        values: personas, set: setPersonas, options: facets.personas },
      { key: "platform", label: "AI platform", placeholder: "All platforms",
        values: models, set: setModels, options: facets.models, labels: MODEL_LABELS },
      { key: "verdict", label: "Result", placeholder: "Any result",
        values: verdicts, set: setVerdicts, options: VERDICT_ORDER,
        labels: Object.fromEntries(
          VERDICT_ORDER.map((v) => [v, VERDICT_META[v].label]),
        ) as Record<string, string> },
    ],
    [areas, diseases, brands, rivals, personas, models, verdicts, facets],
  );

  const chips = filters.flatMap((f) =>
    f.values.map((value) => ({
      id: `${f.key}:${value}`,
      label: `${f.label}: ${f.labels?.[value] ?? value}`,
      clear: () => f.set(f.values.filter((v) => v !== value)),
    })),
  );

  const clearAll = () => filters.forEach((f) => f.set([]));

  // FR-612: one sentence that leads with the action, before any chart.
  const headline = useMemo(() => {
    if (!board || !board.pairs.length) return null;
    const losing = board.pairs.filter((p) => p.verdict === "LOSING");
    const worst = losing[0] ?? board.pairs[0];
    const lostAnswers = board.pairs.reduce((n, p) => n + p.losing_answers, 0);
    return { losing, worst, lostAnswers };
  }, [board]);

  return (
    <div>
      <ComingSoonBanner
        title="Head-to-Head is coming soon"
        message={
          "This view is still being built. The pairs and verdicts below are an early preview drawn " +
          "from the answers collected so far, so read them as directional and do not rely on them " +
          "for competitive claims yet."
        }
      />

      <PageHeader
        title="Head-to-Head"
        subtitle="What AI tells people when they ask how our brands compare with a named rival."
        tooltip={
          "Built from answers already collected — comparison questions that name both our " +
          "brand and one competitor. No new model calls are made to open this page."
        }
      />

      <div className="mb-4 flex flex-wrap items-end gap-3">
        {filters.map((f) => (
          <MultiSelect
            key={f.key}
            label={f.label}
            values={f.values}
            options={f.options}
            optionLabels={f.labels}
            placeholder={f.placeholder}
            onChange={f.set}
          />
        ))}
      </div>

      {chips.length > 0 && (
        <div className="mb-5 flex flex-wrap items-center gap-2">
          {chips.map((chip) => (
            <button
              key={chip.id}
              onClick={chip.clear}
              className="flex items-center gap-1 rounded-full border border-line bg-canvas-card px-2.5 py-1 text-[11px] font-semibold text-ink transition-colors hover:border-brand-light hover:text-brand"
            >
              {chip.label}
              <X size={11} />
            </button>
          ))}
          <button
            onClick={clearAll}
            className="text-[11px] font-bold text-ink-light underline-offset-2 transition-colors hover:text-brand hover:underline"
          >
            Clear all
          </button>
        </div>
      )}

      {err && (
        <div className="mb-5 flex items-start gap-2 rounded-xl border border-red-200 bg-red-50 p-3">
          <AlertTriangle className="mt-0.5 text-red-600" size={16} />
          <p className="text-xs font-medium text-red-800">{err}</p>
        </div>
      )}

      {loading ? (
        <div className="flex justify-center py-20">
          <Spinner />
        </div>
      ) : !board || !board.pairs.length ? (
        <EmptyState
          icon={<Swords size={40} />}
          message={
            chips.length > 0
              ? `No matchup matches these ${chips.length} filter${chips.length === 1 ? "" : "s"}. ${board?.answers_in_corpus ?? 0} comparison answers exist in total — clear a filter to widen the board.`
              : board && board.answers_examined > 0
              ? `None of the ${board.answers_examined} comparison answers collected so far name both one of our brands and a tracked rival, so there is no head-to-head to score yet.`
              : "No comparison answers have been collected yet. Approve the comparison questions in Discover and run them to populate this board."
          }
          action={
            chips.length > 0 ? (
              <button
                onClick={clearAll}
                className="rounded-lg border border-line px-3 py-1.5 text-xs font-bold text-brand transition-colors hover:border-brand-light"
              >
                Clear all filters
              </button>
            ) : undefined
          }
        />
      ) : (
        <>
          {headline && (
            <div className="mb-5 rounded-2xl border border-line bg-brand-surface/40 p-4">
              <div className="mb-1 flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wide text-brand-dark">
                <Sparkles size={13} /> The big picture
              </div>
              <p className="text-sm font-medium leading-relaxed text-ink">
                {headline.losing.length > 0 ? (
                  <>
                    AI argues against us in{" "}
                    <span className="font-bold">
                      {headline.losing.length} of {board.pairs_total}
                    </span>{" "}
                    monitored matchups. The most exposed is{" "}
                    <span className="font-bold">
                      {headline.worst.brand} vs {headline.worst.competitor}
                    </span>
                    , losing{" "}
                    <span className="font-bold">
                      {headline.worst.losing_answers} of {headline.worst.answers}
                    </span>{" "}
                    answers
                    {headline.worst.by_model[0] &&
                      headline.worst.by_model[0].loss_rate === 1 &&
                      ` on every platform`}
                    . Open it to see the claims driving the loss.
                  </>
                ) : (
                  <>
                    No matchup is currently going against us across{" "}
                    <span className="font-bold">{board.answers_on_the_board}</span> scored
                    answers. Keep an eye on the ones marked too close to call.
                  </>
                )}
              </p>
            </div>
          )}

          <div className="mb-5 grid grid-cols-2 gap-4 lg:grid-cols-4">
            <Stat label="Matchups monitored" value={board.pairs_total} />
            <Stat
              label="Answers on the board"
              value={board.answers_on_the_board}
              sub={
                chips.length > 0
                  ? `of ${board.answers_in_corpus} collected, ${board.answers_examined} in this scope`
                  : `of ${board.answers_examined} comparison answers`
              }
            />
            <Stat
              label="Answers we lose"
              value={headline?.lostAnswers ?? 0}
              tooltip="An answer counts as lost when AI puts us second-line, advises against us, or leaves us out entirely."
            />
            <Stat
              label="Matchups going against us"
              value={board.pairs.filter((p) => p.verdict === "LOSING").length}
            />
          </div>

          {/* Fed the SAME board object the list below reads, so a chart can never disagree
              with the row it sits above and one filter change moves both. */}
          <HeadToHeadCharts board={board} />

          <AnimatedCard>
            <Card>
              <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
                <div className="flex items-start gap-2">
                  <Swords className="mt-0.5 text-brand" size={18} />
                  <div>
                    <h3 className="flex items-center gap-1 text-sm font-bold text-ink">
                      Every matchup, most exposed first
                      <InfoTooltip content={board.verdict_rule} />
                    </h3>
                    <p className="mt-0.5 text-xs font-medium text-ink-light">
                      Ranked by how many answers are actually going against us, not by
                      percentage — a matchup losing 15 of 25 matters more than one losing 2 of 2.
                    </p>
                  </div>
                </div>
              </div>

              <div className="space-y-2">
                {board.pairs.map((p) => (
                  <PairRow key={p.key} pair={p} onOpen={() => setOpenPair(p.key)} />
                ))}
              </div>

              {board.answers_excluded > 0 && (
                <div className="mt-4 border-t border-line pt-3">
                  <button
                    onClick={() => setShowExcluded((s) => !s)}
                    className="flex items-center gap-1 text-xs font-bold text-ink-light transition-colors hover:text-ink"
                  >
                    {board.answers_excluded} comparison answers are not on this board
                    <ChevronDown
                      size={13}
                      className={`transition-transform ${showExcluded ? "rotate-180" : ""}`}
                    />
                  </button>
                  {showExcluded && (
                    <div className="mt-2 space-y-1.5">
                      {board.exclusions.map((e) => (
                        <div key={e.reason} className="flex items-start gap-2">
                          <span className="mt-0.5 w-8 shrink-0 text-right text-xs font-bold tabular-nums text-ink">
                            {e.answers}
                          </span>
                          <span
                            className={`text-xs font-medium ${
                              e.reason === H2H_FILTERED_OUT ? "text-brand-dark" : "text-ink-light"
                            }`}
                          >
                            {e.explanation}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </Card>
          </AnimatedCard>
        </>
      )}

      {openPair && (
        <PairDrawer
          pairKey={openPair}
          personas={personas}
          models={models}
          onClose={() => setOpenPair(null)}
        />
      )}
    </div>
  );
}
