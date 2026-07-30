# Evidence Monitoring Agent — POC
## Implementation Plan v2 (Provider-Agnostic · Phase-Driven · AWS-Deployed)
**Prepared for:** Eshwari · **Stack:** Python · FastAPI · React · AWS Bedrock (POC models) · AWS (deployment)

**What changed from v1:**
1. The LLM integration layer is now fully **provider-agnostic** — Bedrock, OpenRouter, OpenAI, Google, Anthropic direct, or any future provider plug in through one contract. Bedrock supplies all models for this POC, but that is a *configuration fact*, not an architectural one.
2. The plan is restructured around **POC phases with exit criteria** mapped to the SRS acceptance criteria — no calendar dates.
3. A full **AWS production-POC deployment architecture** is added (ECS Fargate, RDS Postgres, EventBridge Scheduler, Secrets Manager, S3/CloudFront, CloudWatch).

---

# PART 1 — Problem Statement (Plain-Language, for Non-Technical Stakeholders)

## The problem in one paragraph

Millions of patients, caregivers, and clinicians now ask AI chatbots questions like *"What's the best treatment for my symptoms?"*, *"What should I prescribe my patient?"*, and *"Is there a better or cheaper option than the drug I'm on?"* These AI systems have become a new, invisible channel where AbbVie's therapies are described, recommended, ranked against competitors — or omitted entirely. Today AbbVie has **no visibility** into this channel. If an LLM tells patients a competitor's drug is first-line, gives incorrect dosing for an AbbVie therapy, or frames it negatively, no one at AbbVie would know. That is simultaneously a commercial blind spot, a competitive-intelligence gap, and a medical-accuracy risk.

## Why we are solving it

1. **Visibility (BR-001/002):** a factual, ongoing record of what each monitored LLM says about AbbVie therapies, asked the way real people ask — through the three personas on your slides: **Prospect**, **Provider**, **Patient**.
2. **Change detection (BR-003/004):** LLMs are retrained and updated silently. A favorable answer today can change tomorrow. Daily longitudinal capture lets Medical Affairs detect and investigate material shifts.
3. **Competitive intelligence (BR-005/006):** a baseline of how AbbVie therapies are positioned vs. named competitors across all monitored LLMs, scored for sentiment and treatment-line positioning.
4. **Risk & governance (BR-007/009–011):** flag potentially inaccurate or damaging responses for Medical Affairs review, with Medical-Affairs-approved questions only, no PII, and an immutable audit trail that can withstand compliance review.

## What is being built

| Piece | Plain-language description | Slide mapping |
|---|---|---|
| **Question Repository** | A curated, Medical-Affairs-approved, versioned library of realistic questions tagged by persona, therapeutic area, brand, and domain. Nothing is ever deleted. | Box 1: "What questions should we evaluate?" |
| **LLM Response Agent** | The automation engine: every day it submits every active question to every monitored LLM and stores each answer verbatim, with retries, rate limiting, and full logging. Claude orchestrates dispatch, validation, and error handling. | Box 2 — the **POC Focus**; the Question Repo → Agent → Response Repo diagram |
| **Response Repository** | A structured, queryable, immutable database of every answer with full metadata (LLM, model version, timestamps, token counts, status). | Box 2 output |
| **Sentiment & Competitive Scoring** | Claude reads each stored response and produces a structured score: brand sentiment (−1.0 to +1.0), competitive position (first-line → not recommended → not mentioned), brand mentions, key claims, and a rationale. Alert rules flag concerning responses. | Box 3: "What is the sentiment and impact?" (prototyped) |
| **Dashboard** | A web application for Medical Affairs and Commercial: sentiment distribution by LLM and therapy, positioning breakdown, alert feed, volume over time — filterable, with click-through to full responses and side-by-side LLM comparison. | The POC readout deliverable |

## How it works end to end

1. Medical Affairs curates and approves questions (CSV import supported); only APPROVED + active questions ever leave the building.
2. On schedule, a **run** begins: the agent submits each question to every configured LLM with a persona-appropriate system prompt, concurrently, within rate limits, retrying transient failures.
3. Every response is stored full and unmodified, append-only, with a complete audit log of every external call.
4. A scoring pass populates sentiment, positioning, claims, and rationale; alert rules fire on negative findings.
5. The dashboard surfaces it all; results are exportable to CSV/JSON.

## What success looks like

An unattended run of 100 questions across 3–4 LLMs completes with no manual intervention and ≥95% capture; everything is stored, queryable, and auditable; the dashboard shows baseline sentiment and competitive positioning that Medical Affairs and Commercial confirm is **actionable**. Formal acceptance (AC-01–03) is measured during a 7-day continuous deployed run.

## Explicitly out of scope

MLR workflow integration, production alerting channels (webhook prototyped only), clinical-accuracy scoring against a reference library, Veeva/Salesforce/data-lake integration, authentication/RBAC/multi-tenancy, mobile. Open Evidence is conditional on confirmed API access.

---

# PART 2 — Design Feedback & Key Decisions

### 1. Provider-agnostic by contract, Bedrock by configuration ✅ (your directive #1)
The SRS itself hints at this (NF-010: "adding a new LLM target must require only a config change and a new adapter module"). The design below goes one level deeper than v1: instead of one adapter per *model*, there is one **ProviderClient** per *API provider* (Bedrock, OpenRouter, OpenAI, Google, Anthropic), and each **target** is pure YAML: `provider + model_id + params + rate limits + credential reference`. Result: all POC models run on Bedrock today; pointing the same logical target at OpenRouter or Google tomorrow is a two-line YAML edit and zero code. This also resolves the v1 conflict between the SRS's GPT-4o/Gemini requirements and your Bedrock constraint — the GPT-4o and Gemini target definitions ship in the config *disabled*, ready to enable the day credentials exist (via OpenRouter, both are even reachable with a single key).

### 2. Postgres from day one, because production-POC means deployed (your directive #3)
v1 proposed SQLite. Since this POC deploys to AWS and must survive a 7-day continuous acceptance run, the system targets **PostgreSQL (RDS)** as the primary store, with SQLite remaining available for local development through the same `DATABASE_URL` — SQLAlchemy makes this free. Append-only enforcement moves from "application discipline" to **database-level protection** (no UPDATE/DELETE grants on `responses` and `audit_log` for the app role) — a materially stronger SE-003/FR-304 story.

### 3. Scheduling moves out of the process and into AWS EventBridge
v1 used in-process APScheduler. For a deployed POC that must run unattended for 7 days (AC-01), in-process scheduling is fragile: if the API container restarts at 01:59, the run is silently missed. The deployed design uses **EventBridge Scheduler → launches a dedicated ECS Fargate run task** (cron expression per FR-502, fully decoupled from the API service). APScheduler remains as a local-dev fallback behind the same `RunLauncher` interface. Ad-hoc runs (FR-506) trigger the same task via API.

### 4. Orchestrator: deterministic pipeline + Claude judgment where it counts
Unchanged recommendation from v1, restated because it matters for governance: dispatch, retry, rate limiting, and resume are deterministic, testable Python (what SE/NF audits require); Claude-with-thinking (FR-202) is invoked at the two points where judgment is genuinely needed — **response validation** (truncated? refusal? safety-blocked?) and **scoring** — and every Claude call is logged with `role=ORCHESTRATOR` vs `role=TARGET` (IN-302).

### 5. Other resolved items (carried from v1)
- **Open Evidence (IN-401/402):** stub provider behind the same contract; enabling it is config-only. Deferred until access is confirmed.
- **Content-agnostic code (SE-007):** brand, competitor, and TA names live exclusively in the question data and `config/brands.yaml`.
- **Immutability with mutable-looking fields (FR-302 vs FR-304):** `sentiment_score` / `competitive_position` / `alert_triggered` are served by joining the latest versioned scoring record — the response row itself is never updated.
- **Dashboard delivery (FR-603):** React app on a shared internal URL **plus** a one-click self-contained static HTML snapshot export.
- **Gemini safety blocks (IN-204):** `BLOCKED` status exists in the schema regardless of which providers are live, so the data model is future-proof.

---

# PART 3 — System Architecture

## 3.1 Logical architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              FastAPI backend                                 │
│                                                                              │
│  Medical Affairs CSV ──▶ Question Repository (versioned, approval workflow)  │
│                                   │ active + APPROVED                        │
│                                   ▼                                          │
│   EventBridge / API ──▶ ┌───────────────────────────────┐                   │
│        trigger          │   Orchestrator (Run Engine)    │                   │
│                         │ dispatch · concurrency · retry │                   │
│                         │ rate-limit · resume · budget   │                   │
│                         └──────────────┬────────────────┘                   │
│                                        ▼                                     │
│                         ┌───────────────────────────────┐                   │
│                         │      TARGET REGISTRY (YAML)    │                   │
│                         │  target = provider + model_id  │                   │
│                         │        + params + limits       │                   │
│                         └───┬───────┬───────┬───────┬───┘                   │
│                             ▼       ▼       ▼       ▼                        │
│                      ┌──────────┐ ┌──────┐ ┌──────┐ ┌──────────┐            │
│        PROVIDER      │ Bedrock  │ │OpenAI│ │Google│ │OpenRouter│  + stub:   │
│        CLIENTS       │(Converse)│ │      │ │      │ │          │  OpenEvid. │
│                      └────┬─────┘ └──┬───┘ └──┬───┘ └────┬─────┘            │
│                           └──────────┴───┬───┴───────────┘                   │
│            ── POC: every target points at Bedrock ──                         │
│                                          ▼                                   │
│                          Response Repository (append-only, Postgres)         │
│                                          │            ╲                      │
│                                          ▼             ╲ every external call │
│                          Scoring Engine (Claude,        ▼                    │
│                          structured output, versioned)  Audit Log            │
│                                          │              (append-only,        │
│                                          ▼               DB-enforced +       │
│                                   Alert Engine           CloudWatch)         │
│                                          │                                   │
│            REST API: questions · runs · responses · scores · alerts ·        │
│                      analytics · export · health · static-snapshot           │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   ▼
                 React dashboard (Vite + Recharts) — S3 + CloudFront
```

## 3.2 The provider-agnostic LLM layer (core design)

**Three concepts, strictly separated:**

1. **ProviderClient** (code, one per API provider) — knows *how to talk to an API*: auth, request/response shape, error taxonomy, token accounting.
   ```python
   class ProviderClient(ABC):
       async def chat(self, model_id: str, system: str, user: str,
                      params: ModelParams) -> ProviderResult: ...
       async def health_check(self, model_id: str) -> HealthStatus: ...

   @dataclass
   class ProviderResult:
       text: str
       finish_reason: Literal["stop", "length", "blocked", "error"]
       prompt_tokens: int; completion_tokens: int
       model_version: str          # provider-reported, for FR-302 llm_model_version
       raw_status: int | None      # HTTP status for audit (FR-208)
       block_reason: str | None    # safety-block detail (IN-204)
   ```
   Implementations: `BedrockClient` (boto3 `converse()` — one client covers Claude, Nova, Llama, Mistral, Cohere, DeepSeek…), `OpenAIClient`, `GoogleClient`, `OpenRouterClient` (OpenAI-compatible — one key reaches GPT-4o *and* Gemini *and* hundreds more), `AnthropicClient`, `OpenEvidenceClient` (stub). Every client normalizes provider-specific failures into one error taxonomy (`RateLimited`, `Transient`, `SafetyBlocked`, `AuthError`, `Fatal`) so the orchestrator's retry logic is provider-blind.

2. **Target** (pure configuration) — *what we monitor*. `config/targets.yaml`:
   ```yaml
   targets:
     - name: claude            # logical name; survives provider swaps
       role: TARGET
       provider: bedrock
       model_id: ${TARGET_CLAUDE_MODEL_ID}        # resolved from .env / Secrets Manager
       params: {max_tokens: 1024, thinking: adaptive}    # IN-303: no temperature with thinking
       rate_limit: {rpm: 20, tpm: 80000}
       enabled: true
     - name: nova-pro
       provider: bedrock
       model_id: ${TARGET_NOVA_MODEL_ID}
       params: {max_tokens: 1024, temperature: 0.3}
       rate_limit: {rpm: 30, tpm: 100000}
       enabled: true
     - name: llama
       provider: bedrock
       model_id: ${TARGET_LLAMA_MODEL_ID}
       params: {max_tokens: 1024, temperature: 0.3}
       enabled: true
     # Ready, disabled — flipping these on requires only a credential + enabled:true
     - name: gpt-4o
       provider: openai          # or provider: openrouter
       model_id: ${TARGET_GPT4O_MODEL_ID}
       enabled: false
     - name: gemini
       provider: google          # or provider: openrouter
       model_id: ${TARGET_GEMINI_MODEL_ID}
       enabled: false

   orchestrator: {provider: bedrock, model_id: ${ORCHESTRATOR_MODEL_ID}, thinking: adaptive}
   scoring:      {provider: bedrock, model_id: ${SCORING_MODEL_ID},      thinking: adaptive}
   ```

3. **Credential resolution** — each provider declares which secrets it needs; locally they come from `.env`, in AWS from **Secrets Manager / IAM role** (Bedrock needs no static key at all when running on AWS — the ECS task role grants `bedrock:InvokeModel`). Startup validation (IN-502) iterates every *enabled* target, resolves credentials, and fires a 1-token health check; any failure aborts before a single question is sent.

**Why not just use LiteLLM?** Worth naming at the readout: LiteLLM gives this abstraction off the shelf, and the design is compatible with swapping it in under `ProviderClient` later. For the POC, thin custom clients are preferred because the audit requirements (SE-003: log every call with status/tokens; SE-006: credential redaction; IN-302: role tagging) demand full control of the request path, and the surface area is small.

## 3.3 Data model (PostgreSQL on RDS; SQLite locally)

- **questions** — `question_id`, `question_text`, `persona`, `therapeutic_area`, `brand_focus`, `domain`, `active`, `approval_status` (PENDING/APPROVED/REJECTED), `approver_name`, `version`, `superseded_by`, timestamps, `deleted_at` + `delete_reason` (soft delete, DM-003). Edits create new version rows (FR-103).
- **runs** — `run_id`, `trigger` (SCHEDULED/ADHOC), `status` (RUNNING/COMPLETED/FAILED/PAUSED_BUDGET), `started_at`, `ended_at`, per-status counts, `total_tokens`, `estimated_cost_usd`, `config_snapshot` (JSON — which targets/params were live, for reproducibility).
- **responses** *(append-only — DB role has INSERT/SELECT only)* — all FR-302 fields: `response_id` UUID, `run_id`, `timestamp_utc`, `llm_name`, `llm_model_version`, `persona`, `question_id`, `question_text` (denormalised), `therapeutic_area`, `brand_focus`, `domain`, `response_text` (full, unedited — DM-002), `prompt_tokens`, `response_tokens`, `finish_reason`, `status` (SUCCESS/FAILED/TRUNCATED/BLOCKED), `created_at`. Derived fields (`sentiment_score`, `competitive_position`, `alert_triggered`) are projected from the latest scoring record at the API layer (FR-302 + FR-304 simultaneously).
- **scoring_records** *(versioned)* — `score_id`, `response_id`, `score_version`, `prompt_version`, `sentiment_score`, `competitive_position`, `brand_mentions` JSONB, `key_claims` JSONB (≤5), `scoring_rationale`, `scored_by` (model id, or HUMAN for FR-408 overrides with rationale), `created_at`.
- **alerts** — `alert_id`, `score_id`, `rule_triggered` (LOW_SENTIMENT / NOT_RECOMMENDED / COMPETITOR_ADVANTAGE), `created_at`, `acknowledged`.
- **audit_log** *(append-only, DB-enforced)* — `timestamp`, `role` (ORCHESTRATOR/TARGET), `event`, `question_id`, `llm_target`, `http_status`, `tokens`, `context` JSONB (credential-redacted, SE-006). Mirrored to CloudWatch Logs for tamper-evidence.
- **response_diffs** — `(question_id, llm_name)` → unified diff + similarity ratio vs. previous run (FR-306); rows with similarity below a configurable threshold feed the BR-004 "material change" flag.

## 3.4 Run lifecycle

1. Trigger (EventBridge schedule, or `POST /runs` for ad-hoc/filtered runs — FR-506/210) creates the `run` row and audit `RUN_START`; `config_snapshot` captured.
2. Active + APPROVED questions fetched (optional persona/TA/domain filter).
3. Per question: dispatch to **all enabled targets concurrently** (NF-003), each call wrapped in a per-target token-bucket rate limiter (FR-207) and retry policy (3 attempts, 2s/4s/8s backoff → FAILED; FR-206). All target responses for a question commit before the next question (FR-204).
4. Persona-appropriate system prompts from `config/system_prompts.yaml` (Medical-Affairs reviewable text files — FR-205).
5. Validator (Claude, role=ORCHESTRATOR) classifies suspicious responses; truncation triggers one retry with raised `max_tokens` (FR-211); provider safety blocks → BLOCKED (IN-204).
6. Budget guard: token count checked each iteration; exceeding `MAX_TOKENS_PER_RUN` → run status PAUSED_BUDGET + notification (NF-015).
7. **Resume (FR-504/NF-005):** every (run, question, target) result commits independently; relaunching the same `run_id` skips pairs that already have rows. The EventBridge run task retries once on task failure, achieving automatic resume with zero operator action.
8. Run summary persisted: duration, counts by status, alerts, tokens, estimated cost from `config/pricing.yaml` (NF-008/014); webhook notification (FR-505, prototype).
9. Scoring pass launches automatically post-run, plus a sweeper that scores any unscored responses every 5 minutes (FR-406 — within-5-minutes satisfied).

## 3.5 Scoring & alerting

- Claude invoked with **forced tool-use / structured output** against a strict JSON schema (FR-404): `{sentiment_score: float, competitive_position: enum, brand_mentions: [], key_claims: [≤5], scoring_rationale: str}`; Pydantic-validated; parse failures stored raw in audit and retried once.
- Brand/competitor context injected from `config/brands.yaml` (SE-007).
- Alert rules (FR-405): `sentiment_score < −0.3` **OR** `competitive_position == NOT_RECOMMENDED` **OR** competitor sentiment exceeds focus-brand sentiment by a configurable Δ (default 0.4) within the same response.
- `POST /scores/rescore?prompt_version=…` re-scores historical responses as **new versions** (FR-407); human override endpoint records `scored_by=HUMAN` + rationale without touching the AI score (FR-408).

## 3.6 API surface (FastAPI)

| Endpoint group | Purpose / SRS |
|---|---|
| `GET/POST/PATCH /questions` · `POST /questions/import-csv` · `GET /questions/coverage-gaps` | CRUD, versioning, CSV import (FR-101–106; FR-107 if time) |
| `POST /runs` (filters allowed) · `GET /runs` · `GET /runs/{id}` (live progress) · `POST /runs/dry-run` | FR-501–506, FR-209/210 |
| `GET /responses` (filter by LLM, persona, TA, brand, domain, date range, sentiment range, alert status) · `GET /responses/{id}` (+latest score, rationale, diff) | FR-303/306/307, FR-605 |
| `GET /export?format=csv\|json` | FR-305 |
| `GET /analytics/sentiment-distribution` · `/positioning` · `/volume` · `/alerts-summary` · `/llm-comparison` | FR-602/606 |
| `POST /scores/rescore` · `POST /scores/{id}/override` | FR-407/408 |
| `GET /health` (per-target connectivity report) | NF-009, IN-502 |
| `GET /export/static-dashboard` | self-contained HTML snapshot (FR-603) |

## 3.7 Dashboard (React + Vite + Recharts, served via S3/CloudFront)

- **Overview** — the four FR-602 panels: sentiment distribution by LLM × therapy, stacked competitive-positioning bars by LLM, alert KPIs + feed, response volume over time; global filters for persona/TA/LLM/date (FR-604).
- **Responses** — filterable, paginated table; detail drawer with full unedited response text, scoring rationale, key-claim chips, and a colorized diff vs. the previous run (FR-605, BR-004).
- **Compare** — pick a question, see every LLM's answer side by side with sentiment badges (FR-606; the highest-impact readout screen).
- **Runs** — run history with status/tokens/cost, live progress for in-flight runs, "Run now" and "Dry run" buttons.
- **Questions** — repository browser with approval status, CSV import, coverage-by-persona/TA view (supports the AC-02 evidence).

---

# PART 4 — AWS Deployment Architecture (Production POC)

## 4.1 Topology

```
                        ┌────────────────────────── AWS Account ──────────────────────────┐
                        │                                                                  │
  Stakeholders ───────▶ │  CloudFront ──▶ S3 (React static build)                          │
  (internal URL)        │      │                                                           │
                        │      └─/api/*─▶ ALB ──▶ ECS Fargate Service: FastAPI (API)       │
                        │                              │            ▲                      │
                        │  EventBridge Scheduler ──▶ ECS Fargate **Run Task**              │
                        │  (cron 0 2 * * * — FR-502)   (orchestrator batch job)            │
                        │                              │                                   │
                        │                              ├──▶ Amazon Bedrock (IAM task role  │
                        │                              │     — no static keys)             │
                        │                              ├──▶ RDS PostgreSQL (private subnet)│
                        │                              ├──▶ Secrets Manager (DB creds,     │
                        │                              │     future OpenAI/Google keys)    │
                        │                              └──▶ CloudWatch Logs (JSON logs,    │
                        │                                    audit mirror) + Alarms        │
                        │  SNS / webhook ◀── run-completion + budget notifications         │
                        │  AWS Budgets ◀── Bedrock spend guardrail                         │
                        └──────────────────────────────────────────────────────────────────┘
```

## 4.2 Component choices and rationale

| Concern | Choice | Why (and why not alternatives) |
|---|---|---|
| API hosting | **ECS Fargate service** behind an ALB | Containerized FastAPI; no servers to manage; scales to zero-ish cost at POC size. App Runner is the simpler fallback if VPC/RDS networking is a constraint. |
| Run execution | **Separate ECS Fargate task** launched by EventBridge | A daily run can take up to 4 hours (NF-001) — wrong shape for Lambda (15-min cap) and unsafe inside the API container (deploys/restarts would kill runs). Decoupled task = AC-01's "unattended" is structural. |
| Scheduling | **EventBridge Scheduler** (cron, FR-502) | Survives application restarts; native retry; one-click disable. APScheduler kept for local dev behind the same `RunLauncher` interface. |
| Database | **RDS PostgreSQL** (single-AZ, small instance for POC) | Durable across container restarts (NF-005); JSONB for claims/mentions; DB-level append-only grants (SE-003/FR-304); 24-month retention (DM-001) is trivial. |
| LLM access | **Bedrock via task IAM role** | `bedrock:InvokeModel` on the task role — zero static credentials for the entire POC model fleet (IN-501 exceeded). |
| Secrets | **Secrets Manager** | DB credentials now; OpenAI/Google/OpenRouter keys later, injected as env vars into tasks — same variable names the local `.env` uses, so code is identical in both environments (IN-501/502). |
| Frontend | **S3 + CloudFront** | Static React build; one shared internal URL (FR-603); cheap, instant. |
| Observability | **CloudWatch Logs + Alarms** | Structured JSON app logs (NF-007) stream natively; audit log mirrored for tamper-evidence (SE-003); alarms on run-task failure and zero-runs-in-24h (supports AC-01 evidence); dashboards optional. |
| Cost guardrails | **AWS Budgets + in-app token budget** | NF-014/015 in-app per-run; account-level Bedrock spend alarm as backstop. |
| IaC | **Terraform (or CDK)** — one `infra/` module | Reproducible environment; tear-down after readout; a credibility signal at the review. |
| CI | **GitHub Actions** — lint, tests (NF-013), Docker build → ECR | Keeps the 70%-coverage requirement honest. |

## 4.3 Security posture mapped to SE requirements

- RDS and ECS tasks in private subnets; only ALB/CloudFront public. POC skips end-user auth per scope, but the internal URL can sit behIND corporate VPN/IP allow-listing — call this out at the readout.
- SE-001/BR-012: question bank is generic by construction; CSV import runs a PII heuristic lint (regex for names/DOB/MRN patterns) and flags suspect rows for review before approval.
- SE-003: append-only enforced by DB grants + CloudWatch mirror.
- SE-004: all data at rest in AbbVie-controlled AWS account (RDS + S3, encrypted at rest); responses never leave it.
- SE-005: legal ToS confirmation is a launch gate tracked in the README's go-live checklist — the system supports it but cannot satisfy it in code.
- SE-006: logging layer redacts strings matching credential patterns before write.
- SE-007: zero brand/competitor/indication strings in code — verified by a CI grep step against `brands.yaml` content.

## 4.4 Environments

| Env | DB | Models | Scheduler | Purpose |
|---|---|---|---|---|
| **local** | SQLite (or dockerized Postgres) | Bedrock via your AWS profile | APScheduler / manual CLI | development, demos |
| **aws-poc** | RDS Postgres | Bedrock via task role | EventBridge | the 7-day acceptance run + stakeholder URL |

Identical codebase and env-var names; the only difference is where values come from.

---

# PART 5 — POC Execution Strategy (Phases with Exit Criteria — no dates)

Each phase is independently demonstrable and gated by explicit exit criteria. Phases 1–5 deliver every SRS MUST; Phase 6 is the acceptance run.

### Phase 0 — Foundations
Repo scaffold; config system (pydantic-settings: `.env` + YAML with `${ENV}` interpolation); SQLAlchemy models + Alembic migrations; Dockerfiles; CI skeleton; `.env.example`.
**Exit:** app boots, migrations apply on SQLite and Postgres, CI green.

### Phase 1 — Question Repository
CRUD + versioning + soft delete; approval workflow fields; CSV import with PII lint; filtering; seed script generating a 100+ placeholder bank (3 personas × ≥2 TAs — AC-02 shape); coverage view.
**Exit:** 100 approved questions imported and filterable via API; edits create versions; nothing deletable. *(FR-1xx, SE-001/002 ✔)*

### Phase 2 — Provider Layer + Response Agent  ← the slide-2 "POC Focus"
ProviderClient ABC + `BedrockClient` (Converse) + dormant OpenAI/Google/OpenRouter clients + Open Evidence stub; target registry from YAML; startup credential validation; health endpoint; orchestrator with concurrency, rate limiting, retry/backoff, truncation handling, resume, budget guard; append-only response + audit writes; run summaries with cost; dry-run mode.
**Exit:** a full 100-question run across 3–4 Bedrock targets completes unattended; kill-and-relaunch mid-run resumes without duplicates; a disabled OpenRouter target can be enabled by config alone and health-checks successfully. *(FR-2xx, FR-3xx storage, FR-503/504, IN-3xx/5xx, NF-001/003/005–008/010/014/015 ✔)*

### Phase 3 — Scoring & Alerting
Structured-output scorer; versioned scoring records; alert rules; auto-score sweeper; re-score and human-override endpoints; diff computation + material-change flag.
**Exit:** every SUCCESS response from Phase 2's run has a score within 30 minutes (NF-002); alerts fire on synthetic negative fixtures; re-scoring produces v2 records with v1 intact. *(FR-4xx, BR-004–007 ✔)*

### Phase 4 — Query API + Dashboard
Full filter/pagination/export endpoints; analytics endpoints; React dashboard (Overview → Responses+drawer → Compare → Runs → Questions); static HTML snapshot export.
**Exit:** Medical Affairs walkthrough script executable end-to-end: filter → drill into a flagged response → read rationale → compare LLMs side-by-side → export CSV + static HTML. *(FR-3xx query, FR-6xx ✔)*

### Phase 5 — AWS Deployment & Hardening
Terraform for the Part-4 topology; ECR images; EventBridge schedule live; Secrets Manager wiring; CloudWatch alarms; DB append-only grants; unit tests to ≥70% on the four NF-013 modules; README with setup + add-a-target guide (NF-011).
**Exit:** scheduled 02:00 UTC run executes in AWS with zero human action; dashboard reachable on the shared URL; alarms verified by forced failure. *(FR-501/502, SE-003/004/006, NF-009–013 ✔)*

### Phase 6 — Acceptance Run & Readout
7 consecutive days of scheduled runs in aws-poc; daily glance at CloudWatch (observation, not intervention); accumulate longitudinal data → diff/change-detection views become genuinely meaningful; prepare the readout: baseline sentiment & positioning narrative (BR-005), alert review with Medical Affairs, AC evidence pack pulled from run logs.
**Exit = AC sign-off:** AC-01 (0 interventions / 7 days), AC-02 (≥30 questions per persona, ≥2 TAs), AC-03 (≥95% response rate per target).

### Demo strategy for any showcase along the way
Regardless of phase, the highest-impact live demo is: dashboard populated with real prior runs → trigger a small **live filtered run** from the UI → watch responses and scores stream into the Runs page → open a fired alert → side-by-side Compare → export the static HTML. Each phase exit makes one more beat of that demo real.

---

# PART 6 — Requirements Traceability & Open Items

**Every SRS MUST maps to a phase:** BR-001–003/005/006/009–011 → Phases 1–3; FR-1xx → Phase 1; FR-2xx/3xx/5xx → Phase 2 (+5 for cloud scheduling); FR-4xx → Phase 3; FR-6xx → Phase 4; DM → schema (Phase 0/2); IN-3xx/5xx → Phase 2; SE → Phases 1/2/5; NF → Phases 2/5; AC-01–03 → Phase 6.

**Deferred (SHOULD/COULD or blocked):** FR-212 triple-query non-determinism analysis; BR-008 weekly digest; FR-107 coverage-gap report (cheap — include if time); Open Evidence (pending access confirmation); email/Slack notification channels (webhook prototyped); FR-408 UI polish (API exists).

**Open items for you:**
1. Which Bedrock model IDs are enabled in your account/region? I'll slot them into `targets.yaml`/`.env` — recommend Claude (target) + Nova Pro + Llama and/or Mistral Large for 3–4 targets.
2. Real curated question CSV, or shall I generate the placeholder bank (generic brand names, 3 personas, 2 TAs)?
3. Real brand/competitor names for `brands.yaml`, or placeholders?
4. Any constraints on the AWS account for the deployment (existing VPC, region, IaC preference Terraform vs CDK, VPN/IP-allow-listing for the dashboard URL)?
