import { useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ExternalLink,
  FlaskConical,
  X,
} from "lucide-react";

import {
  api,
  type EvidenceStudyDetail,
  type EvidenceStudyRow,
  type StudySourceCheck,
} from "../api/client";
import {
  Card,
  EmptyState,
  InfoTooltip,
  PageHeader,
  Select,
  Spinner,
} from "../components/ui";

const VERIFICATION_CLS: Record<string, string> = {
  EXTRACTED: "bg-slate-100 text-ink-light",
  MAPPED: "bg-sky-100 text-sky-800",
  VERIFIED: "bg-emerald-100 text-emerald-800",
  REJECTED: "bg-red-100 text-red-800",
};

/**
 * Study browser.
 *
 * Two things it refuses to smooth over:
 *
 * - **Canonical endpoint counts per study.** A trial can post dozens of results and
 *   contribute nothing, because a row without a canonical id is invisible to every network.
 *   The count says so rather than implying depth.
 * - **Mismatch flags travel with every row.** Showing a clean number for a result flagged
 *   `EVENTS_DERIVED_FROM_PERCENTAGE` launders a caveat the extraction was careful to record.
 */
export default function EvidenceStudies() {
  const [rows, setRows] = useState<EvidenceStudyRow[]>([]);
  const [states, setStates] = useState<string[]>([]);
  const [indications, setIndications] = useState<string[]>([]);
  const [indication, setIndication] = useState("");
  const [status, setStatus] = useState("");
  const [treatment, setTreatment] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    api
      .evidenceStudies({
        indication: indication || undefined,
        verification_status: status || undefined,
        treatment: treatment.trim() || undefined,
      })
      .then((res) => {
        setRows(res.studies);
        setStates(res.verification_states);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [indication, status, treatment]);

  useEffect(() => {
    api
      .evidenceStudies()
      .then((res) =>
        setIndications([...new Set(res.studies.map((s) => s.indication))].sort()),
      )
      .catch(() => setIndications([]));
  }, []);

  return (
    <div>
      <PageHeader
        title="Ingested Studies"
        subtitle="Trials, their randomised arms and every endpoint result, with extraction flags intact."
      />

      <div className="mb-5 flex flex-wrap items-end gap-3">
        <Select
          label="Indication"
          value={indication}
          options={["", ...indications]}
          optionLabels={{ "": "All indications" }}
          onChange={setIndication}
        />
        <Select
          label="Verification"
          value={status}
          options={["", ...states]}
          optionLabels={{ "": "Any status" }}
          onChange={setStatus}
        />
        <label className="flex flex-col gap-1">
          <span className="text-xs font-bold uppercase tracking-wide text-ink-light">
            Has an arm on
          </span>
          <input
            value={treatment}
            onChange={(e) => setTreatment(e.target.value)}
            placeholder="e.g. Rinvoq"
            className="rounded-lg border border-line bg-canvas-card px-3 py-2 text-sm text-ink outline-none focus:border-brand-light"
          />
        </label>
        <span className="pb-2 text-xs text-ink-light">
          {rows.length} stud{rows.length === 1 ? "y" : "ies"}
        </span>
      </div>

      {error && (
        <div className="mb-4 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800">
          {error}
        </div>
      )}

      {loading ? (
        <div className="flex justify-center py-16">
          <Spinner size={26} />
        </div>
      ) : !rows.length ? (
        <EmptyState icon={<FlaskConical size={30} />} message="No studies match." />
      ) : (
        <Card>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-ink-light">
                  <th className="pb-2 pr-4 font-bold">Study</th>
                  <th className="pb-2 pr-4 font-bold">Indication</th>
                  <th className="pb-2 pr-4 font-bold">Arms</th>
                  <th className="pb-2 pr-4 font-bold">
                    <span className="flex items-center gap-1">
                      Usable results
                      <InfoTooltip content="Outcome rows that resolved to a canonical endpoint id. Only these can join a network." />
                    </span>
                  </th>
                  <th className="pb-2 font-bold">Verification</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((s) => (
                  <tr
                    key={s.study_id}
                    onClick={() => setSelected(s.study_id)}
                    className="cursor-pointer border-b border-line/60 transition-colors last:border-0 hover:bg-brand-surface/40"
                  >
                    <td className="py-3 pr-4">
                      <div className="font-semibold text-ink">
                        {s.acronym || s.registry_id || s.study_id}
                      </div>
                      <div className="max-w-md truncate text-[11px] text-ink-light">
                        {s.title || "untitled"}
                      </div>
                    </td>
                    <td className="py-3 pr-4 text-xs text-ink">
                      {s.indication}
                      <div className="text-[11px] text-ink-light">
                        {s.phase || "phase unknown"} · {s.treatment_phase}
                      </div>
                    </td>
                    <td className="py-3 pr-4 text-xs text-ink">
                      {s.arm_count}
                      <div className="max-w-[14rem] truncate text-[11px] text-ink-light">
                        {s.treatments.join(", ")}
                      </div>
                    </td>
                    <td className="py-3 pr-4 text-xs">
                      <span
                        className={
                          s.canonical_outcome_count
                            ? "font-bold text-ink"
                            : "font-bold text-amber-700"
                        }
                      >
                        {s.canonical_outcome_count}
                      </span>
                      <span className="text-ink-light"> / {s.outcome_count}</span>
                      {!s.canonical_outcome_count && s.outcome_count > 0 && (
                        <div className="text-[11px] text-amber-700">
                          no network can use these
                        </div>
                      )}
                    </td>
                    <td className="py-3">
                      <span
                        className={`rounded-full px-2.5 py-1 text-[10px] font-bold ${VERIFICATION_CLS[s.verification_status] || "bg-slate-100"}`}
                      >
                        {s.verification_status.toLowerCase()}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {selected && (
        <StudyDrawer studyId={selected} onClose={() => setSelected(null)} />
      )}
    </div>
  );
}

function StudyDrawer({
  studyId,
  onClose,
}: {
  studyId: string;
  onClose: () => void;
}) {
  const [data, setData] = useState<EvidenceStudyDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showUnmapped, setShowUnmapped] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    setData(null);
    api
      .evidenceStudy(studyId)
      .then(setData)
      .catch((e) => setError(String(e)));
  }, [studyId, reloadKey]);

  const outcomes = (data?.outcomes || []).filter(
    (o) => showUnmapped || o.canonical_outcome_id,
  );

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/30" onClick={onClose}>
      <div
        className="h-full w-full max-w-3xl overflow-y-auto bg-canvas p-6 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-5 flex items-start justify-between gap-4">
          <div>
            <h2 className="text-lg font-bold text-ink">
              {data?.acronym || data?.registry_id || studyId}
            </h2>
            <p className="text-xs text-ink-light">{data?.title}</p>
          </div>
          <button
            onClick={onClose}
            className="rounded-lg p-1.5 text-ink-light transition-colors hover:bg-slate-100"
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>

        {error && (
          <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800">
            {error}
          </div>
        )}
        {!data && !error && (
          <div className="flex justify-center py-16">
            <Spinner size={24} />
          </div>
        )}

        {data && (
          <div className="space-y-5">
            <CurationPanel
              studyId={studyId}
              onVerified={() => setReloadKey((k) => k + 1)}
            />

            <Card title="Provenance and standing">
              <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
                <Row label="Registry" value={data.registry_id || "—"} />
                <Row label="Sponsor" value={data.sponsor || "—"} />
                <Row label="Phase" value={data.phase || "—"} />
                <Row label="Treatment phase" value={data.treatment_phase} />
                <Row label="Randomised" value={data.is_randomised ? "yes" : "no"} />
                <Row label="Enrollment" value={data.enrollment ?? "—"} />
                <Row
                  label="Results posted"
                  value={data.results_first_posted || "not posted"}
                />
                <Row label="Risk of bias" value={data.risk_of_bias || "not assessed"} />
                <Row
                  label="Verification"
                  value={`${data.verification_status}${data.verified_by ? ` · ${data.verified_by}` : ""}`}
                />
                <Row label="Stratum" value={data.population_stratum || "unstated"} />
              </div>
              <div className="mt-3 border-t border-line pt-3 text-xs leading-relaxed text-ink-light">
                <p>
                  <strong>Citable:</strong>{" "}
                  {data.source_is_citable ? "yes" : "no"} ·{" "}
                  <strong>Approved for external use:</strong>{" "}
                  {data.claim_is_approved_for_external_use ? "yes" : "no"}
                </p>
                <p className="mt-1">
                  Two independent properties. A registry record is citable the moment it
                  exists; our extraction of it is not approved for external use until
                  review says so.
                </p>
              </div>
            </Card>

            <Card title={`Arms (${data.arms.length})`}>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-ink-light">
                      <th className="pb-2 pr-3 font-bold">Treatment</th>
                      <th className="pb-2 pr-3 font-bold">Label as printed</th>
                      <th className="pb-2 pr-3 font-bold">Dose</th>
                      <th className="pb-2 pr-3 font-bold">Route</th>
                      <th className="pb-2 font-bold">N</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.arms.map((a) => (
                      <tr key={a.arm_id} className="border-b border-line/60 last:border-0">
                        <td className="py-2 pr-3 font-semibold text-ink">
                          {a.treatment}
                          {a.is_placebo && (
                            <span className="ml-1.5 rounded bg-slate-100 px-1.5 py-0.5 text-[9px] font-bold text-ink-light">
                              placebo
                            </span>
                          )}
                        </td>
                        <td className="max-w-xs truncate py-2 pr-3 text-xs text-ink-light">
                          {a.label || "—"}
                        </td>
                        <td className="py-2 pr-3 text-xs text-ink">
                          {a.dose_value != null
                            ? `${a.dose_value}${a.dose_unit ? ` ${a.dose_unit}` : ""}${a.dose_frequency ? ` ${a.dose_frequency}` : ""}`
                            : "—"}
                        </td>
                        <td className="py-2 pr-3 text-xs text-ink">
                          {a.administration_route || "—"}
                        </td>
                        <td className="py-2 text-xs tabular-nums text-ink">
                          {a.sample_size ?? "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="mt-3 text-xs leading-relaxed text-ink-light">
                Dose is kept structured because <code>dose_policy</code> decides whether
                doses may be pooled, and silently pooling them is among the most common
                NMA criticisms.
              </p>
            </Card>

            <Card
              title={
                <span className="flex items-center justify-between gap-3">
                  <span>Endpoint results ({data.outcomes.length})</span>
                  <label className="flex items-center gap-1.5 text-xs font-normal text-ink-light">
                    <input
                      type="checkbox"
                      checked={showUnmapped}
                      onChange={(e) => setShowUnmapped(e.target.checked)}
                      className="accent-brand-light"
                    />
                    show unmapped
                  </label>
                </span>
              }
            >
              {!outcomes.length ? (
                <p className="text-sm text-ink-light">Nothing to show.</p>
              ) : (
                <div className="space-y-2">
                  {outcomes.map((o) => (
                    <div
                      key={o.result_id}
                      className="rounded-lg border border-line bg-canvas-card p-3"
                    >
                      <div className="flex items-baseline justify-between gap-3">
                        <span className="text-sm font-semibold text-ink">
                          {o.endpoint}
                          {o.timepoint_week != null && (
                            <span className="ml-1.5 text-xs font-normal text-ink-light">
                              week {o.timepoint_week}
                            </span>
                          )}
                        </span>
                        <span className="shrink-0 text-xs text-ink-light">
                          {o.arm_treatment || "unattached"}
                        </span>
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink">
                        {o.canonical_outcome_id ? (
                          <span className="rounded bg-emerald-50 px-1.5 py-0.5 font-mono text-[10px] font-bold text-emerald-800">
                            {o.canonical_outcome_id}
                          </span>
                        ) : (
                          <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] font-bold text-amber-800">
                            no canonical endpoint — unusable by any network
                          </span>
                        )}
                        {o.events != null && o.sample_size != null && (
                          <span className="tabular-nums">
                            {o.events}/{o.sample_size}
                          </span>
                        )}
                        {o.mean != null && (
                          <span className="tabular-nums">
                            mean {o.mean}
                            {o.standard_deviation != null
                              ? ` (SD ${o.standard_deviation})`
                              : ""}
                          </span>
                        )}
                        {o.effect_estimate != null && (
                          <span className="tabular-nums">
                            {o.effect_measure || "effect"} {o.effect_estimate}
                            {o.ci_lower != null && o.ci_upper != null
                              ? ` (${o.ci_lower}–${o.ci_upper})`
                              : ""}
                          </span>
                        )}
                        {o.is_safety_outcome && (
                          <span className="rounded bg-rose-50 px-1.5 py-0.5 text-[10px] font-bold text-rose-800">
                            safety
                          </span>
                        )}
                      </div>
                      {!!o.mismatch_flags.length && (
                        <div className="mt-1.5 flex flex-wrap gap-1">
                          {o.mismatch_flags.map((f) => (
                            <span
                              key={f}
                              className="rounded bg-amber-50 px-1.5 py-0.5 font-mono text-[10px] text-amber-800"
                            >
                              {f}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </Card>
          </div>
        )}
      </div>
    </div>
  );
}

function cell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  return String(value);
}

/**
 * The curator's step, and the narrowest of the three gates.
 *
 * Evidence gathering skips an unverified study **even in EXPLORATORY mode**, so this — not
 * protocol approval, not ratification — is what stands between a built network and any
 * number at all. It is also the one gate a careful person can clear today: it asserts the
 * extraction matches its source, which is a data-accuracy judgement, not a clinical one.
 *
 * The panel deliberately does not offer a green tick as an answer. Reproducing from the
 * retained document proves the stored rows are not *separately* stale; it cannot prove the
 * parser read them correctly, because a misreading reproduces perfectly. The source link
 * and the parser's own flags are what send a curator to the registry record itself.
 */
function CurationPanel({
  studyId,
  onVerified,
}: {
  studyId: string;
  onVerified: () => void;
}) {
  const [check, setCheck] = useState<StudySourceCheck | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [who, setWho] = useState("");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    setCheck(null);
    setError(null);
    setSaveError(null);
    api
      .studySourceCheck(studyId)
      .then(setCheck)
      .catch((e) => setError(String(e)));
  }, [studyId]);

  async function confirm() {
    setSaving(true);
    setSaveError(null);
    try {
      await api.recordCuratorCheck(studyId, {
        verified_by: who.trim(),
        note: note.trim() || undefined,
      });
      setCheck(await api.studySourceCheck(studyId));
      onVerified();
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  if (error) {
    return (
      <Card title="Source check">
        <p className="text-sm text-red-800">{error}</p>
      </Card>
    );
  }
  if (!check) {
    return (
      <Card title="Source check">
        <div className="flex justify-center py-6">
          <Spinner size={20} />
        </div>
      </Card>
    );
  }

  const verified = check.verification_status === "VERIFIED";
  const flags = Object.entries(check.flag_counts);

  return (
    <Card title="Source check and verification">
      {check.blocked_reason ? (
        <div className="flex gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          <AlertTriangle size={16} className="mt-0.5 shrink-0" />
          <div>
            <p className="font-semibold">Cannot be checked against a stored document.</p>
            <p className="mt-0.5 text-xs">{check.blocked_reason}</p>
          </div>
        </div>
      ) : check.reproducible ? (
        <div className="flex gap-2 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-900">
          <CheckCircle2 size={16} className="mt-0.5 shrink-0" />
          <div>
            <p className="font-semibold">
              Reproduces exactly from the retained source document.
            </p>
            <p className="mt-0.5 text-xs">
              {check.counts.arms?.stored} arms and {check.counts.outcomes?.stored} result
              rows re-derive identically. This proves the stored rows are not stale — it
              does <strong>not</strong> prove they are correct, because a misreading
              reproduces just as perfectly. Check the numbers that matter against the
              registry record before confirming.
            </p>
          </div>
        </div>
      ) : (
        <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-900">
          <div className="flex gap-2">
            <AlertTriangle size={16} className="mt-0.5 shrink-0" />
            <div>
              <p className="font-semibold">
                {check.difference_count} difference
                {check.difference_count === 1 ? "" : "s"} from the source.
              </p>
              <p className="mt-0.5 text-xs">
                The stored rows no longer match what today's parser reads out of the same
                bytes, so the extraction is stale. Re-parse this study before verifying —
                a VERIFIED row is skipped by re-ingestion, which would put the correction
                out of reach.
              </p>
            </div>
          </div>
          <div className="mt-3 max-h-52 overflow-y-auto rounded border border-red-200 bg-white">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-red-50">
                <tr className="text-left text-[10px] uppercase tracking-wide text-red-900">
                  <th className="p-1.5 font-bold">Row</th>
                  <th className="p-1.5 font-bold">Field</th>
                  <th className="p-1.5 font-bold">Stored</th>
                  <th className="p-1.5 font-bold">Source</th>
                </tr>
              </thead>
              <tbody>
                {check.differences.map((d, i) => (
                  <tr key={`${d.id}-${d.field}-${i}`} className="border-t border-red-100">
                    <td className="p-1.5 font-mono text-[10px] text-ink-light">
                      {d.kind} · {d.id}
                    </td>
                    <td className="p-1.5 font-semibold text-ink">{d.field}</td>
                    <td className="p-1.5 tabular-nums text-ink">{cell(d.stored)}</td>
                    <td className="p-1.5 tabular-nums text-ink">{cell(d.source)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {check.differences_omitted > 0 && (
            <p className="mt-1.5 text-xs text-red-900">
              and {check.differences_omitted} more not shown.
            </p>
          )}
        </div>
      )}

      {check.source && (
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-line pt-3 text-xs text-ink-light">
          {check.source.url ? (
            <a
              href={check.source.url}
              target="_blank"
              rel="noreferrer"
              className="flex items-center gap-1 font-semibold text-brand-light hover:underline"
            >
              {check.source.source_identifier}
              <ExternalLink size={12} />
            </a>
          ) : (
            <span className="font-semibold text-ink">
              {check.source.source_identifier}
            </span>
          )}
          <span>retrieved {check.source.retrieved_at?.slice(0, 10) || "—"}</span>
          <span>{check.source.license_class}</span>
        </div>
      )}

      {!!flags.length && (
        <div className="mt-3 border-t border-line pt-3">
          <p className="text-xs font-bold uppercase tracking-wide text-ink-light">
            What the parser was unsure about
          </p>
          <div className="mt-1.5 flex flex-wrap gap-1">
            {flags.map(([flag, count]) => (
              <span
                key={flag}
                className="rounded bg-amber-50 px-1.5 py-0.5 font-mono text-[10px] text-amber-800"
              >
                {flag} · {count}
              </span>
            ))}
          </div>
          <p className="mt-1.5 text-xs leading-relaxed text-ink-light">
            These are the rows to check by eye. `EVENTS_DERIVED_FROM_PERCENTAGE` in
            particular means the count was back-derived from a rounded percentage rather
            than posted by the registry.
          </p>
        </div>
      )}

      <div className="mt-4 border-t border-line pt-4">
        {verified ? (
          <p className="text-sm text-ink">
            <span className="rounded-full bg-emerald-100 px-2.5 py-1 text-[10px] font-bold text-emerald-800">
              verified
            </span>{" "}
            by <strong>{check.verified_by}</strong>
            {check.verified_at ? ` on ${check.verified_at.slice(0, 10)}` : ""}.
          </p>
        ) : (
          <>
            <p className="mb-2 text-xs leading-relaxed text-ink-light">
              Confirming records that <strong>a person checked this extraction against
              its source</strong>. That is a data-accuracy check, not a clinical or
              statistical review, and the name is recorded but{" "}
              <strong>not authenticated</strong>.
            </p>
            <div className="flex flex-wrap items-end gap-2">
              <label className="flex flex-1 flex-col gap-1">
                <span className="text-xs font-bold uppercase tracking-wide text-ink-light">
                  Checked by
                </span>
                <input
                  value={who}
                  onChange={(e) => setWho(e.target.value)}
                  placeholder="your name"
                  className="rounded-lg border border-line bg-canvas-card px-3 py-2 text-sm text-ink outline-none focus:border-brand-light"
                />
              </label>
              <label className="flex flex-[2] flex-col gap-1">
                <span className="text-xs font-bold uppercase tracking-wide text-ink-light">
                  What you checked
                </span>
                <input
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  placeholder="e.g. arms and ACR50 counts against the registry record"
                  className="rounded-lg border border-line bg-canvas-card px-3 py-2 text-sm text-ink outline-none focus:border-brand-light"
                />
              </label>
              <button
                onClick={confirm}
                disabled={saving || !who.trim() || !check.reproducible}
                className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-brand-light disabled:cursor-not-allowed disabled:opacity-40"
              >
                {saving ? "Recording…" : "Confirm"}
              </button>
            </div>
            {!check.reproducible && (
              <p className="mt-2 text-xs text-ink-light">
                Confirmation is disabled until this study reproduces from its source.
              </p>
            )}
            {saveError && (
              <p className="mt-2 rounded-lg border border-red-200 bg-red-50 p-2 text-xs text-red-800">
                {saveError}
              </p>
            )}
          </>
        )}
      </div>
    </Card>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-xs font-semibold text-ink-light">{label}</span>
      <span className="text-right text-sm text-ink">{value}</span>
    </div>
  );
}
