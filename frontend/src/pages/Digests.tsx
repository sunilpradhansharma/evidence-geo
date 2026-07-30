import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { motion } from "framer-motion";
import {
  AlertTriangle,
  CalendarClock,
  CheckCircle2,
  ChevronDown,
  Clock,
  ExternalLink,
  FileText,
  FlaskConical,
  Mail,
  Play,
  Plus,
  Quote,
  Send,
  ShieldCheck,
  Trash2,
  Webhook,
} from "lucide-react";
import {
  api,
  DigestProfile,
  DigestProfileCreate,
  DigestRule,
  DigestRun,
  SesStatus,
  WorkshopCitations,
  WorkshopInsights,
  WorkshopModelSourceDomain,
} from "../api/client";
import { Card, EmptyState, PageHeader, PositionBadge, Select, Spinner } from "../components/ui";

const ALERT_CATEGORIES = ["LOW_SENTIMENT", "NOT_RECOMMENDED", "COMPETITOR_ADVANTAGE"];
const DOMAINS = ["General", "Efficacy", "Safety", "Comparative", "Access"];
const DELIVERY_METHODS = ["in_app", "email", "webhook"];

/* ================================================================== */
/*  SCHEDULE HELPERS (translate a friendly day+time to/from cron)      */
/* ================================================================== */
const WEEKDAYS = [
  { value: "1", label: "Monday" },
  { value: "2", label: "Tuesday" },
  { value: "3", label: "Wednesday" },
  { value: "4", label: "Thursday" },
  { value: "5", label: "Friday" },
  { value: "6", label: "Saturday" },
  { value: "0", label: "Sunday" },
];
const WEEKDAY_LABEL: Record<string, string> = Object.fromEntries(
  WEEKDAYS.map((d) => [d.value, d.label]),
);

type ScheduleParts = { frequency: "daily" | "weekly"; day: string; time: string };

function scheduleToCron(s: ScheduleParts): string {
  const [h = "8", m = "0"] = (s.time || "08:00").split(":");
  const min = parseInt(m, 10) || 0;
  const hr = parseInt(h, 10) || 0;
  const dow = s.frequency === "daily" ? "*" : s.day;
  return `${min} ${hr} * * ${dow}`;
}

function cronToSchedule(cron: string): ScheduleParts {
  const parts = (cron || "").trim().split(/\s+/);
  if (parts.length < 5) return { frequency: "weekly", day: "1", time: "08:00" };
  const [min, hr, , , dow] = parts;
  const time = `${String(hr).padStart(2, "0")}:${String(min).padStart(2, "0")}`;
  if (dow === "*") return { frequency: "daily", day: "1", time };
  const day = dow === "7" ? "0" : dow;
  return { frequency: "weekly", day: WEEKDAY_LABEL[day] ? day : "1", time };
}

function scheduleSummary(cron: string): string {
  const s = cronToSchedule(cron);
  const [h, m] = s.time.split(":").map((x) => parseInt(x, 10));
  const ampm = h >= 12 ? "PM" : "AM";
  const h12 = ((h + 11) % 12) + 1;
  const t = `${h12}:${String(m).padStart(2, "0")} ${ampm}`;
  return s.frequency === "daily"
    ? `Every day at ${t}`
    : `Every ${WEEKDAY_LABEL[s.day] ?? "Monday"} at ${t}`;
}

function Chips({
  label,
  options,
  selected,
  onToggle,
}: {
  label: string;
  options: string[];
  selected: string[];
  onToggle: (v: string) => void;
}) {
  return (
    <div>
      <label className="block text-xs font-bold text-ink-light uppercase tracking-widest mb-1.5">{label}</label>
      <div className="flex flex-wrap gap-2">
        {options.map((o) => {
          const on = selected.includes(o);
          return (
            <button
              key={o}
              type="button"
              onClick={() => onToggle(o)}
              className={`px-3 py-1.5 rounded-full text-xs font-bold border transition-colors ${
                on ? "bg-brand text-white border-brand" : "bg-white text-ink-light border-line hover:border-brand-light"
              }`}
            >
              {o.replace(/_/g, " ")}
            </button>
          );
        })}
      </div>
    </div>
  );
}

/* ================================================================== */
/*  CREATE PROFILE FORM                                                */
/* ================================================================== */
const EMPTY_RULE: DigestRule = { alert_categories: [], domains: [] };

function CreateProfileForm({ onCreated }: { onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<DigestProfileCreate>({
    role: "",
    description: "",
    cron: "0 8 * * 1",
    timezone: "America/Chicago",
    recipients: [],
    delivery_methods: ["in_app"],
    rules: [{ ...EMPTY_RULE }],
  });
  const [recipientsText, setRecipientsText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const toggleRuleField = (idx: number, field: "alert_categories" | "domains", v: string) => {
    setForm((f) => {
      const rules = [...(f.rules ?? [])];
      const cur = new Set((rules[idx][field] as string[]) ?? []);
      cur.has(v) ? cur.delete(v) : cur.add(v);
      rules[idx] = { ...rules[idx], [field]: [...cur] };
      return { ...f, rules };
    });
  };

  const toggleMethod = (m: string) =>
    setForm((f) => {
      const cur = new Set(f.delivery_methods ?? []);
      cur.has(m) ? cur.delete(m) : cur.add(m);
      return { ...f, delivery_methods: [...cur] };
    });

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const recipients = recipientsText.split(/[,\n;]/).map((s) => s.trim()).filter(Boolean);
      await api.createDigestProfile({ ...form, recipients });
      setForm({
        role: "", description: "", cron: "0 8 * * 1", timezone: "America/Chicago",
        recipients: [], delivery_methods: ["in_app"], rules: [{ ...EMPTY_RULE }],
      });
      setRecipientsText("");
      setOpen(false);
      onCreated();
    } catch {
      setError("Could not create the profile. Ensure the role is set.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card
      title={
        <button onClick={() => setOpen((o) => !o)} className="flex items-center gap-2 text-left w-full">
          <Plus size={16} className="text-brand" /> New Digest Profile
          <ChevronDown size={16} className={`ml-auto transition-transform ${open ? "rotate-180" : ""}`} />
        </button>
      }
    >
      {open && (
        <div className="space-y-5 pt-1">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-bold text-ink-light uppercase tracking-widest mb-1.5">Role</label>
              <input
                type="text"
                placeholder="e.g. PV, Brand, Medical Affairs"
                value={form.role}
                onChange={(e) => setForm({ ...form, role: e.target.value })}
                className="w-full rounded-xl border border-line bg-white px-3 py-2 text-sm font-medium text-ink focus:border-brand-light focus:outline-none"
              />
            </div>
            <div>
              <label className="block text-xs font-bold text-ink-light uppercase tracking-widest mb-1.5 flex items-center gap-1.5">
                <CalendarClock size={13} /> Schedule
              </label>
              <div className="grid grid-cols-2 gap-2">
                <select
                  value={cronToSchedule(form.cron ?? "").frequency}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      cron: scheduleToCron({
                        ...cronToSchedule(form.cron ?? ""),
                        frequency: e.target.value as "daily" | "weekly",
                      }),
                    })
                  }
                  className="rounded-xl border border-line bg-white px-3 py-2 text-sm font-medium text-ink focus:border-brand-light focus:outline-none"
                >
                  <option value="weekly">Weekly</option>
                  <option value="daily">Daily</option>
                </select>
                {cronToSchedule(form.cron ?? "").frequency === "weekly" ? (
                  <select
                    value={cronToSchedule(form.cron ?? "").day}
                    onChange={(e) =>
                      setForm({
                        ...form,
                        cron: scheduleToCron({
                          ...cronToSchedule(form.cron ?? ""),
                          day: e.target.value,
                        }),
                      })
                    }
                    className="rounded-xl border border-line bg-white px-3 py-2 text-sm font-medium text-ink focus:border-brand-light focus:outline-none"
                  >
                    {WEEKDAYS.map((d) => (
                      <option key={d.value} value={d.value}>{d.label}</option>
                    ))}
                  </select>
                ) : (
                  <div />
                )}
              </div>
              <div className="mt-2 flex items-center gap-2">
                <Clock size={14} className="text-ink-light" />
                <input
                  type="time"
                  value={cronToSchedule(form.cron ?? "").time}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      cron: scheduleToCron({
                        ...cronToSchedule(form.cron ?? ""),
                        time: e.target.value,
                      }),
                    })
                  }
                  className="rounded-xl border border-line bg-white px-3 py-2 text-sm font-medium text-ink focus:border-brand-light focus:outline-none"
                />
              </div>
              <p className="mt-1.5 text-[11px] text-ink-muted font-medium">
                {scheduleSummary(form.cron ?? "")} · {form.timezone}
              </p>
            </div>
          </div>

          <div>
            <label className="block text-xs font-bold text-ink-light uppercase tracking-widest mb-1.5">Description</label>
            <input
              type="text"
              placeholder="What this role's digest covers"
              value={form.description ?? ""}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
              className="w-full rounded-xl border border-line bg-white px-3 py-2 text-sm font-medium text-ink focus:border-brand-light focus:outline-none"
            />
          </div>

          <div className="rounded-xl border border-line p-4 space-y-4 bg-brand-surface/30">
            <p className="text-xs font-bold text-ink uppercase tracking-widest">Filter Rule (which alerts this role receives)</p>
            <Chips
              label="Alert Categories"
              options={ALERT_CATEGORIES}
              selected={(form.rules?.[0]?.alert_categories as string[]) ?? []}
              onToggle={(v) => toggleRuleField(0, "alert_categories", v)}
            />
            <Chips
              label="Question Domains"
              options={DOMAINS}
              selected={(form.rules?.[0]?.domains as string[]) ?? []}
              onToggle={(v) => toggleRuleField(0, "domains", v)}
            />
            <p className="text-[11px] text-ink-muted font-medium">Leave a filter empty to include all values for that dimension.</p>
          </div>

          <Chips label="Delivery Methods" options={DELIVERY_METHODS} selected={form.delivery_methods ?? []} onToggle={toggleMethod} />

          <div>
            <label className="block text-xs font-bold text-ink-light uppercase tracking-widest mb-1.5">Recipients (email delivery)</label>
            <textarea
              rows={2}
              placeholder="comma or newline separated emails"
              value={recipientsText}
              onChange={(e) => setRecipientsText(e.target.value)}
              className="w-full rounded-xl border border-line bg-white px-3 py-2 text-sm font-medium text-ink focus:border-brand-light focus:outline-none"
            />
          </div>

          {error && <p className="text-xs font-semibold text-red-600">{error}</p>}
          <button
            disabled={busy || !form.role}
            onClick={submit}
            className="flex items-center gap-2 px-5 py-2.5 bg-brand text-white rounded-xl text-sm font-bold hover:bg-brand-dark disabled:opacity-40 transition-colors"
          >
            <Plus size={16} /> Create Profile
          </button>
        </div>
      )}
    </Card>
  );
}

/* ================================================================== */
/*  WORKSHOP QUESTIONS INSIGHTS (live snapshot, same as the digest)   */
/* ================================================================== */
// Workshop designation (Persona + indication from Rhem.csv) -> badge colors.
// Mirrors the Approved Question Bank (Questions.tsx) convention.
const DESIGNATION_CLS: Record<string, string> = {
  "Patient RA": "bg-sky-100 text-sky-700",
  "Patient PsA": "bg-cyan-100 text-cyan-700",
  "HCP RA": "bg-indigo-100 text-indigo-700",
  "HCP PsA": "bg-violet-100 text-violet-700",
  "HCP RA & PsA": "bg-fuchsia-100 text-fuchsia-700",
};
const desigCls = (d: string) => DESIGNATION_CLS[d] ?? "bg-slate-100 text-ink-light";

const fmtSent = (v: number | null): string =>
  v === null || v === undefined ? "\u2014" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}`;

function MiniStat({ label, value, sub }: { label: string; value: ReactNode; sub?: string }) {
  return (
    <div className="rounded-xl border border-line bg-brand-surface/40 px-4 py-3">
      <div className="text-[11px] font-semibold text-ink-light uppercase tracking-wide">{label}</div>
      <div className="mt-0.5 text-2xl font-bold tabular-nums text-ink">{value}</div>
      {sub && <div className="text-[11px] text-ink-muted font-medium mt-0.5 truncate" title={sub}>{sub}</div>}
    </div>
  );
}

function ShareOfVoiceBar({ c }: { c: WorkshopCitations }) {
  const segs = [
    { label: "AbbVie", pct: c.abbvie_share_pct, cls: "bg-teal-500" },
    { label: "Competitor", pct: c.competitor_share_pct, cls: "bg-red-500" },
    { label: "Independent", pct: c.independent_share_pct, cls: "bg-slate-400" },
  ];
  return (
    <div>
      <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-slate-100">
        {segs.filter((s) => s.pct > 0).map((s) => (
          <div key={s.label} className={s.cls} style={{ width: `${s.pct}%` }} title={`${s.label} ${s.pct}%`} />
        ))}
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-[11px] font-semibold text-ink-light">
        {segs.map((s) => (
          <span key={s.label} className="inline-flex items-center gap-1">
            <span className={`h-2 w-2 rounded-full ${s.cls}`} /> {s.label} {s.pct}%
          </span>
        ))}
      </div>
    </div>
  );
}

const CONTROL_CHIP: Record<string, string> = {
  ABBVIE: "border-teal-200 bg-teal-50 text-teal-800",
  COMPETITOR: "border-red-200 bg-red-50 text-red-700",
  INDEPENDENT: "border-slate-200 bg-slate-50 text-ink-light",
  UNKNOWN: "border-slate-200 bg-slate-50 text-ink-light",
};

function SourceChip({ d }: { d: WorkshopModelSourceDomain }) {
  const cls = CONTROL_CHIP[d.control_type] ?? CONTROL_CHIP.UNKNOWN;
  const label = `${d.publisher_name || d.authority_domain} (${d.citation_count})`;
  const chip = (
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-semibold ${cls}`}>
      {label}
    </span>
  );
  return d.url ? (
    <a href={d.url} target="_blank" rel="noopener noreferrer" className="hover:opacity-80" title={d.url}>{chip}</a>
  ) : chip;
}

const SCOPE_TABS = [
  { key: "workshop" as const, label: "Workshop Questions" },
  { key: "all" as const, label: "All Tracked Questions" },
];
type InsightScope = "workshop" | "all";

const SCOPE_COPY: Record<InsightScope, { intro: string; empty: string }> = {
  workshop: {
    intro:
      "How AI is answering the curated workshop set right now: how it positions the brands, what each AI platform says, and the sources each platform cites. This is the same snapshot included in every stakeholder digest.",
    empty:
      "No workshop-question answers to summarize yet. Run the curated Workshop Questions set and this tab shows how AI positions the brands, what each AI platform says, and the sources each platform cites. It is the same snapshot included in every stakeholder digest.",
  },
  all: {
    intro:
      "How AI is answering every tracked question right now: how it positions the brands across all audiences and areas, what each AI platform says, and the sources each platform cites.",
    empty:
      "No answered questions to summarize yet. Once questions have been run, this tab shows how AI positions the brands across your entire tracked set, what each AI platform says, and the sources each platform cites.",
  },
};

function ScopeTabs({ scope, onChange }: { scope: InsightScope; onChange: (s: InsightScope) => void }) {
  return (
    <div className="mb-4 inline-flex rounded-xl border border-line bg-brand-surface/40 p-1">
      {SCOPE_TABS.map((t) => (
        <button
          key={t.key}
          onClick={() => onChange(t.key)}
          className={`px-3.5 py-1.5 rounded-lg text-xs font-bold transition-colors ${
            scope === t.key ? "bg-brand text-white shadow-sm" : "text-ink-light hover:text-ink"
          }`}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

function WorkshopInsightsPanel() {
  const [scope, setScope] = useState<InsightScope>("workshop");
  const [data, setData] = useState<WorkshopInsights | null>(null);
  const [available, setAvailable] = useState(false);
  const [loading, setLoading] = useState(true);
  const polls = useRef(0);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    polls.current = 0;
    setLoading(true);
    const load = () => {
      api
        .digestWorkshopInsights(scope)
        .then((r) => {
          if (!alive) return;
          setAvailable(r.available);
          setData(r.insights);
          // The server kicks off the per-platform LLM summary in the background; re-fetch a
          // few times so freshly-generated narratives appear without a manual reload.
          if (r.insights?.needs_summary_refresh && polls.current < 4) {
            polls.current += 1;
            timer = setTimeout(load, 6000);
          }
        })
        .catch(() => { if (alive) { setAvailable(false); setData(null); } })
        .finally(() => { if (alive) setLoading(false); });
    };
    load();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [scope]);

  const title = (
    <span className="flex items-center gap-2">
      <FlaskConical size={15} className="text-brand" /> AI Answer Insights
    </span>
  );
  const tabs = <ScopeTabs scope={scope} onChange={setScope} />;

  if (loading) {
    return (
      <Card accent title={title}>
        {tabs}
        <div className="flex justify-center py-10"><Spinner /></div>
      </Card>
    );
  }
  if (!available || !data) {
    return (
      <Card accent title={title}>
        {tabs}
        <EmptyState icon={<FlaskConical size={28} />} message={SCOPE_COPY[scope].empty} />
      </Card>
    );
  }

  const neutralPct = Math.max(0, Math.round((100 - data.favorable_pct - data.weak_pct) * 10) / 10);

  return (
    <Card accent title={title}>
      {tabs}
      <p className="text-sm text-ink-light font-medium mb-4">
        {SCOPE_COPY[scope].intro}
      </p>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <MiniStat label="Questions" value={data.questions_covered} />
        <MiniStat label="AI answers" value={data.responses} sub={data.latest_at ? `latest ${data.latest_at}` : undefined} />
        <MiniStat label="AI Platforms" value={data.models.length} sub={data.models.join(", ") || undefined} />
        <MiniStat label="Avg sentiment" value={fmtSent(data.avg_sentiment)} />
      </div>

      {data.scored > 0 && (
        <div className="mt-5">
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-xs font-bold uppercase tracking-widest text-ink">How AI positions the brands</span>
            <span className="text-[11px] font-semibold text-ink-light">
              <span className="text-teal-700">{data.favorable_pct}% favorable</span> · <span className="text-red-600">{data.weak_pct}% weak</span>
            </span>
          </div>
          <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-slate-100">
            {data.favorable_pct > 0 && <div className="bg-teal-500" style={{ width: `${data.favorable_pct}%` }} title={`Favorable ${data.favorable_pct}%`} />}
            {data.weak_pct > 0 && <div className="bg-red-500" style={{ width: `${data.weak_pct}%` }} title={`Weak ${data.weak_pct}%`} />}
            {neutralPct > 0 && <div className="bg-slate-300" style={{ width: `${neutralPct}%` }} title={`Not mentioned / neutral ${neutralPct}%`} />}
          </div>
        </div>
      )}

      {data.by_designation.length > 0 && (
        <div className="mt-6 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wide text-ink-light">
                <th className="pb-2 pr-3 font-bold">Audience · indication</th>
                <th className="pb-2 px-3 font-bold text-right">Answers</th>
                <th className="pb-2 px-3 font-bold text-right">Avg sentiment</th>
                <th className="pb-2 px-3 font-bold text-right">Favorable</th>
                <th className="pb-2 pl-3 font-bold text-right">Weak</th>
              </tr>
            </thead>
            <tbody>
              {data.by_designation.map((d) => (
                <tr key={d.designation} className="border-t border-line">
                  <td className="py-2 pr-3">
                    <span className={`px-2 py-0.5 rounded-full text-[11px] font-bold ${desigCls(d.designation)}`}>{d.designation}</span>
                  </td>
                  <td className="py-2 px-3 text-right tabular-nums text-ink">{d.responses}</td>
                  <td className="py-2 px-3 text-right tabular-nums text-ink-light">{fmtSent(d.avg_sentiment)}</td>
                  <td className="py-2 px-3 text-right tabular-nums font-semibold text-teal-700">{d.favorable}</td>
                  <td className="py-2 pl-3 text-right tabular-nums font-semibold text-red-600">{d.weak}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data.by_model.length > 0 && (
        <div className="mt-6">
          <h4 className="text-xs font-bold uppercase tracking-widest text-ink mb-2 flex items-center gap-1.5">
            <Quote size={13} className="text-brand" /> What each AI platform is saying
          </h4>
          <div className="space-y-3">
            {data.by_model.map((m) => (
              <div key={m.llm} className="rounded-xl border border-line bg-canvas-card p-3">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="text-sm font-bold text-ink">{m.llm}</span>
                  <span className="text-[11px] font-semibold text-ink-light">
                    {m.responses} answer{m.responses !== 1 ? "s" : ""}
                    {m.avg_sentiment !== null && <> · sentiment {fmtSent(m.avg_sentiment)}</>}
                    {(m.favorable > 0 || m.weak > 0) && (
                      <> · <span className="text-teal-700">{m.favorable} favorable</span> / <span className="text-red-600">{m.weak} weak</span></>
                    )}
                  </span>
                </div>
                {m.summary ? (
                  <p className="mt-2 text-[13px] text-ink leading-relaxed">{m.summary}</p>
                ) : (
                  <p className="mt-2 text-xs text-ink-muted italic">
                    {data.needs_summary_refresh ? "Generating summary\u2026" : "No summary available yet."}
                  </p>
                )}
                {m.sources ? (
                  <div className="mt-3 border-t border-line pt-2">
                    <div className="text-[11px] font-bold text-ink mb-1.5">
                      Sources {m.llm} cited{" "}
                      <span className="font-semibold text-ink-light">
                        ({m.sources.total_citations}: AbbVie {m.sources.abbvie} · Competitor {m.sources.competitor} · Independent {m.sources.independent})
                      </span>
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {m.sources.domains.map((d) => <SourceChip key={d.authority_domain} d={d} />)}
                    </div>
                  </div>
                ) : m.answered_from_knowledge ? (
                  <p className="mt-3 border-t border-line pt-2 text-[11px] text-ink-muted italic">
                    Answered from the model's own knowledge (no web sources cited).
                  </p>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      )}

      {data.needs_attention.length > 0 && (
        <div className="mt-6">
          <h4 className="text-xs font-bold uppercase tracking-widest text-ink mb-2 flex items-center gap-1.5">
            <AlertTriangle size={13} className="text-red-500" /> Needs attention
            <span className="text-[11px] font-semibold text-ink-light normal-case tracking-normal">
              ({data.needs_attention_count} weak or negative answer{data.needs_attention_count !== 1 ? "s" : ""})
            </span>
          </h4>
          <div className="space-y-2">
            {data.needs_attention.map((n, i) => (
              <div key={i} className="rounded-xl border border-red-100 bg-red-50/40 p-3">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-xs font-bold text-ink">{n.platform}</span>
                  {n.designation && <span className={`px-2 py-0.5 rounded-full text-[11px] font-bold ${desigCls(n.designation)}`}>{n.designation}</span>}
                  {n.competitive_position && <PositionBadge position={n.competitive_position} />}
                  {n.sentiment_score !== null && <span className="text-[11px] font-semibold text-ink-muted">sentiment {fmtSent(n.sentiment_score)}</span>}
                </div>
                <p className="mt-1 text-xs font-semibold text-ink">{n.question}</p>
                {n.summary && <p className="mt-0.5 text-xs text-ink-light leading-relaxed">{n.summary}</p>}
              </div>
            ))}
          </div>
        </div>
      )}

      {data.citations && (
        <div className="mt-6">
          <h4 className="text-xs font-bold uppercase tracking-widest text-ink mb-2">Where those answers come from</h4>
          <p className="text-[11px] text-ink-light font-medium mb-2">
            {data.citations.total_citations} source{data.citations.total_citations !== 1 ? "s" : ""} cited across the workshop answers.
          </p>
          {!data.abbvie_cited && data.citations.total_citations > 0 && (
            <p className="-mt-1 mb-2 text-[11px] font-semibold text-red-600">
              No AbbVie-owned sources were cited. AI is not surfacing our content here.
            </p>
          )}
          <ShareOfVoiceBar c={data.citations} />
          {data.citations.top_competitors.length > 0 && (
            <p className="mt-3 text-xs text-ink-light">
              <span className="font-bold text-ink">Top competitor sources:</span>{" "}
              {data.citations.top_competitors.map((t) => `${t.publisher_name || t.authority_domain} (${t.citation_count})`).join(", ")}
            </p>
          )}
          {data.citations.top_competitor_pages.length > 0 && (
            <div className="mt-2">
              <span className="text-xs font-bold text-ink">Most-cited competitor pages:</span>
              <ul className="mt-1 space-y-1">
                {data.citations.top_competitor_pages.map((p) => (
                  <li key={p.url} className="text-xs">
                    <a href={p.url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-brand-light hover:text-brand break-all">
                      <ExternalLink size={11} className="shrink-0" /> {p.url}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

/* ================================================================== */
/*  MAIN PAGE                                                          */
/* ================================================================== */
export default function Digests() {
  const [profiles, setProfiles] = useState<DigestProfile[]>([]);
  const [runs, setRuns] = useState<DigestRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState<number | null>(null);
  const [ses, setSes] = useState<SesStatus | null>(null);
  const [toast, setToast] = useState<{ kind: "ok" | "warn"; text: string } | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    return Promise.all([
      api.digestProfiles().then(setProfiles).catch(() => setProfiles([])),
      api.digestRuns().then(setRuns).catch(() => setRuns([])),
    ]).finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
    api.sesCheck().then(setSes).catch(() => setSes(null));
  }, [load]);

  const flash = (kind: "ok" | "warn", text: string) => {
    setToast({ kind, text });
    window.setTimeout(() => setToast(null), 8000);
  };

  const runNow = async (id: number) => {
    setRunning(id);
    try {
      const run = await api.runDigest(id);
      await load();
      const emailNote = run.delivery_detail?.email;
      if (run.delivered_email) {
        flash("ok", `Digest generated with ${run.findings_count} finding(s). ${emailNote ?? "Emailed."}`);
      } else if (emailNote) {
        flash("warn", `Digest generated (${run.findings_count} finding(s)), but email did not send: ${emailNote}`);
      } else {
        flash("ok", `Digest generated with ${run.findings_count} finding(s) and stored in-app.`);
      }
    } catch {
      flash("warn", "Could not generate the digest. Check the server logs and try again.");
    } finally {
      setRunning(null);
    }
  };

  const remove = async (id: number, role: string) => {
    if (!window.confirm(`Delete the "${role}" digest profile and all its generated digests? This cannot be undone.`)) {
      return;
    }
    try {
      await api.deleteDigestProfile(id);
      await load();
      flash("ok", `Deleted the "${role}" profile.`);
    } catch {
      flash("warn", `Could not delete the "${role}" profile. Please try again.`);
    }
  };

  const toggleEnabled = async (p: DigestProfile) => {
    await api.updateDigestProfile(p.id, { enabled: !p.enabled });
    await load();
  };

  const emailNotReady = ses && ses.enabled && !!ses.reason;
  const senderOk = !!ses && (ses.sender_verified === true || ses.sender_domain_verified === true);

  return (
    <div className="space-y-8">
      <PageHeader
        title="Stakeholder Digests"
        subtitle="Set up a profile for each team, choose which alerts they care about, and pick a schedule. The system then emails each team a short summary of their top findings automatically."
      />

      {toast && (
        <div
          className={`flex items-start gap-2 rounded-xl border px-4 py-3 text-sm font-medium ${
            toast.kind === "ok"
              ? "border-teal-200 bg-teal-50 text-teal-800"
              : "border-amber-200 bg-amber-50 text-amber-800"
          }`}
        >
          {toast.kind === "ok" ? <CheckCircle2 size={16} className="mt-0.5 shrink-0" /> : <AlertTriangle size={16} className="mt-0.5 shrink-0" />}
          <span>{toast.text}</span>
        </div>
      )}

      {emailNotReady && (
        <div className="flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm font-medium text-amber-800">
          <AlertTriangle size={16} className="mt-0.5 shrink-0" />
          <span>
            <strong>Email delivery isn't ready yet.</strong> {ses?.reason} Digests are still generated and stored in-app in the meantime.
          </span>
        </div>
      )}
      {ses && ses.enabled && !ses.reason && senderOk && ses.mode === "production" && (
        <div className="flex items-center gap-2 rounded-xl border border-teal-200 bg-teal-50 px-4 py-2.5 text-xs font-semibold text-teal-800">
          <ShieldCheck size={14} /> Email delivery is live — sending as {ses.sender}
          {ses.sender_domain_verified ? " (domain verified)" : ""}. Any recipient can receive digests; no per-address verification needed.
        </div>
      )}
      {ses && ses.enabled && !ses.reason && senderOk && ses.mode !== "production" && (
        <div className="flex items-center gap-2 rounded-xl border border-teal-200 bg-teal-50 px-4 py-2.5 text-xs font-semibold text-teal-800">
          <ShieldCheck size={14} /> Email delivery is ready (sender {ses.sender} verified
          {ses.mode === "sandbox" ? ", sandbox mode — recipients must be verified" : ""}).
        </div>
      )}

      <WorkshopInsightsPanel />

      <CreateProfileForm onCreated={load} />

      <Card title="Digest Profiles">
        {loading ? (
          <div className="flex justify-center py-12"><Spinner /></div>
        ) : profiles.length === 0 ? (
          <EmptyState message="No digest profiles yet. Create one above (or seed PV / Brand / Medical Affairs)." />
        ) : (
          <div className="space-y-3">
            {profiles.map((p) => (
              <div key={p.id} className="rounded-xl border border-line p-4">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-bold text-ink">{p.role}</span>
                      <span className={`px-2 py-0.5 rounded-full text-[11px] font-bold ${p.enabled ? "bg-teal-100 text-teal-700" : "bg-slate-100 text-ink-light"}`}>
                        {p.enabled ? "Active" : "Paused"}
                      </span>
                      <span className="inline-flex items-center gap-1 text-xs text-ink-light font-semibold">
                        <CalendarClock size={12} /> {scheduleSummary(p.cron)}
                      </span>
                    </div>
                    {p.enabled && p.next_run_at && (
                      <p className="mt-1 text-[11px] text-ink-muted font-medium">
                        Next automatic send: {new Date(p.next_run_at).toLocaleString()}
                      </p>
                    )}
                    {p.description && <p className="mt-1 text-sm text-ink-light font-medium">{p.description}</p>}
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {(p.delivery_methods ?? []).map((m) => (
                        <span key={m} className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-brand-surface text-brand-dark text-[11px] font-bold">
                          {m === "email" ? <Mail size={11} /> : m === "webhook" ? <Webhook size={11} /> : <FileText size={11} />}
                          {m}
                        </span>
                      ))}
                      {(p.rules ?? []).flatMap((r) => r.alert_categories ?? []).map((c, i) => (
                        <span key={`c${i}`} className="px-2 py-0.5 rounded-full bg-red-50 text-red-600 text-[11px] font-bold">{c.replace(/_/g, " ")}</span>
                      ))}
                      {(p.rules ?? []).flatMap((r) => r.domains ?? []).map((d, i) => (
                        <span key={`d${i}`} className="px-2 py-0.5 rounded-full bg-slate-100 text-ink-light text-[11px] font-bold">{d}</span>
                      ))}
                    </div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <button
                      onClick={() => runNow(p.id)}
                      disabled={running === p.id}
                      className="flex items-center gap-1.5 px-3 py-1.5 bg-brand text-white rounded-lg text-xs font-bold hover:bg-brand-dark disabled:opacity-40 transition-colors"
                    >
                      {running === p.id ? <Spinner size={14} /> : <Play size={13} />} Run Now
                    </button>
                    <button onClick={() => toggleEnabled(p)} className="px-3 py-1.5 border border-line rounded-lg text-xs font-bold text-ink-light hover:border-brand-light transition-colors">
                      {p.enabled ? "Pause" : "Enable"}
                    </button>
                    <button onClick={() => remove(p.id, p.role)} className="p-1.5 text-ink-muted hover:text-red-500 transition-colors" title="Delete profile">
                      <Trash2 size={15} />
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title="Past Digests">
        {runs.length === 0 ? (
          <EmptyState message="No digests generated yet. Use Run Now on a profile to generate one." icon={<Send size={28} />} />
        ) : (
          <div className="space-y-3">
            {runs.map((r, i) => (
              <motion.div
                key={r.id}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: Math.min(i * 0.03, 0.4) }}
                className="rounded-xl border border-line p-4"
              >
                <div className="flex items-center justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-bold text-ink">{r.role}</span>
                      <span className="text-xs text-ink-muted">{new Date(r.generated_at).toLocaleString()}</span>
                      <span className="px-2 py-0.5 rounded-full bg-brand-surface text-brand-dark text-[11px] font-bold">
                        {r.findings_count} finding{r.findings_count !== 1 ? "s" : ""}
                      </span>
                      {r.delivered_email ? (
                        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-teal-100 text-teal-700 text-[11px] font-bold">
                          <CheckCircle2 size={11} /> Emailed
                        </span>
                      ) : r.delivery_detail?.email ? (
                        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-amber-100 text-amber-700 text-[11px] font-bold" title={r.delivery_detail.email}>
                          <AlertTriangle size={11} /> Not emailed
                        </span>
                      ) : null}
                    </div>
                    {!r.delivered_email && r.delivery_detail?.email && (
                      <p className="mt-1 text-[11px] text-amber-700 font-medium">{r.delivery_detail.email}</p>
                    )}
                    {r.summary && <p className="mt-1.5 text-sm text-ink-light font-medium line-clamp-3">{r.summary}</p>}
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <a
                      href={api.digestHtmlUrl(r.id)}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="flex items-center gap-1.5 px-3 py-1.5 border border-line rounded-lg text-xs font-bold text-ink-light hover:border-brand-light transition-colors"
                    >
                      <FileText size={13} /> HTML
                    </a>
                    <a
                      href={api.digestPdfUrl(r.id)}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="flex items-center gap-1.5 px-3 py-1.5 border border-line rounded-lg text-xs font-bold text-ink-light hover:border-brand-light transition-colors"
                    >
                      <FileText size={13} /> PDF
                    </a>
                  </div>
                </div>
              </motion.div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
