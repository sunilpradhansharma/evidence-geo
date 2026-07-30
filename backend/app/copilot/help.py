"""Curated how-to / capability knowledge for the copilot.

Authored from the in-app How to Use page + the five-stage workflow. The
``get_help`` tool keyword-matches a topic against these sections; a compact
overview is also injected into the system prompt so the agent always knows
what it can do and where each capability lives.
"""
from __future__ import annotations

APP_OVERVIEW = (
    "This is the Evidence Monitoring Agent (AI Brand Intelligence / Generative "
    "Engine Optimization platform). It monitors what large language models "
    "(Claude, Gemini, Llama, Nova, GPT-4o, and EvidenceMD) say about AbbVie "
    "brands across personas (Prospect, Provider, Patient) and therapeutic areas. "
    "The workflow has five stages: (1) Discover Questions, (2) Approved Question "
    "Bank, (3) Run Analysis, (4) AI Response Review, and (5) Insights & Trends. "
    "It also has a complementary Social Listening surface "
    "that analyzes what real people say about monitored therapies on public "
    "social channels (Reddit, TikTok, Instagram, Facebook, X) — including the "
    "comments/replies on those posts (scored as a separate sentiment dimension), "
    "with non-English text auto-translated to English. The dashboard also has four "
    "intelligence surfaces: Head-to-Head (the us-vs-them scoreboard, plus how any "
    "competitor is talked about across every scored answer \u2014 a rival is never a "
    "brand_focus, so this is the only place their own sentiment lives), GEO "
    "Interventions (ranked, plain-language content "
    "recommendations for weak brand positions), Source Authority (which web domains "
    "the models cite, classified and tracked), and Prompt Volume (uploaded "
    "search-demand data as a proxy for AI-inquiry demand). It also produces "
    "Stakeholder Digests (role-specific intelligence delivered by email/webhook/in-app) "
    "with a live in-app 'AI Answer Insights' panel over the curated Workshop Questions, "
    "tracks AI Update Impact (auto-detected model releases correlated with answer drift), "
    "and supports Variation Testing (phrasing-robustness groups: a base question plus "
    "approved paraphrases run together). It also has a Clinical Evidence section: a curated "
    "corpus of randomised trials and regulatory labels, evidence networks for indirect "
    "treatment comparison, a medical/statistical governance workflow, competitor discovery, "
    "an AI-vs-Evidence claim check, and a per-indication synthesis. Two more dashboard "
    "surfaces close the loop: Activation & Impact (owned interventions created from GEO "
    "recommendations, measured before and after publication) and the Influence Graph (which "
    "sources drive which narratives and brand positions). Several one-time BACKFILL sweeps "
    "fold existing history into newly enabled capabilities."
)

# Each entry: keywords -> (title, body). Bodies are plain text so they render
# cleanly in the chat bubble.
HELP_SECTIONS: list[dict] = [
    {
        "keywords": ["overview", "what is", "capabilities", "help", "start", "get started", "workflow"],
        "title": "What this app does",
        "body": (
            APP_OVERVIEW
            + "\n\nYou can ask me to explain any page, answer questions about your "
            "data (runs, questions, responses, sentiment, alerts, insights), or DO "
            "things for you (start a run, discover questions, change the schedule, "
            "rebuild insights, override a score, and more). Anything that changes "
            "data asks you to confirm first."
        ),
    },
    {
        "keywords": ["discover", "harvest", "scrape", "find questions", "new questions", "discovery"],
        "title": "Discover Questions (/harvest)",
        "body": (
            "Discovery searches public health communities for real questions people "
            "ask about the brands, classifies them by persona / therapeutic area / "
            "domain, and stages them for review. To run it: ask me to 'discover new "
            "questions' (I will confirm, then start it and take you to the Discover "
            "Questions page to watch progress), or open /harvest and click Run. "
            "Staged items can be Promoted (creates a PENDING question for Medical-"
            "Affairs approval) or Rejected. Adverse-event items are quarantined and "
            "need pharmacovigilance sign-off before promotion. There is also a one-click "
            "'Run to Pipeline' action that promotes, APPROVES and immediately runs the "
            "selected items \u2014 it bypasses the Medical-Affairs step and leaves them "
            "approved in the bank for future runs, so it needs a named reviewer and skips "
            "anything flagged for adverse events, PII or missing fields."
        ),
    },
    {
        "keywords": ["question bank", "approve", "approval", "questions", "create question", "edit question", "csv", "import"],
        "title": "Approved Question Bank (/questions)",
        "body": (
            "This is the governed repository of questions used in runs. Questions "
            "have a persona (Prospect/Provider/Patient), therapeutic area, brand "
            "focus, and domain (Efficacy/Safety/Access/Comparative/General). New or "
            "promoted questions land as PENDING and must be APPROVED (with an "
            "approver name) before a run uses them. I can list questions, check "
            "coverage gaps, create/edit a question, approve or reject one (I will "
            "ask for the approver name), or soft-delete one with a reason. Bulk CSV "
            "import is done on the page itself."
        ),
    },
    {
        "keywords": ["run", "run analysis", "pipeline", "start run", "dry run", "cancel", "execute"],
        "title": "Run Analysis (/run-analysis)",
        "body": (
            "A run sends the approved questions to every configured AI model and "
            "stores their answers. You can filter by persona / therapeutic area / "
            "domain, or pick specific questions, and you can do a dry-run (no model "
            "calls) to preview scope. Ask me to 'start a run' (optionally filtered) "
            "and I will confirm, launch it, and take you to the Run Analysis page "
            "where live per-model progress is shown. You can also ask me to cancel "
            "an in-flight run."
        ),
    },
    {
        "keywords": ["evidencemd", "clinician", "provider", "clinical", "medical"],
        "title": "Provider persona & EvidenceMD",
        "body": (
            "Provider-persona questions are answered automatically by EvidenceMD, "
            "a clinical-reasoning API that cites peer-reviewed literature, "
            "alongside the public platforms. There is no manual step: EvidenceMD "
            "is queried like any other model on Run Analysis, and its answer is "
            "scored and folded into model consensus and shown as a normal "
            "response in AI Response Review."
        ),
    },
    {
        "keywords": ["results", "responses", "review", "compare", "score", "override", "rescore", "export", "pinpoint"],
        "title": "AI Response Review (/results)",
        "body": (
            "Browse every scored model response with rich filters (model, persona, "
            "therapeutic area, domain, sentiment range, alerts-only, run). You can "
            "compare all models' answers to one question side by side, inspect a "
            "single response, override its AI score with a human review (sentiment + "
            "competitive position + rationale + reviewer name), re-score historical "
            "responses, run a scoring sweep on anything unscored, and export a "
            "filtered slice to CSV/JSON or a Pinpoint corpus."
        ),
    },
    {
        "keywords": ["insights", "trends", "dashboard", "analytics", "kpi", "sentiment", "alerts", "themes", "rebuild", "cortex", "ask a question"],
        "title": "Insights & Trends (/dashboard)",
        "body": (
            "The dashboard has Overview KPIs, an Insights tab (theme discovery, "
            "trends, and signals), and an 'Ask a Question' tab (Cortex Analyst "
            "natural-language Q&A over the warehouse). I can pull analytics "
            "(sentiment distribution, competitive positioning, alerts summary, "
            "per-LLM comparison, per-run and per-persona summaries, worst "
            "questions), surface theme/trend insights, answer free-form data "
            "questions, and rebuild the theme taxonomy."
        ),
    },
    {
        "keywords": ["competitor", "competitors", "competitive", "rival", "rivals",
                     "head to head", "head-to-head", "us vs them", "versus", " vs ",
                     "tremfya", "stelara", "cosentyx", "dupixent", "taltz", "entyvio",
                     "competitor sentiment", "share of voice", "who wins", "beating us"],
        "title": "Competitors: how rivals are talked about (/dashboard/head-to-head)",
        "body": (
            "Our brands and our competitors are tracked on DIFFERENT axes, and asking for "
            "one with the other's filter quietly returns nothing. A question's brand_focus "
            "is the monitored AbbVie brand it is about (Rinvoq, Skyrizi, Humira, Imbruvica, "
            "Venclexta, Vraylar, Lupron Depot). Every other agent \u2014 Tremfya, Stelara, "
            "Cosentyx, Dupixent and the rest \u2014 is a competitor, and never appears in that "
            "column no matter how much the models discuss it. What a model said about a "
            "rival is recorded per answer, when the model actually named them.\n\n"
            "There are three ways to read that, and I can run all of them:\n"
            "\u2022 How is one rival talked about, everywhere? Ask me for a competitor's "
            "sentiment by name and I read every scored answer that named them \u2014 share of "
            "answers, average sentiment with the number of mentions behind it, the "
            "positive/neutral/negative mix, and the split by AI model, persona, therapeutic "
            "area and which of our brands the question was about. Aliases resolve, so "
            "'guselkumab' and 'Tremfya' are one agent. I can also rank every agent the "
            "models named, including ones missing from our config.\n"
            "\u2022 Do we win the direct comparisons? The Head-to-Head board scores each "
            "brand-vs-competitor pair from the Comparative questions we asked on purpose, "
            "worst exposure first, with the per-model loss rate, the claims driving it, the "
            "sources cited, and the answers that never mentioned us at all.\n"
            "\u2022 Who owns a whole disease field? The disease-state landscape aggregates every "
            "agent named across brand-less answers into share of voice, mean sentiment and a "
            "position mix.\n\n"
            "One counting rule matters: a competitor is only scored in the answers that "
            "NAMED them, so an average always comes with its mention count and silence is "
            "never averaged in as neutral. A rival can be quiet on the Head-to-Head board "
            "(we asked few comparison questions) while dominating ordinary answers \u2014 which "
            "is why the two reads are separate."
        ),
    },
    {
        "keywords": ["geo intervention", "geo interventions", "recommendation", "recommendations", "content recommendation", "remediation", "semrush", "gap", "what to publish", "intervention", "impact"],
        "title": "GEO Interventions / Recommendations (/dashboard/recommendations)",
        "body": (
            "GEO Interventions turns weak competitive positions into a ranked, "
            "plain-language action list: for every focus-brand gap (scored "
            "SECOND_LINE or NOT_RECOMMENDED) it proposes what content to publish, "
            "enriched with SEMrush SEO metrics (search volume, domain authority) and "
            "ranked by estimated impact (gap severity x search volume). Open the GEO "
            "Interventions tab and click Generate to (re)build the list; filter by "
            "therapeutic area / persona / model and export to CSV. IMPORTANT: every "
            "item is a STRATEGIC SUGGESTION ONLY, never MLR-approved content \u2014 it "
            "requires Medical, Legal & Regulatory review before any use. It needs "
            "scored responses with position gaps first, so run and score analyses "
            "before generating."
        ),
    },
    {
        "keywords": ["source authority", "citation", "citations", "domain", "domains", "authority", "preferred source", "preferred sources", "who cites", "publisher", "cited", "coverage"],
        "title": "Source Authority (/dashboard/source-authority)",
        "body": (
            "Source Authority maps which web domains the AI models cite and classifies "
            "them (owned / earned / competitor / unverified and by content type), then "
            "shows the distribution, the top domains per model, citation coverage, a "
            "competitor/unverified risk callout, and preferred-source presence/absence "
            "tracking. New responses are classified automatically after each run. "
            "Responses captured BEFORE the feature existed need a one-time BACKFILL: "
            "click Backfill on the Source Authority page (or POST "
            "/source-authority/classify/sweep). The sweep is idempotent and paginated, "
            "so it is safe to re-run until it reports 0 remaining."
        ),
    },
    {
        "keywords": ["prompt volume", "search volume", "demand", "ahrefs", "keyword", "keywords", "search demand", "volume intelligence", "upload csv", "semrush export"],
        "title": "AI Prompt Volume Intelligence (/prompt-volume)",
        "body": (
            "Prompt Volume ingests third-party SEARCH-DEMAND exports (SEMrush / Ahrefs) "
            "as a proxy for how much people are asking AI about each topic. Upload a "
            "keyword/volume CSV on the page (source tool, label, and dataset date are "
            "required); the whole file is PII-linted and REJECTED ENTIRELY on any hit, "
            "so nothing partial is stored. It then shows relative volume by therapeutic "
            "area and competitor, high-volume GAP topics missing from the Approved "
            "Question Bank, an audit-friendly upload history, and a CSV export. This is "
            "a manual upload surface \u2014 there is no automatic backfill."
        ),
    },
    {
        "keywords": ["social", "listening", "social listening", "reddit", "tiktok", "instagram", "facebook", "social media", "share of voice", "apify", "posts", "engagement", "comment", "comments", "reply", "replies", "translate", "translation", "language"],
        "title": "Social Listening (/social-listening)",
        "body": (
            "Social Listening is a complementary surface (separate from the six-"
            "stage monitoring workflow) that analyzes what real people say about "
            "monitored therapies on public social channels (Reddit, TikTok, "
            "Instagram, Facebook, X), scraped via Apify. Posts AND their comments/"
            "replies are PII-scrubbed, screened for adverse events, and classified "
            "by brand, therapeutic area, topic, and sentiment. Comment sentiment is "
            "tracked as a SEPARATE dimension from post sentiment (the crowd's "
            "reaction vs. the original author), and non-English posts and comments "
            "are auto-translated to English with a 'Show original' toggle. The "
            "dashboard shows share of voice by brand and channel, post and comment "
            "sentiment, volume over time, top topics, adverse-event signals (across "
            "posts and comments), and per-channel engagement leaders. Note: these "
            "are captured-sample metrics, not market-level share of voice, and "
            "engagement is compared per channel only (never summed across "
            "channels). I can report the status, summarize the social insights, "
            "list captured posts or comments, or start a new ingest (which I will "
            "confirm first). Live ingestion needs an Apify API token in the .env."
        ),
    },
    {
        "keywords": ["workshop", "workshop insights", "ai answer insights", "digest", "digests",
                     "stakeholder", "stakeholder digest", "email digest", "webhook", "recipients",
                     "profile", "cadence", "designation", "current standing"],
        "title": "Workshop Insights & Stakeholder Digests (/digests)",
        "body": (
            "Stakeholder Digests are role-specific intelligence summaries (e.g. PV, Brand, "
            "Medical Affairs) delivered on a weekly cadence by email, webhook, or in-app. Admins "
            "configure a profile per role (recipients, cadence, and rules that narrow which alerts "
            "that role sees) on the Digests page and can trigger one on demand. The page also shows "
            "a live 'AI Answer Insights' panel: the current standing of how AI positions the brands "
            "for the curated Workshop Questions (by designation = persona x indication), with "
            "per-platform summaries, provenance sources, needs-attention callouts, and citation "
            "share of voice. Two scopes: Workshop Questions (the curated set) and All Tracked "
            "Questions. I can read the profiles, past digest runs, and the AI Answer Insights "
            "snapshot for either scope."
        ),
    },
    {
        "keywords": ["ai update impact", "model release", "model releases", "model update",
                     "model version", "version", "drift", "response drift", "correlation",
                     "changelog", "vendor", "gpt version", "gemini version", "claude version"],
        "title": "AI Update Impact / Model Releases (/dashboard/ai-update-impact)",
        "body": (
            "AI Update Impact tracks when the underlying AI models change and how that moved our "
            "tracked answers. Model updates are AUTO-DETECTED from response-drift spikes and from "
            "the real vendor version each response reports (no manual logging); the system "
            "correlates material answer drift against those release events. It shows the detected "
            "releases, a drift-vs-release timeline, per-version product impact (how many answers "
            "changed + net brand-sentiment shift), the high-impact alert feed, the "
            "correlated-vs-unexplained drift ratio, and the current live version per model. I can "
            "read the releases, drifts (and one drift's before/after detail), the timeline, "
            "version impact, high-impact updates, the correlation ratio, and current versions."
        ),
    },
    {
        "keywords": ["variation", "variations", "variation testing", "paraphrase", "paraphrases",
                     "phrasing", "robustness", "consistency", "rewording", "same question",
                     "question group", "divergence"],
        "title": "Variation Testing (/run-analysis/variations)",
        "body": (
            "Variation Testing checks how robust an AI's answer is to rewording. Claude drafts "
            "intent-preserving paraphrases of a base question; a human reviews/approves them (drafts "
            "never run against a monitored model until approved), then the base plus its approved "
            "variations run together so their answers can be compared side by side. The results view "
            "is a variation x model matrix plus a divergence/consistency summary (does the position "
            "or sentiment change when the question is phrased differently?). I can list the variation "
            "groups, show one group's drafts/approved variations, and read a group's results."
        ),
    },
    {
        "keywords": ["clinical evidence", "evidence", "trial", "trials", "study", "studies",
                     "network", "networks", "nma", "meta-analysis", "indirect comparison",
                     "drug fact", "drug facts", "label", "labels", "outcome", "endpoint",
                     "comparison", "comparisons", "head to head", "clinicaltrials", "openfda"],
        "title": "Clinical Evidence (/evidence)",
        "body": (
            "Clinical Evidence is a curated corpus, separate from what the AI models say. It "
            "holds randomised trials ingested from ClinicalTrials.gov (with their arms and "
            "outcome results mapped to canonical endpoints) and regulatory drug facts from "
            "openFDA labels. Trials are assembled into evidence NETWORKS so a comparison with "
            "no head-to-head trial can still be answered through shared comparators. Ask me "
            "for the overview, a network, a study, a brand's label facts, or what a network "
            "can actually answer for a given pair of treatments. Two things to know: an "
            "unanswerable comparison is a normal result with a named gap (not an error), and "
            "only a GOVERNED result \u2014 one from a ratified network under an approved "
            "protocol \u2014 is releasable. Everything else is EXPLORATORY."
        ),
    },
    {
        "keywords": ["verify", "verification", "curator", "curate", "curation queue", "ratify",
                     "ratification", "protocol", "protocols", "approval role", "medical review",
                     "statistical review", "membership", "governance", "gate", "reviewer",
                     "source check", "reproduce", "evidence governance"],
        "title": "Evidence governance & curation (/evidence/governance)",
        "body": (
            "Three separate gates stand between ingested evidence and a releasable number. "
            "(1) STUDY VERIFICATION is a CURATOR job \u2014 data accuracy, not clinical "
            "judgement: re-derive a study from its retained source, confirm it reproduces, "
            "then mark it VERIFIED. This one binds hardest, because evidence gathering skips "
            "an unverified study even in exploratory mode. (2) NETWORK MEMBERSHIP decides "
            "whether a study belongs in THIS analysis \u2014 note that with nothing included, "
            "membership narrows nothing and every proposed study is consulted; the first "
            "inclusion binds the filter. (3) RATIFICATION + PROTOCOL APPROVAL are REVIEWER "
            "jobs: a network goes DRAFT \u2192 medical review \u2192 statistical review, and "
            "approving the statistical stage is what ratifies it. I can show the queues "
            "(ask 'which studies are worth verifying?' \u2014 the queue ranks by whether the "
            "work changes the answer) and record any of these decisions, but I will always "
            "ask who is deciding first. Names are recorded, not authenticated."
        ),
    },
    {
        "keywords": ["ai vs evidence", "alignment", "claim", "claims", "claim check",
                     "misinformation", "accuracy", "hallucination", "fact check", "verdict",
                     "grounded", "coverage"],
        "title": "AI vs Evidence (/evidence/alignment)",
        "body": (
            "This checks what the monitored models actually claimed against our own curated "
            "evidence. Each answer is decomposed into individual claims (comparative, trial "
            "result, approval, safety, mechanism, pipeline\u2026) and each claim is graded "
            "against the authority that can settle it. Ask me to run the check on a run or a "
            "single answer \u2014 it costs one model call per answer, so I will confirm first. "
            "Read COVERAGE before the score: claims can only be graded against VERIFIED "
            "studies and labels and RATIFIED networks, so on an uncurated corpus the check "
            "returns near-zero coverage and a high score there means unmeasured, not aligned."
        ),
    },
    {
        "keywords": ["competitor discovery", "new competitor", "discover competitor",
                     "candidate", "candidates", "molecule", "class map", "landscape"],
        "title": "Competitor Discovery (/evidence/competitors)",
        "body": (
            "A sweep over the trial evidence surfaces molecules that behave like competitors "
            "but are not on any watch list \u2014 ranked by named signals (how often they "
            "appear as a comparator, whether they are newly active, whether published "
            "syntheses include them). A human accepts or rejects each candidate. Accepting "
            "does NOT add the drug to a competitor list: the page renders a brands.yaml "
            "fragment that a person commits by hand, and recording that commit is a separate "
            "step, so the queue can never claim a change nobody made."
        ),
    },
    {
        "keywords": ["synthesis", "what the evidence shows", "implications", "strategic",
                     "published nma", "published synthesis", "limitations", "evidence strength"],
        "title": "Evidence Synthesis (/evidence/synthesis)",
        "body": (
            "A per-indication roll-up: what the evidence shows, what changed recently "
            "(studies, labels and published syntheses inside a window), how much of the "
            "corpus has actually been reviewed, the accepted competitor threats, how the AI "
            "models align with all of it, and the strategic implications separated by who "
            "owns them. It also stores third-party PUBLISHED network meta-analyses under "
            "their licence's retention rules, and can assess whether one is usable for a "
            "specific comparison. Read the limitations first: where nothing is verified and "
            "no network is ratified, the limitations ARE the finding."
        ),
    },
    {
        "keywords": ["ingest", "ingestion", "fetch trials", "import evidence", "reparse",
                     "re-parse", "clinicaltrials.gov", "openfda", "harvest trials"],
        "title": "Evidence Ingest (/evidence/ingest)",
        "body": (
            "Fetches the evidence: randomised trials for one indication from "
            "ClinicalTrials.gov, drug labels from openFDA, or a re-parse that re-extracts "
            "stored studies from their own retained payloads with no network call. Every "
            "mode is PREVIEW by default \u2014 only an explicit commit writes anything. "
            "Ingestion CANNOT verify: rows land EXTRACTED or MAPPED and a curator verifies "
            "them one at a time. A re-parse deliberately SKIPS verified and rejected rows, so "
            "a re-parse that 'did nothing' on a curated corpus is the expected outcome. Only "
            "one ingestion job runs at a time."
        ),
    },
    {
        "keywords": ["activation", "impact", "intervention", "interventions", "publish",
                     "published content", "before and after", "did it work", "measure",
                     "measurement", "owner", "outcome status"],
        "title": "Activation & Impact (/dashboard/activation-impact)",
        "body": (
            "The measured half of a GEO recommendation. You turn a recommendation into an "
            "owned INTERVENTION (an owner, a due date, the questions it should move), "
            "publish the content, and the system re-asks exactly those questions before and "
            "after so you can see whether the AI's answer actually shifted. The lifecycle is "
            "PROPOSED \u2192 IN_PROGRESS \u2192 PUBLISHED \u2192 MEASURING \u2192 COMPLETED "
            "(or DEFERRED / CANCELLED), with a daily sweep advancing measurement and an "
            "immutable event timeline. Publishing launches real, billed baseline runs, so I "
            "confirm first. The result is deliberately reported as IMPROVED / NO CLEAR "
            "CHANGE / WORSENED / INCONCLUSIVE \u2014 a single-arm before/after is not proof "
            "of causation."
        ),
    },
    {
        "keywords": ["influence graph", "influence", "narrative", "narratives", "web",
                     "who drives", "drivers", "force directed", "graph"],
        "title": "Influence Graph (/dashboard/influence-graph)",
        "body": (
            "A corpus-wide map of source \u2192 claim \u2192 narrative \u2192 brand position: "
            "which cited domains are actually driving each theme the models repeat, and which "
            "competitive position that theme pushes your brand into. Use it to answer 'who "
            "is shaping this story?' rather than 'who gets cited most'. You can focus on one "
            "narrative or one domain's subgraph, and every node can show the real answers "
            "behind it."
        ),
    },
    {
        "keywords": ["curation", "coverage gap", "comparison coverage", "missing question",
                     "what are we not asking", "generate questions", "comparison matrix"],
        "title": "Question curation (comparison coverage gaps)",
        "body": (
            "Separate from the bank's persona/area/domain counts, this measures the "
            "BRAND-VS-COMPETITOR comparison matrix: for every focus brand, competitor and "
            "indication, does the bank already ask that head-to-head question? Uncovered "
            "cells are ranked by what is most worth writing. I can report the gaps for free, "
            "and I can generate the missing questions with a model \u2014 that one is billed, "
            "so it is a dry run by default and tells you the exact number of model calls a "
            "real run would make. Generated candidates land in the Discover review queue, "
            "never straight into the bank."
        ),
    },
    {
        "keywords": ["schedule", "daily", "cron", "automatic"],
        "title": "Schedule (daily run)",
        "body": (
            "A daily run can be scheduled with a cron expression and timezone. I can "
            "tell you the current schedule (enabled, cron, timezone, next run) and "
            "enable/disable or reconfigure it (confirmed first)."
        ),
    },
    {
        "keywords": ["backfill", "backfills", "sweep", "rebuild", "maintenance", "reprocess", "re-process", "historical", "history", "re-redact", "redact sweep", "populate", "one-time", "classify sweep"],
        "title": "Backfills & data maintenance",
        "body": (
            "New capabilities only process data captured AFTER they were switched on, "
            "so run these one-time backfills to fold in existing history (all are "
            "idempotent \u2014 safe to re-run):\n"
            "1) Source Authority \u2014 classify citations on old responses: ask me "
            "to 'backfill Source Authority' (or the Backfill button on the page; "
            "POST /source-authority/classify/sweep).\n"
            "2) Insights themes \u2014 discover the taxonomy and tag ALL past "
            "responses: ask me to 'rebuild insights' (or the Rebuild button).\n"
            "3) Scoring \u2014 score any unscored responses: ask me to 'run a scoring "
            "sweep' (POST /scores/sweep).\n"
            "4) GEO Interventions \u2014 (re)generate recommendations from the latest "
            "scored gaps: the Generate button on the GEO Interventions page.\n"
            "5) Compliance \u2014 re-run the PHI/PII redactor over stored harvest + "
            "social text after a detector upgrade: ask me to 're-redact stored "
            "text' (or an operator runs POST /compliance/redact-sweep or "
            "scripts/redact_backfill.py).\n"
            "6) Question-bank seeds \u2014 operators load extra therapeutic areas via "
            "scripts/seed_lupron_questions.py, scripts/seed_rheumatology_questions.py, "
            "scripts/seed_vraylar_questions.py (Neuroscience), "
            "scripts/seed_dermatology_questions.py, and "
            "scripts/seed_gastroenterology_questions.py.\n"
            "I can run four of these for you directly, confirming first: the Source "
            "Authority backfill, the insights rebuild, the scoring sweep, and the "
            "compliance re-redaction. The GEO Interventions generate lives on its page."
        ),
    },
]


def search_help(topic: str | None) -> list[dict]:
    """Return the most relevant help sections for a topic (or all when blank)."""
    if not topic:
        return HELP_SECTIONS
    t = topic.lower()
    scored: list[tuple[int, dict]] = []
    for sec in HELP_SECTIONS:
        score = sum(1 for kw in sec["keywords"] if kw in t)
        # Also score title-word overlap so "how do I run analysis" matches.
        score += sum(1 for w in sec["title"].lower().split() if len(w) > 3 and w in t)
        if score:
            scored.append((score, sec))
    if not scored:
        return [HELP_SECTIONS[0]]  # overview fallback
    scored.sort(key=lambda x: x[0], reverse=True)
    return [sec for _, sec in scored[:3]]
