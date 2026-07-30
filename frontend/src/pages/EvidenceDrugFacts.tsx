import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  Pill,
  ShieldAlert,
} from "lucide-react";

import {
  api,
  type DrugFactQueue,
  type DrugFactQueueRow,
  type DrugFactSourceCheck,
} from "../api/client";
import {
  AnimatedCard,
  Card,
  EmptyState,
  PageHeader,
  Spinner,
  Stat,
} from "../components/ui";

/**
 * Drug Evidence Profile — regulatory labels, and the curator gate three phases wait on.
 *
 * Nothing rendered `/evidence/drug-facts` and nothing could verify a label, so
 * `DrugFact.verification_status` never left EXTRACTED and every consumer that filters on
 * VERIFIED returned nothing. That reads as "no findings" rather than "not wired", which is
 * why it went unnoticed: Phase 7's approval and safety questions, Phase 8's approval,
 * safety-warning and mechanism claims, and Phase 9's misinformation-risk implication were
 * all silently empty.
 *
 * The page leads with **what verifying a row would unblock**, and separates that from what
 * verifying cannot fix. A label whose indications prose was never structured cannot answer
 * an approval claim however carefully it is checked — that is pipeline work, not curator
 * backlog, and filing it as backlog would send someone to spend a morning on a row that
 * was never going to answer the question they were asked about.
 */
export default function EvidenceDrugFactsPage() {
  const [queue, setQueue] = useState<DrugFactQueue | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    api
      .drugFactQueue()
      .then(setQueue)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(load, [load]);

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <Spinner size={28} />
      </div>
    );
  }
  if (error) {
    return (
      <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800">
        {error}
      </div>
    );
  }
  if (!queue || queue.total === 0) {
    return (
      <div>
        <PageHeader
          title="Drug Evidence"
          subtitle="Label-derived facts. Independent of the NMA stack — these stay valuable where a network is disconnected."
        />
        <EmptyState message="No drug facts ingested yet. Run scripts/ingest_drug_facts.py --commit." />
      </div>
    );
  }

  const approvalBlocked = queue.approval_blocked.length;

  return (
    <div>
      <PageHeader
        title="Drug Evidence"
        subtitle="Current label versions, their extraction status, and what each one can answer."
      />

      <AnimatedCard className="mb-6">
        <div className="rounded-2xl border border-line bg-brand-surface p-5">
          <div className="mb-1 text-[11px] font-bold uppercase tracking-wide text-brand-dark">
            The big picture
          </div>
          <p className="text-sm leading-relaxed text-ink">
            <strong>{queue.blocking}</strong> of <strong>{queue.total}</strong> labels are
            unverified, and <strong>{queue.worth_verifying}</strong> of those would change
            what the platform can answer. Approval, safety and mechanism claims read
            verified labels only.
          </p>
        </div>
      </AnimatedCard>

      {approvalBlocked > 0 && (
        <div
          role="alert"
          className="mb-6 flex items-start gap-3 rounded-xl border-2 border-amber-300 bg-amber-50 p-4"
        >
          <AlertTriangle size={18} className="mt-0.5 shrink-0 text-amber-600" strokeWidth={2.2} />
          <div className="text-sm text-amber-900">
            <p className="font-bold">
              {approvalBlocked} label{approvalBlocked === 1 ? "" : "s"} cannot answer an
              approval claim — and curation will not change that.
            </p>
            <p className="mt-1 leading-relaxed">
              The adapter records <code>INDICATIONS_TEXT_NOT_STRUCTURED</code> rather than
              half-parsing the label's indications prose, because a partially parsed
              regulatory list is worse than an absent one. Structuring it is extraction
              pipeline work. This is not curator backlog.
            </p>
          </div>
        </div>
      )}

      <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-3">
        <Stat label="Labels on record" value={queue.total} icon={<Pill size={16} />} />
        <Stat
          label="Worth verifying"
          value={queue.worth_verifying}
          sub={`of ${queue.blocking} unverified`}
          tooltip="Only rows where verifying would change what a question or claim can be answered from."
        />
        <Stat
          label="Approval-blocked"
          value={approvalBlocked}
          sub="needs structured indications"
        />
      </div>

      <div className="space-y-3">
        {queue.facts.map((fact) => (
          <FactRow key={fact.fact_id} fact={fact} onChanged={load} />
        ))}
      </div>
    </div>
  );
}

function FactRow({ fact, onChanged }: { fact: DrugFactQueueRow; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [check, setCheck] = useState<DrugFactSourceCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const verified = fact.verification_status === "VERIFIED";

  const runCheck = () => {
    setChecking(true);
    setMessage(null);
    api
      .drugFactSourceCheck(fact.fact_id)
      .then(setCheck)
      .catch((e) => setMessage(String(e)))
      .finally(() => setChecking(false));
  };

  const confirm = async () => {
    setBusy(true);
    setMessage(null);
    try {
      await api.recordDrugFactCheck(fact.fact_id, { verified_by: name.trim() });
      onChanged();
    } catch (e: any) {
      setMessage(e?.message ?? String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-start gap-3 text-left"
      >
        {open ? (
          <ChevronDown size={16} className="mt-1 shrink-0 text-ink-light" />
        ) : (
          <ChevronRight size={16} className="mt-1 shrink-0 text-ink-light" />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-bold text-ink">{fact.brand}</span>
            {fact.generic && (
              <span className="text-xs italic text-ink-light">{fact.generic}</span>
            )}
            <StatusBadge status={fact.verification_status} />
            {fact.has_boxed_warning && (
              <span className="inline-flex items-center gap-1 rounded-full border border-red-200 bg-red-50 px-2 py-0.5 text-[11px] font-bold text-red-800">
                <ShieldAlert size={11} strokeWidth={2.6} /> Boxed warning
              </span>
            )}
            {fact.superseded_by && (
              <span className="rounded-full border border-slate-300 bg-slate-100 px-2 py-0.5 text-[11px] font-bold text-slate-600">
                superseded
              </span>
            )}
          </div>
          <div className="mt-1 flex flex-wrap gap-3 text-xs text-ink-light">
            <span>label {fact.label_updated_at ?? "undated"}</span>
            <span>v{fact.version}</span>
            {fact.verified_by && <span>checked by {fact.verified_by}</span>}
          </div>
        </div>
        <AnswerPills fact={fact} />
      </button>

      {open && (
        <div className="mt-4 space-y-4 border-t border-line pt-4">
          {fact.blockers.length > 0 && (
            <div>
              <div className="mb-1.5 text-xs font-bold uppercase tracking-wide text-ink-light">
                What this label cannot answer
              </div>
              <ul className="space-y-1">
                {fact.blockers.map((b) => (
                  <li key={b} className="text-xs leading-relaxed text-ink-light">
                    · {b}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {fact.mismatch_flags.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {fact.mismatch_flags.map((f) => (
                <span
                  key={f}
                  className="rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 font-mono text-[11px] text-amber-900"
                >
                  {f}
                </span>
              ))}
            </div>
          )}

          {fact.prescribing_information && (
            <a
              href={fact.prescribing_information}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 text-xs font-semibold text-brand-dark hover:underline"
            >
              Open the prescribing information
              <ExternalLink size={12} strokeWidth={2.4} />
            </a>
          )}

          {!verified && !fact.superseded_by && (
            <div className="rounded-xl border border-line bg-canvas p-3">
              <div className="mb-2 text-xs font-bold uppercase tracking-wide text-ink">
                Curator check
              </div>
              {!check ? (
                <button
                  onClick={runCheck}
                  disabled={checking}
                  className="rounded-lg border border-line bg-canvas-card px-3 py-1.5 text-xs font-semibold text-brand-dark disabled:opacity-50"
                >
                  {checking ? "Re-deriving…" : "Re-derive from the retained label"}
                </button>
              ) : (
                <>
                  <div
                    className={`mb-2 rounded-lg border p-2.5 text-xs leading-relaxed ${
                      check.reproducible
                        ? "border-emerald-200 bg-emerald-50 text-emerald-900"
                        : "border-amber-300 bg-amber-50 text-amber-900"
                    }`}
                  >
                    {check.blocked_reason ? (
                      check.blocked_reason
                    ) : check.reproducible ? (
                      <>
                        <span className="font-bold">Reproduces cleanly.</span> This checks{" "}
                        {check.checks} — it does not check {check.does_not_check}.
                      </>
                    ) : (
                      <>
                        <span className="font-bold">
                          {check.difference_count} difference(s).
                        </span>{" "}
                        Re-ingest this brand before verifying: a verified row is skipped by
                        ingestion, so certifying a stale mapping freezes it.
                      </>
                    )}
                  </div>
                  {check.differences.slice(0, 8).map((d, i) => (
                    <div key={i} className="text-[11px] text-ink-light">
                      <code>{d.field}</code>: stored {JSON.stringify(d.stored)} → source{" "}
                      {JSON.stringify(d.source)}
                    </div>
                  ))}
                  {check.reproducible && (
                    <div className="mt-3 flex flex-wrap items-center gap-2">
                      <input
                        value={name}
                        onChange={(e) => setName(e.target.value)}
                        placeholder="Your name — recorded, not authenticated"
                        className="min-w-[260px] flex-1 rounded-lg border border-line bg-canvas-card px-2.5 py-1.5 text-xs text-ink"
                      />
                      <button
                        onClick={confirm}
                        disabled={busy || !name.trim()}
                        className="inline-flex items-center gap-1.5 rounded-lg bg-brand-dark px-3 py-1.5 text-xs font-bold text-white disabled:opacity-40"
                      >
                        <CheckCircle2 size={13} strokeWidth={2.4} />
                        {busy ? "Recording…" : "Confirm checked"}
                      </button>
                    </div>
                  )}
                </>
              )}
              {message && (
                <p className="mt-2 text-xs leading-relaxed text-red-700">{message}</p>
              )}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

function StatusBadge({ status }: { status: string }) {
  const style =
    status === "VERIFIED"
      ? "border-emerald-200 bg-emerald-50 text-emerald-800"
      : status === "REJECTED"
        ? "border-red-200 bg-red-50 text-red-800"
        : "border-slate-300 bg-slate-100 text-slate-700";
  return (
    <span className={`rounded-full border px-2 py-0.5 text-[11px] font-bold ${style}`}>
      {status}
    </span>
  );
}

/** Which claim types this label could answer. Verification changes none of these — it
 *  changes whether the answer is allowed to be used. */
function AnswerPills({ fact }: { fact: DrugFactQueueRow }) {
  const items: [string, boolean][] = [
    ["approval", fact.answers_approval_claim],
    ["safety", fact.answers_safety_claim],
    ["mechanism", fact.answers_mechanism_claim],
  ];
  return (
    <div className="hidden shrink-0 gap-1.5 sm:flex">
      {items.map(([label, ok]) => (
        <span
          key={label}
          className={`rounded px-1.5 py-0.5 text-[10px] font-bold uppercase ${
            ok ? "bg-emerald-50 text-emerald-800" : "bg-slate-100 text-slate-400"
          }`}
        >
          {label}
        </span>
      ))}
    </div>
  );
}
