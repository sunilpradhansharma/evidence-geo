# FR-108a — Disease-State / Pre-Launch Question Mode: Research & Build-Effort Findings

| Field | Value |
| --- | --- |
| **Requirement** | FR-108a (NEW — extends FR-108) — Disease-State / Pre-Launch Question Mode |
| **Priority** | Medium |
| **Status** | Research / Draft (no code written) |
| **Date researched** | 2026-07-09 |
| **Purpose** | (1) Decompose the requirement, (2) determine whether/how Profound (tryprofound.com) provides it, (3) assess what it would take for us to build it — money (subscription / purchase / platform) vs. human engineering effort. |

> **Honesty note:** Where a fact could not be confirmed from public sources, it is explicitly flagged as *not found* / *not confirmed* rather than assumed. No pricing figures are invented.

---

## TL;DR

- **This requirement fits Profound's architecture well** — arguably better than the previous two. Profound is **category- and competitor-native**: verified from its MCP capabilities, it retrieves "visibility metrics and performance data for **companies within specific categories**" and "sentiment data … **across companies and topics**," and its REST API has a **`Categories`** group. You do **not** need to own a brand — tracking a category with a set of competitors (i.e., disease-state / pre-launch monitoring) is the default way Profound works.
- **What Profound does NOT provide (pharma-specific):** a **governed, MLR-approved, versioned question bank** (it auto-recommends prompts), an explicit **Brand-vs-Disease-State mode toggle** (unnecessary in a tool with no privileged "your brand"), pharma vocabulary (**disease state / indication / therapeutic area**), and the **"Pre-Launch / Pipeline Intelligence — No AbbVie Brand Asset"** labeling.
- **Our side is closer than it looks.** `Question` already has `disease` + `indication` columns and full **approval / versioning / audit** (so FR-108a.5 is free). A **brand-agnostic precedent already exists** in `brands.yaml` (the `Obesity` area tracks competitor GLP-1s as "focus" because AbbVie has no asset) — but only Social Listening / Discovery use it, not the run/scoring path.
- **What blocks us:** `Question.brand_focus` is **required (non-null)**, and scoring/positioning + analytics are **single-focus-brand oriented** (grouped by `brand_focus`). FR-108a needs a **mode flag**, an **optional brand focus**, a **multi-competitor landscape** scoring/aggregation, a **mode filter**, and **report labeling**.
- **Money: $0 mandatory.** It is a **human-engineering effort** on the existing stack (SQLAlchemy, Bedrock scoring, recharts, existing analytics). No subscription/purchase required. This is a **meatier build than the previous two** because it touches the scoring model and several aggregations.

---

## 1. Requirement decomposed into capability primitives

| # | Sub-requirement | Capability primitive |
| --- | --- | --- |
| FR-108a.1 | "Disease-State Monitoring" mode — runs without a primary AbbVie brand focus | Mode flag + optional brand focus |
| FR-108a.2 | Questions tagged with TA, disease state, competitor brands as focus entities | Focus-entity tagging |
| FR-108a.3 | Scoring, competitive benchmarking, citation analysis using competitors / disease entities as subject | Subject-agnostic scoring |
| FR-108a.4 | Multi-competitor landscape view (each competitor relative to others and to the disease state) | Landscape aggregation + view |
| FR-108a.5 | Same approval, versioning, audit as brand questions (FR-102/103/106) | Reuse existing governance |
| FR-108a.6 | Dashboard filter by mode (Brand vs Disease-State); disease-state insight summaries | Mode filter + insights scoping |
| FR-108a.7 | Pre-launch reports labeled "Pre-Launch / Pipeline Intelligence — No AbbVie Brand Asset" | Report labeling |

---

## 2. How Profound does it

### 2.1 What Profound has (verified — a good fit)

- **Category + multi-competitor is the native model.** Verified from `https://docs.tryprofound.com/mcp/overview`: **Visibility Reports** = "visibility metrics and performance data for **companies within specific categories**"; **Sentiment Analysis** = "across **companies and topics**"; **Citation Reports** = "mentions, references, and citation patterns across domains and pages." The REST API has a first-class **`Categories`** endpoint group (`https://docs.tryprofound.com/rest-api/introduction`).
- **Brand-agnostic demand + prompts.** **Prompt Volumes** ("See what your audience asks Answer Engines"), a **data-driven prompt recommendation engine**, **Personas** (audience segments), and **Keyword / Asset Hierarchies** — all category-level, not dependent on owning a brand.
- **Pre-launch is inherent.** Because Profound is a marketing-intelligence tool, monitoring a category before you have a product in it is the default; there is no "you must own the top brand" constraint. Competitive **share-of-voice / landscape** across companies in a category is core to the product.

### 2.2 What Profound does NOT have / could NOT be confirmed (precise)

- **No governed question bank.** Profound generates/recommends prompts; there is **no MLR-approval workflow, versioning, or audit trail on a question repository** (FR-108a.5). That governance is ours to provide.
- **No explicit "Brand vs Disease-State mode."** Profound has no privileged "your brand" entity, so it needs no mode switch — it is *always* category/competitor-first. The **mode concept is an AbbVie-specific framing**, not a Profound feature.
- **No pharma vocabulary or pre-launch labeling.** Profound uses "category / topic / keyword," not "disease state / indication / therapeutic area," and has no **"Pre-Launch / Pipeline Intelligence — No AbbVie Brand Asset"** label.

### 2.3 Sourcing caveat (method + limitation)

Profound's marketing/app-feature pages render in chunks the available fetch tool cannot fully page into. Feature *existence* is grounded in **developer docs** (MCP capabilities + REST API index — authoritative for what exists) and **blog content/titles**; exact in-app competitive-landscape UI (e.g., a specific "competitor matrix" chart) is **inferred, not fully verified**.

### 2.4 Net assessment of Profound

Profound's data model **is** disease-state / pre-launch competitive monitoring (category + companies), so conceptually it "already builds it." The gaps versus FR-108a are exactly the **pharma-specific wrappers** — governed question bank, mode toggle/labeling, and disease-state vocabulary — which our system already has (governance) or needs to add (mode + label). Buying Profound would give strong category/competitor data but would **not** deliver the governed, labeled, mode-aware workflow the requirement specifies.

---

## 3. What we have today (the build baseline)

**Already present and reusable:**

- **Disease-state tagging fields exist.** `Question.disease` and `Question.indication` are already columns (nullable) — `backend/app/models/question.py`.
- **Governance is built in (FR-108a.5 is free).** `Question` has `approval_status`, `approver_name`, `version`, `superseded_by`, soft-delete + audit — `backend/app/models/question.py`.
- **A brand-agnostic precedent already exists in config.** `backend/app/config/brands.yaml` `Obesity` area tracks competitor GLP-1s (Wegovy, Zepbound, Ozempic, Mounjaro…) as `focus_brands` with an explicit comment: *"AbbVie has no marketed obesity asset yet, so the marketed GLP-1 leaders are tracked as the monitored ('focus') brands."* Every entry carries `company`, making non-AbbVie ownership explicit. **However, this is only consumed by Social Listening + Discovery — not the run/question-bank/scoring path.**
- **Multi-brand signal already captured per response.** Scoring stores `brand_mentions` (list of `{brand, sentiment, is_competitor}`), which is a ready hook for a multi-competitor landscape.

**Gaps (what FR-108a needs):**

- **`brand_focus` is required (non-null).** `backend/app/models/question.py` (`Mapped[str]`). A brand-agnostic question can't be authored without making this optional or using a disease/competitor sentinel.
- **No `monitoring_mode` concept** on `Question` or `Run`.
- **Scoring/positioning is single-focus-brand oriented.** `competitive_position` is a single-brand classification (FIRST_LINE … NOT_RECOMMENDED / NOT_MENTIONED) about *the* focus brand; `scorer._context_for` and the `COMPETITOR_ADVANTAGE` alert compare competitors *against* one focus brand — `backend/app/scoring/scorer.py`, `backend/app/scoring/alert_engine.py`.
- **Analytics + warehouse group by `brand_focus`.** `sentiment_by_brand`, `positioning_by_brand`, etc. assume a focus brand — `backend/app/snowflake/cortex.py`, `backend/app/api/analytics.py`, Snowflake views + semantic model.
- **No multi-competitor landscape view**, **no mode filter**, **no pre-launch report label**.

---

## 4. What it would take to build

### Path A — Build it natively in our app (recommended)

| Component | New work | Money | Effort |
| --- | --- | --- | --- |
| `monitoring_mode` (BRAND / DISEASE_STATE) on `Question` + `Run`; make `brand_focus` optional | Schema + SQLite auto-migrate + Snowflake column | None | Low–Medium |
| Disease-state question authoring (tag TA / disease / competitor focus entities) | Reuse `disease`/`indication` + add focus-entity handling; UI form | None | Medium |
| Run engine: execute without an AbbVie brand; select disease-state question sets | Relax focus-brand assumptions in run/orchestrator | None | Low–Medium |
| Subject-agnostic scoring (competitor / disease as subject) | Adapt `scorer._context_for` + scoring prompt; reuse `brand_mentions` | Uses existing Bedrock spend | **Medium–High** |
| Multi-competitor landscape view (FR-108a.4) | Aggregate `brand_mentions` → per-competitor matrix; new endpoint + chart | None (**recharts already a dep**) | Medium |
| Dashboard mode filter + disease-state insight summaries (FR-108a.6) | Add mode param (insights already filters by disease/TA/indication/brand) | None | Low–Medium |
| Pre-launch report labeling (FR-108a.7) | Inject label in exports / pinpoint / reports when mode = disease-state | None | Low |
| Governance (FR-108a.5) | Reuse existing `Question` approval/versioning/audit | None | None |
| Ripple: null-`brand_focus` handling across analytics / cortex / Snowflake views + semantic model | Group-by + view updates for null / mode | None | Medium |

**Key point:** Path A needs **no mandatory purchase, subscription, or third-party platform** — everything reuses the existing stack (SQLAlchemy, Bedrock scoring, recharts, existing analytics/insights). The substantive work is (a) the **subject-agnostic scoring redesign** and (b) the **`brand_focus`-optional ripple** across aggregations — which make this **larger than the BR-008a and FR-707a builds**.

### Path B — Adopt Profound and integrate

- **Fit:** strong for the *data* (category + competitor visibility/sentiment/citation benchmarking is native), and it would satisfy the spirit of FR-108a.3/.4 well. But it would **not** provide the governed/versioned question bank (FR-108a.5), the mode toggle, or the pre-launch labeling — those remain ours.
- **Money:** enterprise contract, **custom pricing (not public)**. No figure guessed.
- **Verdict:** the most defensible "buy" candidate of the three requirements *if* category/competitor intelligence is wanted broadly — but for FR-108a as specified (governed, labeled, mode-aware), we still build the pharma wrapper.

### Money summary (precise)

- **To satisfy FR-108a alone: $0 in mandatory licensing.** It is a **human-engineering effort**.
- **No purchase unlocks the pharma-specific parts** (governed bank, mode, labeling) — those are bespoke either way.
- **Profound:** only if we want their category/competitor platform; enterprise pricing is not public.

### Key risks / caveats (accuracy)

- **Scoring redesign is the crux.** The `competitive_position` enum is single-brand-centric; a true multi-competitor landscape needs per-entity positioning or a new landscape scoring approach. The existing per-response `brand_mentions` (multi-brand sentiment) reduces but does not eliminate this work.
- **`brand_focus` is assumed non-null in many places** (analytics group-bys, Cortex SQL, Snowflake views, exports). Making it optional has a **moderate ripple**; each aggregation must handle null / mode.
- **Vocabulary alignment.** We already have `disease` + `indication`; ensure disease-state "focus entities" (competitor set) are modeled consistently (config vs per-question) so scoring context is correct.

---

## 5. Open decisions that change scope

- **Where do "focus entities" live?** Per-question competitor list, or derived from `brands.yaml` per disease state (the `Obesity` pattern), or both?
- **`brand_focus` optional vs sentinel:** make the column nullable, or store the disease/competitor as the focus value with a mode flag?
- **Landscape scoring shape:** per-competitor `competitive_position` rows, or a single "landscape" record per question aggregating `brand_mentions`?
- **Scope for POC:** reuse the existing `Obesity` config as the first disease-state exemplar to prove the flow end-to-end before generalizing?

---

## Appendix — Sources consulted (2026-07-09)

- `https://docs.tryprofound.com/mcp/overview` — "companies within specific categories," "across companies and topics," citation reports (category/competitor-native model).
- `https://docs.tryprofound.com/rest-api/introduction` — REST API groups incl. `Categories`, `Answers`, `Reports`, `Content Optimization`.
- `https://www.tryprofound.com/blog` — Prompt Volumes (brand-agnostic demand), data-driven prompt recommendation engine, Personas, Keyword/Asset Hierarchies.
- `https://www.tryprofound.com/pricing` — custom enterprise pricing (no public numbers).

**Codebase files referenced:** `backend/app/models/question.py`, `backend/app/config/brands.yaml`, `backend/app/scoring/scorer.py`, `backend/app/scoring/alert_engine.py`, `backend/app/snowflake/cortex.py`, `backend/app/api/analytics.py`, `backend/app/api/insights.py`, `backend/app/snowflake/tables.py`.
