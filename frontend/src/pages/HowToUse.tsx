import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { LucideIcon } from "lucide-react";
import {
  Radar,
  ListChecks,
  Workflow,
  Stethoscope,
  Search,
  Grid3x3,
  BarChart3,
  Megaphone,
  Globe,
  ShieldCheck,
  CheckCircle2,
  ExternalLink,
  Users,
  Calendar,
  AlertTriangle,
  ArrowRight,
  Info,
  Lightbulb,
  TrendingUp,
  RefreshCw,
  Mail,
} from "lucide-react";
import { Card, PageHeader, AnimatedCard } from "../components/ui";

/* The five workflow stages, mirrored from the main navigation. */
const STAGES: {
  to: string;
  label: string;
  icon: LucideIcon;
  blurb: string;
  chip: string;
  manual?: boolean;
}[] = [
  { to: "/harvest", label: "Discover Questions", icon: Radar, blurb: "Find the questions worth monitoring", chip: "bg-emerald-50 text-emerald-700 group-hover:bg-emerald-600 group-hover:text-white" },
  { to: "/questions", label: "Approved Question Bank", icon: ListChecks, blurb: "Review & approve the bank", chip: "bg-sky-50 text-sky-700 group-hover:bg-sky-600 group-hover:text-white" },
  { to: "/run-analysis", label: "Run Analysis", icon: Workflow, blurb: "Launch an AI platform run", chip: "bg-brand-surface text-brand group-hover:bg-brand group-hover:text-white" },
  { to: "/results", label: "AI Response Review", icon: Search, blurb: "Inspect scored responses", chip: "bg-amber-50 text-amber-700 group-hover:bg-amber-600 group-hover:text-white" },
  { to: "/dashboard", label: "Insights & Trends", icon: BarChart3, blurb: "Track trends & insights", chip: "bg-blue-50 text-blue-700 group-hover:bg-blue-600 group-hover:text-white" },
];

/* ------------------------------------------------------------------ */
/*  Small helpers                                                      */
/* ------------------------------------------------------------------ */
function Pill({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex items-center rounded-md bg-brand/10 px-1.5 py-0.5 text-[0.85em] font-bold text-brand">
      {children}
    </span>
  );
}

function Mono({ children }: { children: ReactNode }) {
  return <code className="font-mono text-[0.85em] bg-brand-surface px-1.5 py-0.5 rounded">{children}</code>;
}

function TabLink({ to, label, icon: Icon }: { to: string; label: string; icon: LucideIcon }) {
  return (
    <Link
      to={to}
      className="inline-flex items-center gap-1.5 text-sm font-bold text-brand hover:text-brand-dark transition-colors"
    >
      <Icon size={15} strokeWidth={2.2} /> Open {label}
      <ArrowRight size={14} />
    </Link>
  );
}

function OrderedSteps({ items }: { items: ReactNode[] }) {
  return (
    <ol className="space-y-2.5">
      {items.map((it, i) => (
        <li key={i} className="flex gap-3 text-sm text-ink-light leading-relaxed">
          <span className="shrink-0 mt-0.5 w-5 h-5 rounded-full bg-brand-surface text-brand text-[11px] font-extrabold flex items-center justify-center">
            {i + 1}
          </span>
          <span>{it}</span>
        </li>
      ))}
    </ol>
  );
}

function GuideStep({
  n,
  icon: Icon,
  title,
  badge,
  intro,
  items,
  link,
}: {
  n: number;
  icon: LucideIcon;
  title: string;
  badge?: string;
  intro?: ReactNode;
  items: ReactNode[];
  link?: { to: string; label: string; icon: LucideIcon };
}) {
  return (
    <Card>
      <div className="flex gap-5">
        <div className="w-11 h-11 rounded-2xl bg-brand text-white flex items-center justify-center font-extrabold text-lg shadow-sm shrink-0">
          {n}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2.5 mb-2 flex-wrap">
            <Icon size={18} className="text-brand-light" strokeWidth={2.2} />
            <h3 className="text-base font-extrabold text-ink">{title}</h3>
            {badge && (
              <span className="px-2 py-0.5 rounded-full text-[11px] font-bold bg-slate-100 text-ink-light uppercase tracking-wide">
                {badge}
              </span>
            )}
          </div>
          {intro && <p className="text-sm text-ink-light leading-relaxed mb-3">{intro}</p>}
          <OrderedSteps items={items} />
          {link && (
            <div className="mt-4">
              <TabLink to={link.to} label={link.label} icon={link.icon} />
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/*  Provider / EvidenceMD deep-dive (the highlighted step)             */
/* ------------------------------------------------------------------ */
function ProviderSection() {
  return (
    <div className="rounded-2xl border-2 border-cyan-300 bg-gradient-to-br from-cyan-50 to-brand-surface/40 p-6 shadow-md">
      <div className="flex gap-5">
        <div className="w-11 h-11 rounded-2xl bg-cyan-600 text-white flex items-center justify-center shadow-sm shrink-0">
          <Stethoscope size={22} strokeWidth={2.2} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2.5 mb-2 flex-wrap">
            <h3 className="text-lg font-extrabold text-ink">Provider persona &amp; EvidenceMD</h3>
            <span className="px-2 py-0.5 rounded-full text-[11px] font-bold bg-cyan-600 text-white uppercase tracking-wide">
              Automated
            </span>
            <span className="px-2 py-0.5 rounded-full text-[11px] font-bold bg-slate-100 text-ink-light uppercase tracking-wide">
              Part of Run Analysis
            </span>
          </div>
          <p className="text-sm text-ink-light leading-relaxed">
            Clinician-facing (<strong>Provider</strong>) questions go to the same public platforms as every other
            persona, plus <strong>EvidenceMD</strong>: a clinical-reasoning API that answers with peer-reviewed
            citations. There is <strong>no manual step and no pause</strong>&mdash;EvidenceMD is queried automatically
            like any other model, and its answer is scored and folded into the consensus across AI platforms.
          </p>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-4">
            <div className="rounded-xl border border-cyan-200 bg-white/70 p-4">
              <div className="flex items-center gap-2 mb-1.5">
                <ShieldCheck size={16} className="text-cyan-700" />
                <p className="text-sm font-bold text-ink">Clinical-grade answers</p>
              </div>
              <p className="text-xs text-ink-light leading-relaxed">
                EvidenceMD is tuned for clinical reasoning and cites peer-reviewed literature, so Provider answers can
                be compared against the general-purpose models on the very same question.
              </p>
            </div>
            <div className="rounded-xl border border-cyan-200 bg-white/70 p-4">
              <div className="flex items-center gap-2 mb-1.5">
                <CheckCircle2 size={16} className="text-status-success" />
                <p className="text-sm font-bold text-ink">Fully automated</p>
              </div>
              <p className="text-xs text-ink-light leading-relaxed">
                Provider-only routing means EvidenceMD runs solely for clinician questions. It shows up as its own
                platform card on <strong>Run Analysis</strong> and as a normal response in <strong>AI Response Review</strong>.
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Comparison coverage (part of Discover Questions)                   */
/* ------------------------------------------------------------------ */
function ComparisonCoverageSection() {
  return (
    <div className="rounded-2xl border-2 border-emerald-200 bg-gradient-to-br from-emerald-50 to-brand-surface/30 p-6 shadow-md">
      <div className="flex gap-5">
        <div className="w-11 h-11 rounded-2xl bg-emerald-600 text-white flex items-center justify-center shadow-sm shrink-0">
          <Grid3x3 size={22} strokeWidth={2.2} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2.5 mb-2 flex-wrap">
            <h3 className="text-lg font-extrabold text-ink">Comparison coverage</h3>
            <span className="px-2 py-0.5 rounded-full text-[11px] font-bold bg-emerald-600 text-white uppercase tracking-wide">
              Part of Discover Questions
            </span>
          </div>
          <p className="text-sm text-ink-light leading-relaxed">
            Scraping can only surface questions somebody happened to post online.{" "}
            <strong>Comparison coverage</strong> takes the other route: it lays out every{" "}
            <strong>brand-vs-competitor</strong> comparison worth monitoring, marks the ones your question bank
            already asks, and writes the ones nobody asked. It sits at the top of the{" "}
            <strong>Discover Questions</strong> tab.
          </p>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-4">
            <div className="rounded-xl border border-emerald-200 bg-white/70 p-4">
              <div className="flex items-center gap-2 mb-1.5">
                <BarChart3 size={16} className="text-emerald-700" />
                <p className="text-sm font-bold text-ink">What it shows</p>
              </div>
              <p className="text-xs text-ink-light leading-relaxed">
                A headline for whatever scope you pick: <strong>Covered</strong>, <strong>Gaps</strong>, and the total
                number of <strong>Comparisons</strong>. Open <em>Show what&rsquo;s missing</em> to see where the gap
                actually sits &mdash; by area, by indication, and by brand &mdash; plus the exact comparisons that
                would be written next.
              </p>
            </div>
            <div className="rounded-xl border border-emerald-200 bg-white/70 p-4">
              <div className="flex items-center gap-2 mb-1.5">
                <ShieldCheck size={16} className="text-emerald-700" />
                <p className="text-sm font-bold text-ink">Same review queue</p>
              </div>
              <p className="text-xs text-ink-light leading-relaxed">
                Nothing is written until you click <em>Generate questions</em>. What it writes lands in the candidate
                table further down the same page and follows the <strong>same review path</strong> as a scraped
                question. There is no separate fast lane here for a question the model wrote.
              </p>
            </div>
          </div>

          <p className="text-xs font-bold text-ink uppercase tracking-widest mt-6 mb-3">How to use it</p>
          <OrderedSteps
            items={[
              <>Set the scope: pick a <Pill>Brand</Pill>, <Pill>Area</Pill>, and <Pill>Persona</Pill>. Leave any of them empty to cover all of it. Choosing a brand narrows <Pill>Area</Pill> to the areas that brand is actually indicated in, so one pass covers every one of them.</>,
              <>Click <Pill>Show what&rsquo;s missing</Pill> to read where the gap is concentrated <em>before</em> you write anything.</>,
              <>That panel ends with the exact comparisons <Pill>Generate questions</Pill> would write next. Read it first &mdash; nothing is written until you ask for it.</>,
              <>Happy with it? Click <Pill>Generate questions</Pill>. The new candidates appear in the table below, ready to review and <Pill>Submit for Medical Affairs review</Pill>.</>,
              <>If the panel reports no uncovered comparisons in your scope, every head-to-head already has a question in the bank or in review &mdash; nothing to do.</>,
              <>Or just ask <strong>Ema</strong>: try <em>&ldquo;where are our comparison coverage gaps?&rdquo;</em> or <em>&ldquo;generate the missing comparison questions&rdquo;</em>.</>,
            ]}
          />

          <div className="mt-4">
            <TabLink to="/harvest" label="Discover Questions" icon={Grid3x3} />
          </div>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Personas reference                                                 */
/* ------------------------------------------------------------------ */
const PERSONAS: { name: string; targets: string; note: string; manual: boolean }[] = [
  { name: "Prospect", targets: "Public LLMs only", note: "Pre-diagnosis, exploring options.", manual: false },
  { name: "Patient", targets: "Public LLMs only", note: "On or considering therapy.", manual: false },
  { name: "Provider", targets: "Public LLMs + EvidenceMD", note: "Clinician-facing: EvidenceMD answers automatically.", manual: false },
];

/* ------------------------------------------------------------------ */
/*  Page                                                               */
/* ------------------------------------------------------------------ */
export default function HowToUse() {
  return (
    <div className="space-y-8">
      <PageHeader
        title="How to Use"
        subtitle="A step-by-step guide to running the Evidence Monitoring Agent end to end. Every stage is fully automated, including the Provider persona's clinical model, EvidenceMD."
      />

      {/* Overview */}
      <Card accent>
        <div className="flex gap-3">
          <Info size={18} className="text-brand-light shrink-0 mt-0.5" />
          <p className="text-sm text-ink-light leading-relaxed">
            <span className="font-bold text-ink">Evidence Monitor</span> watches what leading LLMs (Claude, Nova-Pro,
            Llama, Gemini, GPT-4o, plus <strong>EvidenceMD</strong>&mdash;a clinical-reasoning API for the Provider
            persona) say about your therapies across three audiences:{" "}
            <strong>Prospect</strong>, <strong>Patient</strong>, and <strong>Provider</strong>. Every answer is scored
            for sentiment and competitive positioning, reviewed across AI platforms and summarized into a <strong>consensus answer</strong>,
            then rolled up into dashboards and theme insights. Follow the five steps below in order.
          </p>
        </div>
      </Card>

      {/* Workflow map */}
      <Card title="The monitoring workflow">
        <p className="-mt-2 mb-5 text-sm text-ink-light">
          Five stages, start to finish: click any stage to jump straight in.
        </p>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          {STAGES.map((s, i) => {
            const Icon = s.icon;
            return (
              <AnimatedCard key={s.to} delay={i * 0.05} className="relative h-full">
                <Link
                  to={s.to}
                  className={`group flex h-full flex-col rounded-2xl border p-4 transition-all hover:-translate-y-0.5 hover:shadow-md ${
                    s.manual
                      ? "border-cyan-300 bg-cyan-50/40 ring-1 ring-cyan-200"
                      : "border-slate-200 bg-white hover:border-brand-light"
                  }`}
                >
                  <div className="mb-3 flex items-center justify-between">
                    <span className="flex h-6 w-6 items-center justify-center rounded-full bg-brand text-[11px] font-extrabold text-white">
                      {i + 1}
                    </span>
                    {s.manual && (
                      <span className="rounded-full bg-cyan-600 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide text-white">
                        Manual
                      </span>
                    )}
                  </div>
                  <div className={`mb-2.5 flex h-10 w-10 items-center justify-center rounded-xl transition-colors ${s.chip}`}>
                    <Icon size={20} strokeWidth={2.2} />
                  </div>
                  <p className="text-sm font-extrabold leading-tight text-ink">{s.label}</p>
                  <p className="mt-1 text-[11px] font-medium leading-snug text-ink-muted">{s.blurb}</p>
                </Link>
                {i < STAGES.length - 1 && (
                  <ArrowRight
                    className="absolute top-1/2 -right-[10px] z-10 hidden -translate-y-1/2 text-slate-300 lg:block"
                    size={16}
                  />
                )}
              </AnimatedCard>
            );
          })}
        </div>
      </Card>

      {/* Steps */}
      <AnimatedCard delay={0.02}>
        <GuideStep
          n={1}
          icon={Radar}
          title="Discover the questions to monitor"
          badge="Optional"
          intro="Questions reach the bank two ways on this tab: scraped verbatim from public health communities, or written to close a brand-vs-competitor comparison nobody happened to ask. Either way, they land in the same review queue."
          items={[
            <>First pick a <Pill>Monitoring Mode</Pill> (required): <strong>AbbVie</strong> to focus on AbbVie brands, or <strong>All Brands</strong> to harvest across the whole disease landscape (every competitor). Discover Questions stays disabled until you choose.</>,
            <>Click <Pill>Discover Questions</Pill> to scrape Reddit, Quora, drugs.com, HealthUnlocked, patient.info and more. Candidates stream into the table as they're found. Live scraping needs a <Mono>TAVILY_API_KEY</Mono> in the backend, so the page shows a banner until it is set.</>,
            <>Each is PII-scrubbed, de-duplicated, and auto-classified by persona, brand, and domain. Possible adverse events are flagged and quarantined for safety review.</>,
            <>Scraping only ever finds questions that already exist online. For the rest, use <strong>Comparison coverage</strong> at the top of the page: pick a <Pill>Brand</Pill>, <Pill>Area</Pill>, and <Pill>Persona</Pill> to see how many head-to-head comparisons the bank already covers and how many are missing, then <Pill>Show what's missing</Pill> to break that gap down by area, indication, and brand.</>,
            <>That same list ends with the exact comparisons <Pill>Generate questions</Pill> would write next, so you can check them before anything is written. Click <Pill>Generate questions</Pill> and the new candidates drop straight into the candidate table below. See the highlighted note below for the full walkthrough.</>,
            <>Click a candidate to review it, adjust the persona/therapy/brand/domain, then <Pill>Submit for Medical Affairs review</Pill>.</>,
            <>Submitting creates a <Pill>PENDING</Pill> question: it still needs Medical-Affairs approval (Step 2) before any run can use it.</>,
          ]}
          link={{ to: "/harvest", label: "Discover Questions", icon: Radar }}
        />
      </AnimatedCard>

      {/* Highlighted Comparison coverage note, referenced from Step 1 */}
      <AnimatedCard delay={0.03}>
        <ComparisonCoverageSection />
      </AnimatedCard>

      <AnimatedCard delay={0.04}>
        <GuideStep
          n={2}
          icon={ListChecks}
          title="Review & approve the question bank"
          intro="The Approved Question Bank is your Medical-Affairs-approved, versioned source of truth. Only APPROVED questions are eligible to run."
          items={[
            <>Filter by persona or therapeutic area to find questions needing attention.</>,
            <>For each <Pill>PENDING</Pill> question, click <Pill>Approve</Pill> or <Pill>Deny</Pill>. Approval is the governance gate: nothing runs without it.</>,
            <>To test quickly, tick one or more approved questions and click <Pill>Run Selected</Pill> to launch an ad-hoc run immediately.</>,
          ]}
          link={{ to: "/questions", label: "Questions", icon: ListChecks }}
        />
      </AnimatedCard>

      <AnimatedCard delay={0.06}>
        <GuideStep
          n={3}
          icon={Workflow}
          title="Launch a monitoring run"
          intro="The Run Analysis tab is mission control. Launch on demand or schedule a daily sweep, then watch the AI platform run live."
          items={[
            <>Pick a <Pill>Monitoring Mode</Pill> first (required): <strong>AbbVie</strong> runs the focus-brand question set, <strong>All Brands</strong> runs the brand-less landscape set and scores every competitor. Run Now stays disabled until you choose.</>,
            <>Under <strong>Quick Run from Question Bank</strong>, optionally filter by persona, therapy, and domain, then click <Pill>Run Now</Pill> (or <Pill>Preview before launching</Pill> to check which questions will run before any AI calls are made).</>,
            <>Have your own list? Drop a CSV with <Mono>question_text, persona, therapeutic_area, brand_focus, domain</Mono> to import and run it.</>,
            <>Watch the <strong>Live run progress</strong> and Run History as each model responds.</>,
            <>To automate it, open the <strong>Scheduled Runs</strong> card and toggle the daily run (midnight, America/Chicago).</>,
            <><strong>Heads-up:</strong> Provider questions also query <strong>EvidenceMD</strong>, a clinical-reasoning model, automatically&mdash;see the highlighted note below.</>,
          ]}
          link={{ to: "/run-analysis", label: "Pipeline", icon: Workflow }}
        />
      </AnimatedCard>

      {/* Highlighted Provider / EvidenceMD note */}
      <AnimatedCard delay={0.08}>
        <ProviderSection />
      </AnimatedCard>

      <AnimatedCard delay={0.1}>
        <GuideStep
          n={4}
          icon={Search}
          title="Review the scored results"
          intro="Every individual model response lands in AI Response Review, fully scored and searchable."
          items={[
            <>Filter by therapy, intent, or consensus level, and search the response text.</>,
            <>Each row shows <strong>sentiment</strong>, <strong>competitive position</strong>, <strong>intent</strong>, and <strong>consensus</strong> badges.</>,
            <>Open any response to read the full answer, its <strong>sources/citations</strong>, the <strong>consensus answer across AI platforms</strong>, and the synthesized final answer.</>,
            <>Export responses for offline analysis when needed.</>,
          ]}
          link={{ to: "/results", label: "Results", icon: Search }}
        />
      </AnimatedCard>

      <AnimatedCard delay={0.12}>
        <GuideStep
          n={5}
          icon={BarChart3}
          title="Track trends on the Dashboard"
          intro="Roll everything up into the big picture. The dashboard analytics are served from Snowflake."
          items={[
            <><strong>Overview</strong>: leads with <strong>Recommended Next Steps</strong> (the top questions needing attention), then sentiment, positioning, consensus, and alerting across all runs, served live from Snowflake views.</>,
            <><strong>Insights</strong>: auto-discovered themes and signals (the "needle in a haystack" view), opening with a plain-English big-picture takeaway.</>,
            <><strong>Ask a Question</strong>: a plain-English chat (AI Chat Assistant) that answers anything about your stored questions, responses, sentiment, positioning, alerts, and runs, live from Snowflake.</>,
          ]}
          link={{ to: "/dashboard", label: "Dashboard", icon: BarChart3 }}
        />
      </AnimatedCard>

      {/* Complementary surface: Social Listening */}
      <AnimatedCard delay={0.13}>
        <div className="rounded-2xl border-2 border-sky-200 bg-gradient-to-br from-sky-50 to-brand-surface/30 p-6 shadow-md">
          <div className="flex gap-5">
            <div className="w-11 h-11 rounded-2xl bg-sky-600 text-white flex items-center justify-center shadow-sm shrink-0">
              <Megaphone size={22} strokeWidth={2.2} />
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2.5 mb-2 flex-wrap">
                <h3 className="text-lg font-extrabold text-ink">Social Listening</h3>
                <span className="px-2 py-0.5 rounded-full text-[11px] font-bold bg-sky-600 text-white uppercase tracking-wide">
                  Complementary
                </span>
              </div>
              <p className="text-sm text-ink-light leading-relaxed">
                Separate from the five-stage monitoring flow above, <strong>Social Listening</strong> shows what real
                people are saying about monitored therapies on public social channels (<strong>Reddit, TikTok,
                Instagram, Facebook, X</strong>), scraped via Apify. Posts <strong>and their comments/replies</strong> are
                PII-scrubbed, screened for adverse events, and classified by brand, topic, and sentiment. <strong>Comment
                sentiment is tracked separately from post sentiment</strong>, and non-English text is auto-translated to
                English with a <em>Show original</em> toggle.
              </p>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-4">
                <div className="rounded-xl border border-sky-200 bg-white/70 p-4">
                  <div className="flex items-center gap-2 mb-1.5">
                    <BarChart3 size={16} className="text-sky-700" />
                    <p className="text-sm font-bold text-ink">What you get</p>
                  </div>
                  <p className="text-xs text-ink-light leading-relaxed">
                    Share of voice by brand and channel, post <strong>and comment</strong> sentiment, volume over time,
                    top topics, adverse-event signals (across posts and comments), and per-channel engagement leaders.
                  </p>
                </div>
                <div className="rounded-xl border border-amber-200 bg-amber-50 p-4">
                  <div className="flex items-center gap-2 mb-1.5">
                    <AlertTriangle size={16} className="text-amber-600" />
                    <p className="text-sm font-bold text-amber-900">Read the numbers carefully</p>
                  </div>
                  <p className="text-xs text-amber-800 leading-relaxed">
                    These are <strong>captured-sample</strong> metrics, not market-level share of voice. Engagement
                    (upvotes vs likes vs views) is compared per channel only, never summed across channels.
                  </p>
                </div>
              </div>

              <p className="text-xs font-bold text-ink uppercase tracking-widest mt-6 mb-3">How to use it</p>
              <OrderedSteps
                items={[
                  <>Click <Pill>Ingest now</Pill> (optionally pick channels) to scrape fresh posts. Live ingestion needs an <Mono>APIFY_API_TOKEN</Mono> in the backend, so the page shows a banner until it is set.</>,
                  <>Watch posts stream into the table as they are captured, each tagged with brand, topic, and a sentiment badge (translated rows show a <Mono>ES&rarr;EN</Mono> chip).</>,
                  <>Open any post to see <strong>what people are saying</strong> &mdash; the captured comments with their own sentiment, plus a <em>Show original</em> toggle on translated text.</>,
                  <>Explore the dashboard charts (including <strong>comment sentiment</strong> as a separate dimension), then filter the posts by channel, search the text, or flag <Pill>Adverse events</Pill> for review.</>,
                  <>Or just ask <strong>Ema</strong>: try <em>"summarize the social listening insights"</em> or <em>"start a social ingest"</em>.</>,
                ]}
              />

              <div className="mt-4">
                <TabLink to="/social-listening" label="Social Listening" icon={Megaphone} />
              </div>
            </div>
          </div>
        </div>
      </AnimatedCard>

      {/* New: dashboard intelligence surfaces */}
      <AnimatedCard delay={0.135}>
        <Card title="Dashboard intelligence surfaces">
          <p className="-mt-2 mb-5 text-sm text-ink-light">
            Beyond the five core stages, these surfaces turn your monitoring data into action.{" "}
            <strong>GEO Interventions</strong>, <strong>Source Authority</strong> and{" "}
            <strong>AI Update Impact</strong> live under <strong>Insights &amp; Trends</strong>;
            <strong> Prompt Volume</strong> sits in the main navigation.
          </p>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {[
              {
                to: "/dashboard/recommendations",
                icon: Lightbulb,
                title: "GEO Interventions",
                chip: "bg-amber-50 text-amber-700",
                body: (
                  <>
                    Ranked, plain-language <strong>content recommendations</strong> that move weak brand
                    positions up: built from competitive-position gaps and enriched with SEMrush SEO
                    metrics. Click <Pill>Generate</Pill> to (re)build the list. Every item is a{" "}
                    <strong>strategic suggestion, not MLR-approved content</strong>.
                  </>
                ),
              },
              {
                to: "/dashboard/source-authority",
                icon: ShieldCheck,
                title: "Source Authority",
                chip: "bg-emerald-50 text-emerald-700",
                body: (
                  <>
                    Maps <strong>which web domains the models cite</strong> and classifies them (owned /
                    earned / competitor / unverified), with distribution, top domains per model, coverage,
                    and preferred-source tracking. New runs classify automatically; older responses need a
                    one-time <strong>backfill</strong> (below).
                  </>
                ),
              },
              {
                to: "/dashboard/ai-update-impact",
                icon: TrendingUp,
                title: "AI Update Impact",
                chip: "bg-violet-50 text-violet-700",
                body: (
                  <>
                    Shows whether big changes in AI answers line up with <strong>known updates to the AI
                    tools</strong> (Claude, GPT-4o, EvidenceMD, etc.), so you can tell an organic shift from
                    one caused by a vendor update. Log updates in the admin form; correlated answer changes are
                    flagged automatically.
                  </>
                ),
              },
              {
                to: "/prompt-volume",
                icon: TrendingUp,
                title: "Prompt Volume",
                chip: "bg-sky-50 text-sky-700",
                body: (
                  <>
                    Upload third-party <strong>search-demand exports</strong> (such as SEMrush) as a proxy
                    for AI-inquiry demand. The file is PII-linted and rejected on any hit. Surfaces relative
                    volume by area/competitor and high-volume <strong>gap topics</strong> missing from the
                    question bank.
                  </>
                ),
              },
            ].map((f) => {
              const Icon = f.icon;
              return (
                <Link
                  key={f.to}
                  to={f.to}
                  className="group flex h-full flex-col rounded-2xl border border-slate-200 bg-white p-4 transition-all hover:-translate-y-0.5 hover:border-brand-light hover:shadow-md"
                >
                  <div className={`mb-2.5 flex h-10 w-10 items-center justify-center rounded-xl ${f.chip}`}>
                    <Icon size={20} strokeWidth={2.2} />
                  </div>
                  <p className="text-sm font-extrabold text-ink">{f.title}</p>
                  <p className="mt-1 text-xs leading-relaxed text-ink-light">{f.body}</p>
                </Link>
              );
            })}
          </div>
        </Card>
      </AnimatedCard>

      {/* Stakeholder Digests */}
      <AnimatedCard delay={0.137}>
        <div className="rounded-2xl border-2 border-brand-light/40 bg-gradient-to-br from-brand-surface/40 to-white p-6 shadow-md">
          <div className="flex gap-5">
            <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl bg-brand text-white shadow-sm">
              <Mail size={22} strokeWidth={2.2} />
            </div>
            <div className="min-w-0 flex-1">
              <div className="mb-2 flex flex-wrap items-center gap-2.5">
                <h3 className="text-lg font-extrabold text-ink">Stakeholder Digests</h3>
                <span className="rounded-full bg-brand px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide text-white">
                  Automated summaries
                </span>
              </div>
              <p className="text-sm leading-relaxed text-ink-light">
                Turn monitoring signals into a short, role-specific briefing for each team. Every digest is
                <strong> always generated and stored in-app</strong>; email delivery is optional (via AWS SES).
              </p>
              <p className="mt-3 mb-3 text-xs font-bold uppercase tracking-widest text-ink">How to use it</p>
              <OrderedSteps
                items={[
                  <>Create a <Pill>digest profile</Pill> for a role (e.g. <strong>PV</strong>, <strong>Brand</strong>, <strong>Medical Affairs</strong>) and pick which alert categories &amp; domains that team cares about.</>,
                  <>Set the <Pill>schedule</Pill> in plain language: choose <strong>Weekly</strong> or <strong>Daily</strong>, then the day and time. No cron syntax.</>,
                  <>Add <strong>recipients</strong> and delivery methods (in-app is always on; add <strong>email</strong> and/or a <strong>webhook</strong>).</>,
                  <>Use <Pill>Run Now</Pill> to generate on demand: a toast confirms the result, and each past digest shows its findings and whether it emailed (with the reason if not).</>,
                  <>Email needs SES configured: the page shows a readiness banner, and any send issue is reported per digest so you always know delivery status.</>,
                ]}
              />
              <div className="mt-4">
                <TabLink to="/digests" label="Stakeholder Digests" icon={Mail} />
              </div>
            </div>
          </div>
        </div>
      </AnimatedCard>

      {/* Backfills & data maintenance */}
      <AnimatedCard delay={0.14}>
        <div className="rounded-2xl border-2 border-brand-light/40 bg-gradient-to-br from-brand-surface/50 to-white p-6 shadow-md">
          <div className="flex gap-5">
            <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl bg-brand text-white shadow-sm">
              <RefreshCw size={22} strokeWidth={2.2} />
            </div>
            <div className="min-w-0 flex-1">
              <div className="mb-2 flex flex-wrap items-center gap-2.5">
                <h3 className="text-lg font-extrabold text-ink">Backfills &amp; data maintenance</h3>
                <span className="rounded-full bg-brand px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide text-white">
                  One-time after enabling
                </span>
              </div>
              <p className="text-sm leading-relaxed text-ink-light">
                New capabilities only process data captured <em>after</em> they are switched on. To fold in
                your existing history (so the new dashboards are not empty) run each relevant backfill
                once. All of them are <strong>idempotent</strong> (safe to re-run).
              </p>

              <div className="mt-4 space-y-2.5">
                {[
                  {
                    label: "Source Authority: classify historical citations",
                    ema: true,
                    how: (
                      <>
                        Ask Ema to <em>“backfill Source Authority”</em>, or click <Pill>Backfill</Pill> on the
                        Source Authority page (<Mono>POST /source-authority/classify/sweep</Mono>). Classifies
                        citations on responses captured before the feature existed; it drains all pending.
                      </>
                    ),
                  },
                  {
                    label: "Insights: tag historical responses with themes",
                    ema: true,
                    how: (
                      <>
                        Ask Ema to <em>“rebuild insights”</em>, or click <Pill>Rebuild</Pill> on the Insights
                        tab. Discovers the theme taxonomy and tags every past response.
                      </>
                    ),
                  },
                  {
                    label: "Scoring: score any unscored responses",
                    ema: true,
                    how: (
                      <>
                        Ask Ema to <em>“run a scoring sweep”</em>. Scores responses that were never scored
                        (e.g. imported or from an interrupted run).
                      </>
                    ),
                  },
                  {
                    label: "GEO Interventions: generate from current gaps",
                    ema: false,
                    how: (
                      <>
                        Click <Pill>Generate</Pill> on the GEO Interventions page to (re)build
                        recommendations from the latest scored competitive-position gaps.
                      </>
                    ),
                  },
                  {
                    label: "Comparison coverage: write the missing head-to-head questions",
                    ema: true,
                    how: (
                      <>
                        Open <strong>Comparison coverage</strong> at the top of Discover Questions, click{" "}
                        <Pill>Show what&rsquo;s missing</Pill> to see the gap, then <Pill>Generate questions</Pill> to
                        write it. Or ask Ema to <em>“generate the missing comparison questions”</em>. Candidates land
                        in the review queue, never straight into the bank.
                      </>
                    ),
                  },
                  {
                    label: "Compliance: re-redact stored text",
                    ema: true,
                    how: (
                      <>
                        Ask Ema to <em>“re-redact stored text”</em>, or an operator runs the sweep (<Mono>POST
                        /compliance/redact-sweep</Mono> or <Mono>scripts/redact_backfill.py</Mono>) after a
                        PHI/PII detector upgrade to clean already-stored harvest &amp; social text in place.
                      </>
                    ),
                  },
                ].map((b) => (
                  <div key={b.label} className="rounded-xl border border-slate-200 bg-white/70 p-4">
                    <div className="flex flex-wrap items-center gap-2">
                      <CheckCircle2 size={15} className="shrink-0 text-brand-light" />
                      <p className="text-sm font-bold text-ink">{b.label}</p>
                      {b.ema && (
                        <span className="rounded-full bg-brand-surface px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-brand">
                          Ema can run it
                        </span>
                      )}
                    </div>
                    <p className="mt-1 pl-6 text-xs leading-relaxed text-ink-light">{b.how}</p>
                  </div>
                ))}
              </div>

              <p className="mt-4 text-xs leading-relaxed text-ink-muted">
                Ema can run five of these directly (it confirms first): the <strong>Source Authority
                backfill</strong>, <strong>insights rebuild</strong>, <strong>scoring sweep</strong>,{" "}
                <strong>comparison coverage generate</strong>, and <strong>compliance re-redaction</strong>. The GEO
                Interventions generate lives on its own page.
              </p>
            </div>
          </div>
        </div>
      </AnimatedCard>

      {/* Personas reference */}
      <Card title="Personas at a glance">
        <div className="flex items-center gap-2 mb-4">
          <Users size={16} className="text-brand-light" />
          <p className="text-sm text-ink-light">Each question targets one audience, which determines who gets asked.</p>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          {PERSONAS.map((p) => (
            <div
              key={p.name}
              className={`rounded-xl border p-4 ${p.manual ? "border-cyan-300 bg-cyan-50" : "border-slate-200 bg-white"}`}
            >
              <div className="flex items-center justify-between mb-1.5">
                <p className="text-sm font-extrabold text-ink">{p.name}</p>
                {p.manual && (
                  <span className="px-2 py-0.5 rounded-full text-[10px] font-bold bg-cyan-600 text-white uppercase tracking-wide">
                    Manual
                  </span>
                )}
              </div>
              <p className="text-xs font-bold text-brand">{p.targets}</p>
              <p className="text-xs text-ink-light mt-1 leading-relaxed">{p.note}</p>
            </div>
          ))}
        </div>
      </Card>

      {/* Things to remember */}
      <Card title="Good to know">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-4">
          <div className="flex gap-3">
            <ShieldCheck size={16} className="text-brand-light shrink-0 mt-0.5" />
            <p className="text-sm text-ink-light leading-relaxed">
              <span className="font-bold text-ink">Approval gate:</span> only Medical-Affairs-<strong>APPROVED</strong>{" "}
              questions can run. Harvested questions always start as PENDING.
            </p>
          </div>
          <div className="flex gap-3">
            <Calendar size={16} className="text-brand-light shrink-0 mt-0.5" />
            <p className="text-sm text-ink-light leading-relaxed">
              <span className="font-bold text-ink">Daily runs:</span> toggle the schedule on the Pipeline tab. They run
              the full approved bank across every enabled model.
            </p>
          </div>
          <div className="flex gap-3">
            <AlertTriangle size={16} className="text-amber-500 shrink-0 mt-0.5" />
            <p className="text-sm text-ink-light leading-relaxed">
              <span className="font-bold text-ink">Adverse events:</span> harvested posts describing harm are
              quarantined and excluded from submission until safety review signs off.
            </p>
          </div>
          <div className="flex gap-3">
            <Stethoscope size={16} className="text-cyan-600 shrink-0 mt-0.5" />
            <p className="text-sm text-ink-light leading-relaxed">
              <span className="font-bold text-ink">EvidenceMD is Provider-only</span>: the clinical-reasoning model runs
              just for clinician questions, automatically, alongside the public platforms.
            </p>
          </div>
          <div className="flex gap-3">
            <Globe size={16} className="text-brand-light shrink-0 mt-0.5" />
            <p className="text-sm text-ink-light leading-relaxed">
              <span className="font-bold text-ink">Real provenance:</span> grounded targets return the actual sources
              they cited, surfaced on each response in Results.
            </p>
          </div>
        </div>
      </Card>
    </div>
  );
}
