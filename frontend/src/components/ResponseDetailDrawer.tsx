import { type ReactNode, useEffect, useState } from "react";
import { motion } from "framer-motion";
import { X, AlertTriangle, Shield, ExternalLink, GitBranch } from "lucide-react";
import { api, PRELAUNCH_LABEL } from "../api/client";
import {
  InfoTooltip,
  Markdown,
  SentimentBadge,
  PositionBadge,
  IntentBadge,
  ConsensusBadge,
  ThemeBadge,
} from "./ui";

export function buildDetailTooltip(detail: any): string {
  const lines: string[] = [];
  const s = detail.sentiment_score;
  if (s != null) {
    const desc =
      s > 0 ? "\u2022 Positive (> 0): AI speaks favorably about this brand"
      : s < 0 ? "\u2022 Negative (< 0): AI discourages or downplays this brand"
      : "\u2022 Neutral (\u22480): balanced or neutral coverage";
    lines.push("Sentiment: \u22121.0 to +1.0", desc);
  } else {
    lines.push("Sentiment: unscored", "\u2022 Score pending: scoring runs asynchronously after the LLM responds and fills in automatically within a few minutes.");
  }
  const posMap: Record<string, string> = {
    FIRST_LINE_RECOMMENDED: "\u2022 Leading: first-choice recommendation",
    AMONG_OPTIONS:          "\u2022 Present: mentioned among options",
    SECOND_LINE:            "\u2022 Backup: secondary option",
    NOT_RECOMMENDED:        "\u2022 Not Endorsed: actively avoided",
    NOT_MENTIONED:          "\u2022 Absent: not mentioned",
  };
  if (detail.competitive_position && posMap[detail.competitive_position]) {
    lines.push("", "Position: brand\u2019s rank in treatment options", posMap[detail.competitive_position]);
  }
  const intentMap: Record<string, string> = {
    CLINICAL:     "\u2022 Clinical: treatment, dosing, outcomes",
    EXPERIENTIAL: "\u2022 Experiential: side effects, quality of life",
    SCREENING:    "\u2022 Screening: eligibility, diagnosis",
    SHORTHAND:    "\u2022 Shorthand: quick brand lookup",
  };
  const intentKey = (detail.intent_type || "").toUpperCase();
  if (intentKey && intentMap[intentKey]) {
    lines.push("", "Question Type:", intentMap[intentKey]);
  }
  const consensusMap: Record<string, string> = {
    FULL:    "\u2022 Full: all 5 platforms agreed",
    PARTIAL: "\u2022 Partial: some platforms diverged",
    MISSING: "\u2022 Missing: significant disagreement",
  };
  if (detail.consensus_level && consensusMap[detail.consensus_level]) {
    lines.push("", "Models in Agreement:", consensusMap[detail.consensus_level]);
  }
  const personaMap: Record<string, string> = {
    Prospect: "\u2022 Prospect: exploring treatment options",
    Patient:  "\u2022 Patient: currently on treatment",
    Provider: "\u2022 Provider: a clinician",
  };
  if (detail.persona && personaMap[detail.persona]) {
    lines.push("", "Persona: who is asking the question", personaMap[detail.persona]);
  }
  const domainMap: Record<string, string> = {
    Efficacy:    "\u2022 Efficacy: treatment outcomes & dosing",
    Safety:      "\u2022 Safety: side effects & risks",
    Access:      "\u2022 Access: coverage & cost",
    Comparative: "\u2022 Comparative: vs. competitors",
    General:     "\u2022 General: broad or exploratory",
  };
  if (detail.domain && domainMap[detail.domain]) {
    lines.push("", "Theme: the focus of the question", domainMap[detail.domain]);
  }
  return lines.join("\n");
}

function Section({ title, children }: { title: ReactNode; children: ReactNode }) {
  return <div className="mb-6"><h3 className="flex items-center gap-1.5 text-xs font-bold text-ink uppercase tracking-widest mb-3">{title}</h3><div className="text-sm text-ink">{children}</div></div>;
}

/**
 * Verified brand ground truth (GEO): the curated + FDA-label-seeded schema the Chairman
 * uses as a fallback when AI platforms disagree. Lazily fetches GET /geo/schema/{brand}
 * and renders nothing when the brand has no verified record (offline-safe).
 */
function GeoGroundTruth({ brand }: { brand?: string | null }) {
  const [schema, setSchema] = useState<any>(null);
  useEffect(() => {
    if (!brand) { setSchema(null); return; }
    let alive = true;
    api.geoSchema(brand).then((s) => { if (alive) setSchema(s); }).catch(() => { if (alive) setSchema(null); });
    return () => { alive = false; };
  }, [brand]);

  if (!schema) return null;
  const prov = schema.provenance || {};
  const verified = prov.clinicalValuesVerified;
  const ref = schema.labelReference || {};
  const sections = ([
    ["Boxed Warning", ref.boxedWarning],
    ["Indications & Usage", ref.indicationsAndUsage],
    ["Adverse Reactions", ref.adverseReactions],
    ["Dosage & Administration", ref.dosageAndAdministration],
  ] as [string, string | undefined][]).filter((s) => Boolean(s[1])) as [string, string][];

  return (
    <Section title={<span className="flex items-center gap-1.5"><InfoTooltip content="Verified ground-truth data for this brand: curated by Medical Affairs and seeded from the official FDA drug label (openFDA/DailyMed). This is the reference the Chairman falls back to when AI platforms disagree." /> Verified Brand Data (GEO)</span>}>
      <div className="rounded-xl border border-teal-200 bg-teal-50/50 p-4">
        <div className="flex items-center gap-2 mb-3 flex-wrap">
          <Shield size={14} className="text-teal-700 flex-shrink-0" />
          <span className="text-sm font-bold text-ink">{schema.name}{schema.nonProprietaryName ? ` (${schema.nonProprietaryName})` : ""}</span>
          {verified === true && <span className="px-2 py-0.5 rounded-md bg-teal-100 text-teal-800 text-[10px] font-bold uppercase tracking-wide">MA-verified</span>}
          {verified === false && <span className="px-2 py-0.5 rounded-md bg-amber-100 text-amber-800 text-[10px] font-bold uppercase tracking-wide">Placeholder clinical values (pending MA verification)</span>}
        </div>
        <div className="flex flex-wrap gap-1.5 mb-3">
          {prov.labelSource && <span className="px-2 py-0.5 rounded-md bg-white text-teal-800 text-[11px] font-semibold border border-teal-200">{prov.labelSource}</span>}
          {prov.labelEffectiveTime && <span className="px-2 py-0.5 rounded-md bg-white text-teal-800 text-[11px] font-semibold border border-teal-200">Label effective {prov.labelEffectiveTime}</span>}
          {schema.prescribingInformation && (
            <a href={schema.prescribingInformation} target="_blank" rel="noopener noreferrer" className="px-2 py-0.5 rounded-md bg-white text-brand-dark text-[11px] font-semibold border border-teal-200 inline-flex items-center gap-1 hover:underline"><ExternalLink size={11} /> Prescribing Information</a>
          )}
        </div>
        {schema.dataSource && <p className="text-[11px] text-ink-muted mb-3 leading-relaxed">{schema.dataSource}</p>}
        {sections.length > 0 && (
          <div className="space-y-1.5">
            <p className="text-[11px] font-bold text-ink-light uppercase tracking-wide">FDA Label Reference</p>
            {sections.map(([label, text]) => (
              <details key={label} className="border-t border-teal-100 pt-1.5">
                <summary className="text-xs font-semibold text-ink cursor-pointer hover:text-brand-dark">{label}</summary>
                <div className="text-xs text-ink-light mt-1.5 whitespace-pre-wrap max-h-56 overflow-y-auto bg-white rounded-lg p-3 border border-teal-100 leading-relaxed">{text}</div>
              </details>
            ))}
          </div>
        )}
      </div>
    </Section>
  );
}

/**
 * Slide-in drawer showing a single AI response in full: text, scoring rationale,
 * key claims, sources, and the cross-platform consensus. Shared by AI Response
 * Review (Results) and Phrasing Variation Testing. `detail` is the payload from
 * GET /responses/{id} (api.responseDetail).
 */
export function ResponseDetailDrawer({ detail, onClose }: { detail: any; onClose: () => void }) {
  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="fixed inset-0 bg-black/30 flex justify-end z-50" onClick={onClose}>
      <motion.div initial={{ x: "100%" }} animate={{ x: 0 }} exit={{ x: "100%" }} transition={{ type: "spring", damping: 30, stiffness: 300 }} className="w-full max-w-[640px] bg-white h-full overflow-y-auto shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="p-6">
          <div className="flex items-center justify-between mb-6">
            <div><h2 className="text-lg font-extrabold text-ink">{detail.llm_name}</h2><p className="text-xs text-ink-light font-medium">{detail.monitoring_mode === "DISEASE_STATE" ? "Disease-State Landscape" : detail.brand_focus} · {detail.therapeutic_area}</p></div>
            <button onClick={onClose} className="p-2 hover:bg-slate-100 rounded-xl transition-colors"><X size={18} className="text-ink-light" /></button>
          </div>
          <div className="flex gap-2 mb-6 flex-wrap items-center">
            <InfoTooltip content={buildDetailTooltip(detail)} />
            <SentimentBadge score={detail.sentiment_score} /><PositionBadge position={detail.competitive_position} /><IntentBadge intent={detail.intent_type} /><ConsensusBadge level={detail.consensus_level} />
            <span className="px-2.5 py-1 rounded-full text-xs font-semibold bg-slate-100 text-ink-light">{detail.persona}</span>
            <ThemeBadge theme={detail.domain} />
          </div>
          {detail.monitoring_mode === "DISEASE_STATE" && (
            <div className="mb-6 rounded-xl border-2 border-violet-300 bg-violet-50 px-4 py-2">
              <p className="text-[11px] font-extrabold uppercase tracking-wide text-violet-800">{PRELAUNCH_LABEL}</p>
            </div>
          )}
          <Section title="Question">
            {detail.question_text}
            {detail.is_variation && (detail.variation_of_text || detail.variation_of) && (
              <div className="mt-2 flex items-center gap-1.5 text-xs font-medium text-violet-700">
                <GitBranch size={13} className="shrink-0" />
                <span>Variation of: {detail.variation_of_text || detail.variation_of}</span>
              </div>
            )}
          </Section>
          <Section title="Response"><Markdown>{detail.response_text}</Markdown></Section>
          {detail.sources?.length > 0 && (
            <Section title={`Sources (${detail.sources.length})`}>
              <div className="space-y-2">
                {detail.sources.map((s: any, i: number) => (
                  <div key={i} className="flex items-start gap-2 text-sm">
                    <span className="text-xs font-bold text-ink-muted mt-0.5 w-6 flex-shrink-0">[{i + 1}]</span>
                    <ExternalLink size={14} className="mt-0.5 flex-shrink-0 text-brand-light" />
                    <a href={s.url} target="_blank" rel="noopener noreferrer" className="text-brand-dark hover:underline break-all">
                      <span className="font-medium">{s.title || s.url}</span>
                      {s.domain && <span className="text-ink-muted font-normal"> · {s.domain}</span>}
                    </a>
                  </div>
                ))}
              </div>
              {detail.search_queries?.length > 0 && (
                <div className="mt-4 flex flex-wrap items-center gap-2">
                  <span className="text-xs font-bold text-ink-muted uppercase tracking-wide">Searched</span>
                  {detail.search_queries.map((q: string, i: number) => (
                    <span key={i} className="px-2 py-0.5 bg-slate-100 text-ink-light rounded-md text-xs font-medium">{q}</span>
                  ))}
                </div>
              )}
            </Section>
          )}
          {detail.grounding_supports?.length > 0 && (
            <Section title={<span className="flex items-center gap-1.5"><InfoTooltip content="Specific quotes from the AI response that were verified against cited sources. These are the most trustworthy claims in the response." /> Grounded Claims</span>}>
              <div className="space-y-3">
                {detail.grounding_supports.map((g: any, i: number) => (
                  <div key={i} className="border-l-2 border-brand-light/40 pl-3">
                    <p className="text-sm text-ink-light italic">"{g.text}"</p>
                    <div className="flex flex-wrap gap-1 mt-1.5">
                      {g.source_indices.map((idx: number) => (
                        <span key={idx} className="px-1.5 py-0.5 bg-brand-surface text-brand-dark rounded text-[10px] font-bold">[{idx + 1}]</span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </Section>
          )}
          {detail.scoring_rationale && <Section title={<span className="flex items-center gap-1.5"><InfoTooltip content="The AI's explanation for why it assigned this sentiment score and competitive position to the response." /> Scoring Rationale</span>}><Markdown>{detail.scoring_rationale}</Markdown></Section>}
          {detail.key_claims?.length > 0 && (
            <Section title={<span className="flex items-center gap-1.5"><InfoTooltip content="The most important statements the AI made about this brand: extracted automatically from the full response." /> Key Claims</span>}><div className="flex flex-wrap gap-2">{detail.key_claims.map((c: string, i: number) => <span key={i} className="px-2.5 py-1 bg-brand-surface text-brand-dark rounded-lg text-xs font-semibold">{c}</span>)}</div></Section>
          )}
          {detail.alerts?.length > 0 && (
            <Section title="Alerts">{detail.alerts.map((a: any, i: number) => <div key={i} className="flex items-center gap-2 text-red-600 text-sm mb-2 font-medium"><AlertTriangle size={14} /> {a.rule}: {a.detail}</div>)}</Section>
          )}
          {detail.consensus && (
            <Section title={`Consensus Across AI Platforms (${detail.consensus.consensus_level})`}>
              {detail.consensus.final_answer && (
                <div className="mb-3">
                  <span className="flex items-center gap-1.5 text-xs font-bold text-ink-light uppercase tracking-wide"><InfoTooltip content="A single consolidated answer created by evaluating what most AI platforms agreed on. More reliable than any single model's response." /> Synthesized Final Answer</span>
                  <Markdown>{detail.consensus.final_answer}</Markdown>
                </div>
              )}
              {(detail.consensus.overall_sentiment != null || detail.consensus.overall_position) && (
                <div className="flex items-center gap-2 mb-3 flex-wrap">
                  <span className="text-xs font-bold text-ink-light uppercase tracking-wide mr-1">Overall</span>
                  <SentimentBadge score={detail.consensus.overall_sentiment ?? null} />
                  <PositionBadge position={detail.consensus.overall_position ?? null} />
                  {detail.consensus.models_scored > 0 && (
                    <span className="text-xs text-ink-light font-medium">across {detail.consensus.models_scored} models</span>
                  )}
                  {detail.consensus.sentiment_min != null && detail.consensus.sentiment_max != null && (
                    <span className="text-xs text-ink-light font-medium">range {Number(detail.consensus.sentiment_min).toFixed(2)} to {Number(detail.consensus.sentiment_max).toFixed(2)}</span>
                  )}
                </div>
              )}
              {detail.consensus.agreed_recommendation && <p className="text-sm mb-3 font-medium">{detail.consensus.agreed_recommendation}</p>}
              {detail.consensus.divergence_points?.length > 0 && (
                <div className="mb-3"><span className="text-xs font-bold text-ink-light uppercase tracking-wide">Divergence Points</span><ul className="list-disc list-inside text-sm text-ink-light mt-1 space-y-1">{detail.consensus.divergence_points.map((p: string, i: number) => <li key={i}>{p}</li>)}</ul></div>
              )}
              {detail.consensus.geo_fallback_used && <div className="flex items-center gap-2 text-teal-700 text-xs mt-3 p-3 bg-teal-50 rounded-xl font-semibold"><Shield size={14} />GEO verified data was used as fallback for this evaluation</div>}
              {detail.consensus.geo_context && (
                <details className="mt-3"><summary className="text-xs text-ink-light cursor-pointer font-bold uppercase tracking-wide">View GEO Context Data</summary><pre className="text-xs bg-brand-surface p-4 rounded-xl mt-2 overflow-x-auto max-h-48 font-mono">{JSON.stringify(detail.consensus.geo_context, null, 2)}</pre></details>
              )}
            </Section>
          )}
          <GeoGroundTruth brand={detail.monitoring_mode === "DISEASE_STATE" ? undefined : detail.brand_focus} />
          {detail.diff && (
            <Section title={`Change vs Previous (similarity ${detail.diff.similarity_ratio})`}>
              {detail.diff.material_change && <div className="text-amber-700 text-xs mb-2 font-bold">⚠ Material change detected</div>}
              <pre className="text-xs bg-brand-surface p-4 rounded-xl overflow-x-auto max-h-60 font-mono">{detail.diff.diff_text || "No prior response."}</pre>
            </Section>
          )}
        </div>
      </motion.div>
    </motion.div>
  );
}
