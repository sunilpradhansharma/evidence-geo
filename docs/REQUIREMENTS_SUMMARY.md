# Evidence Monitoring Agent: Requirements (Brief)

*Full spec: [`docs/REQUIREMENTS.md`](REQUIREMENTS.md). Design: [`docs/TECHNICAL_DESIGN.md`](TECHNICAL_DESIGN.md).*

## Problem

Patients, prospects, and providers increasingly ask AI assistants (ChatGPT, Gemini, Claude, Perplexity,
OpenEvidence) what to take or prescribe, and for many that answer is their only research. The channel is
invisible to the manufacturer: answers vary by phrasing and model, drift silently over time, and leave
no record. A model may misrepresent a therapy, omit the brand, favor a competitor, hallucinate claims,
surface an adverse event, or cite weak evidence. SEO analytics, social listening, MLR, and manual
spot-checks do not measure it. As AI answers become a primary discovery surface, continuous, structured
measurement is needed to detect drift, quantify exposure, and respond.

## Users

Primary user is a non-technical brand/marketing manager; Medical Affairs and Pharmacovigilance review
and approve. Questions are asked through three personas: Prospect, Patient, and Provider.

## Goals

A repeatable, auditable record of what each model says per therapy and persona over time; quantified
sentiment and competitive positioning; change detection and alerting; evidence-gap detection; governed
(approved questions only), private (no PII), and auditable. It is not medical advice, not an attempt to
manipulate models, and not a replacement for MLR or pharmacovigilance (it feeds them).

## What it must do

- **Question bank:** approved, versioned questions tagged by persona, therapeutic area, brand, and
  domain; CSV import with PII linting; coverage reporting; soft-delete only.
- **Run engine:** send every approved question to every enabled model with persona prompts; concurrent
  with rate limits, retries, dry-runs, filtered ad-hoc runs, and a per-run cost budget.
- **Responses:** stored verbatim and immutable with full metadata; queryable and exportable (CSV, JSON);
  run-over-run difference.
- **Scoring and alerts:** sentiment (-1 to +1) and competitive position with key claims and rationale;
  alerts on low sentiment, "not recommended", competitor advantage, or adverse events; cross-model
  consensus; versioned re-scoring and human overrides.
- **Scheduling:** unattended, time-zone-aware runs that are resumable and cancellable; Provider runs
  query EvidenceMD (an automated clinical model) with no manual pause.
- **Dashboard:** marketer-first and insights-first; leads with recommended next steps; plain-English
  labels; natural-language "Ask a Question"; no install required.
- **Discovery and social listening:** mine real patient questions from public communities and capture
  public social posts (Reddit, Facebook, TikTok, X, YouTube); scrub PII, screen, translate, quarantine
  adverse events; report sentiment, share of voice, and volume on a repeatable schedule.

## Cross-cutting

No PII; append-only responses and audit log; brand and competitor content in configuration, not code;
prompt-injection screening; concurrent and fault-tolerant (one failure never aborts a run); durable and
resumable; per-target health checks; new models added by config only; multiple providers behind one
contract; optional analytics-warehouse mirror for rollups and natural-language query; 24-month retention.

## Done when

Scheduled runs complete unattended for days; at least 30 questions per persona across two or more
therapeutic areas; 95% or better capture per model; everything scored and auditable; alerts fire on
change and risk; a new model is added by configuration; only approved, PII-free questions run; and a
marketer reaches insights and the top action without training.

## Scope

**In:** the above on synthetic but realistic data (real brand names, no PII). **Out (this phase):** MLR
integration, production alert channels beyond a webhook, end-user auth and RBAC, multi-tenancy, a managed
clinical-accuracy corpus, data-platform integration (Veeva, Salesforce, data lake), and mobile or native
apps. **Open:** final product name (pending brand sign-off); monitored models, areas, and frequency;
retention and alert-threshold defaults.
