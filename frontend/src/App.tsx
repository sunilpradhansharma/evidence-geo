import { motion } from "framer-motion";

import { useEffect } from "react";

import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";

import { api } from "./api/client";

import { applyTaxonomy, useTaxonomy } from "./lib/taxonomy";

import {

  BarChart3,

  BrainCircuit,

  Crosshair,

  DownloadCloud,

  FlaskConical,

  GitCompare,

  Pill,

  Sparkles,

  Scale,

  HelpCircle,

  ListChecks,

  Lightbulb,

  Megaphone,

  Network,

  Radar,

  Rocket,

  GitCommitVertical,

  Mail,

  ScanEye,

  Search,

  ScrollText,

  Snowflake,

  ShieldCheck,

  Share2,

  Swords,

  TrendingUp,

  Workflow,

  Wand2,

} from "lucide-react";

import Pipeline from "./pages/Pipeline";

import Results from "./pages/Results";

import Dashboard from "./pages/Dashboard";

import Insights from "./pages/Insights";

import Recommendations from "./pages/Recommendations";

import HeadToHead from "./pages/HeadToHead";

import ActivationImpact from "./pages/ActivationImpact";

import Cortex from "./pages/Cortex";

import SourceAuthority from "./pages/SourceAuthority";

import InfluenceGraph from "./pages/InfluenceGraph";

import Questions from "./pages/Questions";

import Harvest from "./pages/Harvest";

import SocialListening from "./pages/SocialListening";

import PromptVolume from "./pages/PromptVolume";

import HowToUse from "./pages/HowToUse";

import ModelReleases from "./pages/ModelReleases";

import Digests from "./pages/Digests";

import VariationTesting from "./pages/VariationTesting";

import EvidenceOverview from "./pages/EvidenceOverview";

import EvidenceNetworks from "./pages/EvidenceNetworks";

import EvidenceStudies from "./pages/EvidenceStudies";

import EvidenceGovernance from "./pages/EvidenceGovernance";

import CompetitorDiscovery from "./pages/CompetitorDiscovery";

import EvidenceAlignment from "./pages/EvidenceAlignment";

import EvidenceComparisons from "./pages/EvidenceComparisons";

import EvidenceDrugFacts from "./pages/EvidenceDrugFacts";

import EvidenceSynthesis from "./pages/EvidenceSynthesis";

import EvidenceIngest from "./pages/EvidenceIngest";

import ChatWidget from "./components/ChatWidget";

import { ComingSoonBanner } from "./components/ui";



const CHIP_TONES: Record<string, string> = {

  brand: "bg-brand-surface text-brand-dark",

  amber: "bg-amber-100 text-amber-800",

};



function NavChip({ children, tone = "brand" }: { children: string; tone?: string }) {

  return (

    <span

      className={`relative z-10 whitespace-nowrap rounded-full px-1.5 py-0.5 text-[9px] font-bold uppercase leading-none tracking-wide ${

        CHIP_TONES[tone] ?? CHIP_TONES.brand

      }`}

    >

      {children}

    </span>

  );

}



const NAV = [

  { to: "/dashboard", label: "Insights & Trends", icon: BarChart3 },

  { to: "/harvest", label: "Discover Questions", icon: Radar },

  { to: "/social-listening", label: "Social Listening", icon: Megaphone },

  { to: "/questions", label: "Approved Question Bank", icon: ListChecks },

  { to: "/prompt-volume", label: "Prompt Volume", icon: TrendingUp },

  { to: "/run-analysis", label: "Run Analysis", icon: Workflow },

  { to: "/results", label: "AI Response Review", icon: Search },

  { to: "/evidence", label: "Clinical Evidence", icon: FlaskConical, badge: "Coming Soon", badgeTone: "amber" },

  { to: "/digests", label: "Stakeholder Digests", icon: Mail },

  { to: "/how-to-use", label: "How to Use", icon: HelpCircle },

];



const DASHBOARD_SUBNAV = [

  { to: "/dashboard", label: "Overview", icon: BarChart3, end: true },

  { to: "/dashboard/insights", label: "Insights", icon: BrainCircuit },

  { to: "/dashboard/head-to-head", label: "Head-to-Head", icon: Swords, badge: "Coming Soon", badgeTone: "amber" },

  { to: "/dashboard/recommendations", label: "GEO Interventions", icon: Lightbulb },

  { to: "/dashboard/activation-impact", label: "Activation & Impact", icon: Rocket },

  { to: "/dashboard/source-authority", label: "Source Authority", icon: ShieldCheck },

  { to: "/dashboard/influence-graph", label: "Influence Graph", icon: Share2, badge: "Beta" },

  { to: "/dashboard/ai-update-impact", label: "AI Update Impact", icon: GitCommitVertical },

  { to: "/dashboard/cortex", label: "Ask a Question", icon: Snowflake },

];



function DashboardSection() {

  return (

    <div>

      {/* ── Dashboard sub-tabs ── */}

      <div className="mb-6 flex flex-wrap items-center gap-0.5 p-1 bg-slate-100 rounded-xl w-fit">

        {DASHBOARD_SUBNAV.map(({ to, label, icon: Icon, end, badge, badgeTone }) => (

          <NavLink key={to} to={to} end={end}>

            {({ isActive }) => (

              <div

                className={`relative flex items-center gap-2 px-4 py-2 text-sm font-bold rounded-lg cursor-pointer transition-colors duration-200 ${

                  isActive ? "text-brand-dark" : "text-slate-700 hover:text-slate-900"

                }`}

              >

                {isActive && (

                  <motion.div

                    layoutId="subnav-indicator"

                    className="absolute inset-0 bg-white shadow-sm rounded-lg"

                    transition={{ type: "spring", bounce: 0.2, duration: 0.4 }}

                  />

                )}

                <Icon size={15} strokeWidth={2.2} className="relative z-10" />

                <span className="relative z-10">{label}</span>

                {badge && <NavChip tone={badgeTone}>{badge}</NavChip>}

              </div>

            )}

          </NavLink>

        ))}

      </div>



      <Routes>

        <Route index element={<Dashboard />} />

        <Route path="insights" element={<Insights />} />

        <Route path="head-to-head" element={<HeadToHead />} />

        <Route path="recommendations" element={<Recommendations />} />

        <Route path="activation-impact" element={<ActivationImpact />} />

        <Route path="source-authority" element={<SourceAuthority />} />

        <Route path="influence-graph" element={<InfluenceGraph />} />

        <Route path="ai-update-impact" element={<ModelReleases />} />

        <Route path="cortex" element={<Cortex />} />

      </Routes>

    </div>

  );

}



// X2 — every evidence surface hangs off ONE top-level tab with a sub-nav, so later phases
// extend this list instead of adding tabs to the header (which is already 10 wide and scrolls).
const EVIDENCE_SUBNAV = [
  { to: "/evidence", label: "Overview", icon: FlaskConical, end: true },
  { to: "/evidence/networks", label: "Networks", icon: Network },
  { to: "/evidence/comparisons", label: "Comparisons", icon: GitCompare },
  { to: "/evidence/studies", label: "Studies", icon: BrainCircuit },
  { to: "/evidence/drug-facts", label: "Drug Evidence", icon: Pill },
  { to: "/evidence/governance", label: "Governance", icon: ScrollText },
  { to: "/evidence/competitors", label: "Competitor Discovery", icon: Crosshair },
  { to: "/evidence/alignment", label: "AI vs Evidence", icon: Scale },
  { to: "/evidence/synthesis", label: "Synthesis", icon: Sparkles },
  { to: "/evidence/ingest", label: "Ingest", icon: DownloadCloud },
];
// NOTE ON CROWDING — THE NINE-PILL LIMIT WAS OVERRIDDEN, DELIBERATELY, FOR THE TENTH.
// X2 exists because the top-level header overflows and scrolls at nine tabs, and the note
// that stood here said the next evidence surface must fold into an existing tab rather than
// become a tenth pill. Ingest did not fold: it is the only WRITE surface in the section, and
// hiding a form that spends an external API budget and grows the corpus behind a filter on a
// read page is a worse outcome than one more pill.
//
// The mitigation is that the container below now WRAPS instead of overflowing, so ten pills
// reflow onto a second line rather than reproducing the horizontal scroll this phase existed
// to fix. That is a real fix for the stated symptom, and it does not license an eleventh —
// the next surface still folds in.

function EvidenceSection() {
  return (
    <div>
      <ComingSoonBanner
        title="Clinical Evidence is coming soon"
        message={
          "This section is still being built. The trial data, networks and comparisons below are an " +
          "early preview: the corpus is incomplete and nothing here has been through medical review, " +
          "so please do not use it for decisions yet."
        }
      />

      {/* ── Evidence sub-tabs (wraps: see the crowding note above) ── */}
      <div className="mb-6 flex flex-wrap items-center gap-0.5 p-1 bg-slate-100 rounded-xl w-fit">
        {EVIDENCE_SUBNAV.map(({ to, label, icon: Icon, end }) => (
          <NavLink key={to} to={to} end={end}>
            {({ isActive }) => (
              <div
                className={`relative flex items-center gap-2 px-4 py-2 text-sm font-bold rounded-lg cursor-pointer transition-colors duration-200 ${
                  isActive ? "text-brand-dark" : "text-slate-700 hover:text-slate-900"
                }`}
              >
                {isActive && (
                  <motion.div
                    layoutId="evidence-subnav-indicator"
                    className="absolute inset-0 bg-white shadow-sm rounded-lg"
                    transition={{ type: "spring", bounce: 0.2, duration: 0.4 }}
                  />
                )}
                <Icon size={15} strokeWidth={2.2} className="relative z-10" />
                <span className="relative z-10">{label}</span>
              </div>
            )}
          </NavLink>
        ))}
      </div>

      <Routes>
        <Route index element={<EvidenceOverview />} />
        <Route path="networks" element={<EvidenceNetworks />} />
        <Route path="comparisons" element={<EvidenceComparisons />} />
        <Route path="studies" element={<EvidenceStudies />} />
        <Route path="drug-facts" element={<EvidenceDrugFacts />} />
        <Route path="governance" element={<EvidenceGovernance />} />
        <Route path="competitors" element={<CompetitorDiscovery />} />
        <Route path="alignment" element={<EvidenceAlignment />} />
        <Route path="synthesis" element={<EvidenceSynthesis />} />
        <Route path="ingest" element={<EvidenceIngest />} />
      </Routes>
    </div>
  );
}

const RUN_ANALYSIS_SUBNAV = [
  { to: "/run-analysis", label: "Standard Run", icon: Workflow, end: true },
  { to: "/run-analysis/variations", label: "Phrasing Variation", icon: Wand2 },
];

function RunAnalysisSection() {
  return (
    <div>
      {/* ── Run Analysis sub-tabs ── */}
      <div className="mb-6 flex items-center gap-0.5 p-1 bg-slate-100 rounded-xl w-fit">
        {RUN_ANALYSIS_SUBNAV.map(({ to, label, icon: Icon, end }) => (
          <NavLink key={to} to={to} end={end}>
            {({ isActive }) => (
              <div
                className={`relative flex items-center gap-2 px-4 py-2 text-sm font-bold rounded-lg cursor-pointer transition-colors duration-200 ${
                  isActive ? "text-brand-dark" : "text-slate-700 hover:text-slate-900"
                }`}
              >
                {isActive && (
                  <motion.div
                    layoutId="run-subnav-indicator"
                    className="absolute inset-0 bg-white shadow-sm rounded-lg"
                    transition={{ type: "spring", bounce: 0.2, duration: 0.4 }}
                  />
                )}
                <Icon size={15} strokeWidth={2.2} className="relative z-10" />
                <span className="relative z-10">{label}</span>
              </div>
            )}
          </NavLink>
        ))}
      </div>

      <Routes>
        <Route index element={<Pipeline />} />
        <Route path="variations" element={<VariationTesting />} />
      </Routes>
    </div>
  );
}

// Preserve the old /variation-testing deep links (e.g. ?group=...) by redirecting
// into the nested Run Analysis sub-tab.
function VariationTestingRedirect() {
  const { search } = useLocation();
  return <Navigate to={`/run-analysis/variations${search}`} replace />;
}

export default function App() {

  // Load the taxonomy from the backend once, then let the tree re-render with it.
  //
  // `lib/taxonomy.ts` ships a hardcoded copy as a fallback so the first paint and any
  // offline case still render usable pickers. Subscribing here is what makes the swap
  // visible: the exports are live module bindings, so every descendant reading them
  // picks up the new values on this re-render without importing anything itself.
  //
  // A failure is swallowed on purpose — stale options beat empty ones.
  useTaxonomy();

  useEffect(() => {

    api.taxonomy().then(applyTaxonomy).catch(() => {});

  }, []);

  return (

    <div className="min-h-screen bg-slate-100">

      {/* ── Top header bar ── */}

      <header className="fixed top-0 left-0 right-0 z-40 bg-brand-dark">

        <div className="flex items-center gap-3 px-6 h-14">

          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-light/15 ring-1 ring-brand-light/25">

            <ScanEye className="text-brand-light" size={20} strokeWidth={2} />

          </div>

          <div className="flex flex-col justify-center leading-tight">

            <span className="text-[15px] font-bold tracking-tight text-white">

              AI Brand Intelligence

            </span>

            <span className="mt-0.5 text-[11px] font-medium tracking-wide text-white/55">

              Generative Engine Optimization Platform

            </span>

          </div>

        </div>

      </header>



      {/* ── Animated tab nav ── */}

      <div className="fixed top-14 left-0 right-0 z-30 bg-white border-b border-slate-200 shadow-sm">

        <nav className="flex items-center px-4 py-2 gap-0.5 overflow-x-auto [&::-webkit-scrollbar]:hidden [-ms-overflow-style:none] [scrollbar-width:none]">

          {NAV.map(({ to, label, icon: Icon, badge, badgeTone }) => (

            <NavLink key={to} to={to} className="shrink-0 flex-1 min-w-fit">

              {({ isActive }) => (

                <div

                  className={`relative flex items-center justify-center gap-2 px-3 py-2 text-sm font-bold rounded-lg cursor-pointer transition-colors duration-200 ${

                    isActive ? "text-brand-dark" : "text-slate-700 hover:text-slate-900"

                  }`}

                >

                  {isActive && (

                    <motion.div

                      layoutId="nav-tab-indicator"

                      className="absolute inset-0 bg-brand/10 rounded-lg"

                      transition={{ type: "spring", bounce: 0.2, duration: 0.5 }}

                    />

                  )}

                  <Icon size={16} strokeWidth={2.2} className="relative z-10" />

                  <span className="relative z-10 whitespace-nowrap">{label}</span>

                  {badge && <NavChip tone={badgeTone}>{badge}</NavChip>}

                </div>

              )}

            </NavLink>

          ))}

        </nav>

      </div>



      {/* ── Main content – full screen ── */}

      <main className="pt-[104px] min-h-screen overflow-x-hidden">

        <div className="px-3 py-4 sm:px-6 sm:py-6">

          <Routes>

            <Route path="/" element={<Navigate to="/dashboard" replace />} />

            <Route path="/run-analysis/*" element={<RunAnalysisSection />} />

            <Route path="/variation-testing" element={<VariationTestingRedirect />} />

            <Route path="/results" element={<Results />} />

            <Route path="/dashboard/*" element={<DashboardSection />} />

            <Route path="/insights" element={<Navigate to="/dashboard/insights" replace />} />

            <Route path="/harvest" element={<Harvest />} />

            <Route path="/social-listening" element={<SocialListening />} />

            <Route path="/discovery" element={<Navigate to="/harvest" replace />} />

            <Route path="/questions" element={<Questions />} />

            <Route path="/prompt-volume" element={<PromptVolume />} />

            <Route path="/evidence/*" element={<EvidenceSection />} />

            <Route path="/model-releases" element={<Navigate to="/dashboard/ai-update-impact" replace />} />

            <Route path="/digests" element={<Digests />} />

            <Route path="/how-to-use" element={<HowToUse />} />

          </Routes>

        </div>

      </main>



      {/* ── Global AI Chat Assistant (bottom-right, all pages) ── */}

      <ChatWidget />

    </div>

  );

}

