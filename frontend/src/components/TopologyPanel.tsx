/**
 * One presentation of one topology, shared by every surface that shows a network's shape.
 *
 * Extracted out of `pages/EvidenceNetworks.tsx` when the ingest report needed the same panel.
 * A second copy would eventually disagree with the first about whether a graph is connected
 * or what "lost to the window" means — the same argument that keeps `evidence/topology.py`
 * as the single implementation on the backend.
 *
 * `summary` is optional and additive: pass a `topology.summary()` payload to also show the
 * facts the CLI prints (components, independent loops, multi-arm, simple star). Call sites
 * that omit it render exactly as they did before this was extracted.
 */
import { Card } from "./ui";

export default function TopologyPanel({
  title,
  note,
  nodes,
  routes,
  lost,
  summary,
}: {
  title: string;
  note: string;
  nodes: string[];
  routes: Record<string, string>;
  lost: string[];
  summary?: Record<string, any> | null;
}) {
  const lostSet = new Set(lost);
  return (
    <Card title={title}>
      <p className="mb-3 text-xs leading-relaxed text-ink-light">{note}</p>
      <div className="mb-3 text-2xl font-bold tabular-nums text-ink">
        {nodes.length}
        <span className="ml-1 text-xs font-semibold text-ink-light">nodes</span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {nodes.map((n) => (
          <span
            key={n}
            className="rounded-full border border-line bg-canvas-card px-2 py-0.5 text-[11px] font-semibold text-ink"
          >
            {n}
            {routes[n] && (
              <span className="ml-1 text-[9px] font-bold text-ink-light">
                {routes[n]}
              </span>
            )}
          </span>
        ))}
      </div>
      {summary && <TopologyFacts summary={summary} />}
      {!!lost.length && (
        <div className="mt-3 border-t border-line pt-3">
          <div className="mb-1.5 text-[10px] font-bold uppercase tracking-wide text-amber-700">
            Lost to the window
          </div>
          <div className="flex flex-wrap gap-1.5">
            {[...lostSet].map((n) => (
              <span
                key={n}
                className="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-semibold text-amber-900 line-through"
              >
                {n}
              </span>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

/**
 * The facts the CLI prints under each topology.
 *
 * `loops (independent)` is reported separately from `loops (all)` on purpose, and multi-arm
 * separately again: **zero independent loops means inconsistency cannot be tested**, while a
 * multi-arm trial means netmeta is *required*. Collapsing the three into "has loops" is a live
 * trap — the production PsA network has one loop, zero independent loops, and is not a star.
 */
function TopologyFacts({ summary }: { summary: Record<string, any> }) {
  const rows: [string, React.ReactNode][] = [
    ["edges", summary.edge_count ?? "—"],
    ["studies", summary.study_count ?? "—"],
    [
      "connected",
      summary.is_connected ? "yes" : `no · ${summary.component_count ?? "?"} components`,
    ],
    ["loops (all)", summary.loop_count ?? "—"],
    ["loops (independent)", summary.independent_loop_count ?? "—"],
    ["multi-arm studies", summary.has_multi_arm_studies ? "yes" : "no"],
    ["simple star", summary.is_simple_star ? "yes" : "no"],
  ];
  return (
    <div className="mt-3 space-y-1 border-t border-line pt-3">
      {rows.map(([label, value]) => (
        <div key={label} className="flex items-baseline justify-between gap-3 text-xs">
          <span className="font-semibold text-ink-light">{label}</span>
          <span className="tabular-nums text-ink">{value}</span>
        </div>
      ))}
      {summary.independent_loop_count === 0 && (
        <p className="pt-1.5 text-[11px] leading-relaxed text-ink-light">
          No independent loops, so inconsistency cannot be <em>assessed</em> on this network.
          That is a different fact from having no loops at all, and from carrying a multi-arm
          trial — which requires netmeta rather than Bucher.
        </p>
      )}
    </div>
  );
}
