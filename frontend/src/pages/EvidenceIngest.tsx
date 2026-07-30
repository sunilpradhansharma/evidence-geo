import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  DownloadCloud,
  Eye,
  FlaskConical,
  Info,
  Pill,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";

import {
  api,
  type IngestJobStatus,
  type IngestOptions,
  type IngestStarted,
} from "../api/client";
import {
  Card,
  InfoTooltip,
  MultiSelect,
  PageHeader,
  Select,
  Spinner,
} from "../components/ui";
import IngestReport from "../components/IngestReport";

const POLL_MS = 2000;

/**
 * Evidence ingestion — the three corpus-growing routines, off the shell.
 *
 * All three already existed as CLI scripts, which meant growing the evidence corpus required
 * a shell inside the production container. What this page adds is not a button; it is the
 * **report the scripts print before they write anything**: screened-out studies with their
 * reasons, the three label buckets with the advice attached to each, extraction warnings, and
 * both topologies with the nodes a protocol window costs you. A surface that rendered a
 * spinner and a row count would delete the review step the dry run exists for.
 *
 * Two properties the page states rather than implies:
 *
 * - **Preview writes nothing** — including no audit row — and any report produced without a
 *   commit carries a PREVIEW badge, so it can never be misread as a completed ingest.
 * - **Nothing here verifies.** Ingested rows land EXTRACTED or MAPPED and every membership
 *   lands PROPOSED. Verification is one study at a time on the curator surface, which is the
 *   gate that actually unblocks output.
 */
export default function EvidenceIngest() {
  const [options, setOptions] = useState<IngestOptions | null>(null);
  const [job, setJob] = useState<IngestJobStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [disabled, setDisabled] = useState(false);
  const timer = useRef<number | null>(null);

  const poll = useCallback(async () => {
    try {
      const next = await api.evidenceIngestStatus();
      setJob(next);
      if (next.running) {
        timer.current = window.setTimeout(poll, POLL_MS);
      }
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    api
      .evidenceIngestOptions()
      .then(setOptions)
      // A 403 here is the settings flag, not a bug. Say which.
      .catch((e) => {
        setDisabled(true);
        setError(String(e));
      });
    // Resume polling on mount if a job is already in flight — the job lives in the backend,
    // not in this component, so a reload must not lose sight of it.
    poll();
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, [poll]);

  /** Fire a start, then take over the poll loop. Errors are the server's `detail`. */
  const start = async (fn: () => Promise<IngestStarted>) => {
    setError(null);
    try {
      await fn();
      // Optimistically mark running so every Run button disables before the first poll.
      setJob((prev) => ({
        ...(prev || {
          kind: null, mode: null, scope: null, started_at: null, finished_at: null,
          progress: null, report: null, error: null,
        }),
        running: true,
        report: null,
        error: null,
      }));
      if (timer.current) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(poll, 400);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const running = !!job?.running;

  return (
    <div>
      <PageHeader
        title="Ingest Evidence"
        subtitle="Grow the corpus without a shell in the container. Every form previews by default — a preview reads the live source and writes nothing at all."
      />

      {disabled ? (
        <Card className="border-2 border-amber-300 bg-amber-50">
          <div className="flex items-start gap-3">
            <ShieldAlert size={18} className="mt-0.5 shrink-0 text-amber-600" strokeWidth={2.2} />
            <div className="text-sm text-amber-900">
              <p className="font-bold">Ingestion from the UI is unavailable.</p>
              <p className="mt-1 leading-relaxed">
                {error}
              </p>
              <p className="mt-2 leading-relaxed">
                Either <code>EVIDENCE_INGESTION_API_ENABLED</code> is false or the backend has
                not been restarted since the setting was added. The CLI path still works:{" "}
                <code>python -m scripts.ingest_evidence --indication "…"</code>.
              </p>
            </div>
          </div>
        </Card>
      ) : (
        <>
          <Governance />

          {error && (
            <div
              role="alert"
              className="mb-5 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800"
            >
              {error}
            </div>
          )}

          {running && <RunBanner job={job!} />}

          <div className="space-y-5">
            <TrialsCard options={options} running={running} onRun={start} />
            <DrugFactsCard options={options} running={running} onRun={start} />
            <ReparseCard options={options} running={running} onRun={start} />
          </div>

          {job?.error && !running && (
            <div
              role="alert"
              className="mt-6 flex items-start gap-3 rounded-xl border-2 border-amber-300 bg-amber-50 p-4"
            >
              <AlertTriangle size={18} className="mt-0.5 shrink-0 text-amber-600" strokeWidth={2.2} />
              <div className="text-sm text-amber-900">
                <p className="font-bold">The run stopped and explained why.</p>
                <p className="mt-1 leading-relaxed">{job.error}</p>
              </div>
            </div>
          )}

          {job?.report && !running && (
            <div className="mt-6">
              <IngestReport report={job.report} scope={job.scope} />
            </div>
          )}
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  What this surface can and cannot do — stated, not implied          */
/* ------------------------------------------------------------------ */
function Governance() {
  return (
    <div className="mb-5 grid grid-cols-1 gap-4 lg:grid-cols-2">
      <div className="flex items-start gap-3 rounded-xl border border-line bg-brand-surface/50 p-4">
        <Eye size={18} className="mt-0.5 shrink-0 text-brand" strokeWidth={2.2} />
        <div className="text-sm text-ink">
          <p className="font-bold">Preview reads the source and writes nothing.</p>
          <p className="mt-1 leading-relaxed text-ink-light">
            Not even an audit row — a run that promises to write nothing must not write a row
            to say so. It still queries ClinicalTrials.gov / openFDA, because that is the only
            way to report what it <em>would</em> store, so a later commit re-harvests and can
            legitimately differ from the preview you read.
          </p>
        </div>
      </div>
      <div className="flex items-start gap-3 rounded-xl border-2 border-amber-300 bg-amber-50 p-4">
        <ShieldAlert size={18} className="mt-0.5 shrink-0 text-amber-600" strokeWidth={2.2} />
        <div className="text-sm text-amber-900">
          <p className="font-bold">Nothing here verifies anything.</p>
          <p className="mt-1 leading-relaxed">
            Studies land <code>EXTRACTED</code> or <code>MAPPED</code>, labels the same, and
            memberships land <code>PROPOSED</code> on a <code>DRAFT</code> network.
            Verification is one study at a time on{" "}
            <Link to="/evidence/studies" className="font-bold underline">
              Studies
            </Link>
            , and it is the gate that actually unblocks output — evidence gathering skips an
            unverified study even in exploratory mode.
          </p>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Live run banner                                                    */
/* ------------------------------------------------------------------ */
const PHASE_LABEL: Record<string, string> = {
  starting: "Starting",
  searching: "Searching the registry",
  fetching: "Fetching records",
  building: "Assembling the network",
  reparsing: "Re-reading retained documents",
  done: "Finishing",
};

function RunBanner({ job }: { job: IngestJobStatus }) {
  const [elapsed, setElapsed] = useState("");
  const progress = job.progress || {};
  const phase = String(progress.phase || "starting");

  useEffect(() => {
    if (!job.started_at) return;
    const started = new Date(job.started_at).getTime();
    const tick = () => {
      const secs = Math.max(0, Math.round((Date.now() - started) / 1000));
      const mins = Math.floor(secs / 60);
      setElapsed(mins ? `${mins}m ${secs % 60}s` : `${secs}s`);
    };
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [job.started_at]);

  // Whichever counter pair this kind of job actually has. Never invent a denominator: a bar
  // whose total is unknown is more honest as a phase label than as a guess.
  const pairs: [string, number, number][] = [];
  if (typeof progress.drugs_total === "number" && progress.drugs_total > 0) {
    pairs.push(["searches", progress.drugs_done || 0, progress.drugs_total]);
  }
  if (typeof progress.studies_total === "number" && progress.studies_total > 0) {
    pairs.push(["studies", progress.studies_done || 0, progress.studies_total]);
  }
  if (typeof progress.brands_total === "number" && progress.brands_total > 0) {
    pairs.push(["labels", progress.brands_done || 0, progress.brands_total]);
  }
  const lead = pairs[pairs.length - 1];
  const pct = lead ? Math.round((lead[1] / lead[2]) * 100) : null;

  return (
    <Card className="mb-5 border-2 border-brand-light/50">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <Spinner size={20} />
          <div>
            <div className="text-sm font-bold text-ink">
              {PHASE_LABEL[phase] || phase}
              <span className="ml-2 rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-bold uppercase text-ink-light">
                {job.kind} · {job.mode}
              </span>
            </div>
            <div className="mt-0.5 text-xs text-ink-light">
              {pairs.length
                ? pairs.map(([label, done, total]) => `${done} of ${total} ${label}`).join(" · ")
                : "Working…"}
              {progress.current_study ? ` · ${progress.current_study}` : ""}
              {progress.current_brand ? ` · ${progress.current_brand}` : ""}
            </div>
          </div>
        </div>
        <div className="text-right">
          <div className="text-lg font-bold tabular-nums text-ink">{elapsed}</div>
          <div className="text-[10px] font-bold uppercase tracking-wide text-ink-light">
            elapsed
          </div>
        </div>
      </div>
      {pct !== null && (
        <div className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
          <div
            className="h-full rounded-full bg-brand-light transition-all duration-500"
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
      <p className="mt-3 text-xs leading-relaxed text-ink-light">
        A full indication is minutes of throttled requests (~50/min). Nothing blocks the
        browser, and reloading this page picks the job back up.
      </p>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/*  Shared form furniture                                             */
/* ------------------------------------------------------------------ */
function PreviewToggle({
  commit,
  onChange,
  disabled,
}: {
  commit: boolean;
  onChange: (v: boolean) => void;
  disabled: boolean;
}) {
  return (
    <label className="flex cursor-pointer items-start gap-2.5">
      <input
        type="checkbox"
        checked={!commit}
        disabled={disabled}
        onChange={(e) => onChange(!e.target.checked)}
        className="mt-0.5 h-4 w-4 shrink-0 rounded border-slate-300 text-brand focus:ring-brand-light/40"
      />
      <span className="text-sm">
        <span className="font-bold text-ink">Preview only (write nothing)</span>
        <span className="mt-0.5 block text-xs leading-relaxed text-ink-light">
          {commit
            ? "OFF — this run will write to the corpus."
            : "A preview still spends the source's rate budget. Committing afterwards re-harvests, so the numbers can move."}
        </span>
      </span>
    </label>
  );
}

function RunButton({
  label,
  running,
  commit,
  onClick,
}: {
  label: string;
  running: boolean;
  commit: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={running}
      className={`flex items-center gap-2 rounded-xl px-4 py-2 text-sm font-bold text-white transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
        commit ? "bg-brand-dark hover:bg-brand" : "bg-brand hover:bg-brand-dark"
      }`}
    >
      <DownloadCloud size={15} strokeWidth={2.4} />
      {commit ? `${label} — commit` : `${label} — preview`}
    </button>
  );
}

function FieldNote({ children }: { children: React.ReactNode }) {
  return (
    <p className="flex items-start gap-1.5 text-xs leading-relaxed text-ink-light">
      <Info size={12} className="mt-0.5 shrink-0" />
      <span>{children}</span>
    </p>
  );
}

type Starter = (fn: () => Promise<IngestStarted>) => Promise<void>;

/* ------------------------------------------------------------------ */
/*  1. Trials + network                                                */
/* ------------------------------------------------------------------ */
function TrialsCard({
  options,
  running,
  onRun,
}: {
  options: IngestOptions | null;
  running: boolean;
  onRun: Starter;
}) {
  const [indication, setIndication] = useState("");
  const [drugs, setDrugs] = useState<string[]>([]);
  const [outcome, setOutcome] = useState("");
  const [protocol, setProtocol] = useState("");
  const [phase, setPhase] = useState("PRIMARY");
  const [stratum, setStratum] = useState("");
  const [limit, setLimit] = useState("");
  const [commit, setCommit] = useState(false);

  useEffect(() => {
    if (options && !indication && options.indications.length) {
      setIndication(options.indications[0]);
    }
  }, [options]); // eslint-disable-line react-hooks/exhaustive-deps

  // Scoped to the chosen indication, so the form cannot offer an outcome the route rejects.
  const outcomes = options?.outcomes_by_indication[indication] || [];
  useEffect(() => {
    if (outcome && !outcomes.includes(outcome)) setOutcome("");
  }, [indication]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <FlaskConical size={16} className="text-brand" strokeWidth={2.2} />
          Trials + network
          <InfoTooltip content="A search per drug, then each record fetched in full — a search payload is not guaranteed to carry a complete results section." />
        </span>
      }
    >
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <Select
          label="Indication"
          value={indication}
          options={options?.indications || []}
          onChange={setIndication}
        />
        <MultiSelect
          label="Drugs to search"
          values={drugs}
          options={options?.full_depth_drugs || []}
          onChange={setDrugs}
          placeholder="Full-depth drugs"
          tooltip="Discovery is one registry search per drug. Every full-depth drug in brands.yaml is included — untick the ones you want left out."
        />
        <Select
          label="Canonical outcome"
          value={outcome}
          options={["", ...outcomes]}
          optionLabels={{ "": "None — ingest only, no network" }}
          onChange={setOutcome}
          tooltip="One network is one analysable question. Without an outcome this ingests studies and builds nothing."
        />
        <Select
          label="Protocol"
          value={protocol}
          options={["", ...(options?.protocols || [])]}
          optionLabels={{ "": "None" }}
          onChange={setProtocol}
          tooltip="Its approved time window is REPORTED against the built topology, never applied to it."
        />
        <Select
          label="Treatment phase"
          value={phase}
          options={options?.treatment_phases || ["PRIMARY"]}
          onChange={setPhase}
          tooltip="Induction and maintenance populations are never poolable — maintenance cohorts are re-randomised induction responders."
        />
        <div className="flex flex-col">
          <label className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-ink-light">
            Population stratum
          </label>
          <input
            value={stratum}
            onChange={(e) => setStratum(e.target.value)}
            placeholder="e.g. BIO_NAIVE (optional)"
            className="rounded-xl border border-line bg-canvas-card px-3 py-2 text-sm font-medium text-ink transition-colors focus:border-brand-light focus:outline-none focus:ring-2 focus:ring-brand-light/40"
          />
        </div>
        <div className="flex flex-col">
          <label className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-ink-light">
            Limit studies
          </label>
          <input
            value={limit}
            onChange={(e) => setLimit(e.target.value.replace(/[^0-9]/g, ""))}
            placeholder="blank = all"
            inputMode="numeric"
            className="rounded-xl border border-line bg-canvas-card px-3 py-2 text-sm font-medium text-ink transition-colors focus:border-brand-light focus:outline-none focus:ring-2 focus:ring-brand-light/40"
          />
        </div>
      </div>

      <div className="mt-4 space-y-3 border-t border-line pt-4">
        <FieldNote>
          A study whose arm names a drug <em>class</em> or a care <em>strategy</em> is screened
          out whole, not arm by arm: keeping its drug arms while dropping the comparator would
          leave edges pointing at a node that is no longer there.
        </FieldNote>
        <PreviewToggle commit={commit} onChange={setCommit} disabled={running} />
        <RunButton
          label="Ingest trials"
          running={running}
          commit={commit}
          onClick={() =>
            onRun(() =>
              api.evidenceIngestTrials({
                indication,
                drugs: drugs.length ? drugs : undefined,
                outcome: outcome || null,
                protocol: protocol || null,
                phase,
                stratum: stratum.trim() || null,
                limit: limit ? Number(limit) : null,
                commit,
              }),
            )
          }
        />
      </div>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/*  2. Drug facts                                                      */
/* ------------------------------------------------------------------ */
function DrugFactsCard({
  options,
  running,
  onRun,
}: {
  options: IngestOptions | null;
  running: boolean;
  onRun: Starter;
}) {
  const [brands, setBrands] = useState<string[]>([]);
  const [commit, setCommit] = useState(false);

  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <Pill size={16} className="text-brand" strokeWidth={2.2} />
          Drug facts (openFDA labels)
          <InfoTooltip content="Independent of the NMA stack: these stay valuable for an indication whose network turns out to be disconnected." />
        </span>
      }
    >
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <MultiSelect
          label="Brands"
          values={brands}
          options={options?.full_depth_drugs || []}
          onChange={setBrands}
          placeholder="Full-depth drugs"
          tooltip="openFDA needs no key. A brand it has no label for is reported NOT_FOUND and the run continues."
        />
      </div>

      <div className="mt-4 space-y-3 border-t border-line pt-4">
        <FieldNote>
          A <strong>new label date supersedes</strong> the previous row rather than replacing
          it — the old row is still a true statement about what the label said then, and a
          claim graded against it last quarter has to stay explicable. An <em>older</em> label
          never supersedes a newer one.
        </FieldNote>
        <PreviewToggle commit={commit} onChange={setCommit} disabled={running} />
        <RunButton
          label="Fetch labels"
          running={running}
          commit={commit}
          onClick={() =>
            onRun(() =>
              api.evidenceIngestDrugFacts({
                brands: brands.length ? brands : undefined,
                commit,
              }),
            )
          }
        />
      </div>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/*  3. Re-parse                                                        */
/* ------------------------------------------------------------------ */
function ReparseCard({
  options,
  running,
  onRun,
}: {
  options: IngestOptions | null;
  running: boolean;
  onRun: Starter;
}) {
  const [indication, setIndication] = useState("");
  const [ids, setIds] = useState("");
  const [commit, setCommit] = useState(false);

  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <RefreshCw size={16} className="text-brand" strokeWidth={2.2} />
          Re-parse stored studies
          <InfoTooltip content="No network call: every byte re-read is already on disk, so a change is attributable to our parser rather than to the registry moving." />
        </span>
      }
    >
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Select
          label="Indication"
          value={indication}
          options={["", ...(options?.indications || [])]}
          optionLabels={{ "": "All indications" }}
          onChange={setIndication}
        />
        <div className="flex flex-col">
          <label className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-ink-light">
            Or explicit study ids
          </label>
          <input
            value={ids}
            onChange={(e) => setIds(e.target.value)}
            placeholder="NCT03104400, NCT02349451"
            className="rounded-xl border border-line bg-canvas-card px-3 py-2 text-sm font-medium text-ink transition-colors focus:border-brand-light focus:outline-none focus:ring-2 focus:ring-brand-light/40"
          />
        </div>
      </div>

      <div className="mt-4 space-y-3 border-t border-line pt-4">
        <FieldNote>
          <strong>VERIFIED and REJECTED rows are skipped by design.</strong> A maintenance
          routine does not step around the verification lifecycle, so on a curated corpus a
          re-parse that reports mostly skips is the expected outcome, not a failure. A stale
          parse is a defect in our code — re-harvesting to fix it would move two variables at
          once and make the delta unattributable.
        </FieldNote>
        <PreviewToggle commit={commit} onChange={setCommit} disabled={running} />
        <RunButton
          label="Re-parse"
          running={running}
          commit={commit}
          onClick={() =>
            onRun(() =>
              api.evidenceIngestReparse({
                indication: indication || null,
                study_ids: ids
                  .split(/[,\s]+/)
                  .map((s) => s.trim())
                  .filter(Boolean),
                commit,
              }),
            )
          }
        />
      </div>
    </Card>
  );
}
