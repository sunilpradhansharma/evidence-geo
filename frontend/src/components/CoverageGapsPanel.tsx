import { useCallback, useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import { Grid3x3, Sparkles, AlertTriangle, Check, ChevronDown, Plus } from "lucide-react";
import {
  api,
  CoverageFunnel,
  CurationCoverage,
  CurationGenerateResult,
  CurationScope,
} from "../api/client";
import { Card, Spinner, InfoTooltip, MultiSelect } from "./ui";
import { AddBrandModal } from "./AddBrandModal";
import {
  AREA_OPTIONS,
  COVERAGE_BRAND_OPTIONS,
  applyTaxonomy,
  areasForBrand,
  useTaxonomy,
} from "../lib/taxonomy";

const PERSONAS = ["Patient", "Provider", "Prospect"];
const BREAKDOWN_ROWS = 6;

/** Tally straight from the summary, which counts every gap — not the page-capped list. */
function sortedTally(tally?: Record<string, number>): [string, number][] {
  return Object.entries(tally ?? {}).sort((a, b) => b[1] - a[1]);
}

const STATE_STYLE: Record<string, { bar: string; text: string }> = {
  ANSWERED: { bar: "bg-emerald-500", text: "text-emerald-700" },
  APPROVED_NOT_RUN: { bar: "bg-brand-light", text: "text-brand-dark" },
  IN_REVIEW: { bar: "bg-amber-400", text: "text-amber-700" },
  DECLINED: { bar: "bg-slate-400", text: "text-ink-light" },
  NOT_ASKED: { bar: "bg-slate-200", text: "text-ink-muted" },
};

/**
 * How far each comparison actually got.
 *
 * The coverage percentage above counts a comparison as covered as soon as a question
 * exists — including one still sitting unreviewed in the queue below. That is not the
 * same as being monitored, and on the real bank the two numbers are far apart, so the
 * gap between them is stated outright rather than left for the reader to discover.
 */
function FunnelBand({ data }: { data: CoverageFunnel }) {
  const total = data.total_cells || 1;
  const shown = data.states.filter((s) => s.cells > 0);
  return (
    <div className="mt-4 rounded-xl border border-line bg-canvas p-3">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <p className="flex items-center gap-1 text-xs font-bold text-ink">
          Actually monitored
          <InfoTooltip
            content={
              "A comparison is monitored only once a model has answered it and the answer " +
              "has been scored. A question that exists but is still awaiting review, or is " +
              "approved but has never been run, is not being watched."
            }
          />
        </p>
        <p className="text-xs font-medium text-ink-light">
          <span className="font-bold text-ink">{data.monitored_cells}</span> of {data.total_cells}{" "}
          ({data.monitored_pct}%)
        </p>
      </div>

      <span className="flex h-2 w-full overflow-hidden rounded-full bg-slate-100">
        {shown.map((s) => (
          <span
            key={s.state}
            className={STATE_STYLE[s.state]?.bar ?? "bg-slate-200"}
            style={{ width: `${(100 * s.cells) / total}%` }}
            title={`${s.cells} — ${s.label}`}
          />
        ))}
      </span>

      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
        {shown.map((s) => (
          <span key={s.state} className="flex items-center gap-1.5 text-[11px] font-medium">
            <span className={`h-2 w-2 rounded-full ${STATE_STYLE[s.state]?.bar ?? "bg-slate-200"}`} />
            <span className="font-bold text-ink">{s.cells}</span>
            <span className={STATE_STYLE[s.state]?.text ?? "text-ink-light"}>{s.label}</span>
          </span>
        ))}
      </div>

      {data.covered_but_unmonitored > 0 && (
        <p className="mt-2.5 border-t border-line pt-2 text-xs font-medium text-ink">
          <span className="font-bold text-amber-700">{data.covered_but_unmonitored}</span>{" "}
          comparison{data.covered_but_unmonitored === 1 ? "" : "s"} already {" "}
          {data.covered_but_unmonitored === 1 ? "has" : "have"} a question written but{" "}
          {data.covered_but_unmonitored === 1 ? "is" : "are"} still not being monitored —
          generating more will not change that. Review the queue below first.
        </p>
      )}
    </div>
  );
}

function Breakdown({ title, entries }: { title: string; entries: [string, number][] }) {
  const shown = entries.slice(0, BREAKDOWN_ROWS);
  const rest = entries.length - shown.length;
  const max = entries[0]?.[1] || 1;
  return (
    <div>
      <p className="text-[11px] font-bold uppercase tracking-wide text-ink-light mb-1.5">{title}</p>
      <div className="space-y-1">
        {shown.map(([label, n]) => (
          <div key={label} className="flex items-center gap-2">
            <span className="w-28 shrink-0 truncate text-xs font-medium text-ink" title={label}>{label}</span>
            <span className="h-1.5 flex-1 rounded-full bg-slate-200">
              <span className="block h-full rounded-full bg-amber-400" style={{ width: `${Math.round((100 * n) / max)}%` }} />
            </span>
            <span className="w-7 shrink-0 text-right text-xs font-bold tabular-nums text-ink-light">{n}</span>
          </div>
        ))}
      </div>
      {rest > 0 && <p className="mt-1 text-[11px] font-medium text-ink-muted">+{rest} more</p>}
    </div>
  );
}

/**
 * Comparison coverage and the questions that would close the gaps.
 *
 * Lives on the Discover page on purpose: generated candidates land in the same review
 * queue below, so the existing select -> Run to Pipeline flow works on them unchanged.
 * There is no promote button here — a model-written question gets no shortcut past
 * Medical Affairs.
 */
export function CoverageGapsPanel({ onStaged }: { onStaged: () => void }) {
  // Changes when the backend taxonomy replaces the built-in fallback.
  const taxonomyVersion = useTaxonomy();
  const [brands, setBrands] = useState<string[]>([]);
  const [areas, setAreas] = useState<string[]>([]);
  const [personas, setPersonas] = useState<string[]>([]);
  const [limit, setLimit] = useState(10);
  const [data, setData] = useState<CurationCoverage | null>(null);
  const [funnel, setFunnel] = useState<CoverageFunnel | null>(null);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [result, setResult] = useState<CurationGenerateResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [showGaps, setShowGaps] = useState(false);
  const [addingBrand, setAddingBrand] = useState(false);
  const [added, setAdded] = useState<string | null>(null);

  // An empty brand or area list means "every one of them" to the server, but an empty
  // PERSONA list means Patient + Provider only. So the one scope field whose "all"
  // state has to be sent explicitly is this one, or the dropdown would show Prospect
  // ticked while the server quietly left it out.
  const activePersonas = personas.length ? personas : PERSONAS;

  const scope: CurationScope = {
    brands,
    therapeutic_areas: areas,
    personas: activePersonas,
  };

  const load = useCallback(() => {
    setLoading(true);
    setErr(null);
    api
      .curationCoverage({ brands, therapeutic_areas: areas, personas: activePersonas })
      .then(setData)
      .catch((e) => setErr(String(e?.message || e)))
      .finally(() => setLoading(false));
    // Same resolved scope, separate request — and deliberately not allowed to fail the
    // panel: the gap list is still usable when the funnel cannot be computed.
    api
      .curationFunnel({ brands, therapeutic_areas: areas, personas: activePersonas })
      .then(setFunnel)
      .catch(() => setFunnel(null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [brands.join("|"), areas.join("|"), personas.join("|")]);

  useEffect(load, [load]);

  // Read inside the component, not at module scope. As a module-level constant this was
  // captured at import — before the backend taxonomy had loaded — so a brand added through
  // the UI would never have appeared in its own picker.
  //
  // Coverage brands, not all focus brands: a brand whose therapeutic area declares no
  // disease overlay has no comparison defined for it, and offering it here would answer
  // "no gaps" — indistinguishable from being fully covered.
  const focusBrands = useMemo(() => COVERAGE_BRAND_OPTIONS, [taxonomyVersion]);

  // Selecting a brand narrows the area options to the areas it is actually indicated in,
  // so a multi-area brand shows all three rather than being collapsed to a primary one.
  const areaOptions = brands.length
    ? [...new Set(brands.flatMap((b) => areasForBrand(b)))]
    : AREA_OPTIONS;

  const pickBrands = (next: string[]) => {
    const valid = new Set(next.length ? next.flatMap((b) => areasForBrand(b)) : AREA_OPTIONS);
    setBrands(next);
    setAreas(areas.filter((a) => valid.has(a)));
  };

  const generate = async () => {
    setGenerating(true);
    setErr(null);
    setResult(null);
    try {
      const res = await api.curationGenerate({ ...scope, limit, commit: true });
      setResult(res);
      load();
      onStaged();
    } catch (e: any) {
      setErr(String(e?.message || e));
    } finally {
      setGenerating(false);
    }
  };

  const summary = data?.summary;
  // Zero tracked comparisons is not zero gaps: the selection has nothing to measure at
  // all, which reads as a clean bill of health unless it is said in words.
  const totalCells = summary?.total_cells ?? 0;
  // The returned gap list is page-capped and generate() re-ranks server-side, so the
  // summary total — not the page length — is what the cost line has to be based on.
  const gapCount = summary?.gaps ?? 0;
  const willWrite = Math.min(limit, gapCount);
  const plannedCalls = Math.ceil(willWrite / 5);
  const byArea = sortedTally(summary?.gaps_by_area);
  const byDisease = sortedTally(summary?.gaps_by_disease);
  const byBrand = sortedTally(summary?.gaps_by_brand);
  // The page holds 100 and the generator never writes more than 50, so this head is the
  // exact set the next commit would fill, in the same rank order the server uses.
  const nextUp = (data?.gaps ?? []).slice(0, willWrite);
  // Gaps rank by evidence depth before anything else, so a brand outside the Rinvoq/
  // Skyrizi programme sits at the back of a wide selection and this pass never reaches
  // it. Naming the brands that get nothing is the difference between a considered
  // priority order and a run that looks like it silently skipped them.
  const nextUpBrands = new Set(nextUp.map((g) => g.brand));
  const brandsLeftOut = byBrand.filter(([b]) => !nextUpBrands.has(b)).map(([b]) => b);

  return (
    <Card>
      <div className="flex items-start justify-between gap-4 flex-wrap mb-4">
        <div className="flex items-start gap-2">
          <Grid3x3 className="text-brand mt-0.5" size={18} />
          <div>
            <h3 className="text-sm font-bold text-ink flex items-center gap-1">
              Comparison coverage
              <InfoTooltip content={
                "Every head-to-head comparison the taxonomy says should be monitored " +
                "(brand vs competitor, per indication, per persona), minus the ones the " +
                "question bank already covers. Web harvesting can only find questions " +
                "that happen to exist online; this asks the model to write the ones that " +
                "are missing. Candidates land in the review queue below and still need " +
                "Medical Affairs approval."
              } />
            </h3>
            <p className="text-xs text-ink-light mt-0.5 font-medium">
              Every brand, area and persona is included — untick anything you want left out.
            </p>
          </div>
        </div>
        {summary && (totalCells === 0 ? (
          <p className="max-w-[18rem] text-xs font-medium text-ink-light text-right">
            No head-to-head comparisons are tracked for this selection yet.
          </p>
        ) : (
          <div className="flex gap-5 text-right">
            <div>
              <div className="text-lg font-bold text-ink">{summary.coverage_pct}%</div>
              <div className="text-[11px] text-ink-light font-semibold uppercase tracking-wide">Covered</div>
            </div>
            <div>
              <div className="text-lg font-bold text-amber-600">{summary.gaps}</div>
              <div className="text-[11px] text-ink-light font-semibold uppercase tracking-wide">Gaps</div>
            </div>
            <div>
              <div className="text-lg font-bold text-ink-light">{summary.total_cells}</div>
              <div className="text-[11px] text-ink-light font-semibold uppercase tracking-wide">Comparisons</div>
            </div>
          </div>
        ))}
      </div>

      <div className="flex flex-wrap items-end gap-3 border-t border-line pt-4">
        <div className="flex items-end gap-1.5">
          <MultiSelect
            label="Brand"
            values={brands}
            options={focusBrands}
            placeholder="All brands"
            onChange={pickBrands}
            tooltip={"Focus brand(s) to check. All are included — untick the ones you want left out."}
          />
          <button
            onClick={() => setAddingBrand(true)}
            title="Add a brand to the taxonomy"
            className="flex h-[38px] items-center gap-1 rounded-lg border border-line px-2.5 text-xs font-semibold text-ink-light hover:border-brand-light hover:text-brand-dark"
          >
            <Plus size={13} /> Add brand
          </button>
        </div>
        <MultiSelect
          label="Therapeutic Area"
          values={areas}
          options={areaOptions}
          placeholder="All areas"
          onChange={setAreas}
          tooltip={"Therapeutic area(s) to scope to — narrowed to the areas the selected brands are indicated in."}
        />
        <MultiSelect
          label="Persona"
          values={personas}
          options={PERSONAS}
          placeholder="All personas"
          onChange={setPersonas}
          tooltip={"Who is asking: Patient, Provider or Prospect."}
        />
        <div className="flex flex-col">
          <label className="text-xs font-semibold text-ink-light mb-1.5 uppercase tracking-wide">
            Generate up to
          </label>
          <input
            type="number" min={1} max={50} value={limit}
            onChange={(e) => setLimit(Math.max(1, Math.min(50, Number(e.target.value) || 1)))}
            className="w-20 border border-line rounded-xl px-3 py-2 text-sm bg-canvas-card text-ink font-medium focus:outline-none focus:ring-2 focus:ring-brand-light/40"
          />
        </div>
        <button
          onClick={generate}
          disabled={generating || !gapCount}
          className="ml-auto flex items-center gap-2 px-5 py-2 bg-brand text-white rounded-xl text-sm font-bold hover:bg-brand-dark disabled:opacity-40 transition-colors"
        >
          {generating ? <Spinner size={16} /> : <Sparkles size={16} />}
          {generating ? "Writing…" : "Generate questions"}
        </button>
      </div>

      <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
        {/* Stated before the click, not after: every generated question also costs one
            call per monitored model on every future run. */}
        <p className="flex items-center gap-2 text-xs text-ink-light font-medium">
          {loading ? (
            <><Spinner size={14} /> Counting uncovered comparisons…</>
          ) : totalCells === 0 ? (
            "Nothing to measure here — this selection has no head-to-head comparisons to monitor. Widen it to see coverage."
          ) : gapCount === 0 ? (
            "No uncovered comparisons in this scope — every head-to-head already has a question in the bank or in review."
          ) : (
            `${plannedCalls} model call${plannedCalls === 1 ? "" : "s"} to write ${willWrite} question${willWrite === 1 ? "" : "s"} for the highest-value gaps.`
          )}
        </p>
        {!loading && gapCount > 0 && (
          <button
            onClick={() => setShowGaps((s) => !s)}
            className="flex items-center gap-1 text-xs font-bold text-brand hover:text-brand-dark transition-colors"
          >
            {showGaps ? "Hide what's missing" : `Show what's missing (${gapCount})`}
            <ChevronDown size={13} className={`transition-transform ${showGaps ? "rotate-180" : ""}`} />
          </button>
        )}
      </div>

      {funnel && funnel.total_cells > 0 && <FunnelBand data={funnel} />}

      {showGaps && gapCount > 0 && (
        <motion.div
          initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }}
          className="mt-3 rounded-xl border border-line bg-canvas p-3"
        >
          {!!byArea.length && !!byBrand.length && (
            <p className="mb-3 text-xs text-ink font-medium">
              Most of the gap sits in <span className="font-bold">{byArea[0][0]}</span>{" "}
              ({byArea[0][1]} of {gapCount}), and <span className="font-bold">{byBrand[0][0]}</span>{" "}
              is the brand missing the most comparisons ({byBrand[0][1]}).
            </p>
          )}
          <div className="grid gap-4 sm:grid-cols-3">
            <Breakdown title="By area" entries={byArea} />
            <Breakdown title="By indication" entries={byDisease} />
            <Breakdown title="By brand" entries={byBrand} />
          </div>
          {!!nextUp.length && (
            <div className="mt-3 border-t border-line pt-3">
              <p className="text-[11px] font-bold uppercase tracking-wide text-ink-light mb-1.5">
                The {nextUp.length} Generate would write next
              </p>
              <div className="flex flex-wrap gap-1.5">
                {nextUp.map((g) => (
                  <span
                    key={g.key}
                    className="rounded-full border border-line bg-canvas-card px-2.5 py-1 text-[11px] font-medium text-ink-light"
                  >
                    <span className="font-bold text-ink">{g.brand}</span> vs{" "}
                    <span className="font-bold text-ink">{g.competitor}</span> · {g.disease} · {g.persona}
                  </span>
                ))}
              </div>
              {brandsLeftOut.length > 0 && (
                <p className="mt-2 text-[11px] font-medium text-ink-muted">
                  Highest-value gaps first, so this pass writes nothing for{" "}
                  <span className="font-semibold text-ink-light">
                    {brandsLeftOut.slice(0, 4).join(", ")}
                  </span>
                  {brandsLeftOut.length > 4 ? ` and ${brandsLeftOut.length - 4} more` : ""}.{" "}
                  Pick a brand above to target it.
                </p>
              )}
            </div>
          )}
        </motion.div>
      )}

      {err && (
        <div className="mt-4 flex items-start gap-2 rounded-xl border border-red-200 bg-red-50 p-3">
          <AlertTriangle className="text-red-600 mt-0.5" size={16} />
          <p className="text-xs font-medium text-red-800">{err}</p>
        </div>
      )}

      {result && (
        <motion.div
          initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}
          className="mt-4 rounded-xl border border-emerald-200 bg-emerald-50 p-3"
        >
          <p className="text-xs font-bold text-ink flex items-center gap-1.5">
            <Check size={14} className="text-emerald-600" />
            Staged {result.created ?? 0} new and refreshed {result.refreshed ?? 0} in{" "}
            {result.model_calls} model call{result.model_calls === 1 ? "" : "s"}. Review them below.
          </p>
          {!!result.rejected?.length && (
            <p className="text-xs text-amber-700 mt-1 font-medium">
              {result.rejected.length} candidate(s) discarded: {result.rejected[0].reason}
              {result.rejected.length > 1 ? " (and others)" : ""}
            </p>
          )}
        </motion.div>
      )}

      {added && (
        <motion.div
          initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}
          className="mt-4 rounded-xl border border-emerald-200 bg-emerald-50 p-3"
        >
          <p className="flex items-center gap-1.5 text-xs font-bold text-ink">
            <Check size={14} className="text-emerald-600" />
            {added} added. It is selectable above and its comparison cells are included below.
          </p>
        </motion.div>
      )}

      {addingBrand && (
        <AddBrandModal
          onClose={() => setAddingBrand(false)}
          onAdded={async (created) => {
            setAddingBrand(false);
            setAdded(created.brand);
            // Re-read the taxonomy rather than patching the store by hand: the backend is the
            // source, and a locally-assembled version could disagree with what was actually
            // written. This is what puts the new brand in the picker without a reload.
            await api.taxonomy().then(applyTaxonomy).catch(() => {});
            load();
          }}
        />
      )}
    </Card>
  );
}
