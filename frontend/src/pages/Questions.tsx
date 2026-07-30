import { Fragment, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { motion } from "framer-motion";
import { FileText, Users, Layers, Grid3X3, Play, CheckSquare, Square, X, Check, Ban, Plus, Wand2, Stethoscope, TrendingUp, Filter, Upload, ChevronRight, ChevronDown, GitBranch, Search, AlertTriangle } from "lucide-react";
import { api, Question, PrioritizedQuestion, PromptImportResult, PromptPreviewResult, VariationExpansion, VariationGroupDetail } from "../api/client";
import { Card, Stat, PageHeader, MultiSelect, AnimatedCard, EmptyState, Spinner, InfoTooltip, Tooltip, ThemeBadge } from "../components/ui";
import { TA_GROUPS, TA_VALUES, BRAND_OPTIONS, DISEASE_OPTIONS, DISEASE_BRAND_MAP, brandsForIndication, competitorsForIndication, diseasesForIndication } from "../lib/taxonomy";

const STATUS_COLORS: Record<string, string> = {
  APPROVED: "bg-teal-100 text-teal-800",
  PENDING: "bg-amber-100 text-amber-800",
  REJECTED: "bg-red-100 text-red-700",
};

// Variation staging status -> badge colors (used in the reverse expandable lineage list).
const VAR_STATUS_CLS: Record<string, string> = {
  DRAFT: "bg-amber-100 text-amber-800",
  APPROVED: "bg-teal-100 text-teal-800",
  REJECTED: "bg-slate-200 text-ink-light",
};

// Derived provenance bucket -> compact badge (Source column).
const SOURCE_META: Record<string, { label: string; cls: string }> = {
  MANUAL:        { label: "Manual",        cls: "bg-slate-100 text-ink-light" },
  PROMPT_VOLUME: { label: "Prompt Volume", cls: "bg-emerald-100 text-emerald-700" },
  DISCOVER:      { label: "Discover",      cls: "bg-sky-100 text-sky-700" },
  VARIATION:     { label: "Variation",     cls: "bg-violet-100 text-violet-700" },
};
/** Bank filters, each holding every value the user ticked. Empty list = no filter. */
type BankFilters = {
  persona: string[];
  therapeutic_area: string[];
  brand_focus: string[];
  disease: string[];
  domain: string[];
};

// Full labels for the Source filter dropdown (value -> label).
const SOURCE_LABELS: Record<string, string> = {
  MANUAL: "Manual",
  PROMPT_VOLUME: "Prompt Volume",
  DISCOVER: "Discover Questions",
  VARIATION: "Variation",
};

// Workshop designation (Persona + indication from Rhem.csv) -> badge colors.
const DESIGNATION_CLS: Record<string, string> = {
  "Patient RA": "bg-sky-100 text-sky-700",
  "Patient PsA": "bg-cyan-100 text-cyan-700",
  "HCP RA": "bg-indigo-100 text-indigo-700",
  "HCP PsA": "bg-violet-100 text-violet-700",
  "HCP RA & PsA": "bg-fuchsia-100 text-fuchsia-700",
};

const FIELD_CLS =
  "w-full border border-line rounded-xl px-3 py-2 text-sm bg-canvas-card text-ink font-medium focus:outline-none focus:ring-2 focus:ring-brand-light/40 focus:border-brand-light transition-colors";

// Demo-only quick filters (Workshop Questions + Rheumatology Only) share a subtle
// amber treatment (instead of the brand/slate used by the real controls) so it's
// clear at a glance that these two toggles are curated shortcuts kept for the demo.
const demoFilterCls = (active: boolean) =>
  `flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-bold transition-colors ${
    active ? "bg-amber-500 text-white" : "text-amber-800 bg-amber-100 hover:bg-amber-200"
  }`;

// Sentinel Brand value for a question that isn't tied to any single brand -> saved as a
// brand-less DISEASE_STATE (landscape) question tagged with the area's competitors.
const NO_BRAND = "__OTHER__";

const DESIGNATION_DISEASES: Record<string, string[]> = {
  "Patient RA": ["Rheumatoid Arthritis"],
  "HCP RA": ["Rheumatoid Arthritis"],
  "Patient PsA": ["Psoriatic Arthritis"],
  "HCP PsA": ["Psoriatic Arthritis"],
  "HCP RA & PsA": ["Rheumatoid Arthritis", "Psoriatic Arthritis"],
};

// A pre-fill payload — passed via router state when "Create question" is clicked on a
// Prompt Volume coverage gap, so the analyst lands on a ready-to-edit draft.
export type QuestionDraft = {
  question_text?: string;
  therapeutic_area?: string | null;
  brand_focus?: string;
  competitor?: string | null;
  question_origin?: "prompt" | "synthesized" | "keyword";
};

// FR-116 demand provenance shown as a bank badge. Keyed by the stored (uppercase) demand_origin.
const DEMAND_ORIGIN_META: Record<string, { label: string; cls: string }> = {
  PROMPT: { label: "Real", cls: "bg-emerald-100 text-emerald-700" },
  SYNTHESIZED: { label: "AI-generated", cls: "bg-violet-100 text-violet-700" },
  KEYWORD: { label: "From keyword", cls: "bg-amber-100 text-amber-800" },
};
function QuestionOriginBadge({ origin }: { origin?: string | null }) {
  const meta = origin ? DEMAND_ORIGIN_META[origin] : undefined;
  if (!meta) return null;
  return (
    <span
      title={`Created from a Prompt Volume gap — ${meta.label}`}
      className={`shrink-0 inline-flex items-center rounded-full ${meta.cls} px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide`}
    >
      {meta.label}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/*  Create-question modal (manual, or pre-filled from a coverage gap)          */
/* -------------------------------------------------------------------------- */
function CreateQuestionModal({ draft, onClose, onCreated }: {
  draft: QuestionDraft;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [text, setText] = useState(draft.question_text ?? "");
  const [persona, setPersona] = useState("Patient");
  const [ta, setTa] = useState(TA_VALUES.includes(draft.therapeutic_area ?? "") ? (draft.therapeutic_area as string) : "");
  const [brand, setBrand] = useState(draft.brand_focus ?? "");
  const [domain, setDomain] = useState("General");
  const [weight, setWeight] = useState("1");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Brands are scoped to the chosen therapeutic area (falls back to all brands when an area has
  // no AbbVie focus brand, so the field is never a dead end).
  const scopedBrands = ta ? brandsForIndication(ta) : [];
  const brandList = !ta ? [] : (scopedBrands.length ? scopedBrands : BRAND_OPTIONS.filter(Boolean));
  const areaCompetitors = ta ? competitorsForIndication(ta) : [];

  const canSave = text.trim().length > 0 && !!persona && !!ta && !!brand && !!domain && !saving;

  const submit = async () => {
    if (!canSave) return;
    setSaving(true);
    setError(null);
    try {
      const isOther = brand === NO_BRAND;
      await api.createQuestion({
        question_text: text.trim(),
        persona,
        therapeutic_area: ta,
        domain,
        priority_weight: parseFloat(weight) || 1,
        demand_origin: draft.question_origin ? draft.question_origin.toUpperCase() : undefined,
        ...(isOther
          ? { monitoring_mode: "DISEASE_STATE", competitor_focus: areaCompetitors }
          : { monitoring_mode: "BRAND", brand_focus: brand }),
      });
      onCreated();
    } catch {
      setError("Could not save the question. Check the fields and try again.");
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="w-full max-w-lg rounded-2xl bg-canvas-card p-6 shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-1">
          <h3 className="text-sm font-bold text-ink">New question{draft.question_text ? " from gap" : ""}</h3>
          <button onClick={onClose} className="text-ink-light hover:text-ink transition-colors"><X size={18} /></button>
        </div>
        <p className="text-xs text-ink-light font-medium mb-4">
          Refine this into a clear question. It saves as <b>Pending</b> for Medical Affairs review before it can run.
        </p>
        {draft.competitor && (
          <p className="text-[11px] text-ink-muted font-medium mb-3">Gap context: competitor <b>{draft.competitor}</b></p>
        )}
        {draft.question_origin && (
          <p className="text-[11px] text-ink-muted font-medium mb-3">
            Origin: <b>{DEMAND_ORIGIN_META[draft.question_origin.toUpperCase()]?.label ?? draft.question_origin}</b> — saved as a label on this question.
          </p>
        )}
        <div className="space-y-3">
          <div>
            <label className="text-[11px] font-bold text-ink-light uppercase tracking-widest">Question</label>
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={3}
              placeholder="e.g. Does Humira cause hair loss?"
              className={`${FIELD_CLS} mt-1 resize-none`}
            />
            {draft.question_text && (
              <p className="text-[11px] text-ink-muted mt-1">Prefilled from the search topic: edit into a natural question.</p>
            )}
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-[11px] font-bold text-ink-light uppercase tracking-widest">Persona</label>
              <select value={persona} onChange={(e) => setPersona(e.target.value)} className={`${FIELD_CLS} mt-1`}>
                {["Patient", "Prospect", "Provider"].map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
            </div>
            <div>
              <label className="text-[11px] font-bold text-ink-light uppercase tracking-widest">Theme</label>
              <select value={domain} onChange={(e) => setDomain(e.target.value)} className={`${FIELD_CLS} mt-1`}>
                {["Efficacy", "Safety", "Access", "Comparative", "General"].map((d) => <option key={d} value={d}>{d}</option>)}
              </select>
            </div>
            <div>
              <label className="text-[11px] font-bold text-ink-light uppercase tracking-widest">Therapeutic area</label>
              <select
                value={ta}
                onChange={(e) => {
                  const next = e.target.value;
                  setTa(next);
                  // Drop a brand (or the "Other" choice) that doesn't fit the newly chosen area.
                  if (brand === NO_BRAND) {
                    if (!next || competitorsForIndication(next).length === 0) setBrand("");
                  } else {
                    const scoped = next ? brandsForIndication(next) : [];
                    if (scoped.length && brand && !scoped.includes(brand)) setBrand("");
                  }
                }}
                className={`${FIELD_CLS} mt-1`}
              >
                <option value="">Select…</option>
                {TA_GROUPS.map((e) =>
                  e.type === "option" ? (
                    <option key={e.label} value={e.label}>{e.label}</option>
                  ) : (
                    <optgroup key={e.label} label={e.label}>
                      {e.options.map((o) => <option key={o} value={o}>{o}</option>)}
                    </optgroup>
                  )
                )}
              </select>
            </div>
            <div>
              <label className="text-[11px] font-bold text-ink-light uppercase tracking-widest">Brand</label>
              <select value={brand} onChange={(e) => setBrand(e.target.value)} disabled={!ta}
                className={`${FIELD_CLS} mt-1 disabled:opacity-40`}>
                <option value="">{ta ? "Select…" : "Pick an area first"}</option>
                {brandList.map((b) => <option key={b} value={b}>{b}</option>)}
                {areaCompetitors.length > 0 && <option value={NO_BRAND}>Other (not brand-specific)</option>}
              </select>
            </div>
          </div>
          {brand === NO_BRAND && (
            <p className="text-[11px] text-ink-muted font-medium">
              Saved as a <b>disease-state</b> question (no single brand), monitored against this area's competitors: {areaCompetitors.slice(0, 3).join(", ")}{areaCompetitors.length > 3 ? ` +${areaCompetitors.length - 3} more` : ""}.
            </p>
          )}
          <div className="w-1/2">
            <label className="text-[11px] font-bold text-ink-light uppercase tracking-widest">Priority weight</label>
            <input type="number" min="0" step="0.1" value={weight} onChange={(e) => setWeight(e.target.value)} className={`${FIELD_CLS} mt-1`} />
          </div>
        </div>
        {error && <p className="text-xs font-bold text-red-600 mt-3">{error}</p>}
        <div className="flex justify-end gap-2 mt-5">
          <button onClick={onClose} className="px-4 py-2 rounded-xl text-sm font-bold text-ink-light bg-slate-100 hover:bg-slate-200 transition-colors">Cancel</button>
          <button
            onClick={submit}
            disabled={!canSave}
            className="inline-flex items-center gap-2 px-5 py-2 rounded-xl text-sm font-bold bg-brand text-white hover:bg-brand-dark disabled:opacity-40 transition-colors"
          >
            {saving ? <Spinner size={16} /> : <Check size={16} />} Save as Pending
          </button>
        </div>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Import-prompts modal: upload CSV -> review extracted questions -> add       */
/* -------------------------------------------------------------------------- */
function ImportPromptsModal({ onClose, onImported }: {
  onClose: () => void;
  onImported: () => void;
}) {
  const [phase, setPhase] = useState<"pick" | "review" | "done">("pick");
  const [file, setFile] = useState<File | null>(null);
  const [persona, setPersona] = useState("Patient");
  const [brand, setBrand] = useState("");
  const [domain, setDomain] = useState("General");
  const [drag, setDrag] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<PromptPreviewResult | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [result, setResult] = useState<PromptImportResult | null>(null);

  const canPreview = !!file && !!persona && !!brand && !busy;

  const pick = (f: File | null) => {
    if (f && !/\.csv$/i.test(f.name)) { setError("Please choose a .csv file."); return; }
    setError(null);
    setFile(f);
  };

  const runPreview = async () => {
    if (!canPreview || !file) return;
    setBusy(true);
    setError(null);
    const form = new FormData();
    form.append("file", file);
    form.append("persona", persona);
    form.append("brand_focus", brand);
    form.append("domain", domain);
    const res = await api.importPromptsPreview(form).catch(() => null);
    setBusy(false);
    if (!res || !res.ok) {
      setError((res && typeof res.data?.detail === "string" && res.data.detail) || "Couldn't read the file. Make sure it's a CSV with a prompt/question column.");
      return;
    }
    setPreview(res.data);
    setSelected(new Set(res.data.questions));
    setPhase("review");
  };

  const commit = async () => {
    if (!preview || selected.size === 0 || busy) return;
    setBusy(true);
    setError(null);
    try {
      const data = await api.importPromptsCommit({
        questions: [...selected],
        persona: preview.persona,
        brand_focus: preview.brand_focus,
        domain: preview.domain,
        therapeutic_area: preview.therapeutic_area,
        demand_origin: preview.demand_origin,
      });
      setResult(data);
      setPhase("done");
      onImported();
    } catch {
      setError("Couldn't add the questions. Please try again.");
    } finally {
      setBusy(false);
    }
  };

  const allSelected = !!preview && preview.questions.length > 0 && selected.size === preview.questions.length;
  const toggleAll = () => { if (preview) setSelected(allSelected ? new Set() : new Set(preview.questions)); };
  const toggleOne = (q: string) => setSelected((prev) => {
    const n = new Set(prev);
    if (n.has(q)) n.delete(q); else n.add(q);
    return n;
  });

  const title = phase === "review" ? "Review extracted questions" : phase === "done" ? "Import complete" : "Import questions from CSV";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div className="w-full max-w-2xl rounded-2xl bg-canvas-card p-6 shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-bold text-ink">{title}</h3>
          <button onClick={onClose} className="text-ink-light hover:text-ink transition-colors"><X size={18} /></button>
        </div>

        {phase === "pick" && (
          <div className="space-y-3">
            <p className="text-xs text-ink-light font-medium">
              Upload a CSV and we'll pull the distinct questions from its <b>prompt/question</b> column (e.g. a Profound or “People Also Ask” export). You'll review them on the next step — nothing is added until you confirm.
            </p>
            <label
              onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
              onDragLeave={() => setDrag(false)}
              onDrop={(e) => { e.preventDefault(); setDrag(false); pick(e.dataTransfer.files?.[0] ?? null); }}
              className={`flex cursor-pointer flex-col items-center justify-center gap-1 rounded-xl border-2 border-dashed px-4 py-6 text-center transition-colors ${drag ? "border-brand-light bg-brand-surface/60" : "border-line hover:border-brand-light/60"}`}
            >
              <Upload size={20} className="text-brand" />
              <span className="text-sm font-bold text-ink">{file ? file.name : "Drop a CSV here or click to choose"}</span>
              <span className="text-[11px] text-ink-muted">We read only the prompt/question column — other columns (answers, metrics) are ignored.</span>
              <input type="file" accept=".csv,text/csv" className="hidden" onChange={(e) => pick(e.target.files?.[0] ?? null)} />
            </label>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="text-[11px] font-bold text-ink-light uppercase tracking-widest">Persona</label>
                <select value={persona} onChange={(e) => setPersona(e.target.value)} className={`${FIELD_CLS} mt-1`}>
                  {["Patient", "Prospect", "Provider"].map((p) => <option key={p} value={p}>{p}</option>)}
                </select>
              </div>
              <div>
                <label className="text-[11px] font-bold text-ink-light uppercase tracking-widest">Theme</label>
                <select value={domain} onChange={(e) => setDomain(e.target.value)} className={`${FIELD_CLS} mt-1`}>
                  {["Efficacy", "Safety", "Access", "Comparative", "General"].map((d) => <option key={d} value={d}>{d}</option>)}
                </select>
              </div>
              <div className="col-span-2">
                <label className="text-[11px] font-bold text-ink-light uppercase tracking-widest">Brand</label>
                <select value={brand} onChange={(e) => setBrand(e.target.value)} className={`${FIELD_CLS} mt-1`}>
                  <option value="">Select…</option>
                  {BRAND_OPTIONS.filter(Boolean).map((b) => <option key={b} value={b}>{b}</option>)}
                </select>
                <p className="text-[11px] text-ink-muted mt-1">Applied to every imported question. The therapeutic area is detected from the brand.</p>
              </div>
            </div>
            {error && <p className="text-xs font-bold text-red-600">{error}</p>}
            <div className="flex justify-end gap-2 pt-1">
              <button onClick={onClose} className="px-4 py-2 rounded-xl text-sm font-bold text-ink-light bg-slate-100 hover:bg-slate-200 transition-colors">Cancel</button>
              <button
                onClick={runPreview}
                disabled={!canPreview}
                className="inline-flex items-center gap-2 px-5 py-2 rounded-xl text-sm font-bold bg-brand text-white hover:bg-brand-dark disabled:opacity-40 transition-colors"
              >
                {busy ? <Spinner size={16} /> : <Upload size={16} />} Extract questions
              </button>
            </div>
          </div>
        )}

        {phase === "review" && preview && (
          <div className="space-y-3">
            <p className="text-xs text-ink-light font-medium">
              <b className="text-ink">{preview.questions.length}</b> distinct question{preview.questions.length === 1 ? "" : "s"} found for <b className="text-ink">{preview.brand_focus}</b> · {preview.therapeutic_area} · {preview.persona}
              {preview.duplicates > 0 ? ` · ${preview.duplicates} duplicate${preview.duplicates === 1 ? "" : "s"} removed` : ""}
              {preview.skipped.length > 0 ? ` · ${preview.skipped.length} skipped` : ""}
            </p>

            {preview.questions.length > 0 ? (
              <>
                <div className="flex items-center justify-between">
                  <button onClick={toggleAll} className="inline-flex items-center gap-1.5 text-[11px] font-bold text-brand hover:text-brand-dark transition-colors">
                    {allSelected ? <CheckSquare size={14} /> : <Square size={14} />} {allSelected ? "Deselect all" : "Select all"}
                  </button>
                  <span className="text-[11px] font-bold text-ink-light">{selected.size} selected</span>
                </div>
                <ul className="max-h-72 overflow-y-auto rounded-xl border border-line divide-y divide-line">
                  {preview.questions.map((q) => {
                    const on = selected.has(q);
                    return (
                      <li key={q}>
                        <button onClick={() => toggleOne(q)} className={`flex w-full items-start gap-2 px-3 py-2 text-left transition-colors ${on ? "bg-brand-surface/40" : "hover:bg-slate-50"}`}>
                          {on ? <CheckSquare size={15} className="mt-0.5 shrink-0 text-brand" /> : <Square size={15} className="mt-0.5 shrink-0 text-ink-muted" />}
                          <span className="text-xs text-ink">{q}</span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </>
            ) : (
              <div className="rounded-xl border border-line bg-slate-50 p-4 text-center">
                <p className="text-sm font-bold text-ink">No new questions to add</p>
                <p className="text-xs text-ink-light mt-1">Everything in this file is already in the bank or was skipped.</p>
              </div>
            )}

            {preview.skipped.length > 0 && (
              <details className="rounded-xl border border-amber-200 bg-amber-50 p-3">
                <summary className="cursor-pointer text-xs font-bold text-amber-800">Skipped {preview.skipped.length} (PII / too short)</summary>
                <ul className="mt-2 space-y-1 max-h-32 overflow-y-auto">
                  {preview.skipped.slice(0, 20).map((s, i) => (
                    <li key={i} className="text-[11px] text-amber-700"><b>{s.reason}</b>{s.text ? ` — “${s.text}”` : ""}</li>
                  ))}
                  {preview.skipped.length > 20 && <li className="text-[11px] text-amber-700">…and {preview.skipped.length - 20} more</li>}
                </ul>
              </details>
            )}

            {error && <p className="text-xs font-bold text-red-600">{error}</p>}
            <div className="flex justify-between gap-2 pt-1">
              <button onClick={() => { setPhase("pick"); setError(null); }} className="px-4 py-2 rounded-xl text-sm font-bold text-ink-light bg-slate-100 hover:bg-slate-200 transition-colors">Back</button>
              <button
                onClick={commit}
                disabled={selected.size === 0 || busy}
                className="inline-flex items-center gap-2 px-5 py-2 rounded-xl text-sm font-bold bg-brand text-white hover:bg-brand-dark disabled:opacity-40 transition-colors"
              >
                {busy ? <Spinner size={16} /> : <Plus size={16} />} Add {selected.size} to repository
              </button>
            </div>
          </div>
        )}

        {phase === "done" && result && (
          <div className="space-y-3">
            <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-4">
              <p className="text-sm font-bold text-emerald-800">Added {result.imported} question{result.imported === 1 ? "" : "s"} as Pending</p>
              <p className="text-xs font-medium text-emerald-700 mt-1">
                {result.brand_focus} · {result.therapeutic_area} · review &amp; approve them in the bank below.
              </p>
            </div>
            {result.skipped.length > 0 && (
              <p className="text-[11px] text-amber-700">{result.skipped.length} were skipped on save (PII / duplicate).</p>
            )}
            <div className="flex justify-end">
              <button onClick={onClose} className="inline-flex items-center gap-2 px-5 py-2 rounded-xl text-sm font-bold bg-brand text-white hover:bg-brand-dark transition-colors"><Check size={16} /> Done</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default function Questions() {
  const navigate = useNavigate();
  const location = useLocation();
  const [questions, setQuestions] = useState<Question[]>([]);
  const [coverage, setCoverage] = useState<any>(null);
  // Every bank filter takes several values at once: a persona split is one list, not
  // one value, so "Patient + Provider" is a single pass instead of two.
  const [filters, setFilters] = useState<BankFilters>({
    persona: [], therapeutic_area: [], brand_focus: [], disease: [], domain: [],
  });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [reviewing, setReviewing] = useState<number | null>(null);
  const [bulkApproving, setBulkApproving] = useState(false);
  // A refused approval (the server explains why) must be shown, not swallowed — a button
  // that silently does nothing reads as a broken button.
  const [reviewError, setReviewError] = useState<string | null>(null);
  // FR-116.4 — demand ranking (priority_weight × matched search-demand volume)
  const [prioritized, setPrioritized] = useState<Record<string, PrioritizedQuestion>>({});
  const [sortByDemand, setSortByDemand] = useState(false);
  // "Analyst" filter: when on, the bank shows ONLY the curated analyst question set
  // (Rhem.csv) — matched server-side on question text — plus each one's variations.
  const [analystOnly, setAnalystOnly] = useState(false);
  const [savingWeight, setSavingWeight] = useState<number | null>(null);
  const [creating, setCreating] = useState<QuestionDraft | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  // Reverse lineage: which originals are expanded + a cache of their variation groups.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [groupCache, setGroupCache] = useState<Record<string, VariationGroupDetail | "loading" | "error">>({});
  // Forward lineage: full-text popup for the original question a variation was created from.
  const [sourcePopup, setSourcePopup] = useState<{ sourceId: string | null; sourceText: string; variationText: string } | null>(null);
  // Client-only bank filters (kept out of `filters` so they don't trigger an API refetch).
  const [search, setSearch] = useState("");
  const [sourceFilter, setSourceFilter] = useState<string[]>([]);
  // Variations (by promoted question_id) ticked inside expanded dropdowns, for the
  // "Run original + selected variations" combined run.
  const [selectedVars, setSelectedVars] = useState<Set<string>>(new Set());
  // "Run with Variations" confirm step: the server's expansion of the current selection.
  // Held here (not recomputed at Start) so the run is exactly the run that was approved.
  const [expansion, setExpansion] = useState<VariationExpansion | null>(null);
  const [expanding, setExpanding] = useState(false);
  // One channel for "the run action failed", whether working out the expansion or starting the
  // run itself. A refused start that says nothing is indistinguishable from a broken button.
  const [runError, setRunError] = useState<string | null>(null);

  const load = () => {
    const p = new URLSearchParams();
    // Repeated params (persona=Patient&persona=Provider) — the API ORs within a field.
    filters.persona.forEach((v) => p.append("persona", v));
    filters.therapeutic_area.forEach((v) => p.append("therapeutic_area", v));
    filters.brand_focus.forEach((v) => p.append("brand_focus", v));
    filters.domain.forEach((v) => p.append("domain", v));
    if (analystOnly) p.set("analyst", "1");
    p.set("limit", "500");
    api.questions(`?${p.toString()}`).then(setQuestions).catch(() => {});
  };

  const loadPrioritized = () => {
    api.prioritizedQuestions().then((r) => {
      const map: Record<string, PrioritizedQuestion> = {};
      for (const it of r.items) map[it.question_id] = it;
      setPrioritized(map);
    }).catch(() => {});
  };

  const updateWeight = async (q: Question, raw: string) => {
    const w = parseFloat(raw);
    const current = q.priority_weight ?? 1;
    if (isNaN(w) || w < 0 || w === current) return;
    setSavingWeight(q.id);
    try {
      await api.updateQuestion(q.id, { priority_weight: w });
      load();
      loadPrioritized();
    } finally {
      setSavingWeight(null);
    }
  };

  useEffect(() => {
    api.coverage().then(setCoverage).catch(() => {});
    loadPrioritized();
    // Arriving from a Prompt Volume coverage gap? Open a pre-filled draft, then clear the
    // router state so a refresh / back-nav doesn't reopen it.
    const draft = (location.state as { createDraft?: QuestionDraft } | null)?.createDraft;
    if (draft) {
      setCreating(draft);
      navigate(location.pathname, { replace: true });
    }
  }, []);
  useEffect(load, [filters, analystOnly]);

  const toggle = (qid: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(qid)) next.delete(qid);
      else next.add(qid);
      return next;
    });

  // Tick/untick a single (approved) variation inside a dropdown for the combined run.
  const toggleVar = (pid: string) =>
    setSelectedVars((prev) => {
      const next = new Set(prev);
      if (next.has(pid)) next.delete(pid);
      else next.add(pid);
      return next;
    });

  // Reverse lineage: expand an original to reveal the variations created from it (lazy-loaded).
  const toggleExpand = (qid: string) => {
    const willOpen = !expanded.has(qid);
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(qid)) next.delete(qid);
      else next.add(qid);
      return next;
    });
    if (willOpen && groupCache[qid] === undefined) {
      setGroupCache((c) => ({ ...c, [qid]: "loading" }));
      api.variationGroup(qid)
        .then((g) => setGroupCache((c) => ({ ...c, [qid]: g })))
        .catch(() => setGroupCache((c) => ({ ...c, [qid]: "error" })));
    }
  };

  // Demo quick-filter: "Rheumatology Only" is just a shortcut that pins the
  // therapeutic-area filter to Rheumatology (composes with Workshop Questions). It reads
  // as active only when Rheumatology is the SOLE area — "only" has to mean only.
  const rheumOnly = filters.therapeutic_area.length === 1 && filters.therapeutic_area[0] === "Rheumatology";

  // Brand + Disease dropdowns are scoped to the selected Therapeutic Areas (e.g. Neuroscience ->
  // Vraylar only), unioned across them. Areas with no AbbVie focus brand (e.g. Obesity) fall back
  // to the full lists so the field is never a dead end.
  const taScopedBrands = [...new Set(filters.therapeutic_area.flatMap((ta) => brandsForIndication(ta)))];
  const brandFilterOptions = taScopedBrands.length ? taScopedBrands : BRAND_OPTIONS;
  const taScopedDiseases = [...new Set(filters.therapeutic_area.flatMap((ta) => diseasesForIndication(ta)))];
  const diseaseFilterOptions = taScopedDiseases.length ? taScopedDiseases : DISEASE_OPTIONS;

  const searchTerm = search.trim().toLowerCase();
  const visibleQuestions = questions.filter((q) => {
    // Variations never appear as their own top-level rows — they live only inside the
    // parent original's expandable variations dropdown.
    if (q.is_variation) return false;
    if (filters.disease.length) {
      const tagged = (q.disease ?? "").trim();
      const covered = DESIGNATION_DISEASES[q.designation ?? ""] ?? (tagged ? [tagged] : []);
      const matchesDisease = covered.length
        ? filters.disease.some((d) => covered.includes(d))
        : filters.disease.some((d) => (DISEASE_BRAND_MAP[d] ?? []).includes(q.brand_focus ?? ""));
      if (!matchesDisease) return false;
    }
    if (sourceFilter.length && !sourceFilter.includes(q.source ?? "")) return false;
    if (searchTerm && !`${q.question_text} ${q.question_id} ${q.brand_focus ?? ""}`.toLowerCase().includes(searchTerm)) return false;
    return true;
  });

  const demandFor = (qid: string) => prioritized[qid]?.demand_score ?? 0;
  // "Sort by demand" overrides date order; otherwise sort by date added in the chosen direction.
  const rankedQuestions = sortByDemand
    ? [...visibleQuestions].sort((a, b) => demandFor(b.question_id) - demandFor(a.question_id))
    : [...visibleQuestions].sort(
        (a, b) => new Date(b.created_at ?? 0).getTime() - new Date(a.created_at ?? 0).getTime()
      );

  const allVisibleSelected =
    visibleQuestions.length > 0 && visibleQuestions.every((q) => selected.has(q.question_id));

  const toggleAll = () =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (allVisibleSelected) visibleQuestions.forEach((q) => next.delete(q.question_id));
      else visibleQuestions.forEach((q) => next.add(q.question_id));
      return next;
    });

  const runQuestions = async (ids: string[]) => {
    if (ids.length === 0 || busy) return;
    setBusy(true);
    setRunError(null);
    try {
      await api.createRun({ trigger: "ADHOC", question_ids: ids });
      navigate("/run-analysis");
    } catch (e) {
      setRunError(e instanceof Error ? e.message : "The run could not be started.");
      setBusy(false);
    }
  };

  // "Run with Variations": ask the server what the selection actually expands to, then show
  // the numbers before anything is spent. The expansion is read-only — it approves nothing —
  // and only variations a human already cleared come back, so the review gate is untouched.
  const openRunWithVariations = async () => {
    if (!canRunSelected || expanding) return;
    setExpanding(true);
    setRunError(null);
    try {
      setExpansion(await api.expandQuestionVariations([...selected]));
    } catch (e) {
      setRunError(e instanceof Error ? e.message : "Could not work out what would run.");
    } finally {
      setExpanding(false);
    }
  };

  const closeExpansion = () => { setExpansion(null); setRunError(null); };

  // Jump to the Phrasing Variation page with this question pre-selected. Drafts are
  // generated there when the user clicks "Generate drafts" (not on this click).
  const makeVariations = (q: Question) => {
    const qs = new URLSearchParams({ base: String(q.id) });
    if ((q.variation_count ?? 0) > 0) qs.set("group", q.question_id);
    navigate(`/run-analysis/variations?${qs.toString()}`);
  };

  // Harvested questions land in the repository as PENDING. Reviewers approve/deny
  // them here; only APPROVED questions can be run.
  const review = async (q: Question, approval_status: "APPROVED" | "REJECTED") => {
    if (reviewing !== null) return;
    setReviewing(q.id);
    setReviewError(null);
    try {
      await api.updateQuestion(q.id, { approval_status });
      setSelected((prev) => { const next = new Set(prev); next.delete(q.question_id); return next; });
      load();
      loadPrioritized();
    } catch (e) {
      setReviewError(
        `${q.question_id}: ${e instanceof Error ? e.message : "the update could not be saved."}`
      );
    } finally {
      setReviewing(null);
    }
  };

  // Selected questions that are still PENDING — only these can be bulk-approved.
  const selectedPending = visibleQuestions.filter(
    (q) => selected.has(q.question_id) && q.approval_status === "PENDING"
  );

  // All currently selected questions. Run Selected is only allowed when every
  // selected question is APPROVED — unapproved questions can't be run.
  const selectedQuestions = visibleQuestions.filter((q) => selected.has(q.question_id));
  const canRunSelected =
    selectedQuestions.length > 0 &&
    selectedQuestions.every((q) => q.approval_status === "APPROVED");
  // The row chip counts every staged draft (approved + pending + rejected), so this only
  // says "there is something to expand" — how many may actually run is the server's answer.
  const selectionHasVariations = selectedQuestions.some((q) => (q.variation_count ?? 0) > 0);

  // Bulk-approve every selected PENDING question in one pass.
  const bulkApprove = async () => {
    if (selectedPending.length === 0 || bulkApproving) return;
    setBulkApproving(true);
    setReviewError(null);
    try {
      // Each refusal is reported with the question it belongs to; the rest still go through,
      // so one blocked question cannot quietly cost the reviewer the whole batch.
      const failures: string[] = [];
      await Promise.all(
        selectedPending.map((q) =>
          api.updateQuestion(q.id, { approval_status: "APPROVED" }).catch((e) => {
            failures.push(`${q.question_id}: ${e instanceof Error ? e.message : "could not be approved."}`);
            return null;
          })
        )
      );
      setSelected((prev) => {
        const next = new Set(prev);
        selectedPending.forEach((q) => next.delete(q.question_id));
        return next;
      });
      if (failures.length) {
        setReviewError(
          `${failures.length} of ${selectedPending.length} could not be approved \u2014 ${failures.join(" | ")}`
        );
      }
      load();
    } finally {
      setBulkApproving(false);
    }
  };

  return (
    <div className="space-y-8">
      <PageHeader
        title="Approved Question Bank"
        subtitle="Your approved library of patient questions. Only questions cleared by Medical Affairs can go into a run."
      />

      {coverage && (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <AnimatedCard delay={0}>
            <Stat
              label="Approved & Active"
              value={coverage.total_active_approved}
              icon={<FileText size={16} />}
            />
          </AnimatedCard>
          <AnimatedCard delay={0.05}>
            <Stat
              label="Personas"
              value={Object.keys(coverage.by_persona || {}).length}
              sub={Object.entries(coverage.by_persona || {}).map(([k, v]) => `${k}: ${v}`).join(" · ")}
              icon={<Users size={16} />}
            />
          </AnimatedCard>
          <AnimatedCard delay={0.1}>
            <Stat
              label="Therapeutic Areas"
              value={Object.keys(coverage.by_area || {}).length}
              sub={Object.entries(coverage.by_area || {}).map(([k, v]) => `${k}: ${v}`).join(" · ")}
              icon={<Layers size={16} />}
            />
          </AnimatedCard>
          <AnimatedCard delay={0.15}>
            <Stat
              label="Themes"
              value={Object.keys(coverage.by_domain || {}).length}
              icon={<Grid3X3 size={16} />}
            />
          </AnimatedCard>
        </div>
      )}

      <Card>
        <div className="relative mb-4">
          <Search size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-muted" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search questions by text, ID, or brand…"
            className="w-full rounded-xl border border-line bg-canvas-card py-2.5 pl-9 pr-9 text-sm font-medium text-ink focus:outline-none focus:ring-2 focus:ring-brand-light/40 focus:border-brand-light transition-colors"
          />
          {search && (
            <button
              onClick={() => setSearch("")}
              title="Clear search"
              className="absolute right-3 top-1/2 -translate-y-1/2 text-ink-muted hover:text-ink transition-colors"
            >
              <X size={15} />
            </button>
          )}
        </div>
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div className="flex flex-wrap gap-4">
            <MultiSelect label="Persona" values={filters.persona} options={["Prospect", "Patient", "Provider"]}
                    placeholder="All personas"
                    onChange={(v) => setFilters({ ...filters, persona: v })}
                    tooltip={"Who is asking the question:\n• Prospect: exploring treatment options\n• Patient: currently on treatment\n• Provider: a clinician"} />
            <MultiSelect label="Therapeutic Area & Indication" values={filters.therapeutic_area} groups={TA_GROUPS}
                    placeholder="All areas"
                    onChange={(v) => {
                      // Drop now-invalid brand/disease picks when the area selection changes so
                      // the filters don't silently combine into an empty result (e.g. Humira +
                      // Neuroscience). Areas are unioned, so a pick survives if ANY area allows it.
                      const brands = v.flatMap((ta) => brandsForIndication(ta));
                      const diseases = v.flatMap((ta) => diseasesForIndication(ta));
                      setFilters({
                        ...filters,
                        therapeutic_area: v,
                        brand_focus: brands.length ? filters.brand_focus.filter((b) => brands.includes(b)) : filters.brand_focus,
                        disease: diseases.length ? filters.disease.filter((d) => diseases.includes(d)) : filters.disease,
                      });
                    }}
                    tooltip={"The disease area or specific indication this question targets.\nExamples: Dermatology, Gastroenterology, Oncology, Endometriosis."} />
            <MultiSelect label="Brand" values={filters.brand_focus} options={brandFilterOptions}
                    placeholder="All brands"
                    onChange={(v) => setFilters({ ...filters, brand_focus: v })}
                    tooltip={"Filter by the AbbVie brand this question focuses on. Scoped to the selected therapeutic areas."} />
            <MultiSelect label="Disease" values={filters.disease} options={diseaseFilterOptions}
                    placeholder="All diseases"
                    onChange={(v) => setFilters({ ...filters, disease: v })}
                    tooltip={"Filter by the specific disease/indication a question is tagged with.\nUntagged questions fall back to the brands that treat the condition."} />
            <MultiSelect label="Theme" values={filters.domain} options={["Efficacy", "Safety", "Access", "Comparative", "General"]}
                    placeholder="All themes"
                    onChange={(v) => setFilters({ ...filters, domain: v })}
                    tooltip={"The theme of the question:\n• Efficacy: treatment outcomes & dosing\n• Safety: side effects & risks\n• Access: coverage & cost\n• Comparative: vs. competitors\n• General: broad or exploratory"} />
            <MultiSelect label="Source" values={sourceFilter} options={["MANUAL", "PROMPT_VOLUME", "DISCOVER", "VARIATION"]} optionLabels={SOURCE_LABELS}
                    placeholder="All sources"
                    onChange={setSourceFilter}
                    tooltip={"Where this question came from:\n• Manual: authored by an analyst\n• Prompt Volume: bulk prompt/keyword import\n• Discover Questions: harvested from the web\n• Variation: AI-generated paraphrase"} />
          </div>
          <div className="flex items-center gap-3 rounded-xl border border-cyan-200 bg-cyan-50/70 px-4 py-3">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-cyan-100 text-cyan-700">
              <Stethoscope size={18} />
            </div>
            <div className="leading-snug">
              <p className="text-sm font-bold text-cyan-900">Want EvidenceMD in the mix?</p>
              <p className="text-xs font-medium text-cyan-700">
                Choose the <span className="font-bold">Provider</span> persona. It's the only one that includes it.
              </p>
            </div>
          </div>
        </div>
      </Card>

      <Card>
        <div className="flex items-center justify-between gap-3 mb-4 flex-wrap">
          <div>
            <h3 className="text-xs font-bold text-ink uppercase tracking-widest">{visibleQuestions.length} Questions</h3>
            <p className="text-xs text-ink-light mt-1 font-medium">
              Select one or more questions to run a quick test pass before launching the full repository.
            </p>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            <button
              onClick={() => setCreating({})}
              className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-bold text-brand bg-brand-surface hover:bg-brand hover:text-white transition-colors"
            >
              <Plus size={14} /> New question
            </button>
            <button
              onClick={() => setImportOpen(true)}
              title="Bulk-import real questions from a CSV (e.g. a Profound / People-Also-Ask export)"
              className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-bold text-brand bg-brand-surface hover:bg-brand hover:text-white transition-colors"
            >
              <Upload size={14} /> Import prompts
            </button>
            <button
              onClick={() => setAnalystOnly((s) => !s)}
              title="Demo shortcut: show only the workshop question set (from Rhem.csv) and their phrasing variations"
              className={demoFilterCls(analystOnly)}
            >
              <Filter size={14} /> Workshop Questions
            </button>
            <button
              onClick={() => setFilters({ ...filters, therapeutic_area: rheumOnly ? [] : ["Rheumatology"] })}
              title="Demo shortcut: show only Rheumatology questions"
              className={demoFilterCls(rheumOnly)}
            >
              <Filter size={14} /> Rheumatology Only
            </button>
            <button
              onClick={() => setSortByDemand((s) => !s)}
              title="Sort the bank by demand = priority weight × matched search-demand volume"
              className={`flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-bold transition-colors ${sortByDemand ? "bg-brand text-white" : "text-ink-light bg-slate-100 hover:bg-slate-200"}`}
            >
              <TrendingUp size={14} /> Sort by demand
            </button>
            {selected.size > 0 && (
              <button
                onClick={() => setSelected(new Set())}
                className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-bold text-ink-light bg-slate-100 hover:bg-slate-200 transition-colors"
              >
                <X size={14} /> Clear ({selected.size})
              </button>
            )}
            <button
              disabled={selectedPending.length === 0 || bulkApproving}
              onClick={bulkApprove}
              className="flex items-center gap-2 px-5 py-2.5 bg-teal-600 text-white rounded-xl text-sm font-bold hover:bg-teal-700 disabled:opacity-40 transition-colors"
            >
              {bulkApproving ? <Spinner size={16} /> : <Check size={16} />}
              {bulkApproving ? "Approving\u2026" : `Approve Selected${selectedPending.length > 0 ? ` (${selectedPending.length})` : ""}`}
            </button>
            <button
              disabled={!canRunSelected || !selectionHasVariations || busy || expanding}
              onClick={openRunWithVariations}
              title={
                selected.size > 0 && !canRunSelected
                  ? "Only approved questions can be run. Approve the selected questions first."
                  : selected.size > 0 && !selectionHasVariations
                    ? "None of the selected questions have variations yet \u2014 use Create variations first."
                    : "Run each selected question together with the variations already approved for it"
              }
              className="flex items-center gap-2 px-5 py-2.5 rounded-xl border-2 border-brand text-brand bg-canvas-card text-sm font-bold hover:bg-brand-surface disabled:opacity-40 disabled:hover:bg-canvas-card transition-colors"
            >
              {expanding ? <Spinner size={16} /> : <GitBranch size={16} />}
              {expanding ? "Checking\u2026" : `Run with Variations${selected.size > 0 ? ` (${selected.size})` : ""}`}
            </button>
            <button
              disabled={!canRunSelected || busy}
              onClick={() => runQuestions([...selected])}
              title={
                selected.size > 0 && !canRunSelected
                  ? "Only approved questions can be run. Approve the selected questions first."
                  : undefined
              }
              className="flex items-center gap-2 px-5 py-2.5 bg-brand text-white rounded-xl text-sm font-bold hover:bg-brand-dark disabled:opacity-40 transition-colors"
            >
              {busy ? <Spinner size={16} /> : <Play size={16} />}
              {busy ? "Starting\u2026" : `Run Selected${selected.size > 0 ? ` (${selected.size})` : ""}`}
            </button>
          </div>
        </div>
        {runError && !expansion && (
          <div className="mt-4 flex items-start gap-2 rounded-xl border border-red-200 bg-red-50 px-4 py-3">
            <p className="flex-1 text-xs font-bold text-red-700">{runError}</p>
            <button
              onClick={() => setRunError(null)}
              title="Dismiss"
              className="text-red-400 hover:text-red-700 transition-colors"
            >
              <X size={14} />
            </button>
          </div>
        )}
        {reviewError && (
          <div className="mt-4 flex items-start gap-2 rounded-xl border border-red-200 bg-red-50 px-4 py-3">
            <p className="flex-1 text-xs font-bold text-red-700">{reviewError}</p>
            <button
              onClick={() => setReviewError(null)}
              title="Dismiss"
              className="text-red-400 hover:text-red-700 transition-colors"
            >
              <X size={14} />
            </button>
          </div>
        )}
        {visibleQuestions.length === 0 ? (
          <EmptyState message="No questions match the current filters." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] xl:min-w-[880px] text-sm">
              <thead>
                <tr className="text-left border-b-2 border-slate-200">
                  <th className="pb-3 pr-3 w-8">
                    <button
                      onClick={toggleAll}
                      className="text-ink-light hover:text-brand transition-colors align-middle"
                      title={allVisibleSelected ? "Deselect all" : "Select all"}
                    >
                      {allVisibleSelected ? <CheckSquare size={18} className="text-brand" /> : <Square size={18} />}
                    </button>
                  </th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">
                    <span className="inline-flex items-center gap-1"><InfoTooltip content={"Unique identifier for this question in the repository.\nUsed to reference it in runs, responses, and audit logs."} /> ID</span>
                  </th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">
                    <span className="inline-flex items-center gap-1"><InfoTooltip content={"Who is asking the question:\n• Prospect: exploring treatment options\n• Patient: currently on treatment\n• Provider: a clinician"} /> Persona</span>
                  </th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">
                    <span className="inline-flex items-center gap-1"><InfoTooltip content={"Workshop designation from Rhem.csv: Persona + indication (e.g. Patient RA, HCP PsA). Blank for questions outside the workshop set."} /> Designation</span>
                  </th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">
                    <span className="inline-flex items-center gap-1"><InfoTooltip content={"What this question monitors: a specific brand or the wider disease state."} /> Focus</span>
                  </th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">
                    <span className="inline-flex items-center gap-1"><InfoTooltip content={"The theme of the question:\n• Efficacy: treatment outcomes & dosing\n• Safety: side effects & risks\n• Access: coverage & cost\n• Comparative: vs. competitors\n• General: broad or exploratory"} /> Theme</span>
                  </th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">
                    <span className="inline-flex items-center gap-1"><InfoTooltip content={"The full text of the question as it will be sent to AI platforms."} /> Question</span>
                  </th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">
                    <span className="inline-flex items-center gap-1"><InfoTooltip content={"Where this question came from:\n• Manual: authored by an analyst\n• Prompt Volume: bulk prompt/keyword import\n• Discover: harvested from the web\n• Variation: AI-generated paraphrase"} /> Source</span>
                  </th>
                  <th className="hidden xl:table-cell pb-3 font-bold text-xs text-ink-light uppercase tracking-widest text-right">
                    <span className="inline-flex items-center justify-end gap-1"><InfoTooltip content={"Third-party search-demand volume (a proxy for AI-inquiry demand) matched to this question, summed over deduplicated keywords."} /> Volume</span>
                  </th>
                  <th className="hidden xl:table-cell pb-3 font-bold text-xs text-ink-light uppercase tracking-widest text-right">
                    <span className="inline-flex items-center justify-end gap-1"><InfoTooltip content={"Internal priority weight (editable). Demand = weight × matched volume."} /> Weight</span>
                  </th>
                  <th className="hidden xl:table-cell pb-3 font-bold text-xs text-ink-light uppercase tracking-widest text-right">
                    <span className="inline-flex items-center justify-end gap-1"><InfoTooltip content={"Demand = priority weight × matched search-demand volume (FR-116.4)."} /> Demand</span>
                  </th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">
                    <span className="inline-flex items-center gap-1"><InfoTooltip content={"Approval stage of the question:\n• Approved: cleared by Medical Affairs, ready to run\n• Pending: awaiting Medical Affairs review\n• Rejected: not approved for use"} /> Status</span>
                  </th>
                  <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest text-right">
                    <span className="inline-flex items-center justify-end gap-1"><InfoTooltip content={"Available actions for this question:\n• Approve / Deny: review pending questions\n• Run: run an approved question immediately"} /> Action</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {rankedQuestions.map((q, i) => {
                  const isSel = selected.has(q.question_id);
                  const isPending = q.approval_status === "PENDING";
                  const isReviewing = reviewing === q.id;
                  const d = prioritized[q.question_id];
                  const isExpanded = expanded.has(q.question_id);
                  const group = groupCache[q.question_id];
                  const varCount = q.variation_count ?? 0;
                  const groupDetail = group && group !== "loading" && group !== "error" ? group : null;
                  const selectedVarIds = groupDetail
                    ? groupDetail.drafts
                        .filter((v) => v.status === "APPROVED" && v.promoted_question_id && selectedVars.has(v.promoted_question_id))
                        .map((v) => v.promoted_question_id as string)
                    : [];
                  return (
                    <Fragment key={q.id}>
                    <motion.tr
                      initial={{ opacity: 0, y: 6 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ delay: Math.min(i * 0.015, 0.5) }}
                      onClick={() => toggle(q.question_id)}
                      className={`border-b border-slate-100 cursor-pointer transition-colors ${isSel ? "bg-brand-surface/60" : "hover:bg-brand-surface/50"}`}
                    >
                      <td className="py-3 pr-3">
                        <button
                          onClick={(e) => { e.stopPropagation(); toggle(q.question_id); }}
                          className="text-ink-light hover:text-brand transition-colors align-middle"
                        >
                          {isSel ? <CheckSquare size={18} className="text-brand" /> : <Square size={18} />}
                        </button>
                      </td>
                      <td className="py-3 text-ink-light text-xs font-mono font-bold">
                        {q.question_id}
                        {q.is_variation && <span className="ml-1.5 px-1.5 py-0.5 rounded bg-violet-100 text-violet-700 text-[10px] font-bold">var</span>}
                      </td>
                      <td className="py-3 font-semibold text-ink">{q.persona}</td>
                      <td className="py-3">
                        {q.designation ? (
                          <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-bold whitespace-nowrap ${DESIGNATION_CLS[q.designation] ?? "bg-slate-100 text-ink-light"}`}>
                            {q.designation}
                          </span>
                        ) : (
                          <span className="text-ink-muted text-xs">—</span>
                        )}
                      </td>
                      <td className="py-3 font-medium text-ink">{q.monitoring_mode === "DISEASE_STATE" ? <span className="px-2 py-0.5 rounded-full bg-violet-100 text-violet-700 text-[11px] font-bold" title={(q.competitor_focus ?? []).join(", ")}>Disease state</span> : q.brand_focus}</td>
                      <td className="py-3"><ThemeBadge theme={q.domain} /></td>
                      <td className="py-3 max-w-[180px] lg:max-w-xs xl:max-w-md text-ink-light font-medium">
                        <div className="flex items-center gap-2">
                          <Tooltip content={q.question_text} width={360} className="min-w-0 truncate cursor-help">{q.question_text}</Tooltip>
                          <QuestionOriginBadge origin={q.demand_origin} />
                        </div>
                        {q.is_variation && (q.variation_of_text || q.variation_of) && (
                          <button
                            onClick={(e) => { e.stopPropagation(); setSourcePopup({ sourceId: q.variation_of ?? null, sourceText: q.variation_of_text || q.variation_of || "", variationText: q.question_text }); }}
                            title="Click to view the full original question"
                            className="mt-1 inline-flex max-w-full items-center gap-1 rounded-md bg-violet-50 px-2 py-0.5 text-[11px] font-semibold text-violet-700 hover:bg-violet-100 hover:text-violet-900 transition-colors"
                          >
                            <GitBranch size={11} className="shrink-0" />
                            <span className="truncate">Variation of: {q.variation_of_text || q.variation_of}</span>
                          </button>
                        )}
                        {!q.is_variation && varCount > 0 && (
                          <button
                            onClick={(e) => { e.stopPropagation(); toggleExpand(q.question_id); }}
                            title="Show the variations created from this question"
                            className={`mt-1.5 inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-bold shadow-sm transition-colors ${isExpanded ? "bg-brand text-white" : "bg-brand-surface text-brand hover:bg-brand hover:text-white"}`}
                          >
                            <GitBranch size={13} className="shrink-0" />
                            {varCount} variation{varCount === 1 ? "" : "s"}
                            {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                          </button>
                        )}
                      </td>
                      <td className="py-3">
                        {q.source && SOURCE_META[q.source] && (
                          <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-bold ${SOURCE_META[q.source].cls}`}>
                            {SOURCE_META[q.source].label}
                          </span>
                        )}
                      </td>
                      <td className="hidden xl:table-cell py-3 text-right tabular-nums text-ink-light">{d ? d.search_volume.toLocaleString() : "N/A"}</td>
                      <td className="hidden xl:table-cell py-3 text-right" onClick={(e) => e.stopPropagation()}>
                        <input
                          type="number" min="0" step="0.1"
                          defaultValue={q.priority_weight ?? 1}
                          disabled={savingWeight === q.id}
                          onClick={(e) => e.stopPropagation()}
                          onBlur={(e) => updateWeight(q, e.target.value)}
                          className="w-16 border border-line rounded-lg px-2 py-1 text-xs text-right tabular-nums bg-canvas-card focus:outline-none focus:ring-2 focus:ring-brand-light/40"
                        />
                      </td>
                      <td className="hidden xl:table-cell py-3 text-right tabular-nums font-bold text-ink">{d ? d.demand_score.toLocaleString() : "N/A"}</td>
                      <td className="py-3">
                        <span className={`px-2.5 py-1 rounded-full text-xs font-bold ${STATUS_COLORS[q.approval_status] || "bg-slate-100 text-ink-light"}`}>
                          {q.approval_status}
                        </span>
                      </td>
                      <td className="py-3 text-right">
                        {isPending ? (
                          <div className="inline-flex items-center gap-2">
                            <button
                              disabled={reviewing !== null}
                              onClick={(e) => { e.stopPropagation(); review(q, "APPROVED"); }}
                              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-bold bg-teal-50 text-teal-700 hover:bg-teal-600 hover:text-white disabled:opacity-40 transition-colors"
                            >
                              {isReviewing ? <Spinner size={12} /> : <Check size={12} />} Approve
                            </button>
                            <button
                              disabled={reviewing !== null}
                              onClick={(e) => { e.stopPropagation(); review(q, "REJECTED"); }}
                              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-bold bg-red-50 text-red-700 hover:bg-red-600 hover:text-white disabled:opacity-40 transition-colors"
                            >
                              <Ban size={12} /> Deny
                            </button>
                            <button
                              disabled
                              title="Approve this question before running it"
                              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-bold bg-slate-100 text-ink-muted opacity-50 cursor-not-allowed"
                            >
                              <Play size={12} /> Run
                            </button>
                          </div>
                        ) : (
                          <div className="inline-flex items-center gap-2">
                            {q.approval_status === "APPROVED" && !q.is_variation && (
                              <button
                                onClick={(e) => { e.stopPropagation(); makeVariations(q); }}
                                title="Generate phrasing variations of this question"
                                className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-bold bg-brand-surface text-brand hover:bg-brand-light/20 disabled:opacity-40 transition-colors"
                              >
                                <Wand2 size={12} /> Create variations
                              </button>
                            )}
                            <button
                              disabled={busy}
                              onClick={(e) => { e.stopPropagation(); runQuestions([q.question_id]); }}
                              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-bold bg-brand text-white hover:bg-brand-dark disabled:opacity-40 transition-colors"
                            >
                              <Play size={12} /> Run
                            </button>
                          </div>
                        )}
                      </td>
                    </motion.tr>
                    {isExpanded && (
                      <tr className="border-b border-slate-100 bg-slate-50/70">
                        <td colSpan={13} className="px-6 py-3">
                          {(!group || group === "loading") && (
                            <div className="flex items-center gap-2 text-xs font-medium text-ink-light">
                              <Spinner size={14} /> Loading variations…
                            </div>
                          )}
                          {group === "error" && (
                            <div className="text-xs font-bold text-red-600">Could not load variations. Try again.</div>
                          )}
                          {group && group !== "loading" && group !== "error" && (
                            group.drafts.length === 0 ? (
                              <div className="text-xs font-medium text-ink-light">No variations recorded for this question.</div>
                            ) : (
                              <div className="space-y-2">
                                <div className="flex flex-wrap items-center justify-between gap-2">
                                  <p className="text-[11px] font-bold text-ink-light uppercase tracking-widest">
                                    Variations of {q.question_id} · {group.counts.approved} approved · {group.counts.draft} pending · {group.counts.rejected} rejected
                                  </p>
                                  <button
                                    disabled={busy || q.approval_status !== "APPROVED"}
                                    onClick={(e) => { e.stopPropagation(); runQuestions([q.question_id, ...selectedVarIds]); }}
                                    title={q.approval_status !== "APPROVED" ? "Approve the original question before running it" : "Run the original question together with the selected variations"}
                                    className="inline-flex shrink-0 items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-bold bg-brand text-white hover:bg-brand-dark disabled:opacity-40 transition-colors"
                                  >
                                    <Play size={12} /> Run original{selectedVarIds.length > 0 ? ` + ${selectedVarIds.length} selected` : ""}
                                  </button>
                                </div>
                                <div className="overflow-hidden rounded-xl border border-line">
                                  <div className="flex items-start gap-3 border-b border-line bg-slate-100/70 px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider text-ink-muted">
                                    <span className="w-5 shrink-0" aria-hidden="true"></span>
                                    <span className="w-24 shrink-0">Status</span>
                                    <span className="flex-1 min-w-0">Question</span>
                                    <span className="w-28 shrink-0 text-right">ID</span>
                                    <span className="w-16 shrink-0 text-right">Run</span>
                                  </div>
                                  <ul className="divide-y divide-line">
                                    {group.drafts.map((v) => {
                                      const pid = v.promoted_question_id;
                                      const canRun = v.status === "APPROVED" && !!pid;
                                      const isVarSel = canRun && !!pid && selectedVars.has(pid);
                                      return (
                                        <li key={v.id} className="flex items-start gap-3 bg-canvas-card px-3 py-2">
                                          <button
                                            disabled={!canRun}
                                            onClick={(e) => { e.stopPropagation(); if (pid) toggleVar(pid); }}
                                            title={canRun ? "Select this variation to include in the combined run" : "Only approved variations can be run"}
                                            className="mt-0.5 w-5 shrink-0 text-ink-light hover:text-brand disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                                          >
                                            {isVarSel ? <CheckSquare size={16} className="text-brand" /> : <Square size={16} />}
                                          </button>
                                          <span className="w-24 shrink-0">
                                            <span className={`mt-0.5 inline-flex rounded-full px-2 py-0.5 text-[10px] font-bold ${VAR_STATUS_CLS[v.status] || "bg-slate-100 text-ink-light"}`}>
                                              {v.status}
                                            </span>
                                          </span>
                                          <span className="flex-1 min-w-0 text-xs text-ink">{v.variation_text}</span>
                                          <span className="mt-0.5 w-28 shrink-0 text-right font-mono text-[10px] text-ink-muted" title="Question ID">{pid ?? "—"}</span>
                                          <span className="w-16 shrink-0 text-right">
                                            {canRun && (
                                              <button
                                                disabled={busy}
                                                onClick={(e) => { e.stopPropagation(); if (pid) runQuestions([pid]); }}
                                                title="Run just this variation"
                                                className="inline-flex items-center gap-1 px-2 py-1 rounded-lg text-[11px] font-bold bg-brand-surface text-brand hover:bg-brand hover:text-white disabled:opacity-40 transition-colors"
                                              >
                                                <Play size={11} /> Run
                                              </button>
                                            )}
                                          </span>
                                        </li>
                                      );
                                    })}
                                  </ul>
                                </div>
                              </div>
                            )
                          )}
                        </td>
                      </tr>
                    )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {sourcePopup && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={() => setSourcePopup(null)}>
          <div className="w-full max-w-lg rounded-2xl bg-canvas-card p-6 shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <div className="mb-4 flex items-center justify-between">
              <h3 className="flex items-center gap-2 text-sm font-bold text-ink"><GitBranch size={15} className="text-violet-600" /> Variation lineage</h3>
              <button onClick={() => setSourcePopup(null)} className="text-ink-light hover:text-ink transition-colors"><X size={18} /></button>
            </div>
            <div className="space-y-4">
              <div>
                <p className="text-[11px] font-bold uppercase tracking-widest text-violet-700">Original question{sourcePopup.sourceId ? ` · ${sourcePopup.sourceId}` : ""}</p>
                <p className="mt-1 whitespace-pre-wrap text-sm leading-relaxed text-ink">{sourcePopup.sourceText}</p>
              </div>
              <div className="border-t border-line pt-4">
                <p className="text-[11px] font-bold uppercase tracking-widest text-ink-light">This variation</p>
                <p className="mt-1 whitespace-pre-wrap text-sm leading-relaxed text-ink-light">{sourcePopup.variationText}</p>
              </div>
            </div>
            <div className="mt-6 flex justify-end">
              <button onClick={() => setSourcePopup(null)} className="rounded-xl bg-slate-100 px-4 py-2 text-sm font-bold text-ink-light hover:bg-slate-200 transition-colors">Close</button>
            </div>
          </div>
        </div>
      )}

      {expansion && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={closeExpansion}>
          <div className="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-2xl bg-canvas-card p-6 shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <div className="mb-4 flex items-center justify-between">
              <h3 className="flex items-center gap-2 text-sm font-bold text-ink">
                <GitBranch size={15} className="text-brand" /> Run with variations
              </h3>
              <button onClick={closeExpansion} className="text-ink-light hover:text-ink transition-colors"><X size={18} /></button>
            </div>

            {/* The size of the run, stated before it is bought. */}
            <div className="rounded-xl border border-brand-light/40 bg-brand-surface px-4 py-3">
              <p className="text-sm font-bold text-ink">
                {expansion.total} question{expansion.total === 1 ? "" : "s"} will run
              </p>
              <p className="mt-1 text-xs font-medium text-ink-light">
                {expansion.base_count} selected + {expansion.variation_count} approved variation
                {expansion.variation_count === 1 ? "" : "s"}, each asked of every enabled model.
              </p>
            </div>

            {expansion.variation_count === 0 && (
              <div className="mt-3 flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3">
                <AlertTriangle size={15} className="mt-0.5 shrink-0 text-amber-600" />
                <p className="text-xs font-medium text-amber-800">
                  <span className="font-bold">No variations are approved yet</span>, so this is the
                  same run as <span className="font-bold">Run Selected</span>. Approve them on the
                  Phrasing Variation page first.
                </p>
              </div>
            )}

            <div className="mt-4 min-h-0 flex-1 overflow-y-auto rounded-xl border border-line">
              <table className="w-full text-sm">
                <tbody>
                  {expansion.groups.map((g) => (
                    <tr key={g.question_id} className="border-b border-slate-100 last:border-0 align-top">
                      <td className="px-4 py-3">
                        <p className="text-sm font-medium leading-snug text-ink">{g.question_text}</p>
                        <p className="mt-1 text-[11px] font-medium text-ink-muted">{g.question_id}</p>
                      </td>
                      <td className="w-52 px-4 py-3 text-right">
                        {g.approved_count > 0 ? (
                          <span className="inline-flex items-center rounded-full bg-teal-100 px-2.5 py-1 text-[11px] font-bold text-teal-800">
                            + {g.approved_count} variation{g.approved_count === 1 ? "" : "s"}
                          </span>
                        ) : (
                          <span className="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-1 text-[11px] font-bold text-ink-light">
                            runs on its own
                          </span>
                        )}
                        {g.pending_count > 0 && (
                          <p className="mt-1.5 text-[11px] font-medium text-amber-700">
                            {g.pending_count} awaiting review — not included
                          </p>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {expansion.missing.length > 0 && (
              <p className="mt-3 text-[11px] font-medium text-ink-light">
                {expansion.missing.length} selected question{expansion.missing.length === 1 ? " is" : "s are"} no
                longer in the repository and will be skipped.
              </p>
            )}

            {runError && (
              <p className="mt-3 text-xs font-bold text-red-600">{runError}</p>
            )}

            <div className="mt-6 flex justify-end gap-2">
              <button
                onClick={closeExpansion}
                className="rounded-xl bg-slate-100 px-4 py-2 text-sm font-bold text-ink-light hover:bg-slate-200 transition-colors"
              >
                Cancel
              </button>
              <button
                disabled={busy || expansion.total === 0}
                onClick={() => runQuestions(expansion.question_ids)}
                className="inline-flex items-center gap-2 rounded-xl bg-brand px-5 py-2 text-sm font-bold text-white hover:bg-brand-dark disabled:opacity-40 transition-colors"
              >
                {busy ? <Spinner size={16} /> : <Play size={16} />}
                {busy ? "Starting\u2026" : `Start run (${expansion.total})`}
              </button>
            </div>
          </div>
        </div>
      )}

      {creating && (
        <CreateQuestionModal
          draft={creating}
          onClose={() => setCreating(null)}
          onCreated={() => { setCreating(null); load(); loadPrioritized(); }}
        />
      )}

      {importOpen && (
        <ImportPromptsModal
          onClose={() => setImportOpen(false)}
          onImported={() => { load(); loadPrioritized(); api.coverage().then(setCoverage).catch(() => {}); }}
        />
      )}
    </div>
  );
}
