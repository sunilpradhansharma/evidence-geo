import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import { Radar, Search, AlertTriangle, ExternalLink, Check, X, ShieldAlert, CheckSquare, Square, Rocket } from "lucide-react";
import { api, HarvestedItem, HarvestRunResult } from "../api/client";
import { Card, Stat, PageHeader, Select, Spinner, AnimatedCard, EmptyState, InfoTooltip, ThemeBadge } from "../components/ui";
import { CoverageGapsPanel } from "../components/CoverageGapsPanel";
import { TA_GROUPS } from "../lib/taxonomy";

const PERSONAS = ["", "Prospect", "Patient", "Provider"];
const DOMAINS = ["Efficacy", "Safety", "Access", "Comparative", "General"];

export default function Harvest() {
  const [status, setStatus] = useState<any>(null);
  const [items, setItems] = useState<HarvestedItem[]>([]);
  const [filters, setFilters] = useState({ status: "CLASSIFIED", persona: "", therapeutic_area: "", domain: "" });
  // Monitoring mode is a REQUIRED choice for a harvest run (AbbVie vs All Brands); kept
  // separate from list filters so switching it doesn't refetch/clear the candidate list.
  const [mode, setMode] = useState("");
  const [running, setRunning] = useState(false);
  const [selected, setSelected] = useState<HarvestedItem | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [submitting, setSubmitting] = useState(false);
  const [confirmRun, setConfirmRun] = useState(false);
  const [runReviewer, setRunReviewer] = useState("");
  const [runningPipeline, setRunningPipeline] = useState(false);
  const [runResult, setRunResult] = useState<HarvestRunResult | null>(null);
  const navigate = useNavigate();
  const pollRef = useRef<number | null>(null);

  const loadStatus = () => api.harvestStatus().then(setStatus).catch(() => {});
  const loadItems = () => {
    const p = new URLSearchParams();
    if (filters.status) p.set("status", filters.status);
    if (filters.persona) p.set("persona", filters.persona);
    if (filters.therapeutic_area) p.set("therapeutic_area", filters.therapeutic_area);
    if (filters.domain) p.set("domain", filters.domain);
    p.set("limit", "1000");
    api.harvestItems(`?${p.toString()}`).then(setItems).catch(() => {});
  };

  useEffect(() => { loadStatus(); }, []);
  useEffect(loadItems, [filters]);

  // If the backend is already harvesting (e.g., after a page refresh), resume polling.
  useEffect(() => {
    if (status?.harvest?.running && !running) setRunning(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status?.harvest?.running]);

  // Poll while a harvest is running: refresh the table every tick so questions
  // stream in as they're scraped, and stop once the backend reports it's done.
  useEffect(() => {
    if (!running) return;
    pollRef.current = window.setInterval(async () => {
      const s = await api.harvestStatus().catch(() => null);
      if (!s) return;
      setStatus(s);
      loadItems();
      if (!s.harvest?.running) setRunning(false);
    }, 2000);
    return () => { if (pollRef.current) window.clearInterval(pollRef.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running]);

  const startHarvest = async () => {
    if (running || !mode) return;  // mandatory: force an AbbVie / All Brands choice first
    setRunning(true);
    try { await api.harvestRun(mode); await loadStatus(); }
    catch { setRunning(false); }
  };

  // Clear the multi-select whenever the underlying list changes (filter switch,
  // new harvest, etc.) so stale ids never get submitted.
  useEffect(() => { setSelectedIds(new Set()); }, [filters]);

  const toggleId = (id: number) =>
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });

  const allSelected = items.length > 0 && items.every((it) => selectedIds.has(it.id));

  const toggleAll = () =>
    setSelectedIds((prev) => {
      if (allSelected) return new Set();
      return new Set(items.map((it) => it.id));
    });

  // Bulk-submit the selected candidates for Medical Affairs review, reusing each
  // item's auto-classified persona/therapy/brand/domain values.
  const submitSelected = async () => {
    if (selectedIds.size === 0 || submitting) return;
    setSubmitting(true);
    try {
      const toSubmit = items.filter((it) => selectedIds.has(it.id));
      await Promise.all(
        toSubmit.map((it) =>
          api.harvestPromote(it.id, {
            persona: it.persona,
            therapeutic_area: it.therapeutic_area,
            brand_focus: it.brand_focus,
            domain: it.domain || "General",
            reviewer_name: null,
            override_ae: it.ae_flag,
          }).catch(() => null)
        )
      );
      setSelectedIds(new Set());
      loadItems();
      loadStatus();
    } finally {
      setSubmitting(false);
    }
  };

  // One-click: promote + approve the selected candidates and immediately launch a run
  // scoped to just those questions. AE / PII / injection / incomplete items are skipped
  // (surfaced in the result banner), never run.
  const doRunToPipeline = async () => {
    if (selectedIds.size === 0 || runningPipeline) return;
    setRunningPipeline(true);
    try {
      const res = await api.harvestRunToPipeline([...selectedIds], { reviewer_name: runReviewer || null });
      setConfirmRun(false);
      setSelectedIds(new Set());
      loadItems();
      loadStatus();
      if (res.run_id) {
        // A run started -> take the user straight to Run Analysis to watch it. Any skipped
        // items stay on the Discover list under their status filter.
        navigate("/run-analysis");
      } else {
        // Nothing qualified to run (AE / PII / incomplete) -> stay here and explain why.
        setRunResult(res);
      }
    } catch {
      setConfirmRun(false);
      setRunResult({ run_id: null, ran_count: 0, promoted: [], skipped: [{ id: 0, question_text: null, reason: "Run to Pipeline failed. Please try again." }] });
    } finally {
      setRunningPipeline(false);
    }
  };

  const configured = status?.configured;
  const byStatus = status?.by_status || {};
  const lastResult = status?.harvest?.last_result;
  const progress = status?.harvest?.progress;

  return (
    <div className="space-y-8">
      <PageHeader
        title="Discover Questions"
        subtitle="Real questions patients and providers are asking online: reviewed and submitted for Medical Affairs approval."
        tooltip="These questions are scraped from the live web. Nothing here is written by us. Each candidate is harvested by web scraping public health communities (Reddit, Quora, drugs.com, HealthUnlocked, patient.info and more) via live web search, then PII-scrubbed, de-duplicated, and auto-classified before it lands in this review queue."
      />

      {status && !configured && (
        <Card className="border-amber-200 bg-amber-50">
          <div className="flex items-start gap-3">
            <ShieldAlert className="text-amber-600 mt-0.5" size={18} />
            <div>
              <p className="text-sm font-bold text-amber-900">Tavily API key not set</p>
              <p className="text-xs text-amber-800 mt-1 font-medium">
                Add <code className="font-mono">TAVILY_API_KEY=…</code> to your{" "}
                <code className="font-mono">.env</code> and restart the backend to enable harvesting.
              </p>
            </div>
          </div>
        </Card>
      )}

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <AnimatedCard delay={0}><Stat label="Staged total" value={status?.total ?? 0} icon={<Radar size={16} />} /></AnimatedCard>
        <AnimatedCard delay={0.05}><Stat label="Awaiting review" value={byStatus.CLASSIFIED ?? 0} /></AnimatedCard>
        <AnimatedCard delay={0.1}><Stat label="Submitted" value={byStatus.PROMOTED ?? 0} /></AnimatedCard>
        <AnimatedCard delay={0.15}><Stat label="FLAGS" value={byStatus.QUARANTINED_AE ?? 0} icon={<AlertTriangle size={16} />} /></AnimatedCard>
      </div>

      {/* Web harvesting can only surface questions that already exist online. This covers
          the comparisons nobody happened to post about, and stages them into the same
          queue below so the existing review + Run to Pipeline flow handles them. */}
      <CoverageGapsPanel onStaged={() => { loadItems(); loadStatus(); }} />

      <Card>
        <div className="flex items-end justify-between gap-4 flex-wrap">
          <div className="flex gap-4 flex-wrap">
            <Select label="Status" value={filters.status}
              options={["CLASSIFIED", "QUARANTINED_AE", "PROMOTED", "REJECTED", ""]}
              onChange={(v) => setFilters({ ...filters, status: v })}
              tooltip={"Filter by review stage:\n• Classified: awaiting review\n• Promoted: submitted for MA approval\n• Rejected: dismissed\n• Quarantined AE: flagged for adverse event content"} />
            <Select label="Persona" value={filters.persona} options={PERSONAS}
              onChange={(v) => setFilters({ ...filters, persona: v })}
              tooltip={"Who is asking the question:\n• Prospect: exploring treatment options\n• Patient: currently on treatment\n• Provider: a clinician"} />
            <Select label="Therapeutic Area & Indication" value={filters.therapeutic_area} groups={TA_GROUPS}
              onChange={(v) => setFilters({ ...filters, therapeutic_area: v })}
              tooltip={"The disease area or specific indication this question targets.\nExamples: Dermatology, Gastroenterology, Oncology, Endometriosis."} />
            <Select label="Theme" value={filters.domain} options={["", ...DOMAINS]}
              onChange={(v) => setFilters({ ...filters, domain: v })}
              tooltip={"The theme of the question:\n• Efficacy: treatment outcomes & dosing\n• Safety: side effects & risks\n• Access: coverage & cost\n• Comparative: vs. competitors\n• General: broad or exploratory"} />
          </div>
          <div className="flex items-end gap-4 flex-wrap">
            <div className="flex flex-col">
              <label className="text-xs font-semibold text-ink-light mb-1.5 uppercase tracking-wide flex items-center gap-1">
                <InfoTooltip content={"Required. AbbVie: harvest questions about AbbVie brands.\nAll Brands: harvest across the whole disease landscape (all competitors, no single AbbVie brand)."} />
                Monitoring Mode
              </label>
              <select
                value={mode}
                onChange={(e) => setMode(e.target.value)}
                className={`border rounded-xl px-3 py-2 text-sm bg-canvas-card text-ink font-medium focus:outline-none focus:ring-2 focus:ring-brand-light/40 focus:border-brand-light transition-colors ${mode ? "border-line" : "border-amber-300"}`}
              >
                <option value="" disabled>Select…</option>
                <option value="BRAND">AbbVie</option>
                <option value="DISEASE_STATE">All Brands</option>
              </select>
            </div>
            <button onClick={startHarvest} disabled={running || !configured || !mode}
              className="flex items-center gap-2 px-5 py-2.5 bg-brand text-white rounded-xl text-sm font-bold hover:bg-brand-dark disabled:opacity-40 transition-colors">
              {running ? <Spinner size={16} /> : <Search size={16} />}
              {running ? "Harvesting…" : "Discover Questions"}
            </button>
          </div>
        </div>
        {!mode && (
          <p className="mt-2 text-xs font-medium text-amber-700">Choose a Monitoring Mode (AbbVie or All Brands) to start discovering questions.</p>
        )}
        {running && progress && (
          <p className="text-xs font-bold text-brand mt-3">
            Scraping live · {progress.queries_done ?? 0}/{progress.queries_total ?? 0} searches ·{" "}
            {progress.staged ?? 0} questions staged so far…
          </p>
        )}
        {!running && lastResult && (
          <p className="text-xs text-ink-light mt-3 font-medium">
            Last run:{" "}
            {lastResult.status === "ok"
              ? `${lastResult.staged} staged · ${lastResult.duplicates} dupes · ${lastResult.filtered_off_topic} off-topic · ${lastResult.quarantined_ae} AE quarantined (from ${lastResult.raw_results} results)`
              : lastResult.reason || lastResult.status}
          </p>
        )}
      </Card>

      {runResult && (
        <Card className="border-brand/40 bg-brand-surface/40">
          <div className="flex items-start justify-between gap-3">
            <div className="space-y-2 min-w-0">
              {runResult.run_id ? (
                <p className="text-sm font-bold text-brand">
                  Started a run with {runResult.ran_count} question{runResult.ran_count === 1 ? "" : "s"}.
                </p>
              ) : (
                <p className="text-sm font-bold text-amber-700">No questions were run.</p>
              )}
              {runResult.skipped.length > 0 && (
                <div className="text-xs text-ink-light font-medium">
                  <p className="mb-1 font-bold text-ink">{runResult.skipped.length} skipped:</p>
                  <ul className="list-disc pl-5 space-y-0.5 max-h-40 overflow-y-auto">
                    {runResult.skipped.map((s) => (
                      <li key={s.id}>
                        <span className="text-ink">{s.question_text || `#${s.id}`}</span>: {s.reason}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
            <div className="flex items-center gap-2 shrink-0">
              {runResult.run_id && (
                <button
                  onClick={() => navigate("/run-analysis")}
                  className="px-4 py-2 bg-brand text-white rounded-xl text-xs font-bold hover:bg-brand-dark transition-colors"
                >
                  View run
                </button>
              )}
              <button onClick={() => setRunResult(null)} className="text-ink-light hover:text-ink"><X size={16} /></button>
            </div>
          </div>
        </Card>
      )}

      <Card>
        <div className="flex items-center justify-between gap-3 mb-4 flex-wrap">
          <h3 className="text-xs font-bold text-ink uppercase tracking-widest">{items.length} Candidates</h3>
          <div className="flex items-center gap-2">
            {selectedIds.size > 0 && (
              <button
                onClick={() => setSelectedIds(new Set())}
                className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-bold text-ink-light bg-slate-100 hover:bg-slate-200 transition-colors"
              >
                <X size={14} /> Clear ({selectedIds.size})
              </button>
            )}
            <button
              disabled={selectedIds.size === 0 || submitting}
              onClick={submitSelected}
              className="flex items-center gap-2 px-5 py-2.5 bg-brand text-white rounded-xl text-sm font-bold hover:bg-brand-dark disabled:opacity-40 transition-colors"
            >
              {submitting ? <Spinner size={16} /> : <Check size={16} />}
              {submitting ? "Submitting\u2026" : `Submit for Medical Affairs Review${selectedIds.size > 0 ? ` (${selectedIds.size})` : ""}`}
            </button>
            <button
              disabled={selectedIds.size === 0 || runningPipeline}
              onClick={() => { setRunResult(null); setConfirmRun(true); }}
              title="Add the selected questions to the bank, approve them, and run them now."
              className="flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-bold border-2 border-brand text-brand hover:bg-brand-surface disabled:opacity-40 transition-colors"
            >
              <Rocket size={16} /> Run to Pipeline{selectedIds.size > 0 ? ` (${selectedIds.size})` : ""}
            </button>
          </div>
        </div>
        {items.length === 0 ? (
          <EmptyState message="No harvested questions for this filter. Click “Discover Questions” to scrape the web for fresh ones." icon={<Radar size={28} />} />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[840px] text-sm">
              <thead>
                <tr className="text-left border-b-2 border-slate-200">
                  <th className="pb-3 pr-3 w-8">
                    <button
                      onClick={toggleAll}
                      className="text-ink-light hover:text-brand transition-colors align-middle"
                      title={allSelected ? "Deselect all" : "Select all"}
                    >
                      {allSelected ? <CheckSquare size={18} className="text-brand" /> : <Square size={18} />}
                    </button>
                  </th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">
                    <span className="inline-flex items-center gap-1"><InfoTooltip content={"The verbatim question scraped from a public health community.\nShown exactly as found: no edits or paraphrasing."} /> Question</span>
                  </th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">
                    <span className="inline-flex items-center gap-1"><InfoTooltip content={"The theme of the question:\n• Efficacy: treatment outcomes & dosing\n• Safety: side effects & risks\n• Access: coverage & cost\n• Comparative: vs. competitors\n• General: broad or exploratory"} /> Theme</span>
                  </th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">
                    <span className="inline-flex items-center gap-1"><InfoTooltip content={"Who is asking the question:\n• Prospect: exploring treatment options\n• Patient: currently on treatment\n• Provider: a clinician"} /> Persona</span>
                  </th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">
                    <span className="inline-flex items-center gap-1"><InfoTooltip content={"The specific therapy or drug this question focuses on.\nSet during review before submitting for Medical Affairs approval."} /> Brand</span>
                  </th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">
                    <span className="inline-flex items-center gap-1"><InfoTooltip content={"The website or community where this question was originally found.\nExamples: Reddit, Quora, drugs.com, HealthUnlocked, patient.info."} /> Source</span>
                  </th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">
                    <span className="inline-flex items-center gap-1"><InfoTooltip content={"Automated safety signals detected during scraping:\n• AE: adverse event content, requires safety team sign-off\n• PII: personally identifiable information detected, must be reviewed"} /> Flags</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((it, i) => {
                  const isSel = selectedIds.has(it.id);
                  return (
                  <motion.tr key={it.id}
                    initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: Math.min(i * 0.015, 0.5) }}
                    onClick={() => setSelected(it)}
                    className={`border-b border-slate-100 cursor-pointer transition-colors ${isSel ? "bg-brand-surface/60" : "hover:bg-brand-surface/50"}`}>
                    <td className="py-3 pr-3">
                      <button
                        onClick={(e) => { e.stopPropagation(); toggleId(it.id); }}
                        className="text-ink-light hover:text-brand transition-colors align-middle"
                      >
                        {isSel ? <CheckSquare size={18} className="text-brand" /> : <Square size={18} />}
                      </button>
                    </td>
                    <td className="py-3 max-w-[200px] lg:max-w-xs xl:max-w-md text-ink font-medium">{it.question_text}</td>
                    <td className="py-3"><ThemeBadge theme={it.domain} /></td>
                    <td className="py-3 text-ink-light">{it.persona || "N/A"}</td>
                    <td className="py-3 text-ink-light">{it.brand_focus || "N/A"}</td>
                    <td className="py-3 text-ink-light text-xs">{it.source_domain || it.source}</td>
                    <td className="py-3 whitespace-nowrap">
                      {it.ae_flag && (
                        <span className="inline-flex items-center gap-1">
                          <InfoTooltip content="Adverse Event: this question may contain drug safety concerns. Requires safety team sign-off before it can be submitted." />
                          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-bold bg-red-100 text-red-700">
                            <AlertTriangle size={11} /> AE
                          </span>
                        </span>
                      )}
                      {(it.pii_flags?.length ?? 0) > 0 && (
                        <span className="inline-flex items-center gap-1">
                          <InfoTooltip content="Personally Identifiable Information detected: this question may contain patient-identifiable data and must be reviewed before submission." />
                          <span className="px-2 py-0.5 rounded-full text-xs font-bold bg-amber-100 text-amber-800">PII</span>
                        </span>
                      )}
                    </td>
                  </motion.tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <AnimatePresence>
        {selected && (
          <ReviewDrawer
            item={selected}
            onClose={() => setSelected(null)}
            onChanged={() => { loadItems(); loadStatus(); }}
          />
        )}
      </AnimatePresence>

      {confirmRun && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={() => !runningPipeline && setConfirmRun(false)}>
          <div className="w-full max-w-md rounded-2xl bg-canvas-card p-6 shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-2">
              <h3 className="text-sm font-bold text-ink">Run {selectedIds.size} to pipeline</h3>
              <button onClick={() => setConfirmRun(false)} className="text-ink-light hover:text-ink"><X size={18} /></button>
            </div>
            <p className="text-xs text-ink-light font-medium leading-relaxed mb-4">
              This adds the selected questions to the bank, marks them <b className="text-ink">Approved</b>, and starts a run right away. Adverse-event, PII, or prompt-injection flagged items are skipped for safety. This bypasses the usual Medical Affairs review step.
            </p>
            <div className="flex flex-col mb-4">
              <label className="text-[11px] font-bold text-ink-light uppercase tracking-widest mb-1">Reviewer name (optional)</label>
              <input value={runReviewer} onChange={(e) => setRunReviewer(e.target.value)}
                className="border border-line rounded-xl px-3 py-2 text-sm bg-canvas-card text-ink font-medium focus:outline-none focus:ring-2 focus:ring-brand-light/40" />
            </div>
            <div className="flex gap-2 justify-end">
              <button onClick={() => setConfirmRun(false)} disabled={runningPipeline}
                className="px-4 py-2.5 bg-slate-100 text-ink-light rounded-xl text-sm font-bold hover:bg-slate-200 disabled:opacity-40 transition-colors">
                Cancel
              </button>
              <button onClick={doRunToPipeline} disabled={runningPipeline || selectedIds.size === 0}
                className="flex items-center gap-2 px-5 py-2.5 bg-brand text-white rounded-xl text-sm font-bold hover:bg-brand-dark disabled:opacity-40 transition-colors">
                {runningPipeline ? <Spinner size={16} /> : <Rocket size={16} />} Run {selectedIds.size} to pipeline
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function ReviewDrawer({ item, onClose, onChanged }: {
  item: HarvestedItem; onClose: () => void; onChanged: () => void;
}) {
  const [persona, setPersona] = useState(item.persona || "");
  const [ta, setTa] = useState(item.therapeutic_area || "");
  const [brand, setBrand] = useState(item.brand_focus || "");
  const [domain, setDomain] = useState(item.domain || "General");
  const [reviewer, setReviewer] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const promote = async () => {
    setBusy(true); setErr(null);
    try {
      await api.harvestPromote(item.id, {
        persona, therapeutic_area: ta, brand_focus: brand, domain,
        reviewer_name: reviewer || null, override_ae: item.ae_flag,
      });
      setDone("Submitted for Medical Affairs review. Status: Pending.");
      onChanged();
    } catch {
      setErr("Submit failed: set persona, therapy, brand, and theme (and clear any PII), then retry.");
    } finally { setBusy(false); }
  };

  const reject = async () => {
    setBusy(true); setErr(null);
    try { await api.harvestReject(item.id, "Rejected by reviewer"); setDone("Rejected."); onChanged(); }
    catch { setErr("Reject failed."); }
    finally { setBusy(false); }
  };

  return (
    <>
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
        onClick={onClose} className="fixed inset-0 bg-black/30 z-40" />
      <motion.div initial={{ x: "100%" }} animate={{ x: 0 }} exit={{ x: "100%" }}
        transition={{ type: "spring", damping: 30, stiffness: 300 }}
        className="fixed top-0 right-0 h-full w-full max-w-lg bg-canvas-card shadow-2xl z-50 overflow-y-auto p-6 space-y-5">
        <div className="flex items-start justify-between">
          <h2 className="text-lg font-extrabold text-ink">Review candidate</h2>
          <button onClick={onClose} className="text-ink-light hover:text-ink"><X size={20} /></button>
        </div>

        {item.ae_flag && (
          <div className="flex items-start gap-2 p-3 rounded-xl bg-red-50 border border-red-200">
            <AlertTriangle className="text-red-600 mt-0.5" size={16} />
            <p className="text-xs font-medium text-red-800">
              Possible adverse-event content. Route to safety review. Only submit after sign-off.
            </p>
          </div>
        )}

        <div>
          <p className="text-xs font-bold text-ink-light uppercase tracking-widest mb-1">Question (verbatim)</p>
          <p className="text-sm text-ink font-medium">{item.question_text}</p>
          {item.search_persona && (
            <p className="text-xs text-ink-light mt-1.5 font-medium">
              Surfaced via the <span className="font-bold text-brand">{item.search_persona}</span> search lens.
            </p>
          )}
        </div>

        {item.source_url && (
          <a href={item.source_url} target="_blank" rel="noreferrer"
            className="inline-flex items-center gap-1.5 text-xs font-bold text-brand hover:text-brand-dark">
            <ExternalLink size={13} /> {item.source_domain || "source"}
          </a>
        )}

        {item.raw_excerpt && (
          <div>
            <p className="text-xs font-bold text-ink-light uppercase tracking-widest mb-1">Source context</p>
            <p className="text-xs text-ink-light leading-relaxed bg-slate-50 rounded-xl p-3 max-h-40 overflow-y-auto">{item.raw_excerpt}</p>
          </div>
        )}

        <div className="grid grid-cols-2 gap-3">
          <Select label="Persona" value={persona} options={PERSONAS} onChange={setPersona} />
          <Select label="Therapeutic Area & Indication" value={ta} groups={TA_GROUPS} onChange={setTa} />
          <Select label="Theme" value={domain} options={DOMAINS} onChange={setDomain} />
          <div className="flex flex-col">
            <label className="text-xs font-semibold text-ink-light mb-1.5 uppercase tracking-wide">Brand</label>
            <input value={brand} onChange={(e) => setBrand(e.target.value)}
              className="border border-slate-200 rounded-xl px-3 py-2 text-sm bg-white text-ink font-medium focus:outline-none focus:ring-2 focus:ring-brand-light/40" />
          </div>
        </div>

        <div className="flex flex-col">
          <label className="text-xs font-semibold text-ink-light mb-1.5 uppercase tracking-wide">Reviewer name (optional)</label>
          <input value={reviewer} onChange={(e) => setReviewer(e.target.value)}
            className="border border-slate-200 rounded-xl px-3 py-2 text-sm bg-white text-ink font-medium focus:outline-none focus:ring-2 focus:ring-brand-light/40" />
        </div>

        {err && <p className="text-xs font-semibold text-red-600">{err}</p>}
        {done && <p className="text-xs font-semibold text-teal-700">{done}</p>}

        <div className="flex gap-2 pt-1 items-center">
          <InfoTooltip content="Sends this question to the Medical Affairs team for approval. It becomes PENDING and can only be used in a monitoring run after MA signs off." />
          <button onClick={promote} disabled={busy || item.status === "PROMOTED"}
            className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 bg-brand text-white rounded-xl text-sm font-bold hover:bg-brand-dark disabled:opacity-40 transition-colors">
            {busy ? <Spinner size={16} /> : <Check size={16} />} Submit for Medical Affairs review
          </button>
          <button onClick={reject} disabled={busy}
            className="px-4 py-2.5 bg-slate-100 text-ink-light rounded-xl text-sm font-bold hover:bg-slate-200 disabled:opacity-40 transition-colors">
            Reject
          </button>
        </div>
        <p className="text-[11px] text-ink-light leading-relaxed">
          Submitting creates a <span className="font-bold">PENDING</span> question: it still needs Medical-Affairs approval before any monitoring run can use it.
        </p>
      </motion.div>
    </>
  );
}
