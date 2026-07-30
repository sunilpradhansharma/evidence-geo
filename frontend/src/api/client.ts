import type { TaxonomyPayload } from "../lib/taxonomy";

const BASE = "/api";

// FR-108a: the exact mandated label that must appear on every disease-state / pre-launch
// dashboard and export. Do not alter the wording.
export const PRELAUNCH_LABEL =
  "Pre-Launch / Pipeline Intelligence - No AbbVie Brand Asset";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`GET ${path} failed: ${res.status}`);
  return res.json();
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`POST ${path} failed: ${res.status}`);
  return res.json();
}

async function put<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`PUT ${path} failed: ${res.status}`);
  return res.json();
}

async function patch<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`PATCH ${path} failed: ${res.status}`);
  return res.json();
}

async function del<T = void>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { method: "DELETE" });
  if (!res.ok && res.status !== 204) throw new Error(`DELETE ${path} failed: ${res.status}`);
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

/**
 * POST/PATCH that surfaces the server's `detail` on failure (FastAPI HTTPException),
 * so compliance messages like a PII block are shown verbatim to the reviewer.
 */
async function mutate<T>(method: "POST" | "PATCH", path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => undefined);
  if (!res.ok) {
    const detail = (data as any)?.detail;
    const msg =
      typeof detail === "string"
        ? detail
        : detail?.error
          ? `${detail.error}${detail.pii_flags ? `: ${detail.pii_flags.join(", ")}` : ""}`
          : `${method} ${path} failed: ${res.status}`;
    throw new Error(msg);
  }
  return data as T;
}

export interface Question {
  id: number;
  question_id: string;
  question_text: string;
  persona: string;
  therapeutic_area: string;
  disease?: string | null;
  brand_focus: string | null;
  monitoring_mode?: string;
  competitor_focus?: string[] | null;
  domain: string;
  intent_type: string | null;
  approval_status: string;
  active: boolean;
  priority_weight?: number;
  demand_origin?: string | null;
  variation_group_id?: string | null;
  variation_of?: string | null;
  is_variation?: boolean;
  generation_method?: string | null;
  // Computed lineage (bidirectional): source text for a variation; count of variations for an original.
  variation_of_text?: string | null;
  variation_count?: number;
  // Derived provenance bucket: MANUAL | PROMPT_VOLUME | DISCOVER | VARIATION
  source?: string | null;
  // Workshop designation (Persona + indication from Rhem.csv): "Patient RA" / "HCP PsA" / ... ; null otherwise.
  designation?: string | null;
  created_at?: string;
}

// FR-116 — bulk prompt importer (preview -> commit).
export interface PromptSkip { text: string; reason: string; }
// Step 1: dry-run preview from POST /questions/import-prompts/preview (nothing persisted).
export interface PromptPreviewResult {
  questions: string[];        // distinct, clean, not-already-in-bank — the ones we'd add
  duplicates: number;         // collapsed (repeated in file or already in the bank)
  skipped: PromptSkip[];      // dropped for PII / length, with reasons
  total_rows: number;
  persona: string;
  brand_focus: string;
  therapeutic_area: string;   // derived from the brand
  domain: string;
  demand_origin: string;      // PROMPT ("Real") | KEYWORD ("From keyword")
  prompt_column: string;
  detail?: string;
}
// Step 2: summary from POST /questions/import-prompts (after the analyst confirms).
export interface PromptImportResult {
  imported: number;
  duplicates: number;
  skipped: PromptSkip[];
  persona: string;
  brand_focus: string;
  therapeutic_area: string;
  demand_origin: string;
  detail?: string;
}

export interface ResponseItem {
  response_id: string;
  run_id: string;
  llm_name: string;
  persona: string;
  question_id: string;
  question_text: string;
  therapeutic_area: string;
  brand_focus: string | null;
  monitoring_mode?: string;
  competitor_focus?: string[] | null;
  domain: string;
  intent_type: string | null;
  consensus_level: string | null;
  response_text: string;
  status: string;
  sentiment_score: number | null;
  competitive_position: string | null;
  scoring_rationale: string | null;
  brand_mentions: any[];
  key_claims: string[];
  sources?: { url: string; title: string | null; domain?: string | null; redirect_url?: string | null; snippet?: string | null; origin?: string }[];
  grounding_supports?: { text: string; source_indices: number[]; start_index?: number | null; end_index?: number | null }[];
  search_queries?: string[];
  alert_triggered: boolean;
}

export interface Run {
  run_id: string;
  trigger: string;
  monitoring_mode?: string;
  status: string;
  started_at: string;
  ended_at: string | null;
  questions_attempted: number;
  responses_success: number;
  responses_failed: number;
  responses_truncated: number;
  responses_blocked: number;
  total_tokens: number;
  estimated_cost_usd: number;
  alerts_triggered: number;
  consensus_full: number;
  consensus_partial: number;
  consensus_missing: number;
  /** Why the run ended as it did: failure reason, budget pause, or resume marker. */
  notes?: string | null;
}

/** True while scripts/ec2_deploy.sh is staging a deploy that will replace the container. */
export interface DeployStatus {
  deploying: boolean;
}

export interface RunProgressModel {
  done: number;
  success: number;
  truncated: number;
  blocked: number;
  failed: number;
}

export interface RunProgress {
  run_id: string;
  status: string;
  questions_attempted: number;
  responses_done: number;
  by_model: Record<string, RunProgressModel>;
}

export interface Schedule {
  enabled: boolean;
  cron: string;
  timezone: string;
  next_run_at: string | null;
  last_run_at: string | null;
  last_run_id: string | null;
}

export interface HarvestedItem {
  id: number;
  source: string;
  source_url: string | null;
  source_domain: string | null;
  source_title: string | null;
  search_query: string | null;
  raw_excerpt: string | null;
  question_text: string;
  persona: string | null;
  therapeutic_area: string | null;
  brand_focus: string | null;
  domain: string | null;
  intent_type: string | null;
  relevance_score: number | null;
  search_persona: string | null;
  pii_flags: string[];
  ae_flag: boolean;
  status: string;
  promoted_question_id: string | null;
  review_note: string | null;
  harvested_at: string | null;
}

export interface HarvestRunResult {
  run_id: string | null;
  ran_count: number;
  promoted: { id: number; question_id: string; question_text: string }[];
  skipped: { id: number; question_text: string | null; reason: string }[];
}

// ----- Social Listening (Obesity demo — Apify) -----
export interface SocialPost {
  id: number;
  channel: string;
  source: string | null;
  post_url: string | null;
  source_domain: string | null;
  search_term: string | null;
  text: string;
  text_original: string | null;
  language: string | null;
  is_translated: boolean;
  brand_focus: string | null;
  therapeutic_area: string | null;
  domain: string | null;
  topic: string | null;
  sentiment: number | null;
  sentiment_label: string | null;
  engagement_score: number | null;
  engagement_metric: string;
  comment_count: number | null;
  comment_sentiment: number | null;
  comments_captured: number;
  brand_mentions: BrandMention[];
  patient_signals: PatientSignals | null;
  posted_at: string | null;
  ae_flag: boolean;
  pii_flags: string[];
  harvested_at: string | null;
}

export interface SocialComment {
  id: number;
  post_id: number;
  channel: string;
  text: string;
  text_original: string | null;
  language: string | null;
  is_translated: boolean;
  sentiment: number | null;
  sentiment_label: string | null;
  topic: string | null;
  engagement_score: number | null;
  engagement_metric: string;
  ae_flag: boolean;
  pii_flags: string[];
  posted_at: string | null;
  harvested_at: string | null;
}

export interface SovBrand { brand: string; posts: number; post_share: number }
export interface SovChannelBrand { brand: string; posts: number; post_share: number; engagement: number; engagement_share: number }
export interface SentRow { n: number; avg_sentiment: number | null; positive: number; neutral: number; negative: number; brand?: string; channel?: string }
export interface EngagementLeader {
  channel: string;
  metric: string;
  posts: { brand: string; topic: string | null; engagement: number | null; comment_count: number | null; sentiment: number | null; sentiment_label: string | null; snippet: string; post_url: string | null; posted_at: string | null }[];
}

export interface SocialVerbatim {
  quote: string;
  channel: string;
  brand: string | null;
  sentiment: number | null;
  sentiment_label: string | null;
  topic: string | null;
  ae_flag: boolean;
  kind: "post" | "comment";
  why: string | null;
}

export interface SocialAiBrief {
  narrative: string | null;
  verbatims: SocialVerbatim[];
  unmet_questions: UnmetQuestion[];
  posts_analyzed: number | null;
  model: string | null;
  updated_at: string | null;
}

// Per-platform "AbbVie vs each competitor brand" comparison (captured sample; posts only).
export interface PlatformSentStat {
  posts: number;
  post_share: number;
  avg_sentiment: number | null;
  positive: number;
  neutral: number;
  negative: number;
}
export interface PlatformCompetitor extends PlatformSentStat {
  brand: string;
  company: string | null;
}
export interface PlatformComparisonChannel {
  channel: string;
  metric: string;
  total_posts: number;
  attributed_posts: number;
  unattributed_posts: number;
  abbvie: PlatformSentStat & { brands: string[] };
  competitors: PlatformCompetitor[];
  gist: string | null;
}
export interface PlatformComparison {
  channels: PlatformComparisonChannel[];
  abbvie_present: boolean;
}

// ----- Patient Community Insights (myRAteam / Bezzy RA enrichment) -----
export interface BrandMention {
  name: string;
  generic: string | null;
  company: string | null;
  owner: string; // "AbbVie" | "Competitor"
  sentiment: number | null;
  context: string | null;
}
export interface PatientSignals {
  concerns: string[];
  journey_stage: string | null;
  switching_drivers: string[];
  qol_impacts: string[];
  access_barriers: string[];
  questions: string[];
}
export interface CommunityCount { label: string; count: number }
export interface CommunityDrugMention {
  name: string;
  company: string | null;
  owner: string; // "AbbVie" | "Competitor"
  mentions: number;
  mention_share: number;
  avg_sentiment: number | null;
}
export interface UnmetQuestion {
  question: string;
  theme: string | null;
  brand: string | null;
}
export interface CommunityInsights {
  posts: number;
  channels: string[];
  concerns: CommunityCount[];
  journey_stages: CommunityCount[];
  switching_drivers: CommunityCount[];
  qol_impacts: CommunityCount[];
  access_barriers: CommunityCount[];
  drug_mentions: CommunityDrugMention[];
  drug_sov: {
    total_mentions: number;
    abbvie_mentions: number;
    competitor_mentions: number;
    abbvie_share: number;
    abbvie_present: boolean;
  };
  unmet_questions: UnmetQuestion[];
}

export interface SocialInsights {
  therapeutic_area: string;
  basis: string;
  ai_brief: SocialAiBrief | null;
  platform_comparison: PlatformComparison;
  community_insights: CommunityInsights | null;
  total_posts: number;
  channels: string[];
  channel_metrics: Record<string, string>;
  share_of_voice: {
    by_brand: SovBrand[];
    by_channel: { channel: string; metric: string; posts: number; engagement_total: number; brands: SovChannelBrand[] }[];
  };
  sentiment_by_brand: SentRow[];
  sentiment_by_channel: SentRow[];
  sentiment_overall: { n: number; scored: number; avg_sentiment: number | null; positive: number; neutral: number; negative: number };
  comment_sentiment_overall: { n: number; scored: number; avg_sentiment: number | null; positive: number; neutral: number; negative: number };
  comment_sentiment_by_channel: SentRow[];
  total_comments: number;
  volume_over_time: { channels: string[]; rows: Record<string, any>[] };
  window: { as_of: string; recent_days: number; recent_posts: number; prior_posts: number; delta_pct: number | null } | null;
  top_topics: { topic: string; count: number; avg_sentiment: number | null }[];
  adverse_events: { total: number; posts: number; comments: number; rate: number; by_brand: { brand: string; count: number }[]; by_channel: { channel: string; count: number }[] };
  engagement_leaders: EngagementLeader[];
}

export interface OERunSummary {
  run_id: string;
  trigger: string;
  status: string;
  started_at: string;
  provider_questions: number;
  captured: number;
  pending: number;
}

export interface OEWorkItem {
  question_id: string;
  question_text: string;
  brand_focus: string;
  therapeutic_area: string;
  domain: string;
  intent_type: string | null;
  captured: boolean;
  response_id: string | null;
  status: string | null;
  scored: boolean;
  sentiment_score: number | null;
  competitive_position: string | null;
}

export interface OEWorklist {
  run_id: string;
  status: string;
  provider_questions: number;
  captured: number;
  pending: number;
  items: OEWorkItem[];
}

export interface OECaptureBody {
  run_id: string;
  question_id: string;
  answer_text: string;
  model_version?: string;
  sources?: { url: string; title?: string | null }[];
}

export interface SnowflakeSyncState {
  TABLE_NAME: string;
  LAST_WATERMARK: string | null;
  ROWS_SYNCED: number | null;
  UPDATED_AT: string | null;
}

export interface SnowflakeStatus {
  enabled: boolean;
  connected: boolean;
  detail?: string;
  account?: string;
  warehouse?: string;
  database?: string;
  schema?: string;
  role?: string;
  sync_state?: SnowflakeSyncState[];
}

export interface CortexInsights {
  enabled: boolean;
  model?: string;
  llm_available?: boolean;
  sentiment_by_brand?: { BRAND: string; SCORED: number; AVG_SENTIMENT: number; MIN_SENTIMENT: number; MAX_SENTIMENT: number }[];
  sentiment_trend?: { DAY: string; BRAND: string; AVG_SENTIMENT: number; N: number }[];
  positioning_by_brand?: { BRAND: string; POSITION: string; N: number }[];
  executive_summary?: string;
}

export interface CortexAnswer {
  enabled: boolean;
  question?: string;
  generated_sql?: string;
  columns: string[];
  rows: Record<string, any>[];
  answer: string;
  error?: string;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export interface CortexChatReply {
  enabled: boolean;
  answer: string;
  suggestions?: string[];
  error?: string;
}

// ----- Copilot agent (application-wide assistant) -----
export interface CopilotToolCall {
  tool_name: string;
  elapsed_ms: number;
  ok: boolean;
  summary: string;
}

export interface CopilotPendingField {
  key: string;
  label: string;
  value: string;
  editable: boolean;
  type: "text" | "select" | "number" | "boolean";
  options: string[];
  allow_empty: boolean;
  raw: any;
}

export interface CopilotPreset {
  label: string;
  description?: string;
  args: Record<string, any>;
}

export interface CopilotPendingAction {
  token: string;
  tool_name: string;
  args: Record<string, any>;
  summary: string;
  issued_at: number;
  trace_id: string;
  governance: boolean;
  nav_target: string | null;
  fields?: CopilotPendingField[];
  presets?: CopilotPreset[];
}

export interface CopilotUiAction {
  target: string;
  to?: string;
  [k: string]: any;
}

export interface CopilotPromptOption {
  value: string;
  label: string;
  hint?: string | null;
}

export interface CopilotPromptOptions {
  prompt: string;
  param?: string | null;
  options: CopilotPromptOption[];
  send_template?: string | null;
}

export interface CopilotMessage {
  role: "user" | "assistant" | "tool";
  content: string;
  tool_name?: string | null;
}

export interface CopilotAgentResponse {
  trace_id: string;
  intent: string;
  messages: CopilotMessage[];
  tool_calls: CopilotToolCall[];
  ui_action: CopilotUiAction | null;
  pending_action: CopilotPendingAction | null;
  prompt_options?: CopilotPromptOptions | null;
  guardrail_flags: string[];
  refusal_card: any | null;
}

export interface CopilotHealth {
  status: "ok" | "unavailable";
  provider?: string | null;
  model_id?: string | null;
  error?: string | null;
}

export interface CopilotJob {
  kind: string;
  run_id?: string;
}

export interface CopilotConfirmResult {
  ok: boolean;
  summary: string;
  data: Record<string, any>;
  error?: string | null;
  ui_action?: CopilotUiAction | null;
  job?: CopilotJob | null;
}

export interface CopilotJobStatus {
  kind: string;
  status: "running" | "done" | "unknown";
  ok: boolean;
  summary: string;
}

export interface CopilotChatBody {
  message: string;
  history?: ChatMessage[];
  ui_context?: Record<string, any>;
  trace_id?: string;
}

export interface CopilotConfirmBody {
  token: string;
  tool_name: string;
  args: Record<string, any>;
  trace_id: string;
  issued_at: number;
  actor?: string;
}

export interface CopilotPreviewBody {
  tool_name: string;
  args: Record<string, any>;
  trace_id: string;
  base_token: string;
  base_args: Record<string, any>;
  base_issued_at: number;
}

export type CopilotStreamEvent =
  | { event: "start"; data: { trace_id: string } }
  | { event: "status"; data: { node?: string; intent?: string } }
  | { event: "tool"; data: CopilotToolCall }
  | { event: "ui_action"; data: CopilotUiAction }
  | { event: "pending"; data: CopilotPendingAction }
  | { event: "done"; data: CopilotAgentResponse }
  | { event: "error"; data: { code?: string; error?: string; trace_id?: string } };

// ----- Model Release Event Correlation (FR-707a) -----
export type ModelReleaseSource = "api" | "changelog" | "inferred" | "auto" | "seed" | "manual";

export interface ModelRelease {
  id: number;
  target_platform: string;
  release_date: string;
  version: string | null;
  release_notes: string | null;
  url: string | null;
  source: ModelReleaseSource;
  event_type: string;
  summary: string | null;
  effective_date: string | null;
  first_seen_at: string | null;
  confidence: number | null;
  created_at: string;
}

export interface LiveVersion {
  target_platform: string;
  current_version: string | null;
  current_since: string | null;
  last_seen_at: string | null;
  versions_observed: number;
  total_responses: number;
}

export interface VersionImpact {
  release_id: number;
  target_platform: string;
  version: string | null;
  release_date: string;
  effective_date: string | null;
  source: ModelReleaseSource;
  event_type: string;
  summary: string | null;
  confidence: number | null;
  url: string | null;
  questions_changed: number;
  drift_count: number;
  sentiment_before: number | null;
  sentiment_after: number | null;
  sentiment_delta: number | null;
  position_changes: number;
  is_high_impact: boolean;
}

export interface ModelUpdateSyncStatus {
  enabled: boolean;
  sync_flag: boolean;
  scheduler_enabled: boolean;
  sync_hour_utc: number;
  sources: { vendor: string; platforms: string[]; url: string; fmt: string }[];
}

export interface ModelUpdateSyncResult {
  versions_observed: number;
  version_transitions_created: number;
  changelog_sync_enabled: boolean;
  vendors_synced: number;
  changelog_events_created: number;
  changelog_events_enriched: number;
  vendor_errors: string[];
  drifts_linked: number;
}

export interface DriftTimeline {
  drifts: { date: string; material_drifts: number; correlated_drifts: number }[];
  releases: { id: number; date: string; target_platform: string; version: string | null; url: string | null }[];
}

export interface CorrelationRatio {
  material_drifts: number;
  correlated_drifts: number;
  unexplained_drifts: number;
  correlation_ratio: number;
}

export interface ResponseDriftItem {
  id: number;
  question_id: string;
  question_text: string | null;
  llm_name: string;
  observed_date: string | null;
  similarity_ratio: number | null;
  correlated_release_id: number | null;
  correlated_release_platform: string | null;
  correlated_release_date: string | null;
  previous_snippet: string | null;
  current_snippet: string | null;
}

export interface ResponseDriftDetail {
  id: number;
  question_id: string;
  question_text: string | null;
  llm_name: string;
  observed_date: string | null;
  similarity_ratio: number | null;
  material_change: boolean;
  diff_text: string | null;
  previous_response_id: string | null;
  previous_response_text: string | null;
  current_response_id: string | null;
  current_response_text: string | null;
  correlated_release_id: number | null;
  correlated_release_platform: string | null;
  correlated_release_date: string | null;
  correlated_release_notes: string | null;
}

// ----- Stakeholder Digests (BR-008a) -----
export interface DigestRule {
  id?: number;
  alert_categories?: string[] | null;
  domains?: string[] | null;
  therapeutic_areas?: string[] | null;
  personas?: string[] | null;
  llm_names?: string[] | null;
}

export interface DigestProfile {
  id: number;
  role: string;
  description: string | null;
  enabled: boolean;
  cron: string;
  timezone: string;
  recipients: string[] | null;
  delivery_methods: string[] | null;
  webhook_url: string | null;
  rules: DigestRule[];
  created_at: string;
  updated_at: string;
  next_run_at: string | null;
}

export interface DigestProfileCreate {
  role: string;
  description?: string | null;
  enabled?: boolean;
  cron?: string;
  timezone?: string;
  recipients?: string[] | null;
  delivery_methods?: string[] | null;
  webhook_url?: string | null;
  rules?: DigestRule[];
}

export interface DigestRun {
  id: number;
  profile_id: number;
  role: string;
  generated_at: string;
  period_start: string | null;
  period_end: string | null;
  findings_count: number;
  findings: any[] | null;
  summary: string | null;
  delivered_email: boolean;
  delivered_webhook: boolean;
  delivery_detail: Record<string, string> | null;
}

export interface SesStatus {
  enabled: boolean;
  sender: string | null;
  region: string | null;
  sender_verified: boolean | null;
  sender_domain_verified: boolean | null;
  sandbox: boolean | null;
  mode: "production" | "sandbox" | "unknown";
  verified_identities: string[];
  reason: string | null;
  note: string | null;
}

// Workshop Questions insights (BR-008a) — the same 'current standing' snapshot the
// stakeholder digest renders, surfaced live in-app.
export interface WorkshopNeedsAttention {
  platform: string;
  designation: string | null;
  question: string;
  competitive_position: string | null;
  sentiment_score: number | null;
  summary: string | null;
}
export interface WorkshopModelSourceDomain {
  authority_domain: string;
  publisher_name: string | null;
  control_type: string;
  citation_count: number;
  url: string | null;
}
export interface WorkshopModelSources {
  total_citations: number;
  abbvie: number;
  competitor: number;
  independent: number;
  domains: WorkshopModelSourceDomain[];
}
export interface WorkshopModel {
  llm: string;
  responses: number;
  avg_sentiment: number | null;
  favorable: number;
  weak: number;
  summary: string | null;          // LLM 'general summary' of what this platform is saying
  summary_stale: boolean;          // cached summary present but its inputs changed
  answered_from_knowledge: boolean; // parametric answers, no web sources cited
  sources: WorkshopModelSources | null;
}
export interface WorkshopDesignation {
  designation: string;
  responses: number;
  avg_sentiment: number | null;
  favorable: number;
  weak: number;
}
export interface WorkshopCompetitor {
  authority_domain: string;
  publisher_name: string | null;
  citation_count: number;
}
export interface WorkshopPage {
  url: string;
  publisher_name: string | null;
  citation_count: number;
}
export interface WorkshopCitations {
  total_citations: number;
  abbvie_share_pct: number;
  competitor_share_pct: number;
  independent_share_pct: number;
  top_competitors: WorkshopCompetitor[];
  top_competitor_pages: WorkshopPage[];
}
export interface WorkshopInsights {
  questions_covered: number;
  responses: number;
  scored: number;
  models: string[];
  latest_at: string | null;
  avg_sentiment: number | null;
  favorable_pct: number;
  weak_pct: number;
  positioning: Record<string, number>;
  by_designation: WorkshopDesignation[];
  by_model: WorkshopModel[];
  citations: WorkshopCitations | null;
  needs_attention: WorkshopNeedsAttention[];
  needs_attention_count: number;
  abbvie_cited: boolean;
  needs_summary_refresh: boolean;  // a background LLM summary refresh was requested
}
export interface WorkshopInsightsResponse {
  available: boolean;
  insights: WorkshopInsights | null;
  scope?: "workshop" | "all";
}

// The taxonomy the UI renders its pickers from. Defined in lib/taxonomy.ts alongside the
// store it hydrates, so the wire shape and the thing it populates cannot drift apart.
export type { TaxonomyPayload } from "../lib/taxonomy";

export interface TaxonomyStatus {
  therapeutic_areas: number;
  indications: number;
  drug_catalog: number;
  curated_drugs: number;
  full_depth_drugs: string[];
  draft_diseases: string[];
  /** Recomputed live, so this is "wrong now", not "was wrong at boot". */
  errors: string[];
  errors_at_startup: string[];
}

export interface TaxonomyAreaChoice {
  ta_key: string;
  area: string;
  diseases: string[];
}

/**
 * What a typed brand name turned out to be.
 *
 * `exact_match` means it is already curated — the analyst should edit that entry, not create
 * a second one. `near_matches` is a deterministic did-you-mean, so a typo of a known brand is
 * caught by string comparison rather than by model judgement.
 */
export interface BrandResolveResult {
  status: "exact_match" | "near_matches" | "novel" | "invalid";
  typed?: string;
  reason?: string;
  canonical?: string;
  /** True when the typed text was an alias rather than the brand's own name. */
  matched_alias?: boolean;
  company?: string | null;
  areas?: string[];
  near_matches?: { name: string; company: string | null; areas: string[] }[];
  /**
   * Model spelling verdict, present only on `novel`.
   *
   * Distinct from `near_matches`, which means "already curated under a similar name". This
   * means "probably a misspelling of a real drug the taxonomy does not carry" — the case
   * string comparison cannot see, because there is nothing to compare against.
   */
  spelling?: SpellingVerdict;
}

export interface SpellingVerdict {
  /** False when the model was unavailable — the flow continues regardless. */
  checked: boolean;
  verdict?: "correct" | "misspelling" | "unknown";
  corrected?: string;
  generic?: string | null;
  company?: string | null;
  note?: string;
  reason?: string;
}

/** Model-drafted identity. Every field is editable; `available: false` means draft as blank. */
export interface BrandDraft {
  available: boolean;
  reason?: string;
  known?: boolean;
  generic?: string | null;
  company?: string | null;
  drug_class?: string | null;
  administration_route?: string | null;
  aliases?: string[];
}

export interface CompetitorSuggestion {
  name: string;
  generic: string | null;
  company: string | null;
  /** Shown beside the tickbox and saved as the membership note if accepted. */
  reason: string;
  already_curated: boolean;
}

export interface OutcomeDraft {
  available: boolean;
  reason?: string;
  canonical_outcomes?: string[];
  verification_status?: string;
}

export interface BrandCreate {
  name: string;
  therapeutic_area_key: string;
  /**
   * A therapeutic area to create as part of this addition.
   *
   * Only accepted together with the brand being filed into it — an area created on its own
   * would appear in every therapeutic-area filter with nothing behind it. `ta_key` must equal
   * `therapeutic_area_key`, or the write is refused as self-contradictory.
   */
  new_therapeutic_area?: { ta_key: string; area: string } | null;
  diseases: {
    disease: string;
    competitors: { name: string; note?: string | null }[];
    area?: string | null;
    therapeutic_area_key?: string | null;
    canonical_outcomes?: string[];
  }[];
  generic?: string | null;
  company?: string | null;
  drug_class?: string | null;
  administration_route?: string | null;
  aliases?: string[];
  reviewer?: string;
}

export interface BrandCreated {
  brand: string;
  therapeutic_area_key: string;
  diseases: string[];
  /** Indications saved with model-drafted endpoints, fenced out of the evidence programme. */
  draft_indications: string[];
}

/** A refused write, carrying the backend's reasons so the modal can show them. */
export class BrandRejectedError extends Error {
  reasons: string[];
  constructor(reasons: string[]) {
    super(reasons.join(" "));
    this.name = "BrandRejectedError";
    this.reasons = reasons;
  }
}

/**
 * Create a brand, preserving the rejection reasons on a 400.
 *
 * Its own function rather than the shared `post`, which discards the response body on
 * failure. Here the body IS the point: a refusal says the alias collides with Rinvoq, or the
 * endpoint is undefined, and dropping that would reduce a specific, fixable problem to
 * "failed: 400".
 */
async function postBrand(body: BrandCreate): Promise<BrandCreated> {
  const res = await fetch(`${BASE}/taxonomy/brands`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.ok) return res.json();

  let reasons: string[] = [];
  try {
    const detail = (await res.json())?.detail;
    reasons = Array.isArray(detail?.errors) ? detail.errors : [];
  } catch {
    // Non-JSON error body (a proxy or gateway failure) — fall through to the generic message.
  }
  throw new BrandRejectedError(
    reasons.length ? reasons : [`Could not add the brand (HTTP ${res.status}).`]
  );
}

export interface BrandMatrixBrand {
  brand: string;
  company?: string | null;
}

export interface BrandMatrixRow {
  area: string;
  indication: string;
  diseases: string[];
  focus_brands: BrandMatrixBrand[];
  competitors: BrandMatrixBrand[];
}

export interface BrandMatrix {
  rows: BrandMatrixRow[];
}

// FR-108a: disease-state / pre-launch multi-competitor landscape matrix.
export interface LandscapeEntry {
  brand: string;
  is_competitor: boolean;
  mentions: number;
  share_of_voice: number;
  avg_sentiment: number | null;
  positions: Record<string, number>;
  dominant_position: string;
}
export interface LandscapeMatrix {
  pre_launch_notice: string;
  responses_analyzed: number;
  questions: number;
  llms: string[];
  matrix: LandscapeEntry[];
}

export interface TaFilters {
  therapeutic_area?: string;
  indication?: string;
  disease?: string;
  brand?: string;
  persona?: string;
}

function _taQs(filters?: TaFilters, prefixAmp = false): string {
  if (!filters) return "";
  const params: string[] = [];
  if (filters.therapeutic_area) params.push(`therapeutic_area=${encodeURIComponent(filters.therapeutic_area)}`);
  if (filters.indication) params.push(`indication=${encodeURIComponent(filters.indication)}`);
  if (filters.disease) params.push(`disease=${encodeURIComponent(filters.disease)}`);
  if (filters.brand) params.push(`brand=${encodeURIComponent(filters.brand)}`);
  if (filters.persona) params.push(`persona=${encodeURIComponent(filters.persona)}`);
  if (!params.length) return "";
  return (prefixAmp ? "&" : "?") + params.join("&");
}

// ----- GEO Intervention Recommendations (BR-012) -----
// "Where to publish / earn a citation" guidance derived from the classified citation graph.
export interface PlacementDomain {
  domain: string | null;
  authority_type: string | null;
  display_category: string | null;
  is_preferred: boolean;
  response_count: number | null;
  opportunity_score: number | null;
}
export interface PlacementPreferredGap {
  domain: string | null;
  absence_pct: number | null;
  absent: number | null;
}
export interface PlacementQuery {
  query: string;
  count: number;
}
export interface PlacementGuidance {
  scope?: { persona: string | null; therapeutic_area: string | null; brand: string | null };
  earn_citations: PlacementDomain[];
  preferred_gaps: PlacementPreferredGap[];
  target_queries: PlacementQuery[];
}

export interface Recommendation {
  rec_id: string;
  batch_id: string;
  created_at: string | null;
  source_response_id: string | null;
  question_id: string | null;
  run_id: string | null;
  persona: string | null;
  therapeutic_area: string | null;
  indication: string | null;
  brand_focus: string | null;
  llm_name: string | null;
  competitive_position: string;
  gap_severity: number;
  outperforming_competitor: string | null;
  competitor_domain: string | null;
  missing_citations: string[];
  search_volume: number | null;
  domain_authority: number | null;
  metrics_source: string;
  volume_multiplier: number;
  citation_gap_score: number;
  citation_multiplier: number;
  content_type: string;
  recommended_action: string;
  rationale: string | null;
  content_brief: string[];
  suggested_questions: string[];
  impact_score: number;
  mlr_status: string;
  placement?: PlacementGuidance | null;
  /* --- Phase 9 --------------------------------------------------------------------- */
  /** Which finder produced this row. POSITIONING_GAP asks how the answer reads;
   *  EVIDENCE_GAP asks whether it is right. The remedies are not interchangeable. */
  source_type: string;
  /** Derived from the governance state of the evidence behind the finding, never from a
   *  model's self-report. Null on positioning rows, which rest on a score. */
  confidence: number | null;
  strategic_implication: string | null;
  implication_owner: string | null;
  /** False when no publishable asset can close the finding. The UI must not offer
   *  "create intervention" on these — the work belongs to a curator or to clinical dev. */
  externally_actionable: boolean;
  evidence_action: string | null;
  claim_id: string | null;
  claim_text: string | null;
  classification: string | null;
  certainty_verdict: string | null;
  finding_reason: string | null;
  gap_attribution: string | null;
}

export interface StrategicImplicationMeta {
  implication: string;
  owner: string;
  severity: number;
  externally_actionable: boolean;
}

/** A finding the engine refused to answer with content.
 *
 *  Returned by generate rather than dropped: "3 comparisons are blocked by our own
 *  verification backlog" is work somebody owns, and a shorter recommendation list would
 *  hide it entirely.
 */
export interface InternalOnlyItem {
  claim_id: string | null;
  claim_text: string | null;
  strategic_implication: string | null;
  owner: string | null;
  evidence_action: string | null;
  reason: string | null;
}

export interface GenerateRecommendationsResult {
  batch_id: string;
  gaps_found: number;
  evidence_gaps_found: number;
  generated: number;
  rec_ids: string[];
  semrush_source: string;
  semrush_live: number;
  internal_only: InternalOnlyItem[];
  internal_only_count: number;
}

export interface RecommendationBatch {
  batch_id: string | null;
  count: number;
  generated_at: string | null;
  items: Recommendation[];
}

export interface RecFilters {
  persona?: string;
  therapeutic_area?: string;
  indication?: string;
  brand?: string;
  llm_name?: string;
  batch_id?: string;
  source_type?: string;
  strategic_implication?: string;
}

function _recQs(f?: RecFilters): string {
  if (!f) return "";
  const params: string[] = [];
  if (f.persona) params.push(`persona=${encodeURIComponent(f.persona)}`);
  if (f.therapeutic_area) params.push(`therapeutic_area=${encodeURIComponent(f.therapeutic_area)}`);
  if (f.indication) params.push(`indication=${encodeURIComponent(f.indication)}`);
  if (f.brand) params.push(`brand=${encodeURIComponent(f.brand)}`);
  if (f.llm_name) params.push(`llm_name=${encodeURIComponent(f.llm_name)}`);
  if (f.batch_id) params.push(`batch_id=${encodeURIComponent(f.batch_id)}`);
  if (f.source_type) params.push(`source_type=${encodeURIComponent(f.source_type)}`);
  if (f.strategic_implication)
    params.push(`strategic_implication=${encodeURIComponent(f.strategic_implication)}`);
  if (!params.length) return "";
  return "?" + params.join("&");
}

// Citation-gap analytics fused with the classified Source Authority graph (BR-005 / FR-706a).
export interface CitationOpportunity {
  domain: string;
  control_type: string;
  authority_type: string;
  display_category: string;
  verification: string;
  publisher_name: string | null;
  citation_count: number;
  response_count: number;
  weak_position_count: number;
  opportunity_score: number;
  is_preferred: boolean;
  brands: string[];
}

export interface CitationOpportunityResult {
  count: number;
  responses_with_citations: number;
  items: CitationOpportunity[];
}

// Share of AI citations by source control (same number as /source-authority/share-of-voice).
export interface CitationVoice {
  control_type: string;
  label: string;
  citation_count: number;
  response_count: number;
  share_pct: number;
}

export interface CompetitorCitation {
  authority_domain: string;
  publisher_name: string | null;
  citation_count: number;
  response_count: number;
  share_pct: number;
}

export interface ShareOfCitation {
  total_citations: number;
  response_count: number;
  voice: CitationVoice[];
  abbvie_share_pct: number;
  competitor_share_pct: number;
  independent_share_pct: number;
  competitor_total_citations: number;
  competitors: CompetitorCitation[];
}

// Medical-Affairs preferred domains AI omits (FR-706a.7).
export interface PreferredSourceGap {
  authority_domain: string;
  therapeutic_area: string;
  note: string | null;
  observations: number;
  present: number;
  absent: number;
  absence_pct: number | null;
}

export interface PreferredSourceGapResult {
  count: number;
  configured: number;
  items: PreferredSourceGap[];
}

// Query fanouts — the real search terms grounded models ran.
export interface QueryFanout {
  query: string;
  count: number;
  response_count: number;
  brands: string[];
}

export interface QueryFanoutResult {
  count: number;
  responses_with_queries: number;
  items: QueryFanout[];
}

// Citation share over time (day buckets).
export interface CitationTrendPoint {
  period: string;
  total: number;
  abbvie: number;
  competitor: number;
  independent: number;
  unknown: number;
  abbvie_share_pct: number;
  competitor_share_pct: number;
  independent_share_pct: number;
  unknown_share_pct: number;
}

export interface CitationTrend {
  granularity: string;
  periods: CitationTrendPoint[];
}

// Persisted recommendation triage (BR-010).
export type ReviewStatus = "NEW" | "REVIEWING" | "ACTIONED" | "DISMISSED";
export interface RecommendationReview {
  rec_id: string;
  status: ReviewStatus;
  owner: string | null;
  note: string | null;
  updated_by: string | null;
  updated_at: string | null;
}
export interface RecommendationReviewResult {
  count: number;
  items: RecommendationReview[];
}

// ---------- Prompt Volume Intelligence (FR-116) ----------
export interface PromptVolumeBatch {
  batch_id: string;
  source_tool: string;
  source_label: string;
  dataset_date: string;
  metric_type: string;
  filename: string | null;
  rows_total: number;
  rows_ingested: number;
  rows_rejected: number;
  gap_topics_flagged: number;
  created_at: string | null;
}
export interface PromptVolumeBatchList { count: number; batches: PromptVolumeBatch[]; }
export interface PromptVolumeTaVolume {
  therapeutic_area: string; volume: number; query_count: number;
  share_pct?: number; avg_difficulty?: number | null;
}
export interface PromptVolumeCompetitorVolume { competitor: string; volume: number; share_pct?: number; }
export interface PromptVolumeAreaShare {
  therapeutic_area: string; brand_volume: number; competitor_volume: number;
  brand_share_pct: number; competitor_share_pct: number;
}
export interface PromptVolumeShareOfDemand {
  brand_volume: number; competitor_volume: number; category_volume: number;
  brand_share_pct: number; competitor_share_pct: number; by_area: PromptVolumeAreaShare[];
}
export interface PromptVolumePersonaVolume { persona: string; volume: number; query_count: number; }
export interface PromptVolumeIntelligence {
  batch_id: string | null;
  batch: PromptVolumeBatch | null;
  metric_type?: string | null;
  total_volume: number;
  unmapped_volume: number;
  raw_row_count?: number;
  distinct_query_count?: number;
  prompt_backed_count?: number;
  by_therapeutic_area: PromptVolumeTaVolume[];
  by_competitor: PromptVolumeCompetitorVolume[];
  share_of_demand?: PromptVolumeShareOfDemand;
  by_persona?: PromptVolumePersonaVolume[];
}
export interface PromptVolumeGapTopic {
  label: string;
  question?: string;
  question_origin?: "prompt" | "synthesized" | "keyword";
  brand?: string | null;
  therapeutic_area: string;
  competitor: string | null;
  query_count: number;
  queries: string[];
  combined_volume: number;
  avg_difficulty?: number | null;
  avg_cpc?: number | null;
  opportunity_score?: number;
}
export interface PromptVolumeGaps { batch_id: string | null; count: number; topics: PromptVolumeGapTopic[]; }
export interface PromptVolumeGapAlert {
  alert_id: string;
  topic_key: string;
  label: string;
  question?: string | null;
  therapeutic_area: string | null;
  competitor: string | null;
  status: string;
  combined_volume: number;
  opportunity_score: number;
  query_count: number;
  first_seen_batch_id: string;
  first_seen_at: string | null;
  last_seen_batch_id: string;
  last_seen_at: string | null;
  resolved_at: string | null;
  resolved_reason: string | null;
  is_new: boolean;
}
export interface PromptVolumeGapAlerts { count: number; status: string; alerts: PromptVolumeGapAlert[]; }
export interface PromptVolumeGapAlertSummary { open: number; resolved: number; dismissed: number; }
export interface PromptVolumeTrendPoint {
  batch_id: string; dataset_date: string; source_label: string; source_tool: string;
  total_volume: number; brand_volume: number; competitor_volume: number; category_volume: number;
  areas: Record<string, number>; competitors: Record<string, number>;
}
export interface PromptVolumeEmergingTopic {
  query_text: string; therapeutic_area: string; competitor: string | null;
  previous_volume: number; current_volume: number; delta: number;
  pct_change: number | null; is_new: boolean;
}
export interface PromptVolumeEmerging {
  current_label: string; current_date: string; previous_label: string; previous_date: string;
  topics: PromptVolumeEmergingTopic[];
}
export interface PromptVolumeTrend {
  count: number;
  series: PromptVolumeTrendPoint[];
  top_areas: string[];
  top_competitors: string[];
  emerging: PromptVolumeEmerging | null;
}
export interface PromptVolumeUploadResult { ok: boolean; status: number; data: any; }
// In-app SEMrush fetch (FR-116): pull questions + related keywords straight from the API.
export interface SemrushStatus {
  configured: boolean;
  database: string;
  per_seed_limit: number;
  max_seeds: number;
  reports: string;
}
export interface SemrushPreviewRow {
  query_text: string;
  prompt_text?: string | null;
  search_volume: number;
  cpc?: number | null;
  report: "questions" | "related";
  therapeutic_area?: string | null;
  competitor?: string | null;
  brand?: string | null;
}
export interface SemrushNovelty {
  new_count: number;
  seen_in_last_count: number;
  covered_count: number;
  novel_volume: number;
}
export interface SemrushPreview {
  fetch_id: string | null;
  therapeutic_area: string;
  brand?: string | null;
  seeds_queried: number;
  lines_returned: number;
  distinct_query_count: number;
  total_volume: number;
  novelty: SemrushNovelty;
  by_therapeutic_area: { therapeutic_area: string; volume: number }[];
  by_competitor: { competitor: string; volume: number }[];
  gap_topics: PromptVolumeGapTopic[];
  sample: SemrushPreviewRow[];
  reports: string[];
  estimated_units: number;
  expires_in_sec: number;
}
export interface SemrushPreviewRequest {
  therapeutic_area: string;
  brand?: string | null;
  include_generics: boolean;
  include_indications: boolean;
  include_competitors: boolean;
  per_seed_limit?: number | null;
  reports?: string | null;
}
export interface SemrushIngestRequest {
  fetch_id: string;
  source_label: string;
  dataset_date: string;
  synthesize: boolean;
  only_new?: boolean;
  limit?: number | null;
}
export interface PrioritizedQuestion {
  id: number;
  question_id: string;
  question_text: string;
  persona: string;
  therapeutic_area: string;
  brand_focus: string;
  domain: string;
  approval_status: string;
  priority_weight: number;
  search_volume: number;
  matched_queries: number;
  demand_score: number;
}
export interface PrioritizedQuestions { batch_id: string | null; count: number; items: PrioritizedQuestion[]; }

// ----- Source Authority Mapping (FR-706a) -----
export interface SourceAuthorityCoverage {
  total_responses: number;
  citation_capable: number;
  with_citations: number;
  coverage_pct: number;
  states: Record<string, number>;
}
export interface SourceAuthorityCategory {
  display_category: string;
  citation_count: number;
  response_count: number;
  citation_share_pct: number;
}
export interface SourceAuthorityDistribution {
  total_citations: number;
  categories: SourceAuthorityCategory[];
  coverage: SourceAuthorityCoverage;
}
export interface SourceDomainRank {
  authority_domain: string;
  display_category: string;
  control_type: string;
  authority_type: string;
  verification: string;
  publisher_name: string | null;
  citation_count: number;
  response_count: number;
}
export interface SourceTopDomains {
  group_by: string | null;
  items?: SourceDomainRank[];
  groups?: { llm_name: string; items: SourceDomainRank[] }[];
}
export interface PreferredSource {
  pref_id: string;
  therapeutic_area: string;
  authority_domain: string;
  registrable_domain: string | null;
  note: string | null;
  active: boolean;
  created_by: string;
  updated_by: string | null;
  change_reason: string | null;
  created_at: string | null;
  updated_at: string | null;
}
export interface PreferredObservation {
  pref_id: string;
  therapeutic_area: string;
  authority_domain: string;
  note: string | null;
  observations: number;
  present: number;
  absent: number;
  presence_pct: number | null;
}
export interface SaFilters {
  llm_name?: string;
  therapeutic_area?: string;
  indication?: string;
  brand?: string;
  persona?: string;
}
// --- Source Authority enhancements: trends / drill-down / sentiment correlation ---
export interface SourceTrendPeriod {
  period: string;
  total_citations: number;
  categories: Record<string, number>;
}
export interface SourceTrends {
  granularity: string;
  periods: SourceTrendPeriod[];
  categories_seen: string[];
}
export interface SourceDomainCitation {
  response_id: string;
  run_id: string | null;
  question_id: string;
  question_text: string;
  persona: string;
  llm_name: string;
  therapeutic_area: string;
  indication: string | null;
  brand_focus: string | null;
  timestamp: string | null;
  citation_count: number;
  urls: string[];
  sentiment_score: number | null;
  competitive_position: string | null;
}
export interface SourceDomainDetail {
  authority_domain: string;
  classification: {
    display_category: string;
    control_type: string;
    authority_type: string;
    verification: string;
    publisher_name: string | null;
  } | null;
  total_citations: number;
  response_count: number;
  items: SourceDomainCitation[];
}
export interface SentimentSourceBucket {
  control_type: string;
  label: string;
  response_count: number;
  avg_sentiment: number | null;
  position_distribution: Record<string, number>;
  weak_position_pct: number;
}
export interface SentimentBySource {
  total_scored_responses: number;
  buckets: SentimentSourceBucket[];
}
export interface VoiceSlice {
  control_type: string;
  label: string;
  citation_count: number;
  response_count: number;
  share_pct: number;
}
export interface CompetitorSource {
  authority_domain: string;
  publisher_name: string | null;
  citation_count: number;
  response_count: number;
  share_pct: number;
}
export interface ShareOfVoice {
  total_citations: number;
  response_count: number;
  voice: VoiceSlice[];
  abbvie_share_pct: number;
  competitor_share_pct: number;
  independent_share_pct: number;
  competitor_total_citations: number;
  competitors: CompetitorSource[];
}
export interface CitedPage {
  url: string;
  authority_domain: string;
  control_type: string;
  display_category: string;
  publisher_name: string | null;
  citation_count: number;
  response_count: number;
}
export interface SourcePages {
  total_pages: number;
  items: CitedPage[];
}
export interface ClaimSource {
  authority_domain: string | null;
  url: string | null;
  display_category: string | null;
  control_type: string | null;
}
export interface ProvenanceClaim {
  text: string;
  bucket: string;
  sources: ClaimSource[];
}
export interface ResponseProvenance {
  response_id: string;
  found: boolean;
  question_text?: string;
  llm_name?: string;
  claims_total: number;
  summary: Record<string, number>;
  claims: ProvenanceClaim[];
}
// --- Source-to-Claim Influence Graph (corpus-wide provenance web) ---
export type InfluenceNodeType = "source" | "claim" | "theme" | "position";
export interface InfluenceNode {
  id: string;
  type: InfluenceNodeType;
  label: string;
  display_label?: string;
  weight: number;
  authority_domain?: string;
  control_type?: string;
  authority_type?: string | null;
  display_category?: string | null;
  url?: string | null;
  text?: string;
}
export interface InfluenceLink {
  source: string;
  target: string;
  value: number;
}
export interface InfluenceDriverSource {
  authority_domain: string;
  publisher_name?: string | null;
  control_type: string;
  responses: number;
  share_pct: number;
}
export interface InfluenceThemeDriver {
  theme: string;
  theme_responses: number;
  top_sources: InfluenceDriverSource[];
}
export interface InfluenceGraphMeta {
  grounded_responses: number;
  total_responses: number;
  coverage_pct: number;
  node_count: number;
  link_count: number;
  truncated: boolean;
  theme_drivers: InfluenceThemeDriver[];
  filters: Record<string, unknown>;
  generated_at: string;
}
export interface InfluenceGraph {
  nodes: InfluenceNode[];
  links: InfluenceLink[];
  meta: InfluenceGraphMeta;
}
export interface InfluenceNodeEvidence {
  node_type: string;
  key: string;
  response_count: number;
  items: SourceDomainCitation[];
}
function _saQs(f?: SaFilters, extra?: Record<string, string>): string {
  const p = new URLSearchParams();
  if (f?.llm_name) p.set("llm_name", f.llm_name);
  if (f?.therapeutic_area) p.set("therapeutic_area", f.therapeutic_area);
  if (f?.indication) p.set("indication", f.indication);
  if (f?.brand) p.set("brand", f.brand);
  if (f?.persona) p.set("persona", f.persona);
  for (const [k, v] of Object.entries(extra ?? {})) if (v) p.set(k, v);
  const s = p.toString();
  return s ? `?${s}` : "";
}

// ---------- Question Variations (phrasing-robustness grouping) ----------
export interface Variation {
  id: number;
  variation_group_id: string;
  base_question_id: string;
  variation_text: string;
  dedupe_hash: string;
  generation_method: string;
  generation_model: string | null;
  pii_flags: string[] | null;
  status: "DRAFT" | "APPROVED" | "REJECTED";
  promoted_question_id: string | null;
  reviewer_name: string | null;
  review_note: string | null;
  edited: boolean;
  created_at: string;
  updated_at: string;
}

export interface VariationGenerateResult {
  group_id: string;
  base_question_id: string;
  created: number;
  variations: Variation[];
}

export interface VariationGroupSummary {
  group_id: string;
  base_question_text: string | null;
  persona: string | null;
  therapeutic_area: string | null;
  brand_focus: string | null;
  draft_count: number;
  approved_count: number;
  rejected_count: number;
  total: number;
}

export interface VariationGroupList {
  count: number;
  groups: VariationGroupSummary[];
}

export interface VariationBase {
  question_id: string;
  question_text: string;
  persona: string | null;
  therapeutic_area: string | null;
  brand_focus: string | null;
  domain: string | null;
  monitoring_mode: string | null;
  approval_status: string;
}

export interface VariationGroupDetail {
  group_id: string;
  base: VariationBase | null;
  drafts: Variation[];
  approved_variation_count: number;
  counts: { draft: number; approved: number; rejected: number };
}

/** One selected bank question, with the variations it is allowed to bring into a run. */
export interface VariationExpansionGroup {
  question_id: string;
  question_text: string;
  approval_status: string;
  is_variation: boolean;
  approved_variation_ids: string[];
  approved_count: number;
  /** Staged drafts still awaiting review — reported so the modal can say why they sit out. */
  pending_count: number;
  rejected_count: number;
}

/** Read-only preview of what a bank selection runs as. `question_ids` is what gets run. */
export interface VariationExpansion {
  question_ids: string[];
  base_count: number;
  variation_count: number;
  total: number;
  groups: VariationExpansionGroup[];
  missing: string[];
}

export interface VariationAnswerCell {
  llm_name: string;
  status: string;
  response_id: string;
  sentiment_score: number | null;
  competitive_position: string | null;
  mentioned: boolean;
  answer_excerpt: string;
}

export interface VariationResultRow {
  question_id: string;
  question_text: string;
  is_base: boolean;
  generation_method: string | null;
  answers: VariationAnswerCell[];
  mean_sentiment: number | null;
  modal_position: string | null;
  mention_rate: number | null;
}

export interface VariationDivergence {
  variations_scored: number;
  sentiment_mean: number | null;
  sentiment_spread: number;
  group_modal_position: string | null;
  position_agreement: number | null;
  mention_rate_spread: number;
  consistency_score: number | null;
  outliers: { question_id: string; reasons: string[] }[];
}

export interface VariationGroupResults {
  group_id: string;
  run_id: string | null;
  base: VariationBase | null;
  variations: VariationResultRow[];
  summary: VariationDivergence;
}

export interface VariationGroupRunResult {
  run_id: string;
  status: string;
  question_ids: string[];
  count: number;
}

// ----- Activation & Impact (interventions, thin v1) -----
export interface MetricValues {
  n: number;
  n_sentiment: number;
  position_counts: Record<string, number>;
  brand_mention_rate: number | null;
  leading_rate: number | null;
  consideration_rate: number | null;
  missing_rate: number | null;
  weak_position_rate: number | null;
  avg_sentiment: number | null;
  response_consistency: number | null;
  by_model: Record<string, { n: number; leading: number; consideration: number; avg_sentiment: number | null }>;
}

export interface MeasurementSnapshot {
  id: string;
  snapshot_type: "DISCOVERY" | "OFFICIAL_BASELINE" | "POST";
  response_count: number;
  metrics: MetricValues | null;
  model_versions: Record<string, string>;
  scorer_version: string | null;
  prompt_version: string | null;
  run_ids: string[];
  pending: boolean;
  captured_at: string | null;
}

export interface MetricChange {
  label: string;
  kind: "rate" | "score";
  baseline: number;
  post: number;
  change: number;
  change_pp: number | null;
}

export interface Confounder { code: string; detail: string }

export interface InterventionResultData {
  id: string;
  baseline_snapshot_id: string | null;
  post_snapshot_id: string | null;
  metric_changes: Record<string, MetricChange>;
  confounders: Confounder[];
  confidence: "HIGH" | "MEDIUM" | "LOW" | null;
  outcome_status: string | null;
  interpretation: string | null;
  measured_at: string | null;
}

export interface Intervention {
  id: string;
  recommendation_id: string | null;
  source_type: string;
  source_id: string | null;
  title: string;
  description: string | null;
  status: string;
  priority: string | null;
  owner_name: string | null;
  reviewer_name: string | null;
  review_required: boolean;
  review_status: string | null;
  therapeutic_area: string | null;
  indication: string | null;
  brand_focus: string | null;
  publication_url: string | null;
  publication_date: string | null;
  due_date: string | null;
  monitoring_mode: string;
  target_question_ids: string[];
  target_personas: string[];
  target_models: string[];
  target_metrics: string[];
  primary_metric: string;
  measurement_wait_days: number;
  repetitions_per_question: number;
  measurement_status: string;
  post_due_at: string | null;
  outcome_status: string | null;
  evidence: Record<string, any> | null;
  created_at: string | null;
  updated_at: string | null;
  // Detail-only (GET /interventions/{id}):
  snapshots?: { discovery: MeasurementSnapshot | null; official_baseline: MeasurementSnapshot | null; post: MeasurementSnapshot | null };
  result?: InterventionResultData | null;
  metric_defs?: Record<string, { label: string; kind: string }>;
  measure_action?: string;
}

export interface InterventionList { count: number; items: Intervention[] }

export interface InterventionEvent {
  id: number;
  event_type: string;
  previous_status: string | null;
  new_status: string | null;
  actor_name: string | null;
  notes: string | null;
  metadata: Record<string, any> | null;
  created_at: string | null;
}
export interface InterventionTimeline { count: number; items: InterventionEvent[] }

export interface InterventionResultBundle {
  intervention_id: string;
  measurement_status: string;
  outcome_status: string | null;
  primary_metric: string;
  result: InterventionResultData | null;
  discovery: MeasurementSnapshot | null;
  official_baseline: MeasurementSnapshot | null;
  post: MeasurementSnapshot | null;
}

export interface InterventionCreateBody {
  title?: string;
  description?: string;
  owner_name?: string;
  reviewer_name?: string;
  review_required?: boolean;
  priority?: "LOW" | "MEDIUM" | "HIGH";
  due_date?: string;
  extra_question_ids?: string[];
  target_models?: string[];
  primary_metric?: string;
  measurement_wait_days?: number;
  repetitions_per_question?: number;
}

export interface InterventionUpdateBody {
  title?: string;
  description?: string;
  owner_name?: string;
  reviewer_name?: string;
  review_required?: boolean;
  review_status?: string;
  priority?: "LOW" | "MEDIUM" | "HIGH";
  due_date?: string;
  target_question_ids?: string[];
  target_models?: string[];
  target_metrics?: string[];
  primary_metric?: string;
  measurement_wait_days?: number;
  repetitions_per_question?: number;
}

export interface InterventionPublishBody {
  publication_url: string;
  publication_date?: string;
  actor_name?: string;
}

export interface InterventionTransitionBody {
  to_status: "PROPOSED" | "IN_PROGRESS" | "DEFERRED" | "CANCELLED";
  actor_name?: string;
  notes?: string;
}

/* ------------------------------------------------------------------ */
/*  Evidence store (X2) + competitor discovery (Phase 5)               */
/* ------------------------------------------------------------------ */

export interface EvidenceOverview {
  studies: {
    total: number;
    by_verification_status: Record<string, number>;
    by_indication: Record<string, number>;
  };
  /**
   * Led with deliberately. A row without a canonical outcome id is invisible to every
   * network, so raw ingest volume without this ratio is true and misleading.
   */
  outcome_results: {
    total: number;
    with_canonical_outcome: number;
    canonical_coverage_pct: number;
  };
  networks: {
    total: number;
    by_ratification_status: Record<string, number>;
    connected: number;
  };
  drug_facts: { total: number; by_verification_status: Record<string, number> };
}

export interface EvidenceNetworkRow {
  network_id: string;
  label: string | null;
  indication: string;
  canonical_outcome_id: string;
  population_stratum: string | null;
  treatment_phase: string;
  protocol_id: string | null;
  ratification_status: string;
  is_connected: boolean | null;
  has_closed_loops: boolean | null;
  has_multi_arm_studies: boolean | null;
  node_count: number;
  edge_count: number;
  membership_counts: Record<string, number>;
  version: number;
  superseded_by: string | null;
  updated_at: string | null;
}

export interface EvidenceNetworkList {
  total: number;
  networks: EvidenceNetworkRow[];
  ratification_states: string[];
  membership_states: string[];
}

/** The protocol-scoped re-reading of a network. Derived per request, never stored. */
export interface EvidenceProtocolScope {
  protocol_id: string;
  approved_time_window: number[];
  topology: Record<string, any>;
  nodes_lost_to_window: string[];
  studies_out_of_window: string[];
  narrows_the_network: boolean;
}

export interface EvidenceMembership {
  membership_id: string;
  study_id: string;
  protocol_id: string | null;
  membership_status: string;
  exclusion_reason: string | null;
  proposal_rationale: string | null;
  review_note: string | null;
  mismatch_flags: string[];
  decided_by: string | null;
  decided_at: string | null;
  registry_id: string | null;
  acronym: string | null;
  title: string | null;
  verification_status: string | null;
}

export interface EvidenceNetworkDetail extends EvidenceNetworkRow {
  endpoint_topology: {
    nodes: string[];
    edges: [string, string, number][];
    administration_routes: Record<string, string>;
  };
  protocol_scope: EvidenceProtocolScope | null;
  /** True when the stored graph promises more than a protocol-scoped resolve delivers. */
  overstates_answerable: boolean;
  ratification: {
    status: string;
    medical_reviewer: string | null;
    medical_reviewed_at: string | null;
    medical_review_note: string | null;
    statistical_reviewer: string | null;
    statistical_reviewed_at: string | null;
    statistical_review_note: string | null;
    rejection_reason: string | null;
  };
  memberships: EvidenceMembership[];
}

export interface EvidenceStudyRow {
  study_id: string;
  registry_id: string | null;
  acronym: string | null;
  title: string | null;
  indication: string;
  phase: string | null;
  treatment_phase: string;
  is_randomised: boolean;
  population_stratum: string | null;
  enrollment: number | null;
  sponsor: string | null;
  results_first_posted: string | null;
  risk_of_bias: string | null;
  verification_status: string;
  verified_by: string | null;
  mismatch_flags: string[];
  source_is_citable: boolean;
  claim_is_approved_for_external_use: boolean;
  version: number;
  superseded_by: string | null;
  updated_at: string | null;
  arm_count: number;
  outcome_count: number;
  canonical_outcome_count: number;
  canonical_outcome_ids: string[];
  treatments: string[];
}

export interface EvidenceStudyList {
  total: number;
  studies: EvidenceStudyRow[];
  verification_states: string[];
}

export interface EvidenceArm {
  arm_id: string;
  label: string | null;
  treatment: string;
  is_placebo: boolean;
  drug_class: string | null;
  administration_route: string | null;
  dose_value: number | null;
  dose_unit: string | null;
  dose_frequency: string | null;
  dose_description: string | null;
  sample_size: number | null;
}

export interface EvidenceOutcomeRow {
  result_id: string;
  arm_id: string | null;
  arm_treatment: string | null;
  canonical_outcome_id: string | null;
  endpoint: string;
  timepoint_week: number | null;
  population_stratum: string | null;
  treatment_phase: string;
  outcome_type: string;
  events: number | null;
  sample_size: number | null;
  mean: number | null;
  standard_deviation: number | null;
  comparator_treatment: string | null;
  effect_estimate: number | null;
  effect_measure: string | null;
  ci_lower: number | null;
  ci_upper: number | null;
  is_significant: boolean | null;
  is_safety_outcome: boolean;
  mismatch_flags: string[];
  verification_status: string;
}

export interface CurationQueueRow {
  study_id: string;
  registry_id: string | null;
  acronym: string | null;
  title: string | null;
  indication: string;
  verification_status: string;
  verified_by: string | null;
  arm_count: number;
  canonical_outcome_count: number;
  has_retained_document: boolean;
  in_scope_arm_count?: number;
  could_contribute?: boolean;
  withheld_row_count?: number;
  withheld_reasons?: string[];
}

export interface CurationQueue {
  network_id: string | null;
  total: number;
  blocking: number;
  worth_verifying: number | null;
  protocol_blocked: string[];
  by_status: Record<string, number>;
  studies: CurationQueueRow[];
  note: string;
}

export interface StudySourceDifference {
  kind: string;
  id: string;
  field: string;
  stored: unknown;
  source: unknown;
  note?: string;
}

export interface StudySourceCheck {
  study_id: string;
  registry_id: string | null;
  verification_status: string;
  verified_by: string | null;
  verified_at: string | null;
  source: {
    payload_id: string;
    source_type: string;
    source_identifier: string;
    url: string | null;
    retrieved_at: string | null;
    checksum: string | null;
    license_class: string;
    retention_policy: string;
  } | null;
  checkable: boolean;
  reproducible: boolean;
  difference_count: number;
  differences: StudySourceDifference[];
  differences_omitted: number;
  counts: Record<string, { stored: number; source: number }>;
  source_warnings: string[];
  flag_counts: Record<string, number>;
  blocked_reason: string | null;
}

export interface EvidenceStudyDetail extends EvidenceStudyRow {
  study_design: string | null;
  population_description: string | null;
  prior_treatment_status: string | null;
  risk_of_bias_rationale: string | null;
  start_date: string | null;
  completion_date: string | null;
  source_payload_id: string | null;
  extraction_confidence: number | null;
  extraction_rationale: string | null;
  rejection_reason: string | null;
  arms: EvidenceArm[];
  outcomes: EvidenceOutcomeRow[];
}

export interface EvidenceDrugFactRow {
  fact_id: string;
  brand: string;
  generic: string | null;
  molecule: string | null;
  manufacturer: string | null;
  mechanism_of_action: string | null;
  drug_class: string | null;
  administration_route: string | null;
  dosage_form: string | null;
  approved_indications: string[];
  approval_date: string | null;
  label_updated_at: string | null;
  contraindications: string[];
  boxed_warnings: string[];
  common_adverse_events: string[];
  serious_adverse_events: string[];
  has_boxed_warning: boolean;
  regulatory_source: string | null;
  prescribing_information: string | null;
  extraction_confidence: number | null;
  mismatch_flags: string[];
  verification_status: string;
  verified_by: string | null;
  /** Independent of each other on purpose — a citable source with an unreviewed reading. */
  source_is_citable: boolean;
  claim_is_approved_for_external_use: boolean;
  version: number;
  superseded_by: string | null;
  updated_at: string | null;
}

export interface EvidenceDrugFactList {
  total: number;
  drug_facts: EvidenceDrugFactRow[];
}

export interface EvidenceProtocol {
  protocol_id: string;
  content_hash: string | null;
  status: string;
  decisions?: Record<string, any>[];
  [key: string]: any;
}

export interface EvidenceNetworkGate {
  network_id: string;
  blocking_status?: string | null;
  // Whether DRAFT is a legal move from here, answered by the state machine rather than
  // re-derived client-side from the status string.
  can_reopen?: boolean;
  [key: string]: any;
}

/**
 * The ratification half of the review surface: which stage a network is at, and the moves the
 * state machine will accept from there.
 *
 * `allowed_transitions` is the server's own account of what is legal next, returned so a caller
 * never has to derive it. The two review stages are ordered — approving medical advances to
 * statistical review rather than ratifying — and this is the authority on that ordering.
 */
export interface EvidenceNetworkRatification {
  network_id: string;
  protocol_id: string | null;
  ratification_status: string;
  allowed_transitions: string[];
  is_computable: boolean;
  medical_reviewer: string | null;
  medical_reviewed_at: string | null;
  statistical_reviewer: string | null;
  statistical_reviewed_at: string | null;
  rejection_reason: string | null;
}

export interface ComparisonMatrix {
  network_id: string;
  pairs?: Record<string, any>[];
  [key: string]: any;
}

/* ------------------------------------------------------------------ */
/*  Evidence ingestion — the corpus-growing operations, off the shell  */
/* ------------------------------------------------------------------ */

/** Form vocabulary, served from config so the UI cannot offer what the validator rejects. */
export interface IngestOptions {
  indications: string[];
  outcomes_by_indication: Record<string, string[]>;
  protocols: string[];
  treatment_phases: string[];
  full_depth_drugs: string[];
  verification: string;
}

export interface IngestStudyOutcome {
  study_id: string;
  action: string;
  reason: string | null;
  verification_status: string | null;
  arm_count: number;
  outcome_count: number;
  warnings: string[];
}

/**
 * The report the CLI prints, as JSON. The counts are the least of it: the label buckets and
 * the screened-out reasons are what make the run reviewable before it is committed.
 */
export interface IngestionReportView {
  indication: string;
  discovered: number;
  screened_out: number;
  ingested: number;
  updated: number;
  skipped: number;
  fetch_failures: { id: string; reason: string }[];
  /** Real agents the drug catalog does not know — each is currently its own junk node. */
  unmapped_treatments: Record<string, number>;
  /** Not fixable in config: the registry never said what these arms received. */
  uninformative_arms: Record<string, number>;
  /** Named a class or a care strategy, so their whole study is screened out. */
  class_level_arms: Record<string, number>;
  /** label -> the studies that produced it. Without this the advice is unfollowable. */
  label_studies: Record<string, string[]>;
  screened_out_detail: { id: string; reason: string }[];
  studies: IngestStudyOutcome[];
}

export interface BuildReportView {
  network_id: string;
  indication: string;
  canonical_outcome_id: string;
  treatment_phase: string;
  population_stratum: string | null;
  protocol_id: string | null;
  created: boolean;
  proposed_study_count: number;
  proposed_studies: string[];
  excluded: { study_id: string; reason: string }[];
  endpoint_topology: Record<string, any>;
  protocol_scope: EvidenceProtocolScope | null;
  overstates_answerable: boolean;
}

export interface DrugFactOutcomeView {
  brand: string;
  action: string;
  fact_id: string | null;
  reason: string | null;
  verification_status: string | null;
  label_updated_at: string | null;
  supersedes: string | null;
  flags: string[];
}

export interface DrugFactReportView {
  requested: number;
  ingested: number;
  updated: number;
  superseded: number;
  skipped: number;
  not_found: number;
  awaiting_verification: number;
  facts: DrugFactOutcomeView[];
}

export interface ReparseReportView {
  studies: number;
  by_action: Record<string, number>;
  /** VERIFIED and REJECTED rows are skipped by design, so this is expected, not a failure. */
  skipped_because_decided: number;
  results: IngestStudyOutcome[];
}

export interface IngestJobReport {
  kind: "trials" | "drug-facts" | "reparse";
  /** False on a preview. The badge and the "nothing was written" line both key off it. */
  committed: boolean;
  ingestion?: IngestionReportView;
  network?: BuildReportView | null;
  drug_facts?: DrugFactReportView;
  reparse?: ReparseReportView;
}

export interface IngestJobStatus {
  running: boolean;
  kind: "trials" | "drug-facts" | "reparse" | null;
  mode: "PREVIEW" | "COMMIT" | null;
  scope: Record<string, any> | null;
  started_at: string | null;
  finished_at: string | null;
  progress: Record<string, any> | null;
  report: IngestJobReport | null;
  /** Prose, not a status code: a RATIFIED network refusing a rebuild lands here. */
  error: string | null;
}

export interface IngestStarted {
  status: string;
  kind: string;
  mode: "PREVIEW" | "COMMIT";
}

export interface IngestTrialsBody {
  indication: string;
  drugs?: string[];
  outcome?: string | null;
  protocol?: string | null;
  phase?: string;
  stratum?: string | null;
  limit?: number | null;
  commit?: boolean;
}

export interface DiscoveryReason {
  code: string;
  label: string;
  weight: number;
}

export interface DiscoveryReasonVocabulary {
  reasons: DiscoveryReason[];
  review_states: string[];
  newly_active_days: number;
  tier_b2_out_of_scope: string;
}

export interface CompetitorCandidate {
  candidate_id: string;
  treatment: string;
  generic: string | null;
  sponsor: string | null;
  indication: string;
  therapeutic_area: string | null;
  /** Copied from curation or null. Never inferred — Tier B2 is out of scope. */
  drug_class: string | null;
  administration_route: string | null;
  is_curated_drug: boolean;
  discovery_reasons: string[];
  reason_labels: string[];
  evidence_count: number;
  direct_comparison_count: number;
  compared_with: string[];
  shared_comparators: string[];
  published_nma_count: number;
  development_phase: string | null;
  has_posted_results: boolean;
  latest_evidence_date: string | null;
  source_study_ids: string[];
  discovery_confidence: number;
  review_status: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  review_note: string | null;
  config_applied: boolean;
  first_seen_at: string | null;
}

export interface CompetitorCandidateList {
  total: number;
  candidates: CompetitorCandidate[];
  counts_by_status: Record<string, number>;
  review_states: string[];
  reasons: DiscoveryReason[];
}

export interface DiscoverySweepReport {
  indications: {
    indication: string;
    treatments_observed: number;
    already_tracked: number;
    studies_scanned: number;
    published_syntheses_scanned: number;
    candidates_found: number;
  }[];
  created: number;
  updated: number;
  skipped_decided: number;
  candidates: Record<string, any>[];
}

export interface DiscoveryConfigProposal {
  accepted_pending_commit: number;
  indications: string[];
  needs_characterising: string[];
  /** A fragment for a human to commit. Discovery never edits brands.yaml. */
  yaml: string;
  note: string;
}

export interface DiscoveryClassMap {
  indication: string;
  treatment_count: number;
  classes: {
    drug_class: string;
    treatments: string[];
    routes: Record<string, string>;
    monitored: string[];
  }[];
  /** Reported alongside the groups: a class map that hid these would look complete. */
  uncharacterised: string[];
  characterised_pct: number;
  is_route_mixed: boolean;
  routes_present: string[];
}

/* ── Phase 8: claim-level AI-vs-evidence evaluation ────────────────────────────────── */

/** A per-dimension or overall rollup.
 *
 *  `coverage` must be read before `alignment_score`. Claims we could not check are excluded
 *  from the score — otherwise alignment would FALL as our evidence base thins — so a score
 *  of 1.0 over three checkable claims out of forty is unmeasured, not aligned.
 */
export interface AlignmentRollup {
  claim_count: number;
  checkable_count: number;
  coverage: number | null;
  alignment_score: number | null;
  by_classification: Record<string, number>;
  by_dimension: Record<string, { aligned: number; adverse: number; total: number }>;
  certainty_calibration: Record<string, number>;
  adverse_count: number;
  safety_contradictions: number;
}

export interface AdverseClaimExample {
  claim_id: string;
  llm_name: string | null;
  claim_text: string;
  claim_type: string;
  classification: string | null;
  reason: string | null;
}

export interface AlignmentReport {
  filters: Record<string, string | null>;
  overall: AlignmentRollup;
  by_model: Record<string, AlignmentRollup>;
  by_claim_type: Record<string, AlignmentRollup>;
  adverse_examples: AdverseClaimExample[];
}

export interface ClaimEvaluationRunResult {
  run_id: string;
  responses: number;
  evaluated: number;
  failed: number;
  finding_count: number;
}

export interface ClaimVocabulary {
  claim_types: string[];
  classifications: string[];
  adverse_classifications: string[];
  dimensions: string[];
  certainty_levels: string[];
  certainty_verdicts: string[];
  directions: string[];
  policy: Record<
    string,
    { authoritative_evidence: string[]; description: string; dimensions: string[] }
  >;
}

export interface EvaluationClaimRow {
  claim_id: string;
  claim_text: string;
  claim_type: string;
  subject: string;
  comparator: string | null;
  indication: string | null;
  outcome: string | null;
  direction: string;
  polarity: string;
  certainty: string;
  magnitude: number | null;
  magnitude_unit: string | null;
  cited_identifiers: string[];
  expected_evidence_policy: string[];
  classification: string | null;
  reason: string | null;
  dimensions: string[];
  certainty_verdict: string | null;
  flags: string[];
  is_adverse: boolean;
  extracted_by: string | null;
  extraction_version: string;
}

/* ── Phase 9: evidence synthesis ───────────────────────────────────────────────────── */

export interface SynthesisFinding {
  treatment: string;
  comparator: string;
  statement: string;
  crosses_no_effect: boolean | null;
  evidence_level: number | null;
  is_direct: boolean;
  is_internal_output: boolean;
  status: string;
  contributing_studies: string[];
  flags: string[];
}

export interface SynthesisLimitation {
  kind: string;
  detail: string;
  count?: number;
}

export interface EvidenceSynthesis {
  indication: string;
  network_id: string | null;
  generated_at: string;
  what_the_evidence_shows: SynthesisFinding[];
  what_changed: {
    window_days: number;
    new_studies: { study_id: string; title: string | null; verification_status: string }[];
    label_updates: {
      brand: string;
      label_updated_at: string | null;
      verification_status: string;
    }[];
    new_synthesis_results: number;
  };
  evidence_strength: {
    studies_total: number;
    studies_verified: number;
    verified_fraction: number | null;
    studies_by_verification_status: Record<string, number>;
    network_ratification_status: string | null;
    network_is_connected: boolean | null;
    network_has_closed_loops: boolean | null;
  };
  limitations: SynthesisLimitation[];
  competitor_landscape: {
    accepted_count: number;
    threats: (StrategicImplicationMeta & {
      treatment: string;
      candidate_id: string;
      reason: string;
      confidence: number;
    })[];
  };
  ai_alignment: {
    claims_evaluated: number;
    by_classification: Record<string, number>;
    by_certainty_verdict: Record<string, number>;
  };
  strategic_implications: {
    implication: string;
    count: number;
    owner: string | null;
    severity: number | null;
    externally_actionable: boolean;
  }[];
}

/* ── Lifecycle 2: network membership ───────────────────────────────────────────────── */

export interface MembershipPreview {
  network_id: string;
  counts: Record<string, number>;
  total: number;
  included: number;
  /** True once ANY study is INCLUDED — at which point every other study stops contributing. */
  filter_binds: boolean;
  studies_consulted: number;
  note: string;
}

export interface MembershipDecision {
  network_id: string;
  study_id: string;
  membership_status: string;
  exclusion_reason: string | null;
  review_note: string | null;
  decided_by: string;
  decided_at: string | null;
  before: string;
  membership: MembershipPreview;
  narrowed_the_evidence_set: boolean;
  narrowing_warning: string | null;
}

/* ── Drug-fact curation ────────────────────────────────────────────────────────────── */

export interface DrugFactContribution {
  answers_approval_claim: boolean;
  answers_safety_claim: boolean;
  answers_mechanism_claim: boolean;
  could_contribute: boolean;
  blockers: string[];
}

export interface DrugFactQueueRow extends DrugFactContribution {
  fact_id: string;
  brand: string;
  generic: string | null;
  label_updated_at: string | null;
  version: number;
  superseded_by: string | null;
  verification_status: string;
  verified_by: string | null;
  has_boxed_warning: boolean;
  mismatch_flags: string[];
  prescribing_information: string | null;
}

export interface DrugFactQueue {
  total: number;
  blocking: number;
  worth_verifying: number;
  /** Facts no amount of curation makes answer an approval claim. Not curator backlog. */
  approval_blocked: string[];
  by_verification_status: Record<string, number>;
  facts: DrugFactQueueRow[];
  note: string;
}

export interface DrugFactSourceCheck extends DrugFactContribution {
  fact_id: string;
  brand: string;
  generic: string | null;
  label_updated_at: string | null;
  version: number;
  superseded_by: string | null;
  verification_status: string;
  verified_by: string | null;
  verified_at: string | null;
  prescribing_information: string | null;
  mismatch_flags: string[];
  source: Record<string, any> | null;
  checkable: boolean;
  reproducible: boolean;
  difference_count: number;
  differences: { kind: string; id: string; field: string; stored: any; source: any }[];
  differences_omitted: number;
  blocked_reason: string | null;
  checks: string;
  does_not_check: string;
}

// ---- curation (coverage-driven question generation) ----
// Every scope field is a LIST: a brand is not single-area, so scoping Rinvoq to
// Dermatology + Gastroenterology + Rheumatology in one request is the normal case.
export interface CurationScope {
  brands?: string[];
  therapeutic_areas?: string[];
  diseases?: string[];
  personas?: string[];
}

export interface CurationCell {
  key: string;
  disease: string;
  brand: string;
  competitor: string;
  persona: string;
  domain: string;
  therapeutic_area: string | null;
  area: string | null;
}

export interface CurationSummary {
  total_cells: number;
  covered: number;
  gaps: number;
  coverage_pct: number;
  gaps_by_area: Record<string, number>;
  gaps_by_disease: Record<string, number>;
  gaps_by_brand: Record<string, number>;
}

export interface CurationCoverage {
  scope: Required<CurationScope>;
  summary: CurationSummary;
  gaps: CurationCell[];
  gaps_truncated: number;
  estimated_model_calls: number;
}

export interface CurationGenerateResult {
  dry_run: boolean;
  summary: CurationSummary;
  model?: string;
  model_calls: number;
  created?: number;
  refreshed?: number;
  targets?: CurationCell[];
  staged: { status: string; cell: string; question_text: string; reason?: string }[];
  rejected?: { cell: string; question_text: string | null; reason: string }[];
}

// ---- competitive head-to-head (who wins when AI is asked "us vs them") ----
// Every read here is pure aggregation over answers a run already produced: no model calls.
export interface HeadToHeadModelRow {
  llm_name: string;
  answers: number;
  losing: number;
  loss_rate: number;
  verdict: string;
}

/** The same slice as `HeadToHeadModelRow`, cut by audience instead of by platform. */
export interface HeadToHeadPersonaRow {
  persona: string;
  answers: number;
  losing: number;
  loss_rate: number;
  verdict: string;
}

export interface HeadToHeadTrend {
  available: boolean;
  runs: number;
  note?: string;
  previous_loss_rate?: number;
  latest_loss_rate?: number;
  delta?: number;
  direction?: "worse" | "better" | "flat";
}

export interface HeadToHeadPair {
  key: string;
  label: string;
  brand: string;
  competitor: string;
  disease: string | null;
  therapeutic_area: string | null;
  area: string | null;
  answers: number;
  models: string[];
  personas: string[];
  runs: number;
  verdict: string;
  verdict_counts: Record<string, number>;
  losing_answers: number;
  loss_rate: number;
  position_mix: Record<string, number>;
  our_sentiment: number | null;
  their_sentiment: number | null;
  sentiment_gap: number | null;
  indication_known: boolean;
  /** stored | derived | text_only — how confident the pairing itself is. */
  pair_source: string;
  pair_source_note: string;
  by_model: HeadToHeadModelRow[];
  by_persona: HeadToHeadPersonaRow[];
  disagreement: {
    questions_compared: number;
    questions_with_disagreement: number;
    rate: number;
  };
  trend: HeadToHeadTrend;
}

/**
 * One day on the board-level loss-rate line. `answers` counts COMPARISON-answers — an
 * answer naming two rivals contributes one graded point to each — so these sum higher than
 * `answers_on_the_board`, exactly as the per-pair rows do.
 */
export interface HeadToHeadTimelinePeriod {
  period: string;
  answers: number;
  losing: number;
  even: number;
  winning: number;
  loss_rate: number;
  runs: number;
}

export interface HeadToHeadTimeline {
  granularity: string;
  periods: HeadToHeadTimelinePeriod[];
  runs: number;
  /** Answers the board used but could not place in time — reported, never dropped. */
  undated: number;
  min_periods: number;
  /** False below `min_periods`: two points joined up would read as a direction. */
  available: boolean;
  note: string;
}

export interface HeadToHeadExclusion {
  reason: string;
  answers: number;
  explanation: string;
}

/** `reason` of the exclusion line that is the reader's own filter, not a data problem. */
export const H2H_FILTERED_OUT = "filtered_out";

/**
 * The values each picker should offer, computed server-side against everything EXCEPT that
 * picker's own selection — so choosing one brand never removes the others from the brand
 * list, and every option offered has at least one comparison behind it.
 */
export interface HeadToHeadFilterOptions {
  areas: string[];
  diseases: string[];
  brands: string[];
  competitors: string[];
  personas: string[];
  models: string[];
}

export interface HeadToHeadFilters {
  therapeutic_area?: string[];
  disease?: string[];
  brand?: string[];
  competitor?: string[];
  persona?: string[];
  llm_name?: string[];
  verdict?: string[];
  limit?: number;
}

export interface HeadToHeadBoard {
  verdict_rule: string;
  /** Distinct answers. Per-pair counts may sum higher: one answer can name two rivals. */
  answers_examined: number;
  answers_on_the_board: number;
  answers_excluded: number;
  exclusions: HeadToHeadExclusion[];
  pairs_total: number;
  pairs: HeadToHeadPair[];
  pairs_truncated: number;
  /** Scoped to the comparisons that survived the filters, so it agrees with `pairs`. */
  timeline: HeadToHeadTimeline;
  filter_options: HeadToHeadFilterOptions;
  /** Echoed back, so a stale value in the URL is visible rather than silently ignored. */
  filters_applied: Record<string, string[]>;
  /** Comparison answers before any filter — the denominator behind "of N". */
  answers_in_corpus: number;
}

export interface HeadToHeadClaim {
  text: string;
  answers: number;
  models: string[];
  model_count: number;
  losing_answers: number;
  verdicts: Record<string, number>;
  cross_model: boolean;
  names_competitor: boolean;
  against_us: boolean;
}

export interface HeadToHeadDetail {
  verdict_rule: string;
  summary: HeadToHeadPair;
  response_ids: string[];
  claims: {
    answers_with_claims: number;
    claims_extracted: number;
    distinct_claims: number;
    claims_against_us: number;
    claims: HeadToHeadClaim[];
    claims_truncated: number;
    note: string;
  };
  sources: {
    available: boolean;
    error?: string;
    total_citations?: number;
    sourced_answers?: number;
    abbvie_share_pct?: number;
    competitor_share_pct?: number;
    independent_share_pct?: number;
    competitors?: { authority_domain: string; publisher_name: string | null; citation_count: number; share_pct: number }[];
    competitor_pages?: { url: string; authority_domain: string; publisher_name: string | null; citation_count: number; response_count: number }[];
    note?: string;
  };
  absence: {
    not_mentioned_answers: number;
    not_mentioned_pct: number;
    error?: string;
    gaps: {
      response_id: string;
      llm_name: string;
      competitive_position: string;
      competitor: string | null;
      competitor_domain: string | null;
      question_text: string;
    }[];
  };
  sample_answers: {
    response_id: string;
    run_id: string;
    question_id: string;
    question_text: string;
    llm_name: string;
    persona: string;
    position: string | null;
    our_sentiment: number | null;
    their_sentiment: number | null;
    verdict: string;
    rationale: string | null;
    key_claims: string[];
  }[];
}

export interface CoverageFunnelCell {
  key: string;
  disease: string;
  brand: string;
  competitor: string;
  persona: string;
  domain: string;
  therapeutic_area: string | null;
  area: string | null;
  state: string;
  state_label: string;
  questions: number;
  verdict: string | null;
}

export interface CoverageFunnel {
  scope: Required<CurationScope>;
  total_cells: number;
  states: { state: string; label: string; cells: number }[];
  monitored_cells: number;
  monitored_pct: number;
  counted_as_covered: number;
  /** Comparisons a coverage % counts as done that no model has ever actually been asked. */
  covered_but_unmonitored: number;
  cells: CoverageFunnelCell[];
  cells_truncated: number;
}

function competitiveQuery(
  f: Record<string, string | number | string[] | undefined | null>,
): string {
  const params = new URLSearchParams();
  Object.entries(f).forEach(([k, v]) => {
    if (v === undefined || v === null || v === "") return;
    // Repeated singular params (brand=A&brand=B), the same convention `curationQuery`
    // uses. An empty array means "no filter", which is exactly what sending nothing does.
    if (Array.isArray(v)) v.forEach((item) => item && params.append(k, String(item)));
    else params.append(k, String(v));
  });
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

function curationQuery(scope: CurationScope): string {
  const params = new URLSearchParams();
  // The API takes repeated singular params (brand=A&brand=B), which is what lets one
  // request span several areas without a nested body.
  (scope.brands ?? []).forEach((v) => params.append("brand", v));
  (scope.therapeutic_areas ?? []).forEach((v) => params.append("therapeutic_area", v));
  (scope.diseases ?? []).forEach((v) => params.append("disease", v));
  (scope.personas ?? []).forEach((v) => params.append("persona", v));
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

export const api = {
  // taxonomy — the single source of truth for areas, brands, diseases and competitors.
  // `lib/taxonomy.ts` hydrates itself from this at app boot instead of hardcoding a copy.
  taxonomy: () => get<TaxonomyPayload>("/taxonomy"),
  taxonomyStatus: () => get<TaxonomyStatus>("/taxonomy/status"),
  taxonomyExportUrl: () => `${BASE}/taxonomy/export.yaml`,
  taxonomyAreas: () => get<{ areas: TaxonomyAreaChoice[] }>("/taxonomy/areas"),

  // Add Brand. The first three are read-only; only createBrand writes.
  resolveBrand: (name: string) =>
    post<BrandResolveResult>("/taxonomy/brands/resolve", { name }),
  draftBrand: (name: string) => post<BrandDraft>("/taxonomy/brands/draft", { name }),
  draftCompetitors: (brand: string, disease: string) =>
    post<{ competitors: CompetitorSuggestion[] }>(
      "/taxonomy/brands/competitors", { brand, disease },
    ),
  draftOutcomes: (disease: string) =>
    post<OutcomeDraft>("/taxonomy/brands/outcomes", { disease }),
  createBrand: postBrand,

  // questions
  coverage: () => get<any>("/questions/coverage-gaps"),
  brandMatrix: () => get<BrandMatrix>("/questions/brand-matrix"),
  questions: (qs = "") => get<Question[]>(`/questions${qs}`),
  createQuestion: (body: {
    question_text: string;
    persona: string;
    therapeutic_area: string;
    brand_focus?: string | null;
    domain: string;
    monitoring_mode?: string;
    competitor_focus?: string[] | null;
    indication?: string | null;
    disease?: string | null;
    priority_weight?: number;
    approval_status?: string;
    demand_origin?: string | null;
  }) => post<Question>("/questions", body),
  // FR-116 — bulk prompt importer, step 1: dry-run preview (nothing persisted). Raw fetch so
  // we can read the JSON summary on non-2xx too (e.g. a 400 "no prompt column" detail).
  importPromptsPreview: async (form: FormData): Promise<{ ok: boolean; status: number; data: PromptPreviewResult }> => {
    const res = await fetch(`${BASE}/questions/import-prompts/preview`, { method: "POST", body: form });
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, data };
  },
  // FR-116 — bulk prompt importer, step 2: commit the analyst-approved subset into the bank.
  importPromptsCommit: (body: {
    questions: string[];
    persona: string;
    brand_focus: string;
    domain?: string;
    therapeutic_area?: string | null;
    demand_origin?: string;
  }) => post<PromptImportResult>("/questions/import-prompts", body),
  // `mutate` rather than `patch`: an approval the server refuses (e.g. an evidence-generated
  // question over unverified evidence) explains itself in `detail`, and a reviewer clicking
  // Approve has to be told why nothing happened.
  updateQuestion: (rowId: number, body: Partial<Pick<Question, "approval_status" | "active" | "priority_weight">> & { approver_name?: string }) =>
    mutate<Question>("PATCH", `/questions/${rowId}`, body),
  // FR-116.4 — question bank ranked by priority_weight × matched search-demand volume
  prioritizedQuestions: (batchId?: string) =>
    get<PrioritizedQuestions>(`/questions/prioritized${batchId ? `?batch_id=${encodeURIComponent(batchId)}` : ""}`),
  // runs
  runs: () => get<Run[]>("/runs"),
  run: (id: string) => get<Run>(`/runs/${id}`),
  runProgress: (id: string) => get<RunProgress>(`/runs/${id}/progress`),
  // `mutate` rather than `post`: when the server refuses to start a run it explains why in
  // `detail`, and an operator who clicked Run has to be told instead of watching nothing happen.
  createRun: (body: any) => mutate<Run>("POST", "/runs", body),
  dryRun: (body: any) => mutate<Run>("POST", "/runs/dry-run", body),
  cancelRun: (id: string) => post<Run>(`/runs/${id}/cancel`),
  rerunRun: (id: string) => mutate<Run>("POST", `/runs/${id}/rerun`, {}),
  // Continues an interrupted run IN PLACE (same run_id): only the (question, target) pairs
  // with no stored response are dispatched, so nothing already paid for is bought twice.
  // `mutate` so a 409 (not resumable) or 503 (deploy staging) is shown verbatim.
  resumeRun: (id: string) => mutate<Run>("POST", `/runs/${id}/resume`, {}),
  // Re-attempts ONLY the responses that errored, in place under the same run_id. The
  // answers that succeeded are kept and are not bought again. `mutate` so a 409 (nothing
  // to retry, or the run is busy) reaches the operator instead of failing silently.
  retryFailedRun: (id: string) => mutate<Run>("POST", `/runs/${id}/retry-failed`, {}),
  deployStatus: () => get<DeployStatus>("/runs/deploy-status"),
  // question variations (phrasing robustness) — generate -> review -> run -> compare
  generateVariations: (rowId: number, body: { n?: number; reviewer_name?: string } = {}) =>
    mutate<VariationGenerateResult>("POST", `/variations/generate/${rowId}`, body),
  variationGroups: () => get<VariationGroupList>("/variations/groups"),
  variationGroup: (groupId: string) =>
    get<VariationGroupDetail>(`/variations/groups/${encodeURIComponent(groupId)}`),
  variationGroupResults: (groupId: string, runId?: string) =>
    get<VariationGroupResults>(
      `/variations/groups/${encodeURIComponent(groupId)}/results${runId ? `?run_id=${encodeURIComponent(runId)}` : ""}`,
    ),
  editVariation: (varId: number, variation_text: string) =>
    mutate<Variation>("PATCH", `/variations/${varId}`, { variation_text }),
  approveVariation: (varId: number, body: { reviewer_name?: string; note?: string } = {}) =>
    mutate<Variation>("POST", `/variations/${varId}/approve`, body),
  rejectVariation: (varId: number, body: { reviewer_name?: string; note?: string } = {}) =>
    mutate<Variation>("POST", `/variations/${varId}/reject`, body),
  runVariationGroup: (groupId: string, body: { include_base?: boolean; dry_run?: boolean } = {}) =>
    mutate<VariationGroupRunResult>("POST", `/variations/groups/${encodeURIComponent(groupId)}/run`, body),
  // Read-only: what a selection becomes once approved variations are added. Starts no run.
  expandQuestionVariations: (question_ids: string[]) =>
    mutate<VariationExpansion>("POST", "/variations/expand", { question_ids }),
  // schedule
  getSchedule: () => get<Schedule>("/schedule"),
  updateSchedule: (body: Partial<Pick<Schedule, "enabled" | "cron" | "timezone">>) =>
    put<Schedule>("/schedule", body),
  // responses
  responses: (qs = "") => get<{ total: number; count: number; items: ResponseItem[] }>(`/responses${qs}`),
  responseDetail: (id: string) => get<any>(`/responses/${id}`),
  compare: (questionId: string) => get<any>(`/responses/compare?question_id=${questionId}`),
  // analytics
  sentiment: (filters?: TaFilters) => get<any>(`/analytics/sentiment-distribution${_taQs(filters)}`),
  positioning: (filters?: TaFilters) => get<any>(`/analytics/positioning${_taQs(filters)}`),
  volume: () => get<any>("/analytics/volume"),
  alertsSummary: () => get<any>("/analytics/alerts-summary"),
  llmComparison: () => get<any>("/analytics/llm-comparison"),
  consensusSummary: () => get<any>("/analytics/consensus-summary"),
  intentDistribution: () => get<any>("/analytics/intent-distribution"),
  personaSummary: (persona?: string, filters?: TaFilters) =>
    get<any>(`/analytics/persona-summary${_taQs({ ...filters, persona })}`),
  worstQuestions: (limit = 3, persona?: string, filters?: TaFilters) =>
    get<any[]>(`/analytics/worst-questions?limit=${limit}${persona ? `&persona=${encodeURIComponent(persona)}` : ""}${_taQs(filters, true)}`),
  // FR-108a: disease-state / pre-launch multi-competitor landscape matrix
  landscape: (filters?: TaFilters) => get<LandscapeMatrix>(`/analytics/landscape${_taQs(filters)}`),
  // run-scoped analytics
  runSummary: (runId: string) => get<any>(`/analytics/run-summary?run_id=${runId}`),
  // insights (advanced analytics)
  insightsStatus: () => get<any>("/insights/status"),
  insightsThemes: (persona?: string, filters?: TaFilters) => get<any>(`/insights/themes${_taQs({ ...filters, persona })}`),
  insightsTrends: (top = 8, persona?: string, filters?: TaFilters) => get<any>(`/insights/trends?top=${top}${persona ? `&persona=${encodeURIComponent(persona)}` : ""}${_taQs(filters, true)}`),
  insightsSignals: (persona?: string, filters?: TaFilters) => get<any>(`/insights/signals${_taQs({ ...filters, persona })}`),
  insightsThemeDetail: (id: string) => get<any>(`/insights/themes/${id}`),
  insightsRebuild: (targetThemes = 12, sampleCap = 300) =>
    post<any>(`/insights/rebuild?target_themes=${targetThemes}&sample_cap=${sampleCap}`),
  // geo intervention recommendations (BR-012)
  recommendations: (filters?: RecFilters) => get<RecommendationBatch>(`/recommendations${_recQs(filters)}`),
  generateRecommendations: (
    body: RecFilters & {
      limit?: number;
      response_ids?: string[];
      include_evidence_gaps?: boolean;
    },
  ) => post<GenerateRecommendationsResult>("/recommendations/generate", body),
  recommendationContentTypes: () =>
    get<{ content_types: string[]; semrush_configured: boolean }>("/recommendations/content-types"),
  recommendationsCsvUrl: (filters?: RecFilters) => `${BASE}/recommendations/export.csv${_recQs(filters)}`,
  citationOpportunities: (filters?: RecFilters, limit = 20) => {
    const qs = _recQs(filters);
    return get<CitationOpportunityResult>(
      `/recommendations/citation-opportunities${qs ? `${qs}&` : "?"}limit=${limit}`,
    );
  },
  shareOfCitation: (filters?: RecFilters) =>
    get<ShareOfCitation>(`/recommendations/share-of-citation${_recQs(filters)}`),
  preferredSourceGaps: (filters?: RecFilters) =>
    get<PreferredSourceGapResult>(`/recommendations/preferred-source-gaps${_recQs(filters)}`),
  queryFanouts: (filters?: RecFilters, limit = 25) => {
    const qs = _recQs(filters);
    return get<QueryFanoutResult>(
      `/recommendations/query-fanouts${qs ? `${qs}&` : "?"}limit=${limit}`,
    );
  },
  citationTrend: (filters?: RecFilters) =>
    get<CitationTrend>(`/recommendations/citation-trend${_recQs(filters)}`),
  recommendationReviews: (batchId?: string) =>
    get<RecommendationReviewResult>(
      `/recommendations/reviews${batchId ? `?batch_id=${encodeURIComponent(batchId)}` : ""}`,
    ),
  setRecommendationReview: (
    recId: string,
    body: { status: ReviewStatus; owner?: string | null; note?: string | null; updated_by?: string | null },
  ) => put<RecommendationReview>(`/recommendations/${encodeURIComponent(recId)}/review`, body),
  // activation & impact (interventions, thin v1)
  interventions: (status?: string) =>
    get<InterventionList>(`/interventions${status ? `?status=${encodeURIComponent(status)}` : ""}`),
  intervention: (id: string) => get<Intervention>(`/interventions/${encodeURIComponent(id)}`),
  createInterventionFromRec: (recId: string, body: InterventionCreateBody) =>
    mutate<Intervention>("POST", `/interventions/from-recommendation/${encodeURIComponent(recId)}`, body),
  updateIntervention: (id: string, body: InterventionUpdateBody) =>
    mutate<Intervention>("PATCH", `/interventions/${encodeURIComponent(id)}`, body),
  transitionIntervention: (id: string, body: InterventionTransitionBody) =>
    mutate<Intervention>("POST", `/interventions/${encodeURIComponent(id)}/transition`, body),
  publishIntervention: (id: string, body: InterventionPublishBody) =>
    mutate<Intervention>("POST", `/interventions/${encodeURIComponent(id)}/publish`, body),
  measureIntervention: (id: string) =>
    mutate<Intervention>("POST", `/interventions/${encodeURIComponent(id)}/measure`),
  interventionResult: (id: string) =>
    get<InterventionResultBundle>(`/interventions/${encodeURIComponent(id)}/result`),
  interventionTimeline: (id: string) =>
    get<InterventionTimeline>(`/interventions/${encodeURIComponent(id)}/timeline`),
  // source authority mapping (FR-706a)
  sourceAuthorityDistribution: (f?: SaFilters) =>
    get<SourceAuthorityDistribution>(`/source-authority/distribution${_saQs(f)}`),
  sourceAuthorityTopDomains: (f?: SaFilters, groupBy?: string, limit = 10) =>
    get<SourceTopDomains>(
      `/source-authority/top-domains${_saQs(f, { group_by: groupBy ?? "", limit: String(limit) })}`),
  sourceAuthorityCoverage: (f?: SaFilters) =>
    get<SourceAuthorityCoverage>(`/source-authority/coverage${_saQs(f)}`),
  sourceAuthorityStatus: () =>
    get<{
      rdap_enabled: boolean;
      llm_classifier_enabled: boolean;
      website_metadata_enabled: boolean;
      whoisxml_fallback_configured: boolean;
      whoisxml_configured: boolean;
    }>("/source-authority/status"),
  preferredSources: (ta?: string) =>
    get<{ items: PreferredSource[] }>(
      `/source-authority/preferred${ta ? `?therapeutic_area=${encodeURIComponent(ta)}` : ""}`),
  addPreferredSource: (body: { therapeutic_area: string; domain: string; note?: string; created_by?: string; change_reason?: string }) =>
    post<PreferredSource>("/source-authority/preferred", body),
  deletePreferredSource: (id: string) =>
    del<{ status: string; pref_id: string }>(`/source-authority/preferred/${encodeURIComponent(id)}`),
  preferredObservations: (ta?: string, llmName?: string) => {
    const p = new URLSearchParams();
    if (ta) p.set("therapeutic_area", ta);
    if (llmName) p.set("llm_name", llmName);
    const s = p.toString();
    return get<{ count: number; items: PreferredObservation[] }>(
      `/source-authority/preferred/observations${s ? `?${s}` : ""}`);
  },
  classifySourcesSweep: () =>
    post<{ processed: number; failed: number; remaining: number }>("/source-authority/classify/sweep"),
  sourceAuthorityTrends: (f?: SaFilters) =>
    get<SourceTrends>(`/source-authority/trends${_saQs(f)}`),
  sourceAuthorityDomain: (authorityDomain: string, f?: SaFilters, limit = 50) =>
    get<SourceDomainDetail>(
      `/source-authority/domain${_saQs(f, { authority_domain: authorityDomain, limit: String(limit) })}`),
  sourceAuthoritySentiment: (f?: SaFilters) =>
    get<SentimentBySource>(`/source-authority/sentiment-correlation${_saQs(f)}`),
  sourceAuthorityShareOfVoice: (f?: SaFilters) =>
    get<ShareOfVoice>(`/source-authority/share-of-voice${_saQs(f)}`),
  sourceAuthorityPages: (f?: SaFilters, control?: string, limit = 25) =>
    get<SourcePages>(`/source-authority/pages${_saQs(f, { control: control ?? "", limit: String(limit) })}`),
  sourceAuthorityProvenance: (responseId: string) =>
    get<ResponseProvenance>(`/source-authority/response/${encodeURIComponent(responseId)}/provenance`),
  sourceAuthorityInfluenceGraph: (
    f?: SaFilters,
    opts?: { theme?: string; focus_domain?: string; top_n?: number },
  ) =>
    get<InfluenceGraph>(
      `/source-authority/influence-graph${_saQs(f, {
        theme: opts?.theme ?? "",
        focus_domain: opts?.focus_domain ?? "",
        top_n: opts?.top_n ? String(opts.top_n) : "",
      })}`,
    ),
  sourceAuthorityInfluenceNodeEvidence: (
    nodeType: string,
    key: string,
    f?: SaFilters,
    limit = 25,
  ) =>
    get<InfluenceNodeEvidence>(
      `/source-authority/influence-graph/node-evidence${_saQs(f, {
        node_type: nodeType,
        key,
        limit: String(limit),
      })}`,
    ),
  // prompt volume intelligence (FR-116)
  promptVolumeBatches: () => get<PromptVolumeBatchList>("/prompt-volume/batches"),
  promptVolumeIntelligence: (batchId?: string) =>
    get<PromptVolumeIntelligence>(`/prompt-volume/intelligence${batchId ? `?batch_id=${encodeURIComponent(batchId)}` : ""}`),
  promptVolumeGaps: (batchId?: string) =>
    get<PromptVolumeGaps>(`/prompt-volume/gaps${batchId ? `?batch_id=${encodeURIComponent(batchId)}` : ""}`),
  promptVolumeTrend: () => get<PromptVolumeTrend>("/prompt-volume/trend"),
  promptVolumeGapAlerts: (status = "OPEN") =>
    get<PromptVolumeGapAlerts>(`/prompt-volume/gap-alerts?status=${encodeURIComponent(status)}`),
  promptVolumeGapAlertSummary: () =>
    get<PromptVolumeGapAlertSummary>("/prompt-volume/gap-alerts/summary"),
  dismissPromptVolumeGapAlert: (id: string) =>
    post<PromptVolumeGapAlert>(`/prompt-volume/gap-alerts/${encodeURIComponent(id)}/dismiss`, {}),
  syncPromptVolumeGapAlerts: () =>
    post<{ batch_id: string | null; created: number; updated: number; reopened: number; resolved: number }>(
      "/prompt-volume/gap-alerts/sync", {}),
  promptVolumeCsvUrl: (batchId?: string) =>
    `${BASE}/prompt-volume/export.csv${batchId ? `?batch_id=${encodeURIComponent(batchId)}` : ""}`,
  uploadPromptVolume: async (form: FormData): Promise<PromptVolumeUploadResult> => {
    const res = await fetch(`${BASE}/prompt-volume/upload`, { method: "POST", body: form });
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, data };
  },
  // in-app SEMrush fetch (FR-116). Preview/ingest return {ok,status,data} so the UI can show
  // real error detail (e.g. key-not-configured, expired fetch) without throwing.
  promptVolumeSemrushStatus: () => get<SemrushStatus>("/prompt-volume/semrush/status"),
  promptVolumeSemrushPreview: async (
    body: SemrushPreviewRequest,
  ): Promise<{ ok: boolean; status: number; data: any }> => {
    const res = await fetch(`${BASE}/prompt-volume/semrush/preview`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, data };
  },
  promptVolumeSemrushIngest: async (
    body: SemrushIngestRequest,
  ): Promise<{ ok: boolean; status: number; data: any }> => {
    const res = await fetch(`${BASE}/prompt-volume/semrush/ingest`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, data };
  },
  // pinpoint export
  pinpointExport: (body: any) => post<any>("/exports/pinpoint", body),
  pinpointExports: () => get<any>("/exports/pinpoint"),
  pinpointDownloadUrl: (id: string) => `${BASE}/exports/pinpoint/${id}/download`,
  // harvest (discovery)
  harvestStatus: () => get<any>("/harvest/status"),
  harvestItems: (qs = "") => get<HarvestedItem[]>(`/harvest/items${qs}`),
  harvestRun: (monitoringMode?: string) =>
    post<any>(`/harvest/run${monitoringMode ? `?monitoring_mode=${encodeURIComponent(monitoringMode)}` : ""}`),
  harvestPromote: (id: number, body: any) => post<Question>(`/harvest/items/${id}/promote`, body),
  harvestReject: (id: number, reason: string) => post<any>(`/harvest/items/${id}/reject`, { reason }),
  harvestRunToPipeline: (itemIds: number[], body?: { reviewer_name?: string | null; monitoring_mode?: string }) =>
    post<HarvestRunResult>("/harvest/run-to-pipeline", { item_ids: itemIds, ...(body || {}) }),
  // curation (coverage-driven question generation)
  curationCoverage: (scope: CurationScope = {}) =>
    get<CurationCoverage>(`/curation/coverage${curationQuery(scope)}`),
  curationGenerate: (body: CurationScope & { limit?: number; commit?: boolean }) =>
    post<CurationGenerateResult>("/curation/generate", body),
  // How far each comparison actually got. Shares curationQuery so the funnel and the
  // coverage report can never be scoped differently by accident.
  curationFunnel: (scope: CurationScope = {}) =>
    get<CoverageFunnel>(`/curation/funnel${curationQuery(scope)}`),
  // competitive head-to-head — read-only, no model calls. Every filter is a LIST; an empty
  // one is sent as nothing at all, which the API reads as "all".
  headToHead: (f: HeadToHeadFilters = {}) =>
    get<HeadToHeadBoard>(`/competitive/head-to-head${competitiveQuery({ ...f })}`),
  headToHeadDetail: (
    pairKey: string,
    f: { persona?: string[]; llm_name?: string[] } = {},
  ) =>
    get<HeadToHeadDetail>(
      `/competitive/head-to-head/detail${competitiveQuery({ pair_key: pairKey, ...f })}`,
    ),
  // social listening (multi-area + ad-hoc search — Apify)
  socialStatus: () => get<any>("/social/status"),
  socialPosts: (qs = "") => get<SocialPost[]>(`/social/posts${qs}`),
  socialComments: (postId: number) => get<SocialComment[]>(`/social/posts/${postId}/comments`),
  socialInsights: (ta = "Obesity") => get<SocialInsights>(`/social/insights?therapeutic_area=${encodeURIComponent(ta)}`),
  socialBrief: (ta = "Obesity") =>
    post<{
      brief: { status: string; verbatims?: number; analyzed?: number; reason?: string };
      platforms: { status: string; platforms?: number; reason?: string };
      unmet_questions: { status: string; questions?: number; raw?: number; reason?: string };
    }>(`/social/brief?therapeutic_area=${encodeURIComponent(ta)}`),
  // Voice-of-patient bridge: stage a community unmet-need question into Discovery for review.
  socialPromoteUnmet: (body: {
    question: string; therapeutic_area: string; brand?: string | null;
    theme?: string | null; domain?: string; persona?: string;
  }) =>
    mutate<{ status: string; id: number; harvested_status: string; ae_flag?: boolean }>(
      "POST", "/social/unmet-questions/promote", body),
  socialIngest: (channels?: string, ta = "Obesity", terms?: string) => {
    const p = new URLSearchParams();
    if (channels) p.set("channels", channels);
    if (ta) p.set("therapeutic_area", ta);
    if (terms) p.set("terms", terms);
    // `mutate` surfaces the server's `detail` verbatim so the relevance-gate rejection
    // message (422 on an off-topic ad-hoc query) is shown to the analyst.
    return mutate<{ status: string }>("POST", `/social/ingest?${p.toString()}`);
  },
  // geo
  geoBrands: () => get<any>("/geo/brands"),
  geoSchema: (brand: string) => get<any>(`/geo/schema/${brand}`),
  // openevidence (manual capture bridge — Provider persona only)
  oeRuns: () => get<OERunSummary[]>("/openevidence/runs"),
  oeWorklist: (runId: string) => get<OEWorklist>(`/openevidence/worklist?run_id=${runId}`),
  oeCapture: (body: OECaptureBody) => post<any>("/openevidence/capture", body),
  oeFinalizeRun: (runId: string) => post<any>(`/openevidence/runs/${runId}/finalize`, {}),
  // snowflake + cortex
  snowflakeStatus: () => get<SnowflakeStatus>("/snowflake/status"),
  snowflakeSync: () => post<{ status: string }>("/snowflake/sync"),
  cortexInsights: (force = false) => get<CortexInsights>(`/cortex/insights${force ? "?force=true" : ""}`),
  cortexAsk: (question: string) => post<CortexAnswer>("/cortex/ask", { question }),
  cortexAgentStatus: () => get<{ enabled: boolean }>("/cortex/agent/status"),
  cortexChat: (message: string, history: ChatMessage[] = []) =>
    post<CortexChatReply>("/cortex/chat", { message, history }),
  // copilot agent (always-on, Bedrock-backed assistant)
  copilotHealth: () => get<CopilotHealth>("/copilot/health"),
  copilotConfirm: (body: CopilotConfirmBody) =>
    post<CopilotConfirmResult>("/copilot/confirm", body),
  copilotPreview: (body: CopilotPreviewBody) =>
    post<CopilotPendingAction>("/copilot/preview", body),
  copilotJobStatus: (kind: string, runId?: string) =>
    get<CopilotJobStatus>(
      `/copilot/job?kind=${encodeURIComponent(kind)}${runId ? `&run_id=${encodeURIComponent(runId)}` : ""}`,
    ),
  // model release event correlation (FR-707a) — updates are auto-detected, no manual create
  modelReleases: (platform?: string) =>
    get<ModelRelease[]>(`/model-releases${platform ? `?target_platform=${encodeURIComponent(platform)}` : ""}`),
  driftTimeline: (platform?: string) =>
    get<DriftTimeline>(`/model-releases/drift-timeline${platform ? `?target_platform=${encodeURIComponent(platform)}` : ""}`),
  correlationRatio: () => get<CorrelationRatio>("/model-releases/correlation-ratio"),
  responseDrifts: (platform?: string, limit = 100) =>
    get<ResponseDriftItem[]>(`/model-releases/drifts?limit=${limit}${platform ? `&target_platform=${encodeURIComponent(platform)}` : ""}`),
  responseDriftDetail: (id: number) => get<ResponseDriftDetail>(`/model-releases/drifts/${id}`),
  // FR-707a vendor version + changelog capture
  liveVersions: () => get<LiveVersion[]>("/model-releases/versions"),
  versionImpact: (platform?: string) =>
    get<VersionImpact[]>(`/model-releases/version-impact${platform ? `?target_platform=${encodeURIComponent(platform)}` : ""}`),
  modelUpdateSyncStatus: () => get<ModelUpdateSyncStatus>("/model-releases/sync-status"),
  modelUpdateSync: () => post<ModelUpdateSyncResult>("/model-releases/sync"),
  // stakeholder digests (BR-008a)
  digestProfiles: () => get<DigestProfile[]>("/digests/profiles"),
  createDigestProfile: (body: DigestProfileCreate) => post<DigestProfile>("/digests/profiles", body),
  updateDigestProfile: (id: number, body: Partial<DigestProfileCreate>) =>
    put<DigestProfile>(`/digests/profiles/${id}`, body),
  deleteDigestProfile: (id: number) => del(`/digests/profiles/${id}`),
  runDigest: (id: number) => post<DigestRun>(`/digests/profiles/${id}/run`),
  digestRuns: (profileId?: number) =>
    get<DigestRun[]>(`/digests/runs${profileId ? `?profile_id=${profileId}` : ""}`),
  digestHtmlUrl: (runId: number) => `${BASE}/digests/runs/${runId}/html`,
  digestPdfUrl: (runId: number) => `${BASE}/digests/runs/${runId}/pdf`,
  sesCheck: () => get<SesStatus>("/digests/ses-check"),
  digestWorkshopInsights: (scope: "workshop" | "all" = "workshop") =>
    get<WorkshopInsightsResponse>(`/digests/workshop-insights?scope=${scope}`),
  // health
  healthTargets: () => get<any>("/health/targets"),
  exportUrl: (format: string, qs = "") => `${BASE}/responses/export?format=${format}${qs}`,

  // evidence store (X2) — read-only
  evidenceOverview: () => get<EvidenceOverview>("/evidence/overview"),
  evidenceNetworks: (f?: { indication?: string; ratification_status?: string }) =>
    get<EvidenceNetworkList>(`/evidence/networks${_evQs(f)}`),
  evidenceNetwork: (networkId: string) =>
    get<EvidenceNetworkDetail>(`/evidence/networks/${encodeURIComponent(networkId)}`),
  evidenceStudies: (f?: {
    indication?: string;
    verification_status?: string;
    treatment?: string;
  }) => get<EvidenceStudyList>(`/evidence/studies${_evQs(f)}`),
  evidenceStudy: (studyId: string) =>
    get<EvidenceStudyDetail>(`/evidence/studies/${encodeURIComponent(studyId)}`),
  evidenceDrugFacts: (f?: { brand?: string; current_only?: string }) =>
    get<EvidenceDrugFactList>(`/evidence/drug-facts${_evQs(f)}`),

  // evidence governance (X1 surface) — protocols + network ratification
  evidenceProtocols: () => get<EvidenceProtocol[]>("/evidence-review/protocols"),
  evidenceProtocol: (protocolId: string) =>
    get<EvidenceProtocol & { definition: Record<string, any> }>(
      `/evidence-review/protocols/${encodeURIComponent(protocolId)}`,
    ),
  evidenceNetworkGate: (networkId: string) =>
    get<EvidenceNetworkGate>(
      `/evidence-review/networks/${encodeURIComponent(networkId)}/gate`,
    ),

  // Recording a decision, as opposed to reading one. `mutate` rather than `post` for the same
  // reason as the curator check: every refusal on these routes is a sentence a reviewer needs
  // to read — a rejection with no note, a review stage taken out of order, a revocation with
  // no active approval to withdraw — and a bare status code throws that away.
  //
  // None of these send a content hash. The server derives it, which is what stops a client
  // signing off on content other than what is on disk.
  recordProtocolDecision: (
    protocolId: string,
    body: {
      approval_role: "MEDICAL" | "STATISTICAL";
      decision: "APPROVED" | "REJECTED";
      reviewer_id: string;
      review_note?: string;
    },
  ) =>
    mutate<EvidenceProtocol & { approval_id: string }>(
      "POST",
      `/evidence-review/protocols/${encodeURIComponent(protocolId)}/decisions`,
      body,
    ),
  revokeProtocolDecision: (
    protocolId: string,
    body: {
      approval_role: "MEDICAL" | "STATISTICAL";
      revoked_by: string;
      revocation_reason: string;
    },
  ) =>
    mutate<EvidenceProtocol>(
      "POST",
      `/evidence-review/protocols/${encodeURIComponent(protocolId)}/revocations`,
      body,
    ),
  submitNetwork: (networkId: string, body: { submitted_by: string }) =>
    mutate<EvidenceNetworkRatification>(
      "POST",
      `/evidence-review/networks/${encodeURIComponent(networkId)}/submit`,
      body,
    ),
  // One function for both stages: they take an identical body and differ only in which the
  // state machine will accept, so `allowed_transitions` decides that, not the caller.
  reviewNetwork: (
    networkId: string,
    stage: "medical" | "statistical",
    body: { reviewer: string; approve: boolean; note?: string },
  ) =>
    mutate<EvidenceNetworkRatification>(
      "POST",
      `/evidence-review/networks/${encodeURIComponent(networkId)}/${stage}-review`,
      body,
    ),
  // The way out of a frozen network, and the reason `allowed_transitions` stopped being a
  // list of moves the API could not make. Not supersede: no snapshot of the approved
  // evidence set survives, so this is for an approval that should not have happened.
  reopenNetwork: (networkId: string, body: { reopened_by: string; reason: string }) =>
    mutate<EvidenceNetworkRatification>(
      "POST",
      `/evidence-review/networks/${encodeURIComponent(networkId)}/reopen`,
      body,
    ),

  // study curation — a data-accuracy check, deliberately not a clinical review
  studySourceCheck: (studyId: string) =>
    get<StudySourceCheck>(
      `/evidence-review/studies/${encodeURIComponent(studyId)}/source-check`,
    ),
  // `mutate`, not `post`: a refusal here explains why (stale rows, missing document) and
  // that explanation is the whole value of the check. `post` reduces it to a status code.
  recordCuratorCheck: (studyId: string, body: { verified_by: string; note?: string }) =>
    mutate<{ study_id: string; verification_status: string; verified_by: string }>(
      "POST",
      `/evidence-review/studies/${encodeURIComponent(studyId)}/curator-check`,
      body,
    ),
  curationQueue: (f?: {
    network_id?: string;
    indication?: string;
    verification_status?: string;
  }) => get<CurationQueue>(`/evidence-review/studies${_evQs(f)}`),
  rejectStudy: (studyId: string, body: { rejected_by: string; reason: string }) =>
    mutate<{ study_id: string; verification_status: string; rejection_reason: string }>(
      "POST",
      `/evidence-review/studies/${encodeURIComponent(studyId)}/reject`,
      body,
    ),

  // network membership (Lifecycle 2). The preview is what a UI must show BEFORE the first
  // inclusion: with nothing INCLUDED membership narrows nothing, and including one study
  // binds the filter so the rest stop contributing.
  membershipPreview: (networkId: string) =>
    get<MembershipPreview>(
      `/evidence-review/networks/${encodeURIComponent(networkId)}/memberships`,
    ),
  decideMembership: (
    networkId: string,
    studyId: string,
    body: { decision: string; decided_by: string; reason?: string; note?: string },
  ) =>
    mutate<MembershipDecision>(
      "POST",
      `/evidence-review/networks/${encodeURIComponent(networkId)}/memberships/` +
        `${encodeURIComponent(studyId)}/decision`,
      body,
    ),

  // drug-fact curation — the gate Phase 7/8/9 all wait on
  drugFactQueue: (f?: { brand?: string; verification_status?: string }) =>
    get<DrugFactQueue>(`/evidence-review/drug-facts${_evQs(f)}`),
  drugFactSourceCheck: (factId: string) =>
    get<DrugFactSourceCheck>(
      `/evidence-review/drug-facts/${encodeURIComponent(factId)}/source-check`,
    ),
  recordDrugFactCheck: (factId: string, body: { verified_by: string; note?: string }) =>
    mutate<{ fact_id: string; brand: string; verification_status: string }>(
      "POST",
      `/evidence-review/drug-facts/${encodeURIComponent(factId)}/curator-check`,
      body,
    ),

  // evidence ingestion — the three corpus-growing routines, off the shell.
  // The starts use `mutate`, not `post`: a 409 (a job is already running) and a 422 (an
  // outcome that is not this indication's) both carry the explanation in `detail`, and that
  // explanation is the whole reason validation happens at submit rather than in the job.
  evidenceIngestOptions: () => get<IngestOptions>("/evidence-ingestion/options"),
  evidenceIngestStatus: () => get<IngestJobStatus>("/evidence-ingestion/status"),
  evidenceIngestTrials: (body: IngestTrialsBody) =>
    mutate<IngestStarted>("POST", "/evidence-ingestion/trials", body),
  evidenceIngestDrugFacts: (body: { brands?: string[]; commit?: boolean }) =>
    mutate<IngestStarted>("POST", "/evidence-ingestion/drug-facts", body),
  evidenceIngestReparse: (body: {
    indication?: string | null;
    study_ids?: string[];
    commit?: boolean;
  }) => mutate<IngestStarted>("POST", "/evidence-ingestion/reparse", body),

  // comparisons (Phase 6)
  comparisonMatrix: (networkId: string, executionMode = "EXPLORATORY") =>
    get<ComparisonMatrix>(
      `/comparisons/matrix?network_id=${encodeURIComponent(networkId)}` +
        `&execution_mode=${executionMode}`,
    ),

  // competitor discovery (Phase 5, Tier A)
  discoveryReasons: () => get<DiscoveryReasonVocabulary>("/competitor-discovery/reasons"),
  discoverySweep: (indication?: string) =>
    post<DiscoverySweepReport>(
      `/competitor-discovery/sweep${indication ? `?indication=${encodeURIComponent(indication)}` : ""}`,
    ),
  discoveryCandidates: (f?: { indication?: string; review_status?: string }) =>
    get<CompetitorCandidateList>(`/competitor-discovery/candidates${_evQs(f)}`),
  discoveryReview: (
    candidateId: string,
    body: { decision: string; reviewer: string; note?: string },
  ) =>
    mutate<CompetitorCandidate>(
      "POST",
      `/competitor-discovery/candidates/${encodeURIComponent(candidateId)}/review`,
      body,
    ),
  discoveryConfigProposal: (indication?: string) =>
    get<DiscoveryConfigProposal>(
      `/competitor-discovery/config-proposal${indication ? `?indication=${encodeURIComponent(indication)}` : ""}`,
    ),
  discoveryConfigApplied: (candidateIds: string[], appliedBy: string) =>
    mutate<{ applied: string[]; missing: string[] }>(
      "POST",
      "/competitor-discovery/config-applied",
      { candidate_ids: candidateIds, applied_by: appliedBy },
    ),

  // claim-level AI-vs-evidence evaluation (Phase 8)
  claimVocabulary: () => get<ClaimVocabulary>("/claim-evaluation/vocabulary"),
  alignmentReport: (f?: { run_id?: string; llm_name?: string; indication?: string }) =>
    get<AlignmentReport>(`/claim-evaluation/alignment${_evQs(f)}`),
  claimsForResponse: (responseId: string) =>
    get<{ response_id: string; claim_count: number; claims: EvaluationClaimRow[] }>(
      `/claim-evaluation/responses/${encodeURIComponent(responseId)}/claims`,
    ),
  evaluateResponseClaims: (responseId: string, commit = true) =>
    post<Record<string, any>>(
      `/claim-evaluation/responses/${encodeURIComponent(responseId)}?commit=${commit}`,
    ),
  evaluateRunClaims: (runId: string, limit?: number) =>
    mutate<ClaimEvaluationRunResult>(
      "POST",
      `/claim-evaluation/runs/${encodeURIComponent(runId)}` +
        (limit ? `?limit=${limit}` : ""),
    ),

  // evidence synthesis + strategic implications (Phase 9)
  evidenceSynthesis: (indication: string, networkId?: string) =>
    get<EvidenceSynthesis>(
      `/evidence-questions/synthesis?indication=${encodeURIComponent(indication)}` +
        (networkId ? `&network_id=${encodeURIComponent(networkId)}` : ""),
    ),
  strategicImplications: () =>
    get<{ sources: string[]; implications: StrategicImplicationMeta[] }>(
      "/recommendations/implications",
    ),
  discoveryClassMap: (indication: string) =>
    get<DiscoveryClassMap>(
      `/competitor-discovery/class-map?indication=${encodeURIComponent(indication)}`,
    ),
};

/** Query string builder that drops empty values, so a cleared filter is absent not blank. */
function _evQs(filters?: Record<string, string | undefined>): string {
  if (!filters) return "";
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value) params.set(key, value);
  }
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

/**
 * Stream a copilot turn over Server-Sent Events. EventSource cannot POST, so we
 * read the fetch ReadableStream and parse `event:`/`data:` frames by hand.
 */
export async function* copilotStream(
  body: CopilotChatBody,
  signal?: AbortSignal,
): AsyncGenerator<CopilotStreamEvent> {
  const res = await fetch(`${BASE}/copilot/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(`Copilot stream failed: ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const evt = parseSseFrame(frame);
        if (evt) yield evt;
      }
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      /* already released */
    }
  }
}

function parseSseFrame(frame: string): CopilotStreamEvent | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!dataLines.length) return null;
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) } as CopilotStreamEvent;
  } catch {
    return null;
  }
}
