/**
 * The ingestion report — the reason this surface is more than a button.
 *
 * The CLI's value was never that it writes rows; it was the report it prints **before** it
 * writes them. Screened-out studies with reasons, three label buckets each with the advice
 * that actually applies to it, extraction warnings, then both topologies with the nodes the
 * protocol window costs you. A UI that showed a spinner and a row count would delete the
 * review step the dry run exists for.
 *
 * Two rules this file follows deliberately:
 *
 * - **Never print a count above a truncated list.** Every truncated section says how many it
 *   is showing of how many, and ranks by frequency first, so the slice is the informative one
 *   rather than whatever the dict happened to hold.
 * - **A preview is labelled as one.** The badge and an explicit "nothing was written" line,
 *   because a report that looks identical either way is a report that gets misread.
 */
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Eye,
  FileWarning,
  XCircle,
} from "lucide-react";

import type {
  DrugFactReportView,
  IngestJobReport,
  IngestionReportView,
  ReparseReportView,
} from "../api/client";
import { Card, InfoTooltip } from "../components/ui";
import TopologyPanel from "./TopologyPanel";

const SHOW = 25;

export default function IngestReport({
  report,
  scope,
}: {
  report: IngestJobReport;
  scope: Record<string, any> | null;
}) {
  return (
    <div className="space-y-5">
      <ModeBanner report={report} scope={scope} />
      {report.ingestion && <IngestionSection data={report.ingestion} />}
      {report.network && <NetworkSection data={report.network} />}
      {report.drug_facts && <DrugFactsSection data={report.drug_facts} />}
      {report.reparse && <ReparseSection data={report.reparse} />}
      <NextStep report={report} />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Preview vs commit — never ambiguous                                */
/* ------------------------------------------------------------------ */
function ModeBanner({
  report,
  scope,
}: {
  report: IngestJobReport;
  scope: Record<string, any> | null;
}) {
  const summary = scope
    ? Object.entries(scope)
        .filter(([, v]) => v !== null && v !== undefined && !(Array.isArray(v) && !v.length))
        .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : v}`)
        .join(" · ")
    : null;

  if (!report.committed) {
    return (
      <div className="rounded-xl border-2 border-brand-light/60 bg-brand-surface/60 p-4">
        <div className="flex items-start gap-3">
          <Eye size={18} className="mt-0.5 shrink-0 text-brand" strokeWidth={2.2} />
          <div className="text-sm text-ink">
            <p className="flex items-center gap-2 font-bold">
              <span className="rounded-full bg-brand px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-white">
                Preview
              </span>
              Nothing was written.
            </p>
            <p className="mt-1 leading-relaxed text-ink-light">
              No studies, no memberships, no network, and no audit row — the transaction was
              rolled back. Everything below is what a commit <em>would</em> produce from the
              source as it reads right now. Committing re-harvests, so the figures can move.
            </p>
            {summary && (
              <p className="mt-2 font-mono text-[11px] leading-relaxed text-ink-light">
                {summary}
              </p>
            )}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-xl border-2 border-emerald-300 bg-emerald-50 p-4">
      <div className="flex items-start gap-3">
        <CheckCircle2 size={18} className="mt-0.5 shrink-0 text-emerald-600" strokeWidth={2.2} />
        <div className="text-sm text-emerald-900">
          <p className="flex items-center gap-2 font-bold">
            <span className="rounded-full bg-emerald-600 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-white">
              Committed
            </span>
            Written to the corpus.
          </p>
          <p className="mt-1 leading-relaxed">
            Studies landed <code>EXTRACTED</code> or <code>MAPPED</code>, memberships landed{" "}
            <code>PROPOSED</code>, and the network is <code>DRAFT</code>. Nothing is verified,
            so nothing resolves yet.
          </p>
          {summary && (
            <p className="mt-2 font-mono text-[11px] leading-relaxed">{summary}</p>
          )}
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Counts                                                             */
/* ------------------------------------------------------------------ */
function Figure({
  label,
  value,
  tone = "plain",
  tooltip,
}: {
  label: string;
  value: number;
  tone?: "plain" | "good" | "warn";
  tooltip?: string;
}) {
  const cls =
    tone === "good" ? "text-emerald-700" : tone === "warn" ? "text-amber-700" : "text-ink";
  return (
    <div className="rounded-xl border border-line bg-canvas-card p-3">
      <div className="flex items-center gap-1 text-[10px] font-bold uppercase tracking-wide text-ink-light">
        {tooltip && <InfoTooltip content={tooltip} />}
        {label}
      </div>
      <div className={`mt-1 text-xl font-bold tabular-nums ${cls}`}>{value}</div>
    </div>
  );
}

function IngestionSection({ data }: { data: IngestionReportView }) {
  return (
    <Card title={`Ingestion — ${data.indication}`}>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Figure label="discovered" value={data.discovered} />
        <Figure
          label="screened out"
          value={data.screened_out}
          tone="warn"
          tooltip="Screening is the only step that removes real randomised evidence, so it is reported per study with a reason below."
        />
        <Figure label="ingested" value={data.ingested} tone="good" />
        <Figure label="updated" value={data.updated} />
        <Figure
          label="skipped"
          value={data.skipped}
          tooltip="A VERIFIED or REJECTED study is never overwritten — a correction has to arrive as a new version."
        />
        <Figure label="fetch failures" value={data.fetch_failures.length} tone="warn" />
      </div>

      <ReasonRollup rows={data.screened_out_detail} />

      <Bucket
        title="Uncurated treatment labels"
        counts={data.unmapped_treatments}
        sources={data.label_studies}
        advice="Real agents the drug catalog does not know. Add an entry (or an alias) to brands.yaml — until then each one is its own junk node in every network built from this indication."
      />
      <Bucket
        title="Arms whose label names no treatment"
        counts={data.uninformative_arms}
        sources={data.label_studies}
        advice="Not fixable in config: the registry record never said what these arms received. Curation has to read the named study. The study is kept, but the builder refuses it a network — two trials' 'A' arms are not one node."
      />
      <Bucket
        title="Class-level / strategy arms (studies screened OUT)"
        counts={data.class_level_arms}
        sources={data.label_studies}
        advice="These name a drug class or a care strategy, not a molecule. Their whole study is excluded: comparing them to a drug node would assume class equivalence, and pooling two trials' 'Standard Care' invents a common comparator."
        tone="warn"
      />

      <Detail
        title="Studies screened out"
        rows={data.screened_out_detail.map((r) => [r.id, r.reason])}
        note="Named per study rather than only counted: a drug that appears only in screened studies is absent from every bucket above, so without this the census would quietly stop mentioning it."
      />
      <Detail
        title="Fetch failures"
        rows={data.fetch_failures.map((r) => [r.id, r.reason])}
      />
      <Warnings studies={data.studies} />
    </Card>
  );
}

/**
 * Screened-out reasons rolled up by class, above the per-study list.
 *
 * Ranked, never sliced arbitrarily: an alphabetical truncation of rejection reasons once
 * showed a reason true of dozens of rows and hid the only one that needed action.
 */
function ReasonRollup({ rows }: { rows: { id: string; reason: string }[] }) {
  if (!rows.length) return null;
  const byClass: Record<string, number> = {};
  for (const row of rows) {
    const key = row.reason.split(":")[0].trim();
    byClass[key] = (byClass[key] || 0) + 1;
  }
  const ranked = Object.entries(byClass).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  return (
    <div className="mt-4 rounded-xl border border-line bg-slate-50 p-3">
      <div className="mb-2 text-[10px] font-bold uppercase tracking-wide text-ink-light">
        Why they were screened out
      </div>
      <div className="space-y-1">
        {ranked.map(([reason, count]) => (
          <div key={reason} className="flex items-baseline gap-3 text-xs">
            <span className="w-8 shrink-0 text-right font-bold tabular-nums text-ink">
              {count}
            </span>
            <span className="text-ink-light">{reason}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/**
 * One frequency-sorted label bucket with the advice that applies to *this* bucket.
 *
 * Three separate buckets rather than one list of "problem labels", because the three need
 * different actions: a catalog entry, a trip to the source document, and nothing at all.
 */
function Bucket({
  title,
  counts,
  sources,
  advice,
  tone = "plain",
}: {
  title: string;
  counts: Record<string, number>;
  sources: Record<string, string[]>;
  advice: string;
  tone?: "plain" | "warn";
}) {
  const ranked = Object.entries(counts).sort(
    (a, b) => b[1] - a[1] || a[0].localeCompare(b[0]),
  );
  if (!ranked.length) return null;
  const arms = ranked.reduce((sum, [, c]) => sum + c, 0);
  const shown = ranked.slice(0, SHOW);

  return (
    <div
      className={`mt-4 rounded-xl border p-3 ${
        tone === "warn" ? "border-amber-200 bg-amber-50" : "border-line bg-canvas-card"
      }`}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-xs font-bold text-ink">{title}</span>
        <span className="text-[11px] text-ink-light">
          {ranked.length} distinct · {arms} arms
        </span>
      </div>
      <p className="mt-1 text-[11px] leading-relaxed text-ink-light">{advice}</p>
      <div className="mt-2 space-y-1">
        {shown.map(([label, count]) => {
          const studies = sources[label] || [];
          const where =
            studies.slice(0, 2).join(", ") +
            (studies.length > 2 ? ` +${studies.length - 2}` : "");
          return (
            <div key={label} className="flex items-baseline gap-3 text-xs">
              <span className="w-8 shrink-0 text-right font-bold tabular-nums text-ink">
                {count}
              </span>
              <span className="min-w-0 flex-1 truncate font-medium text-ink" title={label}>
                {label}
              </span>
              <span className="shrink-0 font-mono text-[10px] text-ink-light">{where}</span>
            </div>
          );
        })}
      </div>
      {ranked.length > shown.length && (
        <p className="mt-2 text-[11px] text-ink-light">
          Showing the {shown.length} most frequent of {ranked.length}.
        </p>
      )}
    </div>
  );
}

function Detail({
  title,
  rows,
  note,
}: {
  title: string;
  rows: [string, string][];
  note?: string;
}) {
  if (!rows.length) return null;
  const shown = rows.slice(0, SHOW);
  return (
    <div className="mt-4">
      <div className="mb-1.5 flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-xs font-bold text-ink">{title}</span>
        <span className="text-[11px] text-ink-light">
          {rows.length === shown.length
            ? `${rows.length}`
            : `showing ${shown.length} of ${rows.length}`}
        </span>
      </div>
      {note && <p className="mb-2 text-[11px] leading-relaxed text-ink-light">{note}</p>}
      <div className="space-y-1">
        {shown.map(([id, reason]) => (
          <div
            key={id + reason}
            className="flex flex-wrap items-baseline gap-2 rounded-lg border border-line bg-canvas-card px-2.5 py-1.5 text-xs"
          >
            <span className="font-mono text-[11px] font-semibold text-ink">{id}</span>
            <span className="min-w-0 flex-1 text-ink-light">{reason}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function Warnings({ studies }: { studies: IngestionReportView["studies"] }) {
  const flagged = studies.filter((s) => s.warnings.length);
  if (!flagged.length) return null;
  const shown = flagged.slice(0, 15);
  return (
    <div className="mt-4">
      <div className="mb-1.5 flex flex-wrap items-baseline justify-between gap-2">
        <span className="flex items-center gap-1.5 text-xs font-bold text-ink">
          <FileWarning size={13} className="text-amber-600" />
          Studies with extraction warnings
        </span>
        <span className="text-[11px] text-ink-light">
          {flagged.length === shown.length
            ? `${flagged.length}`
            : `showing ${shown.length} of ${flagged.length}`}
        </span>
      </div>
      <p className="mb-2 text-[11px] leading-relaxed text-ink-light">
        Recorded, never resolved here. A warning is what sends a curator to the registry
        record; smoothing it over is how a caveat the extraction was careful to keep gets
        laundered into a clean number.
      </p>
      <div className="space-y-2">
        {shown.map((s) => (
          <div key={s.study_id} className="rounded-lg border border-line bg-canvas-card p-2.5">
            <div className="flex items-baseline justify-between gap-3">
              <span className="font-mono text-[11px] font-semibold text-ink">
                {s.study_id}
              </span>
              <span className="text-[10px] font-bold uppercase text-ink-light">
                {s.action} · {s.verification_status} · {s.arm_count} arms ·{" "}
                {s.outcome_count} rows
              </span>
            </div>
            <ul className="mt-1 space-y-0.5">
              {s.warnings.map((w) => (
                <li key={w} className="text-[11px] leading-relaxed text-amber-800">
                  {w}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Network — both topologies, side by side                            */
/* ------------------------------------------------------------------ */
function NetworkSection({ data }: { data: NonNullable<IngestJobReport["network"]> }) {
  const endpoint = data.endpoint_topology || {};
  const scope = data.protocol_scope;

  return (
    <Card
      title={
        <span className="flex flex-wrap items-center gap-2">
          Network {data.created ? "created" : "refreshed"}
          <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-ink">
            {data.network_id}
          </code>
          <InfoTooltip content="Scope is identity, not filter: indication, endpoint, phase and stratum are what make this network this network, which is why the id is derived from all four." />
        </span>
      }
    >
      {data.overstates_answerable && scope && (
        <div
          role="alert"
          className="mb-4 flex items-start gap-3 rounded-xl border-2 border-amber-300 bg-amber-50 p-4"
        >
          <AlertTriangle size={18} className="mt-0.5 shrink-0 text-amber-600" strokeWidth={2.2} />
          <div className="text-sm text-amber-900">
            <p className="font-bold">This network holds more than its protocol can answer on.</p>
            <p className="mt-1 leading-relaxed">
              The graph below is endpoint-level. Under <code>{scope.protocol_id}</code>'s
              approved window (weeks {scope.approved_time_window.join("–")}) a resolve loses{" "}
              <strong>{scope.nodes_lost_to_window.join(", ") || "no nodes"}</strong>, and a
              comparison naming one of those resolves to an evidence gap. Nothing is filtered:
              the window is one protocol's judgement and can be re-approved without
              re-harvesting anything, so the builder discloses it rather than enforcing a second
              copy of the same rule.
            </p>
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <TopologyPanel
          title="Endpoint-level topology"
          note="Stored. Every study reporting this outcome anywhere inside the endpoint's own allowed window."
          nodes={(endpoint.nodes as string[]) || []}
          routes={{}}
          lost={[]}
          summary={endpoint}
        />
        {scope ? (
          <TopologyPanel
            title="Protocol-scoped topology — what a resolve sees"
            note={`Derived, never stored. Weeks ${scope.approved_time_window.join("–")}.`}
            nodes={(scope.topology?.nodes as string[]) || []}
            routes={{}}
            lost={scope.nodes_lost_to_window}
            summary={scope.topology}
          />
        ) : (
          <Card title="Protocol-scoped topology">
            <p className="text-sm leading-relaxed text-ink-light">
              No protocol governs this network, so there is no approved window to narrow it and
              a resolve will see the same graph. That is different from — and better than —
              reporting an empty graph, which would claim nothing is answerable. Pick a
              protocol to see what an approved window would actually leave.
            </p>
          </Card>
        )}
      </div>

      <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Figure label="studies proposed" value={data.proposed_study_count} tone="good" />
        <Figure label="excluded" value={data.excluded.length} tone="warn" />
        <Figure
          label="out of window"
          value={scope?.studies_out_of_window.length ?? 0}
          tooltip="These stay PROPOSED members on purpose. The window belongs to the protocol, not to the network."
        />
        <Figure label="nodes lost" value={scope?.nodes_lost_to_window.length ?? 0} tone="warn" />
      </div>

      <Detail
        title="Excluded from this network"
        rows={data.excluded.map((r) => [r.study_id, r.reason])}
        note='"Why was my trial not used?" has to be answerable without a second request.'
      />

      <p className="mt-4 flex items-start gap-2 text-xs leading-relaxed text-ink-light">
        <XCircle size={13} className="mt-0.5 shrink-0" />
        Memberships are <code>PROPOSED</code> and the network is <code>DRAFT</code>. Both are
        deliberate: inclusion is a per-analysis clinical judgement and ratification is a
        two-stage human review, so a builder that decided either would be inventing the review
        it exists to prepare for.
      </p>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/*  Drug facts                                                         */
/* ------------------------------------------------------------------ */
const FACT_NOTE: Record<string, string> = {
  INGESTED: "new",
  UPDATED: "same label date, refreshed",
  SUPERSEDED: "new label date; the previous version is kept and marked superseded",
  SKIPPED: "no change made",
  NOT_FOUND: "openFDA returned no usable label",
};

function DrugFactsSection({ data }: { data: DrugFactReportView }) {
  return (
    <Card title="Regulatory labels">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Figure label="requested" value={data.requested} />
        <Figure label="ingested" value={data.ingested} tone="good" />
        <Figure label="updated" value={data.updated} />
        <Figure
          label="superseded"
          value={data.superseded}
          tooltip="A new label date supersedes rather than replaces: the old row is still a true statement about what the label said then."
        />
        <Figure label="skipped" value={data.skipped} />
        <Figure label="not found" value={data.not_found} tone="warn" />
      </div>

      <div className="mt-4 space-y-2">
        {data.facts.map((f) => (
          <div key={f.brand} className="rounded-lg border border-line bg-canvas-card p-3">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <span className="text-sm font-bold text-ink">{f.brand}</span>
              <span className="text-[10px] font-bold uppercase tracking-wide text-ink-light">
                {f.action} — {FACT_NOTE[f.action] || ""}
              </span>
            </div>
            <div className="mt-1 space-y-0.5 text-[11px] text-ink-light">
              {f.fact_id && <div className="font-mono">{f.fact_id}</div>}
              {f.label_updated_at && <div>label updated {f.label_updated_at}</div>}
              {f.supersedes && <div>supersedes {f.supersedes}</div>}
              {f.verification_status && <div>status {f.verification_status}</div>}
              {f.reason && <div className="text-amber-800">{f.reason}</div>}
            </div>
            {!!f.flags.length && (
              <div className="mt-1.5 flex flex-wrap gap-1">
                {f.flags.map((flag) => (
                  <span
                    key={flag}
                    className="rounded bg-amber-50 px-1.5 py-0.5 font-mono text-[10px] text-amber-800"
                  >
                    {flag}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      {data.awaiting_verification > 0 && (
        <p className="mt-4 text-xs leading-relaxed text-ink-light">
          <strong>{data.awaiting_verification}</strong> label(s) await a curator. Question
          generation and every approval, safety and mechanism claim read <em>verified</em>{" "}
          labels only, so nothing downstream changes until they are checked.
        </p>
      )}
      <p className="mt-2 text-xs leading-relaxed text-ink-light">
        The adapter records <code>INDICATIONS_TEXT_NOT_STRUCTURED</code> rather than
        half-parsing the label's indications prose, so an approval question has no list to
        check against yet. Verifying does not change that — structuring the indications is
        pipeline work.
      </p>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/*  Re-parse                                                           */
/* ------------------------------------------------------------------ */
function ReparseSection({ data }: { data: ReparseReportView }) {
  const changed = data.results.filter((r) => r.action !== "SKIPPED");
  const skipped = data.results.filter((r) => r.action === "SKIPPED");
  return (
    <Card title="Re-parse">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Figure label="studies read" value={data.studies} />
        <Figure label="re-extracted" value={changed.length} tone="good" />
        <Figure label="skipped" value={skipped.length} />
        <Figure
          label="already decided"
          value={data.skipped_because_decided}
          tooltip="VERIFIED and REJECTED rows are skipped by design — a maintenance routine does not step around the verification lifecycle."
        />
      </div>

      {data.skipped_because_decided > 0 && (
        <p className="mt-3 text-xs leading-relaxed text-ink-light">
          <strong>{data.skipped_because_decided}</strong> of these were skipped because someone
          has already decided them. That is the design, not a failure: applying a parser fix to
          a verified row needs a deliberate, audited reset first.
        </p>
      )}

      <Detail
        title="Per study"
        rows={data.results.map((r) => [
          r.study_id,
          `${r.action}${r.reason ? ` — ${r.reason}` : ""}`,
        ])}
      />
      <Warnings studies={data.results} />
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/*  What to do next — curation is the gate that unblocks output        */
/* ------------------------------------------------------------------ */
function NextStep({ report }: { report: IngestJobReport }) {
  if (!report.committed) {
    return (
      <p className="text-xs leading-relaxed text-ink-light">
        Nothing was written, so there is nothing to curate yet. Re-run with{" "}
        <strong>Preview only</strong> switched off to apply this.
      </p>
    );
  }

  const networkId = report.network?.network_id;
  return (
    <Card className="border-2 border-brand-light/50">
      <p className="text-sm font-bold text-ink">Curation is the next step, and it is the gate.</p>
      <p className="mt-1 text-xs leading-relaxed text-ink-light">
        Evidence gathering skips an unverified study <em>even in exploratory mode</em>, so a
        corpus nobody has curated yields no number at all — approved protocol or otherwise.
        Verifying is a data-accuracy check against the retained source, one study at a time.
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        <Link
          to="/evidence/studies"
          className="flex items-center gap-1.5 rounded-xl bg-brand px-3 py-2 text-sm font-bold text-white transition-colors hover:bg-brand-dark"
        >
          Curate studies <ArrowRight size={14} strokeWidth={2.4} />
        </Link>
        {networkId && (
          <Link
            to="/evidence/networks"
            className="flex items-center gap-1.5 rounded-xl border border-line bg-canvas-card px-3 py-2 text-sm font-bold text-ink transition-colors hover:border-brand-light"
          >
            Open {networkId} <ArrowRight size={14} strokeWidth={2.4} />
          </Link>
        )}
      </div>
    </Card>
  );
}
