import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { AnimatePresence } from "framer-motion";
import {
  Ban,
  Check,
  ChevronDown,
  ChevronRight,
  GitBranch,
  Pencil,
  Play,
  RefreshCw,
  ShieldAlert,
  Sparkles,
  Wand2,
  X,
} from "lucide-react";
import {
  api,
  Question,
  Variation,
  VariationGroupDetail,
  VariationGroupResults,
  VariationGroupSummary,
} from "../api/client";
import {
  Card,
  EmptyState,
  PageHeader,
  PositionBadge,
  SentimentBadge,
  Spinner,
} from "../components/ui";
import { ResponseDetailDrawer } from "../components/ResponseDetailDrawer";

const STATUS_CLS: Record<string, string> = {
  DRAFT: "bg-amber-100 text-amber-800",
  APPROVED: "bg-teal-100 text-teal-800",
  REJECTED: "bg-slate-200 text-ink-light",
};

const FIELD_CLS =
  "w-full border border-line rounded-xl px-3 py-2 text-sm bg-canvas-card text-ink font-medium focus:outline-none focus:ring-2 focus:ring-brand-light/40 focus:border-brand-light transition-colors";

function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? "N/A" : `${Math.round(v * 100)}%`;
}

/* -------------------------------------------------------------------------- */
/*  Generate panel — self-service entry: pick an approved base question        */
/* -------------------------------------------------------------------------- */
function GeneratePanel({
  initialBase,
  onGenerated,
}: {
  initialBase?: string | null;
  onGenerated: (groupId: string) => void;
}) {
  const [bases, setBases] = useState<Question[]>([]);
  const [rowId, setRowId] = useState("");
  const [n, setN] = useState(4);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Pre-select the base question passed via ?base=<id> exactly once (still editable).
  const appliedBase = useRef(false);

  useEffect(() => {
    api
      .questions("?approval_status=APPROVED&limit=500")
      .then((qs) => setBases(qs.filter((q) => !q.is_variation)))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!initialBase || appliedBase.current || bases.length === 0) return;
    if (bases.some((q) => String(q.id) === String(initialBase))) {
      setRowId(String(initialBase));
      appliedBase.current = true;
    }
  }, [initialBase, bases]);

  const generate = async () => {
    if (!rowId || busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.generateVariations(Number(rowId), { n });
      onGenerated(r.group_id);
    } catch (e: any) {
      setError(e?.message || "Generation failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card accent>
      <div className="flex items-center gap-3 mb-4">
        <div className="w-10 h-10 rounded-xl bg-brand/10 flex items-center justify-center">
          <Wand2 size={20} className="text-brand" />
        </div>
        <div>
          <h3 className="text-sm font-extrabold text-ink">Generate phrasing variations</h3>
          <p className="text-xs text-ink-light font-medium">
            Claude rewrites an approved question into intent-preserving paraphrases. Drafts are
            staged for review. Nothing runs until you approve it.
          </p>
        </div>
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex-1 min-w-[280px]">
          <label className="text-[11px] font-bold text-ink-light uppercase tracking-widest">
            Base question
          </label>
          <select value={rowId} onChange={(e) => setRowId(e.target.value)} className={`${FIELD_CLS} mt-1`}>
            <option value="">Select an approved question…</option>
            {bases.map((q) => (
              <option key={q.id} value={q.id}>
                {q.question_text.length > 90 ? `${q.question_text.slice(0, 90)}\u2026` : q.question_text}
              </option>
            ))}
          </select>
        </div>
        <div className="w-28">
          <label className="text-[11px] font-bold text-ink-light uppercase tracking-widest"># Variations</label>
          <select value={n} onChange={(e) => setN(Number(e.target.value))} className={`${FIELD_CLS} mt-1`}>
            {[2, 3, 4, 5, 6].map((k) => (
              <option key={k} value={k}>{k}</option>
            ))}
          </select>
        </div>
        <button
          onClick={generate}
          disabled={!rowId || busy}
          className="inline-flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-bold bg-brand text-white hover:bg-brand-dark disabled:opacity-40 transition-colors"
        >
          {busy ? <Spinner size={16} /> : <Sparkles size={16} />}
          {busy ? "Generating\u2026" : "Generate drafts"}
        </button>
      </div>
      {error && <p className="text-xs font-bold text-red-600 mt-3">{error}</p>}
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/*  Group list card                                                            */
/* -------------------------------------------------------------------------- */
function GroupCard({ g, active, onClick }: { g: VariationGroupSummary; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`w-full text-left rounded-2xl border p-4 transition-colors ${
        active ? "border-brand-light bg-brand-surface/60" : "border-line bg-canvas-card hover:bg-brand-surface/40"
      }`}
    >
      <p className="text-sm font-bold text-ink line-clamp-2">{g.base_question_text ?? g.group_id}</p>
      <p className="text-[11px] text-ink-light font-medium mt-1">
        {[g.persona, g.therapeutic_area, g.brand_focus].filter(Boolean).join(" \u00b7 ") || "N/A"}
      </p>
      <div className="flex items-center gap-1.5 mt-2 flex-wrap">
        <span className="px-2 py-0.5 rounded-full text-[11px] font-bold bg-amber-100 text-amber-800">
          {g.draft_count} draft
        </span>
        <span className="px-2 py-0.5 rounded-full text-[11px] font-bold bg-teal-100 text-teal-800">
          {g.approved_count} approved
        </span>
        {g.rejected_count > 0 && (
          <span className="px-2 py-0.5 rounded-full text-[11px] font-bold bg-slate-200 text-ink-light">
            {g.rejected_count} rejected
          </span>
        )}
      </div>
    </button>
  );
}

/* -------------------------------------------------------------------------- */
/*  Draft row (review: edit / approve / reject)                                */
/* -------------------------------------------------------------------------- */
function DraftRow({
  v,
  busy,
  onEdit,
  onApprove,
  onReject,
}: {
  v: Variation;
  busy: boolean;
  onEdit: (v: Variation, text: string) => void;
  onApprove: (v: Variation) => void;
  onReject: (v: Variation) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(v.variation_text);
  const hasPii = !!v.pii_flags?.length;
  const isDraft = v.status === "DRAFT";

  return (
    <div className="rounded-xl border border-line p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1">
          {editing ? (
            <textarea value={text} onChange={(e) => setText(e.target.value)} rows={2} className={`${FIELD_CLS} resize-none`} />
          ) : (
            <p className="text-sm font-medium text-ink">{v.variation_text}</p>
          )}
          <div className="flex items-center gap-1.5 mt-2 flex-wrap">
            <span className={`px-2 py-0.5 rounded-full text-[11px] font-bold ${STATUS_CLS[v.status]}`}>{v.status}</span>
            {v.edited && <span className="px-2 py-0.5 rounded-full text-[11px] font-bold bg-sky-100 text-sky-800">edited</span>}
            {hasPii && (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-bold bg-red-100 text-red-700">
                <ShieldAlert size={11} /> PII: {v.pii_flags!.join(", ")}
              </span>
            )}
            {v.promoted_question_id && (
              <span className="px-2 py-0.5 rounded-full text-[11px] font-bold bg-teal-50 text-teal-700 font-mono">
                {"\u2192"} {v.promoted_question_id}
              </span>
            )}
          </div>
        </div>
        {isDraft && (
          <div className="flex items-center gap-1.5 shrink-0">
            {editing ? (
              <>
                <button
                  onClick={() => { onEdit(v, text); setEditing(false); }}
                  className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-bold bg-brand-surface text-brand hover:bg-brand hover:text-white transition-colors"
                >
                  <Check size={12} /> Save
                </button>
                <button
                  onClick={() => { setText(v.variation_text); setEditing(false); }}
                  className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-bold bg-slate-100 text-ink-light hover:bg-slate-200 transition-colors"
                >
                  <X size={12} />
                </button>
              </>
            ) : (
              <>
                <button
                  onClick={() => setEditing(true)}
                  disabled={busy}
                  className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-bold bg-slate-100 text-ink-light hover:bg-slate-200 disabled:opacity-40 transition-colors"
                >
                  <Pencil size={12} /> Edit
                </button>
                <button
                  onClick={() => onApprove(v)}
                  disabled={busy || hasPii}
                  title={hasPii ? "Remove PII before approving" : "Approve: promotes to a runnable question"}
                  className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-bold bg-teal-50 text-teal-700 hover:bg-teal-600 hover:text-white disabled:opacity-40 transition-colors"
                >
                  {busy ? <Spinner size={12} /> : <Check size={12} />} Approve
                </button>
                <button
                  onClick={() => onReject(v)}
                  disabled={busy}
                  className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-bold bg-red-50 text-red-700 hover:bg-red-600 hover:text-white disabled:opacity-40 transition-colors"
                >
                  <Ban size={12} /> Reject
                </button>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Results (divergence summary + per-variation model matrix)                  */
/* -------------------------------------------------------------------------- */
function ResultsPanel({ results, onRefresh, onOpenResponse }: { results: VariationGroupResults; onRefresh: () => void; onOpenResponse: (id: string) => void }) {
  const [open, setOpen] = useState<Set<string>>(new Set());
  const s = results.summary;
  const outlierIds = useMemo(() => new Set(s.outliers.map((o) => o.question_id)), [s.outliers]);

  if (!results.run_id) {
    return (
      <div className="rounded-xl border border-dashed border-line p-6 text-center">
        <p className="text-sm font-medium text-ink-light">
          No run yet for this group. Approve variations, then <b>Run group</b> to compare model answers.
        </p>
      </div>
    );
  }

  const consistency = s.consistency_score;
  const consColor =
    consistency === null ? "text-ink-light" : consistency >= 0.8 ? "text-teal-700" : consistency >= 0.5 ? "text-amber-700" : "text-red-700";

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-bold text-ink uppercase tracking-widest">Comparison across phrasings</h4>
        <button
          onClick={onRefresh}
          className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-bold bg-slate-100 text-ink-light hover:bg-slate-200 transition-colors"
        >
          <RefreshCw size={12} /> Refresh
        </button>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <div className="rounded-xl border border-line p-4">
          <p className="text-[11px] font-bold text-ink-light uppercase tracking-widest">Consistency</p>
          <p className={`text-2xl font-bold tabular-nums mt-1 ${consColor}`}>{pct(consistency)}</p>
          <p className="text-[11px] text-ink-muted mt-0.5">Agreement across phrasings</p>
        </div>
        <div className="rounded-xl border border-line p-4">
          <p className="text-[11px] font-bold text-ink-light uppercase tracking-widest">Position agreement</p>
          <p className="text-2xl font-bold tabular-nums mt-1 text-ink">{pct(s.position_agreement)}</p>
          <p className="text-[11px] text-ink-muted mt-0.5">{s.group_modal_position ? s.group_modal_position.replace(/_/g, " ").toLowerCase() : "N/A"}</p>
        </div>
        <div className="rounded-xl border border-line p-4">
          <p className="text-[11px] font-bold text-ink-light uppercase tracking-widest">Sentiment spread</p>
          <p className="text-2xl font-bold tabular-nums mt-1 text-ink">{s.sentiment_spread.toFixed(2)}</p>
          <p className="text-[11px] text-ink-muted mt-0.5">highest minus lowest across phrasings</p>
        </div>
        <div className="rounded-xl border border-line p-4">
          <p className="text-[11px] font-bold text-ink-light uppercase tracking-widest">Phrasings scored</p>
          <p className="text-2xl font-bold tabular-nums mt-1 text-ink">{s.variations_scored}</p>
          <p className="text-[11px] text-ink-muted mt-0.5">{s.outliers.length} outlier(s)</p>
        </div>
      </div>

      <div className="space-y-2">
        {results.variations.map((row) => {
          const isOpen = open.has(row.question_id);
          const isOutlier = outlierIds.has(row.question_id);
          return (
            <div key={row.question_id} className={`rounded-xl border p-3 ${isOutlier ? "border-red-300 bg-red-50/40" : "border-line"}`}>
              <button
                onClick={() =>
                  setOpen((prev) => {
                    const next = new Set(prev);
                    next.has(row.question_id) ? next.delete(row.question_id) : next.add(row.question_id);
                    return next;
                  })
                }
                className="w-full flex items-start justify-between gap-3 text-left"
              >
                <div className="flex items-start gap-2 flex-1">
                  {isOpen ? <ChevronDown size={16} className="mt-0.5 text-ink-light" /> : <ChevronRight size={16} className="mt-0.5 text-ink-light" />}
                  <div>
                    <p className="text-sm font-medium text-ink">
                      {row.is_base && <span className="mr-1.5 px-1.5 py-0.5 rounded bg-brand text-white text-[10px] font-bold align-middle">BASE</span>}
                      {row.question_text}
                    </p>
                    {isOutlier && (
                      <p className="text-[11px] font-bold text-red-600 mt-0.5">
                        Diverges on: {s.outliers.find((o) => o.question_id === row.question_id)?.reasons.join(", ")}
                      </p>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <SentimentBadge score={row.mean_sentiment} />
                  <PositionBadge position={row.modal_position} />
                  <span className="text-[11px] text-ink-light font-medium tabular-nums">{pct(row.mention_rate)} mention</span>
                </div>
              </button>
              {isOpen && (
                <div className="mt-3 pl-6 space-y-2">
                  {row.answers.length === 0 ? (
                    <p className="text-xs text-ink-muted">No model answers captured.</p>
                  ) : (
                    row.answers.map((a) => (
                      <button
                        key={a.response_id}
                        onClick={() => onOpenResponse(a.response_id)}
                        disabled={!a.response_id}
                        title="View the full response"
                        className="w-full text-left rounded-lg bg-brand-surface/40 border border-line p-2.5 transition-colors hover:border-brand-light hover:bg-brand-surface disabled:cursor-default disabled:hover:border-line disabled:hover:bg-brand-surface/40"
                      >
                        <div className="flex items-center gap-2 mb-1 flex-wrap">
                          <span className="text-xs font-bold text-ink">{a.llm_name}</span>
                          <SentimentBadge score={a.sentiment_score} />
                          <PositionBadge position={a.competitive_position} />
                          {a.response_id && (
                            <span className="ml-auto inline-flex items-center gap-0.5 text-[10px] font-bold text-brand">
                              Full response <ChevronRight size={11} />
                            </span>
                          )}
                        </div>
                        <p className="text-xs text-ink-light line-clamp-3">{a.answer_excerpt || "N/A"}</p>
                      </button>
                    ))
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Page                                                                       */
/* -------------------------------------------------------------------------- */
export default function VariationTesting() {
  const [params, setParams] = useSearchParams();
  const selected = params.get("group");
  const baseParam = params.get("base");
  const [groups, setGroups] = useState<VariationGroupSummary[]>([]);
  const [detail, setDetail] = useState<VariationGroupDetail | null>(null);
  const [results, setResults] = useState<VariationGroupResults | null>(null);
  const [busyVar, setBusyVar] = useState<number | null>(null);
  const [includeBase, setIncludeBase] = useState(true);
  const [running, setRunning] = useState(false);
  const [runInfo, setRunInfo] = useState<{ run_id: string; count: number } | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [reviewer, setReviewer] = useState("");
  const [polling, setPolling] = useState(false);
  const pollTimer = useRef<number | null>(null);
  const pollCancel = useRef(false);
  const [selectedResponse, setSelectedResponse] = useState<any>(null);

  const loadGroups = () => api.variationGroups().then((r) => setGroups(r.groups)).catch(() => {});
  const loadDetail = (gid: string) => api.variationGroup(gid).then(setDetail).catch(() => setDetail(null));
  const loadResults = (gid: string) => api.variationGroupResults(gid).then(setResults).catch(() => setResults(null));
  const openResponse = (id: string) => { if (id) api.responseDetail(id).then(setSelectedResponse).catch(() => {}); };

  // Stop any active auto-refresh loop.
  const stopPolling = () => {
    pollCancel.current = true;
    if (pollTimer.current) { window.clearTimeout(pollTimer.current); pollTimer.current = null; }
    setPolling(false);
  };

  // Auto-refresh a group's results while its run executes and scores, so the matrix fills in
  // live without a manual Refresh. Pinned to the new run_id; stops when the run finishes and
  // scores have landed, or on failure/timeout.
  const pollRun = (gid: string, runId: string) => {
    stopPolling();
    pollCancel.current = false;
    setPolling(true);
    const startedAt = Date.now();
    let terminalSince: number | null = null;
    const tick = async () => {
      if (pollCancel.current) return;
      const [run, res] = await Promise.all([
        api.run(runId).catch(() => null),
        api.variationGroupResults(gid, runId).catch(() => null),
      ]);
      if (pollCancel.current) return;
      if (res) setResults(res);
      const status = run?.status ?? "";
      const failed = status === "FAILED" || status === "CANCELLED" || status === "CANCELED";
      const terminal = failed || status === "COMPLETED" || status === "AWAITING_OPENEVIDENCE";
      const scored = (res?.summary?.variations_scored ?? 0) > 0;
      if (terminal && terminalSince === null) terminalSince = Date.now();
      const graceOver = terminalSince !== null && Date.now() - terminalSince > 45000;
      if (failed || (terminal && scored) || graceOver || Date.now() - startedAt > 300000) {
        setPolling(false);
        loadGroups();
        return;
      }
      pollTimer.current = window.setTimeout(tick, 3000);
    };
    void tick();
  };

  useEffect(() => { loadGroups(); }, []);
  useEffect(() => {
    stopPolling();
    if (selected) { loadDetail(selected); loadResults(selected); setRunInfo(null); setActionError(null); }
    else { setDetail(null); setResults(null); }
  }, [selected]);
  useEffect(() => () => stopPolling(), []);

  const select = (gid: string) => setParams(gid ? { group: gid } : {});

  const refreshAll = async () => {
    if (!selected) return;
    await Promise.all([loadGroups(), loadDetail(selected), loadResults(selected)]);
  };

  const editVar = async (v: Variation, text: string) => {
    setBusyVar(v.id); setActionError(null);
    try { await api.editVariation(v.id, text); await refreshAll(); }
    catch (e: any) { setActionError(e?.message || "Edit failed"); }
    finally { setBusyVar(null); }
  };
  const approveVar = async (v: Variation) => {
    setBusyVar(v.id); setActionError(null);
    try { await api.approveVariation(v.id, { reviewer_name: reviewer || undefined }); await refreshAll(); }
    catch (e: any) { setActionError(e?.message || "Approve failed"); }
    finally { setBusyVar(null); }
  };
  const rejectVar = async (v: Variation) => {
    setBusyVar(v.id); setActionError(null);
    try { await api.rejectVariation(v.id, { reviewer_name: reviewer || undefined }); await refreshAll(); }
    catch (e: any) { setActionError(e?.message || "Reject failed"); }
    finally { setBusyVar(null); }
  };

  const runGroup = async () => {
    if (!detail || running) return;
    setRunning(true); setActionError(null); setRunInfo(null);
    try {
      const r = await api.runVariationGroup(detail.group_id, { include_base: includeBase });
      setRunInfo({ run_id: r.run_id, count: r.count });
      pollRun(detail.group_id, r.run_id);
    } catch (e: any) {
      setActionError(e?.message || "Could not start the run");
    } finally {
      setRunning(false);
    }
  };

  const approvedCount = detail?.counts.approved ?? 0;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Phrasing Variation Testing"
        subtitle="Generate paraphrases of a question, review and approve them, then run the group to see how consistently AI models answer the same intent phrased differently."
      />

      <GeneratePanel initialBase={baseParam} onGenerated={(gid) => { select(gid); loadGroups(); }} />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left: group list */}
        <div className="lg:col-span-1 space-y-3">
          <h3 className="text-xs font-bold text-ink uppercase tracking-widest flex items-center gap-1.5">
            <GitBranch size={14} /> Variation groups ({groups.length})
          </h3>
          {groups.length === 0 ? (
            <EmptyState message="No variation groups yet. Generate drafts above to start." />
          ) : (
            groups.map((g) => (
              <GroupCard key={g.group_id} g={g} active={g.group_id === selected} onClick={() => select(g.group_id)} />
            ))
          )}
        </div>

        {/* Right: detail */}
        <div className="lg:col-span-2">
          {!detail ? (
            <Card>
              <EmptyState message="Select a variation group to review its drafts and results." />
            </Card>
          ) : (
            <div className="space-y-5">
              <Card>
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="text-[11px] font-bold text-ink-light uppercase tracking-widest">Base question</p>
                    <p className="text-sm font-bold text-ink mt-1">{detail.base?.question_text ?? detail.group_id}</p>
                    <p className="text-[11px] text-ink-light font-medium mt-1">
                      {[detail.base?.persona, detail.base?.therapeutic_area, detail.base?.brand_focus].filter(Boolean).join(" \u00b7 ") || "N/A"}
                    </p>
                  </div>
                  <div className="flex flex-col items-end gap-2 shrink-0">
                    <label className="flex items-center gap-1.5 text-xs font-medium text-ink-light">
                      <input type="checkbox" checked={includeBase} onChange={(e) => setIncludeBase(e.target.checked)} />
                      Include base
                    </label>
                    <button
                      onClick={runGroup}
                      disabled={running || approvedCount === 0}
                      title={approvedCount === 0 ? "Approve at least one variation to run the group" : "Run base + approved variations"}
                      className="inline-flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-bold bg-brand text-white hover:bg-brand-dark disabled:opacity-40 transition-colors"
                    >
                      {running ? <Spinner size={16} /> : <Play size={16} />}
                      {running ? "Starting\u2026" : "Run group"}
                    </button>
                  </div>
                </div>
                {runInfo && (
                  <div className="mt-3 p-3 rounded-xl bg-teal-50 text-teal-800 text-sm font-medium">
                    Run started ({runInfo.count} question{runInfo.count === 1 ? "" : "s"}). Models answer and score
                    in the background; results update automatically below.
                  </div>
                )}
                {actionError && <p className="text-xs font-bold text-red-600 mt-3">{actionError}</p>}
              </Card>

              <Card>
                <div className="flex items-center justify-between gap-3 mb-3 flex-wrap">
                  <h4 className="text-xs font-bold text-ink uppercase tracking-widest">
                    Drafts: {detail.counts.draft} pending, {detail.counts.approved} approved
                  </h4>
                  <input
                    value={reviewer}
                    onChange={(e) => setReviewer(e.target.value)}
                    placeholder="Reviewer name (optional)"
                    className="border border-line rounded-lg px-2.5 py-1.5 text-xs bg-canvas-card focus:outline-none focus:ring-2 focus:ring-brand-light/40"
                  />
                </div>
                <p className="text-[11px] text-ink-muted font-medium mb-3">
                  AI-generated drafts are screened for PII and never sent to a monitored model until a human approves them.
                </p>
                {detail.drafts.length === 0 ? (
                  <EmptyState message="No drafts in this group." />
                ) : (
                  <div className="space-y-2">
                    {detail.drafts.map((v) => (
                      <DraftRow key={v.id} v={v} busy={busyVar === v.id} onEdit={editVar} onApprove={approveVar} onReject={rejectVar} />
                    ))}
                  </div>
                )}
              </Card>

              <Card>
                {polling && (
                  <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-brand">
                    <Spinner size={14} /> Running analysis. Results update automatically.
                  </div>
                )}
                {results ? (
                  <ResultsPanel results={results} onRefresh={() => selected && loadResults(selected)} onOpenResponse={openResponse} />
                ) : polling ? (
                  <p className="text-sm font-medium text-ink-light">Waiting for the first model answers…</p>
                ) : (
                  <Spinner />
                )}
              </Card>
            </div>
          )}
        </div>
      </div>

      <AnimatePresence>
        {selectedResponse && (
          <ResponseDetailDrawer detail={selectedResponse} onClose={() => setSelectedResponse(null)} />
        )}
      </AnimatePresence>
    </div>
  );
}
