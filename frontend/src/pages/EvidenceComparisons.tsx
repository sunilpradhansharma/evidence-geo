import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  ChevronDown,
  ChevronRight,
  GitCompare,
  ShieldAlert,
} from "lucide-react";

import {
  api,
  type ComparisonMatrix,
  type EvidenceNetworkList,
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
 * Comparative Evidence Explorer — the resolver's answers, made browsable.
 *
 * `GET /comparisons/matrix` has existed since Phase 6 and nothing rendered it: the numbers
 * that produced "15 pairs, 6 releasable" were reachable only by someone who already knew a
 * network id and had a terminal open.
 *
 * Three refusals shape this page.
 *
 * 1. **An exploratory number never looks releasable.** `is_releasable` is false for
 *    anything short of an approved protocol AND a ratified network, and the badge says so
 *    before the estimate is read. Internal analytical output carries its label.
 * 2. **The fall-through chain is shown, not just the answer.** `considered` is what lets a
 *    row say "a head-to-head trial exists but reports week 12, so this is indirect" —
 *    three facts a reviewer needs, none of which survive showing only where the walk
 *    stopped.
 * 3. **A gap is a finding, rendered as one.** Level 4 is not an error state; it gets the
 *    same visual weight as an estimate, because "this comparison is not estimable, here is
 *    why" is a legitimate product output.
 */

const LEVEL_LABEL: Record<number, string> = {
  1: "Direct evidence",
  2: "Published synthesis",
  3: "Computed synthesis",
  4: "Evidence gap",
};

const LEVEL_STYLE: Record<number, string> = {
  1: "bg-emerald-50 text-emerald-800 border-emerald-200",
  2: "bg-teal-50 text-teal-800 border-teal-200",
  3: "bg-indigo-50 text-indigo-800 border-indigo-200",
  4: "bg-slate-100 text-slate-700 border-slate-300",
};

type Pair = Record<string, any>;

export default function EvidenceComparisonsPage() {
  const [networks, setNetworks] = useState<EvidenceNetworkList | null>(null);
  const [networkId, setNetworkId] = useState("");
  const [matrix, setMatrix] = useState<ComparisonMatrix | null>(null);
  const [loading, setLoading] = useState(true);
  const [resolving, setResolving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .evidenceNetworks()
      .then((data) => {
        setNetworks(data);
        if (data.networks?.length) setNetworkId(data.networks[0].network_id);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!networkId) return;
    setResolving(true);
    setError(null);
    api
      .comparisonMatrix(networkId)
      .then(setMatrix)
      .catch((e) => setError(String(e)))
      .finally(() => setResolving(false));
  }, [networkId]);

  const pairs: Pair[] = useMemo(() => matrix?.pairs ?? [], [matrix]);
  const releasable = pairs.filter((p) => p.is_releasable).length;
  const gaps = pairs.filter((p) => p.evidence_level === 4).length;

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <Spinner size={28} />
      </div>
    );
  }

  return (
    <div>
      <PageHeader
        title="Comparative Evidence"
        subtitle="Every pair the resolver can be asked about, with the level it answered at and what it rejected on the way."
      />

      <AnimatedCard className="mb-6">
        <div className="rounded-2xl border border-line bg-brand-surface p-5">
          <div className="mb-1 text-[11px] font-bold uppercase tracking-wide text-brand-dark">
            The big picture
          </div>
          <p className="text-sm leading-relaxed text-ink">
            {pairs.length === 0 ? (
              <>
                This network resolves <strong>no pairs</strong> yet. Evidence gathering
                skips unverified studies even in exploratory mode, so a corpus nobody has
                curated yields nothing at all.
              </>
            ) : (
              <>
                <strong>{pairs.length}</strong> pairs resolve;{" "}
                <strong>{releasable}</strong> are releasable and{" "}
                <strong>{gaps}</strong> return a structured evidence gap. A gap is a
                finding, not a failure — it names which link is missing.
              </>
            )}
          </p>
        </div>
      </AnimatedCard>

      <div
        role="alert"
        className="mb-6 flex items-start gap-3 rounded-xl border-2 border-amber-300 bg-amber-50 p-4"
      >
        <ShieldAlert size={18} className="mt-0.5 shrink-0 text-amber-600" strokeWidth={2.2} />
        <div className="text-sm text-amber-900">
          <p className="font-bold">Internal analytical output — not approved for external use.</p>
          <p className="mt-1 leading-relaxed">
            Anything computed here is our own synthesis. It keeps full provenance and stays
            referenceable in review and audit, and it does not become an external claim by
            being correct.
          </p>
        </div>
      </div>

      <div className="mb-6 flex flex-wrap items-end gap-3">
        <div className="min-w-[280px]">
          <Select
            label="Network"
            value={networkId}
            options={(networks?.networks ?? []).map((n) => n.network_id)}
            onChange={setNetworkId}
          />
        </div>
      </div>

      {error && (
        <div className="mb-6 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800">
          {error}
        </div>
      )}

      {resolving ? (
        <div className="flex justify-center py-16">
          <Spinner size={24} />
        </div>
      ) : pairs.length === 0 ? (
        <EmptyState message="No comparisons resolve on this network yet." />
      ) : (
        <>
          <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-3">
            <Stat label="Pairs resolved" value={pairs.length} icon={<GitCompare size={16} />} />
            <Stat
              label="Releasable"
              value={releasable}
              sub="approved protocol + ratified network"
              tooltip="Anything short of both is EXPLORATORY, which cannot flow to questions, scoring or recommendations."
            />
            <Stat label="Structured gaps" value={gaps} sub="not estimable, with a reason" />
          </div>

          <div className="space-y-3">
            {pairs.map((pair, i) => (
              <PairRow key={`${pair.treatment}-${pair.comparator}-${i}`} pair={pair} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function PairRow({ pair }: { pair: Pair }) {
  const [open, setOpen] = useState(false);
  const level = Number(pair.evidence_level ?? 4);
  const considered: Pair[] = pair.considered ?? [];
  const sensitivity = pair.sensitivity;

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
            <span className="text-sm font-bold text-ink">
              {pair.treatment} <span className="text-ink-light">vs</span> {pair.comparator}
            </span>
            <span
              className={`rounded-full border px-2 py-0.5 text-[11px] font-bold ${
                LEVEL_STYLE[level] ?? LEVEL_STYLE[4]
              }`}
            >
              L{level} · {LEVEL_LABEL[level] ?? "Unknown"}
            </span>
            {pair.is_releasable ? (
              <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[11px] font-bold text-emerald-800">
                Releasable
              </span>
            ) : (
              <span className="rounded-full border border-slate-300 bg-slate-100 px-2 py-0.5 text-[11px] font-bold text-slate-700">
                Exploratory
              </span>
            )}
            {pair.is_internal_output && (
              <span className="rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-[11px] font-bold text-indigo-800">
                Internal output
              </span>
            )}
          </div>
          <p className="mt-1 text-sm leading-relaxed text-ink-light">{pair.reason}</p>
        </div>
        <Estimate pair={pair} />
      </button>

      {open && (
        <div className="mt-4 space-y-4 border-t border-line pt-4">
          {pair.flags?.length > 0 && (
            <FlagRow flags={pair.flags} />
          )}

          {sensitivity && <SensitivityPanel sensitivity={sensitivity} />}

          {considered.length > 0 && (
            <div>
              <div className="mb-2 text-xs font-bold uppercase tracking-wide text-ink-light">
                What was tried, in order
              </div>
              <div className="space-y-1.5">
                {considered.map((attempt, i) => (
                  <div key={i} className="flex items-start gap-2 text-xs">
                    <span
                      className={`mt-0.5 shrink-0 rounded px-1.5 py-0.5 font-bold ${
                        attempt.succeeded
                          ? "bg-emerald-50 text-emerald-800"
                          : "bg-slate-100 text-slate-600"
                      }`}
                    >
                      L{attempt.level}
                    </span>
                    <div className="min-w-0">
                      <span className="font-semibold text-ink">{attempt.status}</span>
                      <span className="text-ink-light"> — {attempt.reason}</span>
                    </div>
                  </div>
                ))}
              </div>
              <p className="mt-2 text-xs leading-relaxed text-ink-light">
                Falling through is recorded rather than forgotten: a rejected head-to-head
                trial stays in the provenance, because the first question any reviewer asks
                is whether a direct trial exists.
              </p>
            </div>
          )}

          {pair.contributing_studies?.length > 0 && (
            <div>
              <div className="mb-1.5 text-xs font-bold uppercase tracking-wide text-ink-light">
                Contributing studies ({pair.contributing_studies.length})
              </div>
              <div className="flex flex-wrap gap-1.5">
                {pair.contributing_studies.map((s: string) => (
                  <span
                    key={s}
                    className="rounded border border-line bg-canvas px-1.5 py-0.5 font-mono text-[11px] text-ink"
                  >
                    {s}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

function Estimate({ pair }: { pair: Pair }) {
  if (pair.estimate == null) {
    return (
      <div className="shrink-0 text-right">
        <div className="text-sm font-bold text-ink-light">—</div>
        <div className="text-[11px] text-ink-light">not estimable</div>
      </div>
    );
  }
  return (
    <div className="shrink-0 text-right">
      <div className="text-base font-bold tabular-nums text-ink">
        {Number(pair.estimate).toFixed(3)}
      </div>
      {pair.ci_lower != null && pair.ci_upper != null && (
        <div className="text-[11px] tabular-nums text-ink-light">
          {pair.interval_type ?? "CI"} {Number(pair.ci_lower).toFixed(3)}–
          {Number(pair.ci_upper).toFixed(3)}
        </div>
      )}
      <div className="text-[11px] text-ink-light">{pair.effect_measure}</div>
    </div>
  );
}

function FlagRow({ flags }: { flags: string[] }) {
  return (
    <div>
      <div className="mb-1.5 text-xs font-bold uppercase tracking-wide text-ink-light">
        Flags travelling with this number
      </div>
      <div className="flex flex-wrap gap-1.5">
        {flags.map((f) => (
          <span
            key={f}
            className="rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 font-mono text-[11px] text-amber-900"
          >
            {f}
          </span>
        ))}
      </div>
    </div>
  );
}

/**
 * The protocol's required second analysis. Rendered whether or not it ran, because
 * "SENSITIVITY_REQUIRED and we could not compute one" is the finding in the cross-route
 * case — it says the comparison rests entirely on the link the policy is worried about.
 */
function SensitivityPanel({ sensitivity }: { sensitivity: Pair }) {
  const ran = sensitivity.ran;
  const notEstimable = sensitivity.status === "NOT_ESTIMABLE";
  return (
    <div
      className={`rounded-xl border p-3 ${
        notEstimable
          ? "border-amber-300 bg-amber-50"
          : "border-line bg-canvas"
      }`}
    >
      <div className="mb-1 flex items-center gap-2">
        {notEstimable && (
          <AlertTriangle size={14} className="shrink-0 text-amber-600" strokeWidth={2.4} />
        )}
        <span className="text-xs font-bold uppercase tracking-wide text-ink">
          Sensitivity analysis · {sensitivity.status}
        </span>
      </div>
      <p className={`text-xs leading-relaxed ${notEstimable ? "text-amber-900" : "text-ink-light"}`}>
        {sensitivity.reason}
      </p>
      {ran && sensitivity.estimate != null && (
        <div className="mt-2 flex flex-wrap items-center gap-4 text-xs">
          <span className="text-ink">
            Restricted to <strong>{sensitivity.restricted_to_route}</strong>:{" "}
            <strong className="tabular-nums">
              {Number(sensitivity.estimate).toFixed(3)}
            </strong>
            {sensitivity.ci_lower != null && (
              <span className="tabular-nums text-ink-light">
                {" "}
                ({Number(sensitivity.ci_lower).toFixed(3)}–
                {Number(sensitivity.ci_upper).toFixed(3)})
              </span>
            )}
          </span>
          <span
            className={
              sensitivity.diverges ? "font-bold text-amber-800" : "text-ink-light"
            }
          >
            {sensitivity.diverges
              ? "Intervals do not overlap — that disagreement is the finding"
              : "Intervals overlap"}
          </span>
        </div>
      )}
    </div>
  );
}
