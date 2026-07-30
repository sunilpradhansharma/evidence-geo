# End-to-End User Journey

This document maps the **whole product** as a user experiences it: the stages a user moves through to achieve what the platform is built for, which is to **see and shape how AI engines (Claude, Nova-Pro, Llama, Gemini, GPT-4o, plus EvidenceMD for clinicians) answer questions about your brands**.

It has two diagrams: a **journey overview** for the big picture, and a **detailed end-to-end flow** with every input source, the governance gate, the run, automated analysis, the review surfaces, distribution, and the improvement loops.

> Scope: the full app across the main navigation (`frontend/src/App.tsx`) and the `HowToUse` guide. For the run engine internals see [`RUN_ANALYSIS_FLOW.md`](./RUN_ANALYSIS_FLOW.md); for the system view see [`architecture.md`](./architecture.md).

## Legend

- **Terminal / goal** - rounded pill nodes are the user's goal or a place the work is consumed.
- **Process** - blue rectangles are things the user does or a page does for them.
- **Decision** - amber diamonds are branch points (mode, review, how to run).
- **Agent step** - orange nodes are automated AI/agent work (classify, score, consensus).
- **AI platforms** - purple node is the fan-out to the LLM targets.
- **Data / persist** - slate nodes are captured data and the analytics store.
- **Best-effort / optional** - dashed nodes and links never block the main flow if skipped.
- **Governance gate** - nothing runs until Medical Affairs approves it in the Question Bank.

---

## 1. Journey Overview

The core path from raw demand to action, the way most users describe it.

```mermaid
flowchart LR
  subgraph GATHER["1 · Gather demand"]
    direction TB
    G1["Discover Questions<br/>(Harvest)"]
    G2["Prompt Volume<br/>(SEMrush)"]
    G3["Import Prompts<br/>(CSV)"]
    G4["Social Listening"]
  end

  GATHER --> BANK["2 · Approved Question Bank<br/>Medical Affairs approve<br/>+ phrasing variations"]
  BANK --> RUN["3 · Run Analysis<br/>ask Claude · Nova-Pro · Llama ·<br/>Gemini · GPT-4o (+ EvidenceMD)"]
  RUN --> AUTO["4 · Auto-analysis<br/>score · consensus ·<br/>source authority · alerts"]
  AUTO --> REVIEW["5 · Review<br/>AI Response Review<br/>+ Insights dashboards"]
  REVIEW --> ACT(["6 · Act + distribute<br/>GEO interventions ·<br/>Stakeholder Digests"])
  ACT -.->|new gaps become questions| GATHER

  classDef src fill:#E0F2FE,stroke:#0284C7,color:#0C4A6E;
  classDef step fill:#F1F5F9,stroke:#475569,color:#1E293B;
  classDef out fill:#ECFDF5,stroke:#10B981,color:#065F46,font-weight:bold;
  class G1,G2,G3,G4 src;
  class BANK,RUN,AUTO,REVIEW step;
  class ACT out;
```

**Notes**

- The journey is a **loop**, not a straight line: gaps found in the dashboards feed new questions back into the bank for the next cycle.
- **Social Listening** is complementary intelligence that runs alongside the core flow; it has its own dashboard and can inspire new questions.
- A **daily scheduled run** keeps stages 3 to 5 turning automatically once the bank is approved.

---

## 2. Detailed End-to-End Flow

Every stage, decision, input source, output surface, and improvement loop, plus the always-on **Ema** copilot.

```mermaid
flowchart TD
  START(["User goal:<br/>see and shape how AI engines answer<br/>questions about our brands"]):::terminal

  %% ---------- Phase 0: setup ----------
  subgraph P0["Phase 0 · Setup (occasional)"]
    direction TB
    CFG["Configure scope:<br/>brands · therapeutic areas ·<br/>personas · API keys"]:::process
    MODE{"Monitoring mode?"}:::decision
    CFG --> MODE
  end
  START --> CFG

  %% ---------- Phase 1: gather ----------
  subgraph P1["Phase 1 · Gather demand signals (what to ask)"]
    direction TB
    HARV["Discover Questions / Harvest:<br/>scrape Reddit · Quora · drugs.com ·<br/>HealthUnlocked · patient.info (Tavily)"]:::process
    PVOL["Prompt Volume:<br/>SEMrush search demand +<br/>high-volume gap topics"]:::process
    IMPP["Import Prompts:<br/>Profound / AlsoAsked / PAA CSV"]:::process
    CLEAN["Auto per candidate:<br/>PII scrub · de-dupe · classify<br/>persona/brand/theme · flag adverse events"]:::agent
    HARV --> CLEAN
    PVOL --> CLEAN
    IMPP --> CLEAN
  end
  MODE -->|AbbVie focus brands| HARV
  MODE -->|All Brands landscape| HARV
  MODE -.-> PVOL
  MODE -.-> IMPP

  %% ---------- Parallel: social listening ----------
  subgraph SL["Parallel · Social Listening (complementary)"]
    direction TB
    SOC["Ingest posts + comments:<br/>Reddit · TikTok · Instagram · FB · X (Apify)"]:::data
    SOCP["PII scrub · adverse-event screen ·<br/>classify brand/topic/sentiment ·<br/>translate + AI narrative brief"]:::agent
    SOC --> SOCP
  end
  MODE -.-> SOC

  %% ---------- Phase 2: governance ----------
  subgraph P2["Phase 2 · Approved Question Bank (governance gate)"]
    direction TB
    PEND["New questions land as PENDING"]:::process
    REVIEW{"Medical Affairs review"}:::decision
    APPR(["APPROVED<br/>eligible to run"]):::terminal
    DENY["Denied / parked"]:::stop
    VARY["Optional: generate phrasing<br/>variations of a question"]:::process
    REVIEW -->|approve| APPR
    REVIEW -->|deny| DENY
    APPR --> VARY
  end
  CLEAN --> PEND
  PEND --> REVIEW

  %% ---------- Phase 3: run ----------
  subgraph P3["Phase 3 · Run Analysis (execute)"]
    direction TB
    LAUNCH{"How to run?"}:::decision
    QRUN["Quick Run from bank<br/>(persona/therapy/theme filters)"]:::process
    CSVR["CSV import-and-run"]:::process
    ADHOC["Run Selected (ad-hoc)"]:::process
    SCHED["Scheduled daily sweep"]:::process
    ORCH["Orchestrator:<br/>injection gate · intent triage ·<br/>persona routing · budget guard"]:::agent
    LLMS["Ask the AI platforms:<br/>Claude · Nova-Pro · Llama · Gemini ·<br/>GPT-4o (+ EvidenceMD for Provider)"]:::llm
    LAUNCH --> QRUN --> ORCH
    LAUNCH --> CSVR --> ORCH
    LAUNCH --> ADHOC --> ORCH
    LAUNCH --> SCHED --> ORCH
    ORCH --> LLMS
  end
  APPR --> LAUNCH
  VARY -.-> LAUNCH

  %% ---------- Phase 4: auto-analysis ----------
  subgraph P4["Phase 4 · Automated analysis (backend, best-effort)"]
    direction TB
    SCORE["Score each answer:<br/>sentiment · competitive position ·<br/>intent · adverse-event flag"]:::agent
    CONS["Chairman consensus:<br/>FULL/PARTIAL/MISSING + synthesized<br/>final answer + overall sentiment"]:::agent
    SRCA["Source Authority:<br/>classify cited domains<br/>(owned/earned/competitor/unverified)"]:::agent
    DIFF["Diff vs prior run · alerts ·<br/>model-release correlation ·<br/>tag themes (Insights)"]:::post
    STORE[("Persist + Snowflake mirror")]:::data
    SCORE --> CONS --> SRCA --> DIFF --> STORE
  end
  LLMS --> SCORE

  %% ---------- Phase 5: review ----------
  subgraph P5["Phase 5 · Review and analyze"]
    direction TB
    RESP["AI Response Review:<br/>answers · sources · consensus ·<br/>final answer · export"]:::process
    OVER["Dashboard Overview:<br/>recommended next steps · sentiment ·<br/>positioning · consensus · alerts"]:::process
    INS["Insights:<br/>auto-discovered themes + signals"]:::process
    SAUTH["Source Authority dashboard:<br/>share of voice · competitor pages"]:::process
    GEO["GEO Interventions:<br/>ranked content fixes (SEMrush)"]:::process
    AIU["AI Update Impact:<br/>answer shifts vs model releases"]:::process
    ASK["Ask a Question (Cortex):<br/>natural language over Snowflake"]:::process
  end
  STORE --> RESP
  STORE --> OVER
  STORE --> INS
  STORE --> SAUTH
  STORE --> GEO
  STORE --> AIU
  STORE --> ASK

  %% ---------- Phase 6: act ----------
  subgraph P6["Phase 6 · Act and distribute"]
    direction TB
    DIG["Stakeholder Digests:<br/>role briefings (PV / Brand / MA)<br/>in-app + email/webhook"]:::process
    ACT(["Content + strategy actions:<br/>publish GEO fixes · brief teams"]):::terminal
    DIG --> ACT
  end
  OVER --> DIG
  AIU --> DIG
  SAUTH --> ACT
  GEO --> ACT

  %% ---------- Improvement loops ----------
  GEO -.->|new gap topics become questions| PEND
  PVOL -.->|gap topic to draft| PEND
  SOCP -.->|themes inspire questions| PEND
  ACT -.->|continuous monitoring loop| LAUNCH
  SCHED -.->|daily cadence| ORCH

  %% ---------- Cross-cutting: Ema copilot ----------
  EMA{{"Ema copilot (every page):<br/>start runs · harvest · rebuild insights ·<br/>backfills · answer questions"}}:::optional
  EMA -.-> HARV
  EMA -.-> LAUNCH
  EMA -.-> INS
  EMA -.-> ASK

  classDef process fill:#EFF6FF,stroke:#3B82F6,color:#1E3A8A;
  classDef decision fill:#FFFBEB,stroke:#F59E0B,color:#92400E;
  classDef agent fill:#FFF7ED,stroke:#EA580C,color:#7C2D12;
  classDef llm fill:#F5F3FF,stroke:#8B5CF6,color:#4A1078;
  classDef data fill:#F1F5F9,stroke:#475569,color:#1E293B;
  classDef terminal fill:#ECFDF5,stroke:#10B981,color:#065F46,font-weight:bold;
  classDef stop fill:#FEF2F2,stroke:#EF4444,color:#991B1B;
  classDef post fill:#EDE7F6,stroke:#4527A0,color:#1B1240;
  classDef optional fill:#FAF5FF,stroke:#A855F7,color:#581C87,stroke-dasharray:5 4;
```

---

## 3. Where each stage lives in the app

| Stage | Navigation | What the user does |
| --- | --- | --- |
| 0 · Setup | Config + `.env` | Set brands / therapeutic areas / personas and API keys; pick a Monitoring Mode (AbbVie focus brands vs All Brands landscape). |
| 1 · Gather demand | Discover Questions, Prompt Volume, (Import Prompts), Social Listening | Harvest real questions from the web, pull SEMrush demand and gap topics, import prompt CSVs, and listen to social channels. Everything is PII-scrubbed, de-duplicated, classified, and adverse-event screened. |
| 2 · Approve bank | Approved Question Bank | Medical Affairs approves or denies PENDING questions (the governance gate). Optionally generate phrasing variations. Only APPROVED questions can run. |
| 3 · Run Analysis | Run Analysis (Standard Run / Phrasing Variation) | Launch a run on demand, from CSV, ad-hoc from the bank, or on a daily schedule. The orchestrator triages intent, routes by persona, and asks the AI platforms. Provider questions also query EvidenceMD automatically. |
| 4 · Auto-analysis | (backend) | Each answer is scored for sentiment, competitive position, intent, and adverse events; the Chairman computes consensus and a synthesized final answer; citations are classified; diffs, alerts, model-release correlation, and theme tagging run; data is mirrored to Snowflake. |
| 5 · Review + analyze | AI Response Review, Insights and Trends (Overview / Insights / Source Authority / GEO Interventions / AI Update Impact / Ask a Question) | Read scored answers with sources and consensus; track sentiment, positioning, and alerts; explore themes; see which domains the models cite; get ranked content fixes; check whether shifts line up with model releases; ask questions in plain English. |
| 6 · Act + distribute | GEO Interventions, Stakeholder Digests | Turn findings into content and strategy actions; send role-specific briefings (PV, Brand, Medical Affairs) in-app and by email or webhook. |

**Cross-cutting: Ema copilot** is available on every page and can start runs, run harvest, rebuild insights, run backfills, and answer questions about your data.

## 4. Key accuracy notes

- **Governance is mandatory.** Nothing runs until a question is APPROVED in the Question Bank, regardless of which source produced it.
- **Demand sources are independent.** Harvest, Prompt Volume, and Import Prompts each feed PENDING questions; you do not need all of them.
- **Social Listening is a parallel intelligence stream**, separate from the five-stage core flow; it has its own dashboard and can inspire new questions rather than feeding runs directly.
- **The AI platform set is config-driven.** The five public platforms answer every persona; EvidenceMD is added only for Provider-persona questions and only when its API key is set. The dashboards can hide retired targets.
- **Phase 4 is best-effort and runs after the run is already COMPLETED**, so a just-finished run can briefly show scores as pending in AI Response Review. See [`RUN_ANALYSIS_FLOW.md`](./RUN_ANALYSIS_FLOW.md) for the exact engine lifecycle and outcomes.
- **The loop is the point.** GEO gaps, Prompt Volume gaps, and social themes flow back into the bank, and a daily schedule keeps the cycle running with minimal manual effort.
