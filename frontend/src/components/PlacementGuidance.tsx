import { Check, MapPin, Search } from "lucide-react";
import { InfoTooltip } from "./ui";
import type { PlacementGuidance } from "../api/client";

/**
 * "Where to publish / earn a citation" — the *where* that complements the recommendation's
 * *what*. Rendered on both the GEO recommendation card and the Activation & Impact evidence
 * panel. Everything here is derived from the classified citation graph (observed AI behaviour),
 * so it is a data-backed suggestion, not a media plan. Renders nothing when guidance is empty.
 */
export function PlacementGuidancePanel({
  placement,
  className = "",
}: {
  placement?: PlacementGuidance | null;
  className?: string;
}) {
  if (!placement) return null;
  const earn = placement.earn_citations ?? [];
  const gaps = placement.preferred_gaps ?? [];
  const queries = placement.target_queries ?? [];
  if (!earn.length && !gaps.length && !queries.length) return null;

  return (
    <div className={`space-y-2.5 rounded-xl border border-brand-light/40 bg-brand-surface/40 p-3.5 ${className}`}>
      <p className="inline-flex items-center gap-1.5 text-xs font-extrabold uppercase tracking-wide text-brand-dark">
        <MapPin size={14} /> Where to publish / earn a citation
        <InfoTooltip content="Authoritative sources the AI already trusts for this topic, based on the citations it actually used. Getting your content published or cited on these raises the odds the AI mentions your brand. A suggestion from observed citations — not a media plan." />
      </p>

      {earn.length > 0 && (
        <div>
          <p className="mb-1 text-[11px] font-bold uppercase tracking-wide text-ink-light">Trusted domains to target</p>
          <ul className="flex flex-wrap gap-1.5">
            {earn.map((d, i) => (
              <li
                key={i}
                className="inline-flex items-center gap-1 rounded-lg border border-line bg-canvas-card px-2 py-1 text-xs font-semibold text-ink"
                title={[d.display_category, d.response_count != null ? `cited in ${d.response_count} answers` : null]
                  .filter(Boolean)
                  .join(" · ")}
              >
                {d.is_preferred && <Check size={12} className="text-teal-700" />}
                {d.domain}
                {d.is_preferred && <span className="text-[10px] font-bold uppercase text-teal-700">MA-preferred</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {gaps.length > 0 && (
        <div>
          <p className="mb-1 inline-flex items-center gap-1 text-[11px] font-bold uppercase tracking-wide text-ink-light">
            MA-preferred sources the AI is ignoring
            <InfoTooltip content="Medical-Affairs preferred domains the AI rarely or never cites for this topic. Earning presence on these closes a durable evidence gap." />
          </p>
          <ul className="space-y-0.5">
            {gaps.map((g, i) => (
              <li key={i} className="truncate text-xs font-medium text-ink-light">
                • {g.domain}
                {g.absence_pct != null && <span> — absent in {g.absence_pct}% of checks</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {queries.length > 0 && (
        <div>
          <p className="mb-1 inline-flex items-center gap-1 text-[11px] font-bold uppercase tracking-wide text-ink-light">
            <Search size={12} /> Search phrasings to optimise for
            <InfoTooltip content="The actual search terms the AI ran while answering this kind of question. Reflect these phrasings in the asset's headings/FAQ so it is more likely to be retrieved and cited." />
          </p>
          <ul className="flex flex-wrap gap-1.5">
            {queries.map((q, i) => (
              <li key={i} className="inline-flex items-center rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-ink">
                {q.query}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
