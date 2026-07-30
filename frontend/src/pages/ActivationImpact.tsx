import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  Clock,
  ExternalLink,
  FlaskConical,
  Minus,
  RefreshCw,
  Rocket,
  Send,
  Target,
  TrendingDown,
  TrendingUp,
  Users,
} from "lucide-react";
import {
  api,
  type Intervention,
  type InterventionTimeline,
  type MeasurementSnapshot,
  type MetricChange,
} from "../api/client";
import { Card, EmptyState, InfoTooltip, PageHeader, Spinner, Stat } from "../components/ui";
import { PlacementGuidancePanel } from "../components/PlacementGuidance";

/* ------------------------------------------------------------------ badges */
const STATUS_META: Record<string, { label: string; cls: string }> = {
  PROPOSED: { label: "Proposed", cls: "bg-slate-200 text-ink" },
  IN_PROGRESS: { label: "In progress", cls: "bg-sky-100 text-sky-900" },
  PUBLISHED: { label: "Published", cls: "bg-indigo-100 text-indigo-800" },
  MEASURING: { label: "Measuring", cls: "bg-amber-100 text-amber-900" },
  COMPLETED: { label: "Completed", cls: "bg-teal-100 text-teal-900" },
  DEFERRED: { label: "Deferred", cls: "bg-slate-100 text-ink-light" },
  CANCELLED: { label: "Cancelled", cls: "bg-red-100 text-red-700" },
};

const MEASUREMENT_META: Record<string, { label: string; cls: string }> = {
  PLANNED: { label: "Planned", cls: "bg-slate-100 text-ink-light" },
  BASELINE_RUNNING: { label: "Baseline running", cls: "bg-sky-100 text-sky-900" },
  MEASURING: { label: "Awaiting post window", cls: "bg-amber-100 text-amber-900" },
  POST_RUNNING: { label: "Post-measurement running", cls: "bg-sky-100 text-sky-900" },
  DONE: { label: "Measured", cls: "bg-teal-100 text-teal-900" },
  ERROR: { label: "Error", cls: "bg-red-100 text-red-700" },
};

const OUTCOME_META: Record<string, { label: string; cls: string; icon: typeof TrendingUp }> = {
  IMPROVED: { label: "Improved", cls: "bg-teal-100 text-teal-900", icon: TrendingUp },
  WORSENED: { label: "Worsened", cls: "bg-red-100 text-red-700", icon: TrendingDown },
  NO_CLEAR_CHANGE: { label: "No clear change", cls: "bg-slate-200 text-ink", icon: Minus },
  INCONCLUSIVE: { label: "Inconclusive", cls: "bg-amber-100 text-amber-900", icon: AlertTriangle },
};

const CONFIDENCE_CLS: Record<string, string> = {
  HIGH: "bg-teal-100 text-teal-900",
  MEDIUM: "bg-amber-100 text-amber-900",
  LOW: "bg-slate-200 text-ink",
};

const CONFOUNDER_LABELS: Record<string, string> = {
  MODEL_VERSION_CHANGED: "Model version changed",
  MODEL_RELEASE_IN_WINDOW: "Model release in window",
  SCORER_VERSION_CHANGED: "Scorer version changed",
  PROMPT_VERSION_CHANGED: "Prompt version changed",
  LOW_SAMPLE: "Small sample",
  HIGH_VARIABILITY: "High variability",
};

function Pill({ meta, fallback }: { meta?: { label: string; cls: string }; fallback: string }) {
  const m = meta ?? { label: fallback, cls: "bg-slate-100 text-ink-light" };
  return <span className={`px-2.5 py-1 rounded-full text-xs font-bold ${m.cls}`}>{m.label}</span>;
}

/* ------------------------------------------------------------------ helpers */
function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "—" : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function pctText(x: number | null | undefined): string {
  return x === null || x === undefined ? "—" : `${(x * 100).toFixed(0)}%`;
}

function changeText(c: MetricChange): string {
  if (c.kind === "rate") {
    const pp = c.change_pp ?? c.change * 100;
    return `${pp >= 0 ? "+" : ""}${pp.toFixed(1)} pp`;
  }
  return `${c.change >= 0 ? "+" : ""}${c.change.toFixed(2)}`;
}

function valueText(v: number, kind: string): string {
  return kind === "rate" ? `${(v * 100).toFixed(0)}%` : v.toFixed(2);
}

/* ================================================================== LIST */
function ListView({ onOpen }: { onOpen: (id: string) => void }) {
  const [items, setItems] = useState<Intervention[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    api.interventions().then((r) => { if (alive) { setItems(r.items); setLoading(false); } })
      .catch(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, []);

  const counts = useMemo(() => {
    const c = { open: 0, measuring: 0, completed: 0, improved: 0 };
    for (const i of items) {
      if (["PROPOSED", "IN_PROGRESS", "PUBLISHED"].includes(i.status)) c.open += 1;
      if (i.status === "MEASURING") c.measuring += 1;
      if (i.status === "COMPLETED") c.completed += 1;
      if (i.outcome_status === "IMPROVED") c.improved += 1;
    }
    return c;
  }, [items]);

  if (loading) return <div className="flex justify-center py-16"><Spinner size={26} /></div>;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Stat label="Open" value={counts.open} icon={<Rocket size={16} />} sub="Proposed, in progress, published" />
        <Stat label="Measuring" value={counts.measuring} icon={<FlaskConical size={16} />} sub="Awaiting post-publication result" />
        <Stat label="Completed" value={counts.completed} icon={<CheckCircle2 size={16} />} />
        <Stat label="Improved" value={counts.improved} icon={<TrendingUp size={16} />} sub="AI-answer KPI rose after publish" />
      </div>

      {items.length === 0 ? (
        <Card>
          <EmptyState
            icon={<Rocket size={34} />}
            message='No interventions yet. Open GEO Interventions and click "Create intervention" on a recommendation.'
          />
        </Card>
      ) : (
        <Card>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs font-bold uppercase tracking-wide text-ink-light">
                  <th className="py-2.5 pr-3">Intervention</th>
                  <th className="px-3">Status</th>
                  <th className="px-3">Owner</th>
                  <th className="px-3">Due</th>
                  <th className="px-3">Published</th>
                  <th className="px-3">Measurement</th>
                  <th className="px-3">Outcome</th>
                </tr>
              </thead>
              <tbody>
                {items.map((i) => {
                  const outcome = i.outcome_status ? OUTCOME_META[i.outcome_status] : undefined;
                  return (
                    <tr
                      key={i.id}
                      onClick={() => onOpen(i.id)}
                      className="cursor-pointer border-b border-line/60 transition-colors hover:bg-slate-50"
                    >
                      <td className="py-3 pr-3">
                        <div className="font-bold text-ink line-clamp-1">{i.title}</div>
                        <div className="text-xs text-ink-light">
                          {[i.brand_focus, i.therapeutic_area, i.target_personas.join(", ")].filter(Boolean).join(" · ") || "—"}
                        </div>
                      </td>
                      <td className="px-3"><Pill meta={STATUS_META[i.status]} fallback={i.status} /></td>
                      <td className="px-3 text-ink">{i.owner_name || <span className="text-ink-light">Unassigned</span>}</td>
                      <td className="px-3 text-ink-light">{fmtDate(i.due_date)}</td>
                      <td className="px-3 text-ink-light">{fmtDate(i.publication_date)}</td>
                      <td className="px-3"><Pill meta={MEASUREMENT_META[i.measurement_status]} fallback={i.measurement_status} /></td>
                      <td className="px-3">{outcome ? <Pill meta={outcome} fallback={i.outcome_status!} /> : <span className="text-ink-light">—</span>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}

/* ================================================================== DETAIL */
function SnapshotMetrics({ snap }: { snap: MeasurementSnapshot | null }) {
  if (!snap) return <p className="text-sm text-ink-light">Not captured yet.</p>;
  if (snap.pending) return <p className="inline-flex items-center gap-2 text-sm text-ink-light"><Spinner size={14} /> Runs in progress ({snap.run_ids.length} run{snap.run_ids.length === 1 ? "" : "s"})…</p>;
  const m = snap.metrics;
  if (!m) return <p className="text-sm text-ink-light">No scored responses.</p>;
  return (
    <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-3">
      <div><span className="text-ink-light">Samples: </span><span className="font-bold text-ink">{snap.response_count}</span></div>
      <div><span className="text-ink-light">Consideration: </span><span className="font-bold text-ink">{pctText(m.consideration_rate)}</span></div>
      <div><span className="text-ink-light">Leading: </span><span className="font-bold text-ink">{pctText(m.leading_rate)}</span></div>
      <div><span className="text-ink-light">Missing: </span><span className="font-bold text-ink">{pctText(m.missing_rate)}</span></div>
      <div><span className="text-ink-light">Avg sentiment: </span><span className="font-bold text-ink">{m.avg_sentiment === null ? "—" : m.avg_sentiment.toFixed(2)}</span></div>
      <div><span className="text-ink-light">Consistency: </span><span className="font-bold text-ink">{pctText(m.response_consistency)}</span></div>
    </div>
  );
}

function ResultSection({ interv }: { interv: Intervention }) {
  const result = interv.result;
  const defs = interv.metric_defs ?? {};
  if (!result) {
    return (
      <p className="text-sm text-ink-light">
        No result yet. The before/after comparison appears once the post-publication measurement completes.
      </p>
    );
  }
  const outcome = result.outcome_status ? OUTCOME_META[result.outcome_status] : undefined;
  const OutcomeIcon = outcome?.icon ?? Minus;
  const changeEntries = Object.entries(result.metric_changes);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <span className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm font-bold ${outcome?.cls ?? "bg-slate-100 text-ink-light"}`}>
          <OutcomeIcon size={15} /> {outcome?.label ?? result.outcome_status ?? "—"}
        </span>
        {result.confidence && (
          <span className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-bold ${CONFIDENCE_CLS[result.confidence]}`}>
            {result.confidence} confidence
            <InfoTooltip content="Confidence is capped when a confounder (model/scorer/prompt change) is present or the sample is small. Single-arm comparison: association, not proven causation." />
          </span>
        )}
      </div>

      {result.interpretation && <p className="text-sm font-medium text-ink">{result.interpretation}</p>}

      {changeEntries.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs font-bold uppercase tracking-wide text-ink-light">
                <th className="py-2 pr-3">Metric</th>
                <th className="px-3">Baseline</th>
                <th className="px-3">Post</th>
                <th className="px-3">Change</th>
              </tr>
            </thead>
            <tbody>
              {changeEntries.map(([key, c]) => {
                const isPrimary = key === interv.primary_metric;
                const up = c.change > 0;
                const down = c.change < 0;
                return (
                  <tr key={key} className={`border-b border-line/60 ${isPrimary ? "bg-brand-surface/40" : ""}`}>
                    <td className="py-2 pr-3 font-bold text-ink">
                      {defs[key]?.label ?? c.label}
                      {isPrimary && <span className="ml-2 rounded bg-brand-dark px-1.5 py-0.5 text-[10px] font-bold uppercase text-white">Primary</span>}
                    </td>
                    <td className="px-3 text-ink">{valueText(c.baseline, c.kind)}</td>
                    <td className="px-3 text-ink">{valueText(c.post, c.kind)}</td>
                    <td className={`px-3 font-bold ${up ? "text-teal-700" : down ? "text-red-600" : "text-ink-light"}`}>{changeText(c)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {result.confounders.length > 0 && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-3.5">
          <p className="mb-1.5 inline-flex items-center gap-1.5 text-xs font-extrabold uppercase tracking-wide text-amber-900">
            <AlertTriangle size={14} /> Confounders ({result.confounders.length})
          </p>
          <ul className="space-y-1">
            {result.confounders.map((c, i) => (
              <li key={i} className="text-sm text-amber-900">
                <span className="font-bold">{CONFOUNDER_LABELS[c.code] ?? c.code}:</span> {c.detail}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function DetailView({ id, onBack }: { id: string; onBack: () => void }) {
  const [interv, setInterv] = useState<Intervention | null>(null);
  const [timeline, setTimeline] = useState<InterventionTimeline | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [pubUrl, setPubUrl] = useState("");
  const [owner, setOwner] = useState("");
  const [reviewer, setReviewer] = useState("");

  const load = useCallback(async () => {
    try {
      const [d, t] = await Promise.all([api.intervention(id), api.interventionTimeline(id)]);
      setInterv(d);
      setTimeline(t);
      setOwner(d.owner_name ?? "");
      setReviewer(d.reviewer_name ?? "");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to load intervention");
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => { setLoading(true); void load(); }, [load]);

  // Auto-refresh while measurement runs are in flight so results appear without a manual reload.
  const activeMeasurement = interv && ["BASELINE_RUNNING", "POST_RUNNING"].includes(interv.measurement_status);
  useEffect(() => {
    if (!activeMeasurement) return;
    const t = setInterval(() => { void load(); }, 5000);
    return () => clearInterval(t);
  }, [activeMeasurement, load]);

  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    setErr(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Action failed");
    } finally {
      setBusy(false);
    }
  }

  if (loading) return <div className="flex justify-center py-16"><Spinner size={26} /></div>;
  if (!interv) return (
    <Card><EmptyState icon={<AlertTriangle size={30} />} message={err ?? "Intervention not found."} /></Card>
  );

  const i = interv;
  const canPublish = ["PROPOSED", "IN_PROGRESS", "DEFERRED"].includes(i.status);
  const canMeasureNow = ["BASELINE_RUNNING", "MEASURING", "POST_RUNNING"].includes(i.measurement_status);
  const transitions: Record<string, string[]> = {
    PROPOSED: ["IN_PROGRESS", "DEFERRED", "CANCELLED"],
    IN_PROGRESS: ["PROPOSED", "DEFERRED", "CANCELLED"],
    DEFERRED: ["PROPOSED", "IN_PROGRESS", "CANCELLED"],
  };
  const allowed = transitions[i.status] ?? [];

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3">
        <button onClick={onBack} className="inline-flex items-center gap-1.5 text-sm font-bold text-ink-light hover:text-ink">
          <ArrowLeft size={16} /> All interventions
        </button>
      </div>

      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-xl font-extrabold text-ink">{i.title}</h2>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <Pill meta={STATUS_META[i.status]} fallback={i.status} />
            <Pill meta={MEASUREMENT_META[i.measurement_status]} fallback={i.measurement_status} />
            {i.outcome_status && <Pill meta={OUTCOME_META[i.outcome_status]} fallback={i.outcome_status} />}
          </div>
        </div>
        <button onClick={() => void load()} disabled={busy} className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-canvas-card px-3 py-1.5 text-sm font-bold text-ink shadow-sm hover:bg-slate-50 disabled:opacity-50">
          <RefreshCw size={15} className={busy ? "animate-spin" : ""} /> Refresh
        </button>
      </div>

      {err && (
        <div className="flex items-center gap-2 rounded-xl border border-red-100 bg-red-50 p-3 text-sm font-medium text-red-700">
          <AlertTriangle size={16} /> {err}
        </div>
      )}

      {/* 1. Recommendation & evidence */}
      <Card title="Recommendation & evidence">
        {i.description && <p className="text-sm font-medium text-ink">{i.description}</p>}
        {i.evidence && (
          <div className="mt-3 grid grid-cols-1 gap-2 rounded-xl border border-line bg-slate-50 p-3.5 text-sm sm:grid-cols-2">
            {i.evidence.content_type && <div><span className="text-ink-light">Content type: </span><span className="font-bold text-ink">{i.evidence.content_type}</span></div>}
            {i.evidence.competitive_position && <div><span className="text-ink-light">AI position: </span><span className="font-bold text-ink">{i.evidence.competitive_position}</span></div>}
            {i.evidence.outperforming_competitor && <div><span className="text-ink-light">Outperformed by: </span><span className="font-bold text-ink">{i.evidence.outperforming_competitor}</span></div>}
            {i.evidence.search_volume != null && <div><span className="text-ink-light">Monthly searches (proxy): </span><span className="font-bold text-ink">{Number(i.evidence.search_volume).toLocaleString()}</span></div>}
            {i.evidence.rationale && <div className="sm:col-span-2"><span className="text-ink-light">Why: </span><span className="text-ink">{i.evidence.rationale}</span></div>}
          </div>
        )}
        <PlacementGuidancePanel placement={i.evidence?.placement} className="mt-3" />
      </Card>

      {/* 2. Ownership & status */}
      <Card title="Ownership & status">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="flex flex-col gap-1 text-sm">
            <span className="font-bold text-ink-light">Owner</span>
            <input value={owner} onChange={(e) => setOwner(e.target.value)} placeholder="Free text (no login in v1)"
              className="rounded-lg border border-line bg-canvas-card px-3 py-2 text-ink" />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="font-bold text-ink-light">Reviewer</span>
            <input value={reviewer} onChange={(e) => setReviewer(e.target.value)} placeholder="Free text"
              className="rounded-lg border border-line bg-canvas-card px-3 py-2 text-ink" />
          </label>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            onClick={() => act(() => api.updateIntervention(i.id, { owner_name: owner, reviewer_name: reviewer }))}
            disabled={busy}
            className="inline-flex items-center gap-1.5 rounded-lg bg-brand-dark px-3 py-1.5 text-sm font-bold text-white shadow-sm hover:bg-brand disabled:opacity-50"
          >
            <Users size={15} /> Save owners
          </button>
          {allowed.map((s) => (
            <button
              key={s}
              onClick={() => act(() => api.transitionIntervention(i.id, { to_status: s as any, actor_name: owner || undefined }))}
              disabled={busy}
              className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-canvas-card px-3 py-1.5 text-sm font-bold text-ink shadow-sm hover:bg-slate-50 disabled:opacity-50"
            >
              {STATUS_META[s]?.label ?? s}
            </button>
          ))}
        </div>
      </Card>

      {/* 3. Publication */}
      <Card title="Publication">
        {i.publication_url ? (
          <div className="space-y-1 text-sm">
            <div className="flex items-center gap-2">
              <span className="text-ink-light">Published:</span>
              <a href={i.publication_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 font-bold text-brand-dark hover:underline">
                {i.publication_url} <ExternalLink size={13} />
              </a>
            </div>
            <div><span className="text-ink-light">Date: </span><span className="font-bold text-ink">{fmtDate(i.publication_date)}</span></div>
            {i.post_due_at && <div><span className="text-ink-light">Post-measurement due: </span><span className="font-bold text-ink">{fmtDate(i.post_due_at)}</span></div>}
          </div>
        ) : canPublish ? (
          <div className="space-y-3">
            <p className="text-sm text-ink-light">
              Record the published asset URL. This freezes the measurement cohort and launches the official pre-publication baseline runs.
            </p>
            <div className="flex flex-wrap items-end gap-2">
              <label className="flex flex-1 flex-col gap-1 text-sm">
                <span className="font-bold text-ink-light">Publication URL</span>
                <input value={pubUrl} onChange={(e) => setPubUrl(e.target.value)} placeholder="https://…"
                  className="min-w-[240px] rounded-lg border border-line bg-canvas-card px-3 py-2 text-ink" />
              </label>
              <button
                onClick={() => act(() => api.publishIntervention(i.id, { publication_url: pubUrl, actor_name: owner || undefined }))}
                disabled={busy || !pubUrl.trim()}
                className="inline-flex items-center gap-1.5 rounded-lg bg-brand-dark px-4 py-2 text-sm font-bold text-white shadow-sm hover:bg-brand disabled:opacity-50"
              >
                <Send size={15} /> Publish & baseline
              </button>
            </div>
          </div>
        ) : (
          <p className="text-sm text-ink-light">Publication is recorded once the intervention is published.</p>
        )}
      </Card>

      {/* 4. Measurement setup */}
      <Card title="Measurement setup">
        <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
          <div><span className="text-ink-light">Primary KPI: </span><span className="font-bold text-ink">{i.metric_defs?.[i.primary_metric]?.label ?? i.primary_metric}</span></div>
          <div><span className="text-ink-light">Target questions: </span><span className="font-bold text-ink">{i.target_question_ids.length}</span></div>
          <div className="inline-flex items-center gap-1"><span className="text-ink-light">Platforms: </span><span className="font-bold text-ink">{i.target_models.length ? i.target_models.join(", ") : "All enabled"}</span></div>
          <div><span className="text-ink-light">Personas: </span><span className="font-bold text-ink">{i.target_personas.join(", ") || "—"}</span></div>
          <div className="inline-flex items-center gap-1"><span className="text-ink-light">Repeated samples: </span><span className="font-bold text-ink">{i.repetitions_per_question}</span>
            <InfoTooltip content="Each measurement launches this many runs of the cohort, so every question is re-asked N times per platform. More samples = more credible before/after." /></div>
          <div><span className="text-ink-light">Adoption wait: </span><span className="font-bold text-ink">{i.measurement_wait_days} days</span></div>
        </div>

        <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-3">
          <div className="rounded-xl border border-line bg-slate-50 p-3.5">
            <p className="mb-1.5 text-xs font-extrabold uppercase tracking-wide text-ink-light">Discovery baseline</p>
            <SnapshotMetrics snap={i.snapshots?.discovery ?? null} />
          </div>
          <div className="rounded-xl border border-line bg-slate-50 p-3.5">
            <p className="mb-1.5 text-xs font-extrabold uppercase tracking-wide text-ink-light">Official baseline</p>
            <SnapshotMetrics snap={i.snapshots?.official_baseline ?? null} />
          </div>
          <div className="rounded-xl border border-line bg-slate-50 p-3.5">
            <p className="mb-1.5 text-xs font-extrabold uppercase tracking-wide text-ink-light">Post-publication</p>
            <SnapshotMetrics snap={i.snapshots?.post ?? null} />
          </div>
        </div>

        {canMeasureNow && (
          <div className="mt-4 flex items-center gap-3">
            <button
              onClick={() => act(() => api.measureIntervention(i.id))}
              disabled={busy}
              className="inline-flex items-center gap-1.5 rounded-lg border border-brand-light/50 bg-brand-surface/60 px-3 py-1.5 text-sm font-bold text-brand-dark shadow-sm hover:bg-brand-surface disabled:opacity-50"
              title="Advance the measurement now instead of waiting for the scheduled sweep"
            >
              <FlaskConical size={15} /> Measure now
            </button>
            {activeMeasurement && <span className="inline-flex items-center gap-1.5 text-xs text-ink-light"><Clock size={13} /> Runs in progress — auto-refreshing…</span>}
          </div>
        )}
      </Card>

      {/* 5. Results & timeline */}
      <Card title="Results">
        <ResultSection interv={i} />
      </Card>

      <Card title="Timeline">
        {!timeline || timeline.items.length === 0 ? (
          <p className="text-sm text-ink-light">No events yet.</p>
        ) : (
          <ol className="space-y-2.5">
            {timeline.items.map((e) => (
              <li key={e.id} className="flex items-start gap-3 text-sm">
                <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-brand-light" />
                <div>
                  <span className="font-bold text-ink">{e.event_type.replace(/_/g, " ").toLowerCase()}</span>
                  {e.new_status && <span className="text-ink-light"> · {e.new_status}</span>}
                  {e.actor_name && <span className="text-ink-light"> · {e.actor_name}</span>}
                  <span className="text-ink-light"> · {fmtDate(e.created_at)}</span>
                  {e.notes && <div className="text-ink-light">{e.notes}</div>}
                </div>
              </li>
            ))}
          </ol>
        )}
      </Card>
    </div>
  );
}

/* ================================================================== PAGE */
export default function ActivationImpact() {
  const [params, setParams] = useSearchParams();
  const id = params.get("id");

  const open = (openId: string) => setParams({ id: openId });
  const back = () => setParams({});

  return (
    <div>
      <PageHeader
        title="Activation & Impact"
        subtitle="Turn a GEO recommendation into an owned, published intervention and measure the before/after change in AI answers."
        badge={
          <span className="inline-flex items-center gap-1.5 rounded-full bg-brand-surface px-3 py-1 text-xs font-bold text-brand-dark">
            <Target size={13} /> Single-arm, association not causation
          </span>
        }
      />
      {id ? <DetailView id={id} onBack={back} /> : <ListView onOpen={open} />}
    </div>
  );
}
