import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Check,
  Gauge,
  Play,
  Scale,
  ShieldAlert,
} from "lucide-react";

import {
  api,
  type AlignmentReport,
  type AlignmentRollup,
  type ClaimEvaluationRunResult,
  type ClaimVocabulary,
  type EvidenceOverview,
  type Run,
} from "../api/client";
import {
  AnimatedCard,
  Card,
  EmptyState,
  InfoTooltip,
  PageHeader,
  Select,
  Spinner,
  Stat,
} from "../components/ui";

/**
 * Phase 8 — how closely monitored models match our curated evidence, claim by claim.
 *
 * **Coverage is the headline, not the alignment score.** Claims we could not check are
 * excluded from the score deliberately: including them would make alignment fall as our
 * evidence base thins, which is exactly backwards. The consequence is that a score of 100%
 * over three checkable claims out of forty is *unmeasured*, not aligned — so this page
 * refuses to show a score without the coverage beside it, and says plainly when coverage is
 * too thin to read the score at all.
 */

const CLASSIFICATION_META: Record<string, { label: string; cls: string }> = {
  ALIGNED: { label: "Aligned", cls: "bg-teal-100 text-teal-800" },
  PARTIALLY_ALIGNED: { label: "Partially aligned", cls: "bg-amber-100 text-amber-800" },
  CONTRADICTORY: { label: "Contradictory", cls: "bg-red-100 text-red-700" },
  UNSUPPORTED: { label: "Unsupported", cls: "bg-orange-100 text-orange-800" },
  OUTDATED: { label: "Outdated", cls: "bg-violet-100 text-violet-800" },
  IMPORTANT_OMISSION: { label: "Important omission", cls: "bg-rose-100 text-rose-700" },
  EVIDENCE_UNAVAILABLE: { label: "We hold no evidence", cls: "bg-slate-100 text-ink-light" },
  NOT_COMPARABLE: { label: "Not comparable", cls: "bg-slate-100 text-ink-light" },
};

const CERTAINTY_META: Record<
  string,
  { label: string; cls: string; icon: typeof ArrowUp; help: string }
> = {
  OVERCLAIMED: {
    label: "Over-claimed",
    cls: "bg-red-100 text-red-700",
    icon: ArrowUp,
    help: "The model stated more confidence than the interval supports — it asserted a winner where our evidence does not distinguish the treatments.",
  },
  UNDERCLAIMED: {
    label: "Under-claimed",
    cls: "bg-sky-100 text-sky-800",
    icon: ArrowDown,
    help: "Our evidence is more definite than the answer given. Usually means the evidence is not reaching the model — a communication gap, not a model fault.",
  },
  CALIBRATED: {
    label: "Calibrated",
    cls: "bg-teal-100 text-teal-800",
    icon: Check,
    help: "The model's hedging matches the statistical uncertainty.",
  },
};

/** Below this, the score is describing too few claims to mean anything. */
const THIN_COVERAGE = 0.4;

/** The server's own ceiling on responses per evaluation call (`limit` is `le=1000`). */
const MAX_PER_RUN = 1000;

function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;
}

export default function EvidenceAlignmentPage() {
  const [report, setReport] = useState<AlignmentReport | null>(null);
  const [vocab, setVocab] = useState<ClaimVocabulary | null>(null);
  const [indication, setIndication] = useState("");
  const [llmName, setLlmName] = useState("");
  const [runs, setRuns] = useState<Run[]>([]);
  const [readiness, setReadiness] = useState<EvidenceOverview | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.claimVocabulary().then(setVocab).catch(() => setVocab(null));
    api.runs().then(setRuns).catch(() => setRuns([]));
    api.evidenceOverview().then(setReadiness).catch(() => setReadiness(null));
  }, []);

  useEffect(() => {
    setLoading(true);
    api
      .alignmentReport({
        indication: indication || undefined,
        llm_name: llmName || undefined,
      })
      .then((r) => {
        setReport(r);
        setError(null);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [indication, llmName, reloadToken]);

  const models = useMemo(
    () => Object.entries(report?.by_model ?? {}).sort((a, b) => b[1].claim_count - a[1].claim_count),
    [report],
  );
  const claimTypes = useMemo(
    () =>
      Object.entries(report?.by_claim_type ?? {}).sort(
        (a, b) => b[1].claim_count - a[1].claim_count,
      ),
    [report],
  );

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <Spinner size={28} />
      </div>
    );
  }
  if (error) {
    return (
      <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800">
        {error}
      </div>
    );
  }

  const overall = report?.overall;
  const nothingEvaluated = !overall || overall.claim_count === 0;

  return (
    <div>
      <PageHeader
        title="AI vs Evidence Alignment"
        subtitle="Every claim a monitored model made, checked against the evidence that can actually answer it."
      />

      <EvaluateRunPanel
        runs={runs}
        readiness={readiness}
        onEvaluated={() => setReloadToken((t) => t + 1)}
      />

      {nothingEvaluated ? (
        <Card className="mt-4">
          <EmptyState
            icon={<Scale size={34} />}
            message={
              "Nothing has been checked yet. Pick a completed run above and we will read " +
              "every answer it produced, split each one into individual claims, and grade " +
              "each claim against the evidence that can answer it. This is never done " +
              "automatically, because it costs one model call per answer."
            }
          />
        </Card>
      ) : (
        <>
          <HeadlineBanner overall={overall} />

          <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Stat
              label="Coverage"
              value={pct(overall.coverage)}
              sub={`${overall.checkable_count} of ${overall.claim_count} claims checkable`}
              icon={<Gauge size={16} />}
              tooltip="Share of extracted claims our evidence could actually adjudicate. Read this BEFORE the alignment score — a high score over few checkable claims is unmeasured, not aligned."
            />
            <Stat
              label="Alignment score"
              value={pct(overall.alignment_score)}
              sub="of checkable claims"
              icon={<Scale size={16} />}
              tooltip="Aligned counts 1, partially aligned counts 0.5. Claims we could not check are excluded — including them would make alignment fall as our evidence base thins."
            />
            <Stat
              label="Adverse findings"
              value={overall.adverse_count}
              sub="contradictory, unsupported or outdated"
              icon={<AlertTriangle size={16} />}
            />
            <Stat
              label="Safety contradictions"
              value={overall.safety_contradictions}
              sub={overall.safety_contradictions ? "escalate first" : "none detected"}
              icon={<ShieldAlert size={16} />}
              tooltip="A model denying a boxed warning that exists. The single most consequential finding this system can produce."
            />
          </div>

          <div className="mb-6 flex flex-wrap items-end gap-3">
            <TextFilter
              label="Indication"
              value={indication}
              placeholder="All indications"
              onChange={setIndication}
            />
            <TextFilter
              label="Model"
              value={llmName}
              placeholder="All models"
              onChange={setLlmName}
            />
          </div>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Card title="Certainty calibration">
              <CertaintyBreakdown counts={overall.certainty_calibration} />
              <p className="mt-3 text-xs leading-relaxed text-ink-light">
                Measured in <strong>both</strong> directions. Over-claiming is a
                misinformation risk; under-claiming usually means our evidence is not
                reaching the model, which is a communication gap rather than a model fault.
              </p>
            </Card>

            <Card title="Findings by classification">
              <ClassificationBreakdown counts={overall.by_classification} />
              <p className="mt-3 text-xs leading-relaxed text-ink-light">
                <code>Unsupported</code> and <code>Contradictory</code> are deliberately
                distinct. &ldquo;Nothing we hold shows this&rdquo; is an absence of
                evidence; &ldquo;our evidence shows the opposite&rdquo; is a correction.
                They lead to different actions.
              </p>
            </Card>
          </div>

          {models.length > 0 && (
            <Card title="By model" className="mt-4">
              <RollupTable rows={models} firstColumn="Model" />
            </Card>
          )}

          {claimTypes.length > 0 && (
            <Card title="By claim type" className="mt-4">
              <RollupTable rows={claimTypes} firstColumn="Claim type" />
              {vocab && (
                <p className="mt-3 text-xs leading-relaxed text-ink-light">
                  Each claim type is graded only against the evidence that can answer it —
                  an approval claim against {vocab.policy.APPROVAL_CLAIM?.description}, a
                  ranking claim against{" "}
                  {vocab.policy.RANKING_CLAIM?.description}. Routing one to the wrong
                  authority is refused rather than scored.
                </p>
              )}
            </Card>
          )}

          {report.adverse_examples.length > 0 && (
            <Card title="Adverse findings" className="mt-4">
              <div className="space-y-3">
                {report.adverse_examples.map((ex) => (
                  <AnimatedCard key={ex.claim_id}>
                    <div className="rounded-xl border border-line bg-slate-50 p-3.5">
                      <div className="mb-1.5 flex flex-wrap items-center gap-2">
                        <ClassificationBadge value={ex.classification} />
                        <span className="text-xs font-bold uppercase tracking-wide text-ink-light">
                          {ex.claim_type.replace(/_/g, " ").toLowerCase()}
                        </span>
                        {ex.llm_name && (
                          <span className="text-xs font-semibold text-ink-light">
                            · {ex.llm_name}
                          </span>
                        )}
                      </div>
                      <p className="text-sm font-bold leading-snug text-ink">
                        &ldquo;{ex.claim_text}&rdquo;
                      </p>
                      {ex.reason && (
                        <p className="mt-1.5 text-sm leading-relaxed text-ink-light">
                          {ex.reason}
                        </p>
                      )}
                    </div>
                  </AnimatedCard>
                ))}
              </div>
            </Card>
          )}
        </>
      )}
    </div>
  );
}

/**
 * The trigger. Evaluation is opt-in per run because it spends one model call per answer, so
 * this control states the exact number of calls it is about to buy before it buys them.
 *
 * It also refuses to be a silent money hole. Every grader routes to *verified* or *ratified*
 * evidence — an unverified extraction may not grade a response — so when the store holds
 * none of the three authorities, evaluating a run produces claims that are all
 * "we hold no evidence". That is a true report and a useless one, so it has to be asked for
 * explicitly rather than clicked into by accident.
 */
function EvaluateRunPanel({
  runs,
  readiness,
  onEvaluated,
}: {
  runs: Run[];
  readiness: EvidenceOverview | null;
  onEvaluated: () => void;
}) {
  const evaluable = useMemo(
    () =>
      runs
        // Exactly what the server evaluates: SUCCESS plus TRUNCATED.
        .map((run) => ({ run, answers: run.responses_success + run.responses_truncated }))
        .filter((entry) => entry.answers > 0)
        .sort((a, b) => (b.run.started_at ?? "").localeCompare(a.run.started_at ?? "")),
    [runs],
  );

  const [runId, setRunId] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ClaimEvaluationRunResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!runId && evaluable.length) setRunId(evaluable[0].run.run_id);
  }, [evaluable, runId]);

  const selected = evaluable.find((entry) => entry.run.run_id === runId) ?? null;
  const calls = selected ? Math.min(selected.answers, MAX_PER_RUN) : 0;

  const verifiedStudies = readiness?.studies.by_verification_status?.VERIFIED ?? 0;
  const verifiedFacts = readiness?.drug_facts.by_verification_status?.VERIFIED ?? 0;
  const ratifiedNetworks = readiness?.networks.by_ratification_status?.RATIFIED ?? 0;
  const noAuthority =
    readiness !== null && !verifiedStudies && !verifiedFacts && !ratifiedNetworks;

  const evaluate = () => {
    if (!selected) return;
    setBusy(true);
    setError(null);
    setResult(null);
    api
      .evaluateRunClaims(selected.run.run_id, calls)
      .then((r) => {
        setResult(r);
        onEvaluated();
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false));
  };

  if (!evaluable.length) {
    return (
      <Card>
        <p className="text-sm leading-relaxed text-ink-light">
          No completed run has any answers to check yet.{" "}
          <Link to="/run-analysis" className="font-semibold text-brand hover:underline">
            Run the pipeline
          </Link>{" "}
          first — this page reads answers that already exist, it never produces them.
        </p>
      </Card>
    );
  }

  return (
    <Card title="Check a run against our evidence">
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-[18rem]">
          <Select
            label="Run"
            value={runId}
            options={evaluable.map((entry) => entry.run.run_id)}
            optionLabels={Object.fromEntries(
              evaluable.map((entry) => [entry.run.run_id, runLabel(entry.run, entry.answers)]),
            )}
            onChange={(v) => {
              setRunId(v);
              setResult(null);
              setError(null);
            }}
          />
        </div>
        <button
          onClick={evaluate}
          disabled={busy || !selected || (noAuthority && !acknowledged)}
          className="inline-flex items-center gap-2 rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-brand-light disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? <Spinner size={14} /> : <Play size={14} strokeWidth={2.6} />}
          {busy ? "Checking…" : `Check ${calls.toLocaleString()} answer${calls === 1 ? "" : "s"}`}
        </button>
        <p className="pb-1 text-xs leading-relaxed text-ink-light">
          One model call per answer — <strong>{calls.toLocaleString()}</strong> calls.
          {selected && selected.answers > MAX_PER_RUN && (
            <>
              {" "}
              This run holds {selected.answers.toLocaleString()} answers; the server caps one
              request at {MAX_PER_RUN.toLocaleString()}.
            </>
          )}
        </p>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <span className="text-xs font-bold uppercase tracking-wide text-ink-light">
          Evidence that can grade a claim
        </span>
        <ReadinessPill
          label="verified studies"
          count={verifiedStudies}
          help="Trial-result claims are graded against VERIFIED outcome rows only. Telling a brand team a model has the numbers wrong, on an extraction nobody has checked, is the worst output this system could produce."
        />
        <ReadinessPill
          label="verified label facts"
          count={verifiedFacts}
          help="Approval, boxed-warning and mechanism claims are graded against a verified label record."
        />
        <ReadinessPill
          label="ratified networks"
          count={ratifiedNetworks}
          help="Comparative and ranking claims are resolved in GOVERNED mode. An exploratory estimate may not grade a response, so an unratified network yields 'we hold no evidence'."
        />
      </div>

      {noAuthority && (
        <div
          role="alert"
          className="mt-3 flex items-start gap-3 rounded-xl border-2 border-amber-300 bg-amber-50 p-4"
        >
          <AlertTriangle size={18} className="mt-0.5 shrink-0 text-amber-600" strokeWidth={2.2} />
          <div className="text-sm text-amber-900">
            <p className="font-bold">
              Nothing in the evidence store has been verified or ratified yet, so this check
              would grade almost every claim as &ldquo;we hold no evidence&rdquo;.
            </p>
            <p className="mt-1 leading-relaxed">
              You would pay {calls.toLocaleString()} model calls for a report with roughly 0%
              coverage. Claims still get extracted and stored, which is useful for checking
              the extractor itself — but it will not tell you whether a model is right.{" "}
              <Link to="/evidence/studies" className="font-semibold underline">
                Verify studies
              </Link>{" "}
              or{" "}
              <Link to="/evidence/governance" className="font-semibold underline">
                ratify a network
              </Link>{" "}
              first.
            </p>
            <label className="mt-2.5 flex cursor-pointer items-center gap-2 text-xs font-semibold">
              <input
                type="checkbox"
                checked={acknowledged}
                onChange={(e) => setAcknowledged(e.target.checked)}
                className="h-3.5 w-3.5 accent-amber-600"
              />
              Check it anyway — I want to see the extracted claims.
            </label>
          </div>
        </div>
      )}

      {error && (
        <div className="mt-3 rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-800">
          {error}
        </div>
      )}

      {result && (
        <div className="mt-3 rounded-xl border border-line bg-slate-50 p-3 text-sm text-ink">
          <strong>{result.evaluated.toLocaleString()}</strong> of{" "}
          {result.responses.toLocaleString()} answers read,{" "}
          <strong>{result.finding_count.toLocaleString()}</strong> findings recorded
          {result.failed > 0 && (
            <>
              {" "}
              · {result.failed.toLocaleString()} answer
              {result.failed === 1 ? "" : "s"} could not be read
            </>
          )}
          .
          {result.finding_count === 0 && result.evaluated > 0 && (
            <span className="text-ink-light">
              {" "}
              The answers carried no claim this system knows how to check.
            </span>
          )}
        </div>
      )}
    </Card>
  );
}

function runLabel(run: Run, answers: number): string {
  // Time included, not just the date: several runs a day is normal, and two options reading
  // "Jul 26 · ADHOC · 42 answers" would make the picker a guess.
  const when = run.started_at
    ? new Date(run.started_at).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "unknown start";
  return `${when} · ${run.trigger} · ${answers} answer${answers === 1 ? "" : "s"} · ${run.status}`;
}

function ReadinessPill({
  label,
  count,
  help,
}: {
  label: string;
  count: number;
  help: string;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold ${
        count ? "bg-teal-100 text-teal-800" : "bg-amber-100 text-amber-800"
      }`}
    >
      {count.toLocaleString()} {label}
      <InfoTooltip content={help} />
    </span>
  );
}

/** One sentence that says what the numbers mean, including when they mean very little. */
function HeadlineBanner({ overall }: { overall: AlignmentRollup }) {
  const thin = (overall.coverage ?? 0) < THIN_COVERAGE;
  const uncheckable = overall.claim_count - overall.checkable_count;

  if (overall.safety_contradictions > 0) {
    return (
      <div
        role="alert"
        className="mb-6 flex items-start gap-3 rounded-xl border-2 border-red-300 bg-red-50 p-4"
      >
        <ShieldAlert size={18} className="mt-0.5 shrink-0 text-red-600" strokeWidth={2.2} />
        <div className="text-sm text-red-900">
          <p className="font-bold">
            {overall.safety_contradictions} safety contradiction
            {overall.safety_contradictions !== 1 ? "s" : ""} detected.
          </p>
          <p className="mt-1 leading-relaxed">
            A monitored model stated something about a boxed warning that our verified label
            contradicts. This outranks every other finding on this page.
          </p>
        </div>
      </div>
    );
  }

  if (thin) {
    return (
      <div
        role="alert"
        className="mb-6 flex items-start gap-3 rounded-xl border-2 border-amber-300 bg-amber-50 p-4"
      >
        <AlertTriangle size={18} className="mt-0.5 shrink-0 text-amber-600" strokeWidth={2.2} />
        <div className="text-sm text-amber-900">
          <p className="font-bold">
            Coverage is {pct(overall.coverage)} — the alignment score describes too few
            claims to be read as a measure of model quality.
          </p>
          <p className="mt-1 leading-relaxed">
            {uncheckable.toLocaleString()} of {overall.claim_count.toLocaleString()} extracted
            claims could not be checked because we hold no verified evidence able to answer
            them. That is a statement about our corpus, not about the models — which is why
            those claims are excluded from the score rather than counted against it.
          </p>
        </div>
      </div>
    );
  }

  return (
    <AnimatedCard className="mb-6">
      <div className="rounded-2xl border border-line bg-brand-surface p-5">
        <div className="mb-1 text-[11px] font-bold uppercase tracking-wide text-brand-dark">
          The big picture
        </div>
        <p className="text-sm leading-relaxed text-ink">
          Of <strong>{overall.claim_count.toLocaleString()}</strong> claims extracted,{" "}
          <strong>{overall.checkable_count.toLocaleString()}</strong> could be checked against
          our evidence ({pct(overall.coverage)} coverage). Those score{" "}
          <strong>{pct(overall.alignment_score)}</strong> aligned, with{" "}
          <strong>{overall.adverse_count.toLocaleString()}</strong> adverse finding
          {overall.adverse_count !== 1 ? "s" : ""}.
        </p>
      </div>
    </AnimatedCard>
  );
}

function ClassificationBadge({ value }: { value: string | null }) {
  if (!value) return <span className="text-xs font-medium text-ink-light">N/A</span>;
  const meta = CLASSIFICATION_META[value];
  return (
    <span
      className={`rounded-full px-2.5 py-1 text-xs font-semibold ${meta?.cls ?? "bg-slate-100 text-ink-light"}`}
    >
      {meta?.label ?? value.replace(/_/g, " ").toLowerCase()}
    </span>
  );
}

function ClassificationBreakdown({ counts }: { counts: Record<string, number> }) {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  if (!entries.length) return <p className="text-sm text-ink-light">Nothing evaluated yet.</p>;
  const max = Math.max(...entries.map(([, n]) => n));
  return (
    <div className="space-y-2">
      {entries.map(([key, n]) => (
        <div key={key} className="flex items-center gap-3">
          <span className="w-44 shrink-0">
            <ClassificationBadge value={key} />
          </span>
          <div className="h-2 flex-1 overflow-hidden rounded-full bg-slate-100">
            <div
              className="h-full rounded-full bg-brand-light"
              style={{ width: `${max ? (n / max) * 100 : 0}%` }}
            />
          </div>
          <span className="w-10 shrink-0 text-right text-xs font-bold tabular-nums text-ink">
            {n}
          </span>
        </div>
      ))}
    </div>
  );
}

function CertaintyBreakdown({ counts }: { counts: Record<string, number> }) {
  const entries = Object.entries(counts);
  if (!entries.length) {
    return (
      <p className="text-sm text-ink-light">
        No claim carried an interval to calibrate against.
      </p>
    );
  }
  return (
    <div className="space-y-2.5">
      {["OVERCLAIMED", "UNDERCLAIMED", "CALIBRATED"].map((key) => {
        const meta = CERTAINTY_META[key];
        const Icon = meta.icon;
        const n = counts[key] ?? 0;
        return (
          <div key={key} className="flex items-center gap-3">
            <span
              className={`inline-flex w-36 shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold ${meta.cls}`}
            >
              <Icon size={13} /> {meta.label}
            </span>
            <InfoTooltip content={meta.help} />
            <span className="ml-auto text-lg font-bold tabular-nums text-ink">{n}</span>
          </div>
        );
      })}
    </div>
  );
}

function RollupTable({
  rows,
  firstColumn,
}: {
  rows: [string, AlignmentRollup][];
  firstColumn: string;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-line text-left">
            <th className="pb-2 text-xs font-bold uppercase tracking-wide text-ink-light">
              {firstColumn}
            </th>
            <th className="pb-2 text-right text-xs font-bold uppercase tracking-wide text-ink-light">
              Claims
            </th>
            <th className="pb-2 text-right text-xs font-bold uppercase tracking-wide text-ink-light">
              Coverage
            </th>
            <th className="pb-2 text-right text-xs font-bold uppercase tracking-wide text-ink-light">
              Alignment
            </th>
            <th className="pb-2 text-right text-xs font-bold uppercase tracking-wide text-ink-light">
              Adverse
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([name, r]) => {
            const thin = (r.coverage ?? 0) < THIN_COVERAGE;
            return (
              <tr key={name} className="border-b border-line/60 last:border-0">
                <td className="py-2 font-semibold text-ink">
                  {name.replace(/_/g, " ").toLowerCase()}
                </td>
                <td className="py-2 text-right tabular-nums text-ink-light">
                  {r.claim_count}
                </td>
                <td
                  className={`py-2 text-right tabular-nums font-semibold ${thin ? "text-amber-700" : "text-ink"}`}
                  title={thin ? "Too few checkable claims to read the alignment score" : undefined}
                >
                  {pct(r.coverage)}
                </td>
                <td className="py-2 text-right tabular-nums font-bold text-ink">
                  {thin ? (
                    <span className="text-ink-light" title="Unmeasured — coverage too low">
                      —
                    </span>
                  ) : (
                    pct(r.alignment_score)
                  )}
                </td>
                <td
                  className={`py-2 text-right tabular-nums font-bold ${r.adverse_count ? "text-red-700" : "text-ink-light"}`}
                >
                  {r.adverse_count}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function TextFilter({
  label,
  value,
  placeholder,
  onChange,
}: {
  label: string;
  value: string;
  placeholder: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <label className="pl-0.5 text-xs font-bold uppercase tracking-widest text-ink">
        {label}
      </label>
      <input
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="rounded-lg border border-line bg-canvas-card px-3 py-2 text-sm font-medium text-ink shadow-sm transition-colors focus:border-brand-light focus:outline-none focus:ring-2 focus:ring-brand-light/40"
      />
    </div>
  );
}
