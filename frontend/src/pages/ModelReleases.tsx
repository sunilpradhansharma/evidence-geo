import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AnimatePresence, motion } from "framer-motion";
import {
  Bar,
  ComposedChart,
  CartesianGrid,
  Legend,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  AlertTriangle,
  ArrowRightLeft,
  Boxes,
  CalendarClock,
  CheckCircle2,
  GitCommitVertical,
  RefreshCw,
  ShieldCheck,
  TrendingUp,
  X,
} from "lucide-react";
import {
  api,
  CorrelationRatio,
  DriftTimeline,
  LiveVersion,
  ModelReleaseSource,
  ModelUpdateSyncStatus,
  ResponseDriftDetail,
  ResponseDriftItem,
  VersionImpact,
} from "../api/client";
import { Card, EmptyState, PageHeader, Select, Spinner, Stat } from "../components/ui";

const PLATFORMS = ["", "Claude", "Nova-Pro", "Llama", "Gemini", "GPT-4o", "EvidenceMD"];

function pct(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : `${Math.round(n * 100)}%`;
}

function signed(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n > 0 ? `+${n.toFixed(2)}` : n.toFixed(2);
}

/** Where an update event came from, ordered by trust for the UI badge. */
const SOURCE_META: Record<ModelReleaseSource, { label: string; cls: string; title: string }> = {
  api: { label: "Version-confirmed", cls: "bg-teal-100 text-teal-800", title: "A real version change seen in our own traffic (ground truth)." },
  changelog: { label: "Vendor changelog", cls: "bg-violet-100 text-violet-800", title: "Matched to the vendor's published changelog / release notes." },
  inferred: { label: "Inferred", cls: "bg-amber-100 text-amber-800", title: "Reverse-guessed from a spike of answer changes (no vendor version)." },
  auto: { label: "Inferred", cls: "bg-amber-100 text-amber-800", title: "Reverse-guessed from a spike of answer changes (no vendor version)." },
  seed: { label: "Known release", cls: "bg-slate-100 text-ink-light", title: "Curated known vendor release." },
  manual: { label: "Manual", cls: "bg-slate-100 text-ink-light", title: "Entered manually." },
};

function SourceBadge({ source }: { source: ModelReleaseSource }) {
  const m = SOURCE_META[source] ?? SOURCE_META.manual;
  return (
    <span title={m.title} className={`px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wide ${m.cls}`}>
      {m.label}
    </span>
  );
}

function ConfidenceBadge({ value }: { value: number | null }) {
  if (value === null || value === undefined) return <span className="text-xs text-ink-muted">—</span>;
  const strong = value >= 0.85;
  const mid = value >= 0.6;
  const cls = strong ? "bg-teal-100 text-teal-800" : mid ? "bg-violet-100 text-violet-800" : "bg-amber-100 text-amber-800";
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold ${cls}`} title="Attribution confidence">
      <ShieldCheck size={11} /> {Math.round(value * 100)}%
    </span>
  );
}

/* ================================================================== */
/*  BEFORE/AFTER DRIFT DRAWER                                          */
/* ================================================================== */
function DriftDrawer({ id, onClose }: { id: number; onClose: () => void }) {
  const [detail, setDetail] = useState<ResponseDriftDetail | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    api
      .responseDriftDetail(id)
      .then(setDetail)
      .catch(() => setDetail(null))
      .finally(() => setLoading(false));
  }, [id]);

  return (
    <AnimatePresence>
      <motion.div
        className="fixed inset-0 z-50 bg-black/30"
        initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
        onClick={onClose}
      >
        <motion.div
          className="absolute right-0 top-0 h-full w-full max-w-[720px] bg-slate-50 shadow-2xl overflow-y-auto"
          initial={{ x: 720 }} animate={{ x: 0 }} exit={{ x: 720 }}
          transition={{ type: "spring", damping: 30, stiffness: 300 }}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="sticky top-0 z-10 bg-brand-dark px-6 py-4 flex items-center justify-between">
            <div className="flex items-center gap-2 text-white">
              <ArrowRightLeft size={18} className="text-brand-light" />
              <span className="font-bold text-sm">Response drift — before vs after</span>
            </div>
            <button onClick={onClose} className="text-white/70 hover:text-white"><X size={20} /></button>
          </div>

          {loading || !detail ? (
            <div className="flex justify-center py-20"><Spinner size={28} /></div>
          ) : (
            <div className="p-6 space-y-5">
              <div>
                <div className="flex items-center gap-2 mb-1 flex-wrap">
                  <span className="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wide bg-slate-200 text-ink">{detail.llm_name}</span>
                  {detail.observed_date && <span className="text-xs text-ink-light font-medium">{detail.observed_date}</span>}
                  <span className="text-xs text-ink-light font-medium">· similarity {pct(detail.similarity_ratio)}</span>
                </div>
                {detail.question_text && <p className="text-sm font-semibold text-ink">{detail.question_text}</p>}
              </div>

              {detail.correlated_release_platform && (
                <div className="flex items-start gap-2 rounded-xl border border-violet-200 bg-violet-50 px-4 py-2.5 text-xs font-semibold text-violet-800">
                  <GitCommitVertical size={14} className="mt-0.5 shrink-0" />
                  <span>
                    Linked to a detected {detail.correlated_release_platform} update
                    {detail.correlated_release_date ? ` on ${detail.correlated_release_date}` : ""}.
                    {detail.correlated_release_notes ? ` ${detail.correlated_release_notes}` : ""}
                  </span>
                </div>
              )}

              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                <Card title="Before (previous answer)">
                  <p className="text-xs text-ink leading-relaxed whitespace-pre-wrap">{detail.previous_response_text || "No previous answer on record."}</p>
                </Card>
                <Card title="After (current answer)">
                  <p className="text-xs text-ink leading-relaxed whitespace-pre-wrap">{detail.current_response_text || "—"}</p>
                </Card>
              </div>

              {detail.diff_text && (
                <Card title="What changed">
                  <pre className="text-[11px] leading-relaxed overflow-x-auto whitespace-pre-wrap font-mono">
                    {detail.diff_text.split("\n").map((ln, i) => (
                      <div key={i} className={ln.startsWith("+") ? "text-teal-700" : ln.startsWith("-") ? "text-red-600" : "text-ink-light"}>{ln}</div>
                    ))}
                  </pre>
                </Card>
              )}

              <Link
                to={`/results?mode=compare&question_id=${encodeURIComponent(detail.question_id)}`}
                className="inline-flex items-center gap-1.5 text-xs font-bold text-brand-dark hover:underline"
              >
                Open in AI Response Review <ArrowRightLeft size={12} />
              </Link>
            </div>
          )}
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}

/* ================================================================== */
/*  MAIN PAGE                                                          */
/* ================================================================== */
export default function ModelReleases() {
  const [platform, setPlatform] = useState("");
  const [timeline, setTimeline] = useState<DriftTimeline | null>(null);
  const [ratio, setRatio] = useState<CorrelationRatio | null>(null);
  const [drifts, setDrifts] = useState<ResponseDriftItem[]>([]);
  const [versions, setVersions] = useState<LiveVersion[]>([]);
  const [impact, setImpact] = useState<VersionImpact[]>([]);
  const [syncStatus, setSyncStatus] = useState<ModelUpdateSyncStatus | null>(null);
  const [openDriftId, setOpenDriftId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [syncMsg, setSyncMsg] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    const p = platform || undefined;
    return Promise.all([
      api.driftTimeline(p).then(setTimeline).catch(() => setTimeline(null)),
      api.correlationRatio().then(setRatio).catch(() => setRatio(null)),
      api.responseDrifts(p).then(setDrifts).catch(() => setDrifts([])),
      api.liveVersions().then(setVersions).catch(() => setVersions([])),
      api.versionImpact(p).then(setImpact).catch(() => setImpact([])),
      api.modelUpdateSyncStatus().then(setSyncStatus).catch(() => setSyncStatus(null)),
    ]).finally(() => setLoading(false));
  }, [platform]);

  useEffect(() => {
    load();
  }, [load]);

  const runSync = useCallback(async () => {
    setSyncing(true);
    setSyncMsg(null);
    try {
      const res = await api.modelUpdateSync();
      const parts = [
        `${res.versions_observed} version(s) tracked`,
        `${res.version_transitions_created} new transition(s)`,
      ];
      if (res.changelog_sync_enabled) {
        parts.push(`${res.changelog_events_created + res.changelog_events_enriched} changelog match(es)`);
      }
      setSyncMsg(parts.join(" · "));
      await load();
    } catch {
      setSyncMsg("Sync failed — check the backend logs.");
    } finally {
      setSyncing(false);
    }
  }, [load]);

  // Build a date-indexed axis merging drift days + release days so markers line up.
  const driftDates = (timeline?.drifts ?? []).map((d) => d.date);
  const chartData = (timeline?.drifts ?? []).map((d) => ({
    date: d.date,
    material_drifts: d.material_drifts,
    correlated_drifts: d.correlated_drifts,
  }));
  // Aggregate release markers by date so multiple updates on one day render as a single
  // marker ("▲ Claude +2") instead of overlapping labels.
  const markersByDate = new Map<string, string[]>();
  (timeline?.releases ?? [])
    .filter((r) => driftDates.includes(r.date))
    .forEach((r) => {
      const list = markersByDate.get(r.date) ?? [];
      list.push(r.target_platform);
      markersByDate.set(r.date, list);
    });
  const releaseMarkers = Array.from(markersByDate.entries()).map(([date, platforms]) => ({
    date,
    label: platforms.length > 1 ? `▲ ${platforms[0]} +${platforms.length - 1}` : `▲ ${platforms[0]}`,
  }));

  return (
    <div className="space-y-8">
      <PageHeader
        title="AI Update Impact"
        subtitle="See whether big changes in AI answers line up with known updates to the AI tools, so you can tell an organic shift from one caused by a vendor update."
      />

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <Stat
          label="AI Response Drifts"
          value={ratio ? ratio.material_drifts : "N/A"}
          icon={<TrendingUp size={16} className="text-brand-light" />}
          tooltip="How many AI answers changed significantly compared with the previous check."
        />
        <Stat
          label="Linked to an AI Update"
          value={ratio ? ratio.correlated_drifts : "N/A"}
          icon={<GitCommitVertical size={16} className="text-violet-500" />}
          tooltip="Answer changes that happened close in time to a logged AI update: likely caused by it."
        />
        <Stat
          label="% Explained by AI Updates"
          value={ratio ? `${(ratio.correlation_ratio * 100).toFixed(0)}%` : "N/A"}
          sub={ratio ? `${ratio.unexplained_drifts} unexplained` : undefined}
          icon={<CalendarClock size={16} className="text-teal-500" />}
          tooltip="Share of answer changes that line up with a known AI update."
        />
      </div>

      <div className="flex items-end justify-between gap-4 flex-wrap">
        <Select
          label="AI Platform"
          value={platform}
          options={PLATFORMS}
          optionLabels={{ "": "All platforms" }}
          onChange={setPlatform}
          tooltip="Narrow the timeline and update list to a single AI platform."
        />
        <div className="flex items-center gap-3">
          {syncMsg && <span className="text-xs font-semibold text-ink-light">{syncMsg}</span>}
          <button
            onClick={runSync}
            disabled={syncing}
            title={
              syncStatus?.enabled
                ? "Refresh tracked versions and pull vendor changelogs now."
                : "Refresh tracked versions from our own traffic. (Vendor changelog sync is off — enable it in settings to also pull 'what changed'.)"
            }
            className="inline-flex items-center gap-1.5 rounded-lg bg-brand-dark px-3 py-2 text-xs font-bold text-white hover:bg-brand-dark/90 disabled:opacity-60"
          >
            <RefreshCw size={14} className={syncing ? "animate-spin" : ""} />
            {syncing ? "Syncing…" : "Sync vendor versions"}
          </button>
        </div>
      </div>

      {syncStatus && (
        <div className="flex items-start gap-2 rounded-xl border border-slate-200 bg-slate-50 px-4 py-2.5 text-xs font-medium text-ink-light">
          <CheckCircle2 size={14} className={`mt-0.5 shrink-0 ${syncStatus.enabled ? "text-teal-500" : "text-slate-400"}`} />
          <span>
            {syncStatus.enabled ? (
              <>Vendor changelog capture is <b className="text-teal-700">on</b>. Sources: {syncStatus.sources.map((s) => s.vendor).join(", ") || "none configured"}.</>
            ) : (
              <>Versions are tracked from our own traffic (ground truth). Vendor changelog capture is <b>off</b> — enable <code>model_update_sync_enabled</code> to also pull each vendor's "what changed".</>
            )}
          </span>
        </div>
      )}

      <Card title="AI Model Versions">
        <p className="mb-4 text-xs text-ink-light font-medium">
          The exact model version each AI platform is currently answering with, captured from the version stamp on every response.
        </p>
        {versions.length === 0 ? (
          <EmptyState message="No model versions observed yet. They appear once responses with a version stamp are recorded." />
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {versions.map((v) => (
              <div key={v.target_platform} className="rounded-xl border border-slate-200 bg-white px-4 py-3">
                <div className="flex items-center gap-2 mb-1">
                  <Boxes size={14} className="text-brand-light" />
                  <span className="font-bold text-sm text-ink">{v.target_platform}</span>
                </div>
                <div className="font-mono text-xs text-ink break-all">{v.current_version || "unknown"}</div>
                <div className="mt-1 text-[11px] text-ink-light font-medium">
                  {v.current_since ? `Since ${v.current_since.slice(0, 10)}` : "—"} · {v.versions_observed} version(s) seen · {v.total_responses} responses
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title="When AI Answers Changed vs. AI Updates">
        {loading ? (
          <div className="flex justify-center py-16"><Spinner /></div>
        ) : chartData.length === 0 ? (
          <EmptyState message="No significant answer changes recorded yet. These appear once the same question is checked again and its answer changes noticeably." />
        ) : (
          <>
            <ResponsiveContainer width="100%" height={340}>
              <ComposedChart data={chartData} margin={{ top: 16, right: 16, bottom: 8, left: -8 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} />
                <YAxis allowDecimals={false} tick={{ fontSize: 11 }} />
                <Tooltip />
                <Legend />
                <Bar dataKey="material_drifts" name="AI response drifts" fill="#0D4F4F" radius={[4, 4, 0, 0]} />
                <Bar dataKey="correlated_drifts" name="Linked to an AI update" fill="#8b5cf6" radius={[4, 4, 0, 0]} />
                {releaseMarkers.map((r) => (
                  <ReferenceLine
                    key={r.date}
                    x={r.date}
                    stroke="#8b5cf6"
                    strokeDasharray="4 3"
                    label={{ value: r.label, position: "top", fontSize: 10, fill: "#7c3aed" }}
                  />
                ))}
              </ComposedChart>
            </ResponsiveContainer>
            <p className="mt-2 text-xs text-ink-light font-medium">
              Dashed violet lines mark automatically detected AI updates. Violet bars are answer changes the system linked to an update that happened around the same time.
            </p>
          </>
        )}
      </Card>

      {impact.some((i) => i.is_high_impact) && (
        <div className="flex items-start gap-2 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm font-semibold text-amber-900">
          <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-500" />
          <span>
            {impact.filter((i) => i.is_high_impact).length} high-impact model update{impact.filter((i) => i.is_high_impact).length !== 1 ? "s" : ""} detected — a vendor version change materially moved our tracked answers. These are logged as alerts and included in the stakeholder digest.
          </span>
        </div>
      )}

      <Card title="Vendor Model Updates & Their Impact">
        <p className="mb-4 text-xs text-ink-light font-medium">
          Real version changes (captured from each platform's version stamp and, where available, its published changelog), with the exact impact on our tracked answers.
        </p>
        {impact.length === 0 ? (
          <EmptyState message="No model updates captured yet. Version changes appear here automatically; run 'Sync vendor versions' to also pull vendor changelogs." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[860px] text-sm">
              <thead>
                <tr className="border-b border-line text-left">
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">AI Platform</th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">Version / What changed</th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">Effective</th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">Provenance</th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">Confidence</th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest" title="Tracked answers that materially changed across this update">Answers changed</th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest" title="Net brand-sentiment shift across the update">Sentiment Δ</th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest" title="Competitive-position changes across the update">Position Δ</th>
                </tr>
              </thead>
              <tbody>
                {impact.map((r) => {
                  const delta = r.sentiment_delta;
                  const deltaCls = delta === null ? "text-ink-muted" : delta < 0 ? "text-red-600" : delta > 0 ? "text-teal-700" : "text-ink-light";
                  return (
                    <tr key={r.release_id} className={`border-b border-slate-100 align-top ${r.is_high_impact ? "bg-amber-50/60" : ""}`}>
                      <td className="py-3 font-bold text-ink">
                        <div className="flex items-center gap-1.5">
                          {r.is_high_impact && (
                            <span title="High-impact update (alerted + in digest)">
                              <AlertTriangle size={13} className="text-amber-500" />
                            </span>
                          )}
                          {r.target_platform}
                        </div>
                      </td>
                      <td className="py-3 max-w-sm">
                        {r.version && <div className="font-mono text-xs text-ink break-all">{r.version}</div>}
                        <div className="text-ink-light font-medium text-xs">{r.summary || (r.version ? "New version observed." : "—")}</div>
                        {r.url && (
                          <a href={r.url} target="_blank" rel="noreferrer" className="text-[11px] font-bold text-brand-dark hover:underline">
                            Vendor changelog ↗
                          </a>
                        )}
                      </td>
                      <td className="py-3 font-medium text-ink">{(r.effective_date || r.release_date)?.slice(0, 10)}</td>
                      <td className="py-3"><SourceBadge source={r.source} /></td>
                      <td className="py-3"><ConfidenceBadge value={r.confidence} /></td>
                      <td className="py-3 font-bold text-ink">
                        {r.questions_changed}
                        {r.drift_count > r.questions_changed ? <span className="text-ink-muted font-medium"> ({r.drift_count} drifts)</span> : null}
                      </td>
                      <td className={`py-3 font-bold ${deltaCls}`}>{signed(delta)}</td>
                      <td className="py-3 font-medium text-ink-light">{r.position_changes || "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Response Drifts">
        <p className="mb-4 text-xs text-ink-light font-medium">
          Each row is a question whose AI answer changed noticeably. Open it to see the exact before/after answer for that model.
        </p>
        {drifts.length === 0 ? (
          <EmptyState message="No response drifts recorded yet. They appear once the same question is asked again and the answer changes." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left">
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">Question</th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">AI Platform</th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">Observed</th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">Similarity</th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">Linked update</th>
                  <th className="pb-3"></th>
                </tr>
              </thead>
              <tbody>
                {drifts.map((d) => (
                  <tr key={d.id} className="border-b border-slate-100">
                    <td className="py-3 max-w-sm truncate font-medium text-ink">{d.question_text || d.question_id}</td>
                    <td className="py-3 font-bold text-ink">{d.llm_name}</td>
                    <td className="py-3 font-medium text-ink-light">{d.observed_date || "—"}</td>
                    <td className="py-3 font-medium text-ink-light">{pct(d.similarity_ratio)}</td>
                    <td className="py-3">
                      {d.correlated_release_platform ? (
                        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wide bg-violet-100 text-violet-700">
                          <GitCommitVertical size={11} /> {d.correlated_release_platform}
                        </span>
                      ) : (
                        <span className="text-xs text-ink-muted">—</span>
                      )}
                    </td>
                    <td className="py-3">
                      <div className="flex items-center gap-3">
                        <button onClick={() => setOpenDriftId(d.id)} className="inline-flex items-center gap-1 text-brand-dark hover:underline text-xs font-bold">
                          <ArrowRightLeft size={12} /> View responses
                        </button>
                        <Link to={`/results?mode=compare&question_id=${encodeURIComponent(d.question_id)}`} className="inline-flex items-center gap-1 text-ink-light hover:text-ink text-xs font-bold">
                          Compare <ArrowRightLeft size={12} />
                        </Link>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {openDriftId !== null && <DriftDrawer id={openDriftId} onClose={() => setOpenDriftId(null)} />}
    </div>
  );
}
