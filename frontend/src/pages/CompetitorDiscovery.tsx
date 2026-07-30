import { useEffect, useMemo, useState } from "react";
import {
  Check,
  Clock,
  FileCode2,
  Radar,
  RefreshCw,
  ShieldAlert,
  X,
} from "lucide-react";

import {
  api,
  type CompetitorCandidate,
  type DiscoveryClassMap,
  type DiscoveryConfigProposal,
  type DiscoverySweepReport,
} from "../api/client";
import {
  AnimatedCard,
  Card,
  EmptyState,
  InfoTooltip,
  PageHeader,
  Select,
  Spinner,
  Stat,
} from "../components/ui";

const STATUS_CLS: Record<string, string> = {
  NEW: "bg-sky-100 text-sky-800",
  ACCEPTED: "bg-emerald-100 text-emerald-800",
  REJECTED: "bg-red-100 text-red-800",
  DEFERRED: "bg-amber-100 text-amber-800",
};

/**
 * Tier A competitor discovery queue.
 *
 * Every candidate here is a treatment the *evidence* says competes in an indication while
 * the *curated config* does not list it there. Nothing on this page edits `brands.yaml` —
 * accepting a candidate records a decision and produces a YAML fragment a human commits.
 * That boundary is the whole point: a curated table is a reviewable artefact, an inferred
 * label is an unreviewed assertion, and only the first belongs upstream of a medical review
 * gate.
 */
export default function CompetitorDiscovery() {
  const [candidates, setCandidates] = useState<CompetitorCandidate[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [states, setStates] = useState<string[]>([]);
  const [indication, setIndication] = useState("");
  const [status, setStatus] = useState("NEW");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sweeping, setSweeping] = useState(false);
  const [sweep, setSweep] = useState<DiscoverySweepReport | null>(null);
  const [reviewer, setReviewer] = useState("");
  const [proposal, setProposal] = useState<DiscoveryConfigProposal | null>(null);
  const [classMap, setClassMap] = useState<DiscoveryClassMap | null>(null);

  async function load() {
    setLoading(true);
    try {
      const res = await api.discoveryCandidates({
        indication: indication || undefined,
        review_status: status || undefined,
      });
      setCandidates(res.candidates);
      setCounts(res.counts_by_status);
      setStates(res.review_states);
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [indication, status]);

  useEffect(() => {
    api
      .discoveryConfigProposal(indication || undefined)
      .then(setProposal)
      .catch(() => setProposal(null));
  }, [indication, candidates.length]);

  useEffect(() => {
    if (!indication) {
      setClassMap(null);
      return;
    }
    api.discoveryClassMap(indication).then(setClassMap).catch(() => setClassMap(null));
  }, [indication]);

  const indications = useMemo(
    () => [...new Set((sweep?.indications || []).map((i) => i.indication))].sort(),
    [sweep],
  );

  async function runSweep() {
    setSweeping(true);
    try {
      const report = await api.discoverySweep(indication || undefined);
      setSweep(report);
      await load();
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setSweeping(false);
    }
  }

  async function decide(candidate: CompetitorCandidate, decision: string) {
    if (!reviewer.trim()) {
      setError("A decision needs a named reviewer.");
      return;
    }
    try {
      await api.discoveryReview(candidate.candidate_id, {
        decision,
        reviewer: reviewer.trim(),
      });
      await load();
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <div>
      <PageHeader
        title="Competitor Discovery"
        subtitle="Treatments the evidence says compete here, that the curated competitor list does not mention."
      />

      <div
        role="alert"
        className="mb-6 flex items-start gap-3 rounded-xl border-2 border-amber-300 bg-amber-50 p-4"
      >
        <ShieldAlert
          size={18}
          className="mt-0.5 shrink-0 text-amber-600"
          strokeWidth={2.2}
        />
        <div className="text-sm text-amber-900">
          <p className="font-bold">Discovery proposes; a person commits.</p>
          <p className="mt-1 leading-relaxed">
            Accepting a candidate records a decision — it does <strong>not</strong> add the
            drug to any competitor list. The config fragment below is committed by hand.
            Drug class and route are copied from curation or left blank; they are never
            inferred.
          </p>
        </div>
      </div>

      <div className="mb-5 flex flex-wrap items-end gap-3">
        <Select
          label="Indication"
          value={indication}
          options={["", ...indications]}
          optionLabels={{ "": "All indications" }}
          onChange={setIndication}
        />
        <Select
          label="Review status"
          value={status}
          options={["", ...states]}
          optionLabels={{ "": "All" }}
          onChange={setStatus}
        />
        <label className="flex flex-col gap-1">
          <span className="text-xs font-bold uppercase tracking-wide text-ink-light">
            Your name (recorded, not authenticated)
          </span>
          <input
            value={reviewer}
            onChange={(e) => setReviewer(e.target.value)}
            placeholder="e.g. A. Curator"
            className="rounded-lg border border-line bg-canvas-card px-3 py-2 text-sm text-ink outline-none focus:border-brand-light"
          />
        </label>
        <button
          onClick={runSweep}
          disabled={sweeping}
          className="mb-0.5 inline-flex items-center gap-2 rounded-lg bg-brand-dark px-4 py-2 text-sm font-bold text-white transition-opacity disabled:opacity-50"
        >
          {sweeping ? <Spinner size={15} /> : <RefreshCw size={15} strokeWidth={2.4} />}
          {sweeping ? "Sweeping…" : "Run sweep"}
        </button>
      </div>

      {error && (
        <div className="mb-4 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800">
          {error}
        </div>
      )}

      {sweep && (
        <AnimatedCard className="mb-5">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-4">
            <Stat label="New candidates" value={sweep.created} icon={<Radar size={16} />} />
            <Stat label="Refreshed" value={sweep.updated} />
            <Stat
              label="Left decided"
              value={sweep.skipped_decided}
              tooltip="A re-sweep never overwrites an accepted or rejected candidate — a rerun is not new information about a judgement someone already made."
            />
            <Stat
              label="Already tracked"
              value={sweep.indications.reduce((n, i) => n + i.already_tracked, 0)}
              sub={`of ${sweep.indications.reduce((n, i) => n + i.treatments_observed, 0)} treatments seen`}
            />
          </div>
        </AnimatedCard>
      )}

      <div className="mb-5 flex flex-wrap gap-2">
        {Object.entries(counts).map(([s, n]) => (
          <span
            key={s}
            className={`rounded-full px-3 py-1 text-xs font-bold ${STATUS_CLS[s] || "bg-slate-100"}`}
          >
            {n} {s.toLowerCase()}
          </span>
        ))}
      </div>

      {loading ? (
        <div className="flex justify-center py-16">
          <Spinner size={26} />
        </div>
      ) : !candidates.length ? (
        <EmptyState
          icon={<Radar size={30} />}
          message="No candidates in this view. Run a sweep to look for competitors the config does not list."
        />
      ) : (
        <div className="space-y-3">
          {candidates.map((c) => (
            <CandidateCard
              key={c.candidate_id}
              candidate={c}
              onDecide={(d) => decide(c, d)}
            />
          ))}
        </div>
      )}

      {classMap && <ClassMapPanel data={classMap} />}
      {proposal && proposal.accepted_pending_commit > 0 && (
        <ProposalPanel proposal={proposal} />
      )}
    </div>
  );
}

function CandidateCard({
  candidate: c,
  onDecide,
}: {
  candidate: CompetitorCandidate;
  onDecide: (decision: string) => void;
}) {
  return (
    <Card>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-base font-bold text-ink">{c.treatment}</span>
            <span
              className={`rounded-full px-2.5 py-1 text-[10px] font-bold ${STATUS_CLS[c.review_status] || "bg-slate-100"}`}
            >
              {c.review_status.toLowerCase()}
            </span>
            {c.drug_class ? (
              <span className="rounded-full bg-brand-surface px-2 py-0.5 text-[10px] font-bold text-brand-dark">
                {c.drug_class}
                {c.administration_route ? ` · ${c.administration_route}` : ""}
              </span>
            ) : (
              <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-bold text-amber-800">
                needs characterising
              </span>
            )}
          </div>
          <p className="mt-0.5 text-xs text-ink-light">
            {c.indication}
            {c.sponsor ? ` · ${c.sponsor}` : ""}
            {c.development_phase ? ` · ${c.development_phase}` : ""}
          </p>

          <ul className="mt-3 space-y-1">
            {c.reason_labels.map((label) => (
              <li key={label} className="flex items-start gap-2 text-xs text-ink">
                <Check
                  size={13}
                  className="mt-0.5 shrink-0 text-brand-light"
                  strokeWidth={2.6}
                />
                {label}
              </li>
            ))}
          </ul>

          <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-[11px] text-ink-light">
            <span>
              <strong className="text-ink">{c.evidence_count}</strong> stud
              {c.evidence_count === 1 ? "y" : "ies"}
            </span>
            {!!c.compared_with.length && (
              <span>
                head-to-head vs{" "}
                <strong className="text-ink">{c.compared_with.join(", ")}</strong>
              </span>
            )}
            {!!c.shared_comparators.length && (
              <span>shares {c.shared_comparators.join(", ")}</span>
            )}
            {c.published_nma_count > 0 && (
              <span>
                in <strong className="text-ink">{c.published_nma_count}</strong> published
                NMA{c.published_nma_count === 1 ? "" : "s"}
              </span>
            )}
            {c.latest_evidence_date && <span>latest {c.latest_evidence_date}</span>}
          </div>
        </div>

        <div className="flex shrink-0 flex-col items-end gap-3">
          <div className="text-right">
            <div className="flex items-center justify-end gap-1">
              <span className="text-2xl font-bold tabular-nums text-ink">
                {c.discovery_confidence.toFixed(2)}
              </span>
              <InfoTooltip content="Summed reason weights, capped at 1.0. Deliberately independent of study volume — ten trials of a drug nobody randomised against ours is still weak evidence that it competes." />
            </div>
            <div className="text-[10px] font-bold uppercase tracking-wide text-ink-light">
              signal
            </div>
          </div>

          {c.review_status === "NEW" || c.review_status === "DEFERRED" ? (
            <div className="flex gap-1.5">
              <button
                onClick={() => onDecide("ACCEPTED")}
                className="inline-flex items-center gap-1 rounded-lg bg-emerald-600 px-2.5 py-1.5 text-xs font-bold text-white"
              >
                <Check size={13} strokeWidth={2.6} />
                Accept
              </button>
              <button
                onClick={() => onDecide("REJECTED")}
                className="inline-flex items-center gap-1 rounded-lg border border-line px-2.5 py-1.5 text-xs font-bold text-ink"
              >
                <X size={13} strokeWidth={2.6} />
                Reject
              </button>
              {c.review_status === "NEW" && (
                <button
                  onClick={() => onDecide("DEFERRED")}
                  className="inline-flex items-center gap-1 rounded-lg border border-line px-2.5 py-1.5 text-xs font-bold text-ink-light"
                >
                  <Clock size={13} strokeWidth={2.6} />
                  Later
                </button>
              )}
            </div>
          ) : (
            <div className="text-right text-[11px] text-ink-light">
              {c.reviewed_by && <div>by {c.reviewed_by}</div>}
              {c.config_applied ? (
                <div className="font-bold text-emerald-700">config committed</div>
              ) : c.review_status === "ACCEPTED" ? (
                <div className="font-bold text-amber-700">awaiting config commit</div>
              ) : null}
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}

function ClassMapPanel({ data }: { data: DiscoveryClassMap }) {
  return (
    <Card className="mt-6" title={`Cross-class map — ${data.indication}`}>
      <p className="mb-4 text-xs leading-relaxed text-ink-light">
        Built from the curated class table only. <strong>{data.characterised_pct}%</strong>{" "}
        of the {data.treatment_count} treatments in this indication's trials carry a class.
        The uncharacterised list is shown because a map that dropped them would look
        complete while hiding most of the network.
      </p>

      {data.is_route_mixed && (
        <div className="mb-4 rounded-lg bg-amber-50 p-3 text-xs leading-relaxed text-amber-900">
          <strong>This network mixes administration routes</strong> (
          {data.routes_present.join(", ")}). Oral and injectable placebo arms do not
          reliably produce the same response rate, which is a transitivity threat to
          disclose — never to adjust away.
        </div>
      )}

      <div className="space-y-3">
        {data.classes.map((group) => (
          <div key={group.drug_class}>
            <div className="mb-1.5 flex items-baseline gap-2">
              <span className="text-sm font-bold text-ink">{group.drug_class}</span>
              <span className="text-[11px] text-ink-light">
                {group.treatments.length} treatment
                {group.treatments.length === 1 ? "" : "s"}
              </span>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {group.treatments.map((t) => {
                const tracked = group.monitored.includes(t);
                return (
                  <span
                    key={t}
                    className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${
                      tracked
                        ? "bg-brand-surface text-brand-dark"
                        : "border border-line bg-canvas-card text-ink"
                    }`}
                  >
                    {t}
                    {group.routes[t] && (
                      <span className="ml-1 text-[9px] font-bold text-ink-light">
                        {group.routes[t]}
                      </span>
                    )}
                  </span>
                );
              })}
            </div>
          </div>
        ))}
      </div>

      {!!data.uncharacterised.length && (
        <div className="mt-4 border-t border-line pt-3">
          <div className="mb-1.5 text-[10px] font-bold uppercase tracking-wide text-amber-700">
            No curated class ({data.uncharacterised.length})
          </div>
          <div className="flex flex-wrap gap-1.5">
            {data.uncharacterised.map((t) => (
              <span
                key={t}
                className="rounded-full bg-amber-50 px-2 py-0.5 text-[11px] font-semibold text-amber-900"
              >
                {t}
              </span>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

function ProposalPanel({ proposal }: { proposal: DiscoveryConfigProposal }) {
  const [copied, setCopied] = useState(false);

  return (
    <Card
      className="mt-6"
      title={
        <span className="flex items-center gap-2">
          <FileCode2 size={16} />
          Config to commit ({proposal.accepted_pending_commit})
        </span>
      }
    >
      <p className="mb-3 text-xs leading-relaxed text-ink-light">{proposal.note}</p>
      {!!proposal.needs_characterising.length && (
        <p className="mb-3 rounded-lg bg-amber-50 p-3 text-xs leading-relaxed text-amber-900">
          <strong>{proposal.needs_characterising.join(", ")}</strong> have no curated class
          or route, so their catalog entries are emitted <strong>commented out</strong>. A
          pasteable placeholder would let a fake class ship as a real value; a comment
          cannot.
        </p>
      )}
      <pre className="max-h-80 overflow-auto rounded-lg border border-line bg-slate-50 p-3 font-mono text-[11px] leading-relaxed text-ink">
        {proposal.yaml}
      </pre>
      <button
        onClick={() => {
          navigator.clipboard?.writeText(proposal.yaml);
          setCopied(true);
          setTimeout(() => setCopied(false), 1800);
        }}
        className="mt-3 rounded-lg border border-line px-3 py-1.5 text-xs font-bold text-ink"
      >
        {copied ? "Copied" : "Copy fragment"}
      </button>
    </Card>
  );
}
