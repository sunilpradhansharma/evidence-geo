# Evidence Monitoring Agent: Product Requirements

> Detailed problem and requirements specification (problem and requirements only, not solution design).
> For design see [`docs/TECHNICAL_DESIGN.md`](TECHNICAL_DESIGN.md); for business framing see
> [`docs/OVERVIEW.md`](OVERVIEW.md); for setup see [`README.md`](../README.md).

## 1. Purpose

This document defines the problem the Evidence Monitoring Agent addresses and the requirements it must
meet. It states what the system must do, not how it is built. Requirements are grouped and given stable
IDs (BR, FR, NF, SE, DM, IN, AC) so each can be traced to design, code, and tests.

## 2. The problem

### 2.1 A new, unmonitored channel

Patients, prospects, and healthcare providers increasingly ask AI assistants (ChatGPT, Gemini, Claude,
Perplexity, and clinician tools such as OpenEvidence) what to take, what to prescribe, and whether a
cheaper or better option exists. For a growing share of people, the model's answer is the first and
sometimes only research they do. That answer shapes perception, expectations, and treatment decisions
before any brand, clinician, or medical team is involved.

This channel is effectively invisible to the manufacturer. Unlike a label, an ad, or a web page, a
model's answer is generated on demand, varies by how the question is phrased, differs across models, and
changes over time as models are retrained. There is no native record of what was said.

### 2.2 What can go wrong

For any monitored therapy, a model answer may:

- **Misrepresent the therapy:** state an incorrect indication, dose, mechanism, or contraindication.
- **Omit the brand:** answer a relevant question without mentioning a therapy that should be in scope.
- **Favor a competitor:** present a competitor as first line or preferred, with the brand absent or ranked below.
- **Hallucinate claims:** assert safety or efficacy facts the evidence does not support.
- **Surface an adverse event:** repeat a harm narrative that warrants pharmacovigilance review.
- **Lack credible sources:** answer without citing, or cite weak or outdated evidence, for the therapy.

### 2.3 Why current approaches fall short

- **Web and SEO analytics** measure pages and rankings, not what a model generates in a conversation.
- **Social listening** measures what people post, not what models tell them privately.
- **MLR and medical review** govern the manufacturer's own content, not third-party model output.
- **Manual spot-checks** are occasional, unrepeatable, unscored, and impossible to scale across models,
  personas, questions, and time.

### 2.4 Why now

Model answers are becoming a primary discovery surface (Generative Engine Optimization). Models change
silently, so a favorable answer today can degrade tomorrow without warning. Without continuous,
structured measurement, the manufacturer cannot detect drift, quantify exposure, or respond.

## 3. Stakeholders and audiences

### 3.1 Internal stakeholders

- **Medical Affairs:** owns medical accuracy and the approved question bank; needs to detect
  misstatements and adverse-event signals.
- **Brand and Commercial:** owns competitive positioning and share of narrative; needs to see where
  models favor competitors.
- **Pharmacovigilance and Safety:** must review any adverse-event content the system surfaces.
- **Market Access:** cares about cost, coverage, and access framing in model answers.
- **Brand teams (multiple):** individual brand and franchise teams (for example the high-growth Obesity
  team for GLP-1s and weight management) that want a repeatable capability rather than one-off vendor
  studies.

### 3.2 The three personas (how questions are asked)

Real people ask differently depending on who they are. The system models three audiences:

- **Prospect:** a person considering or comparing treatment options, often price- and outcome-driven.
- **Patient:** a person already on or starting a therapy, asking about use, side effects, and adherence.
- **Provider:** a clinician asking in clinical terms about evidence, lines of therapy, and safety.

Each persona has its own phrasing and appropriate framing, so the same topic must be asked through each
lens.

### 3.3 Primary users of the product

The three personas above describe how questions are asked; they are distinct from who uses the product.
The primary interactive user is a brand or marketing manager who consumes insights and decides on
actions, while Medical Affairs and Pharmacovigilance act as reviewers and approvers. The product's
language, naming, and layout shall be designed for that non-technical marketing user first.

## 4. Cost of inaction

Left unmeasured, this channel creates compounding risk:

- **Commercial:** answers that favor competitors or omit the brand can shift prescribing and erode
  demand, invisibly.
- **Reputation and trust:** inaccurate or negative answers reach patients and providers at the point of
  decision.
- **Compliance and patient safety:** hallucinated safety or dosing claims and unreviewed adverse-event
  narratives create regulatory and safety exposure.
- **Strategy blind spot:** without a baseline, medical and commercial teams cannot prioritize where to
  publish evidence or correct the record.
- **Latency:** by the time impact appears in prescribing or sentiment data, the cause (a changed model
  answer) is already weeks old and untraceable.

## 5. Goals and non-goals

### 5.1 Goals

- Produce a repeatable, auditable record of what each monitored model says about each therapy, per
  persona, over time.
- Quantify brand sentiment and competitive positioning per answer, and in aggregate across models.
- Detect material change between runs and raise alerts on concerning findings.
- Surface where credible evidence for a therapy is missing in grounded answers.
- Keep the whole process governed (approved questions only), private (no PII), and auditable.

### 5.2 Non-goals

- Not a clinical decision tool and not medical advice.
- Not an attempt to influence or manipulate model outputs.
- Not a replacement for MLR, pharmacovigilance, or regulatory processes; it feeds them.
- Not a general web or social monitor; it measures model answers (social listening is a separate,
  complementary surface).

## 6. Definitions

- **Target / model:** an LLM endpoint the system queries (for example Claude, Gemini, GPT-4o).
- **Persona:** the audience lens a question is asked through (Prospect, Patient, Provider).
- **Run:** one execution that sends a set of approved questions to all enabled targets across personas.
- **Response:** one model's verbatim answer to one question, stored immutably.
- **Score:** the sentiment and competitive-position assessment of a response.
- **Consensus:** the cross-model agreement, divergence, and synthesized answer for a question.
- **Drift:** a material change in a model's answer to the same question between runs.
- **Grounding / provenance:** the real source URLs and claims a search-enabled model cited.
- **Adverse event (AE):** content describing a harm from a named therapy, routed for safety review.

## 7. Requirements

Requirement levels follow RFC 2119: "shall" is mandatory, "should" is recommended. IDs are stable for
traceability to design, code, and tests.

### 7.1 Business requirements (BR)

- **BR-001 Visibility:** Maintain an ongoing, factual record of what each monitored model says about
  each therapy, per persona.
- **BR-002 Competitive intelligence:** Establish a baseline of how therapies are positioned versus named
  competitors across models.
- **BR-003 Change detection:** Detect and investigate material shifts in model answers over time.
- **BR-004 Risk and safety:** Flag potentially inaccurate, off-indication, or adverse-event content for
  Medical Affairs and Pharmacovigilance review.
- **BR-005 Evidence gaps (GEO):** Identify where credible sources for a therapy are missing in grounded
  answers.
- **BR-006 Governance and auditability:** Operate on approved questions only, with no PII and a
  defensible audit trail.
- **BR-007 Clinical accuracy (should):** Identify responses that contain clinically inaccurate
  information, such as incorrect dosing, outdated contraindications, or unapproved indications, and flag
  them for Medical Affairs review.
- **BR-008 Intelligence digest (could):** Generate a periodic digest summarizing the most significant
  findings across models and personas for Commercial and Medical Affairs stakeholders.
- **BR-009 Repeatable enterprise capability:** Provide social listening and AI-answer monitoring as an
  ongoing, repeatable enterprise capability across brand teams (for example Obesity), replacing one-off
  vendor analyses with a sustainable in-house service.
- **BR-010 Marketer-facing usability:** The primary product experience shall be usable by a
  non-technical marketing audience, leading with plain-English insight and recommended action.

### 7.2 Functional requirements (FR)

#### 7.2.1 Question repository (FR-1xx)

- **FR-101** The system shall maintain a repository of questions, each tagged with persona, therapeutic
  area, brand focus, and domain (Efficacy, Safety, Access, Comparative, or General).
- **FR-102** Each question shall carry an approval status (Pending, Approved, Rejected) and an approver
  identity; only Approved and active questions may be used in a run.
- **FR-103** Editing a question shall create a new version; prior versions shall be retained.
- **FR-104** The system shall activate or deactivate questions without deleting them.
- **FR-105** The system shall support bulk import of questions (CSV) with validation.
- **FR-106** The system shall support filtering and search by persona, therapeutic area, brand, domain,
  and approval status.
- **FR-107** The system shall report question coverage by persona and therapeutic area and flag gaps.
- **FR-108** The system shall support soft delete with a recorded reason; hard deletion shall not be
  permitted (see DM-003).
- **FR-109** The repository shall support at least 100 active questions at launch and scale to 1,000 or
  more without architectural change.

#### 7.2.2 Response capture and run engine (FR-2xx)

- **FR-201** The system shall dispatch each approved, active question to every enabled target.
- **FR-202** The system shall apply a persona-appropriate system prompt per question, sourced from
  Medical-Affairs-reviewable configuration.
- **FR-203** The system shall support multiple model targets, including an automated clinical-reasoning
  target (EvidenceMD) for Provider questions. OpenEvidence remains supported as an optional manual
  capture (see IN-003).
- **FR-204** All target responses for a question shall be committed before the next question is started.
- **FR-205** The system shall query targets concurrently, subject to per-target rate limits.
- **FR-206** The system shall retry transient failures with backoff and mark exhausted attempts as
  Failed.
- **FR-207** The system shall classify each response as Success, Failed, Truncated, or Blocked (safety).
- **FR-208** On truncation, the system shall retry once with a higher token limit.
- **FR-209** The system shall provide a dry-run mode that validates connectivity without storing
  responses.
- **FR-210** The system shall support ad-hoc runs filtered by persona, therapeutic area, brand, or
  domain.
- **FR-211** The system shall enforce a per-run token and cost budget and pause a run that exceeds it.
- **FR-212** The system may submit each question multiple times per model per run (for example three) to
  assess non-determinism and improve scoring confidence via majority scoring.

#### 7.2.3 Response repository (FR-3xx)

- **FR-301** Every response shall be stored verbatim and unedited.
- **FR-302** Each response shall capture metadata: model name and version, persona, question,
  therapeutic area, brand, domain, timestamps, prompt and response token counts, finish reason, and
  status.
- **FR-303** Responses shall be queryable and filterable by model, persona, therapeutic area, brand,
  domain, date range, sentiment range, and alert status.
- **FR-304** Responses shall be immutable (append-only); derived score fields shall be projected from the
  latest scoring record, never by mutating the response.
- **FR-305** The system shall export responses and scores to CSV and JSON.
- **FR-306** The system shall compute and store a difference of each answer versus the previous run for
  the same question and model.
- **FR-307** The system shall provide a single-response detail view with full text, latest score,
  rationale, cited sources, and the prior-run difference.

#### 7.2.4 Scoring, consensus, and alerting (FR-4xx)

- **FR-401** The system shall score each successful response for brand sentiment on a -1.0 to +1.0 scale.
- **FR-402** The system shall classify competitive position into one of a fixed set: First-line
  recommended, Among options, Second-line, Not recommended, or Not mentioned.
- **FR-403** Scoring shall extract brand mentions and up to five key claims, with a rationale.
- **FR-404** Scoring output shall conform to a strict schema and be validated; parse failures shall be
  logged and retried.
- **FR-405** Alert rules shall flag a response when sentiment is below a threshold, position is Not
  recommended, a competitor's sentiment exceeds the focus brand's by a configurable margin, or an
  adverse-event signal is present.
- **FR-406** Every successful response shall be scored within a bounded time after a run, with a sweeper
  for stragglers.
- **FR-407** The system shall re-score historical responses under a new prompt or score version,
  preserving prior versions.
- **FR-408** The system shall record human score overrides as a separate, human-authored version with
  rationale, without altering the AI score.
- **FR-409** The system shall compute cross-model consensus per question: agreement level, divergence
  points, confidence, and a synthesized final answer.
- **FR-410** The system shall compute an aggregate overall sentiment and overall competitive position
  across models for each question.

#### 7.2.5 Scheduling and runs (FR-5xx)

- **FR-501** The system shall support unattended scheduled runs.
- **FR-502** The schedule shall be configurable (cron), time-zone aware, and able to be enabled or
  disabled.
- **FR-503** Each run shall record trigger (Scheduled or Ad-hoc), status, start and end times, per-status
  counts, total tokens, and estimated cost.
- **FR-504** A run shall be resumable: re-running shall skip question and target pairs already captured,
  with no duplicates.
- **FR-505** The system shall support a run-completion notification (for example a webhook).
- **FR-506** The system shall support cancelling an in-flight run.
- **FR-507** Provider-persona runs shall query an automated clinical-reasoning target (EvidenceMD)
  alongside the public platforms and compute consensus without a manual pause. Optional manual
  OpenEvidence capture remains available to augment Provider consensus after a run.

#### 7.2.6 Dashboard and reporting (FR-6xx)

- **FR-601** The dashboard shall present sentiment distribution by model and therapeutic area.
- **FR-602** The dashboard shall present a competitive-positioning breakdown by model.
- **FR-603** The dashboard shall present response volume over time and an alert feed with key metrics.
- **FR-604** The dashboard shall provide global filters for persona, therapeutic area, model, and date
  range.
- **FR-605** The dashboard shall allow drill-in to a full response with rationale, cited sources, and the
  prior-run difference.
- **FR-606** The dashboard shall provide side-by-side comparison of every model's answer to a selected
  question.
- **FR-607** The dashboard shall surface recurring themes and signals across many responses.
- **FR-608** The dashboard should provide a natural-language query surface over the collected data.
- **FR-609** The dashboard shall be viewable without installing software, delivered as a hosted internal
  URL and as a self-contained static HTML export.
- **FR-610** The product shall default to an insights-first landing view (Insights and Trends) rather
  than the operational pipeline steps; help or "How to Use" shall be de-emphasized (end of navigation or
  a help icon).
- **FR-611** The insights view shall lead with recommended next steps, placed above the metric tiles and
  charts, so the primary user sees the action before the data. The recommended action shall be visually
  primary, not subordinate to diagnostic labels.
- **FR-612** The insights view shall present a one-sentence plain-English headline summarizing the key
  takeaway across runs (for example the brand's absence rate and the topic of greatest model
  disagreement).
- **FR-613** Each active-alert type shall carry linked, plain-language guidance on the recommended action
  for the marketer, not just a count.
- **FR-614** The intent distribution shall include a brief "why this matters" explanation of what each
  intent type means for brand strategy.
- **FR-615** The competitive-positioning legend shall be prominent, with labels understandable at a
  glance without reading fine print.
- **FR-616** Brand sentiment shall be shown with a plain-language, color-coded label (Negative, Mixed,
  Positive) alongside any numeric score.
- **FR-617** The natural-language query feature ("Ask a Question") shall be prominent and shall offer
  several suggested questions the user can ask of the linked data (strengthens FR-608).
- **FR-618** The product shall carry a marketer-native name in place of "Evidence Monitoring"; the final
  name is an open decision (see section 10).
- **FR-619** Navigation and section labels shall be purpose-driven plain English, and "AI Platform" shall
  be used consistently in place of "LLM". The v2 labels shall be preserved: "Discover Patient
  Questions", "Approved Question Bank", "Run Analysis", "AI Response Review", and "Insights and
  Trends".
- **FR-620** Page titles shall match their navigation labels (for example the AI Response Review page
  shall not be titled "Results"), and filters shall reuse the wording of related column headers.
- **FR-621** Positioning labels shall read in plain language for marketers (for example "Absent" shall
  read "Not appearing in AI answers", and "Backup" shall read "Mentioned, but not recommended first").
- **FR-622** Infrastructure and cost metrics such as token counts shall be hidden from the default view
  and available only under an expandable "Technical Details" section.

Plain-language labeling map (illustrative, not exhaustive):

| Current label | Where it appears | Plain-language replacement |
| --- | --- | --- |
| Token count (for example "601,910 tok") | Run history | Move to "Technical Details" or remove from default view |
| PAUSED_BUDGET | Run history status | Paused: budget limit reached |
| ADHOC | Run history type | On-demand run |
| LLM (filter) | AI Response Review | AI Platform |
| Cortex Agent (button) | Dashboard | Ask a Question |
| Full (consensus) | Results table | All platforms agree |
| Snapshot View (toggle) | Dashboard | Point-in-time view (with tooltip) |
| Contains Refs % | Insights and Trends | Includes Citations % |
| Absent (positioning) | Insights | Not appearing in AI answers |
| Backup (positioning) | Insights | Mentioned, but not recommended first |

#### 7.2.7 Question discovery (FR-7xx)

- **FR-701** The system shall mine real, verbatim user questions about monitored therapies from public
  health communities (for example Reddit, Quora, drugs.com, HealthUnlocked, and patient.info). The list
  of sources is configurable and not exhaustive.
- **FR-702** Harvested questions shall be PII-scrubbed, deduplicated, and classified by persona,
  therapeutic area, brand, and domain.
- **FR-703** Posts describing adverse events shall be quarantined and excluded from promotion absent
  safety sign-off.
- **FR-704** Harvested questions shall enter a staging area and require human promotion into the approved
  bank (two-gate governance).
- **FR-705** Discovery shall be scopeable by persona and therapeutic area.

#### 7.2.8 Social listening (complementary) (FR-8xx)

- **FR-801** The system shall capture public social posts and their comments about monitored therapies
  across configured channels (for example Reddit, Facebook, TikTok, X, and YouTube). The channel list is
  configurable and not exhaustive.
- **FR-802** Post and comment text shall be PII-scrubbed, screened for prompt injection, and checked for
  adverse events (fail-closed).
- **FR-803** Posts and comments shall be scored for sentiment, with comment sentiment tracked separately
  from post sentiment.
- **FR-804** Non-English text shall be translated to English with the original preserved and viewable.
- **FR-805** The social dashboard shall show share of voice, sentiment, volume over time, top themes,
  adverse-event signals, and per-channel engagement, presented as captured-sample metrics compared
  within a channel only.
- **FR-806** Social listening shall support configurable topic and therapeutic-area scopes (for example
  GLP-1s, weight management, and broader obesity conversations for the Obesity team), consistent with
  the content-agnostic principle in SE-007.
- **FR-807** Social listening shall run on a repeatable schedule so it operates as an ongoing capability
  rather than a one-off study, reusing the scheduling and run controls in FR-5xx.

### 7.3 Non-functional requirements (NF)

- **NF-001** A full scheduled run shall complete within a bounded window (target: a few hours).
- **NF-002** Scoring of a run shall complete within 30 minutes of run completion.
- **NF-003** Targets shall be queried concurrently, subject to per-target rate limits.
- **NF-004** A single target failure shall not abort the run; other targets shall continue.
- **NF-005** Runs and stored data shall survive process restarts (durable store, resumable runs).
- **NF-006** The system shall emit structured, machine-readable logs.
- **NF-007** The system shall estimate per-run cost from configured pricing.
- **NF-008** The system shall expose a health check reporting per-target connectivity.
- **NF-009** Adding a new target shall require only configuration plus a credential, with no core code
  change.
- **NF-010** Setup and add-a-target steps shall be documented.
- **NF-011** The same codebase shall run in both a local and a deployed environment.
- **NF-012** Core modules shall have automated tests.

### 7.4 Security and privacy (SE)

- **SE-001** Questions and stored data shall contain no PII.
- **SE-002** CSV import shall lint for PII patterns and flag suspect rows before approval.
- **SE-003** Responses and the audit log shall be append-only.
- **SE-004** Data shall remain within the organization's controlled environment.
- **SE-005** Use of each model shall comply with its terms of service (launch gate).
- **SE-006** Credentials and secrets shall be redacted from logs.
- **SE-007** Brand, competitor, and therapeutic-area content shall live in configuration, not code.
- **SE-008** Third-party text (discovery and social) shall be screened for prompt injection before any
  LLM processing.

### 7.5 Data management (DM)

- **DM-001** Collected data shall be retained for a defined period (target: 24 months).
- **DM-002** Response text shall be stored full and unmodified.
- **DM-003** Questions shall support soft delete with a reason; hard deletion shall not be permitted.
- **DM-004** Every external model call shall be recorded in an audit log with role tagging (orchestrator
  versus target), status, and token counts.
- **DM-005** Scoring records shall be versioned; prior versions shall be retained.

### 7.6 Integration (IN)

- **IN-001** The system shall integrate with multiple LLM providers behind a single provider contract.
- **IN-002** The system shall represent provider safety blocks as a Blocked status.
- **IN-003** The system shall provide clinician-grade Provider answers via an automated clinical-reasoning
  API (EvidenceMD), and shall retain optional manual capture for OpenEvidence (which has no public API).
- **IN-004** Credentials shall be supplied via environment or secret configuration, not hard-coded.
- **IN-005** At startup, the system shall validate connectivity for each enabled target before a run
  begins.
- **IN-006** The system may mirror collected data into an analytics warehouse for rollups and
  natural-language query (optional, disabled by default).

## 8. Acceptance criteria (AC)

These criteria define when the requirements are considered met.

- **AC-01 Unattended operation:** scheduled runs complete over a multi-day period with zero manual
  intervention.
- **AC-02 Coverage:** at least 30 questions per persona across at least two therapeutic areas.
- **AC-03 Capture rate:** at least 95% successful capture per enabled target.
- **AC-04 Scored and auditable:** every successful response has a score and is queryable, with every
  external call recorded in the audit log.
- **AC-05 Change and alerting:** material changes between runs and concerning findings surface as alerts.
- **AC-06 Extensibility:** a new model can be enabled by configuration alone and passes its startup
  health check.
- **AC-07 Governance:** only Approved questions are used in runs, and no PII is present in stored data.
- **AC-08 Marketer usability:** a non-technical marketing user can, without training, land on insights,
  identify the top recommended action, and ask a natural-language question of the data.

## 9. Scope and constraints

In scope: the requirements above, exercised on synthetic but realistic data that uses real brand names
and contains no PII.

Out of scope (this phase): MLR workflow integration, production alert channels (webhook only), end-user
authentication and RBAC, multi-tenancy, clinical-accuracy scoring against a managed reference corpus,
integration with existing data platforms (Veeva, Salesforce, data lake), and any mobile or native
application.

## 10. Assumptions and open questions

- **Assumption:** monitored brands, competitors, therapeutic areas, and prompts are supplied via
  configuration, not embedded in code.
- **Assumption:** credentials for each enabled model are available, and each model's terms of service
  permit this use.
- **Open:** the exact set of monitored models, therapeutic areas, and run frequency are configurable and
  to be confirmed per deployment.
- **Open:** retention period, alert thresholds, and the competitive-position taxonomy are configurable
  defaults subject to Medical Affairs and Commercial sign-off.
- **Open:** Provider clinician-grade coverage is provided by EvidenceMD (automated API); OpenEvidence is retained as optional manual capture (no public API).
- **Open:** the final marketer-facing product name is pending brand sign-off. Shortlisted candidates
  include AI Brand Intelligence, Brand Signal Center, AI Brand Briefing, and "What AI Is Saying"; the
  current top recommendation is AI Brand Intelligence.
- **Open:** the first enterprise social-listening use case (Obesity: GLP-1s and weight management across
  Facebook, Reddit, and TikTok) is to be confirmed, along with which brand teams onboard next.
