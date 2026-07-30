import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import {
  Play,
  FileQuestion,
  ShieldCheck,
  Brain,
  Users,
  Cpu,
  GitMerge,
  Scale,
  BarChart3,
  AlertTriangle,
  Database,
  LayoutDashboard,
  ChevronRight,
  X,
  Zap,
  Upload,
  Wand2,
  Calendar,
  PlayCircle,
  Square,
  RefreshCw,
  RotateCcw,
  Repeat,
  FileSpreadsheet,
  Clock,
  CheckCircle2,
  XCircle,
  Loader2,
  ChevronDown,
  ChevronUp,
  ExternalLink,
  Stethoscope,
  Hammer,
} from "lucide-react";
import { api, PRELAUNCH_LABEL, Run, RunProgress, Schedule } from "../api/client";
import { Card, PageHeader, Select, Spinner, InfoTooltip } from "../components/ui";
import { TA_GROUPS } from "../lib/taxonomy";

/* ================================================================== */
/*  MODELS                                                             */
/* ================================================================== */
const MODELS: { name: string; provider: string; color: string; enabled: boolean; providerOnly?: boolean }[] = [
  { name: "Claude", provider: "Bedrock", color: "#0D4F4F", enabled: true },
  { name: "Nova-Pro", provider: "Bedrock", color: "#0F766E", enabled: true },
  { name: "Llama", provider: "Bedrock", color: "#14B8A6", enabled: true },
  { name: "Gemini", provider: "Google", color: "#0284C7", enabled: true },
  { name: "GPT-4o", provider: "OpenAI", color: "#10A37F", enabled: true },
  { name: "EvidenceMD", provider: "Clinical reasoning", color: "#0891B2", enabled: true, providerOnly: true },
];

const STATUS_STYLES: Record<string, { bg: string; icon: React.ElementType }> = {
  RUNNING: { bg: "bg-teal-100 text-teal-800", icon: Loader2 },
  COMPLETED: { bg: "bg-teal-100 text-teal-800", icon: CheckCircle2 },
  FAILED: { bg: "bg-red-100 text-red-700", icon: XCircle },
  PAUSED_BUDGET: { bg: "bg-amber-100 text-amber-800", icon: AlertTriangle },
  AWAITING_OPENEVIDENCE: { bg: "bg-amber-100 text-amber-800", icon: Clock },
  CANCELLED: { bg: "bg-slate-200 text-ink-light", icon: Square },
};

const STATUS_LABELS: Record<string, string> = {
  PENDING: "Pending",
  RUNNING: "Running",
  COMPLETED: "Completed",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
  PAUSED_BUDGET: "Paused: budget limit reached",
  AWAITING_OPENEVIDENCE: "Paused: waiting for clinician input",
};
const statusLabel = (s: string) =>
  STATUS_LABELS[s] ?? s.replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());

// Mirrors RESUMABLE_STATUSES in backend/app/services/run_service.py. These are the three
// recoverable stops: the process went away (a deploy or crash leaves FAILED), the token
// ceiling was hit, or an operator stopped the run. Resuming a cancelled run is a normal
// sequence and costs nothing already bought, whereas Re-run pays for all of it again. The
// button is only a hint: the server re-checks and explains itself with a 409.
const RESUMABLE_STATUSES = new Set(["FAILED", "PAUSED_BUDGET", "CANCELLED"]);
const capturedCount = (r: Run) =>
  r.responses_success + r.responses_failed + r.responses_truncated + r.responses_blocked;
const isResumable = (r: Run) => RESUMABLE_STATUSES.has(r.status) && capturedCount(r) > 0;

// Retry is a different question from Resume: Resume dispatches what was never attempted,
// Retry re-attempts what errored. A run that finished COMPLETED with failures can still be
// retried, which is why this is not keyed on status. Excluded while a run waits on
// OpenEvidence, since reopening it would discard that pause.
const canRetryFailed = (r: Run) =>
  r.status !== "AWAITING_OPENEVIDENCE" && r.responses_failed > 0;

const TRIGGER_LABELS: Record<string, string> = {
  ADHOC: "On-demand run",
  SCHEDULED: "Scheduled run",
  SCHEDULE: "Scheduled run",
  CSV: "CSV upload run",
};
const triggerLabel = (t: string) =>
  TRIGGER_LABELS[t] ?? t.replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());

/* ================================================================== */
/*  PIPELINE NODE DEFINITIONS                                          */
/* ================================================================== */
interface PipelineNode {
  id: string;
  label: string;
  subtitle: string;
  icon: React.ElementType;
  detail: string;
  color: string;
}

const NODES: PipelineNode[] = [
  { id: "trigger", label: "Run Triggered", subtitle: "Schedule or Ad-Hoc", icon: Play, detail: "Runs are triggered daily via schedule or on-demand by CSP users. CSV uploads or synthetic question generation initiate the pipeline.", color: "bg-teal-500" },
  { id: "questions", label: "Question Repository", subtitle: "Fetch & MA Approval", icon: FileQuestion, detail: "Questions are fetched from the approved repository. Each question has persona, therapeutic area, brand, and domain tags. Only MA-approved questions proceed.", color: "bg-sky-500" },
  { id: "triage", label: "The Triage Gate", subtitle: "Intent Classification", icon: Brain, detail: "Hybrid classifier: Layer 1 uses deterministic rules (persona + domain), Layer 2 falls back to Claude Haiku for uncertain cases. Outputs: CLINICAL, EXPERIENTIAL, SCREENING, or SHORTHAND.", color: "bg-amber-500" },
  { id: "persona", label: "Persona Router", subtitle: "Target Selection", icon: Users, detail: "Patient/Prospect queries route to public LLMs only. Provider queries also include EvidenceMD, a clinical-reasoning LLM. Shorthand intents bypass generative synthesis entirely.", color: "bg-orange-500" },
  { id: "execution", label: "Execution Agent", subtitle: "Tested across 5 AI platforms", icon: Cpu, detail: "All 5 AI platforms receive the question simultaneously. Rate limiting, retry with exponential backoff, and truncation handling are applied per-model. Platforms: Claude, Nova-Pro, Llama, Gemini, GPT-4o.", color: "bg-teal-600" },
  { id: "evidencemd", label: "EvidenceMD", subtitle: "Clinical Reasoning LLM", icon: Stethoscope, detail: "For Provider-persona questions, EvidenceMD — a clinical-reasoning LLM that cites peer-reviewed literature — is queried automatically through its API alongside the public platforms. Its evidence-based answer is scored and folded into consensus exactly like any other platform. Provider persona only.", color: "bg-cyan-600" },
  { id: "orchestrator", label: "Master Orchestrator", subtitle: "Collate Responses", icon: GitMerge, detail: "The orchestrator collects all model responses for each question. Failed/blocked responses are logged. Successful responses proceed to consensus evaluation.", color: "bg-brand" },
  { id: "chairman", label: "The Chairman", subtitle: "What most AI platforms agree on", icon: Scale, detail: "Claude evaluates clinical consensus across model responses. FULL = agreement. PARTIAL/MISSING triggers GEO Schema fallback with verified ground truth data (llms.txt + JSON-LD).", color: "bg-teal-700" },
  { id: "scoring", label: "Scoring Engine", subtitle: "Sentiment · Position · Claims", icon: BarChart3, detail: "Automated scoring extracts sentiment (-1 to +1), competitive positioning (first-line recommended → not mentioned), brand mentions, and key clinical claims from each response.", color: "bg-sky-600" },
  { id: "alerts", label: "Alert Engine", subtitle: "Threshold Monitoring", icon: AlertTriangle, detail: "Sentiment < -0.3 or NOT_RECOMMENDED positioning triggers alerts. Alerts route to Medical Affairs for review. Material changes vs. previous runs are flagged.", color: "bg-red-500" },
  { id: "repository", label: "Response Repository", subtitle: "Immutable Storage", icon: Database, detail: "All responses, scores, consensus records, and alerts are written to the immutable repository. Version-controlled scoring enables historical re-analysis.", color: "bg-slate-600" },
  { id: "dashboard", label: "Dashboard Update", subtitle: "Metrics Refresh", icon: LayoutDashboard, detail: "Dashboard metrics refresh automatically: sentiment distributions, positioning charts, consensus breakdowns, alert summaries, and response volume trends.", color: "bg-teal-500" },
];

/* ================================================================== */
/*  PIPELINE VISUALIZATION COMPONENTS                                  */
/* ================================================================== */
function Connector({ active }: { active: boolean }) {
  return (
    <div className="hidden sm:flex items-center mx-1 flex-shrink-0">
      <div className="relative w-8 h-[2px]">
        <div className={`absolute inset-0 ${active ? "bg-brand-light" : "bg-slate-300"} rounded-full transition-colors duration-500`} />
        {active && (
          <motion.div
            className="absolute top-1/2 -translate-y-1/2 w-2 h-2 rounded-full bg-brand-light shadow-lg shadow-brand-light/50"
            animate={{ x: [0, 24, 0] }}
            transition={{ duration: 1.5, repeat: Infinity, ease: "easeInOut" }}
          />
        )}
      </div>
      <ChevronRight size={12} className={`flex-shrink-0 ${active ? "text-brand-light" : "text-slate-300"} -ml-1`} />
    </div>
  );
}

function NodeCard({ node, index, activeIndex, isRunning, onSelect }: {
  node: PipelineNode; index: number; activeIndex: number; isRunning: boolean; onSelect: (n: PipelineNode) => void;
}) {
  const isActive = isRunning && index === activeIndex;
  const isComplete = isRunning && index < activeIndex;
  const isPending = isRunning && index > activeIndex;
  return (
    <motion.button onClick={() => onSelect(node)} className={`relative flex flex-col items-center p-3 rounded-2xl border-2 w-[90px] transition-all duration-300 cursor-pointer group ${isActive ? "border-brand-light bg-brand-surface shadow-lg shadow-brand-light/20 animate-processing" : isComplete ? "border-teal-300 bg-teal-50" : isPending ? "border-slate-200 bg-slate-50 opacity-60" : "border-slate-200 bg-white hover:border-brand-light/40 hover:shadow-md"}`} whileHover={{ scale: 1.05 }} whileTap={{ scale: 0.98 }}>
      <div className={`w-10 h-10 rounded-xl flex items-center justify-center mb-2 transition-colors duration-300 ${isActive ? "bg-brand-light text-white" : isComplete ? "bg-teal-500 text-white" : `${node.color} text-white`}`}>
        <node.icon size={20} strokeWidth={2} />
      </div>
      <span className="text-xs font-bold text-ink text-center leading-tight">{node.label}</span>
      <span className="text-[10px] text-ink-light text-center mt-0.5 leading-tight font-medium">{node.subtitle}</span>
      {isActive && <motion.div className="absolute -top-1 -right-1 w-3 h-3 rounded-full bg-brand-light" animate={{ scale: [1, 1.4, 1] }} transition={{ duration: 1, repeat: Infinity }} />}
      {isComplete && <div className="absolute -top-1 -right-1 w-4 h-4 rounded-full bg-teal-500 flex items-center justify-center"><Zap size={10} className="text-white" /></div>}
    </motion.button>
  );
}

function DetailPanel({ node, onClose }: { node: PipelineNode; onClose: () => void }) {
  return (
    <motion.div initial={{ opacity: 0, x: 40 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: 40 }} className="fixed right-0 top-0 h-full w-full max-w-[420px] bg-white shadow-2xl border-l border-slate-200 z-50 overflow-y-auto">
      <div className="p-6">
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-3">
            <div className={`w-10 h-10 rounded-xl ${node.color} text-white flex items-center justify-center`}><node.icon size={20} /></div>
            <div><h3 className="font-extrabold text-ink text-lg">{node.label}</h3><p className="text-xs text-ink-light font-medium">{node.subtitle}</p></div>
          </div>
          <button onClick={onClose} className="p-2 hover:bg-slate-100 rounded-xl transition-colors"><X size={18} className="text-ink-light" /></button>
        </div>
        <div className="bg-brand-surface rounded-2xl p-5 mb-6"><p className="text-sm text-ink leading-relaxed font-medium">{node.detail}</p></div>
        {node.id === "execution" && (
          <div className="space-y-3">
            <h4 className="text-xs font-bold text-ink uppercase tracking-widest">Active Models</h4>
            {["Claude (Bedrock)", "Nova-Pro (Bedrock)", "Llama (Bedrock)", "Gemini (Google)", "GPT-4o (OpenAI)"].map((m) => (
              <div key={m} className="flex items-center gap-3 p-3 bg-slate-50 rounded-xl"><div className="w-2 h-2 rounded-full bg-brand-light" /><span className="text-sm font-semibold text-ink">{m}</span></div>
            ))}
          </div>
        )}
        {node.id === "chairman" && (
          <div className="space-y-3">
            <h4 className="text-xs font-bold text-ink uppercase tracking-widest">Consensus Levels</h4>
            {[{ level: "FULL", desc: "All models agree on core facts", color: "bg-teal-100 text-teal-800" }, { level: "PARTIAL", desc: "Agreement on some points, divergence on others", color: "bg-amber-100 text-amber-800" }, { level: "MISSING", desc: "Critical contradictions detected", color: "bg-red-100 text-red-700" }].map((c) => (
              <div key={c.level} className="flex items-center gap-3 p-3 bg-slate-50 rounded-xl"><span className={`px-2.5 py-1 rounded-full text-xs font-bold ${c.color}`}>{c.level}</span><span className="text-sm text-ink-light font-medium">{c.desc}</span></div>
            ))}
          </div>
        )}
        {node.id === "triage" && (
          <div className="space-y-3">
            <h4 className="text-xs font-bold text-ink uppercase tracking-widest">Intent Types</h4>
            {[{ intent: "CLINICAL", desc: "Guidelines, dosing, safety data" }, { intent: "EXPERIENTIAL", desc: "Lifestyle, emotional, patient experience" }, { intent: "SCREENING", desc: "Comparative, exploratory queries" }, { intent: "SHORTHAND", desc: "Abbreviated, jargon-heavy queries" }].map((i) => (
              <div key={i.intent} className="flex items-center gap-3 p-3 bg-slate-50 rounded-xl"><span className="text-xs font-bold text-brand w-24">{i.intent}</span><span className="text-sm text-ink-light font-medium">{i.desc}</span></div>
            ))}
          </div>
        )}
      </div>
    </motion.div>
  );
}

/* ================================================================== */
/*  CSV UPLOAD COMPONENT                                               */
/* ================================================================== */
function CsvUploader({ onRunTriggered }: { onRunTriggered: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string[][]>([]);
  const [dragOver, setDragOver] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [result, setResult] = useState<{ imported: number; skipped: any[] } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const parsePreview = (f: File) => {
    const reader = new FileReader();
    reader.onload = (e) => {
      const text = e.target?.result as string;
      const lines = text.split("\n").filter((l) => l.trim());
      setPreview(lines.slice(0, 6).map((l) => l.split(",").map((c) => c.trim().replace(/^"|"$/g, ""))));
    };
    reader.readAsText(f);
  };

  const handleFile = (f: File) => { if (!f.name.endsWith(".csv")) return; setFile(f); setResult(null); parsePreview(f); };
  const handleDrop = (e: React.DragEvent) => { e.preventDefault(); setDragOver(false); const f = e.dataTransfer.files[0]; if (f) handleFile(f); };

  const handleUploadAndRun = async () => {
    if (!file) return;
    setUploading(true);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const res = await fetch("/api/questions/import-csv", { method: "POST", body: formData });
      const data = await res.json();
      setResult(data);
      await api.createRun({ trigger: "ADHOC" });
      onRunTriggered();
    } catch {
      setResult({ imported: 0, skipped: [{ reason: "Upload failed" }] });
    } finally {
      setUploading(false);
    }
  };

  return (
    <Card accent>
      <div className="flex items-center gap-3 mb-5">
        <div className="w-10 h-10 rounded-xl bg-brand/10 flex items-center justify-center"><Upload size={20} className="text-brand" /></div>
        <div><h3 className="text-sm font-extrabold text-ink">Manual CSV Run</h3><p className="text-xs text-ink-light font-medium">Upload questions and run against all models</p></div>
      </div>
      <div onDragOver={(e) => { e.preventDefault(); setDragOver(true); }} onDragLeave={() => setDragOver(false)} onDrop={handleDrop} onClick={() => inputRef.current?.click()} className={`border-2 border-dashed rounded-2xl p-8 text-center cursor-pointer transition-all duration-200 ${dragOver ? "border-brand-light bg-brand-surface" : "border-slate-300 hover:border-brand-light/50 hover:bg-brand-surface/50"}`}>
        <input ref={inputRef} type="file" accept=".csv" className="hidden" onChange={(e) => e.target.files?.[0] && handleFile(e.target.files[0])} />
        <FileSpreadsheet size={32} className="mx-auto mb-3 text-brand-light" />
        {file ? (
          <div><p className="text-sm font-bold text-ink">{file.name}</p><p className="text-xs text-ink-light mt-1 font-medium">{(file.size / 1024).toFixed(1)} KB · {preview.length > 1 ? preview.length - 1 : 0} questions detected</p></div>
        ) : (
          <div><p className="text-sm font-bold text-ink">Drop CSV file here or click to browse</p><p className="text-xs text-ink-light mt-1 font-medium">Columns: question_text, persona, therapeutic_area, brand_focus, domain</p></div>
        )}
      </div>
      {preview.length > 1 && (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full text-xs"><thead><tr className="border-b border-slate-200">{preview[0].slice(0, 5).map((h, i) => <th key={i} className="pb-2 pr-4 text-left font-bold text-ink-light uppercase tracking-wide">{h}</th>)}</tr></thead>
            <tbody>{preview.slice(1, 4).map((row, i) => <tr key={i} className="border-b border-slate-100">{row.slice(0, 5).map((cell, j) => <td key={j} className="py-2 pr-4 text-ink font-medium max-w-[200px] truncate">{cell}</td>)}</tr>)}</tbody>
          </table>
          {preview.length > 4 && <p className="text-xs text-ink-muted mt-2 font-medium">+ {preview.length - 4} more rows</p>}
        </div>
      )}
      {result && <div className={`mt-4 p-3 rounded-xl text-sm font-medium ${result.imported > 0 ? "bg-teal-50 text-teal-800" : "bg-red-50 text-red-700"}`}>{result.imported > 0 ? `Imported ${result.imported} questions. Run triggered.` : `Import failed. ${result.skipped?.[0]?.reason || ""}`}</div>}
      {result && result.imported > 0 && (
        <Link to="/run-analysis/variations" className="mt-3 inline-flex items-center gap-1.5 text-xs font-bold text-brand hover:text-brand-dark">
          <Wand2 size={13} /> Test phrasing robustness: generate question variations
        </Link>
      )}
      <button disabled={!file || uploading} onClick={handleUploadAndRun} className="mt-4 w-full flex items-center justify-center gap-2 px-5 py-3 bg-brand text-white rounded-xl text-sm font-bold hover:bg-brand-dark disabled:opacity-40 transition-colors">
        {uploading ? <Spinner size={16} /> : <PlayCircle size={16} />}
        {uploading ? "Uploading & Running..." : "Upload & Run Pipeline"}
      </button>
    </Card>
  );
}

/* ================================================================== */
/*  SCHEDULE MANAGER                                                   */
/* ================================================================== */
function describeCron(cron: string): string {
  const parts = cron.trim().split(/\s+/);
  if (parts.length === 5) {
    const [m, h, dom, mon, dow] = parts;
    if (dom === "*" && mon === "*" && dow === "*" && /^\d+$/.test(m) && /^\d+$/.test(h)) {
      const hour = parseInt(h, 10);
      const min = parseInt(m, 10);
      const ampm = hour < 12 ? "AM" : "PM";
      const h12 = hour % 12 === 0 ? 12 : hour % 12;
      return `Daily at ${h12}:${String(min).padStart(2, "0")} ${ampm}`;
    }
  }
  return `Cron: ${cron}`;
}

function ScheduleManager() {
  const [sched, setSched] = useState<Schedule | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () => api.getSchedule().then(setSched).catch(() => setError("Could not load schedule")),
    [],
  );
  useEffect(() => { load(); }, [load]);

  const toggle = async () => {
    if (!sched) return;
    setBusy(true);
    setError(null);
    try {
      setSched(await api.updateSchedule({ enabled: !sched.enabled }));
    } catch {
      setError("Update failed");
    } finally {
      setBusy(false);
    }
  };

  const active = !!sched?.enabled;

  return (
    <Card accent>
      <div className="flex items-center gap-3 mb-5">
        <div className="w-10 h-10 rounded-xl bg-brand/10 flex items-center justify-center"><Calendar size={20} className="text-brand" /></div>
        <div><h3 className="text-sm font-extrabold text-ink">Scheduled Runs</h3><p className="text-xs text-ink-light font-medium">Unattended daily run on the server</p></div>
      </div>
      {!sched ? (
        <div className="py-8 flex justify-center"><Spinner size={20} /></div>
      ) : (
        <>
          <div className={`flex items-center justify-between p-4 rounded-xl border transition-colors ${active ? "border-brand-light/30 bg-brand-surface/50" : "border-slate-200 bg-slate-50"}`}>
            <div className="flex items-center gap-3">
              <Clock size={16} className={active ? "text-brand-light" : "text-ink-muted"} />
              <div><span className="text-sm font-bold text-ink">{describeCron(sched.cron)}</span><span className="text-xs text-ink-light ml-2 font-medium">{sched.timezone}</span></div>
            </div>
            <button disabled={busy} onClick={toggle} className={`flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-bold transition-colors disabled:opacity-50 ${active ? "bg-brand-light text-white" : "bg-slate-200 text-ink-light"}`}>
              {busy ? <Loader2 size={12} className="animate-spin" /> : active ? <CheckCircle2 size={12} /> : null}
              {active ? "Active" : "Paused"}
            </button>
          </div>
          <div className="mt-3 grid grid-cols-2 gap-3">
            <div className="p-3 rounded-xl bg-slate-50 border border-slate-200"><span className="block text-[10px] font-bold text-ink-light uppercase tracking-widest mb-1">Next run</span><span className="text-xs font-bold text-ink">{active && sched.next_run_at ? new Date(sched.next_run_at).toLocaleString() : "N/A"}</span></div>
            <div className="p-3 rounded-xl bg-slate-50 border border-slate-200"><span className="block text-[10px] font-bold text-ink-light uppercase tracking-widest mb-1">Last run</span><span className="text-xs font-bold text-ink">{sched.last_run_at ? new Date(sched.last_run_at).toLocaleString() : "Never"}</span></div>
          </div>
          <p className="text-xs text-ink-light mt-3 font-medium">Runs the full question bank across all enabled models. {active ? "Toggle to pause." : "Off by default: toggle to enable."}</p>
          {error && <p className="text-xs text-red-600 mt-2 font-semibold">{error}</p>}
        </>
      )}
    </Card>
  );
}

/* ================================================================== */
/*  EXECUTION THEATER                                                  */
/* ================================================================== */
function ExecutionTheater({ run }: { run: Run }) {
  const [progress, setProgress] = useState<RunProgress | null>(null);
  const total = run.questions_attempted || 1;

  // Poll the backend for real per-model progress (no client-side simulation).
  useEffect(() => {
    let active = true;
    const fetchProgress = () =>
      api.runProgress(run.run_id).then((p) => { if (active) setProgress(p); }).catch(() => {});
    fetchProgress();
    if (run.status !== "RUNNING") return () => { active = false; };
    const t = setInterval(fetchProgress, 2000);
    return () => { active = false; clearInterval(t); };
  }, [run.run_id, run.status]);

  // Backend keys per-model counts by DB llm_name (e.g. "claude", "gpt-4o"); the UI
  // labels use display names (e.g. "Claude", "GPT-4o"). Normalize to lowercase so the
  // real counts land on the right card.
  const byModel: Record<string, RunProgress["by_model"][string]> = {};
  Object.entries(progress?.by_model ?? {}).forEach(([k, v]) => { byModel[k.toLowerCase()] = v; });
  const enabledCount = MODELS.filter((m) => m.enabled && !m.providerOnly).length;
  const responsesDone = progress?.responses_done ?? (run.responses_success + run.responses_failed + run.responses_truncated + run.responses_blocked);
  const overallProgress = Math.round((responsesDone / Math.max(total * enabledCount, 1)) * 100);

  return (
    <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} className="bg-white rounded-2xl border-2 border-brand-light/30 shadow-lg overflow-hidden">
      <div className="bg-brand-surface px-6 py-4 border-b border-brand-light/20">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3"><div className="w-3 h-3 rounded-full bg-brand-light animate-processing" /><span className="text-sm font-extrabold text-ink">{run.status === "RUNNING" ? "Live run progress" : `Run ${run.status.toLowerCase()}`}</span><span className="text-xs text-ink-light font-medium">ID: {run.run_id.slice(0, 8)}...</span></div>
          <div className="flex items-center gap-4"><span className="text-xs font-bold text-ink-light">{responsesDone} / {total * enabledCount} responses</span><span className="text-xs font-bold text-brand">{overallProgress}%</span></div>
        </div>
        <div className="mt-3 h-2 bg-slate-200 rounded-full overflow-hidden"><motion.div className="h-full bg-brand-light rounded-full" initial={{ width: 0 }} animate={{ width: `${overallProgress}%` }} transition={{ duration: 0.5 }} /></div>
      </div>
      <div className="p-6">
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4 mb-6">
          {MODELS.map((model) => {
            const providerOnly = !!model.providerOnly;
            const done = byModel[model.name.toLowerCase()]?.done ?? 0;
            const pct = Math.round((done / total) * 100);
            const isDisabled = !model.enabled;
            const isDone = !isDisabled && !providerOnly && done >= total && run.status !== "RUNNING";
            const isActive = !isDisabled && !isDone && run.status === "RUNNING" && (providerOnly || done < total);
            return (
              <motion.div key={model.name} className={`rounded-2xl border-2 p-4 transition-all ${isDisabled ? "border-dashed border-slate-200 bg-slate-50 opacity-50" : isActive ? "border-brand-light/50 bg-brand-surface shadow-md" : isDone ? "border-teal-300 bg-teal-50" : "border-slate-200 bg-white"}`} whileHover={!isDisabled ? { y: -2 } : undefined}>
                <div className="flex items-center gap-2 mb-3">
                  {isActive ? <Loader2 size={16} className="text-brand-light animate-spin" /> : isDone ? <CheckCircle2 size={16} className="text-teal-600" /> : isDisabled ? <div className="w-4 h-4 rounded-full bg-slate-300" /> : <div className="w-4 h-4 rounded-full" style={{ backgroundColor: model.color }} />}
                  <span className="text-xs font-extrabold text-ink">{model.name}</span>
                </div>
                <div className="text-xs text-ink-light font-medium mb-2">{model.provider}</div>
                {isDisabled ? (
                  <span className="text-[10px] font-bold text-ink-muted uppercase tracking-wide">Placeholder</span>
                ) : providerOnly ? (
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] font-bold uppercase tracking-wide" style={{ color: model.color }}>Provider only</span>
                    {done > 0 && <span className="text-[11px] font-bold text-ink">{done} answered</span>}
                  </div>
                ) : (
                  <><div className="h-1.5 bg-slate-200 rounded-full overflow-hidden mb-2"><motion.div className="h-full rounded-full" style={{ backgroundColor: model.color }} initial={{ width: 0 }} animate={{ width: `${pct}%` }} transition={{ duration: 0.8, ease: "easeOut" }} /></div><div className="flex justify-between"><span className="text-[11px] font-bold text-ink">{done}/{total}</span><span className="text-[11px] font-bold" style={{ color: model.color }}>{pct}%</span></div></>
                )}
              </motion.div>
            );
          })}
        </div>
      </div>
    </motion.div>
  );
}

/* ================================================================== */
/*  MAIN PIPELINE PAGE                                                 */
/* ================================================================== */
export default function Pipeline() {
  const navigate = useNavigate();
  const [runs, setRuns] = useState<Run[]>([]);
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [expandedRun, setExpandedRun] = useState<string | null>(null);
  const [rerunningId, setRerunningId] = useState<string | null>(null);
  const [resumingId, setResumingId] = useState<string | null>(null);
  const [retryingId, setRetryingId] = useState<string | null>(null);
  // A deploy replaces the container and kills whatever is running, so the server refuses
  // to start runs while one is staging. Surfaced here so the operator sees WHY before
  // clicking, not as a failed request afterwards.
  const [deploying, setDeploying] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [filters, setFilters] = useState({ persona: "", therapeutic_area: "", domain: "", monitoring_mode: "" });
  const [selectedNode, setSelectedNode] = useState<PipelineNode | null>(null);
  const [activeIndex, setActiveIndex] = useState(-1);
  const prevRunStatuses = useRef<Record<string, string>>({});

  const load = useCallback(() => {
    return Promise.all([
      api.runs().then(setRuns).catch(() => {}),
      api.deployStatus().then((d) => setDeploying(d.deploying)).catch(() => {}),
    ]);
  }, []);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    const started = Date.now();
    try {
      await load();
    } finally {
      // Keep the spinner visible briefly so a manual click is always acknowledged,
      // even though the list also auto-polls every 3s.
      const elapsed = Date.now() - started;
      if (elapsed < 500) await new Promise((r) => setTimeout(r, 500 - elapsed));
      setRefreshing(false);
    }
  }, [load]);

  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [load]);

  // Auto-navigate to results when a run completes
  useEffect(() => {
    for (const r of runs) {
      const prev = prevRunStatuses.current[r.run_id];
      if (prev === "RUNNING" && r.status === "COMPLETED") {
        navigate(`/results?run_id=${r.run_id}`);
        break;
      }
    }
    const next: Record<string, string> = {};
    for (const r of runs) next[r.run_id] = r.status;
    prevRunStatuses.current = next;
  }, [runs, navigate]);

  const activeRun = runs.find((r) => r.status === "RUNNING");
  const isRunning = !!activeRun;

  // Drive pipeline stage from REAL run progress (no client-side simulation): map the
  // actual fraction of completed responses onto the pipeline stages so the highlighted
  // node reflects genuine execution state, advancing as the backend persists responses.
  useEffect(() => {
    if (!activeRun) { setActiveIndex(-1); return; }
    const enabledCount = MODELS.filter((m) => m.enabled).length;
    const totalExpected = Math.max((activeRun.questions_attempted || 1) * enabledCount, 1);
    const done = activeRun.responses_success + activeRun.responses_failed + activeRun.responses_truncated + activeRun.responses_blocked;
    const frac = Math.min(done / totalExpected, 1);
    setActiveIndex(Math.min(NODES.length - 1, Math.floor(frac * NODES.length)));
  }, [activeRun]);

  const trigger = async (dry: boolean) => {
    if (!filters.monitoring_mode) return;  // mandatory: force an AbbVie / All Brands choice first
    setBusy(true);
    setActionError(null);
    try {
      const body = { trigger: "ADHOC" as const, persona: filters.persona || null, therapeutic_area: filters.therapeutic_area || null, domain: filters.domain || null, monitoring_mode: filters.monitoring_mode, dry_run: dry };
      if (dry) await api.dryRun(body); else await api.createRun(body);
      await load();
    } catch (e) {
      // A refusal (e.g. a deploy is staging) has to reach the operator who clicked Run,
      // rather than leaving them watching a run history that never gains a row.
      setActionError(e instanceof Error ? e.message : String(e));
    } finally { setBusy(false); }
  };

  const cancel = async (runId: string) => { try { await api.cancelRun(runId); await load(); } catch { /* done */ } };

  // Re-run a previous run's exact question set + filters without reselecting questions.
  // The backend rebuilds the request from the original run's config_snapshot. This is a
  // FRESH run: every response is paid for again.
  const rerun = async (runId: string) => {
    setRerunningId(runId);
    setActionError(null);
    try { await api.rerunRun(runId); await load(); }
    catch (e) { setActionError(e instanceof Error ? e.message : String(e)); }
    finally { setRerunningId(null); }
  };

  // Continue an interrupted run IN PLACE. Unlike Re-run this keeps the same run_id, so the
  // backend skips every (question, target) pair that already has a response and dispatches
  // only the remainder — the work already paid for is kept.
  const resume = async (runId: string) => {
    setResumingId(runId);
    setActionError(null);
    try { await api.resumeRun(runId); await load(); }
    catch (e) { setActionError(e instanceof Error ? e.message : String(e)); }
    finally { setResumingId(null); }
  };

  // Re-attempt only the responses that errored, in place. The backend drops those rows
  // first (they hold an error string, never an answer) so the pairs are dispatched again;
  // everything that succeeded is untouched and is not paid for twice.
  const retryFailed = async (runId: string) => {
    setRetryingId(runId);
    setActionError(null);
    try { await api.retryFailedRun(runId); await load(); }
    catch (e) { setActionError(e instanceof Error ? e.message : String(e)); }
    finally { setRetryingId(null); }
  };

  return (
    <div className="space-y-8">
      <PageHeader title="Run Analysis" subtitle="See how AI platforms respond to your most important patient questions, on demand or on a schedule." />

      {/* ── Deploy in progress ──
          A deploy ends by replacing the container, which kills anything in flight. The app
          keeps serving for the whole build, so without this banner there is nothing to stop
          an operator starting an hour-long run seconds before it is destroyed. */}
      {deploying && (
        <div className="rounded-2xl border-2 border-amber-300 bg-amber-50 px-5 py-4 flex items-start gap-3">
          <Hammer size={18} className="text-amber-700 mt-0.5 shrink-0" />
          <div>
            <p className="text-sm font-bold text-amber-900">Deployment in progress — runs are paused</p>
            <p className="mt-1 text-xs font-medium text-amber-800">
              This server is being updated. A run started now would be stopped when the new
              version swaps in, so new runs are blocked for a few minutes. Runs already
              finished are unaffected.
            </p>
          </div>
        </div>
      )}

      {/* A refusal from the server (deploy staging, run not resumable) shown verbatim. */}
      {actionError && (
        <div className="rounded-2xl border-2 border-red-300 bg-red-50 px-5 py-4 flex items-start gap-3">
          <AlertTriangle size={18} className="text-red-700 mt-0.5 shrink-0" />
          <p className="text-xs font-semibold text-red-800 flex-1">{actionError}</p>
          <button onClick={() => setActionError(null)} className="text-red-700 hover:text-red-900" aria-label="Dismiss"><X size={16} /></button>
        </div>
      )}

      {/* ── Run Controls ── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <CsvUploader onRunTriggered={load} />
        <ScheduleManager />
      </div>

      {/* ── Quick Run ── */}
      <Card title="Quick Run from Question Bank">
        <div className="flex gap-4 flex-wrap items-end">
          <div className="flex flex-col">
            <label className="text-xs font-semibold text-ink-light mb-1.5 uppercase tracking-wide flex items-center gap-1">
              <InfoTooltip content={"Required. AbbVie: monitor a single AbbVie brand.\nAll Brands: pre-launch / landscape run with no AbbVie brand; scores the whole competitor field."} />
              Monitoring Mode
            </label>
            <select
              value={filters.monitoring_mode}
              onChange={(e) => setFilters({ ...filters, monitoring_mode: e.target.value })}
              className={`border rounded-xl px-3 py-2 text-sm bg-canvas-card text-ink font-medium focus:outline-none focus:ring-2 focus:ring-brand-light/40 focus:border-brand-light transition-colors ${filters.monitoring_mode ? "border-line" : "border-amber-300"}`}
            >
              <option value="" disabled>Select…</option>
              <option value="BRAND">AbbVie</option>
              <option value="DISEASE_STATE">All Brands</option>
            </select>
          </div>
          <Select label="Persona" value={filters.persona} options={["", "Prospect", "Patient", "Provider"]} onChange={(v) => setFilters({ ...filters, persona: v })} tooltip={"Who is asking the question:\n• Prospect: exploring treatment options\n• Patient: currently on treatment\n• Provider: a clinician"} />
          <Select label="Therapeutic Area & Indication" value={filters.therapeutic_area} groups={TA_GROUPS} onChange={(v) => setFilters({ ...filters, therapeutic_area: v })} tooltip={"The disease area or specific indication this question targets.\nExamples: Dermatology, Gastroenterology, Oncology, Endometriosis."} />
          <Select label="Theme" value={filters.domain} options={["", "Efficacy", "Safety", "Access", "Comparative", "General"]} onChange={(v) => setFilters({ ...filters, domain: v })} tooltip={"The theme of the question:\n• Efficacy: treatment outcomes & dosing\n• Safety: side effects & risks\n• Access: coverage & cost\n• Comparative: vs. competitors\n• General: broad or exploratory"} />
          <button disabled={busy || deploying || !filters.monitoring_mode} onClick={() => trigger(false)} title={deploying ? "A deployment is in progress — a run started now would be stopped" : undefined} className="flex items-center gap-2 px-5 py-2.5 bg-brand text-white rounded-xl text-sm font-bold hover:bg-brand-dark disabled:opacity-40 transition-colors"><PlayCircle size={16} /> Run Now</button>
        </div>
        {!filters.monitoring_mode && (
          <p className="mt-2 text-xs font-medium text-amber-700">Choose a Monitoring Mode (AbbVie or All Brands) to run.</p>
        )}
        {filters.monitoring_mode === "DISEASE_STATE" && (
          <div className="mt-4 rounded-xl border-2 border-violet-300 bg-violet-50 px-4 py-3">
            <p className="text-xs font-extrabold uppercase tracking-wide text-violet-800">{PRELAUNCH_LABEL}</p>
            <p className="mt-1 text-xs font-medium text-violet-700">This run covers all brands: it executes the brand-less landscape question set and scores every competitor rather than a single AbbVie brand.</p>
          </div>
        )}
      </Card>

      {/* ── Active Run: Pipeline Visualization + Execution Theater ── */}
      {isRunning && (
        <>
          <motion.div className="rounded-2xl p-5 border-2 bg-brand-surface border-brand-light/40" layout>
            <div className="flex items-center gap-4">
              <div className="w-3 h-3 rounded-full bg-brand-light animate-processing" />
              <span className="text-sm font-bold text-ink">Run in progress: {activeRun.questions_attempted} questions</span>
              <span className="text-xs text-ink-light font-medium">{activeRun.responses_success} completed · {activeRun.responses_failed} failed</span>
            </div>
          </motion.div>

          <div className="bg-white rounded-2xl border border-slate-200 p-6 shadow-sm">
            <div className="flex flex-wrap items-center justify-center gap-y-4">
              {NODES.map((node, i) => (
                <div key={node.id} className="flex items-center">
                  <NodeCard node={node} index={i} activeIndex={activeIndex} isRunning={isRunning} onSelect={setSelectedNode} />
                  {i < NODES.length - 1 && <Connector active={isRunning && i < activeIndex} />}
                </div>
              ))}
            </div>
          </div>

          <ExecutionTheater run={activeRun} />
        </>
      )}

      {/* ── Run History ── */}
      <Card title="Run History">
        <div className="flex items-center justify-end mb-4">
          <button onClick={refresh} disabled={refreshing} className="flex items-center gap-2 text-xs font-bold text-ink-light hover:text-brand transition-colors disabled:opacity-60"><RefreshCw size={14} className={refreshing ? "animate-spin" : ""} /> {refreshing ? "Refreshing…" : "Refresh"}</button>
        </div>
        {runs.length === 0 ? (
          <div className="py-12 text-center text-ink-light font-medium">No runs yet. Upload a CSV or trigger a run above.</div>
        ) : (
          <div className="space-y-2">
            {runs.map((r) => {
              const style = STATUS_STYLES[r.status] || STATUS_STYLES.CANCELLED;
              const StatusIcon = style.icon;
              const isExpanded = expandedRun === r.run_id;
              return (
                <div key={r.run_id} className="border border-slate-200 rounded-xl overflow-hidden hover:border-brand-light/30 transition-colors">
                  <button onClick={() => setExpandedRun(isExpanded ? null : r.run_id)} className="w-full flex items-center justify-between p-4 text-left">
                    <div className="flex items-center gap-4">
                      <span className="inline-flex items-center gap-1">
                        <span className={`flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-bold ${style.bg}`}><StatusIcon size={12} className={r.status === "RUNNING" ? "animate-spin" : ""} />{statusLabel(r.status)}</span>
                        {r.status === "PAUSED_BUDGET" && <InfoTooltip content="Run stopped automatically because the estimated API cost exceeded the configured budget limit." />}
                      </span>
                      <span className="text-sm font-bold text-ink">{triggerLabel(r.trigger)}</span>
                      <span className="text-xs text-ink-light font-medium">{new Date(r.started_at).toLocaleString()}</span>
                    </div>
                    <div className="flex items-center gap-6">
                      <div className="flex items-center gap-4 text-xs font-bold">
                        <span className="text-ink">{r.questions_attempted} Q</span>
                        <span className="text-teal-700">{r.responses_success} ok</span>
                        <span className="text-red-600">{r.responses_failed} fail</span>
                      </div>
                      {r.status === "RUNNING" ? (
                        <button onClick={(e) => { e.stopPropagation(); cancel(r.run_id); }} className="flex items-center gap-1 px-3 py-1.5 bg-red-100 text-red-700 rounded-lg text-xs font-bold hover:bg-red-200 transition-colors"><Square size={12} /> Cancel</button>
                      ) : (
                        // Wraps because a stopped run with failures carries four actions
                        // (Resume, Retry, Re-run, View Results) and they must not spill
                        // out of the row on a narrow window.
                        <div className="flex items-center justify-end gap-2 flex-wrap">
                          {/* Resume continues THIS run (same run_id): only the questions with no
                              stored response are dispatched, so the responses already paid for
                              are kept. Offered for the three recoverable stops: a run killed by a
                              deploy/crash (FAILED), one that hit the token ceiling, and one an
                              operator stopped. */}
                          {isResumable(r) && (
                            <button
                              onClick={(e) => { e.stopPropagation(); resume(r.run_id); }}
                              disabled={resumingId === r.run_id || deploying}
                              title={deploying
                                ? "A deployment is in progress. Try again in a few minutes."
                                : r.status === "CANCELLED"
                                  ? `You stopped this run. Continue it from the ${capturedCount(r)} response(s) already captured (they are not paid for again).`
                                  : `Continue this run from the ${capturedCount(r)} response(s) already captured (does not re-pay for them)`}
                              className="flex items-center gap-1.5 px-3 py-1.5 bg-teal-100 text-teal-800 rounded-lg text-xs font-bold hover:bg-teal-200 disabled:opacity-50 transition-colors"
                            >
                              <PlayCircle size={12} className={resumingId === r.run_id ? "animate-spin" : ""} /> {resumingId === r.run_id ? "Resuming…" : "Resume"}
                            </button>
                          )}
                          {/* Re-attempts only the responses that errored (timeout, rate limit,
                              provider fault), in place under the same run_id. */}
                          {canRetryFailed(r) && (
                            <button
                              onClick={(e) => { e.stopPropagation(); retryFailed(r.run_id); }}
                              disabled={retryingId === r.run_id || deploying}
                              title={deploying
                                ? "A deployment is in progress. Try again in a few minutes."
                                : `Ask the same models the ${r.responses_failed} question(s) that errored, in this same run. Answers that succeeded are kept and not paid for again.`}
                              className="flex items-center gap-1.5 px-3 py-1.5 bg-amber-100 text-amber-800 rounded-lg text-xs font-bold hover:bg-amber-200 disabled:opacity-50 transition-colors"
                            >
                              <Repeat size={12} className={retryingId === r.run_id ? "animate-spin" : ""} /> {retryingId === r.run_id ? "Retrying…" : `Retry ${r.responses_failed} failed`}
                            </button>
                          )}
                          <button
                            onClick={(e) => { e.stopPropagation(); rerun(r.run_id); }}
                            disabled={rerunningId === r.run_id || deploying}
                            title="Start a NEW run with this run's exact questions and filters (every response is paid for again)"
                            className="flex items-center gap-1.5 px-3 py-1.5 bg-slate-100 text-ink rounded-lg text-xs font-bold hover:bg-slate-200 disabled:opacity-50 transition-colors"
                          >
                            <RotateCcw size={12} className={rerunningId === r.run_id ? "animate-spin" : ""} /> {rerunningId === r.run_id ? "Starting…" : "Re-run"}
                          </button>
                          {(r.status === "COMPLETED" || r.status === "CANCELLED") && (
                            <button onClick={(e) => { e.stopPropagation(); navigate(`/results?run_id=${r.run_id}`); }} className="flex items-center gap-1.5 px-3 py-1.5 bg-brand text-white rounded-lg text-xs font-bold hover:bg-brand-dark transition-colors"><ExternalLink size={12} /> View Results</button>
                          )}
                          {isExpanded ? <ChevronUp size={16} className="text-ink-muted" /> : <ChevronDown size={16} className="text-ink-muted" />}
                        </div>
                      )}
                    </div>
                  </button>
                  <AnimatePresence>
                    {isExpanded && (
                      <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.2 }} className="overflow-hidden">
                        <div className="px-4 pb-4 pt-0 grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4 border-t border-slate-100">
                          <div className="pt-4"><span className="text-[10px] font-bold text-ink-light uppercase tracking-widest">Cost</span><p className="text-sm font-extrabold text-ink mt-1">${r.estimated_cost_usd.toFixed(4)}</p></div>
                          <div className="pt-4"><span className="text-[10px] font-bold text-ink-light uppercase tracking-widest">Alerts</span><p className="text-sm font-extrabold text-ink mt-1">{r.alerts_triggered}</p></div>
                          <div className="pt-4"><span className="text-[10px] font-bold text-ink-light uppercase tracking-widest">Truncated</span><p className="text-sm font-extrabold text-ink mt-1">{r.responses_truncated}</p></div>
                          <div className="pt-4"><span className="text-[10px] font-bold text-ink-light uppercase tracking-widest">Blocked</span><p className="text-sm font-extrabold text-ink mt-1">{r.responses_blocked}</p></div>
                          <div className="pt-4 col-span-2"><span className="text-[10px] font-bold text-ink-light uppercase tracking-widest">Consensus</span><div className="flex items-center gap-3 mt-1"><span className="text-sm font-extrabold text-teal-700">{r.consensus_full} Full</span><span className="text-sm font-extrabold text-amber-700">{r.consensus_partial} Partial</span><span className="text-sm font-extrabold text-red-600">{r.consensus_missing} Missing</span></div></div>
                          {/* The reason the run ended as it did. Previously this lived only in
                              the database, so a failed run was an unexplained red chip. */}
                          {r.notes && (
                            <div className="pt-4 col-span-2 md:col-span-3 lg:col-span-6">
                              <span className="text-[10px] font-bold text-ink-light uppercase tracking-widest">Details</span>
                              <p className="text-xs font-medium text-ink-light mt-1 break-words">{r.notes}</p>
                            </div>
                          )}
                        </div>
                      </motion.div>
                    )}
                  </AnimatePresence>
                </div>
              );
            })}
          </div>
        )}
      </Card>

      {/* ── Pipeline detail panel ── */}
      <AnimatePresence>
        {selectedNode && (
          <>
            <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="fixed inset-0 bg-black/20 z-40" onClick={() => setSelectedNode(null)} />
            <DetailPanel node={selectedNode} onClose={() => setSelectedNode(null)} />
          </>
        )}
      </AnimatePresence>
    </div>
  );
}
