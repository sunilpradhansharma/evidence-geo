import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, ArrowLeft, Check, Loader2, Plus, Sparkles, X } from "lucide-react";
import {
  api,
  BrandCreated,
  BrandRejectedError,
  BrandResolveResult,
  CompetitorSuggestion,
  TaxonomyAreaChoice,
} from "../api/client";
import { Spinner, InfoTooltip } from "./ui";
import { ADMINISTRATION_ROUTES, DISEASE_OPTIONS, useTaxonomy } from "../lib/taxonomy";

/**
 * Add a brand to the taxonomy.
 *
 * Four steps, and the ordering is the safeguard rather than a UX preference:
 *
 * 1. **Identity check first.** A name is resolved against what is already curated before
 *    anything is drafted, so the common case — a typo, or an alias of a brand already
 *    present — is caught before a model is involved at all.
 * 2. **The model drafts, the analyst owns.** Every drafted field is editable and labelled as
 *    a draft. Route is a select over the closed vocabulary, never free text.
 * 3. **Competitors arrive unticked.** A suggestion is not an approval, and each carries the
 *    reason that gets stored with it.
 * 4. **Save can be refused.** The backend re-validates and returns reasons, which are shown
 *    verbatim — an alias collision is a specific, fixable problem, not a failure.
 */
const STEPS = ["Name", "Details", "Indications", "Review"] as const;

/** Sentinel for the "create an area" choice. Bracketed so it cannot collide with a real key. */
const NEW_AREA = "__new_therapeutic_area__";

interface DiseaseEntry {
  disease: string;
  /** Not in the taxonomy yet, so it will be created — with DRAFT endpoints. */
  isNew: boolean;
  area: string;
  therapeuticAreaKey: string;
  canonicalOutcomes: string[];
  outcomesLoading: boolean;
  suggestions: CompetitorSuggestion[];
  ticked: Set<string>;
  loading: boolean;
}

export function AddBrandModal({
  onClose, onAdded,
}: {
  onClose: () => void;
  onAdded: (result: BrandCreated) => void;
}) {
  const [step, setStep] = useState(0);
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);

  // Keeps the route options and disease list current if the taxonomy is re-hydrated while
  // the modal is open.
  useTaxonomy();
  const [areas, setAreas] = useState<TaxonomyAreaChoice[]>([]);

  // Step 1
  const [name, setName] = useState("");
  const [resolving, setResolving] = useState(false);
  const [resolved, setResolved] = useState<BrandResolveResult | null>(null);

  // Step 2
  const [drafting, setDrafting] = useState(false);
  const [draftUsed, setDraftUsed] = useState(false);
  const [generic, setGeneric] = useState("");
  const [company, setCompany] = useState("");
  const [drugClass, setDrugClass] = useState("");
  const [route, setRoute] = useState("");
  const [aliases, setAliases] = useState("");
  const [taKey, setTaKey] = useState("");
  // A therapeutic area this addition creates. Only ever sent alongside the brand being
  // filed into it, so a new area can never exist empty.
  const [newArea, setNewArea] = useState<{ ta_key: string; area: string } | null>(null);

  // Step 3
  const [diseases, setDiseases] = useState<DiseaseEntry[]>([]);
  const [reviewer, setReviewer] = useState("");

  useEffect(() => {
    api.taxonomyAreas().then((r) => setAreas(r.areas)).catch(() => setAreas([]));
  }, []);

  // Empty for an area being created — it has no indications yet, so step 3 offers only the
  // "add an indication" input.
  const areaDiseases = useMemo(
    () => areas.find((a) => a.ta_key === taKey)?.diseases ?? [],
    [areas, taKey],
  );

  async function runResolve(override?: string) {
    const target = (override ?? name).trim();
    if (!target) return;
    setResolving(true);
    setResolved(null);
    try {
      setResolved(await api.resolveBrand(target));
    } catch {
      setResolved({ status: "novel", typed: target });
    } finally {
      setResolving(false);
    }
  }

  /** Adopt the model's spelling correction and re-check it.
   *
   * Re-resolving is the point, not a formality: the corrected spelling may be a brand that
   * IS curated, which is a different outcome from the novel one that produced the suggestion.
   */
  function useCorrection(corrected: string) {
    setName(corrected);
    runResolve(corrected);
  }

  async function runDraft() {
    setDrafting(true);
    try {
      const d = await api.draftBrand(name.trim());
      if (d.available) {
        setGeneric(d.generic ?? "");
        setCompany(d.company ?? "");
        setDrugClass(d.drug_class ?? "");
        setRoute(d.administration_route ?? "");
        setAliases((d.aliases ?? []).join(", "));
        setDraftUsed(true);
      }
    } catch {
      // A model failure leaves the form blank and editable, which is still a working path.
    } finally {
      setDrafting(false);
    }
  }

  function newEntry(disease: string, isNew: boolean): DiseaseEntry {
    return {
      disease,
      isNew,
      // Falls back to the area being created, which is not in `areas` yet because that list
      // came from the server before it existed.
      area: areas.find((a) => a.ta_key === taKey)?.area ?? newArea?.area ?? "",
      therapeuticAreaKey: taKey,
      canonicalOutcomes: [],
      outcomesLoading: isNew,
      suggestions: [],
      ticked: new Set(),
      loading: true,
    };
  }

  function toggleDisease(disease: string) {
    setDiseases((prev) => {
      if (prev.some((d) => d.disease === disease)) {
        return prev.filter((d) => d.disease !== disease);
      }
      loadSuggestions(disease);
      return [...prev, newEntry(disease, !DISEASE_OPTIONS.includes(disease))];
    });
  }

  /** Add an indication the taxonomy does not have yet.
   *
   * Its endpoints are drafted by the model from the defined vocabulary, which is why it saves
   * as DRAFT and stays out of the evidence programme until a human verifies it. Comparison
   * coverage and question generation work with it immediately.
   */
  function addNewDisease(raw: string) {
    const disease = raw.trim();
    if (!disease) return;
    // Typing the name of an existing indication is a selection, not a creation — treating it
    // as new would try to redefine endpoints that are already verified.
    const known = areaDiseases.some((d) => d.toLowerCase() === disease.toLowerCase())
      || DISEASE_OPTIONS.some((d) => d.toLowerCase() === disease.toLowerCase());
    if (diseases.some((d) => d.disease.toLowerCase() === disease.toLowerCase())) return;

    setDiseases((prev) => [...prev, newEntry(disease, !known)]);
    loadSuggestions(disease);
    if (!known) loadOutcomes(disease);
  }

  async function loadOutcomes(disease: string) {
    try {
      const draft = await api.draftOutcomes(disease);
      setDiseases((prev) => prev.map((d) =>
        d.disease === disease
          ? { ...d, canonicalOutcomes: draft.canonical_outcomes ?? [], outcomesLoading: false }
          : d
      ));
    } catch {
      setDiseases((prev) => prev.map((d) =>
        d.disease === disease ? { ...d, outcomesLoading: false } : d
      ));
    }
  }

  async function loadSuggestions(disease: string) {
    try {
      const { competitors } = await api.draftCompetitors(name.trim(), disease);
      setDiseases((prev) => prev.map((d) =>
        d.disease === disease ? { ...d, suggestions: competitors, loading: false } : d
      ));
    } catch {
      setDiseases((prev) => prev.map((d) =>
        d.disease === disease ? { ...d, loading: false } : d
      ));
    }
  }

  function toggleCompetitor(disease: string, competitor: string) {
    setDiseases((prev) => prev.map((d) => {
      if (d.disease !== disease) return d;
      const ticked = new Set(d.ticked);
      ticked.has(competitor) ? ticked.delete(competitor) : ticked.add(competitor);
      return { ...d, ticked };
    }));
  }

  async function save() {
    setSaving(true);
    setErrors([]);
    try {
      const result = await api.createBrand({
        name: name.trim(),
        therapeutic_area_key: taKey,
        new_therapeutic_area: newArea,
        generic: generic.trim() || null,
        company: company.trim() || null,
        drug_class: drugClass.trim() || null,
        administration_route: route || null,
        aliases: aliases.split(",").map((a) => a.trim()).filter(Boolean),
        reviewer: reviewer.trim() || "unknown",
        diseases: diseases.map((d) => ({
          disease: d.disease,
          area: d.isNew ? d.area : null,
          therapeutic_area_key: d.isNew ? d.therapeuticAreaKey : null,
          canonical_outcomes: d.isNew ? d.canonicalOutcomes : [],
          // Only what was ticked. The rest of the suggestions are discarded here.
          competitors: d.suggestions
            .filter((c) => d.ticked.has(c.name))
            .map((c) => ({ name: c.name, note: c.reason || null })),
        })),
      });
      onAdded(result);
    } catch (e) {
      setErrors(
        e instanceof BrandRejectedError ? e.reasons : ["Could not add the brand."]
      );
    } finally {
      setSaving(false);
    }
  }

  const canLeaveName = resolved?.status === "novel" || resolved?.status === "near_matches";
  const canLeaveDetails = Boolean(taKey);
  const canSave = diseases.length > 0 && !saving;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
    >
      <div
        className="flex max-h-[88vh] w-full max-w-2xl flex-col rounded-2xl bg-canvas-card p-6 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <h3 className="flex items-center gap-2 text-sm font-bold text-ink">
            <Plus size={15} className="text-brand-light" /> Add brand
            <InfoTooltip content="Adds a monitored brand to the taxonomy. It becomes selectable and starts producing comparison coverage cells immediately. Suggestions are model-generated; nothing is saved unless you accept it." />
          </h3>
          <button onClick={onClose} className="text-ink-light hover:text-ink">
            <X size={16} />
          </button>
        </div>

        <div className="mb-5 flex items-center gap-1.5">
          {STEPS.map((label, i) => (
            <div key={label} className="flex flex-1 items-center gap-1.5">
              <div
                className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[11px] font-bold ${
                  i < step ? "bg-emerald-500 text-white"
                    : i === step ? "bg-brand-light text-white"
                    : "bg-slate-100 text-ink-light"
                }`}
              >
                {i < step ? <Check size={12} /> : i + 1}
              </div>
              <span className={`text-[11px] font-semibold ${i === step ? "text-ink" : "text-ink-light"}`}>
                {label}
              </span>
              {i < STEPS.length - 1 && <div className="h-px flex-1 bg-line" />}
            </div>
          ))}
        </div>

        <div className="min-h-[280px] flex-1 overflow-y-auto pr-1">
          {step === 0 && (
            <StepName
              name={name} setName={setName} resolving={resolving}
              resolved={resolved} onResolve={() => runResolve()}
              onUseCorrection={useCorrection}
            />
          )}
          {step === 1 && (
            <StepDetails
              name={name} drafting={drafting} draftUsed={draftUsed} onDraft={runDraft}
              generic={generic} setGeneric={setGeneric}
              company={company} setCompany={setCompany}
              drugClass={drugClass} setDrugClass={setDrugClass}
              route={route} setRoute={setRoute} routes={ADMINISTRATION_ROUTES}
              aliases={aliases} setAliases={setAliases}
              taKey={taKey} setTaKey={setTaKey} areas={areas}
              newArea={newArea} setNewArea={setNewArea}
            />
          )}
          {step === 2 && (
            <StepIndications
              areaDiseases={areaDiseases} diseases={diseases}
              onToggleDisease={toggleDisease} onToggleCompetitor={toggleCompetitor}
              onAddNewDisease={addNewDisease}
            />
          )}
          {step === 3 && (
            <StepReview
              name={name} generic={generic} company={company} drugClass={drugClass}
              route={route} taKey={taKey} diseases={diseases} newArea={newArea}
              reviewer={reviewer} setReviewer={setReviewer} errors={errors}
            />
          )}
        </div>

        <div className="mt-5 flex items-center justify-between border-t border-line pt-4">
          <button
            onClick={() => setStep((s) => Math.max(0, s - 1))}
            disabled={step === 0}
            className="flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-semibold text-ink-light hover:bg-slate-50 disabled:opacity-40"
          >
            <ArrowLeft size={13} /> Back
          </button>

          {step < STEPS.length - 1 ? (
            <button
              onClick={() => {
                if (step === 0 && !draftUsed) runDraft();
                setStep((s) => s + 1);
              }}
              disabled={(step === 0 && !canLeaveName) || (step === 1 && !canLeaveDetails)}
              className="rounded-lg bg-brand-dark px-4 py-2 text-xs font-semibold text-white hover:bg-brand-dark/90 disabled:opacity-40"
            >
              Continue
            </button>
          ) : (
            <button
              onClick={save}
              disabled={!canSave}
              className="flex items-center gap-2 rounded-lg bg-brand-dark px-4 py-2 text-xs font-semibold text-white hover:bg-brand-dark/90 disabled:opacity-40"
            >
              {saving && <Loader2 size={13} className="animate-spin" />}
              Add {name.trim() || "brand"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function StepName({
  name, setName, resolving, resolved, onResolve, onUseCorrection,
}: {
  name: string; setName: (v: string) => void; resolving: boolean;
  resolved: BrandResolveResult | null; onResolve: () => void;
  onUseCorrection: (corrected: string) => void;
}) {
  return (
    <div className="space-y-4">
      <div>
        <label className="mb-1.5 block text-[11px] font-bold uppercase tracking-wide text-ink-light">
          Brand name
        </label>
        <div className="flex gap-2">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && onResolve()}
            placeholder="e.g. Tremfya"
            className="flex-1 rounded-lg border border-line px-3 py-2 text-sm focus:border-brand-light focus:outline-none"
            autoFocus
          />
          <button
            onClick={onResolve}
            disabled={!name.trim() || resolving}
            className="rounded-lg bg-slate-100 px-3 py-2 text-xs font-semibold text-ink hover:bg-slate-200 disabled:opacity-40"
          >
            {resolving ? <Spinner size={14} /> : "Check"}
          </button>
        </div>
        <p className="mt-1.5 text-[11px] text-ink-light">
          Checked against the curated taxonomy first — no model call, so a typo of an existing
          brand is caught the same way every time. A name that matches nothing is then spell
          checked against real drugs, which is how a typo of an untracked one gets caught.
        </p>
      </div>

      {resolved?.status === "exact_match" && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-3">
          <div className="flex items-start gap-2">
            <AlertTriangle size={14} className="mt-0.5 shrink-0 text-amber-600" />
            <div className="text-xs text-amber-900">
              <div className="font-semibold">
                {resolved.matched_alias
                  ? `"${resolved.typed}" is already an alias of ${resolved.canonical}.`
                  : `${resolved.canonical} is already curated.`}
              </div>
              <div className="mt-1">
                {resolved.company && <>Owned by {resolved.company}. </>}
                {resolved.areas?.length ? <>Filed under {resolved.areas.join(", ")}. </> : null}
                Adding it again would create a duplicate, so this cannot be saved.
              </div>
            </div>
          </div>
        </div>
      )}

      {resolved?.status === "near_matches" && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-3">
          <div className="mb-2 flex items-center gap-2 text-xs font-semibold text-amber-900">
            <AlertTriangle size={14} className="text-amber-600" /> Did you mean one of these?
          </div>
          <ul className="space-y-1">
            {resolved.near_matches?.map((m) => (
              <li key={m.name} className="text-xs text-amber-900">
                <span className="font-semibold">{m.name}</span>
                {m.company && <span className="text-amber-700"> — {m.company}</span>}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-[11px] text-amber-800">
            Continue only if the name you typed is a genuinely different drug.
          </p>
        </div>
      )}

      {resolved?.status === "novel" && resolved.spelling?.verdict === "misspelling" && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-3">
          <div className="flex items-start gap-2">
            <AlertTriangle size={14} className="mt-0.5 shrink-0 text-amber-600" />
            <div className="flex-1 text-xs text-amber-900">
              <div className="font-semibold">Did you mean {resolved.spelling.corrected}?</div>
              {resolved.spelling.note && (
                <div className="mt-1 leading-snug">{resolved.spelling.note}</div>
              )}
              <div className="mt-1 text-[11px] text-amber-800">
                Neither spelling is in the taxonomy, so this is the model's suggestion rather
                than a match against curated data — keep yours if you know it is right.
              </div>
              <button
                onClick={() => onUseCorrection(resolved.spelling!.corrected!)}
                className="mt-2 rounded-md bg-amber-600 px-2.5 py-1 text-[11px] font-semibold text-white hover:bg-amber-700"
              >
                Use “{resolved.spelling.corrected}”
              </button>
            </div>
          </div>
        </div>
      )}

      {resolved?.status === "novel" && resolved.spelling?.verdict !== "misspelling" && (
        <div className="rounded-lg border border-emerald-200 bg-emerald-50 p-3">
          <div className="flex items-center gap-2 text-xs text-emerald-900">
            <Check size={14} className="shrink-0 text-emerald-600" />
            Not currently curated — this will be a new brand.
          </div>
          {resolved.spelling?.verdict === "correct" && (
            <div className="mt-1 pl-6 text-[11px] text-emerald-800">
              Recognised as a real drug
              {resolved.spelling.company ? ` from ${resolved.spelling.company}` : ""}
              {resolved.spelling.generic ? ` (${resolved.spelling.generic})` : ""}.
            </div>
          )}
          {resolved.spelling?.checked === false && (
            <div className="mt-1 pl-6 text-[11px] text-emerald-800">
              The spelling check could not run — the name was matched against the taxonomy only.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function StepDetails(p: {
  name: string; drafting: boolean; draftUsed: boolean; onDraft: () => void;
  generic: string; setGeneric: (v: string) => void;
  company: string; setCompany: (v: string) => void;
  drugClass: string; setDrugClass: (v: string) => void;
  route: string; setRoute: (v: string) => void; routes: string[];
  aliases: string; setAliases: (v: string) => void;
  taKey: string; setTaKey: (v: string) => void; areas: TaxonomyAreaChoice[];
  newArea: { ta_key: string; area: string } | null;
  setNewArea: (v: { ta_key: string; area: string } | null) => void;
}) {
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between rounded-lg bg-violet-50 px-3 py-2">
        <span className="flex items-center gap-1.5 text-[11px] font-semibold text-violet-900">
          <Sparkles size={12} /> {p.draftUsed ? "Drafted by AI — check every field" : "Fill these in, or let AI draft them"}
        </span>
        <button
          onClick={p.onDraft}
          disabled={p.drafting}
          className="rounded-md bg-violet-600 px-2.5 py-1 text-[11px] font-semibold text-white hover:bg-violet-700 disabled:opacity-50"
        >
          {p.drafting ? "Drafting…" : p.draftUsed ? "Redraft" : "Draft with AI"}
        </button>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <Field label="Generic name" value={p.generic} onChange={p.setGeneric} placeholder="guselkumab" />
        <Field label="Company" value={p.company} onChange={p.setCompany} placeholder="Johnson & Johnson" />
        <Field label="Drug class" value={p.drugClass} onChange={p.setDrugClass} placeholder="IL-23 inhibitor" />
        <div>
          <label className="mb-1.5 block text-[11px] font-bold uppercase tracking-wide text-ink-light">
            Route
          </label>
          <select
            value={p.route}
            onChange={(e) => p.setRoute(e.target.value)}
            className="w-full rounded-lg border border-line px-3 py-2 text-sm focus:border-brand-light focus:outline-none"
          >
            <option value="">Not specified</option>
            {p.routes.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
        </div>
      </div>

      <div>
        <label className="mb-1.5 block text-[11px] font-bold uppercase tracking-wide text-ink-light">
          Aliases <span className="font-medium normal-case text-ink-light">(comma separated)</span>
        </label>
        <input
          value={p.aliases}
          onChange={(e) => p.setAliases(e.target.value)}
          placeholder="guselkumab, CNTO-1959"
          className="w-full rounded-lg border border-line px-3 py-2 text-sm focus:border-brand-light focus:outline-none"
        />
        <p className="mt-1.5 text-[11px] text-ink-light">
          Other names this drug is called. An alias already claimed by another curated drug is
          rejected on save — it would otherwise reattribute existing answers to the wrong agent.
        </p>
      </div>

      <div>
        <label className="mb-1.5 block text-[11px] font-bold uppercase tracking-wide text-ink-light">
          Therapeutic area <span className="text-rose-600">*</span>
        </label>
        <select
          value={p.newArea ? NEW_AREA : p.taKey}
          onChange={(e) => {
            if (e.target.value === NEW_AREA) {
              // Cleared of any previously selected area: the brand must be filed under the
              // area it creates, and the write refuses a payload where the two disagree.
              p.setNewArea({ ta_key: "", area: "" });
              p.setTaKey("");
            } else {
              p.setNewArea(null);
              p.setTaKey(e.target.value);
            }
          }}
          className="w-full rounded-lg border border-line px-3 py-2 text-sm focus:border-brand-light focus:outline-none"
        >
          <option value="">Select an area…</option>
          {p.areas.map((a) => (
            <option key={a.ta_key} value={a.ta_key}>
              {a.area === a.ta_key ? a.area : `${a.area} — ${a.ta_key}`}
            </option>
          ))}
          <option value={NEW_AREA}>+ Add a therapeutic area not listed…</option>
        </select>

        {p.newArea && (
          <div className="mt-2 rounded-lg border border-amber-200 bg-amber-50 p-3">
            <label className="mb-1.5 block text-[11px] font-bold uppercase tracking-wide text-amber-900">
              New therapeutic area
            </label>
            <input
              value={p.newArea.area}
              onChange={(e) => {
                const v = e.target.value;
                // Key and display name are set together. They differ only where several
                // indication-level keys share one heading, as Women's Health does, and that
                // is not something a brand-new area can be yet.
                p.setNewArea({ ta_key: v, area: v });
                p.setTaKey(v);
              }}
              placeholder="e.g. Hepatology"
              className="w-full rounded-lg border border-amber-300 bg-white px-3 py-2 text-sm focus:border-amber-500 focus:outline-none"
              autoFocus
            />
            <p className="mt-1.5 text-[11px] leading-snug text-amber-900">
              This becomes the therapeutic area stored on every question for this brand, and a
              new option in the area filters across the app. It is created only together with
              this brand, so it can never sit there empty. It has no indications yet — add one
              on the next step.
            </p>
          </div>
        )}
      </div>

      <div className="rounded-lg bg-slate-50 px-3 py-2 text-[11px] text-ink-light">
        Evidence depth is fixed at <span className="font-semibold text-ink">standard</span>.
        Enrolling a brand in trial ingestion is a separate, deliberate decision.
      </div>
    </div>
  );
}

function StepIndications({
  areaDiseases, diseases, onToggleDisease, onToggleCompetitor, onAddNewDisease,
}: {
  areaDiseases: string[];
  diseases: DiseaseEntry[];
  onToggleDisease: (d: string) => void;
  onToggleCompetitor: (disease: string, competitor: string) => void;
  onAddNewDisease: (name: string) => void;
}) {
  const [draftName, setDraftName] = useState("");

  function submitNew() {
    onAddNewDisease(draftName);
    setDraftName("");
  }

  return (
    <div className="space-y-4">
      <div>
        <label className="mb-1.5 block text-[11px] font-bold uppercase tracking-wide text-ink-light">
          Indications <span className="text-rose-600">*</span>
        </label>
        {areaDiseases.length === 0 ? (
          <p className="text-xs text-ink-light">
            No indications are declared for that therapeutic area yet.
          </p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {areaDiseases.map((d) => {
              const on = diseases.some((x) => x.disease === d);
              return (
                <button
                  key={d}
                  onClick={() => onToggleDisease(d)}
                  className={`rounded-full px-3 py-1 text-[11px] font-semibold transition-colors ${
                    on ? "bg-brand-dark text-white" : "bg-slate-100 text-ink hover:bg-slate-200"
                  }`}
                >
                  {d}
                </button>
              );
            })}
          </div>
        )}

        <div className="mt-2.5 flex gap-2">
          <input
            value={draftName}
            onChange={(e) => setDraftName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submitNew()}
            placeholder="Or add an indication not listed above…"
            className="flex-1 rounded-lg border border-line px-3 py-1.5 text-xs focus:border-brand-light focus:outline-none"
          />
          <button
            onClick={submitNew}
            disabled={!draftName.trim()}
            className="rounded-lg bg-slate-100 px-3 py-1.5 text-[11px] font-semibold text-ink hover:bg-slate-200 disabled:opacity-40"
          >
            Add
          </button>
        </div>
        <p className="mt-1.5 text-[11px] text-ink-light">
          A new indication is created under the therapeutic area you chose. Its endpoints are
          drafted by the model, so it is saved as a draft and left out of evidence networks,
          NMA and claim grading until someone verifies them.
        </p>
      </div>

      {diseases.map((entry) => (
        <div key={entry.disease} className="rounded-lg border border-line p-3">
          <div className="mb-2 flex items-center justify-between">
            <span className="flex items-center gap-1.5 text-xs font-bold text-ink">
              {entry.disease}
              {entry.isNew && (
                <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-800">
                  new - draft
                </span>
              )}
            </span>
            <span className="text-[11px] text-ink-light">
              {entry.ticked.size} of {entry.suggestions.length} accepted
            </span>
          </div>

          {entry.isNew && (
            <div className="mb-2 rounded-md bg-amber-50 px-2 py-1.5 text-[11px] leading-snug text-amber-900">
              {entry.outcomesLoading ? (
                <span className="flex items-center gap-1.5">
                  <Spinner size={11} /> Drafting endpoints...
                </span>
              ) : entry.canonicalOutcomes.length ? (
                <>
                  Endpoints drafted:{" "}
                  <span className="font-semibold">{entry.canonicalOutcomes.join(", ")}</span>.
                  Saved as a draft and excluded from evidence networks, NMA and claim grading
                  until verified.
                </>
              ) : (
                <>
                  No endpoint matched the defined vocabulary. The indication still saves - as a
                  draft, excluded from the evidence programme until endpoints are added.
                </>
              )}
            </div>
          )}

          {entry.loading ? (
            <div className="flex items-center gap-2 text-[11px] text-ink-light">
              <Spinner size={12} /> Finding competitors…
            </div>
          ) : entry.suggestions.length === 0 ? (
            <p className="text-[11px] text-ink-light">
              No new competitors suggested. The indication's existing competitor list is unchanged.
            </p>
          ) : (
            <ul className="space-y-1.5">
              {entry.suggestions.map((c) => (
                <li key={c.name}>
                  <label className="flex cursor-pointer items-start gap-2 rounded-md px-1.5 py-1 hover:bg-slate-50">
                    <input
                      type="checkbox"
                      checked={entry.ticked.has(c.name)}
                      onChange={() => onToggleCompetitor(entry.disease, c.name)}
                      className="mt-0.5 h-3.5 w-3.5 rounded border-line accent-brand-dark"
                    />
                    <span className="flex-1">
                      <span className="text-xs font-semibold text-ink">{c.name}</span>
                      {c.company && <span className="text-[11px] text-ink-light"> — {c.company}</span>}
                      {c.already_curated && (
                        <span className="ml-1.5 rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-800">
                          already curated
                        </span>
                      )}
                      {c.reason && (
                        <span className="mt-0.5 block text-[11px] leading-snug text-ink-light">
                          {c.reason}
                        </span>
                      )}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          )}
        </div>
      ))}

      {diseases.length > 0 && (
        <p className="text-[11px] text-ink-light">
          Suggestions arrive unticked on purpose. Only what you accept is saved, and the reason
          shown is stored with it so the competitive field stays reviewable.
        </p>
      )}
    </div>
  );
}

function StepReview(p: {
  name: string; generic: string; company: string; drugClass: string; route: string;
  taKey: string; diseases: DiseaseEntry[];
  newArea: { ta_key: string; area: string } | null;
  reviewer: string; setReviewer: (v: string) => void; errors: string[];
}) {
  const totalCompetitors = p.diseases.reduce((n, d) => n + d.ticked.size, 0);
  const drafts = p.diseases.filter((d) => d.isNew);
  return (
    <div className="space-y-4">
      {p.errors.length > 0 && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 p-3">
          <div className="mb-1.5 flex items-center gap-2 text-xs font-bold text-rose-900">
            <AlertTriangle size={14} className="text-rose-600" /> Not saved
          </div>
          <ul className="list-disc space-y-1 pl-5">
            {p.errors.map((e, i) => (
              <li key={i} className="text-[11px] leading-snug text-rose-900">{e}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="rounded-lg border border-line">
        <Row label="Brand" value={p.name} />
        <Row label="Generic" value={p.generic || "—"} />
        <Row label="Company" value={p.company || "—"} />
        <Row label="Class" value={p.drugClass || "—"} />
        <Row label="Route" value={p.route || "—"} />
        <Row
          label="Therapeutic area"
          value={p.newArea ? `${p.taKey} (new)` : p.taKey}
        />
        <Row label="Indications" value={p.diseases.map((d) => d.disease).join(", ") || "—"} />
        <Row label="Competitors accepted" value={String(totalCompetitors)} />
        <Row label="Evidence depth" value="standard" />
      </div>

      {p.newArea && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-[11px] leading-snug text-amber-900">
          <span className="font-semibold">
            {p.newArea.area} is a new therapeutic area.
          </span>{" "}
          It becomes a selectable option in the area filters across the app and the value
          stored on every question written for this brand. It is created only because this
          brand is being filed into it.
        </div>
      )}

      {drafts.length > 0 && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-[11px] leading-snug text-amber-900">
          <span className="font-semibold">
            {drafts.map((d) => d.disease).join(", ")}{" "}
            {drafts.length === 1 ? "is a new indication" : "are new indications"} and will save
            as DRAFT.
          </span>{" "}
          The endpoints came from the model, so it is fenced out of evidence networks, NMA and
          claim grading until a human verifies them. Comparison coverage and question generation
          use it right away.
        </div>
      )}

      <div>
        <label className="mb-1.5 block text-[11px] font-bold uppercase tracking-wide text-ink-light">
          Your name
        </label>
        <input
          value={p.reviewer}
          onChange={(e) => p.setReviewer(e.target.value)}
          placeholder="Recorded on the audit entry"
          className="w-full rounded-lg border border-line px-3 py-2 text-sm focus:border-brand-light focus:outline-none"
        />
        <p className="mt-1.5 text-[11px] text-ink-light">
          Recorded, not verified — there is no sign-in on this tool.
        </p>
      </div>

      <div className="rounded-lg bg-amber-50 px-3 py-2 text-[11px] leading-snug text-amber-900">
        Adding a brand also changes how answers <em>already scored</em> are attributed: the
        mentions rollup and head-to-head board resolve every agent through the taxonomy, so
        figures there can shift without a new run.
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-line px-3 py-2 last:border-b-0">
      <span className="text-[11px] font-semibold uppercase tracking-wide text-ink-light">{label}</span>
      <span className="text-right text-xs text-ink">{value}</span>
    </div>
  );
}

function Field({
  label, value, onChange, placeholder,
}: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string;
}) {
  return (
    <div>
      <label className="mb-1.5 block text-[11px] font-bold uppercase tracking-wide text-ink-light">
        {label}
      </label>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full rounded-lg border border-line px-3 py-2 text-sm focus:border-brand-light focus:outline-none"
      />
    </div>
  );
}
