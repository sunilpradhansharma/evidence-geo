import React, { useEffect, useRef, useState } from "react";
import { CornerDownRight, MessageCircleQuestion, Send, Snowflake, Trash2 } from "lucide-react";
import { api, ChatMessage } from "../api/client";
import { EmptyState, Markdown, PageHeader, Spinner } from "../components/ui";

/* Marketer-oriented starter questions. The first four mirror the global Cortex
   Agent widget; the rest are extra prompts surfaced from the GEO/Social feedback. */
const STARTERS = [
  "Which brand has the lowest average sentiment?",
  "How do AI models position Humira vs Skyrizi?",
  "Are there any recent alerts I should know about?",
  "What were the results of the most recent monitoring run?",
  "Which questions is our brand absent from in AI answers?",
  "What topics are the AI platforms most divided on?",
  "Where is our brand mentioned but not recommended first?",
  "How has sentiment for our brand changed over time?",
];

const GREETING = "Hi! I'm AI Chat Assistant. How can I help you today?";

export default function CortexChat() {
  const [available, setAvailable] = useState<boolean | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api
      .cortexAgentStatus()
      .then((s) => setAvailable(s.enabled))
      .catch(() => setAvailable(false));
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, sending]);

  async function send(text: string) {
    const question = text.trim();
    if (!question || sending) return;
    const history = messages.slice(-8); // keep recent turns for multi-turn context
    setMessages((m) => [...m, { role: "user", content: question }]);
    setInput("");
    setSuggestions([]);
    setSending(true);
    try {
      const reply = await api.cortexChat(question, history);
      setMessages((m) => [...m, { role: "assistant", content: reply.answer }]);
      setSuggestions(reply.suggestions ?? []);
    } catch (err: any) {
      setMessages((m) => [
        ...m,
        { role: "assistant", content: `Sorry, something went wrong: ${err?.message ?? err}` },
      ]);
    } finally {
      setSending(false);
    }
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    send(input);
  }

  if (available === null) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner size={28} />
      </div>
    );
  }

  return (
    <div>
      <PageHeader
        title="Chat with your Data"
        subtitle="Ask anything about your evidence-monitoring data in plain English, answered live from Snowflake."
      />

      {available === false ? (
        <EmptyState
          message="AI Chat Assistant isn't connected right now. Ask an administrator to enable the Snowflake Cortex integration to chat with your evidence-monitoring data."
          icon={<Snowflake size={40} />}
        />
      ) : (
        <div className="flex h-[calc(100vh-220px)] min-h-[460px] flex-col overflow-hidden rounded-2xl border border-slate-200/80 bg-canvas-card shadow-sm">
          {/* ── Header ── */}
          <div className="flex items-center justify-between border-b border-slate-200 bg-brand-dark px-5 py-3.5 text-white">
            <div className="flex items-center gap-2.5">
              <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-light/20">
                <Snowflake size={20} className="text-brand-light" />
              </div>
              <div className="leading-tight">
                <div className="text-sm font-bold">AI Chat Assistant</div>
                <div className="text-[11px] text-white/60">Plain-English answers over your Snowflake data</div>
              </div>
            </div>
            {messages.length > 0 && (
              <button
                onClick={() => {
                  setMessages([]);
                  setSuggestions([]);
                }}
                aria-label="Clear conversation"
                className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-bold text-white/70 transition-colors hover:bg-white/10 hover:text-white"
              >
                <Trash2 size={15} /> Clear
              </button>
            )}
          </div>

          {/* ── Messages ── */}
          <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto bg-slate-50 p-4 sm:p-6">
            {messages.length === 0 && (
              <div className="space-y-4">
                <div className="flex gap-2.5">
                  <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand/10 text-brand">
                    <Snowflake size={16} />
                  </div>
                  <div className="max-w-[85%] rounded-2xl rounded-tl-sm bg-white px-4 py-3 shadow-sm">
                    <Markdown>{GREETING}</Markdown>
                  </div>
                </div>
                <div className="pl-10">
                  <p className="mb-2 text-[11px] font-bold uppercase tracking-widest text-ink-light">
                    Try asking
                  </p>
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                    {STARTERS.map((q) => (
                      <button
                        key={q}
                        onClick={() => send(q)}
                        className="flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-left text-xs font-medium text-ink-light transition-colors hover:border-brand-light hover:text-brand"
                      >
                        <MessageCircleQuestion size={13} className="shrink-0 text-brand-light" />
                        {q}
                      </button>
                    ))}
                  </div>
                </div>
              </div>
            )}

            {messages.map((m, i) =>
              m.role === "user" ? (
                <div key={i} className="flex justify-end">
                  <div className="max-w-[85%] rounded-2xl rounded-tr-sm bg-brand px-4 py-2.5 text-sm text-white shadow-sm">
                    {m.content}
                  </div>
                </div>
              ) : (
                <div key={i} className="flex gap-2.5">
                  <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand/10 text-brand">
                    <Snowflake size={16} />
                  </div>
                  <div className="max-w-[85%] rounded-2xl rounded-tl-sm bg-white px-4 py-3 shadow-sm">
                    <Markdown>{m.content}</Markdown>
                  </div>
                </div>
              )
            )}

            {/* Typing indicator */}
            {sending && (
              <div className="flex gap-2.5">
                <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand/10 text-brand">
                  <Snowflake size={16} />
                </div>
                <div className="flex items-center gap-1 rounded-2xl rounded-tl-sm bg-white px-4 py-3 shadow-sm">
                  <span className="h-2 w-2 animate-bounce rounded-full bg-brand-light [animation-delay:-0.3s]" />
                  <span className="h-2 w-2 animate-bounce rounded-full bg-brand-light [animation-delay:-0.15s]" />
                  <span className="h-2 w-2 animate-bounce rounded-full bg-brand-light" />
                </div>
              </div>
            )}

            {/* Follow-up suggestions returned by Cortex */}
            {!sending && suggestions.length > 0 && (
              <div className="flex flex-wrap gap-2 pl-10 pt-1">
                {suggestions.map((s) => (
                  <button
                    key={s}
                    onClick={() => send(s)}
                    className="flex items-center gap-1.5 rounded-full border border-brand-light/40 bg-brand/5 px-3 py-1.5 text-xs font-semibold text-brand transition-colors hover:bg-brand/10"
                  >
                    <CornerDownRight size={12} /> {s}
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* ── Composer ── */}
          <form onSubmit={handleSubmit} className="border-t border-slate-200 bg-white p-3 sm:p-4">
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
                placeholder="Ask a question about your evidence-monitoring data…"
                rows={1}
                className="max-h-32 flex-1 resize-none rounded-xl border border-slate-300 px-3.5 py-2.5 text-sm focus:border-brand-light focus:outline-none"
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
          </form>
        </div>
      )}
    </div>
  );
}
