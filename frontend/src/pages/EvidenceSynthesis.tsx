import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  BookOpen,
  ClipboardList,
  Info,
  ScrollText,
  TrendingUp,
} from "lucide-react";

import {
  api,
  type EvidenceNetworkList,
  type EvidenceSynthesis as Synthesis,
} from "../api/client";
import {
  AnimatedCard,
  Card,
  EmptyState,
  PageHeader,
  Select,
  Spinner,
  Stat,
} from "../components/ui";

/**
 * Synthesis & Next Steps — the Phase 9 readout, which had an endpoint and no page.
 *
 * **Assembled, never inferred.** Every number here is a stored row some earlier phase
 * decided, so there is no step on this page that can be wrong independently of the phase
 * that produced its input.
 *
 * **Limitations are a section, not a footnote.** The honest readout for a corpus like
 * today's is *"nothing we can release distinguishes these treatments, on a network nobody
 * has ratified, from a corpus that is a third verified"* — and a page that buries that
 * under the findings is telling a brand team something the evidence does not support.
 */
export default function EvidenceSynthesisPage() {
  const [networks, setNetworks] = useState<EvidenceNetworkList | null>(null);
  const [indication, setIndication] = useState("");
  const [data, setData] = useState<Synthesis | null>(null);
  const [loading, setLoading] = useState(true);
  const [fetching, setFetching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .evidenceNetworks()
      .then((list) => {
        setNetworks(list);
        const first = list.networks?.[0]?.indication;
        if (first) setIndication(first);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  const load = useCallback(() => {
    if (!indication) return;
    setFetching(true);
    setError(null);
    api
      .evidenceSynthesis(indication)
      .then(setData)
      .catch((e) => setError(String(e)))
      .finally(() => setFetching(false));
  }, [indication]);

  useEffect(load, [load]);

  const indications = Array.from(
    new Set((networks?.networks ?? []).map((n) => n.indication).filter(Boolean)),
  ) as string[];

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <Spinner size={28} />
      </div>
    );
  }

  const strength = data?.evidence_strength;
  const verifiedPct =
    strength?.verified_fraction != null ? Math.round(strength.verified_fraction * 100) : null;

  return (
    <div>
      <PageHeader
        title="Synthesis & Next Steps"
        subtitle="What the evidence shows, what changed, and what stands between here and a releasable answer."
      />

      {indications.length > 1 && (
        <div className="mb-6 min-w-[280px] max-w-sm">
          <Select
            label="Indication"
            value={indication}
            options={indications}
            onChange={setIndication}
          />
        </div>
      )}

      {error && (
        <div className="mb-6 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800">
          {error}
        </div>
      )}

      {fetching ? (
        <div className="flex justify-center py-16">
          <Spinner size={24} />
        </div>
      ) : !data ? (
        <EmptyState message="No synthesis available for this indication yet." />
      ) : (
        <>
          <AnimatedCard className="mb-6">
            <div className="rounded-2xl border border-line bg-brand-surface p-5">
              <div className="mb-1 text-[11px] font-bold uppercase tracking-wide text-brand-dark">
                The big picture
              </div>
              <p className="text-sm leading-relaxed text-ink">
                <strong>{data.what_the_evidence_shows.length}</strong> comparison
                {data.what_the_evidence_shows.length === 1 ? "" : "s"} can be stated for{" "}
                {data.indication}
                {verifiedPct != null && (
                  <>
                    , from a corpus that is <strong>{verifiedPct}% verified</strong>
                  </>
                )}
                {strength?.network_ratification_status && (
                  <>
                    {" "}
                    on a network that is{" "}
                    <strong>{strength.network_ratification_status}</strong>
                  </>
                )}
                . <strong>{data.limitations.length}</strong> limitation
                {data.limitations.length === 1 ? "" : "s"} qualify everything below.
              </p>
            </div>
          </AnimatedCard>

          {/* Limitations FIRST. Putting them under the findings would let a reader take a
              number before learning what qualifies it. */}
          {data.limitations.length > 0 && (
            <Card
              title="Limitations"
              className="mb-6 border-amber-200"
            >
              <div className="space-y-2">
                {data.limitations.map((l, i) => (
                  <div key={i} className="flex items-start gap-2">
                    <AlertTriangle
                      size={14}
                      className="mt-0.5 shrink-0 text-amber-600"
                      strokeWidth={2.4}
                    />
                    <div className="text-sm">
                      <span className="font-mono text-[11px] font-bold text-amber-900">
                        {l.kind}
                      </span>
                      <span className="text-ink-light"> — {l.detail}</span>
                      {l.count != null && (
                        <span className="text-ink-light"> ({l.count})</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </Card>
          )}

          <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Stat
              label="Findings"
              value={data.what_the_evidence_shows.length}
              icon={<BookOpen size={16} />}
            />
            <Stat
              label="Corpus verified"
              value={verifiedPct != null ? `${verifiedPct}%` : "—"}
              sub={
                strength
                  ? `${strength.studies_verified} of ${strength.studies_total} studies`
                  : undefined
              }
            />
            <Stat
              label="Claims evaluated"
              value={data.ai_alignment.claims_evaluated}
              icon={<ClipboardList size={16} />}
            />
            <Stat
              label="Accepted competitors"
              value={data.competitor_landscape.accepted_count}
              icon={<TrendingUp size={16} />}
            />
          </div>

          <Card title="What the evidence shows" className="mb-6">
            {data.what_the_evidence_shows.length === 0 ? (
              <p className="text-sm text-ink-light">
                Nothing releasable distinguishes these treatments yet.
              </p>
            ) : (
              <div className="space-y-3">
                {data.what_the_evidence_shows.map((f, i) => (
                  <div key={i} className="rounded-xl border border-line bg-canvas p-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-bold text-ink">
                        {f.treatment} <span className="text-ink-light">vs</span>{" "}
                        {f.comparator}
                      </span>
                      {f.evidence_level != null && (
                        <span className="rounded-full border border-line bg-canvas-card px-2 py-0.5 text-[11px] font-bold text-ink-light">
                          L{f.evidence_level}
                        </span>
                      )}
                      {f.is_internal_output && (
                        <span className="rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-[11px] font-bold text-indigo-800">
                          Internal output
                        </span>
                      )}
                      {f.crosses_no_effect && (
                        <span className="rounded-full border border-slate-300 bg-slate-100 px-2 py-0.5 text-[11px] font-bold text-slate-700">
                          crosses no effect
                        </span>
                      )}
                    </div>
                    <p className="mt-1 text-sm leading-relaxed text-ink-light">
                      {f.statement}
                    </p>
                    {f.flags.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {f.flags.map((flag) => (
                          <span
                            key={flag}
                            className="rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 font-mono text-[10px] text-amber-900"
                          >
                            {flag}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </Card>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Card title="Strategic implications">
              {data.strategic_implications.length === 0 ? (
                <p className="text-sm text-ink-light">
                  Nothing to act on — an aligned, calibrated answer produces no work, and a
                  recommendation engine that always finds something is not measuring
                  anything.
                </p>
              ) : (
                <div className="space-y-2">
                  {data.strategic_implications.map((s, i) => (
                    <div
                      key={i}
                      className="flex items-start justify-between gap-3 rounded-lg border border-line bg-canvas p-2.5"
                    >
                      <div className="min-w-0">
                        <div className="text-sm font-semibold text-ink">
                          {s.implication.split("_").join(" ").toLowerCase()}
                        </div>
                        <div className="text-xs text-ink-light">
                          {s.owner ?? "unassigned"}
                          {!s.externally_actionable && " · content cannot fix this"}
                        </div>
                      </div>
                      <span className="shrink-0 text-sm font-bold tabular-nums text-ink">
                        {s.count}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </Card>

            <Card title="What changed">
              <div className="space-y-3 text-sm">
                <div>
                  <div className="text-xs font-bold uppercase tracking-wide text-ink-light">
                    New studies ({data.what_changed.new_studies.length})
                  </div>
                  {data.what_changed.new_studies.length === 0 ? (
                    <p className="text-ink-light">None in the last {data.what_changed.window_days} days.</p>
                  ) : (
                    data.what_changed.new_studies.slice(0, 5).map((s) => (
                      <div key={s.study_id} className="text-xs text-ink-light">
                        <code>{s.study_id}</code> · {s.verification_status}
                      </div>
                    ))
                  )}
                </div>
                <div>
                  <div className="text-xs font-bold uppercase tracking-wide text-ink-light">
                    Label updates ({data.what_changed.label_updates.length})
                  </div>
                  {data.what_changed.label_updates.length === 0 ? (
                    <p className="text-ink-light">None.</p>
                  ) : (
                    data.what_changed.label_updates.slice(0, 5).map((l) => (
                      <div key={l.brand} className="text-xs text-ink-light">
                        {l.brand} · {l.label_updated_at ?? "undated"} ·{" "}
                        {l.verification_status}
                      </div>
                    ))
                  )}
                </div>
              </div>
            </Card>
          </div>

          <div className="mt-6 flex items-start gap-2 rounded-xl border border-line bg-canvas p-3">
            <Info size={14} className="mt-0.5 shrink-0 text-ink-light" />
            <p className="text-xs leading-relaxed text-ink-light">
              Assembled from stored rows, never inferred. Generated{" "}
              {new Date(data.generated_at).toLocaleString()}.
              <ScrollText size={11} className="ml-1 inline" /> Every figure traces back to
              the phase that decided it.
            </p>
          </div>
        </>
      )}
    </div>
  );
}
