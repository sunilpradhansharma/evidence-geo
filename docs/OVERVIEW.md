# Evidence Monitoring Agent: Project Overview

> **One-page summary.** For setup see [`README.md`](../README.md). For the full technical
> design see [`docs/TECHNICAL_DESIGN.md`](TECHNICAL_DESIGN.md).

## What it is

The Evidence Monitoring Agent measures **what large language models say about pharmaceutical
therapies**. It submits a Medical-Affairs-approved bank of questions to multiple LLMs across
three audiences (Prospect, Patient, Provider), stores every answer immutably, scores each one
for **brand sentiment** and **competitive positioning**, evaluates **cross-model consensus**,
detects **drift** between runs, fires **alerts** on concerning findings, and surfaces it all in
a React dashboard.

## Why it matters

Patients, prospects, and providers increasingly ask AI assistants about treatments. If a model
misrepresents a therapy, recommends a competitor, or hallucinates safety data, the manufacturer
needs to know. This system provides a repeatable, auditable measurement of that exposure across
models, audiences, and therapeutic areas (GEO: Generative Engine Optimization).

## Business value

Today, AI assistants are an unmonitored channel that shapes how patients and providers perceive
our therapies. This platform turns that blind spot into a measured, managed asset.

- **Protect brand reputation and revenue.** Catch misrepresentations, wrong safety claims, and
  competitor recommendations early, before they shift prescribing or erode patient trust.
- **Win the competitive narrative.** See where models favor competitors over our brands, by
  audience and therapeutic area, to inform medical and commercial strategy.
- **Reduce compliance and patient-safety risk.** Flag hallucinated safety claims, off-indication
  statements, and adverse-event signals for review, supporting pharmacovigilance.
- **Improve visibility in AI answers (GEO).** Pinpoint where models lack credible evidence for our
  therapies, guiding where to publish and correct the record.
- **Hear the true voice of the customer.** Surface real patient and provider questions that reveal
  unmet information needs traditional channels miss.
- **Build an auditable system of record.** Capture every answer immutably with Medical-Affairs
  governance, for a defensible, repeatable measurement.
- **Scale ahead of AI-first discovery.** Monitor many models, questions, and audiences
  automatically, at a scale manual review cannot match.

**Who it serves:** Medical Affairs, Brand and Commercial, Pharmacovigilance and Safety, and Market
Access teams.

## How it works (end to end)

1. **Discover patient questions.** Harvest real, verbatim questions from public health
   communities (Reddit, Quora, drugs.com, HealthUnlocked, patient.info) via live web search.
   Results are PII-scrubbed, deduped, classified by persona, and adverse-event posts are
   quarantined for review.
2. **Approve the question bank.** Medical Affairs reviews and approves questions before any run
   can use them. This is a two-gate governance flow: harvested to staged to approved.
3. **Run the analysis.** An orchestrator dispatches each approved question to every enabled LLM
   across the three personas — including EvidenceMD, an automated clinical-reasoning model, for the
   Provider persona — with retry, rate-limiting, budget guard, resume, and cancellation.
4. **Review AI responses.** Every answer is stored immutably and scored for sentiment and
   competitive positioning. Search-grounded models attach the real source URLs they cited.
5. **See insights and trends.** Dashboards roll up sentiment and positioning over time, surface
   recurring themes and signals, and offer a natural-language "Ask a Question" data surface.

## What we monitor

- **Audiences (personas):** Prospect, Patient, Provider.
- **Therapeutic areas:** Immunology (Humira, Skyrizi, Rinvoq), Oncology (Imbruvica, Venclexta),
  Rheumatology (Rinvoq, Humira), and the Lupron franchise (Lupron Depot, Lupron Depot-Ped) across
  Central Precocious Puberty, Endometriosis, and Uterine Fibroids.
- **LLM targets:** Anthropic Claude, Amazon Nova Pro, Meta Llama (via Amazon Bedrock), Google
  Gemini and OpenAI GPT-4o (both grounded with live web search for real citations), plus
  EvidenceMD — an automated clinical-reasoning API — for Provider-persona clinician-grade answers.

## Scoring and intelligence

- **Sentiment and positioning:** a "judge" LLM scores brand sentiment and a 5-class competitive
  position for every response.
- **Council of LLMs (consensus):** a Chairman step computes agreement level, divergence points,
  a synthesized final answer, and an overall sentiment and position across models.
- **Source provenance (GEO):** grounded models return the actual URLs and the specific claims
  they cited, resolved to the real source pages.
- **Drift detection and alerts:** changes between runs are detected and concerning findings raise
  alerts.
- **Theme discovery:** patterns and signals are surfaced across many responses (the
  "needle in a haystack" view).

## Social Listening (complementary surface)

Separate from the six-stage monitoring workflow, **Social Listening** measures what real people say
about monitored therapies on public social channels (Reddit, TikTok, Instagram, Facebook, X),
scraped via **Apify** for a demo Obesity/GLP-1 area.

- **Posts and comments.** Both posts and their comments/replies are captured. **Comment sentiment is
  tracked as a separate dimension from post sentiment**, so you can compare the original author's
  stance with the crowd's reaction.
- **Translation.** Non-English posts and comments are auto-translated to English (via the same
  scoring LLM, after redaction) and shown with a "Translated from \<language\> · Show original"
  toggle.
- **Guardrails.** All third-party text — posts and comments alike — is PII/PHI-scrubbed, screened for
  prompt-injection, and run through a fail-closed adverse-event check; AE signals from posts *and*
  comments route for pharmacovigilance review.
- **Dashboard.** Share of voice by brand/channel, post and comment sentiment, volume over time, top
  themes, adverse-event signals, and per-channel engagement leaders. These are captured-sample
  metrics, not market-level share of voice; engagement is compared per channel only.

Live ingestion needs `APIFY_API_TOKEN` in the backend `.env`. Captured posts and comments mirror into
Snowflake/Cortex alongside the monitoring data.

## Technology and architecture

- **Backend:** Python 3.11 with FastAPI and async SQLAlchemy on SQLite. A provider-agnostic LLM
  layer wraps Bedrock, Google, and OpenAI behind one contract. APScheduler powers optional daily
  scheduled runs.
- **Frontend:** React 18 with TypeScript, Vite, Tailwind CSS, and Recharts.
- **Optional warehouse:** all data can mirror into Snowflake, with a Cortex AI layer providing
  rollups and a natural-language "Ask your data" Q&A. Only the backend connects to Snowflake, so
  end-users never authenticate to it. Disabled by default.
- **Deployment:** a single Docker container (nginx plus uvicorn) on EC2, shipped via Bitbucket
  Pipelines.

## Governance, security, and privacy

- **Immutable and auditable:** responses are append-only and every external call is recorded in
  an append-only audit log with role tagging.
- **No PII:** harvested text is redacted with light-touch PII scrubbing; credentials are redacted
  from logs.
- **Content-agnostic code:** brands, therapeutic areas, and prompts live entirely in config, so
  the code carries no proprietary content.
- **Extensible by config:** adding a new LLM is one YAML entry plus a credential, with no core
  logic changes.

## Status and boundaries (POC)

- Runs locally on SQLite with Amazon Bedrock as the live provider.
- All brand, persona, and question data is synthetic but realistic, uses real brand names, and
  contains no PII.
- A single-container EC2 deploy exists today. Managed cloud infrastructure is a future step.
