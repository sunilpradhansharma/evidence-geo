# Evidence Monitoring Agent — POC

An **AI Brand Intelligence & Generative Engine Optimization (GEO)** platform for pharma. It
submits a curated, Medical-Affairs-approved question bank to a panel of large language models
(Claude, Nova, Llama on **Amazon Bedrock**, plus **GPT-4o**, **Gemini**, and the clinical
**EvidenceMD** model), stores every response immutably, scores brand sentiment & competitive
positioning with Claude, arbitrates a cross-model **Chairman consensus**, captures the **real
web sources** each grounded model cited, fires alerts on concerning findings, and surfaces it
all in a React dashboard. Layered on top are discovery, prompt-volume, source-authority,
GEO-intervention, model-release-impact, digest, and social-listening surfaces, plus an in-app
**Ema** copilot. The UI is branded **"AI Brand Intelligence"**.

> **POC scope:** runs locally with SQLite + Bedrock (the other providers are optional and
> keyed by credential). All brand/persona/question data is synthetic but realistic, using real
> brand names. No PII. A single-container EC2 deploy (Bitbucket Pipelines) is available — see
> [`docs/deploy.md`](docs/deploy.md). An optional Snowflake + Cortex warehouse layer is also
> available — see [`docs/snowflake.md`](docs/snowflake.md).

---

## Architecture

```
Question Repository ──▶ Orchestrator (Run Engine) ──▶ Provider Clients ──▶ LLM Targets
 (versioned, approved)   dispatch · retry · rate-limit    (per provider)   Bedrock:   Claude · Nova · Llama
                         resume · budget guard · routing                   OpenAI:    GPT-4o
                                                                           Google:    Gemini
                                                                           Anthropic: Claude (optional)
                                                                           EvidenceMD (Provider persona)
                                                                                   │
                                                                                   ▼
      Chairman (cross-model consensus) ◀────── Response Repository (append-only + cited sources)
                   │                                          │
                   ▼                                          ▼
      Scoring Engine (Claude) ──▶ Alert Engine         Audit Log (append-only)
                   │
                   ▼
      FastAPI REST API ──▶ React Dashboard (Vite + Recharts) · Ema copilot (LangGraph)
```

The `claude` target defaults to Bedrock (parametric, no citations) and auto-switches to the
direct Anthropic API with native web-search citations when `ANTHROPIC_API_KEY` is set.

- **Provider-agnostic by contract** (`app/providers/base.py`): one `ProviderClient` per API
  provider (`bedrock`, `openai`, `google`, `anthropic`, `evidencemd`, `open-evidence`); each
  target is pure YAML config. Adding a provider is a new adapter + a `registry.py` entry;
  adding a target is config only (NF-010).
- **Grounded provenance:** the OpenAI, Google, and (optional) direct-Anthropic targets run
  their hosted web search and return the **real source URLs + cited claims** they used,
  persisted per response.
- **Immutable responses** + **versioned scoring records** (FR-302/304).
- **Cross-model Chairman consensus** — agreement level, agreed recommendation, divergence
  points, and a synthesized final answer (`agent/chairman.py`).
- **Append-only audit log** of every external call with role tagging (SE-003, IN-302).

---

## Prerequisites

1. **Python 3.11+** and **Node.js 18+**
2. **AWS account with Bedrock access** in `us-east-2` (default). Enable model access in the
   AWS Console → Bedrock → **Model access** for the models you configure in `.env`, e.g.:
   - Anthropic Claude (Sonnet) — used for the `claude` target, orchestrator, and scoring
   - Amazon Nova
   - Meta Llama
   - List what your account can use with `python -m scripts.list_bedrock_models`.
3. **Optional providers & integrations** (each activated by a single credential, no code
   change): OpenAI (`OPENAI_API_KEY`), Google Gemini (`GOOGLE_API_KEY` or Vertex AI), direct
   Anthropic (`ANTHROPIC_API_KEY`), EvidenceMD (`EVIDENCEMD_API_KEY`), Tavily (Discovery),
   SEMrush (GEO Recommendations + Prompt Volume fetch), Apify (Social Listening), and
   Snowflake + Cortex. See `.env.example` for the full, commented list.

---

## Setup

### 1. Environment variables

Copy `.env.example` to `.env` and fill in your credentials. The minimum to run is AWS +
Bedrock; every other provider/integration is optional and keyed by its own credential (see
`.env.example` for the full, commented list):

```bash
cp .env.example .env
```

```env
AWS_ACCESS_KEY_ID=<your-key>
AWS_SECRET_ACCESS_KEY=<your-secret>
AWS_REGION=us-east-2
DATABASE_URL=sqlite+aiosqlite:///./evidence_monitoring.db

# Bedrock model IDs (newer models need the "us." inference-profile prefix)
TARGET_CLAUDE_MODEL_ID=us.anthropic.claude-sonnet-4-5-20250929-v1:0
TARGET_NOVA_MODEL_ID=us.amazon.nova-2-lite-v1:0
TARGET_LLAMA_MODEL_ID=us.meta.llama3-3-70b-instruct-v1:0
ORCHESTRATOR_MODEL_ID=us.anthropic.claude-sonnet-4-5-20250929-v1:0
SCORING_MODEL_ID=us.anthropic.claude-sonnet-4-5-20250929-v1:0
MAX_TOKENS_PER_RUN=500000
```

### 2. Backend

```bash
python -m venv .venv
.venv\Scripts\activate              # Windows  (source .venv/bin/activate on macOS/Linux)
pip install -r backend/requirements.txt

# Seed the base synthetic question bank (120+ questions: Dermatology, Gastroenterology
# and Oncology, 3 personas x 5 domains). Real brand names, no PII.
cd backend
python -m scripts.seed_questions

# Optional add-on banks (idempotent, safe to re-run): Lupron (3 areas, ~40 each),
# Dermatology (~40), Gastroenterology (~40), Rheumatology (~40),
# Neuroscience / Vraylar (~40), and pre-launch disease-state questions.
python -m scripts.seed_lupron_questions
python -m scripts.seed_dermatology_questions
python -m scripts.seed_gastroenterology_questions
python -m scripts.seed_rheumatology_questions
python -m scripts.seed_vraylar_questions
python -m scripts.seed_disease_state_questions

# Start the API
python -m uvicorn app.main:app --reload --port 8000
```

API docs available at http://127.0.0.1:8000/docs

### 3. Frontend

```bash
cd frontend
npm install
npm run dev
```

Dashboard at http://127.0.0.1:5173 (proxies `/api` → backend on 8000).

---

## Usage

1. Confirm target connectivity: `GET /health/targets` (or it runs at the start of every run).
2. In **Run Analysis → Standard Run**, use **Dry Run** to validate connectivity without
   storing data.
3. **Run Now** to execute the approved question bank across all enabled targets.
   - Use the persona / therapeutic-area / domain filters for a smaller, faster run.
   - Scoring and Chairman consensus run automatically after each run.
4. Explore results in **Insights & Trends** (dashboard), **AI Response Review** (per-response
   sources, sentiment, positioning, diffs), and the dashboard sub-tabs: **Insights**,
   **GEO Interventions**, **Source Authority**, and **AI Update Impact**.

---

## Dashboard & Feature Surfaces

Beyond the core run/score/review loop, the app ships these surfaces (React pages under
`frontend/src/pages/`, backed by FastAPI routers under `backend/app/api/`):

- **Insights & Trends** (`/dashboard`) — sentiment, competitive positioning, share of voice,
  and trend charts, with **Insights**, **GEO Interventions**, **Source Authority**, **AI
  Update Impact**, and **Ask a Question** (Cortex) sub-tabs.
- **Discover Questions** (`/harvest`) — harvests verbatim questions real people ask from
  public health communities via Tavily, classifies them, and stages them for MA review.
- **Social Listening** (`/social-listening`) — public social posts + comments (Apify) with
  sentiment, share of voice, AE signals, and an AI narrative brief with verbatim quotes.
- **Approved Question Bank** (`/questions`) — the versioned, MA-approved question repository.
- **Prompt Volume** (`/prompt-volume`) — search-demand intelligence from SEO/SERP CSV exports
  or an in-app SEMrush fetch; prioritizes gaps against the approved bank.
- **Run Analysis** (`/run-analysis`) — **Standard Run** (the run engine + execution view) and
  **Phrasing Variation** (test how question phrasing changes model answers).
- **AI Response Review** (`/results`) — every stored response with its cited sources,
  sentiment, positioning, Chairman consensus, and change-over-time diffs.
- **GEO Interventions** — SEMrush-informed, MLR-gated content recommendations to close gaps.
- **Source Authority** — classifies the domains models cite (AbbVie / competitor /
  independent), share of voice, most-cited pages, and claim-level provenance.
- **AI Update Impact** — correlates model version releases with shifts in answers.
- **Stakeholder Digests** (`/digests`) — role-specific (PV / Brand / Medical Affairs) digests,
  stored in-app with optional AWS SES email delivery.
- **Ema copilot** — an application-wide LangGraph assistant (bottom-right) that answers
  questions about the data and triggers governed actions behind a confirm step.

---

## Configuration (`backend/app/config/`)

| File | Purpose |
|------|---------|
| `targets.yaml` | LLM targets: provider + model_id + params + rate limits. Add a target here. |
| `target_routing.yaml` | Which targets run for each persona (e.g. EvidenceMD is Provider-only). |
| `brands.yaml` | Therapeutic areas, focus brands, competitors (content-agnostic code — SE-007). |
| `system_prompts.yaml` | Per-persona system prompts (Medical-Affairs reviewable). |
| `intent_rules.yaml` | Rule-based intent classification for questions. |
| `pricing.yaml` | Per-token pricing for cost estimation. |
| `harvest_sources.yaml` | Discovery: search domains, query templates, harvest limits. |
| `social_sources.yaml` | Social Listening: channels + Apify actors (demo Obesity/GLP-1 area). |
| `source_authority.yaml` | Curated domain → authority-bucket taxonomy (Source Authority). |
| `geo/` | Curated brand ground-truth YAML → generated JSON-LD schema + `llms.txt`. |

### Adding a new LLM target

1. (If a new provider) add a `ProviderClient` subclass in `app/providers/` and register it in
   `registry.py`.
2. Add a target entry to `targets.yaml` with `enabled: true`.
3. Add any required credential to `.env`. Done — no core logic changes.

---

## Snowflake + Cortex (optional)

The backend can mirror **all** data (questions, responses, scores, consensus, alerts,
audit log, themes, runs, social posts and comments, and raw API input/output) into **Snowflake**, and use **Cortex**
for an extra insight layer plus a natural-language "Ask your data" Q&A surfaced under
**Dashboard → Cortex**. SQLite stays the operational store; Snowflake is the warehouse +
Cortex layer. Only the backend connects to Snowflake (one key-pair service identity), so
the public app link works for everyone — end-users never authenticate to Snowflake.

Disabled by default (`SNOWFLAKE_ENABLED=false` → no-op). To enable, follow the one-time
setup and `.env` keys in [`docs/snowflake.md`](docs/snowflake.md).

---

## Social Listening (optional)

A complementary surface (separate from the core monitoring workflow) that scrapes public social
**posts and their comments/replies** (Reddit, TikTok, Instagram, Facebook, X) via **Apify** for a demo
Obesity/GLP-1 area. Posts and comments are PII-scrubbed, prompt-injection-screened,
adverse-event-checked (fail-closed), and LLM-scored for sentiment. **Comment sentiment is a separate
dimension from post sentiment**, and non-English text is auto-translated to English with a "Show
original" toggle. The `/social-listening` dashboard shows share of voice, post and comment sentiment,
volume, top themes, AE signals (posts + comments), and per-channel engagement leaders. Set
`APIFY_API_TOKEN` in `.env` to enable live ingestion (the page works read-only without it).

---

## Synthetic Data

- **Therapeutic areas:** Dermatology (Humira, Skyrizi, Rinvoq), Gastroenterology (Humira, Skyrizi, Rinvoq), Oncology (Imbruvica, Venclexta), Rheumatology (Rinvoq, Humira), Neuroscience (Vraylar), the Lupron franchise — Central Precocious Puberty (Lupron Depot-Ped), Endometriosis and Uterine Fibroids (Lupron Depot) — and Obesity / GLP-1 (Wegovy, Zepbound, Ozempic, Mounjaro) for the Social Listening surface.
- **Competitors:** real names (Stelara, Dupixent, Calquence, Brukinsa, etc.) in `brands.yaml`.
- **Personas:** Prospect, Patient, Provider. The base `seed_questions.py` bank is 120+ (Dermatology, Gastroenterology and Oncology, 3 personas x 5 domains).
- All questions are realistic, clinically plausible, and contain **no PII**.
- **Add-on banks** (idempotent loaders): ~40 approved questions per Lupron therapy (`scripts/seed_lupron_questions.py`), Dermatology (`scripts/seed_dermatology_questions.py`), Gastroenterology (`scripts/seed_gastroenterology_questions.py`), Rheumatology (`scripts/seed_rheumatology_questions.py`), Neuroscience / Vraylar (`scripts/seed_vraylar_questions.py`), and pre-launch disease-state questions (`scripts/seed_disease_state_questions.py`).

> **Immunology is retired.** It used to hold Plaque Psoriasis, Atopic Dermatitis, Ulcerative Colitis and Crohn's Disease in one block, which put a skin and a gut indication behind the same key. It is now split into **Dermatology** and **Gastroenterology**. Existing rows are migrated by `python -m scripts.backfill_therapeutic_area_split` (dry run by default, `--commit` to apply); anything it cannot resolve to a disease keeps the old value and surfaces as "Immunology (legacy)" rather than being guessed into a specialty.

---

## Project Layout

```
backend/
  app/
    config/            settings + YAML configs (targets, brands, routing, geo/, ...)
    models/            SQLAlchemy models (questions, runs, responses, scoring, alerts, ...)
    providers/         provider layer: bedrock, openai, google, anthropic, evidencemd
    agent/             orchestrator, rate limiter, budget guard, validator, Chairman
    scoring/           scorer, alert engine, diff/change detection
    harvest/           Discovery (Tavily) question harvester
    social/            Social Listening ingest + narrative
    source_authority/  cited-domain classification
    prompt_volume/     search-demand ingestion + gap analysis
    remediation/       GEO intervention recommendations
    model_updates/     model-release-impact correlation
    geo/               brand ground-truth corpus builder (JSON-LD + llms.txt)
    copilot/           Ema assistant (LangGraph)
    snowflake/         warehouse mirror + Cortex
    insights/          theme/insight tagging
    api/               FastAPI routers
    services/          business logic
    utils/             logging (JSON + redaction), PII lint, audit
  scripts/             seeders (seed_questions.py, ...) + probes/utilities
  tests/               pytest suite
frontend/
  src/
    pages/       Dashboard, Results, Pipeline, Questions, Harvest, Insights, SourceAuthority,
                 PromptVolume, ModelReleases, Digests, Cortex, SocialListening,
                 VariationTesting, Recommendations, HowToUse
    components/  shared UI + Ema ChatWidget
    api/         typed API client
```

---

## Requirements Mapping (highlights)

| Requirement | Where |
|-------------|-------|
| FR-101..107 Question Repository | `models/question.py`, `services/question_service.py`, `api/questions.py` |
| FR-201..211 Response Agent | `agent/orchestrator.py`, `providers/registry.py`, `providers/bedrock.py` |
| FR-301..307 Response Repository | `models/response.py`, `services/response_service.py` |
| FR-401..408 Scoring & Alerting | `scoring/scorer.py`, `scoring/alert_engine.py` |
| FR-501..506 Scheduling / Runs | `services/run_service.py`, `api/runs.py` |
| FR-601..606 Dashboard | `frontend/src/pages/` |
| Cross-model Chairman consensus | `agent/chairman.py` |
| Grounded source provenance | `providers/openai_client.py`, `providers/google_client.py`, `providers/anthropic_client.py` |
| BR-012 GEO Interventions | `remediation/`, `api/recommendations.py` |
| FR-706a Source Authority | `source_authority/`, `config/source_authority.yaml`, `api/source_authority.py` |
| FR-116 Prompt Volume | `prompt_volume/`, `api/prompt_volume.py` |
| FR-707a Model-Release Impact | `model_updates/`, `api/model_releases.py` |
| BR-008a Stakeholder Digests | `services/digest_service.py`, `api/digests.py` |
| Ema copilot | `copilot/`, `api/copilot.py` |
| SE-003 Append-only audit | `utils/audit.py`, `models/audit_log.py` |
| SE-006 Credential redaction | `utils/logging.py` |
| SE-007 Content-agnostic code | `config/brands.yaml` |
| NF-010 Add target via config | `providers/registry.py`, `config/targets.yaml` |
