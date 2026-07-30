import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Network, ShieldCheck, X } from "lucide-react";

import {
  api,
  type EvidenceNetworkDetail,
  type EvidenceNetworkRow,
} from "../api/client";
import {
  Card,
  EmptyState,
  InfoTooltip,
  PageHeader,
  Select,
  Spinner,
} from "../components/ui";
// Shared with the ingest report. One presentation of one topology — a second copy would
// eventually disagree with this one about whether a graph is connected.
import TopologyPanel from "../components/TopologyPanel";

const RATIFICATION_CLS: Record<string, string> = {
  DRAFT: "bg-slate-100 text-ink-light",
  PENDING_MEDICAL_REVIEW: "bg-amber-100 text-amber-800",
  PENDING_STATISTICAL_REVIEW: "bg-amber-100 text-amber-800",
  RATIFIED: "bg-emerald-100 text-emerald-800",
  REJECTED: "bg-red-100 text-red-800",
  SUPERSEDED: "bg-slate-100 text-ink-light",
};

const MEMBERSHIP_CLS: Record<string, string> = {
  PROPOSED: "bg-slate-100 text-ink-light",
  INCLUDED: "bg-emerald-100 text-emerald-800",
  EXCLUDED: "bg-red-100 text-red-800",
  REQUIRES_REVIEW: "bg-amber-100 text-amber-800",
};

/**
 * Network browser — the entry point the resolver never had.
 *
 * `/comparisons/resolve` and `/evidence-review/networks/{id}` both take a network id, and
 * nothing exposed one until this page's endpoint existed.
 *
 * The detail view shows **both topologies side by side**. The stored graph is endpoint-level;
 * a protocol-scoped resolve applies the approved time window and can legitimately see fewer
 * nodes. Showing only the stored graph is how a surface ends up promising a comparison the
 * resolver then refuses.
 */
export default function EvidenceNetworks() {
  const [rows, setRows] = useState<EvidenceNetworkRow[]>([]);
  const [states, setStates] = useState<string[]>([]);
  const [indication, setIndication] = useState("");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    api
      .evidenceNetworks({
        indication: indication || undefined,
        ratification_status: status || undefined,
      })
      .then((res) => {
        setRows(res.networks);
        setStates(res.ratification_states);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [indication, status]);

  // Derived from the unfiltered-by-indication result set, so clearing the filter is possible.
  const [indications, setIndications] = useState<string[]>([]);
  useEffect(() => {
    api
      .evidenceNetworks()
      .then((res) =>
        setIndications([...new Set(res.networks.map((n) => n.indication))].sort()),
      )
      .catch(() => setIndications([]));
  }, []);

  return (
    <div>
      <PageHeader
        title="Evidence Networks"
        subtitle="One network is one analysable question: this indication, this endpoint, this stratum, this phase, under this protocol."
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
          label="Ratification"
          value={status}
          options={["", ...states]}
          optionLabels={{ "": "Any status" }}
          onChange={setStatus}
        />
        <span className="pb-2 text-xs text-ink-light">
          {rows.length} network{rows.length === 1 ? "" : "s"}
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
        <EmptyState
          icon={<Network size={30} />}
          message="No networks assembled yet. Run scripts/ingest_evidence.py to build one."
        />
      ) : (
        <Card>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-ink-light">
                  <th className="pb-2 pr-4 font-bold">Network</th>
                  <th className="pb-2 pr-4 font-bold">Endpoint</th>
                  <th className="pb-2 pr-4 font-bold">Protocol</th>
                  <th className="pb-2 pr-4 font-bold">Shape</th>
                  <th className="pb-2 pr-4 font-bold">Members</th>
                  <th className="pb-2 font-bold">Ratification</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((n) => (
                  <tr
                    key={n.network_id}
                    onClick={() => setSelected(n.network_id)}
                    className="cursor-pointer border-b border-line/60 transition-colors last:border-0 hover:bg-brand-surface/40"
                  >
                    <td className="py-3 pr-4">
                      <div className="font-semibold text-ink">{n.indication}</div>
                      <div className="font-mono text-[11px] text-ink-light">
                        {n.network_id}
                      </div>
                    </td>
                    <td className="py-3 pr-4">
                      <div className="text-xs font-semibold text-ink">
                        {n.canonical_outcome_id}
                      </div>
                      <div className="text-[11px] text-ink-light">
                        {n.treatment_phase}
                        {n.population_stratum ? ` · ${n.population_stratum}` : ""}
                      </div>
                    </td>
                    <td className="py-3 pr-4 text-xs">
                      {n.protocol_id ? (
                        <span className="font-mono text-[11px] text-ink">
                          {n.protocol_id}
                        </span>
                      ) : (
                        <span className="text-ink-light">none</span>
                      )}
                    </td>
                    <td className="py-3 pr-4 text-xs text-ink">
                      {n.node_count} nodes · {n.edge_count} edges
                      <div className="text-[11px] text-ink-light">
                        {n.is_connected ? "connected" : "disconnected"}
                        {n.has_closed_loops ? " · loops" : ""}
                        {n.has_multi_arm_studies ? " · multi-arm" : ""}
                      </div>
                    </td>
                    <td className="py-3 pr-4">
                      <div className="flex flex-wrap gap-1">
                        {Object.entries(n.membership_counts).map(([s, c]) => (
                          <span
                            key={s}
                            className={`rounded-full px-2 py-0.5 text-[10px] font-bold ${MEMBERSHIP_CLS[s] || "bg-slate-100"}`}
                          >
                            {c} {s.toLowerCase()}
                          </span>
                        ))}
                      </div>
                    </td>
                    <td className="py-3">
                      <span
                        className={`rounded-full px-2.5 py-1 text-[10px] font-bold ${RATIFICATION_CLS[n.ratification_status] || "bg-slate-100"}`}
                      >
                        {n.ratification_status.replace(/_/g, " ").toLowerCase()}
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
        <NetworkDrawer networkId={selected} onClose={() => setSelected(null)} />
      )}
    </div>
  );
}

function NetworkDrawer({
  networkId,
  onClose,
}: {
  networkId: string;
  onClose: () => void;
}) {
  const [data, setData] = useState<EvidenceNetworkDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setData(null);
    api
      .evidenceNetwork(networkId)
      .then(setData)
      .catch((e) => setError(String(e)));
  }, [networkId]);

  const byStatus = useMemo(() => {
    const groups: Record<string, typeof data extends null ? never : any[]> = {};
    for (const m of data?.memberships || []) {
      (groups[m.membership_status] ||= []).push(m);
    }
    return groups;
  }, [data]);

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/30" onClick={onClose}>
      <div
        className="h-full w-full max-w-2xl overflow-y-auto bg-canvas p-6 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-5 flex items-start justify-between gap-4">
          <div>
            <h2 className="text-lg font-bold text-ink">
              {data?.label || networkId}
            </h2>
            <p className="font-mono text-[11px] text-ink-light">{networkId}</p>
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
            {data.overstates_answerable && (
              <div
                role="alert"
                className="flex items-start gap-3 rounded-xl border-2 border-amber-300 bg-amber-50 p-4"
              >
                <AlertTriangle
                  size={18}
                  className="mt-0.5 shrink-0 text-amber-600"
                  strokeWidth={2.2}
                />
                <div className="text-sm text-amber-900">
                  <p className="font-bold">
                    This network holds more than its protocol can answer on.
                  </p>
                  <p className="mt-1 leading-relaxed">
                    The stored graph is endpoint-level. Under{" "}
                    <code>{data.protocol_scope?.protocol_id}</code>'s approved window (
                    weeks {data.protocol_scope?.approved_time_window.join("–")}), a
                    resolve loses{" "}
                    <strong>
                      {data.protocol_scope?.nodes_lost_to_window.join(", ")}
                    </strong>
                    . Nothing is filtered — the window is one protocol's judgement and can
                    be re-approved without re-harvesting.
                  </p>
                </div>
              </div>
            )}

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <TopologyPanel
                title="Endpoint-level topology"
                note="Stored. Every study reporting this outcome inside the endpoint's own allowed window."
                nodes={data.endpoint_topology.nodes}
                routes={data.endpoint_topology.administration_routes}
                lost={[]}
              />
              {data.protocol_scope ? (
                <TopologyPanel
                  title="Protocol-scoped topology"
                  note={`Derived per request, never stored. Weeks ${data.protocol_scope.approved_time_window.join("–")}.`}
                  nodes={
                    (data.protocol_scope.topology?.nodes as string[] | undefined) || []
                  }
                  routes={data.endpoint_topology.administration_routes}
                  lost={data.protocol_scope.nodes_lost_to_window}
                />
              ) : (
                <Card title="Protocol-scoped topology">
                  <p className="text-sm leading-relaxed text-ink-light">
                    No protocol governs this network, so there is no approved window to
                    narrow it. That is different from — and better than — reporting an
                    empty graph, which would claim nothing is answerable.
                  </p>
                </Card>
              )}
            </div>

            <Card title="Ratification">
              <div className="space-y-2 text-sm">
                <Row
                  label="Status"
                  value={
                    <span
                      className={`rounded-full px-2.5 py-1 text-[10px] font-bold ${RATIFICATION_CLS[data.ratification.status] || "bg-slate-100"}`}
                    >
                      {data.ratification.status.replace(/_/g, " ").toLowerCase()}
                    </span>
                  }
                />
                <Row
                  label="Medical review"
                  value={
                    data.ratification.medical_reviewer
                      ? `${data.ratification.medical_reviewer} · ${data.ratification.medical_reviewed_at?.slice(0, 10)}`
                      : "not recorded"
                  }
                />
                <Row
                  label="Statistical review"
                  value={
                    data.ratification.statistical_reviewer
                      ? `${data.ratification.statistical_reviewer} · ${data.ratification.statistical_reviewed_at?.slice(0, 10)}`
                      : "not recorded"
                  }
                />
                {data.ratification.rejection_reason && (
                  <Row
                    label="Rejected because"
                    value={data.ratification.rejection_reason}
                  />
                )}
              </div>
              <p className="mt-3 flex items-start gap-2 text-xs leading-relaxed text-ink-light">
                <ShieldCheck size={14} className="mt-0.5 shrink-0" />
                A reviewer name here is <strong>recorded, not authenticated</strong>.
                RBAC is absent from this tree, so this is a governance record rather than
                a security control.
              </p>
            </Card>

            <Card
              title={
                <span className="flex items-center gap-2">
                  Membership decisions
                  <InfoTooltip content="Scoped to network AND protocol: the same verified study can belong in one network and not another." />
                </span>
              }
            >
              {!data.memberships.length ? (
                <p className="text-sm text-ink-light">No members proposed.</p>
              ) : (
                <div className="space-y-4">
                  {Object.entries(byStatus).map(([status, items]) => (
                    <div key={status}>
                      <div className="mb-2 flex items-center gap-2">
                        <span
                          className={`rounded-full px-2 py-0.5 text-[10px] font-bold ${MEMBERSHIP_CLS[status] || "bg-slate-100"}`}
                        >
                          {status.toLowerCase()}
                        </span>
                        <span className="text-xs text-ink-light">
                          {(items as any[]).length}
                        </span>
                      </div>
                      <div className="space-y-2">
                        {(items as any[]).map((m) => (
                          <div
                            key={m.membership_id}
                            className="rounded-lg border border-line bg-canvas-card p-3"
                          >
                            <div className="flex items-baseline justify-between gap-3">
                              <span className="text-sm font-semibold text-ink">
                                {m.acronym || m.registry_id || m.study_id}
                              </span>
                              <span className="text-[10px] font-bold uppercase text-ink-light">
                                {m.verification_status}
                              </span>
                            </div>
                            {m.title && (
                              <p className="mt-0.5 text-xs text-ink-light">{m.title}</p>
                            )}
                            {m.exclusion_reason && (
                              <p className="mt-1.5 text-xs text-red-700">
                                Excluded: {m.exclusion_reason}
                              </p>
                            )}
                            {m.proposal_rationale && !m.exclusion_reason && (
                              <p className="mt-1.5 text-xs text-ink-light">
                                Proposed because it {m.proposal_rationale}
                              </p>
                            )}
                            {!!m.mismatch_flags.length && (
                              <div className="mt-1.5 flex flex-wrap gap-1">
                                {m.mismatch_flags.map((f: string) => (
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

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4">
      <span className="text-xs font-semibold text-ink-light">{label}</span>
      <span className="text-right text-sm text-ink">{value}</span>
    </div>
  );
}
