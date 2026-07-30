# BR-008a — Stakeholder-Differentiated Digests: Research & Build-Effort Findings

| Field | Value |
| --- | --- |
| **Requirement** | BR-008a (REPLACES / EXTENDS BR-008) — Stakeholder-Differentiated Digests |
| **Priority** | Medium |
| **Status** | Research / Draft (no code written) |
| **Date researched** | 2026-07-09 |
| **Purpose** | (1) Decompose the requirement, (2) determine whether/how Profound (tryprofound.com) provides it, (3) assess what it would take for us to build it — money (subscription / purchase / platform) vs. human engineering effort. |

> **Honesty note:** Where a fact could not be confirmed from public sources, it is explicitly flagged as *not found* / *not confirmed* rather than assumed. No pricing figures are invented.

---

## TL;DR

- **Profound does NOT ship a stakeholder-differentiated pharma digest as a feature.** It provides the *building blocks* to assemble role-targeted periodic summaries: a **Reports API + "Report Generation"**, **Custom Dashboards** (with public share links), native **Slack** integration, and a schedulable **Agents/Workflows** automation layer with output nodes (Google Suite, Notion, Slack, Gamma, etc.).
- Profound's **"Personas" are audience/market segments (how customers query AI)**, **not** internal recipient roles (Medical Affairs, Pharmacovigilance, Market Access, etc.). There is no pharma-role or PV/adverse-event concept.
- Profound pricing is **custom enterprise (Starter / Growth / Enterprise), credit-based for Agents — no public numbers.**
- **To satisfy BR-008a we do NOT need to buy anything mandatory.** It is a **human-engineering effort** on our existing stack (FastAPI + APScheduler + Bedrock) using open-source libraries. Optional low-cost item: a transactional-email provider (or free corporate SMTP).
- We already have the **content sources** (analytics, insights themes/trends/signals, alerts) and an **LLM** for the executive summary. We are missing the **delivery layer** (email/webhook/PDF), a **role/routing model + admin UI**, and (currently) **RBAC**.

---

## 1. Requirement decomposed into capability primitives

| # | Sub-requirement | Capability primitive |
| --- | --- | --- |
| BR-008a.1 | Periodic digests, configurable cadence (default weekly) | Per-profile scheduling |
| BR-008a.2 / .3 | Role-differentiated content; 6 default role profiles; lead with top 3–5 findings | Role content templates + prioritization |
| BR-008a.4 | Routing (recipients, roles, channel) configurable without code | No-code admin routing UI + config model |
| BR-008a.5 | Deliverable via email, exportable PDF/HTML, webhook | Multi-channel delivery layer |
| BR-008a.6 | Plain-English executive summary paragraph (2–4 sentences) | LLM summarization |
| BR-008a.7 | Digest generation logged (recipient roles, delivery status, run ref) | Audit logging |

Default role profiles required: **Brand / Commercial, Medical Affairs, Pharmacovigilance, Market Access, Corporate Affairs & Communications, Pipeline Commercialization.**

---

## 2. How Profound does it

### 2.1 What Profound is

A horizontal **GEO / AEO (Generative / Answer Engine Optimization) marketing platform**. It tracks brand **visibility, citations, and sentiment** across AI answer engines (ChatGPT, Gemini, Perplexity, Claude, Google AI Overviews, Grok, etc.) and tracks **AI-crawler traffic** to your website ("Agent Analytics").

Verified enterprise/procurement facts (from their blog release titles):

- **$96M Series C at ~$1B valuation** (plus prior $35M, $20M, $3.5M rounds).
- **SOC 2 Type 2** certified.
- **"HIPAA compliance for healthcare AEO"** — relevant for pharma data handling.
- Enterprise **SSO (OIDC / SAML)**, **Agency Mode**, **Projects / Folders** for org separation.

### 2.2 Building blocks that map to "digests" (verified from Profound docs)

- **Reports (native + API).** The REST API has a dedicated **`Reports`** endpoint group and a **"Report Generation"** API feature, alongside `Answers`, `Categories`, `Content Optimization`, `Agents`, `Organization`. Report types include **Visibility Reports, Sentiment Analysis, Citation Reports**, and **"Prompt Research Reports."**
  - Source: `https://docs.tryprofound.com/rest-api/introduction` + Profound MCP capabilities.
- **Custom Dashboards + public share links.** Blog: *"Introducing Custom Dashboards"*, *"Share Custom Dashboards with public links."*
- **Slack integration (native).** Blog: *"Profound now integrates with Slack"*, *"Collaborate with Profound in Slack"*, *"Scale content operations with CMS and Slack integrations."* This is their clearest push/delivery channel.
- **Agents / Workflows** — a schedulable automation engine (formerly "Workflows," rebranded "Agents") with an **Agent Template Marketplace** and integration **nodes** (Google Suite, Google Drive, Notion, Slack, Gamma decks, Webflow, Google Search Console, etc.). This is the mechanism by which a customer would *assemble and deliver* a periodic, audience-targeted summary.
- **Ask Profound** (NL Q&A), **Profound MCP server**, **External MCP Connectors**, **Knowledge Bases**, **Documents** — data-access + context primitives.
- **Raw Data Access API** — pull unprocessed data to build a custom reporting layer.

### 2.3 What Profound does NOT have / could NOT be confirmed (precise)

- **No pharma-stakeholder role profiles.** Profound's **"Personas"** are **audience/market segments** (*"Understand your most important audience segments with Personas"*, *"Demographic Information in Prompt Volumes"*) — how *your customers* query AI, **not** internal recipient roles (MA / PV / Market Access / Corporate Affairs / Pipeline). **No packaged role-differentiated digest** keyed to internal functions was found.
- **No confirmed turnkey "scheduled email digest with PDF attachment."** Native push is **Slack**; email/PDF-style delivery would be assembled via **Agents nodes** (e.g., Google Suite / Gamma) or built on the **Reports / Raw Data API**. A simple "email me a weekly PDF" toggle was **not confirmed**.
- **No pharmacovigilance / adverse-event triage and no Medical-vs-Commercial split.** It is a marketing tool; these pharma-governance concepts do not exist in the product.

### 2.4 Pricing (verified; no public numbers)

**"Customized enterprise pricing"** with **Starter / Growth / Enterprise** tiers and **credit-based usage for Profound Agents.** No dollar figures are published.

- Source: `https://www.tryprofound.com/pricing`.

### 2.5 Sourcing caveat (method + limitation)

Profound's marketing / app-feature pages are server-rendered in chunks that the available fetch tool could not fully page into. Therefore:

- **Feature existence** is grounded in **developer docs** (REST API index + MCP capability pages — authoritative for *what exists*), **blog release titles**, and the **pricing page**.
- **Exact in-app UX** for scheduling/delivery of reports is **inferred, not fully verified**, from public sources.

### 2.6 Net assessment of Profound

Profound has the **building blocks** (Reports API, dashboards, Slack, schedulable Agents/Workflows with output nodes) to let a customer **assemble** role-targeted periodic summaries — but it does **not ship a stakeholder-differentiated pharma digest as a feature**. Role differentiation would be achieved by configuring **separate dashboards / agents / reports per team**, not by selecting a "Medical Affairs digest." Buying Profound would **not remove** our need to build the pharma role/PV/MA layer.

---

## 3. What we have today (the build baseline)

Grounded in the current codebase:

**Content sources already exist:**

- Alert rules (`LOW_SENTIMENT`, `NOT_RECOMMENDED`, `COMPETITOR_ADVANTAGE`) — `backend/app/scoring/alert_engine.py`.
- Alerts surfaced via analytics (`/alerts-summary`, `run-summary`, `persona-summary`, `llm-comparison`) — `backend/app/api/analytics.py`.
- Insights themes / trends / signals — `backend/app/api/insights.py` (+ `backend/app/insights/`).
- Rich analytics (sentiment distribution, positioning, consensus, intent, worst questions) — `backend/app/api/analytics.py`.
- So "top findings / new alerts / material drift / run activity / recommended actions" are **already queryable**.

**Scheduler exists — but for one job only:**

- APScheduler drives a single daily full-bank run (`JOB_ID = "daily_full_bank_run"`) — `backend/app/services/scheduler.py`.
- Cadence config — `backend/app/config/settings.py` (`default_schedule_cron`, `schedule_timezone`).
- No per-recipient / per-profile schedules.

**LLM summarization already available:**

- Bedrock via the copilot / insights layer — the executive-summary paragraph (BR-008a.6) needs **no new vendor**.

**Audit logging exists** — BR-008a.7 is reusable.

### Gaps (nothing exists for these)

- **No delivery layer** — no SMTP/email, no webhook, no PDF/HTML rendering. `backend/requirements.txt` has no email/PDF libs. No `digest` / `webhook` / `smtp` code in `backend/app` or `frontend/src`.
- **No role / recipient / routing model** — no `DigestProfile`, no distribution lists, no admin routing UI.
- **No RBAC currently** — auth/roles were removed from the current tree, so the 6 role profiles have **no user-role foundation** today. This is a decision point (see §5).

---

## 4. What it would take to build

### Path A — Build it natively in our app (recommended)

| Component | New work | Money | Effort |
| --- | --- | --- | --- |
| Role / digest profile model + routing (sections, filters, recipients, channel, cadence) | New DB model + CRUD API + admin UI | None (OSS) | Medium |
| Per-profile scheduling | Extend existing APScheduler to N cron jobs | None | Low |
| Content assembly (top findings, alerts, drift, actions per role) | Compose from existing analytics/insights | None | Medium |
| Executive summary paragraph | Reuse existing Bedrock LLM | Uses existing AWS spend | Low |
| HTML render | Jinja2 template | Free (OSS) | Low |
| PDF render | WeasyPrint or ReportLab | Free (OSS; WeasyPrint adds Docker system deps) | Low–Medium |
| Email delivery | `aiosmtplib` + corporate SMTP, or AWS SES / SendGrid / Postmark | Corporate SMTP = free; SES ≈ $0.10 / 1k emails | Low |
| Webhook delivery | Signed `httpx` POST | None | Low |
| Audit logging | Reuse existing audit trail | None | Low |
| RBAC (only if digests must be access-controlled) | Reintroduce roles (previously existed, removed) | None | Medium (conditional) |

**Key point:** Path A needs **no mandatory purchase, subscription, or third-party platform**. Every core dependency is open-source (Jinja2, WeasyPrint / ReportLab, aiosmtplib). The only *optional* cost is a transactional-email provider for deliverability (small, usage-based) — avoidable via corporate SMTP. The LLM summary rides existing Bedrock spend.

### Path B — Adopt Profound and integrate

- **Fit:** Profound gives best-in-class AI-visibility data + a Reports API + Slack + Agents to *assemble* summaries — but it would **not** deliver pharma-stakeholder digests out of the box. We would **still build the role / PV / MA layer** on top of their API.
- **Money:** enterprise contract, **custom pricing (not public)**, credit-based for Agents. No figure is guessed here.
- **Verdict:** justified only if we *also* want Profound's core AI-visibility/GEO product. **Purely for BR-008a, Path B is overkill and still leaves the differentiating work to us.**

### Money summary (precise)

- **To satisfy BR-008a alone: $0 in mandatory licensing.** It is a **human-engineering effort** on the existing stack.
- **Optional spend:** transactional email (AWS SES / SendGrid / Postmark) — low, usage-based; or free via corporate SMTP.
- **Profound:** only if we want their platform; enterprise pricing is not public.

---

## 5. Open decisions that change scope

- **RBAC:** reintroduce user roles for access control, or treat "roles" as **standalone distribution profiles** (no login) for the POC?
- **Delivery priority:** email first, or is **Slack / Teams** the real target channel (Profound leans Slack)? Is webhook needed for the POC?
- **PDF:** hard requirement now, or is **HTML email + link to a dashboard** enough for the POC (defers WeasyPrint Docker work)?
- **Email transport:** corporate SMTP (free) vs. SES / SendGrid (better deliverability, small cost)?

---

## Appendix — Sources consulted (2026-07-09)

- `https://www.tryprofound.com/` — product overview (Prompt Volumes, Answer Engine Insights, Agents, Agent Analytics).
- `https://www.tryprofound.com/pricing` — Starter / Growth / Enterprise, custom enterprise pricing, credit-based Agents.
- `https://www.tryprofound.com/blog` — release-title timeline (Slack integration, Custom Dashboards + public links, Reports, Personas, Agents/Workflows, HIPAA, SOC 2, funding).
- `https://docs.tryprofound.com/` — developer docs index (Agent Analytics, Bots, MCP, SSO).
- `https://docs.tryprofound.com/rest-api/introduction` — REST API groups (`Reports`, `Answers`, `Categories`, `Content Optimization`, `Agents`, `Organization`) + features (Report Generation, Raw Data Access, Organization-Scoped Data).
- `https://docs.tryprofound.com/mcp/overview` — MCP capabilities (Visibility Reports, Sentiment Analysis, Citation Reports, Raw Data Access, Agent Analytics, Build/Run Agents).
- `https://docs.tryprofound.com/mcp/capabilities/analytics-capabilities` and `.../agents-capabilities` — capability structure.

**Codebase files referenced:** `backend/app/scoring/alert_engine.py`, `backend/app/api/analytics.py`, `backend/app/api/insights.py`, `backend/app/services/scheduler.py`, `backend/app/config/settings.py`, `backend/requirements.txt`.
