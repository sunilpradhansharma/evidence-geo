import React, { useState, useRef, useLayoutEffect, useEffect } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { motion, AnimatePresence } from "framer-motion";
import { Info, Check, ChevronDown, Clock, Minus } from "lucide-react";

/* ------------------------------------------------------------------ */
/*  Card                                                               */
/* ------------------------------------------------------------------ */
export function Card({
  title, children, className = "", accent = false,
}: {
  title?: React.ReactNode; children: React.ReactNode; className?: string; accent?: boolean;
}) {
  return (
    <div className={`bg-canvas-card rounded-2xl border ${accent ? "border-brand-light/30 shadow-md" : "border-line shadow-sm"} p-6 ${className}`}>
      {title && (
        <h3 className="text-xs font-bold text-ink uppercase tracking-widest mb-4">{title}</h3>
      )}
      {children}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Stat                                                               */
/* ------------------------------------------------------------------ */
/* ------------------------------------------------------------------ */
/*  InfoTooltip                                                       */
/* ------------------------------------------------------------------ */
type TipPos = { top: number; left: number; arrowLeft: number; flipped: boolean };

// Shared anchored-tooltip positioning: measures the *actual* rendered bubble after it
// mounts, then places + clamps it inside the viewport so it never runs off any screen
// edge (handles long, multi-line content). Used by both the (i) InfoTooltip and the
// generic hover Tooltip below.
function useAnchoredTooltip(
  show: boolean,
  side: "top" | "bottom",
  dep: unknown,
  triggerRef: React.RefObject<HTMLElement>,
  tipRef: React.RefObject<HTMLDivElement>,
  width = 280,
) {
  const [pos, setPos] = useState<TipPos>({ top: 0, left: 0, arrowLeft: 50, flipped: false });

  useLayoutEffect(() => {
    if (!show || !triggerRef.current || !tipRef.current) return;
    const r = triggerRef.current.getBoundingClientRect();
    const tipW = tipRef.current.offsetWidth || width;
    const tipH = tipRef.current.offsetHeight || 120;
    const pad = 10;
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    // Vertical: prefer the requested side, but flip to whichever side has room.
    const spaceAbove = r.top;
    const spaceBelow = vh - r.bottom;
    let flipped = false;
    if (side === "top") flipped = spaceAbove < tipH + pad && spaceBelow > spaceAbove;
    else flipped = spaceBelow < tipH + pad && spaceAbove > spaceBelow;
    const actualSide = flipped ? (side === "top" ? "bottom" : "top") : side;
    const top = actualSide === "top" ? r.top - 8 : r.bottom + 8;

    // Horizontal: center on the trigger, then clamp the box within the viewport.
    const rawCenter = r.left + r.width / 2;
    const left = Math.max(tipW / 2 + pad, Math.min(vw - tipW / 2 - pad, rawCenter));
    // Keep the arrow pointing at the trigger even after the box is clamped.
    const boxLeftEdge = left - tipW / 2;
    const arrowLeft = Math.max(8, Math.min(tipW - 8, rawCenter - boxLeftEdge));

    setPos({ top, left, arrowLeft, flipped });
  }, [show, side, dep, width]);

  const actualSide = pos.flipped ? (side === "top" ? "bottom" : "top") : side;
  return { pos, actualSide };
}

export function InfoTooltip({
  content, side = "top", iconClassName,
}: {
  content: string;
  side?: "top" | "bottom";
  iconClassName?: string;
}) {
  const [show, setShow] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  const tipRef = useRef<HTMLDivElement>(null);
  const { pos, actualSide } = useAnchoredTooltip(show, side, content, ref, tipRef);

  return (
    <span
      ref={ref}
      className="inline-flex items-center cursor-default"
      onMouseEnter={() => setShow(true)}
      onMouseLeave={() => setShow(false)}
    >
      <Info size={15} className={iconClassName ?? "text-slate-600 hover:text-slate-900 transition-colors shrink-0"} />
      {show && createPortal(
        <div
          ref={tipRef}
          style={{
            position: "fixed",
            top: pos.top,
            left: pos.left,
            transform: `translate(-50%, ${actualSide === "top" ? "-100%" : "0"})`,
            zIndex: 9999,
            pointerEvents: "none",
            width: "280px",
            maxWidth: "calc(100vw - 20px)",
          }}
          className="rounded-xl bg-slate-950 px-4 py-3 text-xs leading-relaxed text-white shadow-2xl whitespace-pre-line"
        >
          {content}
          <span
            style={{ left: `${pos.arrowLeft}px` }}
            className={`absolute -translate-x-1/2 border-[6px] border-transparent ${
              actualSide === "top" ? "top-full border-t-slate-950" : "bottom-full border-b-slate-950"
            }`}
          />
        </div>,
        document.body
      )}
    </span>
  );
}

/* ------------------------------------------------------------------ */
/*  Tooltip — generic hover wrapper (e.g. reveal truncated text)       */
/* ------------------------------------------------------------------ */
export function Tooltip({
  content, children, side = "top", width = 280, className = "",
}: {
  content: React.ReactNode;
  children: React.ReactNode;
  side?: "top" | "bottom";
  width?: number;
  className?: string;
}) {
  const [show, setShow] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  const tipRef = useRef<HTMLDivElement>(null);
  const { pos, actualSide } = useAnchoredTooltip(show, side, content, ref, tipRef, width);
  const hasContent = content != null && content !== "";

  return (
    <span
      ref={ref}
      className={className}
      onMouseEnter={() => setShow(true)}
      onMouseLeave={() => setShow(false)}
    >
      {children}
      {show && hasContent && createPortal(
        <div
          ref={tipRef}
          style={{
            position: "fixed",
            top: pos.top,
            left: pos.left,
            transform: `translate(-50%, ${actualSide === "top" ? "-100%" : "0"})`,
            zIndex: 9999,
            pointerEvents: "none",
            width: `${width}px`,
            maxWidth: "calc(100vw - 20px)",
          }}
          className="rounded-xl bg-slate-950 px-4 py-3 text-xs leading-relaxed text-white shadow-2xl whitespace-pre-line"
        >
          {content}
          <span
            style={{ left: `${pos.arrowLeft}px` }}
            className={`absolute -translate-x-1/2 border-[6px] border-transparent ${
              actualSide === "top" ? "top-full border-t-slate-950" : "bottom-full border-b-slate-950"
            }`}
          />
        </div>,
        document.body
      )}
    </span>
  );
}

export function Stat({ label, value, sub, icon, tooltip }: { label: string; value: React.ReactNode; sub?: string; icon?: React.ReactNode; tooltip?: string }) {
  return (
    <div className="h-full bg-canvas-card rounded-2xl border border-line shadow-sm p-5 group hover:border-brand-light/40 transition-colors">
      <div className="flex items-center gap-2 mb-1">
        {icon && <span className="text-brand-light">{icon}</span>}
        {tooltip && <InfoTooltip content={tooltip} />}
        <span className="text-xs font-semibold text-ink-light uppercase tracking-widest">{label}</span>
      </div>
      <div className="text-3xl font-display font-bold tracking-tight tabular-nums text-ink mt-1">{value}</div>
      {sub && <div className="text-xs text-ink-light mt-1 font-medium">{sub}</div>}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Badges                                                             */
/* ------------------------------------------------------------------ */
export function SentimentBadge({ score }: { score: number | null }) {
  if (score === null || score === undefined)
    return <span className="px-2.5 py-1 rounded-full text-xs font-semibold bg-slate-100 text-ink-light">unscored</span>;
  const color =
    score > 0.2 ? "bg-teal-100 text-teal-800" : score < -0.2 ? "bg-red-100 text-red-700" : "bg-amber-100 text-amber-800";
  return <span className={`px-2.5 py-1 rounded-full text-xs font-semibold ${color}`}>{score.toFixed(2)}</span>;
}

const POSITION_META: Record<string, { label: string; cls: string }> = {
  FIRST_LINE_RECOMMENDED: { label: "Leading", cls: "bg-teal-100 text-teal-800" },
  AMONG_OPTIONS:          { label: "Among options", cls: "bg-sky-100 text-sky-800" },
  SECOND_LINE:            { label: "Mentioned, not recommended first",  cls: "bg-amber-100 text-amber-800" },
  NOT_RECOMMENDED:        { label: "Not Endorsed", cls: "bg-red-100 text-red-700" },
  NOT_MENTIONED:          { label: "Not appearing in AI answers",  cls: "bg-slate-100 text-ink-light" },
};

export const POSITION_LABELS: Record<string, string> = Object.fromEntries(
  Object.entries(POSITION_META).map(([k, v]) => [k, v.label])
);

const POSITION_TOOLTIPS: Record<string, string> = {
  FIRST_LINE_RECOMMENDED: "AI consistently recommends this brand as the first-choice option for this indication.",
  AMONG_OPTIONS: "AI mentions this brand as a reasonable option, but does not lead with it.",
  SECOND_LINE: "AI positions this brand as a secondary or backup option.",
  NOT_RECOMMENDED: "AI is actively steering away from this brand: highest-priority risk signal.",
  NOT_MENTIONED: "AI did not mention this brand in its response at all.",
};

export function PositionBadge({ position }: { position: string | null }) {
  if (!position) return <span className="text-ink-light text-xs font-medium">N/A</span>;
  const meta = POSITION_META[position];
  return (
    <span className={`px-2.5 py-1 rounded-full text-xs font-semibold ${meta?.cls || "bg-slate-100 text-ink-light"}`}>
      {meta?.label ?? position.replace(/_/g, " ").toLowerCase()}
    </span>
  );
}

const CONSENSUS_COLORS: Record<string, string> = {
  FULL: "bg-teal-100 text-teal-800",
  PARTIAL: "bg-amber-100 text-amber-800",
  MISSING: "bg-red-100 text-red-700",
};

const CONSENSUS_TOOLTIPS: Record<string, string> = {
  FULL: "All 5 AI platforms agreed on their response for this question.",
  PARTIAL: "AI platforms gave mixed responses: some agreed, some diverged on this question.",
  MISSING: "AI platforms significantly disagreed. Verified GEO schema data was used as a fallback answer.",
};

export const CONSENSUS_LABELS: Record<string, string> = {
  FULL: "All platforms agree",
  PARTIAL: "Partial agreement",
  MISSING: "No consensus",
};

export function ConsensusBadge({ level }: { level: string | null }) {
  if (!level) return <span className="text-ink-muted text-xs font-medium">N/A</span>;
  return (
    <span className={`px-2.5 py-1 rounded-full text-xs font-semibold ${CONSENSUS_COLORS[level] || "bg-slate-100"}`}>
      {CONSENSUS_LABELS[level] ?? level.toLowerCase()}
    </span>
  );
}

const INTENT_COLORS: Record<string, string> = {
  CLINICAL: "bg-teal-100 text-teal-800",
  EXPERIENTIAL: "bg-sky-100 text-sky-800",
  SCREENING: "bg-amber-100 text-amber-800",
  SHORTHAND: "bg-slate-200 text-ink-light",
};

const INTENT_TOOLTIPS: Record<string, string> = {
  CLINICAL: "Question is about treatment efficacy, dosing, or clinical outcomes.",
  EXPERIENTIAL: "Question is about patient lived experience: side effects, quality of life.",
  SCREENING: "Question is about eligibility, diagnosis, or who qualifies for treatment.",
  SHORTHAND: "Quick brand/drug name lookup: no full AI synthesis needed.",
};

export function IntentBadge({ intent }: { intent: string | null }) {
  if (!intent) return <span className="text-ink-muted text-xs font-medium">N/A</span>;
  return (
    <span className={`px-2.5 py-1 rounded-full text-xs font-semibold ${INTENT_COLORS[intent] || "bg-slate-100"}`}>
      {intent.toLowerCase()}
    </span>
  );
}

const THEME_COLORS: Record<string, string> = {
  Efficacy: "bg-teal-100 text-teal-800",
  Safety: "bg-red-100 text-red-700",
  Access: "bg-sky-100 text-sky-800",
  Comparative: "bg-violet-100 text-violet-800",
  General: "bg-slate-100 text-ink-light",
};

export function ThemeBadge({ theme }: { theme: string | null }) {
  if (!theme) return <span className="text-ink-muted text-xs font-medium">N/A</span>;
  return (
    <span className={`px-2.5 py-1 rounded-full text-xs font-semibold ${THEME_COLORS[theme] || "bg-slate-100 text-ink-light"}`}>
      {theme}
    </span>
  );
}

/* ------------------------------------------------------------------ */
/*  StatusDot                                                          */
/* ------------------------------------------------------------------ */
export function StatusDot({ status }: { status: "idle" | "processing" | "complete" | "error" }) {
  const colors = {
    idle: "bg-slate-300",
    processing: "bg-brand-light animate-processing",
    complete: "bg-status-success",
    error: "bg-status-error",
  };
  return <span className={`inline-block w-2.5 h-2.5 rounded-full ${colors[status]}`} />;
}

/* ------------------------------------------------------------------ */
/*  Spinner                                                            */
/* ------------------------------------------------------------------ */
export function Spinner({ size = 20 }: { size?: number }) {
  return (
    <svg className="animate-spinner text-brand-light" width={size} height={size} viewBox="0 0 24 24" fill="none">
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeDasharray="31.4 31.4" />
    </svg>
  );
}

/* ------------------------------------------------------------------ */
/*  AnimatedCard — wrapper for motion entrance                         */
/* ------------------------------------------------------------------ */
export function AnimatedCard({
  children, className = "", delay = 0,
}: {
  children: React.ReactNode; className?: string; delay?: number;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay, ease: "easeOut" }}
      className={className}
    >
      {children}
    </motion.div>
  );
}

/* ------------------------------------------------------------------ */
/*  PageHeader                                                         */
/* ------------------------------------------------------------------ */
export function PageHeader({ title, subtitle, badge, tooltip }: { title: string; subtitle?: string; badge?: React.ReactNode; tooltip?: string }) {
  return (
    <div className="mb-6">
      <div className="flex items-center gap-3">
        <h1 className="text-2xl sm:text-3xl font-extrabold text-ink tracking-tight">{title}</h1>
        {badge}
      </div>
      {subtitle && (
        <p className="text-sm text-ink-light mt-1 font-medium flex items-center gap-1.5">
          {tooltip && <InfoTooltip content={tooltip} />}
          {subtitle}
        </p>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  ComingSoonBanner                                                   */
/* ------------------------------------------------------------------ */
export function ComingSoonBanner({
  title = "Coming soon", message, className = "",
}: {
  title?: string;
  message: string;
  className?: string;
}) {
  return (
    <div
      role="status"
      className={`mb-6 flex items-start gap-3 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 ${className}`}
    >
      <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-amber-100 text-amber-700">
        <Clock size={15} strokeWidth={2.4} />
      </span>
      <div className="min-w-0">
        <p className="text-sm font-bold text-amber-900">{title}</p>
        <p className="mt-1 text-[13px] font-medium leading-relaxed text-amber-800">{message}</p>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Select                                                             */
/* ------------------------------------------------------------------ */
export function Select({ label, value, options, groups, onChange, tooltip, optionLabels }: {
  label: string;
  value: string;
  options?: string[];
  groups?: Array<{ type: "option"; label: string } | { type: "group"; label: string; options: string[] }>;
  onChange: (v: string) => void;
  tooltip?: string;
  optionLabels?: Record<string, string>;
}) {
  return (
    <div className="flex flex-col">
      <label className="text-xs font-semibold text-ink-light mb-1.5 uppercase tracking-wide flex items-center gap-1">
        {tooltip && <InfoTooltip content={tooltip} />}
        {label}
      </label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="border border-line rounded-xl px-3 py-2 text-sm bg-canvas-card text-ink font-medium focus:outline-none focus:ring-2 focus:ring-brand-light/40 focus:border-brand-light transition-colors"
      >
        {groups ? (
          <>
            <option value="">All</option>
            {groups.map((entry) =>
              entry.type === "option" ? (
                <option key={entry.label} value={entry.label}>{entry.label}</option>
              ) : (
                <optgroup key={entry.label} label={entry.label}>
                  {entry.options.map((o) => <option key={o} value={o}>{o}</option>)}
                </optgroup>
              )
            )}
          </>
        ) : (
          (options ?? []).map((o) => <option key={o} value={o}>{o ? (optionLabels?.[o] ?? o) : "All"}</option>)
        )}
      </select>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  MultiSelect — checkbox dropdown for filtering by several values    */
/* ------------------------------------------------------------------ */
type MultiSelectEntry =
  | { type: "option"; label: string }
  | { type: "group"; label: string; options: string[] };

function MultiOptionRow({
  value, label, checked, indeterminate = false, onToggle, indent = false, bold = false,
}: {
  value: string; label: string; checked: boolean; indeterminate?: boolean;
  onToggle: (v: string) => void; indent?: boolean; bold?: boolean;
}) {
  const filled = checked || indeterminate;
  return (
    <button
      type="button"
      onClick={() => onToggle(value)}
      className={`flex w-full items-center gap-2 rounded-lg px-3 py-1.5 text-left text-sm hover:bg-brand-surface ${indent ? "pl-7" : ""}`}
    >
      <span
        className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${
          filled ? "border-brand bg-brand text-white" : "border-slate-300 bg-white"
        }`}
      >
        {indeterminate ? <Minus size={11} strokeWidth={3} /> : checked && <Check size={11} strokeWidth={3} />}
      </span>
      <span className={`text-ink ${bold ? "font-bold" : "font-medium"}`}>{label}</span>
    </button>
  );
}

const SELECT_ALL = "\u0000__all__";

/**
 * Checkbox dropdown that opens with everything already ticked.
 *
 * `values` keeps its contract: an EMPTY array means "all / no filter". That is what the
 * callers send to the API, so a full selection is normalised back to `[]` rather than
 * shipped as an explicit list — several filters sit on columns that can be blank, and an
 * explicit list would quietly exclude those rows, making "everything selected" return
 * fewer results than "nothing selected".
 */
export function MultiSelect({
  label, values, options, groups, onChange, tooltip, placeholder = "All", optionLabels,
}: {
  label: string;
  values: string[];
  options?: string[];
  groups?: MultiSelectEntry[];
  onChange: (v: string[]) => void;
  tooltip?: string;
  placeholder?: string;
  optionLabels?: Record<string, string>;
}) {
  const [open, setOpen] = useState(false);
  // Transient: the user unticked "Select all" so the boxes clear and they can pick one.
  // Nothing has been narrowed yet, which the inline note below says out loud.
  const [choosing, setChoosing] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) {
      setChoosing(false);
      return;
    }
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const flat = (options ?? []).filter(Boolean);
  const allOptions = groups
    ? groups.flatMap((e) => (e.type === "option" ? [e.label] : e.options))
    : flat;

  const allSelected =
    values.length === 0 ||
    (allOptions.length > 0 && allOptions.every((o) => values.includes(o)));

  const commit = (next: string[]) =>
    onChange(next.length === 0 || allOptions.every((o) => next.includes(o)) ? [] : next);

  const toggle = (v: string) => {
    if (v === SELECT_ALL) {
      if (choosing || !allSelected) {
        setChoosing(false);
        onChange([]);
      } else {
        setChoosing(true);
      }
      return;
    }
    if (choosing) {
      setChoosing(false);
      commit([v]);
      return;
    }
    // Everything is ticked, so clicking a row means "not that one".
    if (allSelected) {
      commit(allOptions.filter((o) => o !== v));
      return;
    }
    commit(values.includes(v) ? values.filter((x) => x !== v) : [...values, v]);
  };

  const rowChecked = (v: string) => !choosing && (allSelected || values.includes(v));

  const summary = choosing
    ? "Choose one or more"
    : allSelected
    ? placeholder
    : values.length === 1
    ? optionLabels?.[values[0]] ?? values[0]
    : `${values.length} selected`;

  return (
    <div className="flex flex-col" ref={ref}>
      <label className="text-xs font-semibold text-ink-light mb-1.5 uppercase tracking-wide flex items-center gap-1">
        {tooltip && <InfoTooltip content={tooltip} />}
        {label}
      </label>
      <div className="relative">
        <button
          type="button"
          onClick={() => setOpen((s) => !s)}
          className="flex min-w-[11rem] items-center justify-between gap-2 rounded-xl border border-line bg-canvas-card px-3 py-2 text-sm font-medium text-ink transition-colors focus:outline-none focus:ring-2 focus:ring-brand-light/40 focus:border-brand-light"
        >
          <span className={allSelected || choosing ? "text-ink-light" : "text-ink"}>{summary}</span>
          <ChevronDown size={14} className={`shrink-0 text-ink-muted transition-transform ${open ? "rotate-180" : ""}`} />
        </button>
        {open && (
          <div className="absolute left-0 z-30 mt-1 max-h-72 w-max min-w-full overflow-auto rounded-xl border border-line bg-canvas-card p-1 shadow-lg">
            <MultiOptionRow
              value={SELECT_ALL}
              label="Select all"
              checked={!choosing && allSelected}
              indeterminate={!choosing && !allSelected}
              onToggle={toggle}
              bold
            />
            {choosing && (
              <p className="px-3 pb-1.5 pt-0.5 text-[11px] font-medium text-ink-muted">
                Nothing chosen yet — all are still included.
              </p>
            )}
            <div className="my-1 border-t border-line" />
            {groups
              ? groups.map((entry) =>
                  entry.type === "option" ? (
                    <MultiOptionRow key={entry.label} value={entry.label} label={entry.label} checked={rowChecked(entry.label)} onToggle={toggle} />
                  ) : (
                    <div key={entry.label}>
                      <div className="px-3 pb-1 pt-2 text-[10px] font-bold uppercase tracking-wider text-ink-muted">{entry.label}</div>
                      {entry.options.map((o) => (
                        <MultiOptionRow key={o} value={o} label={optionLabels?.[o] ?? o} checked={rowChecked(o)} onToggle={toggle} indent />
                      ))}
                    </div>
                  )
                )
              : flat.map((o) => (
                  <MultiOptionRow key={o} value={o} label={optionLabels?.[o] ?? o} checked={rowChecked(o)} onToggle={toggle} />
                ))}
          </div>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Markdown renderer                                                  */
/* ------------------------------------------------------------------ */
export function Markdown({ children, className = "" }: { children?: string | null; className?: string }) {
  return (
    <div className={`text-sm text-ink leading-relaxed break-words ${className}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: ({ node, ...props }) => <h1 {...props} className="text-lg font-bold text-ink mt-4 mb-2 first:mt-0" />,
          h2: ({ node, ...props }) => <h2 {...props} className="text-base font-bold text-ink mt-4 mb-2 first:mt-0" />,
          h3: ({ node, ...props }) => <h3 {...props} className="text-sm font-bold text-ink mt-3 mb-1.5 first:mt-0" />,
          h4: ({ node, ...props }) => <h4 {...props} className="text-sm font-semibold text-ink mt-3 mb-1.5 first:mt-0" />,
          h5: ({ node, ...props }) => <h5 {...props} className="text-sm font-semibold text-ink mt-2 mb-1 first:mt-0" />,
          h6: ({ node, ...props }) => <h6 {...props} className="text-sm font-semibold text-ink mt-2 mb-1 first:mt-0" />,
          p: ({ node, ...props }) => <p {...props} className="my-2 first:mt-0 last:mb-0" />,
          ul: ({ node, ...props }) => <ul {...props} className="list-disc pl-5 my-2 space-y-1" />,
          ol: ({ node, ...props }) => <ol {...props} className="list-decimal pl-5 my-2 space-y-1" />,
          li: ({ node, ...props }) => <li {...props} className="leading-relaxed" />,
          strong: ({ node, ...props }) => <strong {...props} className="font-semibold text-ink" />,
          em: ({ node, ...props }) => <em {...props} className="italic" />,
          a: ({ node, ...props }) => (
            <a {...props} target="_blank" rel="noreferrer" className="text-brand-light underline break-words hover:text-brand" />
          ),
          blockquote: ({ node, ...props }) => (
            <blockquote {...props} className="border-l-4 border-brand-light/30 pl-3 my-2 text-ink-light" />
          ),
          hr: ({ node, ...props }) => <hr {...props} className="my-3 border-slate-200" />,
          code: ({ node, ...props }) => (
            <code {...props} className="rounded-md bg-brand-surface px-1.5 py-0.5 font-mono text-[0.85em] text-ink" />
          ),
          pre: ({ node, ...props }) => (
            <pre
              {...props}
              className="my-2 overflow-x-auto rounded-xl bg-brand-surface p-4 text-xs text-ink [&>code]:bg-transparent [&>code]:p-0 [&>code]:text-[0.95em]"
            />
          ),
          table: ({ node, ...props }) => (
            <div className="my-2 overflow-x-auto">
              <table {...props} className="w-full border-collapse text-sm text-ink" />
            </div>
          ),
          thead: ({ node, ...props }) => <thead {...props} className="bg-brand-surface" />,
          th: ({ node, ...props }) => (
            <th {...props} className="border border-slate-200 px-3 py-1.5 text-left font-semibold" />
          ),
          td: ({ node, ...props }) => <td {...props} className="border border-slate-200 px-3 py-1.5 align-top" />,
        }}
      >
        {children || ""}
      </ReactMarkdown>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Empty state                                                        */
/* ------------------------------------------------------------------ */
export function EmptyState({ message = "No data yet.", icon, action }: {
  message?: string;
  icon?: React.ReactNode;
  /** The way out, when there is one — e.g. clearing the filters that emptied the page. */
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-ink-light">
      {icon && <div className="mb-3 text-brand-light/60">{icon}</div>}
      <p className="max-w-xl text-center text-sm font-medium">{message}</p>
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}
