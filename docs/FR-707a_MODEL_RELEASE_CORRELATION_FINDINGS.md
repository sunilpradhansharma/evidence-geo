# FR-707a — Model Release Event Correlation: Research & Build-Effort Findings

| Field | Value |
| --- | --- |
| **Requirement** | FR-707a (NEW — extends FR-707) — Model Release Event Correlation |
| **Priority** | Medium |
| **Status** | Research / Draft (no code written) |
| **Date researched** | 2026-07-09 |
| **Purpose** | (1) Decompose the requirement, (2) determine whether/how Profound (tryprofound.com) provides it, (3) assess what it would take for us to build it — money (subscription / purchase / platform) vs. human engineering effort. |

> **Honesty note:** Where a fact could not be confirmed from public sources, it is explicitly flagged as *not found* / *not confirmed* rather than assumed. No pricing figures are invented.

---

## TL;DR

- **Profound does NOT appear to ship the specific feature FR-707a describes** (a customer-facing Model Release Log with automated drift-to-release correlation, lookback window, timeline overlay, and explained/unexplained %). No such capability was found in their REST API groups, MCP capabilities, or docs.
- Profound **does** have the adjacent ingredients: it **measures citation drift / volatility over time** (verified research: *"AI Search Volatility"* — "citation drift," citations changing "up to 60% in one month"), it **rapidly adds and publicly announces new model releases** (day-0 GPT-5 / GPT-5.2 / GPT-5.6, Grok 4.5, Claude Fable, DeepSeek, Meta AI, Google AI Mode…), it lets you **segment metrics by model/platform**, and it publishes **analyst attribution** of shifts to model/platform updates. But attribution is editorial/research-driven, **not an automated in-product correlation**.
- **We already have the FR-707 foundation** (drift detection + per-response `llm_model_version`), but **nothing** for FR-707a (no release log, no correlation, no timeline, no metric; drift is also not raised as an alert yet).
- **To build FR-707a we do NOT need to buy anything mandatory.** It is a **human-engineering effort** on our existing stack (SQLAlchemy + FastAPI + recharts + Bedrock + httpx/Tavily already wired). The main risk is the **reliability of automated release-note ingestion**, not cost.

---

## 1. Requirement decomposed into capability primitives

| # | Sub-requirement | Capability primitive |
| --- | --- | --- |
| FR-707a.1 | Maintain a Model Release Log per enabled platform (version changes, retrains, capability updates) from public docs | New data model + curation |
| FR-707a.2 | Log entry fields: platform, event type (release / retrain / capability / deprecation), effective date, source URL, notes | Schema |
| FR-707a.3 | On material drift (FR-707), auto-check for a release event within a configurable lookback window (default 30d) for that platform | Correlation logic + config |
| FR-707a.4 | If within window, annotate the drift alert "Possible model update — see Model Release Log" + link | Alert annotation + linkage |
| FR-707a.5 | Dashboard timeline overlaying drift events vs model release events | Timeline visualization |
| FR-707a.6 | Log manually updatable by admins + automated ingestion of release notes from configured URLs where available | Admin CRUD + ingestion pipeline |
| FR-707a.7 | Report proportion of drift events correlated to a known release vs unexplained (operational quality metric) | Aggregation metric |

---

## 2. How Profound does it

### 2.1 What Profound has that is adjacent (verified)

- **Volatility / citation-drift measurement over time.** Verified from `https://www.tryprofound.com/blog/ai-search-volatility` — describes **"citation drift"**, a **study methodology "measuring citation consistency over time"**, and a headline finding that **AI citations can change by up to ~60% in a single month**. So Profound tracks change-over-time in-product (they need the data to run the study).
- **Model-release awareness / de-facto public changelog.** Profound adds and **announces model releases at/near day 0** — verified from many blog release titles: *"Announcing day 0 support for GPT-5,"* *"Now tracking GPT-5.2,"* *"Introducing support for GPT 5.6,"* *"Grok 4.5,"* *"Claude Fable,"* *"DeepSeek,"* *"Meta AI,"* *"Google AI Mode."* Their dashboards let you **filter/segment by model/platform** ("…in your dashboard").
- **Analyst attribution of shifts to model/platform changes.** Verified titles: *"ChatGPT's entity update: Fewer mentions, tougher competition,"* *"AI Search Shift: ChatGPT's growing alignment with Google's index,"* *"AI Search Volatility."* This is **editorial/thought-leadership**, i.e., humans attributing cause — not an automated feature.

### 2.2 What Profound does NOT have / could NOT be confirmed (precise)

- **No automated drift-to-release correlation feature.** No evidence of: a customer-facing **Model Release Log**, an **automatic lookback-window check** tying a detected drift event to a release, a **timeline overlay of drift vs releases**, or an **explained/unexplained %** metric. The REST API groups (`Categories, Answers, Reports, Content Optimization, Agents, Organization`) contain **no** events/releases/annotations group, and the MCP analytics capabilities list **no** change-event/correlation tool.
- **No pharma framing.** Correlation-for-investigation-triage (structural vs organic drift) is a pharma-monitoring concept; Profound is a horizontal marketing tool and does not frame drift this way.

### 2.3 Sourcing caveat (method + limitation)

Profound's marketing/app-feature pages render in chunks the available fetch tool cannot fully page into. Therefore feature *existence* is grounded in **developer docs** (REST API index + MCP capability pages — authoritative for what exists), **blog post/title content**, and the **pricing page**; exact in-app UX (e.g., whether release markers can be manually annotated on a chart) is **inferred, not fully verified**.

### 2.4 Net assessment of Profound

Profound gives you the **data** to see drift and the **model-support timeline** to reason about it, plus analyst content attributing shifts to model updates — but the **automated correlation loop FR-707a specifies is not a packaged Profound feature**. Buying Profound would **not** remove the need to build the Model Release Log + correlation + timeline + metric ourselves.

---

## 3. What we have today (the build baseline)

FR-707's drift foundation exists (internally labeled FR-306 / BR-004 in code; equivalent to the spec's "FR-707 drift metrics"):

- **Drift detection (lexical).** `backend/app/scoring/differ.py` — `difflib.SequenceMatcher` similarity, `MATERIAL_CHANGE_THRESHOLD = 0.85`.
- **Drift record.** `backend/app/models/response_diff.py` — `ResponseDiff(question_id, llm_name, current_response_id, previous_response_id, similarity_ratio, material_change, diff_text, created_at)`.
- **Where computed.** `backend/app/scoring/scorer.py` (`_compute_response_diff`) — compares against the most recent prior answer to the same `(question_id, llm_name)` from a different run.
- **Where surfaced.** Only the Results detail drawer ("Change vs Previous") — `frontend/src/pages/Results.tsx`. Mirrored to Snowflake (`RESPONSE_DIFFS`).
- **We already store per-response model version.** `Response.llm_model_version` (`backend/app/models/response.py`), stamped from the provider (`bedrock.py` / `openai_client.py` / `google_client.py` set `model_version = model_id` / `resp.model`). Useful raw material.

### Gaps (nothing exists for FR-707a)

- **No Model Release Log** model/table, no admin CRUD, no ingestion pipeline.
- **No correlation logic** (lookback window) and **no config** for it.
- **No drift *alert* to annotate.** Material change is stored on `ResponseDiff` but is **not** raised as an `Alert` (the alert engine only fires `LOW_SENTIMENT` / `NOT_RECOMMENDED` / `COMPETITOR_ADVANTAGE`). FR-707a.4 ("annotate the drift alert") therefore first needs a drift alert/event to annotate.
- **No timeline overlay** UI and **no explained/unexplained metric**.

---

## 4. What it would take to build

### Path A — Build it natively in our app (recommended)

| Component | New work | Money | Effort |
| --- | --- | --- | --- |
| Model Release Log model + admin CRUD API (FR-707a.1/.2/.6) | New table (platform, event_type, effective_date, source_url, notes, affected_model_version) + endpoints | None (OSS) | Low–Medium |
| Admin UI for the log | New page/form | None | Medium |
| Surface a drift event/alert to annotate (FR-707a.4 prerequisite) | Add a `DRIFT`/material-change signal (alert row or drift feed) | None | Low |
| Correlation logic + lookback config (FR-707a.3/.4) | On material drift, query log for same platform within N days (default 30, configurable); attach flag + link | None | Low |
| Timeline overlay (FR-707a.5) | Chart overlaying drift events vs release markers | None (**recharts already a dependency**) | Medium |
| Explained-vs-unexplained metric (FR-707a.7) | Aggregation query + KPI tile | None | Low |
| Automated release-note ingestion (FR-707a.6) | Fetch configured URLs + extract structured events | None (reuse httpx + Bedrock LLM extraction; Tavily already wired) | **Medium–High (reliability risk)** |

**Key point:** Path A needs **no mandatory purchase, subscription, or third-party platform**. Everything reuses the existing stack (SQLAlchemy, FastAPI, recharts, Bedrock, httpx, optional Tavily). The manual-entry + correlation + timeline + metric parts are straightforward; the **automated ingestion** is the only fuzzy/higher-effort piece.

### Path B — Adopt Profound and integrate

- **Fit:** gives volatility data + a model-support timeline + analyst content, but **not** the automated correlation feature. We would **still** build the Model Release Log + correlation + timeline + metric on top of their API.
- **Money:** enterprise contract, **custom pricing (not public)**. No figure guessed.
- **Verdict:** overkill for FR-707a; justified only if we also want Profound's core AI-visibility product.

### Money summary (precise)

- **To satisfy FR-707a alone: $0 in mandatory licensing.** It is a **human-engineering effort**.
- **No paid "model changelog API" is needed** — providers (OpenAI / Anthropic / Google / AWS Bedrock) publish release notes/model-version pages for free; the effort is fetching/curating them, not buying them.
- **Profound:** only if we want their platform; enterprise pricing is not public.

### Key risks / caveats (accuracy)

- **Automated ingestion is the hard part.** Provider release notes have no common format; robust extraction needs scraping + LLM parsing and ongoing maintenance. A **manually-curated log is reliable now**; automated ingestion is best-effort. FR-707a.6 already allows "where available," which fits this reality.
- **`llm_model_version` won't reliably flag silent retrains.** Bedrock inference-profile IDs are stable; OpenAI aliases (e.g., `gpt-4o`) resolve to dated snapshots that may or may not surface in `resp.model`. This is *why* an external Model Release Log is the right approach — correlation should key off the curated log + effective dates, not off our stored model_id changing.
- **Drift is lexical (`difflib`), a known POC simplification.** Correlation quality is bounded by drift-detection quality. FR-707a does not require fixing this, but "material drift event" accuracy depends on it.

---

## 5. Open decisions that change scope

- **Drift signal shape:** raise material changes as `Alert` rows (so FR-707a.4 "annotate the drift alert" is literal), or annotate a separate drift feed/view?
- **Ingestion scope for POC:** manual-entry log only first (reliable), and defer automated URL ingestion to a fast-follow?
- **Which platforms to seed:** all enabled targets (Claude/Bedrock, Nova, Llama, Gemini, GPT-4o, OpenEvidence) — and what "release/retrain" source URL per platform?
- **Lookback default:** confirm 30 days and whether it should be per-platform configurable.

---

## Appendix — Sources consulted (2026-07-09)

- `https://www.tryprofound.com/blog/ai-search-volatility` — "citation drift," measuring citation consistency over time, ~60% monthly change.
- `https://www.tryprofound.com/blog` — release-title timeline (day-0 model support: GPT-5/5.2/5.6, Grok 4.5, Claude Fable, DeepSeek, Meta AI, Google AI Mode; "ChatGPT's entity update"; "AI Search Shift").
- `https://docs.tryprofound.com/rest-api/introduction` — REST API groups (no events/releases/annotations group).
- `https://docs.tryprofound.com/mcp/overview` and `.../mcp/capabilities/analytics-capabilities` — analytics/report capabilities (no change-event/correlation tool).
- `https://www.tryprofound.com/pricing` — custom enterprise pricing (no public numbers).

**Codebase files referenced:** `backend/app/scoring/differ.py`, `backend/app/models/response_diff.py`, `backend/app/scoring/scorer.py`, `backend/app/scoring/alert_engine.py`, `backend/app/models/response.py`, `backend/app/providers/{bedrock,openai_client,google_client}.py`, `frontend/src/pages/Results.tsx`, `backend/app/snowflake/tables.py`.
