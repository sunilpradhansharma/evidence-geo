import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  Stethoscope,
  Copy,
  Check,
  ExternalLink,
  RefreshCw,
  Send,
  Info,
  ClipboardList,
  CheckCircle2,
} from "lucide-react";
import { api, OERunSummary, OEWorklist, OEWorkItem, OECaptureBody } from "../api/client";
import {
  Card,
  Stat,
  PageHeader,
  AnimatedCard,
  EmptyState,
  Spinner,
  SentimentBadge,
  PositionBadge,
  IntentBadge,
  InfoTooltip,
} from "../components/ui";
import { copyText } from "../lib/clipboard";

const OE_URL = "https://www.openevidence.com";

function parseCitations(raw: string): { url: string; title?: string }[] {
  return raw
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .map((line) => {
      const parts = line.split("|");
      const url = parts[0].trim();
      const title = parts.slice(1).join("|").trim();
      return title ? { url, title } : { url };
    })
    .filter((s) => s.url);
}

function AccountToggle({
  account,
  setAccount,
  className = "",
}: {
  account: "default" | "own";
  setAccount: (a: "default" | "own") => void;
  className?: string;
}) {
  return (
    <div className={className}>
      <span className="block text-[11px] font-semibold text-ink-light uppercase tracking-wide mb-1.5">
        Which OpenEvidence account?
      </span>
      <div className="inline-flex gap-1 rounded-xl bg-white ring-1 ring-slate-200 p-1">
        <button
          type="button"
          onClick={() => setAccount("default")}
          className={`px-3.5 py-1.5 rounded-lg text-xs font-bold transition-colors ${
            account === "default" ? "bg-brand text-white shadow-sm" : "text-ink-light hover:text-ink"
          }`}
        >
          Default account
        </button>
        <button
          type="button"
          onClick={() => setAccount("own")}
          className={`px-3.5 py-1.5 rounded-lg text-xs font-bold transition-colors ${
            account === "own" ? "bg-brand text-white shadow-sm" : "text-ink-light hover:text-ink"
          }`}
        >
          My own account
        </button>
      </div>
    </div>
  );
}

export default function OpenEvidence() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const [runs, setRuns] = useState<OERunSummary[]>([]);
  const [runId, setRunId] = useState(searchParams.get("run_id") || "");
  const [work, setWork] = useState<OEWorklist | null>(null);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<OEWorkItem | null>(null);
  const [answer, setAnswer] = useState("");
  const [citations, setCitations] = useState("");
  const [modelVersion, setModelVersion] = useState("");
  const [busy, setBusy] = useState(false);
  const [finalizing, setFinalizing] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [account, setAccount] = useState<"default" | "own">("default");

  const loadRuns = () => {
    api
      .oeRuns()
      .then((rs) => {
        setRuns(rs);
        setRunId((cur) => cur || (rs[0]?.run_id ?? ""));
      })
      .catch(() => {});
  };

  const loadWork = (id: string) => {
    if (!id) {
      setWork(null);
      return;
    }
    setLoading(true);
    api
      .oeWorklist(id)
      .then(setWork)
      .catch(() => setWork(null))
      .finally(() => setLoading(false));
  };

  useEffect(loadRuns, []);
  useEffect(() => {
    loadWork(runId);
    setSelected(null);
  }, [runId]);

  const pick = (it: OEWorkItem) => {
    if (it.captured) return;
    setSelected(it);
    setAnswer("");
    setCitations("");
    setError(null);
  };

  const copyQuestion = async () => {
    if (!selected) return;
    const ok = await copyText(selected.question_text);
    if (ok) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
  };

  const submit = async () => {
    if (!selected || !answer.trim() || busy) return;
    setBusy(true);
    setError(null);
    const body: OECaptureBody = {
      run_id: runId,
      question_id: selected.question_id,
      answer_text: answer.trim(),
      model_version: modelVersion.trim() || undefined,
      sources: parseCitations(citations),
    };
    try {
      await api.oeCapture(body);
      setSelected(null);
      setAnswer("");
      setCitations("");
      loadRuns();
      // Pull the fresh worklist to decide what happens next.
      const fresh = await api.oeWorklist(runId);
      setWork(fresh);
      if (fresh.pending <= 0 && fresh.status === "AWAITING_OPENEVIDENCE") {
        // Last pending Provider question captured — the run now finalizes (consensus
        // includes OpenEvidence). Head back to the Pipeline to watch it complete.
        navigate("/run-analysis");
        return;
      }
      // Still pending (or a completed run being refreshed): stay here, advance to the
      // next pending question, and re-pull shortly to surface background scoring.
      setSelected(fresh.items.find((it) => !it.captured) ?? null);
      setTimeout(() => loadWork(runId), 4000);
    } catch (e: any) {
      setError(e?.message || "Capture failed");
    } finally {
      setBusy(false);
    }
  };

  const finalizeWithoutOE = async () => {
    if (!runId || finalizing) return;
    if (
      !window.confirm(
        "Skip OpenEvidence and continue? Provider consensus will be computed from " +
          "the automated platforms only. You can still capture OpenEvidence later to refresh it.",
      )
    )
      return;
    setFinalizing(true);
    setError(null);
    try {
      await api.oeFinalizeRun(runId);
      navigate("/run-analysis");
    } catch (e: any) {
      setError(e?.message || "Finalize failed");
      setFinalizing(false);
    }
  };

  const TRIGGER_LABELS: Record<string, string> = {
    ADHOC: "On-demand run",
    SCHEDULED: "Scheduled run",
    SCHEDULE: "Scheduled run",
    CSV: "CSV upload run",
  };
  const triggerLabel = (t: string) =>
    TRIGGER_LABELS[t] ?? t.replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());

  const runLabel = (r: OERunSummary) => {
    const when = new Date(r.started_at).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
    const awaiting = r.status === "AWAITING_OPENEVIDENCE" ? " · waiting for clinician input" : "";
    return `${when} · ${triggerLabel(r.trigger)} · ${r.captured}/${r.provider_questions} captured${awaiting}`;
  };

  return (
    <div className="space-y-8">
      <PageHeader
        title="Clinician Input (Manual Step)"
        subtitle="This is the one manual step, and only for clinician-facing (Provider) questions. OpenEvidence is gated to verified clinicians and has no public API, so a clinician runs the question there and pastes the answer back here. Sign in to OpenEvidence, run the question there, and paste the answer back. Don't want OpenEvidence? Skip it and the run continues without it. Everything else runs automatically."
      />

      {/* Why manual */}
      <Card accent>
        <div className="flex gap-3">
          <Info size={18} className="text-brand-light shrink-0 mt-0.5" />
          <div className="text-sm text-ink-light leading-relaxed">
            <span className="font-semibold text-ink">OpenEvidence has no public API</span> and is gated to
            verified clinicians, so its answers can't be fetched automatically. Sign in to{" "}
            <a href={OE_URL} target="_blank" rel="noreferrer" className="text-brand-light underline hover:text-brand">
              OpenEvidence
            </a>{" "}
            and run each Provider-persona question there, then
            paste the answer below. It is stored as an{" "}
            <code className="font-mono text-xs bg-brand-surface px-1.5 py-0.5 rounded">open-evidence</code>{" "}
            response, then scored and re-run through consensus in the background. Don't want to use it? Use{" "}
            <span className="font-semibold text-ink">Skip OpenEvidence &amp; continue</span> and the run finishes
            with the automated platforms only.
          </div>
        </div>
      </Card>

      {/* Quick steps */}
      <Card>
        <div className="flex items-center gap-2 mb-4">
          <ClipboardList size={15} className="text-brand-light" />
          <h3 className="text-xs font-bold text-ink uppercase tracking-widest">How to capture, in 5 steps</h3>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3">
          {[
            <>Pick the run below. Parked ones say <span className="font-bold text-ink">waiting for clinician input</span>.</>,
            <>Click a <span className="font-bold text-ink">pending</span> question in the Worklist.</>,
            <>Copy it and open <span className="font-bold text-ink">OpenEvidence</span> to run the question.</>,
            <>Paste the answer and any citations into the panel.</>,
            <>Hit <span className="font-bold text-ink">Capture &amp; Score</span>, or <span className="font-bold text-ink">Skip OpenEvidence</span> to continue without it.</>,
          ].map((step, i) => (
            <div key={i} className="flex items-start gap-2.5 rounded-xl border border-slate-200 bg-white p-3">
              <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-brand text-[11px] font-extrabold text-white">
                {i + 1}
              </span>
              <p className="text-xs font-medium text-ink-light leading-snug">{step}</p>
            </div>
          ))}
        </div>
      </Card>

      {/* Run picker */}
      <Card>
        <div className="flex items-end gap-4 flex-wrap">
          <div className="flex flex-col flex-1 min-w-[280px]">
            <label className="flex items-center gap-1.5 text-xs font-semibold text-ink-light mb-1.5 uppercase tracking-wide"><InfoTooltip content="A 'run' is one complete monitoring cycle where all approved questions were sent to all AI platforms. Only runs with Provider-persona questions appear here." /> Run</label>
            <select
              value={runId}
              onChange={(e) => setRunId(e.target.value)}
              className="border border-slate-200 rounded-xl px-3 py-2 text-sm bg-white text-ink font-medium focus:outline-none focus:ring-2 focus:ring-brand-light/40 focus:border-brand-light transition-colors"
            >
              {runs.length === 0 && <option value="">No runs with Provider questions</option>}
              {runs.map((r) => (
                <option key={r.run_id} value={r.run_id}>
                  {runLabel(r)}
                </option>
              ))}
            </select>
          </div>
          <button
            onClick={() => {
              loadRuns();
              loadWork(runId);
            }}
            className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-bold text-ink-light bg-slate-100 hover:bg-slate-200 transition-colors"
          >
            <RefreshCw size={15} /> Refresh
          </button>
        </div>
      </Card>

      {runs.length === 0 ? (
        <EmptyState
          icon={<Stethoscope size={40} />}
          message="No runs with Provider-persona questions yet. Launch a run that includes Provider questions from the Pipeline tab first."
        />
      ) : (
        <>
          {work && (
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              <AnimatedCard delay={0}>
                <Stat label="Provider Questions" value={work.provider_questions} icon={<ClipboardList size={16} />} />
              </AnimatedCard>
              <AnimatedCard delay={0.05}>
                <Stat label="Captured" value={work.captured} icon={<Check size={16} />} />
              </AnimatedCard>
              <AnimatedCard delay={0.1}>
                <Stat label="Pending" value={work.pending} icon={<Stethoscope size={16} />} />
              </AnimatedCard>
            </div>
          )}

          {work?.status === "AWAITING_OPENEVIDENCE" && (
            <div className="rounded-2xl border-2 border-amber-300 bg-amber-50 p-5 shadow-sm">
              <div className="flex items-start gap-3.5">
                <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-amber-100 ring-1 ring-amber-300">
                  <Stethoscope size={22} className="text-amber-700" />
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-base font-extrabold text-amber-900">
                    Sign in to OpenEvidence to continue
                  </p>
                  <p className="text-sm text-amber-800 mt-1 font-medium leading-relaxed">
                    This run is paused with{" "}
                    <span className="font-bold">
                      {work.pending} pending Provider question{work.pending === 1 ? "" : "s"}
                    </span>{" "}
                    below.{" "}
                    {account === "default"
                      ? "Sign in to OpenEvidence, run each question there, and paste the answer back to finish the run."
                      : "Sign in to OpenEvidence with your own account, run each question there, and paste the answer back to finish the run."}{" "}
                    Don't want OpenEvidence? Skip it to continue with the automated platforms only.
                  </p>
                  <AccountToggle account={account} setAccount={setAccount} className="mt-3.5" />
                  <div className="mt-3.5 flex flex-wrap items-center gap-2.5">
                    <a
                      href={OE_URL}
                      target="_blank"
                      rel="noreferrer"
                      className="flex items-center gap-1.5 px-4 py-2 rounded-xl text-sm font-bold text-white bg-amber-600 hover:bg-amber-700 transition-colors shadow-sm"
                    >
                      <ExternalLink size={15} /> Sign in to OpenEvidence
                    </a>
                    <button
                      onClick={finalizeWithoutOE}
                      disabled={finalizing}
                      className="flex items-center gap-1.5 px-4 py-2 rounded-xl text-sm font-bold text-amber-900 bg-amber-100 ring-1 ring-amber-300 hover:bg-amber-200 disabled:opacity-50 transition-colors"
                    >
                      {finalizing ? <Spinner size={14} /> : <CheckCircle2 size={14} />}
                      Skip OpenEvidence &amp; continue
                    </button>
                    <InfoTooltip content="Skips OpenEvidence and finalizes the run using only the automated AI platform responses. Use this if you don't have an OpenEvidence account. The run continues immediately, but Provider data won't include OpenEvidence." />
                  </div>
                </div>
              </div>
            </div>
          )}

          {work?.status === "COMPLETED" && work.pending > 0 && (
            <div className="rounded-2xl border border-brand-light/30 bg-brand-surface p-4 flex items-start gap-3">
              <Info size={18} className="text-brand-light shrink-0 mt-0.5" />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-bold text-ink">Finalized without OpenEvidence: you can still add it</p>
                <p className="text-xs text-ink-light mt-0.5 font-medium leading-relaxed">
                  This run's Provider consensus was computed from the automated platforms only. Pick any of the{" "}
                  {work.pending} remaining Provider question{work.pending === 1 ? "" : "s"} in the Worklist below, sign
                  in to OpenEvidence and paste the answer: each capture refreshes that
                  question's Provider consensus to include it.
                </p>
              </div>
              <a
                href={OE_URL}
                target="_blank"
                rel="noreferrer"
                className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-bold text-brand bg-white ring-1 ring-brand-light/30 hover:bg-brand hover:text-white transition-colors shrink-0"
              >
                <ExternalLink size={14} /> Sign in to OpenEvidence
              </a>
            </div>
          )}

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Worklist */}
            <div className="lg:col-span-1 flex flex-col">
              <Card title="Worklist" className="flex-1">
                {loading ? (
                  <div className="flex justify-center py-12">
                    <Spinner />
                  </div>
                ) : !work || work.items.length === 0 ? (
                  <EmptyState message="No Provider-persona questions in this run." />
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[640px] text-sm">
                      <thead>
                        <tr className="text-left border-b-2 border-slate-200">
                          <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">Question</th>
                          <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">Brand</th>
                          <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">Domain</th>
                          <th className="pb-3 font-bold text-xs text-ink-light uppercase tracking-widest">OpenEvidence</th>
                        </tr>
                      </thead>
                      <tbody>
                        {work.items.map((it) => {
                          const isSel = selected?.question_id === it.question_id;
                          return (
                            <tr
                              key={it.question_id}
                              onClick={() => pick(it)}
                              className={`border-b border-slate-100 transition-colors ${
                                it.captured ? "cursor-default" : "cursor-pointer"
                              } ${isSel ? "bg-brand-surface/70" : it.captured ? "" : "hover:bg-brand-surface/50"}`}
                            >
                              <td className="py-3 pr-3 max-w-[200px] lg:max-w-xs xl:max-w-md">
                                <div className="text-ink font-medium truncate">{it.question_text}</div>
                                <div className="text-[11px] text-ink-light font-mono mt-0.5">{it.question_id}</div>
                              </td>
                              <td className="py-3 font-medium text-ink whitespace-nowrap">{it.brand_focus}</td>
                              <td className="py-3 font-medium text-ink-light whitespace-nowrap">{it.domain}</td>
                              <td className="py-3 whitespace-nowrap">
                                {!it.captured ? (
                                  <span className="px-2.5 py-1 rounded-full text-xs font-bold bg-amber-100 text-amber-800">
                                    pending
                                  </span>
                                ) : !it.scored ? (
                                  <span className="inline-flex items-center gap-1">
                                    <InfoTooltip content="The captured answer is being automatically scored for sentiment and competitive positioning in the background." />
                                    <span className="px-2.5 py-1 rounded-full text-xs font-bold bg-sky-100 text-sky-800">captured · scoring…</span>
                                  </span>
                                ) : (
                                  <div className="flex items-center gap-1.5">
                                    <Check size={14} className="text-teal-600" />
                                    <SentimentBadge score={it.sentiment_score} />
                                    <PositionBadge position={it.competitive_position} />
                                  </div>
                                )}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>
            </div>

            {/* Capture panel */}
            <div className="lg:col-span-1 flex flex-col">
              <div className="sticky top-[120px] flex-1 flex flex-col">
                <Card title="Capture Answer" className="h-full">
                  {!selected ? (
                    <EmptyState
                      icon={<ClipboardList size={32} />}
                      message="Select a pending question on the left to capture its OpenEvidence answer."
                    />
                  ) : (
                    <div className="space-y-4">
                      <div>
                        <div className="flex items-center gap-2 mb-1.5">
                          <span className="text-xs font-semibold text-ink-light uppercase tracking-wide">Question</span>
                          <IntentBadge intent={selected.intent_type} />
                        </div>
                        <p className="text-sm text-ink font-medium bg-brand-surface/50 rounded-xl p-3 leading-relaxed">
                          {selected.question_text}
                        </p>
                        <div className="flex gap-2 mt-2">
                          <button
                            onClick={copyQuestion}
                            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-bold bg-slate-100 text-ink-light hover:bg-slate-200 transition-colors"
                          >
                            {copied ? <Check size={13} className="text-teal-600" /> : <Copy size={13} />}
                            {copied ? "Copied" : "Copy"}
                          </button>
                          <a
                            href={OE_URL}
                            target="_blank"
                            rel="noreferrer"
                            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-bold bg-brand-surface text-brand hover:bg-brand hover:text-white transition-colors"
                          >
                            <ExternalLink size={13} /> Sign in to OpenEvidence
                          </a>
                        </div>
                        <div className="mt-3">
                          <AccountToggle account={account} setAccount={setAccount} />
                          <p className="text-[11px] text-ink-muted mt-1.5 font-medium leading-relaxed">
                            {account === "default"
                              ? "Sign in to OpenEvidence, run the question there, and paste the answer below."
                              : "Sign in to OpenEvidence with your own account, run the question, and paste the answer below."}
                          </p>
                        </div>
                      </div>

                      <div>
                        <label className="text-xs font-semibold text-ink-light mb-1.5 uppercase tracking-wide block">
                          Answer
                        </label>
                        <textarea
                          value={answer}
                          onChange={(e) => setAnswer(e.target.value)}
                          rows={9}
                          placeholder="Paste the full OpenEvidence answer here…"
                          className="w-full border border-slate-200 rounded-xl px-3 py-2 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-brand-light/40 focus:border-brand-light transition-colors resize-y"
                        />
                      </div>

                      <div>
                        <label className="text-xs font-semibold text-ink-light mb-1.5 uppercase tracking-wide block">
                          Citations{" "}
                          <span className="normal-case font-normal text-ink-muted">
                            (optional, one per line: <code className="font-mono">url | title</code>)
                          </span>
                        </label>
                        <textarea
                          value={citations}
                          onChange={(e) => setCitations(e.target.value)}
                          rows={3}
                          placeholder={"https://pubmed.ncbi.nlm.nih.gov/12345678 | Smith et al. 2023"}
                          className="w-full border border-slate-200 rounded-xl px-3 py-2 text-xs font-mono text-ink focus:outline-none focus:ring-2 focus:ring-brand-light/40 focus:border-brand-light transition-colors resize-y"
                        />
                      </div>

                      <div>
                        <label className="text-xs font-semibold text-ink-light mb-1.5 uppercase tracking-wide block">
                          Model label{" "}
                          <span className="normal-case font-normal text-ink-muted">(optional)</span>
                        </label>
                        <input
                          value={modelVersion}
                          onChange={(e) => setModelVersion(e.target.value)}
                          placeholder="open-evidence-web"
                          className="w-full border border-slate-200 rounded-xl px-3 py-2 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-brand-light/40 focus:border-brand-light transition-colors"
                        />
                      </div>

                      {error && (
                        <div className="text-xs font-semibold text-red-700 bg-red-50 border border-red-200 rounded-xl px-3 py-2">
                          {error}
                        </div>
                      )}

                      <button
                        disabled={!answer.trim() || busy}
                        onClick={submit}
                        className="w-full flex items-center justify-center gap-2 px-5 py-2.5 bg-brand text-white rounded-xl text-sm font-bold hover:bg-brand-dark disabled:opacity-40 transition-colors"
                      >
                        {busy ? <Spinner size={16} /> : <Send size={16} />}
                        {busy ? "Capturing…" : "Capture & Score"}
                      </button>
                    </div>
                  )}
                </Card>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
