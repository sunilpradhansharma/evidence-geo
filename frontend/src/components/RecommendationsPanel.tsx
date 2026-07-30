import React, { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  AlertTriangle,
  Check,
  Copy,
  Download,
  FileText,
  Lightbulb,
  ListPlus,
  MessageSquarePlus,
  Rocket,
  ScanEye,
  Search,
  ShieldAlert,
  Target,
  TrendingUp,
  Wrench,
  X,
} from "lucide-react";
import {
  api,
  type InternalOnlyItem,
  type RecFilters,
  type Recommendation,
  type RecommendationBatch,
  type TaFilters,
} from "../api/client";
import CitationInsights from "./CitationInsights";
import { PlacementGuidancePanel } from "./PlacementGuidance";
import { TaHierarchyFilter, type TaSelection } from "./TaHierarchyFilter";
import { copyText } from "../lib/clipboard";
import { AnimatedCard, Card, EmptyState, InfoTooltip, PositionBadge, Spinner, Stat } from "./ui";

/* Filter option lists (BR-012.6). Models mirror the enabled targets in targets.yaml. */
const PERSONA_OPTS = ["", "Prospect", "Patient", "Provider"] as const;
const MODEL_OPTS = ["", "claude", "nova-pro", "llama", "gpt-4o", "gemini"] as const;
const MODEL_LABELS: Record<string, string> = {
  "": "All models",
  claude: "Claude",
  "nova-pro": "Nova Pro",
  llama: "Llama",
  "gpt-4o": "GPT-4o",
  gemini: "Gemini",
};

const EMPTY_TA: TaSelection = { area: "", indication: "", brand: "", disease: "" };

const CONTENT_TYPE_CLR: Record<string, string> = {
  FAQ: "bg-teal-100 text-teal-800",
  "Clinical Abstract": "bg-violet-100 text-violet-800",
  "Comparison Table": "bg-sky-100 text-sky-800",
  "Dosing & Administration Guide": "bg-amber-100 text-amber-800",
  "Mechanism of Action Explainer": "bg-indigo-100 text-indigo-700",
  "Patient Education Page": "bg-emerald-100 text-emerald-800",
  "HCP Resource Hub": "bg-pink-100 text-pink-700",
};

const SORT_OPTS = ["impact", "volume", "severity"] as const;
type SortKey = (typeof SORT_OPTS)[number];
const SORT_LABELS: Record<SortKey, string> = {
  impact: "Priority (impact)",
  volume: "Search demand",
  severity: "Position severity",
};

/* Plain-language help for the AI positioning labels (mirrors ui.tsx POSITION_TOOLTIPS). */
const POSITION_HELP: Record<string, string> = {
  NOT_RECOMMENDED: "AI is actively steering away from this brand: highest-priority risk signal.",
  SECOND_LINE: "AI positions this brand as a secondary or backup option.",
};

/* Phase 9 — which finder produced the row. The two ask different questions about the same
   answer and their remedies are not interchangeable, so the card says which is which. */
const SOURCE_META: Record<string, { label: string; cls: string; help: string }> = {
  POSITIONING_GAP: {
    label: "Positioning",
    cls: "bg-sky-100 text-sky-800",
    help: "Found by comparing how the AI positioned your brand. This is about how the answer READS.",
  },
  EVIDENCE_GAP: {
    label: "Evidence alignment",
    cls: "bg-violet-100 text-violet-800",
    help: "Found by checking a specific claim against the curated clinical evidence. This is about whether the answer is RIGHT.",
  },
};

/* Plain-language names for the strategic implications, and who owns each. */
const IMPLICATION_META: Record<string, { label: string; cls: string; help: string }> = {
  AI_MISINFORMATION_RISK: {
    label: "Misinformation risk",
    cls: "bg-red-100 text-red-700",
    help: "The model stated something our verified evidence contradicts.",
  },
  COMMUNICATION_GAP: {
    label: "Communication gap",
    cls: "bg-amber-100 text-amber-800",
    help: "Our evidence is more definite than the model's answer — it is probably not reaching the model.",
  },
  MISSING_COMPARATIVE_DATA: {
    label: "Missing comparative data",
    cls: "bg-orange-100 text-orange-800",
    help: "The model asserted a comparison our evidence cannot produce. Saying so plainly is itself the correction.",
  },
  EVIDENCE_GENERATION_NEEDED: {
    label: "Evidence generation needed",
    cls: "bg-indigo-100 text-indigo-700",
    help: "Closing this needs new evidence — a trial or a published synthesis — not new content.",
  },
  INTERNAL_CURATION_REQUIRED: {
    label: "Internal curation",
    cls: "bg-slate-200 text-ink",
    help: "The comparison is blocked by our own verification backlog, not by an absence of evidence. No published content changes this.",
  },
  COMPETITOR_THREAT: {
    label: "Competitor threat",
    cls: "bg-rose-100 text-rose-700",
    help: "A reviewed competitor entered this indication's evidence base.",
  },
  POSITIONING_OPPORTUNITY: {
    label: "Positioning opportunity",
    cls: "bg-teal-100 text-teal-800",
    help: "A weak brand position that authoritative content could improve.",
  },
};

const CLASSIFICATION_LABELS: Record<string, string> = {
  CONTRADICTORY: "Our evidence says the opposite",
  UNSUPPORTED: "Our evidence cannot support this",
  PARTIALLY_ALIGNED: "Partly right",
  OUTDATED: "Based on superseded evidence",
  IMPORTANT_OMISSION: "Left out something material",
};

/* Priority tier derived from impact score (gap severity × search-volume multiplier), FR-616. */
const PRIORITY_META = {
  high: { label: "High priority", cls: "bg-red-100 text-red-700 ring-1 ring-red-200" },
  medium: { label: "Medium", cls: "bg-amber-100 text-amber-800 ring-1 ring-amber-200" },
  low: { label: "Low", cls: "bg-slate-100 text-ink-light ring-1 ring-slate-200" },
} as const;
type PriorityKey = keyof typeof PRIORITY_META;
function priorityOf(impact: number): PriorityKey {
  if (impact >= 8) return "high";
  if (impact >= 5) return "medium";
  return "low";
}

/* Compact number for the summary tiles (27100 -> "27.1K"). */
function fmtCompact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(n >= 10_000 ? 0 : 1)}K`;
  return String(n);
}

/* Triage state is persisted server-side (shared across users + audited, BR-010), not in
   localStorage. The 3 buttons map to the backend REVIEW_STATUSES we surface here. */
const STATUS_OPTS = ["NEW", "REVIEWING", "ACTIONED"] as const;
type StatusKey = (typeof STATUS_OPTS)[number];
const STATUS_META: Record<StatusKey, { label: string; cls: string }> = {
  NEW: { label: "New", cls: "bg-slate-200 text-ink" },
  REVIEWING: { label: "In review", cls: "bg-sky-100 text-sky-900" },
  ACTIONED: { label: "Actioned", cls: "bg-teal-100 text-teal-900" },
};

/* Build a paste-ready brief for a marketer to hand off (includes the MLR caveat). */
function briefText(r: Recommendation): string {
  return [
    `GEO Intervention: ${r.content_type}`,
    `Brand: ${r.brand_focus ?? "n/a"}  |  AI position: ${r.competitive_position}`,
    `Outperformed by: ${r.outperforming_competitor ?? "unknown"}${r.competitor_domain ? ` (${r.competitor_domain})` : ""}`,
    "",
    `Action: ${r.recommended_action}`,
    r.rationale ? `Why: ${r.rationale}` : "",
    "",
    `Monthly search demand: ${(r.search_volume ?? 0).toLocaleString()}  |  Competitor site trust: ${r.domain_authority ?? "N/A"}/100`,
    r.missing_citations.length
      ? `Trusted sources missing your brand:\n${r.missing_citations.map((c) => `  - ${c}`).join("\n")}`
      : "",
    r.content_brief.length
      ? `Suggested outline:\n${r.content_brief.map((b) => `  - ${b}`).join("\n")}`
      : "",
    r.suggested_questions.length
      ? `Questions to monitor (require MA approval):\n${r.suggested_questions.map((q) => `  - ${q}`).join("\n")}`
      : "",
    "",
    "NOTE: Strategic suggestion only: not MLR-approved. Requires Medical, Legal & Regulatory review before use.",
  ]
    .filter((l) => l !== "")
    .join("\n");
}

const SELECT_CLS =
  "appearance-none border border-line rounded-lg pl-3 pr-8 py-2 text-sm font-medium text-ink bg-canvas-card " +
  "shadow-sm focus:outline-none focus:ring-2 focus:ring-brand-light/40 focus:border-brand-light transition-colors cursor-pointer";

function LabelledSelect({
  label, value, options, labels, onChange,
}: {
  label: string;
  value: string;
  options: readonly string[];
  labels?: Record<string, string>;
  onChange: (v: string) => void;
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <label className="text-xs font-bold uppercase tracking-widest text-ink pl-0.5">{label}</label>
      <select value={value} onChange={(e) => onChange(e.target.value)} className={SELECT_CLS}>
        {options.map((o) => (
          <option key={o} value={o}>{labels?.[o] ?? (o || "All")}</option>
        ))}
      </select>
    </div>
  );
}

function MetricPill({ label, value, tip }: { label: string; value: React.ReactNode; tip?: string }) {
  return (
    <span
      title={tip}
      className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-slate-100 px-2.5 py-1 text-sm font-semibold text-ink-light"
    >
      <span className="text-ink-light">{label}</span>
      <span className="font-extrabold text-ink">{value}</span>
    </span>
  );
}

/* One recommendation card: priority-first, plain-language evidence, workflow actions. */
function RecCard({
  r, rank, status, onStatus, onCopy, copied, onCreateIntervention, creating,
}: {
  r: Recommendation;
  rank: number;
  status: StatusKey;
  onStatus: (s: StatusKey) => void;
  onCopy: () => void;
  copied: boolean;
  onCreateIntervention: () => void;
  creating: boolean;
}) {
  const isProvider = r.persona === "Provider";
  const pri = PRIORITY_META[priorityOf(r.impact_score)];
  const isLive = r.metrics_source === "live";
  const isEvidence = r.source_type === "EVIDENCE_GAP";
  const source = SOURCE_META[r.source_type];
  const implication = r.strategic_implication ? IMPLICATION_META[r.strategic_implication] : null;
  return (
    <div
      className={`rounded-2xl border border-line bg-canvas-card shadow-sm p-4 sm:p-5 transition-colors hover:border-brand-light/40 ${status === "ACTIONED" ? "border-teal-200" : ""}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2 flex-wrap">
          <span
            className="inline-flex items-center justify-center h-6 min-w-6 px-1.5 rounded-full bg-brand-dark text-white text-xs font-bold"
            title={`Rank #${rank} by impact`}
          >
            {rank}
          </span>
          <span className={`px-2.5 py-1 rounded-full text-xs font-bold ${CONTENT_TYPE_CLR[r.content_type] || "bg-slate-100 text-ink-light"}`}>
            {r.content_type}
          </span>
          {/* Phase 9: which finder found this, and what it means strategically. Shown
              before the position badge because on an evidence row the position is often
              NOT_ASSESSED and would otherwise read as the headline. */}
          {source && (
            <span className="inline-flex items-center gap-1">
              <span className={`px-2.5 py-1 rounded-full text-xs font-bold ${source.cls}`}>
                {source.label}
              </span>
              <InfoTooltip content={source.help} />
            </span>
          )}
          {implication && (
            <span className="inline-flex items-center gap-1">
              <span className={`px-2.5 py-1 rounded-full text-xs font-bold ${implication.cls}`}>
                {implication.label}
              </span>
              <InfoTooltip content={implication.help} />
            </span>
          )}
          {r.competitive_position !== "NOT_ASSESSED" && (
            <span className="inline-flex items-center gap-1">
              <PositionBadge position={r.competitive_position} />
              {POSITION_HELP[r.competitive_position] && <InfoTooltip content={POSITION_HELP[r.competitive_position]} />}
            </span>
          )}
        </div>
        <span
          className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-bold shrink-0 ${pri.cls}`}
          title={`Impact score ${r.impact_score.toFixed(1)} = gap severity × search-volume multiplier`}
        >
          <TrendingUp size={13} /> {pri.label}
        </span>
      </div>

      <p className="mt-3 text-base font-extrabold leading-snug text-ink">{r.recommended_action}</p>
      {r.rationale && <p className="mt-1.5 text-sm font-medium leading-relaxed text-ink-light">{r.rationale}</p>}

      {/* Phase 9 — an evidence-alignment row instead. The SEMrush block below is about
          search demand for a positioning gap and says nothing useful about a claim that
          contradicts a label, so the two are shown separately rather than merged. */}
      {isEvidence && (
        <div className="mt-3 space-y-2.5 rounded-xl border border-violet-200 bg-violet-50/50 p-3.5">
          {r.claim_text && (
            <div>
              <p className="mb-1 text-xs font-extrabold uppercase tracking-wide text-ink">
                What the AI said
              </p>
              <p className="text-sm font-semibold italic leading-relaxed text-ink">
                &ldquo;{r.claim_text}&rdquo;
              </p>
            </div>
          )}
          {r.finding_reason && (
            <div>
              <p className="mb-1 inline-flex items-center gap-1 text-xs font-extrabold uppercase tracking-wide text-ink">
                {r.classification ? CLASSIFICATION_LABELS[r.classification] ?? "What our evidence shows" : "What our evidence shows"}
                <InfoTooltip content="Produced by checking this claim against the evidence family that can answer it — a label claim against the label, a comparison against head-to-head evidence. The verdict is computed, not written by a model." />
              </p>
              <p className="text-sm font-medium leading-relaxed text-ink-light">
                {r.finding_reason}
              </p>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2 pt-0.5">
            {r.confidence !== null && (
              <MetricPill
                label="Confidence"
                value={`${Math.round(r.confidence * 100)}%`}
                tip="Derived from the review state of the evidence behind this finding — verified studies and a ratified network score higher than an unreviewed extraction. Not a model's self-report."
              />
            )}
            {r.certainty_verdict && (
              <MetricPill
                label="Certainty"
                value={r.certainty_verdict.toLowerCase()}
                tip="How the model's hedging compared with the statistical uncertainty in our evidence."
              />
            )}
            {r.gap_attribution && (
              <MetricPill
                label="Gap is"
                value={r.gap_attribution.toLowerCase()}
                tip="CURATION means the comparison is blocked by our own verification backlog. PROTOCOL means the approved analysis window excludes evidence that exists. EVIDENCE means there genuinely is none."
              />
            )}
          </div>
        </div>
      )}

      {/* Supporting evidence (BR-012.5), plain language (FR-621/622) */}
      {!isEvidence && (
      <div className="mt-3 space-y-2.5 rounded-xl border border-line bg-slate-50 p-3.5">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="inline-flex items-center gap-1.5 font-bold text-ink">
            <Target size={15} className="text-red-500" /> Outperformed by
          </span>
          <span className="font-bold text-ink">{r.outperforming_competitor || "Unknown competitor"}</span>
          {r.competitor_domain && <span className="text-ink-light">· {r.competitor_domain}</span>}
        </div>
        {r.missing_citations.length > 0 && (
          <div>
            <p className="mb-1 inline-flex items-center gap-1 text-xs font-extrabold uppercase tracking-wide text-ink">
              Trusted sources missing your brand ({r.missing_citations.length})
              <InfoTooltip content="Websites the AI relied on for its answer that did not mention your brand. Getting published or cited on these raises your visibility in AI answers." />
            </p>
            <ul className="space-y-0.5">
              {r.missing_citations.slice(0, 5).map((c, j) => (
                <li key={j} className="truncate text-sm font-medium text-ink-light">• {c}</li>
              ))}
            </ul>
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2 pt-0.5">
          <MetricPill
            label="Monthly searches"
            value={(r.search_volume ?? 0).toLocaleString()}
            tip="How many people ask this kind of question on Google each month (SEMrush search volume)."
          />
          <MetricPill
            label="Competitor site trust"
            value={`${r.domain_authority ?? "N/A"}/100`}
            tip="How much search engines trust the competitor's site (SEMrush Authority Score, 0–100)."
          />
          <span
            className={`inline-flex items-center rounded-lg px-2.5 py-1 text-xs font-extrabold uppercase tracking-wide ${isLive ? "bg-teal-100 text-teal-900" : "bg-slate-200 text-ink"}`}
            title={isLive ? "Live SEMrush metrics" : "Estimated metrics (no SEMrush key configured)"}
          >
            {isLive ? "Live data" : "Estimated"}
          </span>
        </div>
      </div>
      )}

      {/* Where to publish / earn a citation — the "where" that complements the "what" */}
      <PlacementGuidancePanel placement={r.placement} className="mt-3" />

      {/* Content brief + suggested monitoring questions (D/E) — still MLR-unapproved */}
      {(r.content_brief.length > 0 || r.suggested_questions.length > 0) && (
        <details className="mt-4">
          <summary className="flex cursor-pointer select-none items-center gap-2 text-sm font-extrabold text-ink transition-colors hover:text-brand-dark">
            <FileText size={16} className="text-brand-dark" />
            Content brief &amp; questions to monitor
          </summary>
          <div className="mt-3 space-y-4 rounded-xl border border-line bg-canvas-card p-4">
            {r.content_brief.length > 0 && (
              <div>
                <p className="mb-1.5 text-xs font-extrabold uppercase tracking-wide text-ink">Suggested outline</p>
                <ul className="list-disc space-y-1 pl-5">
                  {r.content_brief.map((b, i) => (
                    <li key={i} className="text-sm leading-relaxed text-ink">{b}</li>
                  ))}
                </ul>
              </div>
            )}
            {r.suggested_questions.length > 0 && (
              <div>
                <p className="mb-1.5 inline-flex items-center gap-1 text-xs font-extrabold uppercase tracking-wide text-ink">
                  Questions worth monitoring
                  <InfoTooltip content="Generic questions to consider adding to monitoring. Adding to the Question bank still requires Medical-Affairs approval." />
                </p>
                <ul className="space-y-0.5">
                  {r.suggested_questions.map((q, i) => (
                    <li key={i} className="flex items-start gap-2 text-sm leading-relaxed text-ink">
                      <MessageSquarePlus size={15} className="mt-0.5 shrink-0 text-brand-dark" />
                      {q}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </details>
      )}

      {/* Footer: context + workflow actions */}
      <div className="mt-4 flex flex-wrap items-center justify-between gap-4 border-t border-line pt-3">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm font-semibold text-ink">
          {r.brand_focus && <span>Brand: <span className="font-bold text-ink">{r.brand_focus}</span></span>}
          {r.therapeutic_area && <span>· TA: <span className="font-bold text-ink">{r.therapeutic_area}</span></span>}
          {r.persona && <span>· Persona: <span className="font-bold text-ink">{r.persona}</span></span>}
          {r.llm_name && <span>· Model: <span className="font-bold text-ink">{MODEL_LABELS[r.llm_name] ?? r.llm_name}</span></span>}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <div className="inline-flex overflow-hidden rounded-lg border border-line shadow-sm">
            {STATUS_OPTS.map((s) => (
              <button
                key={s}
                onClick={() => onStatus(s)}
                className={`px-3 py-1.5 text-sm font-bold transition-colors ${status === s ? STATUS_META[s].cls : "bg-canvas-card text-ink hover:bg-slate-50"}`}
                title={`Mark as ${STATUS_META[s].label}`}
              >
                {STATUS_META[s].label}
              </button>
            ))}
          </div>
          <button
            onClick={onCopy}
            className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-canvas-card px-3 py-1.5 text-sm font-bold text-ink shadow-sm transition-colors hover:border-brand-light/50 hover:bg-slate-50"
            title="Copy a paste-ready brief (includes the MLR caveat)"
          >
            {copied ? (
              <><Check size={15} className="text-teal-700" /> Copied</>
            ) : (
              <><Copy size={15} /> Copy brief</>
            )}
          </button>
          <button
            onClick={onCreateIntervention}
            disabled={creating || isProvider}
            className="inline-flex items-center gap-1.5 rounded-lg bg-brand-dark px-3 py-1.5 text-sm font-bold text-white shadow-sm transition-colors hover:bg-brand disabled:opacity-50"
            title={isProvider
              ? "Provider cohorts aren't auto-measurable in v1 (they await manual OpenEvidence capture)"
              : "Create an owned, measurable intervention from this recommendation"}
          >
            {creating ? <><Spinner size={14} /> Creating…</> : <><Rocket size={15} /> Create intervention</>}
          </button>
          {/* Note: rows whose finding has no content remedy never reach this card — the
              engine does not generate a recommendation for them at all. They surface in the
              "Not content work" panel above instead. */}
        </div>
      </div>
    </div>
  );
}

export default function RecommendationsPanel() {
  const navigate = useNavigate();
  const [creatingId, setCreatingId] = useState<string | null>(null);
  const [taSelection, setTaSelection] = useState<TaSelection>(EMPTY_TA);
  const [taFilters, setTaFilters] = useState<TaFilters>({});

  async function createIntervention(r: Recommendation) {
    setCreatingId(r.rec_id);
    try {
      const res = await api.createInterventionFromRec(r.rec_id, {});
      navigate(`/dashboard/activation-impact?id=${encodeURIComponent(res.id)}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not create intervention");
    } finally {
      setCreatingId(null);
    }
  }
  const [persona, setPersona] = useState("");
  const [model, setModel] = useState("");
  const [batch, setBatch] = useState<RecommendationBatch | null>(null);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [semrushConfigured, setSemrushConfigured] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastRun, setLastRun] = useState<string | null>(null);
  const [contentType, setContentType] = useState("");
  const [sort, setSort] = useState<SortKey>("impact");
  const [groupByBrand, setGroupByBrand] = useState(false);
  const [statuses, setStatuses] = useState<Record<string, StatusKey>>({});
  const [copiedId, setCopiedId] = useState<string | null>(null);
  /* Phase 9 — findings the engine deliberately did NOT turn into content actions. Held in
     state from the last generate rather than fetched, because "what we refused to
     recommend, and why" is a property of that run. */
  const [internalOnly, setInternalOnly] = useState<InternalOnlyItem[]>([]);

  function currentFilters(): RecFilters {
    return {
      persona: persona || undefined,
      therapeutic_area: taFilters.therapeutic_area,
      indication: taFilters.indication,
      brand: taFilters.brand,
      llm_name: model || undefined,
    };
  }

  function load() {
    setLoading(true);
    api
      .recommendations(currentFilters())
      .then((b) => {
        setBatch(b);
        // Pull persisted triage state for this batch (shared across users, BR-010).
        api
          .recommendationReviews(b?.batch_id ?? undefined)
          .then((r) => {
            const map: Record<string, StatusKey> = {};
            for (const it of r.items) map[it.rec_id] = it.status as StatusKey;
            setStatuses(map);
          })
          .catch(() => setStatuses({}));
      })
      .catch(() => setBatch(null))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    api.recommendationContentTypes().then((r) => setSemrushConfigured(r.semrush_configured)).catch(() => {});
  }, []);

  // Reload whenever a filter changes (BR-012.6).
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [persona, model, taFilters]);

  function handleTaChange(next: TaSelection, filters: TaFilters) {
    setTaSelection(next);
    setTaFilters(filters);
  }

  function setStatus(id: string, s: StatusKey) {
    setStatuses((prev) => ({ ...prev, [id]: s }));  // optimistic
    api.setRecommendationReview(id, { status: s }).catch(() => {
      /* best-effort; the optimistic update stays and will reconcile on next load */
    });
  }

  async function copyBrief(r: Recommendation) {
    const ok = await copyText(briefText(r));
    if (ok) {
      setCopiedId(r.rec_id);
      window.setTimeout(() => setCopiedId((c) => (c === r.rec_id ? null : c)), 1600);
    }
  }

  async function generate() {
    setGenerating(true);
    setError(null);
    try {
      const res = await api.generateRecommendations({ ...currentFilters(), limit: 25 });
      const evidenceCount = res.evidence_gaps_found ?? 0;
      setLastRun(
        `Generated ${res.generated} recommendation${res.generated !== 1 ? "s" : ""} from ` +
          `${res.gaps_found} positioning gap${res.gaps_found !== 1 ? "s" : ""}` +
          (evidenceCount ? ` and ${evidenceCount} evidence gap${evidenceCount !== 1 ? "s" : ""}` : "") +
          ` · SEMrush: ${res.semrush_source}`,
      );
      setInternalOnly(res.internal_only ?? []);
      load();
    } catch {
      setError(
        "Generation failed. Ensure AWS Bedrock credentials are set and that scored responses " +
          "with Second-line / Not-recommended positions exist.",
      );
    } finally {
      setGenerating(false);
    }
  }

  const items = batch?.items ?? [];
  const csvUrl = api.recommendationsCsvUrl(currentFilters());

  // Content-type options reflect what's actually present in the batch (BR-012.6).
  const contentTypeOpts = useMemo(
    () => ["", ...Array.from(new Set(items.map((r) => r.content_type))).sort()],
    [items],
  );

  // Global impact rank stays stable regardless of sort/grouping (BR-012.3).
  const rankById = useMemo(() => {
    const m: Record<string, number> = {};
    items
      .slice()
      .sort((a, b) => b.impact_score - a.impact_score)
      .forEach((r, i) => {
        m[r.rec_id] = i + 1;
      });
    return m;
  }, [items]);

  // Client-side content-type filter + sort (persona/model/TA are server-side).
  const visible = useMemo(() => {
    const sev = (r: Recommendation) => (r.competitive_position === "NOT_RECOMMENDED" ? 1 : 0);
    return (contentType ? items.filter((r) => r.content_type === contentType) : items.slice()).sort(
      (a, b) => {
        if (sort === "volume") return (b.search_volume ?? 0) - (a.search_volume ?? 0);
        if (sort === "severity") return sev(b) - sev(a) || b.impact_score - a.impact_score;
        return b.impact_score - a.impact_score;
      },
    );
  }, [items, contentType, sort]);

  // Headline + tile metrics across the whole batch (FR-611/612).
  const summary = useMemo(() => {
    const brands = new Set<string>();
    let high = 0;
    let volume = 0;
    for (const r of items) {
      if (r.brand_focus) brands.add(r.brand_focus);
      if (priorityOf(r.impact_score) === "high") high += 1;
      volume += r.search_volume ?? 0;
    }
    const top = items.slice().sort((a, b) => b.impact_score - a.impact_score)[0] ?? null;
    return { count: items.length, brands: brands.size, high, volume, top };
  }, [items]);

  // Group the visible list by brand when requested (ranked flat list is the default).
  const groups = useMemo(() => {
    if (!groupByBrand) return [{ brand: "", items: visible }];
    const map = new Map<string, Recommendation[]>();
    for (const r of visible) {
      const b = r.brand_focus || "Unspecified brand";
      if (!map.has(b)) map.set(b, []);
      map.get(b)!.push(r);
    }
    return Array.from(map.entries())
      .map(([brand, list]) => ({ brand, items: list }))
      .sort((a, b) => (b.items[0]?.impact_score ?? 0) - (a.items[0]?.impact_score ?? 0));
  }, [visible, groupByBrand]);

  const activeChips: { key: string; label: string; clear: () => void }[] = [];
  const taLabel = [taFilters.therapeutic_area, taFilters.indication, taFilters.brand]
    .filter(Boolean)
    .join(" › ");
  if (taLabel)
    activeChips.push({ key: "ta", label: taLabel, clear: () => { setTaSelection(EMPTY_TA); setTaFilters({}); } });
  if (persona) activeChips.push({ key: "persona", label: persona, clear: () => setPersona("") });
  if (model) activeChips.push({ key: "model", label: MODEL_LABELS[model] ?? model, clear: () => setModel("") });
  if (contentType) activeChips.push({ key: "ct", label: contentType, clear: () => setContentType("") });

  function clearAll() {
    setTaSelection(EMPTY_TA);
    setTaFilters({});
    setPersona("");
    setModel("");
    setContentType("");
  }

  return (
    <section className="space-y-4">
      {/* Header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 className="flex items-center gap-2 text-lg font-extrabold text-ink">
            <Lightbulb size={18} className="text-brand-light" />
            GEO Intervention Recommendations
            <InfoTooltip content="Plain-language content actions that would improve how AI platforms represent your brand where it is currently under-represented. Ranked by estimated impact (gap severity × search volume)." />
          </h2>
          <p className="text-sm text-ink-light font-medium mt-0.5">
            What to publish to move weak brand positions up: enriched with SEMrush SEO metrics.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <a
            href={csvUrl}
            className={`inline-flex items-center gap-2 px-3.5 py-2 rounded-xl border border-line bg-canvas-card text-sm font-bold text-ink hover:border-brand-light/50 transition-colors ${items.length ? "" : "pointer-events-none opacity-50"}`}
          >
            <Download size={15} /> Export CSV
          </a>
          <button
            onClick={generate}
            disabled={generating}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-brand-dark text-white text-sm font-bold hover:bg-brand transition-colors disabled:opacity-60 shadow-sm"
          >
            {generating ? <Spinner size={16} /> : <ListPlus size={16} />}
            {generating ? "Generating…" : "Generate"}
          </button>
        </div>
      </div>

      {/* Phase 9 — findings the engine refused to answer with content. Shown ABOVE the
          recommendation list, because the whole point is that this work would otherwise be
          invisible: a shorter list looks like less to do rather than like work that belongs
          to somebody else. */}
      {internalOnly.length > 0 && (
        <div className="rounded-2xl border-2 border-slate-300 bg-slate-50 p-4">
          <div className="flex items-start gap-2.5">
            <Wrench size={18} className="mt-0.5 shrink-0 text-ink-light" />
            <div className="min-w-0 flex-1">
              <p className="text-sm font-extrabold text-ink">
                {internalOnly.length} finding{internalOnly.length !== 1 ? "s" : ""} that content
                cannot fix
              </p>
              <p className="mt-0.5 text-sm font-medium leading-relaxed text-ink-light">
                These are real, and no publishable asset closes them — so no recommendation was
                generated. They belong to a curator, a statistician or clinical development.
              </p>
              <ul className="mt-3 space-y-2.5">
                {internalOnly.map((it, i) => {
                  const meta = it.strategic_implication
                    ? IMPLICATION_META[it.strategic_implication]
                    : null;
                  return (
                    <li
                      key={it.claim_id ?? i}
                      className="rounded-xl border border-line bg-canvas-card p-3"
                    >
                      <div className="mb-1 flex flex-wrap items-center gap-2">
                        {meta && (
                          <span className={`rounded-full px-2.5 py-1 text-xs font-bold ${meta.cls}`}>
                            {meta.label}
                          </span>
                        )}
                        {it.owner && (
                          <span className="text-xs font-bold uppercase tracking-wide text-ink-light">
                            {it.owner}
                          </span>
                        )}
                      </div>
                      {it.claim_text && (
                        <p className="text-sm font-semibold italic leading-snug text-ink">
                          &ldquo;{it.claim_text}&rdquo;
                        </p>
                      )}
                      {it.reason && (
                        <p className="mt-1 text-sm leading-relaxed text-ink-light">{it.reason}</p>
                      )}
                      {it.evidence_action && (
                        <p className="mt-1.5 text-sm font-bold text-ink">
                          Next step: {it.evidence_action}
                        </p>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          </div>
        </div>
      )}

      {/* MLR warning banner — hardcoded and NOT dismissible (BR-012.4) */}
      <div
        role="alert"
        className="flex items-start gap-2.5 rounded-xl border-2 border-amber-300 bg-amber-50 px-4 py-3"
      >
        <ShieldAlert size={18} className="text-amber-600 shrink-0 mt-0.5" />
        <p className="text-sm font-semibold text-amber-900">
          These are strategic suggestions, not MLR-approved content. Every recommendation requires
          Medical, Legal &amp; Regulatory review before any use or publication.
        </p>
      </div>

      {/* Big-picture headline + summary tiles (FR-611/612) */}
      {items.length > 0 && (
        <>
          {(() => {
            const s = summary;
            const parts: string[] = [`**${s.count}** content action${s.count !== 1 ? "s" : ""}`];
            if (s.brands) parts.push(`across **${s.brands}** brand${s.brands !== 1 ? "s" : ""}`);
            let text = parts.join(" ") + (s.high ? `, **${s.high}** high-priority.` : ".");
            if (s.top?.brand_focus && s.top?.content_type)
              text += ` Biggest opportunity: a **${s.top.content_type}** for **${s.top.brand_focus}**.`;
            return (
              <div className="rounded-2xl border-2 border-brand/30 bg-brand-surface/50 p-5">
                <div className="flex items-start gap-3">
                  <ScanEye size={18} className="text-brand-light shrink-0 mt-0.5" />
                  <div>
                    <p className="text-[11px] font-bold uppercase tracking-widest text-ink-light mb-1">The big picture</p>
                    <p className="text-sm font-semibold text-ink leading-snug">
                      {text.split("**").map((seg, i) =>
                        i % 2 === 1 ? (
                          <span key={i} className="text-brand-dark font-extrabold">{seg}</span>
                        ) : (
                          <span key={i}>{seg}</span>
                        ),
                      )}
                    </p>
                  </div>
                </div>
              </div>
            );
          })()}

          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <AnimatedCard delay={0}>
              <Stat label="Content actions" value={summary.count} icon={<Lightbulb size={16} />} />
            </AnimatedCard>
            <AnimatedCard delay={0.05}>
              <Stat label="High priority" value={summary.high} sub="need attention first" icon={<TrendingUp size={16} />} />
            </AnimatedCard>
            <AnimatedCard delay={0.1}>
              <Stat label="Brands affected" value={summary.brands} icon={<Target size={16} />} />
            </AnimatedCard>
            <AnimatedCard delay={0.15}>
              <Stat label="Search demand" value={fmtCompact(summary.volume)} sub="monthly searches addressed" icon={<Search size={16} />} />
            </AnimatedCard>
          </div>
        </>
      )}

      {/* Filters (BR-012.6) */}
      <div className="flex items-end gap-3 flex-wrap">
        <TaHierarchyFilter value={taSelection} onChange={handleTaChange} />
        <LabelledSelect label="Persona" value={persona} options={PERSONA_OPTS} onChange={setPersona} />
        <LabelledSelect label="Model" value={model} options={MODEL_OPTS} labels={MODEL_LABELS} onChange={setModel} />
        <LabelledSelect label="Content type" value={contentType} options={contentTypeOpts} onChange={setContentType} />
        <LabelledSelect
          label="Sort by"
          value={sort}
          options={SORT_OPTS}
          labels={SORT_LABELS}
          onChange={(v) => setSort(v as SortKey)}
        />
        <div className="flex flex-col gap-0.5">
          <label className="text-xs font-bold uppercase tracking-widest text-ink pl-0.5">View</label>
          <div className="inline-flex h-[38px] rounded-lg border border-line overflow-hidden bg-canvas-card">
            <button
              onClick={() => setGroupByBrand(false)}
              className={`px-3 text-sm font-bold transition-colors ${!groupByBrand ? "bg-brand-dark text-white" : "text-ink-muted hover:text-ink"}`}
            >
              Ranked
            </button>
            <button
              onClick={() => setGroupByBrand(true)}
              className={`px-3 text-sm font-bold transition-colors ${groupByBrand ? "bg-brand-dark text-white" : "text-ink-muted hover:text-ink"}`}
            >
              By brand
            </button>
          </div>
        </div>
      </div>

      {/* Active filters + result count (BR-012.6) */}
      {items.length > 0 && (
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs font-semibold text-ink-muted">
            Showing {visible.length} of {items.length}
          </span>
          {activeChips.map((chip) => (
            <button
              key={chip.key}
              onClick={chip.clear}
              className="inline-flex items-center gap-1 rounded-full bg-slate-100 hover:bg-slate-200 px-2.5 py-1 text-[11px] font-semibold text-ink transition-colors"
            >
              {chip.label}
              <X size={12} className="text-ink-muted" />
            </button>
          ))}
          {activeChips.length > 0 && (
            <button onClick={clearAll} className="text-[11px] font-bold text-brand-dark hover:underline">
              Clear all
            </button>
          )}
        </div>
      )}

      {lastRun && !generating && (
        <p className="text-xs text-ink-muted font-medium">{lastRun}</p>
      )}
      {semrushConfigured === false && (
        <p className="text-xs text-amber-700 font-medium">
          SEMrush key not set: SEO metrics are simulated (labelled &ldquo;stub&rdquo;). Add
          SEMRUSH_API_KEY for live data.
        </p>
      )}
      {error && (
        <div className="flex items-center gap-2 p-3 rounded-xl bg-red-50 border border-red-100 text-sm text-red-700 font-medium">
          <AlertTriangle size={16} /> {error}
        </div>
      )}

      {/* Ranked list (BR-012.3) */}
      {loading ? (
        <div className="flex justify-center py-12"><Spinner size={26} /></div>
      ) : items.length === 0 ? (
        <Card>
          <EmptyState
            icon={<Lightbulb size={34} />}
            message={
              'No recommendations yet. Click "Generate" to turn Second-line / Not-recommended ' +
              "gaps into ranked content actions."
            }
          />
        </Card>
      ) : visible.length === 0 ? (
        <Card>
          <EmptyState icon={<Lightbulb size={34} />} message="No recommendations match the current filters." />
        </Card>
      ) : (
        <div className="space-y-6">
          {groups.map((g) => (
            <div key={g.brand || "all"}>
              {groupByBrand && (
                <h3 className="flex items-center gap-2 mb-3 text-xs font-bold uppercase tracking-widest text-ink-light">
                  {g.brand}
                  <span className="inline-flex items-center justify-center h-5 min-w-5 px-1.5 rounded-full bg-slate-200 text-ink-light text-[11px] font-bold">
                    {g.items.length}
                  </span>
                </h3>
              )}
              <div className="space-y-3">
                {g.items.map((r, i) => (
                  <AnimatedCard key={r.rec_id} delay={Math.min(i * 0.03, 0.3)}>
                    <RecCard
                      r={r}
                      rank={rankById[r.rec_id]}
                      status={statuses[r.rec_id] ?? "NEW"}
                      onStatus={(s) => setStatus(r.rec_id, s)}
                      onCopy={() => copyBrief(r)}
                      copied={copiedId === r.rec_id}
                      onCreateIntervention={() => createIntervention(r)}
                      creating={creatingId === r.rec_id}
                    />
                  </AnimatedCard>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Citation Gap Analysis (A + C) — BR-005 "where credible sources are missing" */}
      <CitationInsights filters={currentFilters()} />
    </section>
  );
}
