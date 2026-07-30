Showcase by Sunday 6/14 **Evidence Monitoring Agent --- POC**

Software Requirements Specification

**DOCUMENT CONVENTIONS**

Requirements are identified by a unique ID in the format \[AREA-NNN\] and labelled with a MoSCoW priority:

**  MUST ** Required for POC to be considered successful     **  SHOULD ** Strongly desired; acceptable to defer to sprint 3--4 if blocked     **  COULD ** Nice to have; include if time permits     

Requirement areas: BR = Business Requirement  ·  FR = Functional  ·  DM = Data Model  ·  IN = Integration  ·  SC = Scheduling  ·  SE = Security & Compliance  ·  NF = Non-Functional  ·  AC = Acceptance Criteria

**POC SCOPE**

**In Scope**

- Question Repository: a curated, versioned store of persona-tagged questions to be submitted to LLMs

- LLM Response Agent: automated orchestration layer that dispatches questions to target LLMs, captures responses, and writes structured records to the Response Repository

- Response Repository: structured, queryable store of timestamped LLM responses with full metadata

- Prototype Sentiment Scoring: a single scoring pass using Claude to assess brand sentiment and competitive positioning for each captured response

- Prototype Dashboard: a static or lightweight web report surfacing findings to Medical Affairs and Commercial at the POC readout

- Scheduling: daily automated run of the full question bank against all configured LLMs

- Alerting (prototype): threshold-based flag on responses meeting predefined alert conditions

**Out of Scope for POC**

- Production-grade MLR / Medical Affairs workflow integration

- Real-time alerting pipeline (email/Slack/Teams notifications --- prototyped only)

- Full clinical accuracy scoring against a Medical Affairs reference library

- Integration with existing AbbVie data platforms (Veeva, Salesforce, data lake)

- Open Evidence API integration (included if access is confirmed; otherwise deferred to production)

- User authentication, role-based access control, or multi-tenant support

- Mobile or native application

**POC Boundaries --- What Success Looks Like**

The POC is successful when an automated run of 100 questions across 3--4 LLMs completes without manual intervention, responses are correctly stored and queryable, and a prototype dashboard shows baseline sentiment and competitive positioning data that Medical Affairs and Commercial confirm is actionable.

**BUSINESS REQUIREMENTS (BR)**

Business requirements define what the system must achieve in terms of organisational value, regardless of implementation approach.

**Visibility**

**\[BR-001\]  MUST ** The system must provide AbbVie with a record of what each monitored LLM says about AbbVie therapies in response to realistic patient, caregiver, and provider questions.

**\[BR-002\]  MUST ** The system must capture LLM responses across all three persona types --- Prospect, Provider, and Patient --- reflecting the distinct question styles and information needs of each audience.

**\[BR-003\]  MUST ** The system must record responses longitudinally so that changes in LLM behaviour over time --- as models are updated or retrained --- can be detected and investigated.

**\[BR-004\]  SHOULD ** The system should flag when a monitored LLM changes its response materially for a given question between runs, enabling Medical Affairs to assess whether the change is clinically or commercially significant.

**Intelligence**

**\[BR-005\]  MUST ** The system must produce a baseline assessment of how AbbVie therapies are represented relative to named competitors across all monitored LLMs at the end of the POC.

**\[BR-006\]  MUST ** The system must score each response for brand sentiment (positive / neutral / negative) and competitive positioning (first-line / second-line / not recommended).

**\[BR-007\]  SHOULD ** The system should identify responses that contain clinically inaccurate information --- incorrect dosing, outdated contraindications, or unapproved indications --- and flag them for Medical Affairs review.

**\[BR-008\]  COULD ** The system could generate a weekly intelligence digest summarising the most significant findings across all LLMs and personas for distribution to Commercial and Medical Affairs stakeholders.

**Governance and Compliance**

**\[BR-009\]  MUST ** All questions submitted to external LLMs must be reviewed and approved by Medical Affairs prior to submission to ensure they do not constitute off-label solicitation or promotional communication.

**\[BR-010\]  MUST ** The system must maintain an immutable audit log of all queries submitted, responses received, and scoring decisions made, sufficient to support a compliance review.

**\[BR-011\]  MUST ** The handling, storage, and processing of LLM responses must comply with AbbVie data governance policy and any applicable API terms of service for the target LLMs.

**\[BR-012\]  SHOULD ** The system should not store any personally identifiable information. Questions must be generic and not constructed around or seeded with real patient data.

**FUNCTIONAL REQUIREMENTS (FR)**

**FR-1 --- Question Repository**

**\[FR-101\]  MUST ** The system must maintain a Question Repository --- a structured, versioned store of questions to be submitted to monitored LLMs.

**\[FR-102\]  MUST ** Each question record must include: question_id, question_text, persona (Prospect \| Provider \| Patient), therapeutic_area, brand_focus, domain (Efficacy \| Safety \| Access \| Comparative \| General), active flag (boolean), and created/updated timestamps.

**\[FR-103\]  MUST ** The Question Repository must support adding, editing, deactivating, and versioning questions without deleting historical records.

**\[FR-104\]  MUST ** The system must support a minimum of 100 active questions at POC launch and be designed to scale to 1,000+ questions without architectural changes.

**\[FR-105\]  SHOULD ** Questions should be importable from CSV or Excel to support Medical Affairs curation workflow without requiring direct database access.

**\[FR-106\]  SHOULD ** The repository should support filtering questions by persona, therapeutic area, brand, domain, and active status for targeted run configurations.

**\[FR-107\]  COULD ** The system could surface a \'question coverage gap\' report identifying therapeutic areas, personas, or domains with fewer than a defined minimum number of active questions.

**FR-2 --- LLM Response Agent**

**\[FR-201\]  MUST ** The system must include an LLM Response Agent that automatically retrieves questions from the Question Repository and submits them to each configured LLM target.

**\[FR-202\]  MUST ** The agent must be orchestrated by Claude claude-opus-4-6 using the Anthropic API with adaptive thinking enabled. Claude acts as the master coordinator managing question dispatch, response validation, and error handling.

**\[FR-203\]  MUST ** The agent must support the following LLM targets at POC launch: OpenAI GPT-4o, Google Gemini 1.5 Pro, Anthropic Claude claude-opus-4-6 (queried as an end-user, not as orchestrator). Open Evidence to be added if API access is confirmed.

**\[FR-204\]  MUST ** For each question, the agent must submit the question to all configured LLM targets and store all responses before moving to the next question.

**\[FR-205\]  MUST ** The agent must submit each question with a consistent, LLM-specific system prompt that contextualises the query as a realistic user interaction (e.g., patient context for patient questions, clinical context for provider questions). System prompts must be reviewed by Medical Affairs.

**\[FR-206\]  MUST ** The agent must handle API failures gracefully: retry failed requests up to 3 times with exponential backoff (2s, 4s, 8s). After 3 failures, log the error, mark the response record as FAILED, and continue to the next question.

**\[FR-207\]  MUST ** The agent must respect rate limits for each LLM API. Rate limit configuration (requests per minute, tokens per minute) must be externalised in a config file, not hardcoded.

**\[FR-208\]  MUST ** The agent must log every query dispatched and every response received with a timestamp, LLM target, question ID, HTTP status code, and token count.

**\[FR-209\]  SHOULD ** The agent should support a dry-run mode that validates connectivity and configuration for all LLM targets without writing to the Response Repository.

**\[FR-210\]  SHOULD ** The agent should support running against a subset of questions (by persona, therapeutic area, or domain tag) to enable targeted monitoring runs outside the daily full-bank schedule.

**\[FR-211\]  SHOULD ** The agent should detect and flag responses that appear truncated (cut off mid-sentence or at token limit) and retry with adjusted max_tokens before storing.

**\[FR-212\]  COULD ** The agent could submit each question 3 times per LLM per run cycle and store all three responses, enabling non-determinism analysis and improving scoring confidence via majority scoring.

**FR-3 --- Response Repository**

**\[FR-301\]  MUST ** The system must maintain a Response Repository storing every LLM response as a structured, queryable record.

**\[FR-302\]  MUST ** Each response record must contain the following fields:

*↳  response_id (UUID) · run_id (batch identifier) · timestamp_utc · llm_name · llm_model_version · persona · question_id · question_text (denormalised) · therapeutic_area · brand_focus · domain · response_text (full, unedited) · response_tokens · finish_reason (stop \| length \| error) · status (SUCCESS \| FAILED \| TRUNCATED) · sentiment_score (nullable --- populated by scoring pass) · competitive_position (nullable) · alert_triggered (boolean) · created_at*

**\[FR-303\]  MUST ** The Response Repository must be queryable by any combination of: LLM, persona, therapeutic area, brand, domain, date range, sentiment score range, and alert status.

**\[FR-304\]  MUST ** Response records must be immutable once written. Updates to derived fields (sentiment_score, alert_triggered) must be tracked as a separate versioned scoring record linked to the original response_id.

**\[FR-305\]  MUST ** The repository must support export of query results to CSV and JSON for stakeholder review and external analysis.

**\[FR-306\]  SHOULD ** The repository should store a diff between the current and previous response for the same question/LLM pair, enabling change detection without requiring the consumer to compare raw text.

**\[FR-307\]  SHOULD ** The repository should expose a simple query API (Python function or REST endpoint) that accepts filter parameters and returns paginated results. No direct database access should be required for routine queries.

**FR-4 --- Sentiment & Competitive Scoring (Prototype)**

**\[FR-401\]  MUST ** The system must include a scoring pass that evaluates each stored response and populates the sentiment_score and competitive_position fields.

**\[FR-402\]  MUST ** Brand sentiment must be scored on a scale of −1.0 (strongly negative toward AbbVie therapy) to +1.0 (strongly positive), with 0.0 representing neutral. Scoring is performed by Claude claude-opus-4-6 with a structured output schema.

**\[FR-403\]  MUST ** Competitive positioning must be classified as one of: FIRST_LINE_RECOMMENDED \| AMONG_OPTIONS \| SECOND_LINE \| NOT_RECOMMENDED \| NOT_MENTIONED. Classification is based on explicit or implied treatment sequencing in the response text.

**\[FR-404\]  MUST ** The scoring prompt must instruct Claude to return a structured JSON object containing: sentiment_score (float), competitive_position (enum), brand_mentions (list of brand names detected), key_claims (list of up to 5 key claims about the therapy), and scoring_rationale (brief explanation of the score).

**\[FR-405\]  MUST ** Alert logic must flag a response when: sentiment_score \< −0.3, OR competitive_position is NOT_RECOMMENDED, OR brand_mentions contains a known competitor with a materially higher sentiment than the AbbVie therapy in the same response.

**\[FR-406\]  SHOULD ** Scoring should run automatically as a post-processing step within 5 minutes of each response being written to the repository.

**\[FR-407\]  SHOULD ** The system should support re-scoring historical responses when the scoring prompt is updated, with the new scores stored as a versioned scoring record.

**\[FR-408\]  COULD ** The system could include a human-review flag allowing Medical Affairs to manually override a score and record a rationale, without deleting the AI-generated score.

**FR-5 --- Scheduling**

**\[FR-501\]  MUST ** The system must support scheduled, unattended execution of a full question bank run against all configured LLMs on a daily cadence.

**\[FR-502\]  MUST ** Scheduled runs must be configurable via a cron expression or equivalent scheduler. The default schedule for POC is daily at 02:00 UTC.

**\[FR-503\]  MUST ** Each scheduled run must be assigned a unique run_id and logged with start time, end time, total questions attempted, total responses captured, failure count, and total tokens consumed.

**\[FR-504\]  MUST ** If a scheduled run fails mid-execution, it must be resumable from the last successfully processed question without re-submitting completed questions.

**\[FR-505\]  SHOULD ** The system should send a run completion summary (questions run, responses captured, failures, alerts triggered) to a configured notification endpoint (email or webhook) after each scheduled run.

**\[FR-506\]  SHOULD ** The scheduler should support ad-hoc / on-demand runs triggered via CLI command, in addition to the scheduled cadence.

**FR-6 --- Dashboard (Prototype)**

**\[FR-601\]  MUST ** The system must produce a prototype dashboard at the end of the POC that presents findings to Medical Affairs and Commercial stakeholders.

**\[FR-602\]  MUST ** The dashboard must display: (a) sentiment score distribution by LLM and therapy, (b) competitive positioning breakdown by LLM, (c) alert count and list of flagged responses, (d) response volume over time.

**\[FR-603\]  MUST ** The dashboard must be viewable without installing any software --- delivered as a self-contained HTML file or hosted on a shared internal URL.

**\[FR-604\]  SHOULD ** The dashboard should allow filtering by persona, therapeutic area, LLM, and date range.

**\[FR-605\]  SHOULD ** The dashboard should display the full response text for any record when a user clicks on it, alongside the AI-generated scoring rationale.

**\[FR-606\]  COULD ** The dashboard could include a side-by-side LLM comparison view showing how different models answered the same question, with sentiment scores displayed adjacently.

**DATA MODEL REQUIREMENTS (DM)**

**Entity Overview**

| **Entity** | **Description** | **Storage** | **Key Relationships** |
|----|----|----|----|
| **Question** | Curated question approved for LLM submission | questions table / JSON | 1:many → Response |
| **LLM Target** | Configured LLM (name, model version, API endpoint, params) | llm_targets table / config | 1:many → Response |
| **Run** | A single scheduled or ad-hoc execution batch | runs table | 1:many → Response |
| **Response** | Raw LLM response to a single question submission | responses table | many:1 → Question, Run, LLM |
| **Scoring Record** | AI-generated score for a response; versioned | scores table | many:1 → Response |
| **Alert** | Triggered alert record linked to a scoring record | alerts table | many:1 → Scoring Record |

 

**Data Retention**

**\[DM-001\]  MUST ** All response records must be retained for a minimum of 24 months to support longitudinal analysis and trend detection.

**\[DM-002\]  MUST ** Response text must be stored in full and unmodified. No truncation, summarisation, or transformation of the raw LLM response is permitted in the storage layer.

**\[DM-003\]  SHOULD ** The repository should implement a soft-delete pattern. Records are never physically deleted; they are marked inactive with a deleted_at timestamp and reason.

**INTEGRATION REQUIREMENTS (IN)**

**OpenAI API (GPT-4o)**

**\[IN-101\]  MUST ** The system must integrate with the OpenAI Chat Completions API using the gpt-4o model.

**\[IN-102\]  MUST ** Requests must use the messages array format with a system prompt and a single user message containing the question text.

**\[IN-103\]  MUST ** The model version must be pinned (e.g., gpt-4o-2024-11-20) and configurable via environment variable to enable controlled model version testing.

**\[IN-104\]  MUST ** Default parameters: temperature=0.3, max_tokens=1024. Both configurable per question domain via config file.

**\[IN-105\]  SHOULD ** The integration should capture and store the finish_reason, prompt_tokens, and completion_tokens from the API response for each call.

**Google Generative AI API (Gemini)**

**\[IN-201\]  MUST ** The system must integrate with the Google Generative AI API using the gemini-1.5-pro model.

**\[IN-202\]  MUST ** Requests must include a system instruction and a user content part containing the question text.

**\[IN-203\]  MUST ** Model version pinned and configurable. Default parameters: temperature=0.3, max_output_tokens=1024.

**\[IN-204\]  SHOULD ** The integration should handle Gemini safety blocks gracefully: if a response is blocked by safety filters, log the block reason, mark the response as BLOCKED (distinct from FAILED), and continue.

**Anthropic API (Claude --- end-user query)**

**\[IN-301\]  MUST ** The system must query Claude claude-opus-4-6 as a monitored LLM target (distinct from its role as orchestrator). The end-user query must use a separate API call with a non-orchestrator system prompt simulating a patient or provider context.

**\[IN-302\]  MUST ** The orchestrator and monitored-LLM API calls must be clearly distinguished in logging by a role field (ORCHESTRATOR \| TARGET).

**\[IN-303\]  MUST ** Default parameters for monitored Claude calls: thinking={type: adaptive}, max_tokens=1024, temperature not set (adaptive thinking manages this).

**Open Evidence API (conditional)**

**\[IN-401\]  SHOULD ** If API access to Open Evidence is confirmed before Sprint 2, the system should integrate with it for Provider persona questions.

**\[IN-402\]  SHOULD ** Open Evidence integration should follow the same interface contract as other LLM targets to ensure it is a configuration change, not a code change, to add or remove it.

**API Credential Management**

**\[IN-501\]  MUST ** All API keys and credentials must be stored as environment variables or in a secrets manager. No credentials may be hardcoded or committed to version control.

**\[IN-502\]  MUST ** The system must validate that all required credentials are present and reachable at startup. If any required credential is missing or invalid, the system must exit with a clear error before any LLM queries are submitted.

**SECURITY & COMPLIANCE REQUIREMENTS (SE)**

**\[SE-001\]  MUST ** No personally identifiable information (PII) or protected health information (PHI) may be included in any question submitted to an external LLM API.

**\[SE-002\]  MUST ** All questions must be reviewed and formally approved by Medical Affairs before being marked active in the Question Repository. An approval_status field (PENDING \| APPROVED \| REJECTED) and approver_name must be stored on each question record.

**\[SE-003\]  MUST ** The system must maintain a complete, append-only audit log of all API calls made to external LLMs, including timestamp, question_id, LLM target, and HTTP status. This log must not be modifiable by the application.

**\[SE-004\]  MUST ** LLM API responses must be stored in AbbVie-controlled infrastructure only. Responses must not be forwarded to third-party services without explicit data governance approval.

**\[SE-005\]  MUST ** The system must comply with the terms of service of each integrated LLM API. Legal must confirm automated querying is permitted under each agreement before the system goes live.

**\[SE-006\]  SHOULD ** Sensitive configuration (API keys, database credentials) must not appear in application logs. The logging layer must redact or mask any string matching known credential patterns.

**\[SE-007\]  SHOULD ** The codebase must not include any hardcoded references to AbbVie product names, competitor names, or therapeutic indications in the application logic. These must reside in the Question Repository and config files only, so the system is content-agnostic.

**NON-FUNCTIONAL REQUIREMENTS (NF)**

**Performance**

**\[NF-001\]  MUST ** A full daily run of 100 questions across 3 LLMs (300 API calls total) must complete within 4 hours when running at the default rate-limited cadence.

**\[NF-002\]  MUST ** The scoring pass must complete within 30 minutes of the response collection run finishing for the same question set.

**\[NF-003\]  SHOULD ** The system should support parallel execution of questions across LLM targets (i.e., submit question Q1 to GPT-4o, Gemini, and Claude concurrently rather than sequentially) to reduce total run time.

**Reliability**

**\[NF-004\]  MUST ** The system must achieve ≥95% successful response capture rate across a 7-day continuous run (i.e., no more than 5% of questions per run result in a FAILED status after retries).

**\[NF-005\]  MUST ** A scheduled run failure must not result in data loss for responses already captured. The run must be resumable from the point of failure.

**\[NF-006\]  SHOULD ** The system should recover automatically from transient API errors (timeouts, 5xx responses) without operator intervention.

**Observability**

**\[NF-007\]  MUST ** All application events (run start/stop, question dispatch, response received, error, retry, alert triggered) must be written to a structured log file in JSON format with timestamp, severity, and context fields.

**\[NF-008\]  MUST ** A run summary report must be generated after each run showing: run_id, start/end time, duration, question count, response count by status (SUCCESS / FAILED / TRUNCATED / BLOCKED), alert count, and total tokens consumed.

**\[NF-009\]  SHOULD ** The system should expose a simple health-check endpoint or CLI command that verifies connectivity to all configured LLM APIs and returns a status report.

**Maintainability**

**\[NF-010\]  MUST ** LLM targets must be configurable via a YAML or JSON config file. Adding a new LLM target must require only a config change and a new API adapter module --- no changes to the core agent logic.

**\[NF-011\]  MUST ** The codebase must include a README with setup instructions, environment variable definitions, and a guide for adding new LLM targets and question bank entries.

**\[NF-012\]  SHOULD ** Core agent logic, the scoring module, and the repository layer must be implemented as separate, independently testable modules with no circular dependencies.

**\[NF-013\]  SHOULD ** Unit tests must cover: question repository CRUD operations, LLM API adapter retry logic, response record schema validation, and scoring output parsing. Minimum 70% code coverage for these modules.

**Cost**

**\[NF-014\]  MUST ** Token consumption must be tracked per run and per LLM target. The run summary report must include estimated API cost based on current pricing for each LLM.

**\[NF-015\]  SHOULD ** The system should enforce a configurable max_tokens_per_run budget. If a run would exceed the budget, it must pause and notify the operator rather than proceed silently.

**ACCEPTANCE CRITERIA (AC)**

The following criteria must all be met for the POC to be signed off at the Sprint 4 readout. Each criterion is binary --- pass or fail.

| **\#** | **Criterion** | **Measurement Method** | **Pass Threshold** |
|----|----|----|----|
| **AC-01** | Agent runs unattended for 7 consecutive days without operator intervention | Review run logs for manual intervention events | **0 operator interventions in 7-day window** |
| **AC-02** | Question bank covers all three personas and at least two therapeutic areas | Count active questions by persona and TA in repository | **≥ 30 questions per persona; ≥ 2 TAs** |
| **AC-03** | All three public LLM targets return responses for ≥95% of submitted questions |  |  |
