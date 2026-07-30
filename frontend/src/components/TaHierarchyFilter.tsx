import React from "react";
import { ChevronDown, X } from "lucide-react";
import {
  AREA_OPTIONS,
  TA_HIERARCHY,
  indicationsForArea,
  diseasesForArea,
  diseasesForIndication,
  indicationForDisease,
} from "../lib/taxonomy";
import type { TaFilters } from "../api/client";

export interface TaSelection {
  area: string;
  indication: string;
  brand: string;
  disease: string;
}

interface Props {
  value: TaSelection;
  onChange: (next: TaSelection, filters: TaFilters) => void;
  className?: string;
}

const SELECT_BASE =
  "appearance-none bg-white border border-slate-200 rounded-lg pl-3 pr-8 py-2 text-sm font-medium text-ink shadow-sm " +
  "focus:outline-none focus:ring-2 focus:ring-brand/30 focus:border-brand transition-colors cursor-pointer " +
  "disabled:opacity-40 disabled:cursor-not-allowed";

const SELECT_ACTIVE = "border-brand/60 text-brand bg-brand/5";

function StyledSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (v: string) => void;
}) {
  const isActive = !!value;
  return (
    <div className="flex flex-col gap-0.5">
      <label className="text-xs font-bold uppercase tracking-widest text-ink pl-0.5">
        {label}
      </label>
      <div className="relative">
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className={`${SELECT_BASE} ${isActive ? SELECT_ACTIVE : ""}`}
        >
          <option value="">All</option>
          {options.map((opt) => (
            <option key={opt} value={opt}>
              {opt}
            </option>
          ))}
        </select>
        <ChevronDown
          className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-ink-muted"
        />
      </div>
    </div>
  );
}

export function TaHierarchyFilter({ value, onChange, className = "" }: Props) {
  // Indication options (= brands.yaml keys) — filtered by area if selected, otherwise all
  const indicationOptions: string[] = value.area
    ? indicationsForArea(value.area).map((i) => i.label)
    : [...new Set(TA_HIERARCHY.flatMap((a) => a.indications.map((i) => i.label)))];

  // Disease options (= granular conditions) — distinct from indications
  const diseaseOptions: string[] = (() => {
    if (value.indication) return [...diseasesForIndication(value.indication)].sort();
    if (value.area) return [...diseasesForArea(value.area)].sort();
    return [...new Set(TA_HIERARCHY.flatMap((a) => a.indications.flatMap((i) => i.diseases)))].sort();
  })();

  // Brand options — filtered by indication/area if selected, otherwise all
  const brandOptions: string[] = (() => {
    const allInds = TA_HIERARCHY.flatMap((a) => a.indications);
    if (value.indication) {
      return allInds.find((i) => i.label === value.indication)?.brands ?? [];
    }
    if (value.area) {
      return [...new Set(indicationsForArea(value.area).flatMap((i) => i.brands))];
    }
    return [...new Set(allInds.flatMap((i) => i.brands))];
  })();

  function toFilters(sel: TaSelection): TaFilters {
    // The stored Response.therapeutic_area holds the brands.yaml key.
    // - indication selected -> use its key directly
    // - area only           -> send the area name; backend expands area -> child keys
    // - disease only        -> resolve to its parent indication key
    let ta: string | undefined;
    if (sel.indication) {
      ta = sel.indication;
    } else if (sel.disease) {
      ta = indicationForDisease(sel.disease, sel.area) ?? sel.area ?? undefined;
    } else if (sel.area) {
      ta = sel.area;
    }
    return {
      therapeutic_area: ta,
      brand: sel.brand || undefined,
    };
  }

  function handleArea(area: string) {
    const next: TaSelection = { area, indication: "", brand: "", disease: "" };
    onChange(next, toFilters(next));
  }

  function handleIndication(ind: string) {
    const next: TaSelection = { area: value.area, indication: ind, brand: "", disease: "" };
    onChange(next, toFilters(next));
  }

  function handleBrand(brand: string) {
    const next: TaSelection = { ...value, brand };
    onChange(next, toFilters(next));
  }

  function handleDisease(disease: string) {
    const next: TaSelection = { ...value, disease };
    onChange(next, toFilters(next));
  }

  const hasFilter = !!(value.area || value.indication || value.brand || value.disease);

  return (
    <div className={`flex items-end gap-2 flex-wrap ${className}`}>
      {/* Dropdown 1 — Therapeutic Area */}
      <StyledSelect
        label="Therapeutic Area"
        value={value.area}
        options={AREA_OPTIONS}
        onChange={handleArea}
      />

      {/* Dropdown 2 — Indication */}
      <StyledSelect
        label="Indication"
        value={value.indication}
        options={indicationOptions}
        onChange={handleIndication}
      />

      {/* Dropdown 3 — Disease(s) Treated */}
      <StyledSelect
        label="Disease(s) Treated"
        value={value.disease}
        options={diseaseOptions}
        onChange={handleDisease}
      />

      {/* Dropdown 4 — Brand */}
      <StyledSelect
        label="Brand"
        value={value.brand}
        options={brandOptions}
        onChange={handleBrand}
      />

      {/* Clear */}
      {hasFilter && (
        <button
          onClick={() => onChange({ area: "", indication: "", brand: "", disease: "" }, {})}
          className="mb-0.5 inline-flex items-center gap-1 self-end rounded-lg border border-slate-200 bg-white px-2.5 py-2 text-xs text-ink-muted hover:text-red-500 hover:border-red-300 transition-colors shadow-sm"
          title="Clear filters"
        >
          <X className="w-3 h-3" />
          Clear
        </button>
      )}
    </div>
  );
}

/** Small active-filter badge shown below the filter row */
export function TaFilterBadge({ value }: { value: TaSelection }) {
  const parts: string[] = [];
  if (value.area) parts.push(value.area);
  if (value.indication) parts.push(value.indication);
  if (value.disease) parts.push(value.disease);
  if (value.brand) parts.push(value.brand);
  if (!parts.length) return null;
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-brand/10 border border-brand/20 px-2.5 py-0.5 text-[11px] font-medium text-brand">
      {parts.join(" › ")}
    </span>
  );
}
