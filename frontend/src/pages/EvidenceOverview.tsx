import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  ArrowRight,
  Database,
  FlaskConical,
  Network,
  Pill,
} from "lucide-react";

import { api, type EvidenceOverview as Overview } from "../api/client";
import {
  AnimatedCard,
  Card,
  EmptyState,
  PageHeader,
  Spinner,
  Stat,
} from "../components/ui";

/**
 * The evidence store's landing page.
 *
 * It leads with **canonical endpoint coverage**, not with how many rows were ingested. A row
 * with no canonical outcome id is invisible to every network, so "6342 results" without the
 * ratio beside it is true and thoroughly misleading. The headline sentence says which of the
 * two situations the store is actually in.
 */
export default function EvidenceOverviewPage() {
  const [data, setData] = useState<Overview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .evidenceOverview()
      .then(setData)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

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
  if (!data) return <EmptyState message="No evidence store data." />;

  const coverage = data.outcome_results.canonical_coverage_pct;
  const unusable =
    data.outcome_results.total - data.outcome_results.with_canonical_outcome;
  const thin = coverage < 50;

  return (
    <div>
      <PageHeader
        title="Evidence Store"
        subtitle="Trials, arms, endpoint results, networks and label facts — as ingested, with every caveat kept."
      />

      {/* FR-612-style headline: one sentence, the load-bearing numbers in bold. */}
      <AnimatedCard className="mb-6">
        <div className="rounded-2xl border border-line bg-brand-surface p-5">
          <div className="mb-1 text-[11px] font-bold uppercase tracking-wide text-brand-dark">
            The big picture
          </div>
          <p className="text-sm leading-relaxed text-ink">
            The store holds <strong>{data.studies.total.toLocaleString()}</strong>{" "}
            studies and{" "}
            <strong>{data.outcome_results.total.toLocaleString()}</strong> endpoint
            results, but only{" "}
            <strong>
              {data.outcome_results.with_canonical_outcome.toLocaleString()} (
              {coverage}%)
            </strong>{" "}
            carry a canonical endpoint. A result without one is invisible to every
            network, so{" "}
            <strong>{unusable.toLocaleString()}</strong> rows cannot currently
            contribute to any comparison.
          </p>
        </div>
      </AnimatedCard>

      {thin && data.outcome_results.total > 0 && (
        <div
          role="alert"
          className="mb-6 flex items-start gap-3 rounded-xl border-2 border-amber-300 bg-amber-50 p-4"
        >
          <AlertTriangle
            size={18}
            className="mt-0.5 shrink-0 text-amber-600"
            strokeWidth={2.2}
          />
          <div className="text-sm text-amber-900">
            <p className="font-bold">Endpoint mapping is the binding constraint.</p>
            <p className="mt-1 leading-relaxed">
              Most harvested measurements resolve to no canonical endpoint, which is why
              networks look thin. Ambiguity is never guessed — a title naming two
              endpoints returns no match — so this is a signal about the matcher
              vocabulary rather than a property of the trials.
            </p>
          </div>
        </div>
      )}

      <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label="Studies ingested"
          value={data.studies.total.toLocaleString()}
          sub={`${Object.keys(data.studies.by_indication).length} indications`}
          icon={<FlaskConical size={16} />}
        />
        <Stat
          label="Usable endpoint results"
          value={`${coverage}%`}
          sub={`${data.outcome_results.with_canonical_outcome.toLocaleString()} of ${data.outcome_results.total.toLocaleString()} rows`}
          icon={<Database size={16} />}
          tooltip="Share of outcome rows that resolved to a canonical endpoint id. Only these can join a network."
        />
        <Stat
          label="Networks"
          value={data.networks.total.toLocaleString()}
          sub={`${data.networks.connected} connected`}
          icon={<Network size={16} />}
        />
        <Stat
          label="Drug facts"
          value={data.drug_facts.total.toLocaleString()}
          sub="current label versions"
          icon={<Pill size={16} />}
          tooltip="Label-derived facts. Independent of the NMA stack — these stay valuable even where a network is disconnected."
        />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card title="Studies by verification">
          <StatusList counts={data.studies.by_verification_status} />
          <p className="mt-3 text-xs leading-relaxed text-ink-light">
            Ingestion never verifies its own output. Rows land <code>EXTRACTED</code>,
            or <code>MAPPED</code> when every endpoint resolved — a statement of fact,
            not a judgement about accuracy.
          </p>
        </Card>

        <Card title="Networks by ratification">
          <StatusList counts={data.networks.by_ratification_status} />
          <p className="mt-3 text-xs leading-relaxed text-ink-light">
            <code>RATIFIED</code> is reachable only through both review stages in
            order. Until a network reaches it, every computed result is exploratory and
            not releasable.
          </p>
        </Card>

        <Card title="Studies by indication">
          <StatusList counts={data.studies.by_indication} />
        </Card>
      </div>

      <div className="mt-6 flex flex-wrap gap-3">
        <JumpLink to="/evidence/networks" label="Browse networks" />
        <JumpLink to="/evidence/studies" label="Browse studies" />
        <JumpLink to="/evidence/governance" label="Review governance" />
        <JumpLink to="/evidence/competitors" label="Competitor discovery" />
      </div>
    </div>
  );
}

function StatusList({ counts }: { counts: Record<string, number> }) {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  if (!entries.length) {
    return <p className="text-sm text-ink-light">Nothing ingested yet.</p>;
  }
  const max = Math.max(...entries.map(([, n]) => n));
  return (
    <div className="space-y-2">
      {entries.map(([label, n]) => (
        <div key={label} className="flex items-center gap-3">
          <span className="w-40 shrink-0 truncate text-xs font-semibold text-ink">
            {label}
          </span>
          <div className="h-2 flex-1 overflow-hidden rounded-full bg-slate-100">
            <div
              className="h-full rounded-full bg-brand-light"
              style={{ width: `${max ? (n / max) * 100 : 0}%` }}
            />
          </div>
          <span className="w-12 shrink-0 text-right text-xs font-bold tabular-nums text-ink">
            {n.toLocaleString()}
          </span>
        </div>
      ))}
    </div>
  );
}

function JumpLink({ to, label }: { to: string; label: string }) {
  return (
    <Link
      to={to}
      className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-canvas-card px-3 py-2 text-sm font-semibold text-brand-dark transition-colors hover:border-brand-light/50"
    >
      {label}
      <ArrowRight size={14} strokeWidth={2.4} />
    </Link>
  );
}
