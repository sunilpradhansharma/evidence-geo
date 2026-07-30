"""Application settings loaded from environment variables and YAML config."""
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_DIR = Path(__file__).parent
PROJECT_ROOT = CONFIG_DIR.parent.parent.parent


class Settings(BaseSettings):
    """Environment-driven settings."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # AWS
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-2"

    # Database
    database_url: str = "sqlite+aiosqlite:///./evidence_monitoring.db"

    # ===== PHI / PII guardrails (G2) =====
    # Detection backend for the central compliance.phi module:
    #   "regex"              — fast, dependency-free direct-identifier layer (default)
    #   "comprehend_medical" — adds AWS Comprehend Medical NLP (names/geo/contextual PHI)
    # Comprehend Medical incurs AWS cost + needs comprehendmedical:DetectPHI permission.
    phi_detection_backend: str = "regex"
    phi_comprehend_region: str = ""  # blank = fall back to aws_region

    # Bedrock model IDs (us-east-2 inference profiles — newer models require the us. prefix)
    target_claude_model_id: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    target_nova_model_id: str = "us.amazon.nova-2-lite-v1:0"
    target_llama_model_id: str = "us.meta.llama3-3-70b-instruct-v1:0"
    orchestrator_model_id: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    scoring_model_id: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

    # ===== Copilot agent (application-wide assistant chat) =====
    # The LangGraph copilot that powers the floating chat bubble. Backed by
    # Bedrock Converse (tool-use), always-on (not Snowflake-gated). When
    # copilot_model_id is blank it falls back to orchestrator_model_id (the
    # Claude Sonnet inference profile already used elsewhere — must support
    # Converse toolConfig). copilot_action_secret signs confirmed-write tokens;
    # blank uses a per-process random secret (fine for the single-process POC).
    copilot_enabled: bool = True
    copilot_model_id: str = ""
    copilot_action_secret: str = ""
    copilot_react_iter_cap: int = 6

    # Run config
    max_tokens_per_run: int = 500000
    # How many questions a run processes concurrently. Each question still fans out to
    # all its targets in parallel; this bounds how many questions are in flight at once.
    # The slow, network-bound LLM work overlaps across questions while DB writes stay
    # serialized (SQLite is single-writer). 1 = legacy sequential behavior.
    # NOTE: every provider call runs in a worker thread (asyncio.to_thread), so the shared
    # thread pool MUST be sized for max_concurrent_questions * (# targets); the pool is
    # auto-sized from this value at startup (see thread_pool_max_workers below). Env:
    # MAX_CONCURRENT_QUESTIONS.
    max_concurrent_questions: int = 8
    # Per-call wall-clock timeout (seconds) for a single target LLM request. Without it a
    # provider whose socket stalls with no response blocks the awaiting coroutine forever,
    # freezing the whole run (the question never commits and its concurrency slot never
    # frees). asyncio.wait_for bounds each call so a hung target becomes a FAILED response.
    # Generous by default so legitimately slow grounded calls (Gemini Search / GPT-4o
    # web-search, ~30-90s) are not cut off. Env: TARGET_CALL_TIMEOUT_SECONDS.
    target_call_timeout_seconds: int = 120
    # Size of the shared worker-thread pool backing every blocking provider SDK call (all
    # provider clients dispatch via asyncio.to_thread). Python's DEFAULT executor is only
    # min(32, cpu_count + 4) threads — far below a full run's demand (max_concurrent_questions
    # * # targets, plus intent/Chairman/scoring). When it saturates, calls queue with no
    # timeout and large runs appear "stuck". 0 = auto-size at startup from the run config.
    # Env: THREAD_POOL_MAX_WORKERS.
    thread_pool_max_workers: int = 0
    # How many responses the post-run scoring pass scores CONCURRENTLY. The (slow) scoring
    # LLM calls run in parallel bounded by this; DB writes stay serialized on one session.
    # Env: MAX_CONCURRENT_SCORING.
    max_concurrent_scoring: int = 8
    # Daily scheduled-run cadence. The cron expression is evaluated in
    # schedule_timezone (DST-aware), so "0 0 * * *" = midnight Dallas time.
    default_schedule_cron: str = "0 0 * * *"
    schedule_timezone: str = "America/Chicago"
    # Marker file scripts/ec2_deploy.sh creates while a deploy is staging. While it exists
    # the API refuses to START runs, because the container swap at the end of the deploy
    # kills anything in flight. Blank = derive it from the SQLite data directory, which is
    # the host bind-mount the deploy writes to (so prod needs no .env change).
    # Env: DEPLOY_LOCK_PATH.
    deploy_lock_path: str = ""

    # ===== AI Prompt Volume Intelligence (FR-116) =====
    # Third-party SEARCH-DEMAND data (Semrush/Ahrefs) uploaded as a CSV and used as a
    # PROXY for AI-inquiry demand. A gap "topic" is flagged as high-volume when its
    # combined volume clears the absolute floor OR sits in the top-percentile of the
    # upload. Matching uses normalized token-overlap (Jaccard) at the given threshold.
    prompt_volume_abs_volume_floor: int = 500       # combined-volume floor for a gap topic
    prompt_volume_top_percentile: float = 0.20      # OR top 20% of the upload by volume
    prompt_volume_match_threshold: float = 0.5      # min Jaccard to treat a query as covered
    prompt_volume_max_upload_mb: int = 10           # reject uploads larger than this
    prompt_volume_gap_alert_limit: int = 25         # max NEW gap alerts per upload (anti-fatigue)
    # In-app SEMrush fetch (pull questions + related keywords straight from the Analytics API
    # instead of a manual CSV). Reuses semrush_api_key/base_url/database below. Cost guards:
    # each seed x report returns up to per_seed_limit BILLED lines; max_seeds caps the fetch.
    prompt_volume_semrush_per_seed_limit: int = 25  # rows per report per seed (SEMrush display_limit)
    prompt_volume_semrush_max_seeds: int = 40        # cap total seeds sent per fetch (cost guard)
    prompt_volume_semrush_reports: str = "both"      # questions | related | both

    # FR-707a: lookback window (days) for correlating material drift with a logged
    # vendor model release. A material change within this many days of a release for
    # the same platform is flagged as a possible model update.
    model_release_lookback_days: int = 30

    # FR-707a auto-detect: minimum number of material response drifts a single platform
    # must show on one day for the system to auto-log a "Detected model update" event
    # (no manual logging). Keep low for the POC's modest data volumes.
    model_update_min_drifts: int = 3

    # ===== FR-707a Vendor version + changelog capture (opt-in sync) =====
    # Anchors AI Update Impact to REAL vendor model versions instead of only inferring
    # updates from response-drift spikes. Two independent signals feed the model_release_log:
    #   1. Our own traffic — a change in Response.llm_model_version for a target is a real
    #      version transition (source="api"); needs NO network and works with sync disabled.
    #   2. Vendor changelogs — the pages/feeds below are fetched and an LLM extracts
    #      (version, effective_date, summary) to enrich each event (source="changelog").
    # OPT-IN (enable-with-creds, like Snowflake/SES): with model_update_sync_enabled=false the
    # changelog fetch is a safe no-op and the tab still works from the traffic signal alone.
    # get_settings() is lru_cached → restart the backend after changing a flag/URL.
    model_update_sync_enabled: bool = False
    # Daily background changelog sync via the in-process APScheduler (no-op when disabled).
    model_update_sync_scheduler_enabled: bool = True
    # Hour (UTC, 0-23) at which the daily changelog sync fires.
    model_update_sync_hour_utc: int = 6
    # Confirmed public changelog / "what changed" sources per vendor (2026-07-12). Blank a URL
    # to skip that vendor. OpenEvidence / EvidenceMD have no public changelog → drift-inferred.
    model_update_openai_changelog_url: str = "https://developers.openai.com/api/docs/changelog"
    model_update_anthropic_changelog_url: str = "https://docs.anthropic.com/en/release-notes/api"
    model_update_google_changelog_url: str = "https://ai.google.dev/gemini-api/docs/changelog"
    model_update_aws_whatsnew_rss_url: str = "https://aws.amazon.com/about-aws/whats-new/recent/feed/"
    # A changelog entry is only auto-applied when the LLM's extraction confidence clears this.
    model_update_extract_min_confidence: float = 0.60
    # Max changelog entries to keep per vendor per sync (most-recent first; anti-noise).
    model_update_max_entries_per_vendor: int = 25

    # High-impact model-update alerting: a captured update is "high impact" (worth an alert
    # + a dedicated digest section) when it flips >= this many tracked answers OR drops mean
    # brand sentiment by >= this magnitude across the version boundary. Emits an immutable
    # audit signal once per event and surfaces it in the stakeholder digest.
    model_update_high_impact_alert_enabled: bool = True
    model_update_high_impact_min_questions: int = 3
    model_update_high_impact_sentiment_drop: float = 0.15

    # ===== BR-008a Stakeholder-Differentiated Digests =====
    # Digests are always generated + stored in-app. Email delivery via AWS SES is
    # OPT-IN (enable-with-creds, like Snowflake/Apify): with ses_enabled=false OR a
    # blank ses_sender, the email step is a safe no-op and the digest still lands
    # in-app + optional webhook.
    #
    # PRODUCTION (no per-recipient verification): request SES *production access* to
    # leave the sandbox, and verify the sending DOMAIN with DKIM. ses_sender should
    # then be an address on that verified domain (e.g. ema@yourdomain.com) so every
    # From address works without individually verifying each address or recipient.
    digest_enabled: bool = True
    digest_scheduler_enabled: bool = True   # register the weekly APScheduler jobs
    ses_enabled: bool = False
    ses_sender: str = ""                     # address on a DKIM-verified domain, e.g. ema@yourdomain.com
    ses_region: str = ""                     # blank = fall back to aws_region
    # How many days of alerts each weekly digest covers.
    digest_lookback_days: int = 7
    # Where generated PDF artifacts are written (blank = <project_root>/data/digests).
    digest_output_dir: str = ""

    # ===== NMA sidecar (Phase 6) =====
    # The R `netmeta` service. BLANK means "not deployed", and that is a supported state:
    # any comparison whose protocol selects a full NMA then resolves to
    # NMA_SERVICE_UNAVAILABLE — a retryable SERVICE status, never a structured evidence gap,
    # so an absent sidecar can never be mistaken for a finding about the evidence.
    # Bucher and direct pairwise pooling run in-process and are unaffected.
    #
    # SUPPORTED IS NOT THE SAME AS SUFFICIENT. On the corpus we actually have, the only
    # network carries a multi-arm trial, so `select_engine` returns NETMETA for EVERY pair
    # and a blank URL means nothing resolves at Level 3 at all. `scripts/ec2_deploy.sh`
    # therefore runs the sidecar by default and injects this value; blank is the right
    # default for tests and local dev, not for a deployed box.
    nma_sidecar_url: str = ""
    nma_sidecar_timeout_seconds: float = 120.0

    # ===== Evidence ingestion from the UI =====
    # Puts scripts/ingest_evidence.py, scripts/ingest_drug_facts.py and the offline re-parse
    # behind /evidence-ingestion so growing the corpus stops requiring shell access to the
    # prod container. THIS IS THE ONLY GATE ON A WRITE SURFACE: there is no RBAC in this
    # tree, so anyone who can reach the UI can spend the external API budget (ClinicalTrials
    # .gov / openFDA) and write to the evidence corpus. Set false to force the CLI path.
    # It cannot verify anything — verification stays one study at a time on
    # /evidence-review/studies/{id}/curator-check — so the blast radius is unreviewed
    # EXTRACTED/MAPPED rows and PROPOSED memberships, never a signed-off fact.
    # get_settings() is lru_cached → restart the backend after changing this.
    evidence_ingestion_api_enabled: bool = True

    # Server
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # ===== Snowflake (warehouse mirror + Cortex insights) =====
    # The backend mirrors all operational data into Snowflake and uses Cortex for
    # additional insights + a natural-language Q&A. Only the backend connects to
    # Snowflake (one service identity); end-users never authenticate to Snowflake.
    # When snowflake_enabled=false (or creds missing) every Snowflake call is a no-op,
    # so the app runs unchanged without a warehouse.
    snowflake_enabled: bool = False
    snowflake_account: str = ""       # e.g. ab12345.us-east-1
    snowflake_user: str = ""          # service user, e.g. EVIDENCE_SVC
    snowflake_role: str = ""          # e.g. EVIDENCE_APP_ROLE
    snowflake_warehouse: str = ""     # e.g. EVIDENCE_WH
    snowflake_database: str = ""      # e.g. EVIDENCE_DB
    snowflake_schema: str = "PUBLIC"
    # Key-pair auth (recommended for the unattended prod backend — no MFA prompts).
    # Provide a path to the .p8 OR the base64 of the .p8 (preferred for cloud deploy).
    snowflake_private_key_path: str = ""
    snowflake_private_key_b64: str = ""
    snowflake_private_key_passphrase: str = ""  # blank if the key is unencrypted
    # Optional password fallback (POC only; key-pair preferred for prod).
    snowflake_password: str = ""
    # Cortex
    snowflake_cortex_model: str = "claude-3-5-sonnet"  # COMPLETE/AI model; region-dependent
    snowflake_cortex_analyst_enabled: bool = True
    # Cortex Agent chat widget (Cortex Analyst REST API over a native Semantic View).
    # Powers the global "Cortex Agent" chat bubble; answers are narrated to plain English
    # so non-technical users never see SQL. Disable to hide the widget's data path.
    snowflake_cortex_agent_enabled: bool = True
    # Fully-qualified native Semantic View the agent queries (DB.SCHEMA.VIEW).
    snowflake_semantic_view: str = "EVIDENCE_DB.PUBLIC.EVIDENCE_SEMANTIC_VIEW"
    # Optional: a staged semantic-model YAML (e.g. @EVIDENCE_DB.PUBLIC.SEMANTIC_MODELS/
    # cortex_semantic_model.yaml). When set, the agent uses this COMPLETE model instead
    # of the native semantic_view above — preferred, since the repo YAML covers
    # sentiment/brand/positioning/alerts/runs/consensus, not just consensus.
    snowflake_semantic_model_file: str = ""
    # Capture every API request/response into the Snowflake APP_EVENTS table.
    snowflake_capture_events: bool = True

    # OpenAI — API key auth. Web-search grounding returns real source URLs per response.
    openai_api_key: str = ""
    target_openai_model_id: str = "gpt-4o"

    # ===== Anthropic (Claude target — direct API with web-search citations) =====
    # When anthropic_api_key is set, the monitored `claude` target auto-switches from AWS
    # Bedrock (parametric, no citations) to the direct Anthropic API (api.anthropic.com) with
    # the native web_search server tool, returning real source citations like Gemini/GPT-4o
    # (registry.load_targets does the swap + injects `grounding`). A blank key keeps `claude`
    # on Bedrock exactly as today. Orchestrator + scoring ALWAYS stay on Bedrock. The model id
    # is the BARE Anthropic name (no "us." inference-profile prefix). get_settings() is
    # lru_cached -> restart the backend after changing the key.
    anthropic_api_key: str = ""
    target_claude_anthropic_model_id: str = "claude-sonnet-4-5-20250929"
    anthropic_base_url: str = ""  # optional endpoint override; blank = SDK default
    anthropic_web_search_max_uses: int = 5  # cap web searches per response (cost guard)

    # EvidenceMD — OpenAI-COMPATIBLE clinical-reasoning API (separate product from the
    # manual OpenEvidence tool). Uses the openai SDK pointed at evidencemd_base_url via
    # the Chat Completions endpoint. Enabled ONLY when evidencemd_api_key is set (opt-in,
    # like OpenAI/Google); a blank key makes the target a safe no-op. Responses may carry
    # peer-reviewed citations, parsed best-effort into ProviderResult.sources.
    evidencemd_api_key: str = ""
    evidencemd_base_url: str = "https://evidencemd.ai/api/v1"
    target_evidencemd_model_id: str = "evidencemd-pro"

    # Google Gemini — API key auth (preferred) or Vertex AI service-account
    google_api_key: str = ""
    google_cloud_project: str = ""
    google_cloud_location: str = "us-central1"
    google_application_credentials: str = ""
    target_gemini_model_id: str = "gemini-2.5-flash"

    # ===== GEO schema data layer (Chairman ground-truth) =====
    # The verified brand corpus is generated from curated per-brand YAML under
    # config/geo/source/ into JSON-LD schema/*.json + llms.txt (scripts.generate_geo_schema).
    # openFDA (free public FDA drug-label API — https://open.fda.gov, no key required; an
    # optional key only raises rate limits) SEEDS label-derived fields (manufacturer, route,
    # boxed warning, indications/adverse-reaction/dosing text, SPL effective date). Curated
    # YAML values always OVERRIDE the API seed; the fetcher NEVER raises (offline-safe -> the
    # generator just skips seeding). get_settings() is lru_cached -> restart after changes.
    openfda_api_key: str = ""
    openfda_base_url: str = "https://api.fda.gov"
    # Periodic auto-refresh: the in-process APScheduler re-runs the generator (re-seeding
    # label fields from openFDA) every geo_refresh_interval_days, then hot-reloads the cache.
    # Offline-safe: a failed refresh is logged and the last-good corpus keeps serving. Set
    # geo_refresh_enabled=false to rely only on the committed corpus + manual /api/geo/refresh.
    geo_refresh_enabled: bool = True
    geo_refresh_interval_days: int = 7

    # Advanced analytics / Pinpoint export — where generated corpora are written.
    # Empty = <project_root>/exports.
    export_dir: str = ""

    # Tavily web-search question harvester (Discovery feature).
    # Harvests real user-asked questions from public health communities via the
    # Tavily AI-search API. Empty key = feature disabled until a key is provided.
    tavily_api_key: str = ""
    tavily_base_url: str = "https://api.tavily.com"

    # ===== SEMrush SEO enrichment (GEO Intervention Recommendations — BR-012) =====
    # Domain authority (Authority Score) + keyword search-volume metrics that ground the
    # recommendation engine's LLM reasoning and drive impact ranking. Requires SEMrush
    # *Analytics API* access (billed in API units) — not a UI-only/Trends login. When the
    # key is blank (or any live call fails), the remediation engine falls back to a
    # deterministic stub so the feature runs offline + in tests. get_settings() is
    # lru_cached → restart the backend after changing the key.
    semrush_api_key: str = ""
    semrush_base_url: str = "https://api.semrush.com"
    semrush_database: str = "us"  # SEMrush regional database for keyword metrics

    # ===== Source Authority Mapping (FR-706a) — RDAP + optional LLM enrichment =====
    # The curated taxonomy in config/source_authority.yaml (+ brands.yaml-derived AbbVie /
    # competitor domains) is the SOURCE OF TRUTH for classifying a cited domain. For domains
    # NOT in the taxonomy we add best-effort, offline-safe signals:
    #   • RDAP (rdap_base_url)  → registrant org / registrar / registration date / record
    #     visibility. Free, no API key; the ICANN-mandated successor to WHOIS.
    #   • LLM classifier (source_authority_llm_enabled) → classifies an UNCURATED domain from
    #     evidence (optional homepage metadata + the RDAP registrant org) into the authority
    #     buckets, with a confidence. Reuses the configured scoring model, so needs no extra
    #     key. At/above ..._auto_confidence it auto-applies; the ..._apply_min_confidence..auto
    #     band applies but is flagged requires_review; below that nothing is applied.
    # WhoisXML stays OPTIONAL: a registration FALLBACK used only when RDAP returns no data (a
    # few ccTLDs) and only if a key is set. With everything unset/failing, enrichment returns
    # NULLS (never a fabricated owner) and the curated taxonomy alone classifies.
    # get_settings() is lru_cached → restart the backend after changing a key or flag.
    rdap_base_url: str = "https://rdap.org/domain"
    source_authority_llm_enabled: bool = True
    source_authority_fetch_metadata: bool = True
    source_authority_llm_apply_min_confidence: float = 0.70
    source_authority_llm_auto_confidence: float = 0.90
    # WhoisXML (optional registration fallback + legacy categorization product).
    whoisxml_api_key: str = ""
    whoisxml_base_url: str = "https://www.whoisxmlapi.com/whoisserver/WhoisService"
    whoisxml_categorization_api_key: str = ""
    whoisxml_categorization_base_url: str = "https://website-categorization.whoisxmlapi.com/api/v3"
    # Days a domain's cached classification/enrichment stays fresh before a lazy refresh.
    source_authority_enrichment_ttl_days: int = 30

    # ===== Apify social-listening scrapers (Social Listening surface) =====
    # Apify Actors scrape public posts from Reddit/TikTok/Instagram/Facebook/X for the
    # Obesity/GLP-1 Social Listening demo. `apify_enabled` is the MASTER on/off switch
    # (ON by default; flip off via APIFY_ENABLED=false for future development). Live
    # fetch ADDITIONALLY requires `apify_api_token`; with it blank the surface is wired
    # but returns nothing. Author identity is never persisted (handles dropped at scrub).
    # Internal demo — Legal/Privacy/PV sign-off required before any production use.
    apify_enabled: bool = True
    apify_api_token: str = ""
    apify_base_url: str = "https://api.apify.com"

    # ===== OpenEvidence unattended browser automation =====
    # OpenEvidence has no public API and is HCP-gated, so "automation" means driving
    # the real web UI with a headless browser (Playwright) using a seeded, reused
    # login session. The authenticated session persists in a Playwright persistent
    # context (oe_user_data_dir), so we log in once (email+password, no MFA) and reuse
    # it; re-login happens automatically when the session lapses. OFF by default.
    # NOTE: anti-blocking hardening (residential proxy, CAPTCHA) is deferred — the
    # oe_proxy_* fields are wired but optional; leave blank to connect directly.
    oe_auto_enabled: bool = False
    oe_email: str = ""
    oe_password: str = ""
    oe_base_url: str = "https://www.openevidence.com"
    oe_login_url: str = ""  # blank = navigate base_url and auto-detect the login form
    oe_ask_url: str = ""    # blank = use base_url for the prompt/composer
    oe_model_version: str = "open-evidence-web"  # llm_model_version stamped on captures
    oe_headless: bool = True
    oe_browser_channel: str = "chrome"  # real Chrome (harder to detect); "" = bundled chromium
    # Anti-detection ("stealth") hardening for the bot browser. oe_stealth layers a
    # realistic context (viewport/locale/timezone + UA-consistent headers) + JS init
    # scripts (navigator.webdriver, plugins, WebGL, chrome runtime, ...) + human-like
    # timing/typing to reduce the fingerprints OpenEvidence uses to flag automation.
    # NOTE: this lowers the odds of being flagged; it does NOT solve a CAPTCHA already
    # presented to an IP that is already flagged (use a clean/residential IP for that).
    oe_stealth: bool = True
    oe_user_agent: str = ""   # blank = the browser's real UA (safest; matches client hints)
    oe_locale: str = "en-US"
    oe_timezone_id: str = "America/Chicago"
    oe_user_data_dir: str = ""  # blank = <project_root>/.oe_session (persists the login)
    oe_nav_timeout_ms: int = 45000
    oe_answer_timeout_ms: int = 120000   # max wait for a streamed answer to finish
    oe_answer_stable_ms: int = 2500      # answer considered complete after text is stable this long
    oe_question_pause_ms: int = 1500     # polite human-like pause between questions
    # Optional residential proxy (blank = direct connection). Gives a clean IP but does
    # NOT auto-solve a CAPTCHA. Credentials may be embedded in the server URL.
    oe_proxy_server: str = ""
    oe_proxy_username: str = ""
    oe_proxy_password: str = ""
    # Bright Data (or any) "Scraping Browser": a REMOTE Chromium that AUTO-SOLVES
    # CAPTCHAs and rotates residential IPs. When set, the bot connects to it over CDP
    # (connect_over_cdp) instead of launching a local browser — the robust way past
    # OpenEvidence's anti-bot/CAPTCHA wall. Format (from the zone's "Access parameters"):
    #   wss://brd-customer-<id>-zone-<zone>:<password>@brd.superproxy.io:9222
    # The remote browser is ephemeral, so the logged-in session is saved locally
    # (oe_user_data_dir/state.json) and restored each run to avoid repeated logins.
    oe_scraping_browser_cdp: str = ""
    # Optional CSS selector overrides — tune against the live DOM via
    # `python -m scripts.oe_spike`. Blank = use the built-in best-effort candidates.
    oe_prompt_selector: str = ""
    oe_answer_selector: str = ""
    oe_citation_selector: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _interpolate(value: Any) -> Any:
    """Recursively resolve ${ENV_VAR} references inside loaded YAML."""
    if isinstance(value, str):
        def repl(match: re.Match) -> str:
            env_key = match.group(1)
            return os.environ.get(env_key, match.group(0))

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


@lru_cache
def load_yaml_config(filename: str) -> dict:
    """Load a YAML config file from the config directory with env interpolation."""
    # Ensure env vars from settings are available for interpolation
    settings = get_settings()
    for field_name, field_value in settings.model_dump().items():
        env_key = field_name.upper()
        if env_key not in os.environ and field_value is not None:
            os.environ[env_key] = str(field_value)

    path = CONFIG_DIR / filename
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return _interpolate(raw)
