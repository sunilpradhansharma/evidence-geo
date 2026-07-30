import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Bar, BarChart, CartesianGrid, LabelList, Legend, Line, LineChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import {
  ArrowUpRight, Bell, Building2, Check, Download, FileSpreadsheet, Layers, RefreshCw,
  Search, ShieldAlert, Sparkles, Target, TrendingUp, Upload, Wand2, X,
  type LucideIcon,
} from "lucide-react";
import {
  api, PromptVolumeBatch, PromptVolumeGapAlert, PromptVolumeGapAlertSummary,
  PromptVolumeGapTopic, PromptVolumeIntelligence,
  PromptVolumePersonaVolume, PromptVolumeShareOfDemand, PromptVolumeTrend,
  SemrushPreview, SemrushStatus,
} from "../api/client";
import { AREA_OPTIONS, indicationsForArea } from "../lib/taxonomy";
import { AnimatedCard, Card, EmptyState, PageHeader, Spinner, Stat } from "../components/ui";

// Question/PAA tools first (real questions people ask → the strongest signal), then
// keyword-volume tools (a search-demand proxy that yields auto-generated questions).
const SOURCE_TOOLS = ["AlsoAsked", "AnswerThePublic", "Semrush", "Other"];
const INPUT_CLS =
  "border border-line rounded-xl px-3 py-2 text-sm bg-canvas-card text-ink font-medium focus:outline-none focus:ring-2 focus:ring-brand-light/40 focus:border-brand-light transition-colors";
const CHART_TEAL = "#0D9488";
const CHART_ROSE = "#F43F5E";

const fmt = (n: number) => (n ?? 0).toLocaleString();
const today = () => new Date().toISOString().slice(0, 10);
// Compact axis-free labels (e.g. 3.8M, 18K) so bars stay readable when values span orders of magnitude.
const fmtCompact = (n: number) => {
  const v = n ?? 0;
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(v >= 10_000_000 ? 0 : 1)}M`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(v >= 10_000 ? 0 : 1)}K`;
  return `${v}`;
};

// Pre-fill payload handed to the Question Bank when "Create question" is clicked on a gap.
type GapDraft = {
  question_text: string;
  therapeutic_area?: string | null;
  competitor?: string | null;
  brand_focus?: string;
  question_origin?: "prompt" | "synthesized" | "keyword";
};

// Provenance of a gap's monitorable question, shown as a badge: the real thing people asked,
// machine-written from a keyword, or a raw keyword left as-is (synthesis off).
const ORIGIN_META: Record<string, { label: string; cls: string; Icon: LucideIcon; title: string }> = {
  prompt: { label: "Real question", cls: "bg-emerald-100 text-emerald-700", Icon: Check, title: "Captured verbatim from a real question / prompt export." },
  synthesized: { label: "AI-generated", cls: "bg-violet-100 text-violet-700", Icon: Wand2, title: "Auto-generated from a keyword. Turn off synthesis at upload to keep raw keywords instead." },
  keyword: { label: "From keyword", cls: "bg-amber-100 text-amber-800", Icon: FileSpreadsheet, title: "Raw keyword used as-is (synthesis was off at upload)." },
};
function OriginBadge({ origin }: { origin?: string }) {
  const meta = origin ? ORIGIN_META[origin] : undefined;
  if (!meta) return null;
  const { label, cls, Icon, title } = meta;
  return (
    <span title={title} className={`inline-flex items-center gap-1 rounded-full ${cls} px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide shrink-0`}>
      <Icon size={10} /> {label}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/*  Upload panel                                                              */
/* -------------------------------------------------------------------------- */
function Uploader({ onUploaded }: { onUploaded: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [sourceTool, setSourceTool] = useState("");
  const [sourceLabel, setSourceLabel] = useState("");
  const [datasetDate, setDatasetDate] = useState("");
  const [synthesize, setSynthesize] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; status: number; data: any } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const canSubmit = !!file && !!sourceTool && sourceLabel.trim().length > 0 && !!datasetDate && !uploading;

  const handleFile = (f: File) => {
    if (!f.name.toLowerCase().endsWith(".csv")) return;
    setFile(f);
    setResult(null);
  };

  const submit = async () => {
    if (!file) return;
    setUploading(true);
    setResult(null);
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("source_tool", sourceTool);
      form.append("source_label", sourceLabel.trim());
      form.append("dataset_date", datasetDate);
      form.append("synthesize_questions", String(synthesize));
      const res = await api.uploadPromptVolume(form);
      setResult(res);
      if (res.ok) {
        setFile(null);
        onUploaded();
      }
    } catch {
      setResult({ ok: false, status: 0, data: { detail: "Upload failed: could not reach the server." } });
    } finally {
      setUploading(false);
    }
  };

  const piiHits: any[] = result && !result.ok && result.data?.detail?.pii_hits ? result.data.detail.pii_hits : [];
  const errorText =
    result && !result.ok
      ? typeof result.data?.detail === "string"
        ? result.data.detail
        : result.data?.detail?.message || "Upload rejected."
      : "";

  return (
    <Card accent>
      <div className="flex items-center gap-3 mb-5">
        <div className="w-10 h-10 rounded-xl bg-brand/10 flex items-center justify-center">
          <Upload size={20} className="text-brand" />
        </div>
        <div>
          <h3 className="text-sm font-extrabold text-ink">Upload demand CSV</h3>
          <p className="text-xs text-ink-light font-medium">
            A question / prompt export from a research tool, or a keyword export from an SEO tool. PII is rejected before anything is stored.
          </p>
        </div>
      </div>

      {/* Nudge toward question-shaped data: real prompts beat auto-generated ones. */}
      <div className="mb-4 flex items-start gap-2.5 rounded-xl border border-brand-light/30 bg-brand-surface/50 px-3 py-2.5">
        <Wand2 size={15} className="mt-0.5 shrink-0 text-brand" />
        <p className="text-[11px] font-medium leading-relaxed text-ink-light">
          <b className="text-ink">Best input: a question / prompt export</b> (AlsoAsked, AnswerThePublic, or the Semrush <i>Questions</i> report) — a <code className="rounded bg-white/70 px-1 font-mono">prompt</code>/<code className="rounded bg-white/70 px-1 font-mono">question</code> column lets you monitor the <b>exact questions</b> people ask. A bare <code className="rounded bg-white/70 px-1 font-mono">keyword</code> export still works, but each gap question is then <b>auto-generated</b> from the keyword.
        </p>
      </div>

      <div
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => { e.preventDefault(); setDragOver(false); const f = e.dataTransfer.files[0]; if (f) handleFile(f); }}
        onClick={() => inputRef.current?.click()}
        className={`border-2 border-dashed rounded-2xl p-8 text-center cursor-pointer transition-all duration-200 ${
          dragOver ? "border-brand-light bg-brand-surface" : "border-slate-300 hover:border-brand-light/50 hover:bg-brand-surface/50"
        }`}
      >
        <input ref={inputRef} type="file" accept=".csv" className="hidden"
               onChange={(e) => e.target.files?.[0] && handleFile(e.target.files[0])} />
        <FileSpreadsheet size={32} className="mx-auto mb-3 text-brand-light" />
        {file ? (
          <div>
            <p className="text-sm font-bold text-ink">{file.name}</p>
            <p className="text-xs text-ink-light mt-1 font-medium">{(file.size / 1024).toFixed(1)} KB</p>
          </div>
        ) : (
          <div>
            <p className="text-sm font-bold text-ink">Drop CSV file here or click to browse</p>
            <p className="text-xs text-ink-light mt-1 font-medium">Accepted: a keyword/query OR a prompt/question column. Search volume is optional (+ optional KD, CPC) — without it, demand is estimated from how often each prompt recurs.</p>
          </div>
        )}
      </div>

      {/* Required source + date metadata (FR-116.5) */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mt-4">
        <div className="flex flex-col">
          <label className="text-xs font-semibold text-ink-light mb-1.5 uppercase tracking-wide">Source tool *</label>
          <select className={INPUT_CLS} value={sourceTool} onChange={(e) => setSourceTool(e.target.value)}>
            <option value="" disabled>Select tool…</option>
            {SOURCE_TOOLS.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>
        <div className="flex flex-col">
          <label className="text-xs font-semibold text-ink-light mb-1.5 uppercase tracking-wide">Dataset label *</label>
          <input className={INPUT_CLS} type="text" placeholder="e.g. RA keywords Q3"
                 value={sourceLabel} onChange={(e) => setSourceLabel(e.target.value)} />
        </div>
        <div className="flex flex-col">
          <label className="text-xs font-semibold text-ink-light mb-1.5 uppercase tracking-wide">Dataset date *</label>
          <input className={INPUT_CLS} type="date" value={datasetDate} onChange={(e) => setDatasetDate(e.target.value)} />
        </div>
      </div>

      {/* Synthesis choice (FR-116): auto-generate questions from bare keywords? */}
      <label className="mt-4 flex items-start gap-2.5 cursor-pointer select-none">
        <input
          type="checkbox"
          checked={synthesize}
          onChange={(e) => setSynthesize(e.target.checked)}
          className="mt-0.5 h-4 w-4 rounded border-slate-300 text-brand focus:ring-2 focus:ring-brand-light/40"
        />
        <span className="text-[11px] font-medium leading-relaxed text-ink-light">
          <b className="text-ink">Auto-generate questions from keywords</b> (recommended). When a row has no real
          question, we write a natural one from the keyword (labeled <i>AI-generated</i>). Uncheck to keep the raw
          keyword as-is (labeled <i>From keyword</i>). Rows that already include a real question are always kept verbatim.
        </span>
      </label>

      {/* Result / rejection banners */}
      {result?.ok && (
        <div className="mt-4 p-3 rounded-xl text-sm font-medium bg-teal-50 text-teal-800">
          Ingested <b>{fmt(result.data.rows_ingested)}</b> queries · flagged <b>{fmt(result.data.gap_topics_flagged)}</b> high-volume gap topic(s).
        </div>
      )}
      {result && !result.ok && (
        <div className="mt-4 p-3 rounded-xl bg-red-50 text-red-700">
          <div className="flex items-center gap-2 text-sm font-bold">
            <ShieldAlert size={16} /> {piiHits.length ? "Upload rejected: PII detected" : "Upload rejected"}
          </div>
          <p className="text-xs mt-1 font-medium">{errorText} Nothing was stored.</p>
          {piiHits.length > 0 && (
            <ul className="mt-2 text-xs font-medium list-disc pl-5 max-h-32 overflow-y-auto">
              {piiHits.slice(0, 8).map((h, i) => (
                <li key={i}>Row {h.row}, column “{h.column}”: {(h.categories || []).join(", ")}</li>
              ))}
              {piiHits.length > 8 && <li>+ {piiHits.length - 8} more…</li>}
            </ul>
          )}
        </div>
      )}

      <button
        disabled={!canSubmit}
        onClick={submit}
        className="mt-4 w-full flex items-center justify-center gap-2 px-5 py-3 bg-brand text-white rounded-xl text-sm font-bold hover:bg-brand-dark disabled:opacity-40 transition-colors"
      >
        {uploading ? <Spinner size={16} /> : <Upload size={16} />}
        {uploading ? "Uploading & analyzing…" : "Upload & analyze"}
      </button>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/*  Fetch from SEMrush (in-app discovery)                                     */
/* -------------------------------------------------------------------------- */
function ReportBadge({ report }: { report?: string | null }) {
  const q = report === "questions";
  return (
    <span
      title={q ? "A real question people search for" : "A keyword phrase people search for"}
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide shrink-0 ${
        q ? "bg-emerald-100 text-emerald-700" : "bg-slate-100 text-slate-600"
      }`}
    >
      {q ? "Question" : "Keyword"}
    </span>
  );
}

function SummaryTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-line bg-canvas-card px-3 py-2.5">
      <div className="text-lg font-bold tabular-nums text-ink">{value}</div>
      <div className="text-[10px] uppercase tracking-wide text-ink-muted font-semibold">{label}</div>
    </div>
  );
}

// The SEMrush reports the analyst can pull: real questions, related keywords, or both.
const REPORT_OPTIONS: { value: "questions" | "related" | "both"; label: string }[] = [
  { value: "questions", label: "Questions" },
  { value: "related", label: "Keywords" },
  { value: "both", label: "Both" },
];

// Fetch questions + related keywords straight from the SEMrush Analytics API for a chosen
// scope, preview the pulled demand (with a redundancy indicator), then ingest the full
// snapshot as a Prompt Volume dataset — the CSV-free doorway alongside the uploader.
function SemrushFetch({ onUploaded, status }: { onUploaded: () => void; status: SemrushStatus | null }) {
  const [area, setArea] = useState("");
  const [brand, setBrand] = useState("");
  const [incGenerics, setIncGenerics] = useState(true);
  const [incIndications, setIncIndications] = useState(true);
  const [incCompetitors, setIncCompetitors] = useState(true);
  const [reports, setReports] = useState<"questions" | "related" | "both">((status?.reports as "questions" | "related" | "both") || "both");
  const [perSeed, setPerSeed] = useState(status?.per_seed_limit ?? 25);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<SemrushPreview | null>(null);
  const [sourceLabel, setSourceLabel] = useState("");
  const [datasetDate, setDatasetDate] = useState(today());
  const [synthesize, setSynthesize] = useState(true);
  const [onlyNew, setOnlyNew] = useState(false);
  const [limitOn, setLimitOn] = useState(false);
  const [limitVal, setLimitVal] = useState(100);
  const [ingesting, setIngesting] = useState(false);
  const [ingestMsg, setIngestMsg] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => { if (status?.per_seed_limit) setPerSeed(status.per_seed_limit); }, [status?.per_seed_limit]);
  useEffect(() => { if (status?.reports) setReports(status.reports as "questions" | "related" | "both"); }, [status?.reports]);

  const areaBrands = area ? Array.from(new Set(indicationsForArea(area).flatMap((i) => i.brands))) : [];

  const runPreview = async () => {
    if (!area) return;
    setLoading(true); setError(""); setPreview(null); setIngestMsg(null);
    try {
      const res = await api.promptVolumeSemrushPreview({
        therapeutic_area: area, brand: brand || null,
        include_generics: incGenerics, include_indications: incIndications,
        include_competitors: incCompetitors, per_seed_limit: perSeed, reports,
      });
      if (!res.ok) {
        setError(typeof res.data?.detail === "string" ? res.data.detail : "We couldn't look up demand right now. Please try again.");
        return;
      }
      setPreview(res.data as SemrushPreview);
      setSourceLabel(`${area}${brand ? ` · ${brand}` : ""} (Live search demand)`);
      setDatasetDate(today());
    } catch {
      setError("Could not reach the server.");
    } finally {
      setLoading(false);
    }
  };

  const runIngest = async () => {
    if (!preview?.fetch_id) return;
    setIngesting(true); setIngestMsg(null);
    try {
      const res = await api.promptVolumeSemrushIngest({
        fetch_id: preview.fetch_id,
        source_label: sourceLabel.trim() || `${area} (Live search demand)`,
        dataset_date: datasetDate, synthesize,
        only_new: onlyNew,
        limit: limitOn ? Math.max(1, limitVal) : null,
      });
      if (!res.ok) {
        setIngestMsg({ ok: false, text: typeof res.data?.detail === "string" ? res.data.detail : "We couldn't add these searches. Please try again." });
        return;
      }
      setIngestMsg({ ok: true, text: `Added ${fmt(res.data.rows_ingested)} searches · flagged ${fmt(res.data.gap_topics_flagged)} coverage gap(s).` });
      setPreview(null);
      onUploaded();
    } catch {
      setIngestMsg({ ok: false, text: "Could not reach the server." });
    } finally {
      setIngesting(false);
    }
  };

  const nv = preview?.novelty;
  const available = onlyNew ? (nv?.new_count ?? 0) : (preview?.distinct_query_count ?? 0);
  const effective = limitOn ? Math.min(Math.max(1, limitVal), available) : available;
  const subset = onlyNew || limitOn;
  return (
    <Card accent>
      <div className="flex items-center gap-3 mb-5">
        <div className="w-10 h-10 rounded-xl bg-brand/10 flex items-center justify-center">
          <Search size={20} className="text-brand" />
        </div>
        <div>
          <h3 className="text-sm font-extrabold text-ink">See what people are searching</h3>
          <p className="text-xs text-ink-light font-medium">
            Bring in the real questions and keywords people search for your brands, competitors, and conditions, with monthly search volume. Pulled live from SEMrush, so there's no spreadsheet to export or upload.
          </p>
        </div>
      </div>

      {/* Scope: area + optional brand */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div className="flex flex-col">
          <label className="text-xs font-semibold text-ink-light mb-1.5 uppercase tracking-wide">Therapeutic area *</label>
          <select className={INPUT_CLS} value={area} onChange={(e) => { setArea(e.target.value); setBrand(""); }}>
            <option value="" disabled>Select area…</option>
            {AREA_OPTIONS.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
        </div>
        <div className="flex flex-col">
          <label className="text-xs font-semibold text-ink-light mb-1.5 uppercase tracking-wide">Brand (optional)</label>
          <select className={INPUT_CLS} value={brand} onChange={(e) => setBrand(e.target.value)} disabled={!area}>
            <option value="">All brands in area</option>
            {areaBrands.map((b) => <option key={b} value={b}>{b}</option>)}
          </select>
        </div>
      </div>

      {/* Which terms to search around */}
      <label className="mt-4 block text-xs font-semibold text-ink-light uppercase tracking-wide">Also include searches for</label>
      <div className="mt-2 flex flex-wrap gap-x-5 gap-y-2">
        <label className="flex items-center gap-2 cursor-pointer select-none">
          <input type="checkbox" checked={incGenerics} onChange={(e) => setIncGenerics(e.target.checked)}
            className="h-4 w-4 rounded border-slate-300 text-brand focus:ring-2 focus:ring-brand-light/40" />
          <span className="text-xs font-medium text-ink">Generic names</span>
        </label>
        <label className="flex items-center gap-2 cursor-pointer select-none">
          <input type="checkbox" checked={incIndications} onChange={(e) => setIncIndications(e.target.checked)}
            className="h-4 w-4 rounded border-slate-300 text-brand focus:ring-2 focus:ring-brand-light/40" />
          <span className="text-xs font-medium text-ink">Indications</span>
        </label>
        <label className="flex items-center gap-2 cursor-pointer select-none">
          <input type="checkbox" checked={incCompetitors} onChange={(e) => setIncCompetitors(e.target.checked)}
            className="h-4 w-4 rounded border-slate-300 text-brand focus:ring-2 focus:ring-brand-light/40" />
          <span className="text-xs font-medium text-ink">Competitor brands</span>
        </label>
      </div>

      {/* What to bring back: questions, keywords, or both */}
      <div className="mt-4">
        <label className="text-xs font-semibold text-ink-light uppercase tracking-wide">What to bring back</label>
        <div className="mt-1.5 flex w-full max-w-xs overflow-hidden rounded-xl border border-slate-200 text-xs font-bold">
          {REPORT_OPTIONS.map((o) => (
            <button key={o.value} type="button" onClick={() => setReports(o.value)}
              className={`flex-1 px-3 py-2 transition-colors ${reports === o.value ? "bg-brand text-white" : "bg-canvas-card text-ink-light hover:bg-brand-surface"}`}>
              {o.label}
            </button>
          ))}
        </div>
        <p className="mt-1.5 text-[11px] text-ink-muted font-medium">
          {reports === "questions"
            ? "The actual questions people ask (best for AI prompts)."
            : reports === "related"
            ? "Keyword phrases people search (no question wording)."
            : "Both real questions and keyword phrases."}
        </p>
      </div>

      {/* How deep to go per term + context */}
      <div className="mt-4 flex items-center gap-3 flex-wrap">
        <label className="text-xs font-semibold text-ink-light uppercase tracking-wide">Results per term</label>
        <input type="number" min={1} max={100} value={perSeed}
          onChange={(e) => setPerSeed(Math.max(1, Math.min(100, Number(e.target.value) || 1)))}
          className={`${INPUT_CLS} w-24`} />
        <span className="text-[11px] text-ink-muted font-medium">
          how many top searches per term · up to {status?.max_seeds ?? 40} terms · {(status?.database ?? "us").toUpperCase()} market
        </span>
      </div>

      <button disabled={!area || loading} onClick={runPreview}
        className="mt-4 w-full flex items-center justify-center gap-2 px-5 py-3 bg-brand text-white rounded-xl text-sm font-bold hover:bg-brand-dark disabled:opacity-40 transition-colors">
        {loading ? <Spinner size={16} /> : <Search size={16} />}
        {loading ? "Looking up live demand…" : "Preview results"}
      </button>

      {error && <div className="mt-4 p-3 rounded-xl bg-red-50 text-red-700 text-sm font-medium">{error}</div>}

      {/* Preview */}
      {preview && (
        <div className="mt-5 border-t border-line pt-5 space-y-4">
          {preview.distinct_query_count === 0 ? (
            <EmptyState message="No searches came back for this selection. Try a broader area, add competitors, or increase results per term." />
          ) : (
            <>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                <SummaryTile label="Unique searches" value={fmt(preview.distinct_query_count)} />
                <SummaryTile label="Total demand" value={fmtCompact(preview.total_volume)} />
                <SummaryTile label="Terms searched" value={fmt(preview.seeds_queried)} />
                <SummaryTile label="Est. SEMrush credits" value={fmt(preview.estimated_units)} />
              </div>

              {nv && (
                <div className="rounded-xl border border-line bg-brand-surface/40 p-3">
                  <div className="flex items-center gap-2 mb-2">
                    <Sparkles size={14} className="text-brand" />
                    <p className="text-[11px] font-bold uppercase tracking-wide text-ink-muted">How much of this is new?</p>
                  </div>
                  <div className="flex flex-wrap gap-2 text-xs font-semibold">
                    <span className="rounded-full bg-emerald-100 text-emerald-700 px-2.5 py-1">{fmt(nv.new_count)} new</span>
                    <span className="rounded-full bg-amber-100 text-amber-800 px-2.5 py-1">{fmt(nv.seen_in_last_count)} seen last time</span>
                    <span className="rounded-full bg-slate-100 text-slate-600 px-2.5 py-1">{fmt(nv.covered_count)} already tracked</span>
                  </div>

                  {/* Choose how much to add */}
                  <div className="mt-3 space-y-2 border-t border-line/70 pt-3">
                    <label className="flex items-center gap-2 cursor-pointer select-none">
                      <input type="checkbox" checked={onlyNew} onChange={(e) => setOnlyNew(e.target.checked)}
                        className="h-4 w-4 rounded border-slate-300 text-brand focus:ring-2 focus:ring-brand-light/40" />
                      <span className="text-xs font-medium text-ink">
                        Add only new searches
                        <span className="text-ink-muted"> · skips {fmt(nv.seen_in_last_count + nv.covered_count)} already seen or tracked</span>
                      </span>
                    </label>
                    <label className="flex items-center gap-2 cursor-pointer select-none">
                      <input type="checkbox" checked={limitOn} onChange={(e) => setLimitOn(e.target.checked)}
                        className="h-4 w-4 rounded border-slate-300 text-brand focus:ring-2 focus:ring-brand-light/40" />
                      <span className="text-xs font-medium text-ink">Add only the top</span>
                      <input type="number" min={1} value={limitVal} disabled={!limitOn}
                        onChange={(e) => setLimitVal(Math.max(1, Number(e.target.value) || 1))}
                        className={`${INPUT_CLS} w-20 disabled:opacity-40`} />
                      <span className="text-xs font-medium text-ink-light">by demand</span>
                    </label>
                  </div>

                  <p className="mt-2.5 text-[11px] text-ink-light font-medium">
                    {subset
                      ? <>You'll add the <b>top {fmt(effective)}</b>{onlyNew ? " new" : ""} searches by demand. The rest stay out of this import.</>
                      : <>Adding <b>everything</b> ({fmtCompact(nv.novel_volume)} of demand is brand-new). Nothing is dropped, so your volume and trends stay accurate.</>}
                  </p>
                </div>
              )}

              <div>
                <p className="text-[11px] font-bold uppercase tracking-wide text-ink-muted mb-2">Top searches (by demand)</p>
                <div className="max-h-64 overflow-y-auto rounded-xl border border-line">
                  <table className="w-full text-sm">
                    <tbody>
                      {preview.sample.map((r, i) => (
                        <tr key={i} className="border-b border-slate-100 last:border-0">
                          <td className="py-2 px-3">
                            <div className="flex items-center gap-2 min-w-0">
                              <ReportBadge report={r.report} />
                              <span className="text-ink font-medium truncate">{r.prompt_text || r.query_text}</span>
                            </div>
                            <div className="text-[10px] text-ink-muted mt-0.5">
                              {r.therapeutic_area && r.therapeutic_area !== "Unmapped" ? r.therapeutic_area : "Not matched to a brand"}
                              {r.competitor ? ` · ${r.competitor}` : r.brand ? ` · ${r.brand}` : ""}
                            </div>
                          </td>
                          <td className="py-2 px-3 text-right tabular-nums text-ink font-semibold whitespace-nowrap">{fmt(r.search_volume)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* Ingest metadata */}
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div className="flex flex-col">
                  <label className="text-xs font-semibold text-ink-light mb-1.5 uppercase tracking-wide">Name this import</label>
                  <input className={INPUT_CLS} type="text" value={sourceLabel} onChange={(e) => setSourceLabel(e.target.value)} />
                </div>
                <div className="flex flex-col">
                  <label className="text-xs font-semibold text-ink-light mb-1.5 uppercase tracking-wide">As-of date</label>
                  <input className={INPUT_CLS} type="date" value={datasetDate} onChange={(e) => setDatasetDate(e.target.value)} />
                </div>
              </div>
              <label className="flex items-start gap-2.5 cursor-pointer select-none">
                <input type="checkbox" checked={synthesize} onChange={(e) => setSynthesize(e.target.checked)}
                  className="mt-0.5 h-4 w-4 rounded border-slate-300 text-brand focus:ring-2 focus:ring-brand-light/40" />
                <span className="text-[11px] font-medium leading-relaxed text-ink-light">
                  <b className="text-ink">Turn keyword-only searches into draft questions</b>. Real questions stay word-for-word; this only adds question phrasing to keyword rows that don't already have one.
                </span>
              </label>

              <button disabled={ingesting || !preview.fetch_id || effective === 0} onClick={runIngest}
                className="w-full flex items-center justify-center gap-2 px-5 py-3 bg-brand text-white rounded-xl text-sm font-bold hover:bg-brand-dark disabled:opacity-40 transition-colors">
                {ingesting ? <Spinner size={16} /> : <Check size={16} />}
                {ingesting ? "Adding…" : `Add ${fmt(effective)} searches to demand`}
              </button>
            </>
          )}
        </div>
      )}

      {ingestMsg && (
        <div className={`mt-4 p-3 rounded-xl text-sm font-medium ${ingestMsg.ok ? "bg-teal-50 text-teal-800" : "bg-red-50 text-red-700"}`}>
          {ingestMsg.text}
        </div>
      )}
    </Card>
  );
}

// Left-column doorway: tabs between the in-app SEMrush fetch (the primary, default path)
// and the CSV uploader. SEMrush leads and is auto-selected once a key is configured; when no
// key is set its tab is disabled (with a hint) and the CSV uploader is the landing tab.
function IngestPanel({ onUploaded, status }: { onUploaded: () => void; status: SemrushStatus | null }) {
  const configured = !!status?.configured;
  // Land on SEMrush by default, falling back to CSV when no key is configured. Decided once
  // status resolves so a manual tab switch afterwards is never overridden.
  const [tab, setTab] = useState<"csv" | "semrush">("csv");
  const picked = useRef(false);
  useEffect(() => {
    if (status && !picked.current) {
      picked.current = true;
      setTab(status.configured ? "semrush" : "csv");
    }
  }, [status]);
  const active = "flex-1 px-3 py-2 bg-brand text-white";
  const inactive = "flex-1 px-3 py-2 bg-canvas-card text-ink-light hover:bg-brand-surface";
  const useSemrush = tab === "semrush" && configured;
  return (
    <div>
      <div className="flex rounded-xl border border-line overflow-hidden text-xs font-bold mb-3">
        <button
          onClick={() => configured && setTab("semrush")}
          disabled={!configured}
          title={configured ? "" : "Live search demand needs a SEMrush Analytics API key. Ask your admin to add one."}
          className={`${useSemrush ? active : inactive} ${!configured ? "opacity-50 cursor-not-allowed" : ""}`}
        >
          <span className="inline-flex items-center justify-center gap-1.5"><Search size={13} /> Live search demand</span>
        </button>
        <button onClick={() => setTab("csv")} className={tab === "csv" ? active : inactive}>
          <span className="inline-flex items-center justify-center gap-1.5"><Upload size={13} /> Upload a file</span>
        </button>
      </div>
      {useSemrush ? <SemrushFetch onUploaded={onUploaded} status={status} /> : <Uploader onUploaded={onUploaded} />}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Volume bar chart                                                          */
/* -------------------------------------------------------------------------- */
function VolumeBars({
  data, nameKey, color = CHART_TEAL, height,
}: {
  data: { name: string; volume: number; share?: number }[]; nameKey: string; color?: string; height?: number;
}) {
  if (!data.length) return <EmptyState message={`No ${nameKey} volume in this dataset.`} />;
  const max = Math.max(...data.map((d) => d.volume), 1);
  const chartHeight = height ?? Math.max(200, data.length * 46);
  return (
    <ResponsiveContainer width="100%" height={chartHeight}>
      <BarChart data={data} layout="vertical" margin={{ left: 4, right: 60, top: 2, bottom: 2 }} barCategoryGap="26%">
        {/* Axis-free ranking: value labels replace tick clutter so a dominant bar doesn't dwarf the rest into invisibility. */}
        <XAxis type="number" domain={[0, max * 1.18]} hide />
        <YAxis
          type="category" dataKey="name" width={132}
          tick={{ fontSize: 12, fill: "#334155" }} tickLine={false} axisLine={false}
        />
        <Tooltip
          cursor={{ fill: "rgba(20,184,166,0.06)" }}
          content={({ active, payload }: any) => {
            if (!active || !payload?.length) return null;
            const d = payload[0].payload;
            return (
              <div className="bg-white rounded-xl shadow-lg border border-slate-200 p-3 text-xs">
                <p className="font-bold text-ink mb-0.5">{d.name}</p>
                <p className="text-ink-light font-medium">
                  {fmt(d.volume)} demand{d.share != null ? ` \u00b7 ${d.share}% of total` : ""}
                </p>
              </div>
            );
          }}
        />
        <Bar dataKey="volume" radius={[0, 6, 6, 0]} minPointSize={3} maxBarSize={26} fill={color} isAnimationActive={false}>
          <LabelList dataKey="volume" position="right" formatter={(v: any) => fmtCompact(Number(v))} fill="#0f172a" fontSize={11} />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

/* -------------------------------------------------------------------------- */
/*  Share of demand — our brands vs competitors                               */
/* -------------------------------------------------------------------------- */
const SHARE_BRAND = "#0D9488";
const SHARE_COMPETITOR = "#F43F5E";

function ShareOfDemand({ sod }: { sod: PromptVolumeShareOfDemand }) {
  const head = sod.brand_volume + sod.competitor_volume;
  if (head === 0) return <EmptyState message="No branded demand mapped in this dataset." />;
  return (
    <div className="space-y-4">
      <div>
        <div className="flex items-center justify-between text-xs font-semibold mb-1.5">
          <span className="text-brand-dark">Our brands · {fmt(sod.brand_volume)} ({sod.brand_share_pct}%)</span>
          <span className="text-rose-600">Competitors · {fmt(sod.competitor_volume)} ({sod.competitor_share_pct}%)</span>
        </div>
        <div className="flex h-4 w-full overflow-hidden rounded-full bg-slate-100">
          <div style={{ width: `${sod.brand_share_pct}%`, background: SHARE_BRAND }} />
          <div style={{ width: `${sod.competitor_share_pct}%`, background: SHARE_COMPETITOR }} />
        </div>
        <p className="mt-1.5 text-xs text-ink-light font-medium">
          Head-to-head share of <b>branded</b> search demand (proxy). Unbranded/category demand of {fmt(sod.category_volume)} is excluded.
        </p>
      </div>
      {sod.by_area.length > 0 && (
        <div className="space-y-2.5 border-t border-line pt-3">
          <div className="flex items-center justify-between">
            <p className="text-[11px] font-bold uppercase tracking-wide text-ink-muted">By therapeutic area</p>
            <div className="flex items-center gap-3 text-[10px] font-semibold text-ink-light">
              <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-full" style={{ background: SHARE_BRAND }} /> Ours</span>
              <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-full" style={{ background: SHARE_COMPETITOR }} /> Competitors</span>
            </div>
          </div>
          {sod.by_area.slice(0, 6).map((a) => (
            <div key={a.therapeutic_area}>
              <div className="flex items-center justify-between text-xs font-medium mb-1">
                <span className="font-semibold text-ink">{a.therapeutic_area}</span>
                <span className="text-ink-light">{a.brand_share_pct}% ours</span>
              </div>
              <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-slate-100">
                <div style={{ width: `${a.brand_share_pct}%`, background: SHARE_BRAND }} />
                <div style={{ width: `${a.competitor_share_pct}%`, background: SHARE_COMPETITOR }} />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Demand by audience (heuristic persona split)                              */
/* -------------------------------------------------------------------------- */
const PERSONA_COLORS: Record<string, string> = {
  Patient: "#14B8A6", Provider: "#6366F1", Prospect: "#F59E0B", Unclassified: "#94A3B8",
};

function PersonaSplit({ personas }: { personas: PromptVolumePersonaVolume[] }) {
  const total = personas.reduce((s, p) => s + p.volume, 0);
  if (!total) return <EmptyState message="No persona signal in this dataset." />;
  // Known personas ranked by volume; "Unclassified" is pinned last and de-emphasised so it
  // reads as "ambiguous wording" rather than dominating the chart as if it were a real audience.
  const known = personas.filter((p) => p.persona !== "Unclassified").sort((a, b) => b.volume - a.volume);
  const unclassified = personas.find((p) => p.persona === "Unclassified");
  const classifiedPct = Math.round((known.reduce((s, p) => s + p.volume, 0) / total) * 100);
  const ordered = unclassified ? [...known, unclassified] : known;
  return (
    <div className="space-y-3">
      <div className="rounded-xl border border-line bg-brand-surface/40 px-3 py-2.5">
        <p className="text-xs font-medium text-ink-light">
          <b className="text-ink">{classifiedPct}%</b> of demand carries a clear audience signal — the rest is broad/branded wording that maps to no single persona.
        </p>
      </div>
      {ordered.map((p) => {
        const pct = Math.round((p.volume / total) * 100);
        const color = PERSONA_COLORS[p.persona] || "#94A3B8";
        const muted = p.persona === "Unclassified";
        return (
          <div key={p.persona} className={muted ? "opacity-70" : ""}>
            <div className="flex items-center justify-between text-xs font-medium mb-1">
              <span className="font-semibold text-ink flex items-center gap-1.5">
                <span className="h-2.5 w-2.5 rounded-full" style={{ background: color }} />
                {p.persona}
              </span>
              <span className="text-ink-light">{fmt(p.volume)} · {pct}%</span>
            </div>
            <div className="h-2.5 w-full overflow-hidden rounded-full bg-slate-100">
              <div className="h-full rounded-full" style={{ width: `${pct}%`, background: color }} />
            </div>
          </div>
        );
      })}
      <p className="text-xs text-ink-light font-medium pt-1">
        Audience inferred from query wording (heuristic, no ML). "Unclassified" = no clear persona signal, which is normal for broad or branded terms.
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Demand trend over time + rising topics                                    */
/* -------------------------------------------------------------------------- */
function TrendChart({ trend }: { trend: PromptVolumeTrend }) {
  if (trend.series.length < 2)
    return <EmptyState message="Upload a second dated dataset to chart demand over time." />;
  const data = trend.series.map((p) => ({
    name: p.source_label || p.dataset_date,
    Total: p.total_volume,
    "Our brands": p.brand_volume,
    Competitors: p.competitor_volume,
  }));
  return (
    <ResponsiveContainer width="100%" height={260}>
      <LineChart data={data} margin={{ left: 8, right: 16, top: 8, bottom: 4 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
        <XAxis dataKey="name" tick={{ fontSize: 11, fill: "#64748b" }} />
        <YAxis tick={{ fontSize: 11, fill: "#64748b" }} tickFormatter={fmt} width={54} />
        <Tooltip formatter={(v: any) => fmt(Number(v))} />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        <Line type="monotone" dataKey="Total" stroke="#0F766E" strokeWidth={2.5} dot={{ r: 3 }} />
        <Line type="monotone" dataKey="Our brands" stroke={SHARE_BRAND} strokeWidth={2} dot={{ r: 3 }} />
        <Line type="monotone" dataKey="Competitors" stroke={SHARE_COMPETITOR} strokeWidth={2} dot={{ r: 3 }} />
      </LineChart>
    </ResponsiveContainer>
  );
}

function EmergingTopics({ emerging }: { emerging: PromptVolumeTrend["emerging"] }) {
  if (!emerging || emerging.topics.length === 0)
    return <EmptyState message="No rising topics between the two most recent datasets." />;
  return (
    <div className="space-y-2">
      <p className="text-xs text-ink-light font-medium mb-2">
        Rising in <b>{emerging.current_label}</b> vs <b>{emerging.previous_label}</b>.
      </p>
      {emerging.topics.map((t, i) => (
        <div key={i} className="flex items-center justify-between gap-4 rounded-xl border border-line p-3">
          <div className="min-w-0">
            <p className="text-sm font-bold text-ink truncate">{t.query_text}</p>
            <p className="text-xs text-ink-light font-medium mt-0.5">
              {t.therapeutic_area}{t.competitor ? ` · ${t.competitor}` : ""} · {fmt(t.previous_volume)} → {fmt(t.current_volume)}
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {t.is_new ? (
              <span className="rounded-full bg-amber-100 text-amber-800 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide">New</span>
            ) : t.pct_change != null ? (
              <span className="text-xs font-bold text-emerald-600">+{t.pct_change}%</span>
            ) : null}
            <span className="inline-flex items-center gap-1 text-emerald-600 font-bold text-sm tabular-nums">
              <ArrowUpRight size={14} /> +{fmt(t.delta)}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Coverage gaps with demand / opportunity sort                              */
/* -------------------------------------------------------------------------- */
function GapTopics({ gaps, onCreate }: { gaps: PromptVolumeGapTopic[]; onCreate: (draft: GapDraft) => void }) {
  const [sortBy, setSortBy] = useState<"demand" | "opportunity">("demand");
  if (gaps.length === 0)
    return <EmptyState message="No high-volume gaps: the bank covers the top demand in this dataset." />;
  const sorted = [...gaps].sort((a, b) =>
    sortBy === "opportunity"
      ? (b.opportunity_score ?? b.combined_volume) - (a.opportunity_score ?? a.combined_volume)
      : b.combined_volume - a.combined_volume
  );
  return (
    <div>
      <div className="flex items-center justify-between gap-4 mb-3">
        <p className="text-xs text-ink-light font-medium">
          High-demand topics with no matching approved question. Sort by raw demand, or by <b>opportunity</b> (demand discounted by keyword difficulty).
        </p>
        <div className="flex rounded-lg border border-line overflow-hidden text-xs font-bold shrink-0">
          {(["demand", "opportunity"] as const).map((k) => (
            <button
              key={k}
              onClick={() => setSortBy(k)}
              className={`px-3 py-1.5 capitalize transition-colors ${
                sortBy === k ? "bg-brand text-white" : "bg-canvas-card text-ink-light hover:bg-brand-surface"
              }`}
            >
              {k}
            </button>
          ))}
        </div>
      </div>
      <div className="space-y-2">
        {sorted.map((t, i) => {
          const primary = sortBy === "opportunity" ? Math.round(t.opportunity_score ?? t.combined_volume) : t.combined_volume;
          return (
            <div key={i} className="flex items-center justify-between gap-4 rounded-xl border border-line p-3 hover:bg-brand-surface/40 transition-colors">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <p className="text-sm font-bold text-ink truncate">{t.question || t.label}</p>
                  <OriginBadge origin={t.question_origin} />
                </div>
                <p className="text-xs text-ink-light font-medium mt-0.5">
                  {t.label !== (t.question ?? "") ? <span className="text-ink-muted">Keyword: {t.label} · </span> : null}
                  {t.therapeutic_area}{t.competitor ? ` · ${t.competitor}` : ""} · {t.query_count} quer{t.query_count === 1 ? "y" : "ies"}
                  {t.avg_difficulty != null ? ` · KD ${t.avg_difficulty}` : ""}
                  {t.avg_cpc != null ? ` · $${t.avg_cpc.toFixed(2)} CPC` : ""}
                </p>
              </div>
              <div className="flex items-center gap-3 shrink-0">
                <div className="text-right">
                  <div className="text-lg font-bold tabular-nums text-ink">{fmt(primary)}</div>
                  <div className="text-[10px] uppercase tracking-wide text-ink-muted font-semibold">{sortBy}</div>
                </div>
                <button
                  onClick={() => onCreate({ question_text: t.question || t.label, therapeutic_area: t.therapeutic_area, competitor: t.competitor, brand_focus: t.brand ?? undefined, question_origin: t.question_origin })}
                  className="inline-flex items-center gap-1 px-3 py-2 rounded-lg text-xs font-bold bg-brand-surface text-brand hover:bg-brand hover:text-white transition-colors"
                >
                  Create question <ArrowUpRight size={14} />
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Coverage gap alerts — trackable, auto-resolving gap signals                */
/* -------------------------------------------------------------------------- */
const ALERT_TABS: Array<"OPEN" | "RESOLVED" | "DISMISSED"> = ["OPEN", "RESOLVED", "DISMISSED"];
const ALERT_BLURB: Record<string, string> = {
  OPEN: "High-opportunity gaps with no approved question. Tracked until the bank covers them (auto-resolves) or you dismiss them.",
  RESOLVED: "Gaps the Approved Question Bank now covers: closed automatically.",
  DISMISSED: "Gaps you muted. They stay quiet even if the topic recurs.",
};

function GapAlerts({ reloadToken, onCreate }: { reloadToken: number; onCreate: (draft: GapDraft) => void }) {
  const [status, setStatus] = useState<"OPEN" | "RESOLVED" | "DISMISSED">("OPEN");
  const [alerts, setAlerts] = useState<PromptVolumeGapAlert[]>([]);
  const [summary, setSummary] = useState<PromptVolumeGapAlertSummary | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);

  const load = () => {
    api.promptVolumeGapAlerts(status).then((r) => setAlerts(r.alerts)).catch(() => setAlerts([]));
    api.promptVolumeGapAlertSummary().then(setSummary).catch(() => {});
  };
  useEffect(load, [status, reloadToken]);

  const dismiss = async (id: string) => {
    setBusy(id);
    try { await api.dismissPromptVolumeGapAlert(id); load(); } finally { setBusy(null); }
  };

  const sync = async () => {
    setSyncing(true);
    try { await api.syncPromptVolumeGapAlerts(); load(); } finally { setSyncing(false); }
  };

  const countFor = (s: string) =>
    summary ? (s === "OPEN" ? summary.open : s === "RESOLVED" ? summary.resolved : summary.dismissed) : 0;

  return (
    <Card>
      <div className="flex items-center justify-between gap-4 mb-3 flex-wrap">
        <div className="flex items-center gap-2">
          <Bell size={16} className="text-brand" />
          <h3 className="text-xs font-bold text-ink uppercase tracking-widest">Coverage gap alerts</h3>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={sync}
            disabled={syncing}
            title="Reconcile alerts against the latest upload"
            className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-bold text-ink-light bg-slate-100 hover:bg-slate-200 disabled:opacity-40 transition-colors"
          >
            <RefreshCw size={13} className={syncing ? "animate-spin" : ""} /> Sync
          </button>
          <div className="flex rounded-lg border border-line overflow-hidden text-xs font-bold">
            {ALERT_TABS.map((s) => (
              <button
                key={s}
                onClick={() => setStatus(s)}
                className={`px-3 py-1.5 capitalize transition-colors ${status === s ? "bg-brand text-white" : "bg-canvas-card text-ink-light hover:bg-brand-surface"}`}
              >
                {s.toLowerCase()} ({countFor(s)})
              </button>
            ))}
          </div>
        </div>
      </div>
      <p className="text-xs text-ink-light font-medium mb-3">{ALERT_BLURB[status]}</p>
      {alerts.length === 0 ? (
        <EmptyState message={status === "OPEN" ? "No open gap alerts. Upload a dataset to surface new gaps." : `No ${status.toLowerCase()} gap alerts.`} />
      ) : (
        <div className="space-y-2">
          {alerts.map((a) => (
            <div key={a.alert_id} className="flex items-center justify-between gap-4 rounded-xl border border-line p-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <p className="text-sm font-bold text-ink truncate">{a.question || a.label}</p>
                  {a.status === "OPEN" && a.is_new && (
                    <span className="rounded-full bg-amber-100 text-amber-800 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide shrink-0">New</span>
                  )}
                  {a.status === "RESOLVED" && (
                    <span className="inline-flex items-center gap-1 rounded-full bg-emerald-100 text-emerald-700 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide shrink-0">
                      <Check size={10} /> Covered
                    </span>
                  )}
                </div>
                <p className="text-xs text-ink-light font-medium mt-0.5">
                  {a.question ? <span className="text-ink-muted">Keyword: {a.label} · </span> : null}
                  {a.therapeutic_area || "Unmapped"}{a.competitor ? ` · ${a.competitor}` : ""} · {fmt(a.combined_volume)} demand · opp {fmt(Math.round(a.opportunity_score))}
                </p>
              </div>
              {a.status === "OPEN" && (
                <div className="flex items-center gap-2 shrink-0">
                  <button
                    onClick={() => onCreate({ question_text: a.question || a.label, therapeutic_area: a.therapeutic_area, competitor: a.competitor })}
                    className="inline-flex items-center gap-1 px-3 py-2 rounded-lg text-xs font-bold bg-brand-surface text-brand hover:bg-brand hover:text-white transition-colors"
                  >
                    Create question <ArrowUpRight size={14} />
                  </button>
                  <button
                    onClick={() => dismiss(a.alert_id)}
                    disabled={busy === a.alert_id}
                    title="Mute this alert"
                    className="inline-flex items-center px-2.5 py-2 rounded-lg text-xs font-bold text-ink-light bg-slate-100 hover:bg-slate-200 disabled:opacity-40 transition-colors"
                  >
                    <X size={14} />
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/*  Page                                                                      */
/* -------------------------------------------------------------------------- */
// Demand is a proxy either way, but its UNIT differs by source: a search-volume export (SEO
// tools) vs a frequency proxy derived from how often each prompt recurs in a prompt log that
// had no volume column. Label each honestly so the number is never over-read.
function demandLabels(metricType?: string | null) {
  const freq = metricType === "prompt_frequency";
  return {
    freq,
    short: freq ? "prompt mentions (frequency)" : "searches/mo (proxy)",
    noun: freq ? "distinct prompts" : "distinct keywords",
    summed: freq ? "how often each prompt recurred across" : "estimated monthly searches summed across",
    tooltip: freq
      ? "Total times these prompts appeared in your AI-prompt export — a frequency proxy for demand (this file had no search-volume column), not a count of unique people."
      : "Estimated monthly searches summed across every distinct keyword in the latest dataset. A relative demand indicator used as a proxy for AI-inquiry demand, not a count of people or AI prompts.",
  };
}

export default function PromptVolume() {
  const navigate = useNavigate();
  const [intel, setIntel] = useState<PromptVolumeIntelligence | null>(null);
  const [gaps, setGaps] = useState<PromptVolumeGapTopic[]>([]);
  const [batches, setBatches] = useState<PromptVolumeBatch[]>([]);
  const [trend, setTrend] = useState<PromptVolumeTrend | null>(null);
  const [semrush, setSemrush] = useState<SemrushStatus | null>(null);
  const [loading, setLoading] = useState(true);

  const loadAll = () => {
    setLoading(true);
    Promise.all([
      api.promptVolumeIntelligence().catch(() => null),
      api.promptVolumeGaps().catch(() => ({ topics: [] as PromptVolumeGapTopic[] })),
      api.promptVolumeBatches().catch(() => ({ batches: [] as PromptVolumeBatch[] })),
      api.promptVolumeTrend().catch(() => null),
    ]).then(([i, g, b, t]) => {
      setIntel(i as PromptVolumeIntelligence | null);
      setGaps((g as any)?.topics ?? []);
      setBatches((b as any)?.batches ?? []);
      setTrend(t as PromptVolumeTrend | null);
      setLoading(false);
    });
  };

  useEffect(loadAll, []);
  useEffect(() => { api.promptVolumeSemrushStatus().then(setSemrush).catch(() => setSemrush(null)); }, []);

  const hasData = !!intel?.batch_id;
  const taData = (intel?.by_therapeutic_area ?? []).slice(0, 8).map((t) => ({ name: t.therapeutic_area, volume: t.volume, share: t.share_pct }));
  const compData = (intel?.by_competitor ?? []).slice(0, 8).map((c) => ({ name: c.competitor, volume: c.volume, share: c.share_pct }));
  // Equal height for both demand charts so the shorter list never leaves a blank gap beside the taller one.
  const chartHeight = Math.max(220, Math.max(taData.length, compData.length) * 46);

  const demand = demandLabels(intel?.metric_type);

  return (
    <div className="space-y-8">
      <PageHeader
        title="Prompt Volume Intelligence"
        subtitle="Turn third-party demand data into coverage gaps and a demand-ranked question bank."
      />

      {/* Proxy disclaimer (search demand is a proxy, not literal AI-prompt counts) */}
      <div className="flex items-start gap-3 rounded-xl border border-amber-200 bg-amber-50/70 px-4 py-3">
        <TrendingUp size={18} className="mt-0.5 shrink-0 text-amber-700" />
        <p className="text-xs font-medium text-amber-900 leading-relaxed">
          Demand figures are a <b>third-party proxy</b>, not literal counts of prompts entered into ChatGPT, Gemini, or Claude. A file <b>with</b> a search-volume column uses that volume (SEO exports such as Semrush); a <b>prompt log with no volume column</b> has demand estimated from how often each prompt recurs.
          Upload a <b>prompt / question</b> column to monitor the exact questions people ask; keyword-only files auto-generate one question per gap topic.
        </p>
      </div>

      {/* Entry point to phrasing-variation testing (questions created from gaps can be varied). */}
      <Link to="/run-analysis/variations" className="inline-flex items-center gap-1.5 text-xs font-bold text-brand hover:text-brand-dark">
        <Wand2 size={13} /> Test phrasing robustness: generate variations of your questions
      </Link>

      {/* KPI summary row — strong hierarchy, uses horizontal space */}
      {hasData && intel && (
        <AnimatedCard>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <Stat
              label="Total demand"
              value={fmtCompact(intel.total_volume)}
              sub={`${fmt(intel.total_volume)} ${demand.short}`}
              icon={<TrendingUp size={18} />}
              tooltip={demand.tooltip}
            />
            <Stat label="Therapeutic areas" value={intel.by_therapeutic_area.length} sub="mapped in dataset" icon={<Layers size={18} />} />
            <Stat label="Competitors seen" value={intel.by_competitor.length} sub="branded rivals" icon={<Building2 size={18} />} />
            <Stat label="Coverage gaps" value={gaps.length} sub="high-demand, uncovered" icon={<Target size={18} />} />
          </div>
        </AnimatedCard>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <IngestPanel onUploaded={loadAll} status={semrush} />

        {/* Dataset provenance + methodology (KPIs now live in the row above) */}
        <Card>
          <h3 className="text-xs font-bold text-ink uppercase tracking-widest mb-4">Dataset details</h3>
          {loading ? (
            <div className="flex justify-center py-10"><Spinner /></div>
          ) : hasData && intel?.batch ? (
            <div className="space-y-4">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-sm font-extrabold text-ink truncate">{intel.batch.source_label}</p>
                  <p className="text-xs text-ink-light font-medium mt-0.5">{intel.batch.source_tool} · {intel.batch.dataset_date}</p>
                </div>
                <a
                  href={api.promptVolumeCsvUrl()}
                  className="inline-flex items-center gap-1 rounded-lg bg-brand-surface px-3 py-2 text-xs font-bold text-brand hover:bg-brand hover:text-white transition-colors shrink-0"
                >
                  <Download size={13} /> Export CSV
                </a>
              </div>
              {intel.distinct_query_count != null && (
                <div className="rounded-xl border border-line bg-brand-surface/40 p-3">
                  <p className="text-[11px] text-ink-light font-medium leading-relaxed">
                    Total demand = {demand.summed}{" "}
                    <b className="text-ink">{fmt(intel.distinct_query_count)}</b> {demand.noun}
                    {intel.raw_row_count != null && intel.raw_row_count > intel.distinct_query_count
                      ? ` (${fmt(intel.raw_row_count - intel.distinct_query_count)} duplicate rows removed)`
                      : ""}. A relative demand indicator, not a count of people or AI prompts.
                  </p>
                </div>
              )}
              {intel.distinct_query_count != null && intel.prompt_backed_count != null && (
                (intel.prompt_backed_count > 0 ? (
                  <div className="flex items-start gap-2 rounded-xl border border-emerald-200 bg-emerald-50/70 p-3">
                    <Check size={14} className="mt-0.5 shrink-0 text-emerald-600" />
                    <p className="text-[11px] text-emerald-900 font-medium leading-relaxed">
                      <b>{fmt(intel.prompt_backed_count)}</b> of {fmt(intel.distinct_query_count)} queries carry a <b>real question / prompt</b> — those gaps are monitored with the exact wording people used.
                    </p>
                  </div>
                ) : (
                  <div className="flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50/70 p-3">
                    <Wand2 size={14} className="mt-0.5 shrink-0 text-amber-700" />
                    <p className="text-[11px] text-amber-900 font-medium leading-relaxed">
                      Keyword-only dataset — gap questions are <b>auto-generated</b> from keywords. Upload a question / prompt export (AlsoAsked, AnswerThePublic, Semrush <i>Questions</i>) to monitor the exact questions people ask.
                    </p>
                  </div>
                ))
              )}
            </div>
          ) : (
            <EmptyState message="No dataset uploaded yet. Upload a CSV to see demand intelligence." />
          )}
        </Card>
      </div>

      {hasData && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <AnimatedCard className="h-full">
            <Card title="Demand by therapeutic area" className="h-full">
              <p className="text-xs text-ink-light font-medium -mt-2 mb-3">Total search demand per indication (top 8). Longer bar = more market attention.</p>
              <VolumeBars data={taData} nameKey="therapeutic area" height={chartHeight} />
            </Card>
          </AnimatedCard>
          <AnimatedCard delay={0.05} className="h-full">
            <Card title="Demand by competitor" className="h-full">
              <p className="text-xs text-ink-light font-medium -mt-2 mb-3">Search demand for rival brands (top 8), used as a proxy for AI-inquiry interest.</p>
              <VolumeBars data={compData} nameKey="competitor" color={CHART_ROSE} height={chartHeight} />
            </Card>
          </AnimatedCard>
        </div>
      )}

      {/* Share of demand + audience persona split */}
      {hasData && intel?.share_of_demand && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <AnimatedCard delay={0.1} className="h-full">
            <Card title="Share of demand: our brands vs competitors" className="h-full">
              <ShareOfDemand sod={intel.share_of_demand} />
            </Card>
          </AnimatedCard>
          <AnimatedCard delay={0.12} className="h-full">
            <Card title="Demand by audience (persona)" className="h-full">
              <PersonaSplit personas={intel.by_persona ?? []} />
            </Card>
          </AnimatedCard>
        </div>
      )}

      {/* Demand trend over time + rising topics */}
      {hasData && trend && (
        <AnimatedCard delay={0.14}>
          <Card title="Demand trend over time">
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              <div>
                <h4 className="text-xs font-bold text-ink-light uppercase tracking-widest mb-3">Volume by upload</h4>
                <TrendChart trend={trend} />
              </div>
              <div>
                <h4 className="text-xs font-bold text-ink-light uppercase tracking-widest mb-3">Rising topics</h4>
                <EmergingTopics emerging={trend.emerging} />
              </div>
            </div>
          </Card>
        </AnimatedCard>
      )}

      {/* Coverage gap alerts (FR-116.3 enhancement) — trackable, auto-resolving */}
      {hasData && (
        <AnimatedCard delay={0.15}>
          <GapAlerts reloadToken={batches.length} onCreate={(draft) => navigate("/questions", { state: { createDraft: draft } })} />
        </AnimatedCard>
      )}

      {/* Gap topics with opportunity scoring (FR-116.3) */}
      {hasData && (
        <AnimatedCard delay={0.16}>
          <Card title="High-volume coverage gaps">
            <GapTopics gaps={gaps} onCreate={(draft) => navigate("/questions", { state: { createDraft: draft } })} />
          </Card>
        </AnimatedCard>
      )}

      {/* Upload history */}
      {batches.length > 0 && (
        <Card title="Upload history">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-sm">
              <thead>
                <tr className="text-left border-b-2 border-slate-200 text-xs text-ink-light uppercase tracking-widest">
                  <th className="pb-3 font-bold">Dataset</th>
                  <th className="pb-3 font-bold">Source</th>
                  <th className="pb-3 font-bold">Date</th>
                  <th className="pb-3 font-bold text-right">Rows</th>
                  <th className="pb-3 font-bold text-right">Gaps</th>
                  <th className="pb-3 font-bold">Uploaded</th>
                </tr>
              </thead>
              <tbody>
                {batches.map((b) => (
                  <tr key={b.batch_id} className="border-b border-slate-100">
                    <td className="py-3 font-semibold text-ink">{b.source_label}</td>
                    <td className="py-3 text-ink-light font-medium">{b.source_tool}</td>
                    <td className="py-3 text-ink-light font-medium">{b.dataset_date}</td>
                    <td className="py-3 text-right tabular-nums text-ink">{fmt(b.rows_ingested)}</td>
                    <td className="py-3 text-right tabular-nums text-ink">{fmt(b.gap_topics_flagged)}</td>
                    <td className="py-3 text-ink-light font-medium">{b.created_at ? new Date(b.created_at).toLocaleString() : "N/A"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}
