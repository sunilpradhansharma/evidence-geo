import React, { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { motion } from "framer-motion";
import { NetworkSphere } from "./network-sphere";
import {
  ListPlus,
  Send,
  RotateCcw,
  Minus,
  Maximize2,
  Minimize2,
  Wrench,
  Loader2,
  History,
  Trash2,
  Plus,
  Check,
  ShieldCheck,
  AlertTriangle,
  Rocket,
  BarChart3,
  Radar,
  TrendingDown,
  Clock,
  ListChecks,
  Megaphone,
  FlaskConical,
  Scale,
  Share2,
  X,
} from "lucide-react";
import {
  api,
  copilotStream,
  type ChatMessage,
  type CopilotAgentResponse,
  type CopilotPendingAction,
  type CopilotPromptOptions,
  type CopilotToolCall,
  type CopilotUiAction,
  type Run,
  type Schedule,
} from "../api/client";
import { Markdown } from "./ui";

const STARTERS = [
  {
    icon: Rocket,
    title: "How do I start a run?",
    subtitle: "Step-by-step walkthrough",
    prompt: "How do I start a run?",
  },
  {
    icon: BarChart3,
    title: "Results of the most recent run",
    subtitle: "Latest run summary",
    prompt: "What were the results of the most recent run?",
  },
  {
    icon: Radar,
    title: "Discover new questions",
    subtitle: "Find emerging clinician queries",
    prompt: "Discover new questions",
  },
  {
    icon: TrendingDown,
    title: "Lowest average sentiment",
    subtitle: "Brand sentiment leaderboard",
    prompt: "Which brand has the lowest average sentiment?",
  },
];

const GREETING_VARIANTS = [
  "Your evidence-monitoring copilot. Ask about runs, questions, sentiment, and alerts, or tell me to take an action. I'll always confirm before I change anything.",
  "I keep an eye on what the models say about your brands. Ask me for a run summary, sentiment, or alerts, and I'll confirm before changing anything.",
  "Need a read on your latest run, sentiment trends, or open alerts? Ask away, or tell me to start a run or discover questions.",
  "Here to help you monitor evidence across the models. Ask a question or hand me a task, and I'll always check with you first.",
];

function timeGreeting(d = new Date()): string {
  const h = d.getHours();
  if (h < 12) return "Good morning";
  if (h < 18) return "Good afternoon";
  return "Good evening";
}

const STATUS_BY_INTENT: Record<string, string> = {
  ACTION: "Working on it…",
  DATA: "Looking that up…",
  HELP: "Finding the answer…",
  OFF_TOPIC: "Thinking…",
};

interface AssistantTurn {
  role: "assistant";
  content: string;
  toolCalls: CopilotToolCall[];
  promptOptions?: CopilotPromptOptions | null;
}
interface UserTurn {
  role: "user";
  content: string;
}
type Turn = UserTurn | AssistantTurn;

interface StoredSession {
  id: string;
  title: string;
  updatedAt: number;
  turns: Turn[];
}

const HISTORY_KEY = "emaCopilotChats";
const MAX_SESSIONS = 30;
const NUDGE_KEY = "ema:nudge-dismissed";
const FAB_POS_KEY = "ema:fab-pos";

function loadSessions(): StoredSession[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function persistSessions(list: StoredSession[]) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(list.slice(0, MAX_SESSIONS)));
  } catch {
    /* quota / unavailable — history is best-effort */
  }
}

function relativeTime(ts: number): string {
  const s = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d ago`;
  return new Date(ts).toLocaleDateString();
}

// Compact "in 5h" style label for a future ISO timestamp; null if past/invalid.
function relativeFuture(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  const s = Math.floor((t - Date.now()) / 1000);
  if (s <= 0) return null;
  const m = Math.floor(s / 60);
  if (m < 60) return `${Math.max(1, m)}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

type StarterCard = { icon: typeof Rocket; title: string; subtitle: string; prompt: string };

// Reduced, null-safe snapshot of live state used by both the hero chips and the
// header pulse. Every field is optional — only what loaded is rendered.
interface LiveSummary {
  lastRun?: { status: string; when: string };
  nextRunIn?: string | null;
  alerts?: number;
  worstBrand?: string | null;
}

// A page-context starter derived purely from the current route (no live data).
function pageStarter(path: string): StarterCard | null {
  if (path.startsWith("/results"))
    return { icon: BarChart3, title: "Summarize the most recent run", subtitle: "Latest run on this page", prompt: "Summarize the most recent run." };
  if (path.startsWith("/harvest") || path.startsWith("/discovery"))
    return { icon: Radar, title: "What did discovery find?", subtitle: "Latest harvested questions", prompt: "What did discovery find recently?" };
  if (path.startsWith("/social-listening"))
    return { icon: Megaphone, title: "Summarize social listening", subtitle: "Captured social sample", prompt: "Summarize the social listening insights." };
  if (path.startsWith("/questions"))
    return { icon: ListChecks, title: "Questions pending approval", subtitle: "Review the question bank", prompt: "How many questions are pending approval?" };
  if (path.startsWith("/run-analysis"))
    return { icon: BarChart3, title: "Results of the most recent run", subtitle: "Latest run summary", prompt: "What were the results of the most recent run?" };
  if (path.startsWith("/evidence/alignment"))
    return { icon: Scale, title: "Do the models match our evidence?", subtitle: "AI vs Evidence alignment", prompt: "How well do the AI models align with our verified evidence?" };
  if (path.startsWith("/evidence/governance") || path.startsWith("/evidence/studies"))
    return { icon: FlaskConical, title: "Which studies are worth verifying?", subtitle: "Curation queue, ranked", prompt: "Which studies are worth verifying right now?" };
  if (path.startsWith("/evidence"))
    return { icon: FlaskConical, title: "Summarize the clinical evidence", subtitle: "Corpus + network status", prompt: "Summarize the clinical evidence we have." };
  if (path.startsWith("/dashboard/activation-impact"))
    return { icon: Rocket, title: "Did our interventions move anything?", subtitle: "Activation & Impact", prompt: "Summarize our interventions and their measured impact." };
  if (path.startsWith("/dashboard/influence-graph"))
    return { icon: Share2, title: "Who is driving the narratives?", subtitle: "Influence Graph", prompt: "Which sources are driving the narratives the models repeat?" };
  return null;
}

// Build up to 4 starter cards: page context first, then data-driven, then
// stable defaults to fill. Deduped by prompt.
function buildStarters(live: LiveSummary | null, path: string): StarterCard[] {
  const out: StarterCard[] = [];
  const seen = new Set<string>();
  const add = (c: StarterCard | null) => {
    if (!c || out.length >= 4) return;
    const key = c.prompt.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    out.push(c);
  };

  add(pageStarter(path));
  if (live?.lastRun)
    add({ icon: BarChart3, title: "Summarize the most recent run", subtitle: `Last run ${live.lastRun.when}`, prompt: "Summarize the most recent run." });
  if (live?.worstBrand)
    add({ icon: TrendingDown, title: `Why is sentiment low for ${live.worstBrand}?`, subtitle: "Lowest-sentiment brand", prompt: `Why is sentiment low for ${live.worstBrand}?` });
  if (live?.alerts && live.alerts > 0)
    add({ icon: AlertTriangle, title: "Show me the latest alerts", subtitle: `${live.alerts} total alert${live.alerts === 1 ? "" : "s"}`, prompt: "Show me the alerts summary." });
  add({ icon: Rocket, title: "How do I start a run?", subtitle: "Step-by-step walkthrough", prompt: "How do I start a run?" });
  for (const c of STARTERS) add(c as StarterCard);

  return out.slice(0, 4);
}

// Ordered list the header subtitle cycles through: time tagline first, then any
// live stats that exist. Falls back to the static line when nothing is live.
function buildPulses(live: LiveSummary | null): string[] {
  const pulses = [`${timeGreeting()}. How can I help?`, "Ask anything or take an action"];
  if (live?.lastRun) pulses.push(`Last run ${live.lastRun.when}`);
  if (live?.nextRunIn) pulses.push(`Next run in ${live.nextRunIn}`);
  if (live?.alerts && live.alerts > 0) pulses.push(`${live.alerts} alert${live.alerts === 1 ? "" : "s"} logged`);
  return pulses;
}

const VARIANT_KEY = "ema:greet-variant";
const REDUCED_MOTION =
  typeof window !== "undefined" &&
  typeof window.matchMedia === "function" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function ToolChip({ tool }: { tool: CopilotToolCall }) {
  return (
    <span
      className={`inline-flex max-w-full items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-medium ${
        tool.ok
          ? "border-emerald-200 bg-emerald-50 text-emerald-700"
          : "border-amber-200 bg-amber-50 text-amber-700"
      }`}
      title={tool.summary}
    >
      {tool.ok ? <Check size={12} className="shrink-0" /> : <AlertTriangle size={12} className="shrink-0" />}
      <span className="truncate">{tool.summary || tool.tool_name}</span>
    </span>
  );
}

// A dropdown the assistant offers when a question has a fixed set of answers
// (e.g. "which therapeutic area?"). Picking an option sends the follow-up
// message for the user, so they click instead of typing.
function PromptOptionsPicker({
  options,
  onPick,
  disabled,
}: {
  options: CopilotPromptOptions;
  onPick: (value: string) => void;
  disabled?: boolean;
}) {
  const [value, setValue] = useState("");
  const placeholder = options.param
    ? `Select ${options.param.replace(/_/g, " ")}…`
    : "Select an option…";
  return (
    <div className="rounded-xl border border-brand-light/40 bg-brand-surface/50 p-2.5">
      <select
        aria-label={options.prompt || placeholder}
        value={value}
        disabled={disabled}
        onChange={(e) => {
          const v = e.target.value;
          setValue(v);
          if (v) onPick(v);
        }}
        className="w-full rounded-lg border border-slate-300 bg-white px-2.5 py-2 text-sm font-medium text-ink focus:border-brand-light focus:outline-none focus:ring-1 focus:ring-brand-light disabled:cursor-not-allowed disabled:opacity-60"
      >
        <option value="">{placeholder}</option>
        {options.options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
            {o.hint ? ` — ${o.hint}` : ""}
          </option>
        ))}
      </select>
    </div>
  );
}

export default function ChatWidget() {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [maximized, setMaximized] = useState(false);
  const [offline, setOffline] = useState(false);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [status, setStatus] = useState("");
  const [liveTools, setLiveTools] = useState<CopilotToolCall[]>([]);
  const [pending, setPending] = useState<CopilotPendingAction | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [edited, setEdited] = useState<Record<string, any>>({});
  const [dirty, setDirty] = useState(false);
  const [tracking, setTracking] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [sessions, setSessions] = useState<StoredSession[]>([]);
  const sessionIdRef = useRef<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // ── Dynamic greeting: live snapshot, rotating copy, header pulse ──
  const [live, setLive] = useState<LiveSummary | null>(null);
  const [liveState, setLiveState] = useState<"idle" | "loading" | "ready">("idle");
  const liveFetchedRef = useRef(false);
  const [variantIdx, setVariantIdx] = useState(0);
  const [pulseIdx, setPulseIdx] = useState(0);

  // ── First-visit nudge to entice users into the chat ──
  const [nudge, setNudge] = useState(false);
  const [nudgeDone, setNudgeDone] = useState<boolean>(() => {
    try {
      return localStorage.getItem(NUDGE_KEY) === "1";
    } catch {
      return false;
    }
  });
  useEffect(() => {
    if (open || nudgeDone) return;
    const t = window.setTimeout(() => setNudge(true), 2500);
    return () => window.clearTimeout(t);
  }, [open, nudgeDone]);
  const finishNudge = () => {
    setNudge(false);
    setNudgeDone(true);
    try {
      localStorage.setItem(NUDGE_KEY, "1");
    } catch {
      /* best-effort */
    }
  };
  const openChat = () => {
    finishNudge();
    setOpen(true);
  };
  const dismissNudge = (e: React.MouseEvent) => {
    e.stopPropagation();
    finishNudge();
  };

  // ── Draggable launcher position (drag the bubble anywhere) ──
  const fabRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ x: number; y: number } | null>(() => {
    try {
      const raw = localStorage.getItem(FAB_POS_KEY);
      const p = raw ? JSON.parse(raw) : null;
      return p && typeof p.x === "number" && typeof p.y === "number" ? p : null;
    } catch {
      return null;
    }
  });
  const [dragging, setDragging] = useState(false);
  const dragRef = useRef({ startX: 0, startY: 0, baseX: 0, baseY: 0, w: 0, h: 0, moved: false });
  const draggingRef = useRef(false);
  const suppressClickRef = useRef(false);

  // Keep the launcher on-screen on load and when the window resizes.
  useEffect(() => {
    if (!pos) return;
    const clamp = () => {
      const el = fabRef.current;
      const w = el?.offsetWidth ?? 56;
      const h = el?.offsetHeight ?? 56;
      setPos((p) =>
        p
          ? {
              x: Math.min(Math.max(0, p.x), Math.max(0, window.innerWidth - w)),
              y: Math.min(Math.max(0, p.y), Math.max(0, window.innerHeight - h)),
            }
          : p
      );
    };
    clamp();
    window.addEventListener("resize", clamp);
    return () => window.removeEventListener("resize", clamp);
  }, [pos !== null]);

  const onFabPointerDown = (e: React.PointerEvent) => {
    const el = fabRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    dragRef.current = {
      startX: e.clientX,
      startY: e.clientY,
      baseX: rect.left,
      baseY: rect.top,
      w: rect.width,
      h: rect.height,
      moved: false,
    };
    draggingRef.current = true;
    setDragging(true);
    try {
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    } catch {
      /* no-op */
    }
  };
  const onFabPointerMove = (e: React.PointerEvent) => {
    if (!draggingRef.current) return;
    const d = dragRef.current;
    const dx = e.clientX - d.startX;
    const dy = e.clientY - d.startY;
    if (!d.moved && Math.hypot(dx, dy) > 4) d.moved = true;
    if (!d.moved) return;
    const x = Math.min(Math.max(0, d.baseX + dx), Math.max(0, window.innerWidth - d.w));
    const y = Math.min(Math.max(0, d.baseY + dy), Math.max(0, window.innerHeight - d.h));
    setPos({ x, y });
  };
  const onFabPointerUp = (e: React.PointerEvent) => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    setDragging(false);
    try {
      (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
    } catch {
      /* no-op */
    }
    if (dragRef.current.moved) {
      suppressClickRef.current = true;
      setPos((p) => {
        if (p) {
          try {
            localStorage.setItem(FAB_POS_KEY, JSON.stringify(p));
          } catch {
            /* best-effort */
          }
        }
        return p;
      });
    }
  };
  const onFabClick = () => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false;
      return;
    }
    openChat();
  };

  useEffect(() => {
    api
      .copilotHealth()
      .then((h) => setOffline(h.status !== "ok"))
      .catch(() => setOffline(true));
  }, []);

  // Load saved chat history once.
  useEffect(() => {
    setSessions(loadSessions());
  }, []);

  // Persist the active conversation (once it has a user message) to history.
  useEffect(() => {
    if (!turns.some((t) => t.role === "user")) return;
    if (!sessionIdRef.current) {
      sessionIdRef.current = `s_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;
    }
    const id = sessionIdRef.current;
    const firstUser = turns.find((t) => t.role === "user");
    const title = (firstUser?.content || "Conversation").trim().slice(0, 80) || "Conversation";
    setSessions((prev) => {
      const next = [
        { id, title, updatedAt: Date.now(), turns },
        ...prev.filter((s) => s.id !== id),
      ].slice(0, MAX_SESSIONS);
      persistSessions(next);
      return next;
    });
  }, [turns]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [turns, sending, pending, liveTools, tracking]);

  // Pull a compact, null-safe live snapshot from existing endpoints. Uses
  // allSettled so any single failure degrades gracefully to a partial summary.
  async function loadLive() {
    setLiveState("loading");
    const [runsR, schedR, alertsR, worstR] = await Promise.allSettled([
      api.runs(),
      api.getSchedule(),
      api.alertsSummary(),
      api.worstQuestions(1),
    ]);

    const next: LiveSummary = {};
    if (runsR.status === "fulfilled" && Array.isArray(runsR.value) && runsR.value.length) {
      const r = runsR.value[0] as Run;
      const ts = new Date(r.ended_at ?? r.started_at).getTime();
      next.lastRun = {
        status: (r.status ?? "").toLowerCase(),
        when: Number.isNaN(ts) ? "recently" : relativeTime(ts),
      };
    }
    if (schedR.status === "fulfilled" && schedR.value) {
      const sched = schedR.value as Schedule;
      if (sched.enabled) next.nextRunIn = relativeFuture(sched.next_run_at);
    }
    if (alertsR.status === "fulfilled" && alertsR.value) {
      const n = Number(alertsR.value.total_alerts);
      if (!Number.isNaN(n)) next.alerts = n;
    }
    if (worstR.status === "fulfilled" && Array.isArray(worstR.value) && worstR.value.length) {
      const brand = worstR.value[0]?.brand_focus;
      if (brand) next.worstBrand = String(brand);
    }

    setLive(next);
    setLiveState("ready");
  }

  // On open: rotate the greeting copy variant (persisted) and reset the pulse.
  useEffect(() => {
    if (!open) return;
    setVariantIdx(() => {
      let prev = 0;
      try {
        prev = Number(localStorage.getItem(VARIANT_KEY)) || 0;
      } catch {
        /* ignore */
      }
      const next = (prev + 1) % GREETING_VARIANTS.length;
      try {
        localStorage.setItem(VARIANT_KEY, String(next));
      } catch {
        /* best-effort */
      }
      return next;
    });
    setPulseIdx(0);
  }, [open]);

  // On first open (and not offline): lazily fetch the live snapshot once.
  useEffect(() => {
    if (!open || liveFetchedRef.current || offline) return;
    liveFetchedRef.current = true;
    void loadLive();
  }, [open, offline]);

  // Cycle the header subtitle through the tagline + live pulses while open.
  const pulses = buildPulses(live);
  useEffect(() => {
    if (!open || REDUCED_MOTION || pulses.length <= 1) return;
    const id = window.setInterval(() => {
      setPulseIdx((i) => (i + 1) % pulses.length);
    }, 6000);
    return () => window.clearInterval(id);
  }, [open, pulses.length]);

  // Seed the editable confirm-card controls whenever a new action is proposed.
  useEffect(() => {
    if (!pending) {
      setEdited({});
      setDirty(false);
      return;
    }
    const init: Record<string, any> = {};
    for (const f of pending.fields ?? []) {
      init[f.key] = f.type === "boolean" ? !!f.raw : f.raw ?? "";
    }
    setEdited(init);
    setDirty(false);
  }, [pending]);

  function applyUiAction(action: CopilotUiAction | null | undefined) {
    if (action && action.target === "navigate" && action.to) navigate(action.to);
  }

  async function send(text: string) {
    const message = text.trim();
    if (!message || sending) return;
    const history: ChatMessage[] = turns
      .filter((t) => t.content && t.content.trim())
      .slice(-10)
      .map((t) => ({ role: t.role, content: t.content }));

    setTurns((prev) => [...prev, { role: "user", content: message }]);
    setInput("");
    setSending(true);
    setStatus("Thinking…");
    setLiveTools([]);
    setPending(null);

    let finalResp: CopilotAgentResponse | null = null;
    try {
      for await (const ev of copilotStream({
        message,
        history,
        ui_context: { path: window.location.pathname },
      })) {
        switch (ev.event) {
          case "start":
            setStatus("Thinking…");
            break;
          case "status":
            setStatus(STATUS_BY_INTENT[ev.data.intent ?? ""] ?? "Working…");
            break;
          case "tool":
            setLiveTools((t) => [...t, ev.data]);
            setStatus("Working…");
            break;
          case "ui_action":
            applyUiAction(ev.data);
            break;
          case "pending":
            setPending(ev.data);
            setStatus("");
            break;
          case "done":
            finalResp = ev.data;
            break;
          case "error":
            throw new Error(ev.data.error || ev.data.code || "Ema ran into an error.");
        }
      }

      if (finalResp) {
        const answer = finalResp.messages
          .filter((m) => m.role === "assistant" && m.content?.trim())
          .map((m) => m.content)
          .join("\n\n");
        const promptOptions = finalResp.prompt_options ?? null;
        if (answer || promptOptions) {
          setTurns((prev) => [
            ...prev,
            { role: "assistant", content: answer, toolCalls: finalResp!.tool_calls || [], promptOptions },
          ]);
        }
        if (finalResp.pending_action) setPending(finalResp.pending_action);
        applyUiAction(finalResp.ui_action);
      }
    } catch (err: any) {
      setTurns((prev) => [
        ...prev,
        { role: "assistant", content: `Sorry, something went wrong: ${err?.message ?? err}`, toolCalls: [] },
      ]);
    } finally {
      setSending(false);
      setStatus("");
      setLiveTools([]);
    }
  }

  function setField(key: string, value: any) {
    setEdited((prev) => ({ ...prev, [key]: value }));
    setDirty(true);
  }

  // Merge the edited controls back over the original args (empty => omit, so the
  // server applies its default of all/auto).
  function buildArgs(): Record<string, any> {
    if (!pending) return {};
    const next: Record<string, any> = { ...pending.args };
    for (const f of pending.fields ?? []) {
      if (!f.editable) continue;
      const v = edited[f.key];
      if (f.type === "boolean") {
        next[f.key] = !!v;
      } else if (v === "" || v === null || v === undefined) {
        delete next[f.key];
      } else {
        next[f.key] = f.type === "number" ? Number(v) : v;
      }
    }
    return next;
  }

  async function confirmAction() {
    if (!pending || confirming) return;
    setConfirming(true);
    try {
      let active = pending;
      // If the user edited any option, re-mint a token bound to the new args.
      if (dirty) {
        active = await api.copilotPreview({
          tool_name: pending.tool_name,
          args: buildArgs(),
          trace_id: pending.trace_id,
          base_token: pending.token,
          base_args: pending.args,
          base_issued_at: pending.issued_at,
        });
      }
      const res = await api.copilotConfirm({
        token: active.token,
        tool_name: active.tool_name,
        args: active.args,
        trace_id: active.trace_id,
        issued_at: active.issued_at,
      });
      setTurns((prev) => [
        ...prev,
        {
          role: "assistant",
          content: res.ok ? res.summary : `That didn't go through: ${res.error ?? res.summary}`,
          toolCalls: [],
        },
      ]);
      if (res.ok) applyUiAction(res.ui_action);
      if (res.ok && res.job) void trackJob(res.job);
    } catch (err: any) {
      setTurns((prev) => [
        ...prev,
        { role: "assistant", content: `Couldn't complete the action: ${err?.message ?? err}`, toolCalls: [] },
      ]);
    } finally {
      setPending(null);
      setConfirming(false);
    }
  }

  // Apply a quick-fill preset: merge its args onto the current ones and re-mint
  // the pending action via /copilot/preview (keeps the token/governance/validation
  // guarantees — identical to the user editing the fields by hand).
  async function applyPreset(preset: { label: string; args: Record<string, any> }) {
    if (!pending || confirming || previewing) return;
    setPreviewing(true);
    try {
      const next = await api.copilotPreview({
        tool_name: pending.tool_name,
        args: { ...pending.args, ...preset.args },
        trace_id: pending.trace_id,
        base_token: pending.token,
        base_args: pending.args,
        base_issued_at: pending.issued_at,
      });
      setPending(next);
    } catch (err: any) {
      setTurns((prev) => [
        ...prev,
        { role: "assistant", content: `Couldn't apply “${preset.label}”: ${err?.message ?? err}`, toolCalls: [] },
      ]);
    } finally {
      setPreviewing(false);
    }
  }

  // Poll a background job (discovery, run, insight rebuild) started by a
  // confirmed action, and post a completion message to the chat when it's done.
  async function trackJob(job: { kind: string; run_id?: string }) {
    const LABELS: Record<string, string> = {
      harvest: "Discovering new questions…",
      run: "Run in progress…",
      insights: "Rebuilding insights…",
      social: "Ingesting social posts…",
      evidence_ingest: "Ingesting clinical evidence…",
    };
    setTracking(LABELS[job.kind] ?? "Working on it…");
    try {
      for (let i = 0; i < 240; i++) {
        await new Promise((r) => setTimeout(r, 3000));
        let st;
        try {
          st = await api.copilotJobStatus(job.kind, job.run_id);
        } catch {
          continue; // transient error — keep polling
        }
        if (st.status === "done") {
          setTurns((prev) => [...prev, { role: "assistant", content: st.summary, toolCalls: [] }]);
          return;
        }
        if (st.status === "running" && st.summary) setTracking(st.summary);
      }
      setTurns((prev) => [
        ...prev,
        {
          role: "assistant",
          content: "That task is taking a while, so I'll stop watching here. You can check the relevant page for the result.",
          toolCalls: [],
        },
      ]);
    } finally {
      setTracking(null);
    }
  }

  function cancelAction() {
    setPending(null);
    setTurns((prev) => [...prev, { role: "assistant", content: "Okay, I won't do that.", toolCalls: [] }]);
  }

  function resetChat() {
    sessionIdRef.current = null;
    setTurns([]);
    setPending(null);
    setInput("");
    setStatus("");
    setLiveTools([]);
  }

  function newChat() {
    resetChat();
    setHistoryOpen(false);
  }

  function openSession(s: StoredSession) {
    sessionIdRef.current = s.id;
    setTurns(s.turns);
    setPending(null);
    setInput("");
    setHistoryOpen(false);
  }

  function deleteSession(id: string) {
    setSessions((prev) => {
      const next = prev.filter((s) => s.id !== id);
      persistSessions(next);
      return next;
    });
    if (sessionIdRef.current === id) {
      sessionIdRef.current = null;
      setTurns([]);
    }
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    send(input);
  }

  // ── Derived greeting view-model (recomputed each render) ──
  const heroPath = typeof window !== "undefined" ? window.location.pathname : "/";
  const heroStarters = liveState === "ready" ? buildStarters(live, heroPath) : STARTERS;
  const greetingWords = GREETING_VARIANTS[variantIdx].split(" ");
  const statusChips: { key: string; icon: typeof Rocket; label: string; tone: "ok" | "warn" | "info"; to: string }[] = [];
  if (liveState === "ready" && live) {
    if (live.lastRun)
      statusChips.push({ key: "run", icon: BarChart3, label: `Last run ${live.lastRun.status || "done"} · ${live.lastRun.when}`, tone: live.lastRun.status === "failed" ? "warn" : "ok", to: "/run-analysis" });
    if (live.nextRunIn)
      statusChips.push({ key: "next", icon: Clock, label: `Next run in ${live.nextRunIn}`, tone: "info", to: "/run-analysis" });
    if (live.alerts && live.alerts > 0)
      statusChips.push({ key: "alerts", icon: AlertTriangle, label: `${live.alerts} alert${live.alerts === 1 ? "" : "s"}`, tone: "warn", to: "/dashboard" });
  }
  const CHIP_TONE: Record<string, string> = {
    ok: "border-emerald-200 bg-emerald-50 text-emerald-700 hover:border-emerald-300",
    warn: "border-amber-200 bg-amber-50 text-amber-700 hover:border-amber-300",
    info: "border-brand-light/40 bg-brand-surface text-brand hover:border-brand-light",
  };

  return (
    <>
      {/* ── Floating launcher — animated network-sphere orb ── */}
      {!open && (
        <div
          ref={fabRef}
          className={`fixed z-50 h-14 w-14 ${pos ? "" : "bottom-6 right-6"}`}
          style={pos ? { left: pos.x, top: pos.y } : undefined}
        >
          {nudge && (
            <div
              role="button"
              tabIndex={0}
              onClick={openChat}
              aria-label="Open Ema chat"
              className="animate-fade-up absolute bottom-[calc(100%+0.75rem)] right-0 w-64 max-w-[calc(100vw-3rem)] cursor-pointer rounded-2xl border border-slate-200 bg-white p-3.5 pr-9 shadow-[0_14px_34px_-12px_rgba(15,23,42,0.4)] ring-1 ring-black/5"
            >
              <button
                type="button"
                onClick={dismissNudge}
                aria-label="Dismiss"
                className="absolute right-2 top-2 rounded-md p-1 text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-600"
              >
                <X size={14} />
              </button>
              <div className="flex items-start gap-2.5">
                <NetworkSphere size={32} nodeCount={60} color="13, 79, 79" background="transparent" className="mt-0.5 h-8 w-8 shrink-0" />
                <div>
                  <div className="text-sm font-bold text-slate-900">Hi, I'm Ema</div>
                  <p className="mt-0.5 text-xs leading-relaxed text-slate-500">
                    Need a hand? Ask me about your runs, evidence, sentiment, or insights.
                  </p>
                </div>
              </div>
              <span className="absolute -bottom-1.5 right-9 h-3 w-3 rotate-45 border-b border-r border-slate-200 bg-white" />
            </div>
          )}
          <button
            onPointerDown={onFabPointerDown}
            onPointerMove={onFabPointerMove}
            onPointerUp={onFabPointerUp}
            onPointerCancel={onFabPointerUp}
            onClick={onFabClick}
            aria-label="Open Ema chat"
            title="Drag to move, or click to chat with Ema"
            className={`group relative flex h-14 w-14 items-center justify-center touch-none select-none ${dragging ? "cursor-grabbing" : "cursor-grab"}`}
          >
            {/* Floating orb button — Vela-style, themed to the brand teal */}
            <span className="relative flex h-14 w-14 items-center justify-center rounded-full bg-gradient-to-br from-brand to-brand-light shadow-lg shadow-brand-light/40">
              {/* Glow effect */}
              <motion.span
                aria-hidden="true"
                className="absolute inset-0 rounded-full bg-gradient-to-br from-brand to-brand-light blur-lg"
                animate={{ opacity: [0.4, 0.7, 0.4], scale: [1, 1.15, 1] }}
                transition={{ repeat: Infinity, duration: 3, ease: "easeInOut" }}
              />
              {/* Pulse ring */}
              <motion.span
                aria-hidden="true"
                className="absolute inset-0 rounded-full border-2 border-brand-light/50"
                animate={{ scale: [1, 1.4], opacity: [0.6, 0] }}
                transition={{ repeat: Infinity, duration: 2, ease: "easeOut" }}
              />
              {/* Avatar — live animated network sphere */}
              <NetworkSphere size={56} nodeCount={90} className="relative z-10 h-14 w-14 rounded-full" />
            </span>
            {/* First-visit attention dot */}
            {!nudgeDone && (
              <span className="absolute -right-0.5 -top-0.5 z-10 flex h-3.5 w-3.5">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400 opacity-75" />
                <span className="relative inline-flex h-3.5 w-3.5 rounded-full bg-amber-400 ring-2 ring-white" />
              </span>
            )}
          </button>
        </div>
      )}

      {/* ── Chat panel ── */}
      {open && (
        <div
          className={`fixed z-50 flex flex-col overflow-hidden border border-slate-200 bg-white shadow-2xl inset-0 rounded-none ${
            maximized
              ? "sm:inset-auto sm:bottom-6 sm:right-6 sm:h-[min(80vh,820px)] sm:w-[min(560px,calc(100vw-3rem))] sm:rounded-2xl"
              : "sm:inset-auto sm:bottom-6 sm:right-6 sm:h-[600px] sm:w-[420px] sm:rounded-2xl sm:max-w-[calc(100vw-3rem)]"
          }`}
        >
          {/* Header */}
          <div className="flex items-center justify-between bg-gradient-to-r from-brand-dark via-brand-dark to-brand px-4 py-3 text-white">
            <div className="flex items-center gap-2.5">
              <NetworkSphere size={40} nodeCount={70} background="transparent" className="h-10 w-10 shrink-0" />
              <div className="min-w-0 leading-tight">
                <div className="text-sm font-bold tracking-tight">Ema</div>
                <div className="overflow-hidden text-[11px] text-white/60">
                  <span key={pulseIdx} className={`block truncate ${REDUCED_MOTION ? "" : "animate-fade-in"}`}>
                    {pulses[pulseIdx % pulses.length]}
                  </span>
                </div>
              </div>
            </div>
            <div className="flex items-center gap-0.5">
              <button
                onClick={() => setHistoryOpen((v) => !v)}
                aria-label="Past chats"
                title="Past chats"
                className={`rounded-lg p-1.5 transition-colors hover:bg-white/10 hover:text-white ${historyOpen ? "bg-white/15 text-white" : "text-white/70"}`}
              >
                <History size={16} />
              </button>
              <button
                onClick={resetChat}
                disabled={turns.length === 0 && !pending && !input}
                aria-label="Reset conversation"
                title="Reset conversation"
                className="rounded-lg p-1.5 text-white/70 transition-colors hover:bg-white/10 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
              >
                <RotateCcw size={16} />
              </button>
              <button
                onClick={() => setMaximized((m) => !m)}
                aria-label={maximized ? "Restore chat size" : "Maximize chat"}
                title={maximized ? "Restore" : "Maximize"}
                className="hidden rounded-lg p-1.5 text-white/70 transition-colors hover:bg-white/10 hover:text-white sm:inline-flex"
              >
                {maximized ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
              </button>
              <button
                onClick={() => setOpen(false)}
                aria-label="Minimize chat"
                title="Minimize"
                className="rounded-lg p-1.5 text-white/70 transition-colors hover:bg-white/10 hover:text-white"
              >
                <Minus size={18} />
              </button>
            </div>
          </div>

          {historyOpen ? (
            <div className="flex-1 overflow-y-auto bg-slate-50 p-3">
              <div className="mb-2 flex items-center justify-between px-1">
                <span className="text-[11px] font-bold uppercase tracking-wide text-ink-light">Past chats</span>
                <button
                  onClick={newChat}
                  className="flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-[11px] font-bold text-brand transition-colors hover:border-brand-light"
                >
                  <Plus size={12} /> New chat
                </button>
              </div>
              {sessions.length === 0 ? (
                <p className="px-1 py-8 text-center text-xs text-ink-light">No past chats yet.</p>
              ) : (
                <div className="space-y-1.5">
                  {sessions.map((s) => (
                    <div
                      key={s.id}
                      className="group flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 transition-colors hover:border-brand-light"
                    >
                      <button onClick={() => openSession(s)} className="min-w-0 flex-1 text-left">
                        <div className="truncate text-xs font-semibold text-ink">{s.title}</div>
                        <div className="text-[10px] text-ink-light">
                          {relativeTime(s.updatedAt)} · {s.turns.filter((t) => t.role === "user").length} msg
                        </div>
                      </button>
                      <button
                        onClick={() => deleteSession(s.id)}
                        aria-label="Delete chat"
                        title="Delete chat"
                        className="shrink-0 rounded-md p-1 text-ink-light opacity-0 transition hover:bg-slate-100 hover:text-red-500 group-hover:opacity-100"
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <>
          {/* Messages */}
          <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto bg-slate-50 p-4">
            {offline && (
              <div className="flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] font-medium text-amber-700">
                <AlertTriangle size={13} className="shrink-0" />
                Ema looks offline. Check the backend Bedrock credentials.
              </div>
            )}

            {/* Welcome screen — centered hero + starter cards (Magnus-style) */}
            {turns.length === 0 && (
              <div className="pt-1">
                <div key={variantIdx} className="flex flex-col items-center px-2 text-center">
                  <NetworkSphere size={72} nodeCount={90} color="13, 79, 79" background="transparent" />
                  <h3
                    className={`mt-3 text-lg font-bold tracking-tight text-ink ${REDUCED_MOTION ? "" : "animate-fade-up"}`}
                    style={REDUCED_MOTION ? undefined : { animationFillMode: "both" }}
                  >
                    {timeGreeting()}, I'm Ema
                  </h3>
                  <p className="mt-1.5 max-w-[19rem] text-[13px] leading-relaxed text-ink-light">
                    {REDUCED_MOTION
                      ? GREETING_VARIANTS[variantIdx]
                      : greetingWords.map((w, i) => (
                          <React.Fragment key={i}>
                            <span
                              className="animate-fade-up inline-block"
                              style={{ animationDelay: `${80 + i * 28}ms`, animationFillMode: "both" }}
                            >
                              {w}
                            </span>
                            {i < greetingWords.length - 1 ? " " : ""}
                          </React.Fragment>
                        ))}
                  </p>

                  {/* Live status chips — fill in once the snapshot loads */}
                  {liveState === "loading" && (
                    <div className="mt-3 flex flex-wrap justify-center gap-1.5">
                      <span className="h-6 w-24 animate-pulse rounded-full bg-surface-1" />
                      <span className="h-6 w-20 animate-pulse rounded-full bg-surface-1" />
                    </div>
                  )}
                  {statusChips.length > 0 && (
                    <div className="mt-3 flex flex-wrap justify-center gap-1.5">
                      {statusChips.map((c, i) => (
                        <button
                          key={c.key}
                          onClick={() => navigate(c.to)}
                          title={c.label}
                          className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-semibold transition-colors ${CHIP_TONE[c.tone]} ${REDUCED_MOTION ? "" : "animate-fade-up"}`}
                          style={REDUCED_MOTION ? undefined : { animationDelay: `${260 + i * 70}ms`, animationFillMode: "both" }}
                        >
                          <c.icon size={12} className="shrink-0" />
                          <span className="max-w-[12rem] truncate">{c.label}</span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>

                <div className="mb-2 mt-5 px-0.5 text-[11px] font-semibold uppercase tracking-wider text-ink-muted">
                  Try asking
                </div>
                <div className="space-y-2">
                  {heroStarters.map((s, idx) => (
                    <button
                      key={s.prompt}
                      onClick={() => send(s.prompt)}
                      className={`group flex w-full items-center gap-3 rounded-xl border px-3 py-2.5 text-left transition-all hover:border-brand-light hover:shadow-sm ${REDUCED_MOTION ? "" : "animate-fade-up"} ${
                        idx === 0
                          ? "border-brand-light/50 bg-brand-surface/50"
                          : "border-line bg-canvas-card"
                      }`}
                      style={REDUCED_MOTION ? undefined : { animationDelay: `${320 + idx * 70}ms`, animationFillMode: "both" }}
                    >
                      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand-surface text-brand-light ring-1 ring-inset ring-brand-light/20 transition-colors group-hover:bg-brand group-hover:text-white">
                        <s.icon size={15} strokeWidth={2.2} />
                      </span>
                      <span className="min-w-0">
                        <span className="block truncate text-[13px] font-bold text-ink">{s.title}</span>
                        <span className="block truncate text-[11px] font-medium text-ink-muted">{s.subtitle}</span>
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            )}

            {turns.map((m, i) =>
              m.role === "user" ? (
                <div key={i} className="flex justify-end">
                  <div className="max-w-[85%] rounded-2xl rounded-tr-sm bg-gradient-to-br from-brand to-brand-light px-3.5 py-2.5 text-sm text-white shadow-sm">
                    {m.content}
                  </div>
                </div>
              ) : (
                <div key={i} className="flex gap-2">
                  <div className="mt-0.5 flex h-7 w-12 shrink-0 items-center justify-center">
                    <NetworkSphere size={28} nodeCount={40} color="13, 79, 79" background="transparent" className="h-7 w-7" />
                  </div>
                  <div className="max-w-[85%] space-y-1.5">
                    {m.toolCalls.length > 0 && (
                      <div className="flex flex-wrap gap-1.5">
                        {m.toolCalls.map((t, j) => (
                          <ToolChip key={j} tool={t} />
                        ))}
                      </div>
                    )}
                    {m.content && (
                      <div className="rounded-2xl rounded-tl-sm bg-white px-3.5 py-2.5 shadow-sm">
                        <Markdown>{m.content}</Markdown>
                      </div>
                    )}
                    {m.promptOptions && m.promptOptions.options.length > 0 && i === turns.length - 1 && !sending && (
                      <PromptOptionsPicker
                        options={m.promptOptions}
                        disabled={sending}
                        onPick={(value) => {
                          const tmpl = m.promptOptions?.send_template || "{value}";
                          void send(tmpl.replace("{value}", value));
                        }}
                      />
                    )}
                  </div>
                </div>
              )
            )}

            {/* Live status + tool chips while streaming */}
            {sending && (
              <div className="flex gap-2">
                <div className="mt-0.5 flex h-7 w-12 shrink-0 items-center justify-center">
                  <NetworkSphere size={28} nodeCount={40} color="13, 79, 79" background="transparent" className="h-7 w-7" />
                </div>
                <div className="max-w-[85%] space-y-1.5">
                  {liveTools.length > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                      {liveTools.map((t, j) => (
                        <ToolChip key={j} tool={t} />
                      ))}
                    </div>
                  )}
                  <div className="flex items-center gap-2 rounded-2xl rounded-tl-sm bg-white px-3.5 py-2.5 text-sm text-ink-light shadow-sm">
                    <Wrench size={14} className="animate-pulse text-brand-light" />
                    {status || "Thinking…"}
                  </div>
                </div>
              </div>
            )}

            {/* Acknowledging a background job started by a confirmed action */}
            {tracking && !sending && (
              <div className="flex gap-2">
                <div className="mt-0.5 flex h-7 w-12 shrink-0 items-center justify-center">
                  <NetworkSphere size={28} nodeCount={40} color="13, 79, 79" background="transparent" className="h-7 w-7" />
                </div>
                <div className="flex items-center gap-2 rounded-2xl rounded-tl-sm bg-white px-3.5 py-2.5 text-sm text-ink-light shadow-sm">
                  <Loader2 size={14} className="animate-spin text-brand-light" />
                  {tracking}
                </div>
              </div>
            )}

            {/* Confirmation card for a pending write action */}
            {pending && (
              <div className="ml-14 rounded-xl border border-brand-light/40 bg-brand-surface/60 p-3 shadow-sm">
                <div className="mb-1 flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wide text-brand">
                  <ShieldCheck size={13} /> Confirm action
                </div>
                <p className="text-sm text-ink">{pending.summary}</p>
                {pending.presets && pending.presets.length > 0 && (
                  <div className="mt-2">
                    <div className="mb-1 flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-ink-light">
                      <ListPlus size={11} className="text-brand-light" /> Quick fill
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {pending.presets.map((p, i) => (
                        <button
                          key={i}
                          onClick={() => applyPreset(p)}
                          disabled={confirming || previewing}
                          title={p.description}
                          className="rounded-full border border-brand-light/40 bg-white px-2.5 py-1 text-[11px] font-medium text-brand transition-colors hover:border-brand hover:bg-brand/5 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {p.label}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
                {pending.fields && pending.fields.length > 0 && (
                  <div className="mt-2 divide-y divide-brand-light/20 overflow-hidden rounded-lg border border-brand-light/25 bg-white/70">
                    {pending.fields.map((f) => (
                      <div key={f.key} className="flex items-center justify-between gap-3 px-2.5 py-1.5">
                        <label htmlFor={`pf-${f.key}`} className="text-[11px] font-medium text-ink-light">
                          {f.label}
                        </label>
                        {!f.editable ? (
                          <span className="text-right text-xs font-semibold text-ink">{f.value}</span>
                        ) : f.type === "boolean" ? (
                          <input
                            id={`pf-${f.key}`}
                            type="checkbox"
                            checked={!!edited[f.key]}
                            disabled={confirming}
                            onChange={(e) => setField(f.key, e.target.checked)}
                            className="h-4 w-4 rounded border-slate-300 text-brand focus:ring-brand-light"
                          />
                        ) : f.type === "select" ? (
                          <select
                            id={`pf-${f.key}`}
                            value={edited[f.key] ?? ""}
                            disabled={confirming}
                            onChange={(e) => setField(f.key, e.target.value)}
                            className="max-w-[58%] rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-ink focus:border-brand-light focus:outline-none"
                          >
                            {f.allow_empty && <option value="">All</option>}
                            {f.options.map((o) => (
                              <option key={o} value={o}>
                                {o}
                              </option>
                            ))}
                          </select>
                        ) : (
                          <input
                            id={`pf-${f.key}`}
                            type={f.type === "number" ? "number" : "text"}
                            value={edited[f.key] ?? ""}
                            disabled={confirming}
                            placeholder={f.allow_empty ? (f.type === "number" ? "Auto" : "All") : ""}
                            onChange={(e) => setField(f.key, e.target.value)}
                            className="max-w-[58%] rounded-md border border-slate-300 bg-white px-2 py-1 text-right text-xs font-medium text-ink focus:border-brand-light focus:outline-none"
                          />
                        )}
                      </div>
                    ))}
                  </div>
                )}
                {pending.governance && (
                  <p className="mt-1.5 text-[11px] text-ink-light">This is a governed action and will be recorded.</p>
                )}
                <div className="mt-2.5 flex gap-2">
                  <button
                    onClick={confirmAction}
                    disabled={confirming || previewing}
                    className="flex items-center gap-1.5 rounded-lg bg-brand px-3 py-1.5 text-xs font-bold text-white transition-colors hover:bg-brand-dark disabled:opacity-50"
                  >
                    <Check size={14} /> {confirming ? "Working…" : "Confirm"}
                  </button>
                  <button
                    onClick={cancelAction}
                    disabled={confirming}
                    className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-bold text-ink-light transition-colors hover:bg-slate-100 disabled:opacity-50"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </div>

          {/* Composer */}
          <form onSubmit={handleSubmit} className="border-t border-line bg-canvas-card p-3">
            <div className="flex items-end gap-2">
              <textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    send(input);
                  }
                }}
                placeholder="Ask Ema anything, or tell me what to do…"
                rows={1}
                className="max-h-28 flex-1 resize-none rounded-xl border border-line bg-canvas-card px-3 py-2.5 text-sm text-ink placeholder:text-ink-muted focus:border-brand-light focus:outline-none"
              />
              <button
                type="submit"
                disabled={sending || !input.trim()}
                aria-label="Send message"
                className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-brand text-white shadow-sm transition-colors hover:bg-brand-dark disabled:opacity-40"
              >
                <Send size={17} />
              </button>
            </div>
            <div className="mt-2 flex items-center justify-between px-0.5 text-[10.5px] text-ink-muted">
              <span>
                Press <kbd className="rounded border border-line bg-surface-1 px-1 py-px font-mono text-[10px] text-ink-light">↵</kbd> to send
              </span>
              <span className="flex items-center gap-1">
                <kbd className="rounded border border-line bg-surface-1 px-1 py-px font-mono text-[10px] text-ink-light">↵</kbd>
                <span>send</span>
                <span className="px-0.5 opacity-50">·</span>
                <kbd className="rounded border border-line bg-surface-1 px-1 py-px font-mono text-[10px] text-ink-light">⇧↵</kbd>
                <span>newline</span>
              </span>
            </div>
          </form>
            </>
          )}
        </div>
      )}
    </>
  );
}
