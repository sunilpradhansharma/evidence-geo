# BR-013 — Medical Review Workflow (Phase 2): Research & Build-Effort Findings

| Field | Value |
| --- | --- |
| **Requirement** | BR-013 (NEW — Business Requirement) — Medical Review Workflow |
| **Priority** | Phase 2 (architect in Phase 1) |
| **Status** | Research / Draft (no code written) |
| **Date researched** | 2026-07-09 |
| **Purpose** | (1) Decompose the requirement, (2) determine whether/how Profound (tryprofound.com) provides it, (3) assess what it would take for us to build it — money (subscription / purchase / platform) vs. human engineering effort. |

> **Honesty note:** Where a fact could not be confirmed from public sources, it is explicitly flagged as *not found* / *not confirmed* rather than assumed. No pricing figures are invented.

---

## TL;DR

- **Profound does NOT provide this, and it is squarely outside Profound's domain.** BR-013 is a pharma **medical-review / pharmacovigilance (PV) case-management workflow** (flagged-item queue with statuses, dispositions, assignment, audit, role restriction, AE reporting). Profound is a horizontal **marketing GEO/AEO** tool. Across its full public surface (dev docs, REST API groups, MCP capabilities, ~120 blog release titles, pricing) there is **no** review queue, no case/disposition workflow, no adverse-event/PV concept, and no Medical-Affairs/PV roles.
- **Dedicated PV platforms exist** (category examples: Veeva Vault Safety, Oracle Argus, ArisGlobal LifeSphere) for the downstream safety process — but BR-013 is explicitly an **upstream internal triage queue that *feeds* PV, not a PV system** (our own charter says the product "is not a replacement for MLR, pharmacovigilance… it feeds them"). So buying a PV platform is out of scope. **This is a build.**
- **We already have strong raw material:** an **append-only audit trail** (`AuditLog` + `write_audit`, SE-003 "never updates existing rows") ideal for BR-013.4/.7; **social AE flagging** (`ae_flag` on posts/comments, fail-closed) already API-queryable (`GET /social/posts?ae_only=true`); and **response alerts** (FR-405). What is missing is the **queue/case layer itself**.
- **Two dependencies / gaps to flag:** (1) **RBAC is currently removed** from the codebase, so BR-013.6 (MA/PV edit vs Brand read-only) has **no foundation today** — same dependency as BR-008a. (2) The response **alert engine has no adverse-event rule** (only sentiment/position/competitor), so "flagged AI responses" today are not AE-based; the queue's response-side inputs need defining.
- **Money: $0 mandatory.** Pure engineering on the existing stack (SQLAlchemy, FastAPI, existing audit, existing alerts/social-AE, recharts). The **Phase 1 deliverable (BR-013.8) is small**: a data model + API endpoint; the full workflow UI + RBAC + metrics are Phase 2.

---

## 1. Requirement decomposed into capability primitives

| # | Sub-requirement | Capability primitive |
| --- | --- | --- |
| BR-013.1 | Medical Review Queue for flagged responses + social posts (FR-405, FR-804, FR-806) | Queue ingestion from alerts + AE flags |
| BR-013.2 | Item detail: full text, translation, brand/TA, alert type+rule, run ref+timestamp, source platform | Denormalized item view |
| BR-013.3 | Statuses: New, In Review, Escalated to PV, Escalated to MA, Submitted (AE), Reviewed — No Action, Closed | Status state machine |
| BR-013.4 | Annotate disposition, assign owner, escalate to PV, mark non-reportable — logged immutably | Actions + immutable audit |
| BR-013.5 | Track time-to-triage, time-to-close per item + per alert type | Metrics from timestamps |
| BR-013.6 | Role-restricted: MA/PV edit+disposition; Brand read-only status | RBAC (roles + gating) |
| BR-013.7 | Periodic AE Signal Summary report (escalated-to-PV within a date range) | Scheduled/date-ranged report |
| BR-013.8 | Phase 1: expose data structure + API endpoint even if UI deferred to Phase 2 | Model + API (near-term) |

---

## 2. How Profound does it

### 2.1 Finding: no equivalent capability (out of domain)

Profound is a marketing **Answer Engine Optimization** platform. Nothing in its public surface resembles a medical-review / PV case-management workflow:

- **No review/triage/case queue.** No "inbox," "tasks," "tickets," "cases," or "dispositions" appear anywhere in the dev docs, REST API groups (`Categories, Answers, Reports, Content Optimization, Agents, Organization`), MCP capabilities, or the ~120 blog release titles reviewed.
- **No adverse-event / pharmacovigilance concept.** Profound does not detect, triage, or report adverse events; it has no safety/compliance domain at all.
- **No Medical-Affairs / Pharmacovigilance roles.** BR-013.6's role model does not exist there.

### 2.2 Adjacent Profound capabilities (for completeness — none satisfy BR-013)

- **Alerts + Slack** — Profound can push notifications (native Slack integration), but that is notification, not a governed disposition queue.
- **"Actions"** — recommended *marketing optimizations* to improve visibility, not review dispositions.
- **"Agents / Workflows"** — *marketing automation*, not a compliance case workflow.
- **Access model** — enterprise **SSO (OIDC/SAML)**, **Agency Mode**, **Projects/Folders**, and an **`Organization`** REST group provide coarse team/workspace access separation, but **not** domain-specific MA/PV disposition rights.

### 2.3 Buy alternative (honest framing)

Dedicated **pharmacovigilance/safety case-management platforms** exist (category examples, **not evaluated here**: Veeva Vault Safety, Oracle Argus, ArisGlobal LifeSphere). These run the *downstream* regulated PV process. BR-013 is deliberately scoped as an **upstream internal triage queue that feeds** that process (per our charter), so integrating/buying a full PV system is **out of scope and disproportionate**. The right answer is a lightweight native build.

### 2.4 Sourcing caveat (method + limitation)

Profound's marketing/app pages render in chunks the available fetch tool cannot fully page into. This is a **negative finding** grounded in the parts that *are* fully readable (dev docs, REST API index, MCP capabilities) plus the complete blog-title timeline and pricing page. It remains possible an undocumented internal feature exists, but there is **no public evidence** of any medical-review/PV workflow — consistent with Profound being a marketing tool.

---

## 3. What we have today (the build baseline)

**Already present and reusable:**

- **Append-only audit trail (BR-013.4/.7).** `backend/app/models/audit_log.py` (`AuditLog`) + `backend/app/utils/audit.py` (`write_audit`, SE-003, *"Never updates existing rows"*). Ideal immutable action log.
- **Social AE flagging (FR-802 / social side of BR-013.1).** `SocialPost.ae_flag` + `SocialComment.ae_flag`, set fail-closed via `backend/app/guardrails/adverse_event.py` OR'd with the LLM classifier — `backend/app/social/pipeline.py`, `backend/app/social/classify.py`. Already API-queryable: `GET /social/posts?ae_only=true` (`backend/app/api/social.py`).
- **Response alerts (FR-405).** `Alert` rows via `backend/app/scoring/alert_engine.py` (rules: `LOW_SENTIMENT`, `NOT_RECOMMENDED`, `COMPETITOR_ADVANTAGE`), each with `created_at`, surfaced in analytics.
- **Immutable responses + translation.** Responses stored immutably; social non-English text stores `text_en` + original (FR-804) — satisfies BR-013.2's "translation where applicable."
- **Snowflake mirror** of alerts/responses/social for reporting.

**Gaps (nothing exists for BR-013):**

- **No Medical Review Queue / case model** — no item entity linking a response *or* social post to a status, owner, disposition, or triage/close timestamps.
- **No status state machine, assignment, disposition, or escalation** actions.
- **No time-to-triage / time-to-close metrics.**
- **No AE Signal Summary report.**
- **No RBAC (BR-013.6 has no foundation).** Auth/roles were removed from the current tree — grep for `get_current_user` / `require_*` / `auth_service` / `models.user` returns **nothing**. Same dependency as BR-008a.
- **No AE alert rule for AI *responses*.** The alert engine flags sentiment/position/competitor only; AE detection currently exists for *social* content, not responses. So "flagged responses" feeding the queue must be defined (add an AE rule, and/or ingest the 3 existing rules).

---

## 4. What it would take to build

### Path A — Build it natively in our app (recommended)

| Component | New work | Money | Effort | Phase |
| --- | --- | --- | --- | --- |
| `MedicalReviewItem` model (polymorphic link to response *or* social post/comment; denorm brand/TA/source; alert_type+rule; run_ref+timestamp; status; owner; disposition_rationale; triaged_at/closed_at) | New table | None (OSS) | Low–Medium | **P1 (BR-013.8)** |
| Queue API endpoints (list/filter, get detail, status transition, assign, annotate, escalate) | New router | None | Low–Medium | **P1 (BR-013.8)** |
| Ingestion into the queue from response `Alert` rows + social `ae_flag` items | Wire-up + optional new AE alert rule for responses | None | Low–Medium | P1/P2 |
| Immutable action logging (disposition/assign/escalate) | Reuse `write_audit` | None | Low | P1/P2 |
| Status state machine + validation (7 statuses) | Service logic | None | Medium | P2 |
| Time-to-triage / time-to-close metrics (per item + per alert type) | Derive from timestamps + aggregation | None (recharts) | Low–Medium | P2 |
| AE Signal Summary report (escalated-to-PV, date-ranged) | Report/export (synergy with BR-008a digests) | None | Low–Medium | P2 |
| Role restriction — MA/PV edit; Brand read-only (BR-013.6) | **Reintroduce RBAC** (roles + gating) | None | Medium | P2 (**shared dep with BR-008a**) |
| Front-end Medical Review Queue UI | New page + drawers | None (recharts/existing UI kit) | Medium | P2 |

**Key point:** Path A needs **no mandatory purchase, subscription, or third-party platform** — it reuses the existing stack (SQLAlchemy, FastAPI, the append-only audit trail, existing alerts + social-AE detection, recharts). The **Phase 1 slice is intentionally small** (model + API + audit hooks), which is exactly what BR-013.8 asks for; the workflow UI, metrics, AE report, and RBAC land in Phase 2.

### Path B — Adopt Profound and integrate

- **Fit: none.** Profound has no medical-review/PV/AE workflow; adopting it would not advance BR-013 at all. (Profound could still be a *data source* for other requirements, but it does not touch this one.)

### Path C — Integrate a dedicated PV platform

- **Out of scope by design.** BR-013 is upstream triage that *feeds* PV, not the regulated PV system of record. Heavyweight, expensive, and disproportionate for an internal flagged-item queue.

### Money summary (precise)

- **To satisfy BR-013: $0 in mandatory licensing.** It is a **human-engineering effort** on the existing stack.
- **No purchase** (Profound or PV platform) is appropriate or necessary for this requirement.

### Key risks / caveats (accuracy)

- **RBAC is a hard prerequisite for BR-013.6** and is currently absent (removed). BR-013.8 (model + API) can be built without it, but role-restricted disposition rights cannot be *enforced* until RBAC is reintroduced — coordinate with BR-008a, which has the same dependency.
- **"Flagged responses" is under-specified vs our code.** BR-013.1 cites FR-405/FR-804/FR-806, but in our implementation FR-804 = translation and FR-806 = social scope (not alert rules), and the **response alert engine has no AE rule**. Define precisely which signals enqueue items (existing 3 response rules, a new AE response rule, and social `ae_flag`).
- **Mutable queue + immutable audit.** Responses/audit are append-only; the queue item's *state* must be mutable, but every transition should be written to the immutable `AuditLog` — consistent with the existing pattern.
- **AE Signal Summary (BR-013.7) overlaps BR-008a** — build it once as a shared reporting component (PV-escalated items feed both the BR-013.7 report and the BR-008a PV digest).

---

## 5. Open decisions that change scope

- **Queue scope for Phase 1:** responses only, or responses + social AE from the start?
- **AE on responses:** add an adverse-event alert rule to the response alert engine (currently none), or keep AE detection social-only?
- **RBAC timing:** reintroduce roles now (shared with BR-008a) or stub roles and defer enforcement to Phase 2?
- **AE Signal Summary home:** standalone report, an export, or a role profile of the BR-008a digest engine?

---

## Appendix — Sources consulted (2026-07-09)

- Profound developer docs (`https://docs.tryprofound.com/`, `.../rest-api/introduction`, `.../mcp/overview`, `.../mcp/capabilities/*`) — product/API surface; no review-queue / PV / roles-for-disposition capability.
- `https://www.tryprofound.com/blog` — full release-title timeline (Actions, Agents/Workflows, Slack, Agency Mode, SSO, HIPAA, SOC 2) — no medical-review/PV workflow.
- `https://www.tryprofound.com/pricing` — custom enterprise pricing (no public numbers).
- Dedicated PV platforms named as **category examples only, not evaluated**: Veeva Vault Safety, Oracle Argus, ArisGlobal LifeSphere.

**Codebase files referenced:** `backend/app/models/audit_log.py`, `backend/app/utils/audit.py`, `backend/app/scoring/alert_engine.py`, `backend/app/models/alert.py`, `backend/app/social/pipeline.py`, `backend/app/social/classify.py`, `backend/app/guardrails/adverse_event.py`, `backend/app/api/social.py`. RBAC absence verified by grep (`get_current_user` / `require_*` / `auth_service` / `models.user` → no results).
