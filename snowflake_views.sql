/* ============================================================================
   Evidence Monitoring Agent — Snowflake analytics views for Cortex Analyst
   ----------------------------------------------------------------------------
   These views reproduce every visualization/KPI on the app dashboard so you can
   build the same charts in Snowsight / Cortex Analyst dashboards.

   Notes
   - Columns were mirrored as quoted-UPPERCASE identifiers, so unquoted UPPERCASE
     references work everywhere EXCEPT the reserved word TRIGGER, which must be
     written as "TRIGGER".
   - The app always uses the LATEST score per response (MAX(score_version)). All
     sentiment/positioning views below dedupe to that latest score via QUALIFY,
     so numbers match the UI (and avoid double-counting re-scored responses).
   - Sentiment buckets use the app thresholds: positive > 0.2, negative < -0.2,
     neutral otherwise (inclusive of the boundaries).
   ========================================================================== */

-- One-time: let the app role create views (run as an admin; safe to re-run).
USE ROLE ACCOUNTADMIN;
GRANT CREATE VIEW ON SCHEMA EVIDENCE_DB.PUBLIC TO ROLE EVIDENCE_APP_ROLE;

USE ROLE EVIDENCE_APP_ROLE;
USE WAREHOUSE EVIDENCE_WH;
USE SCHEMA EVIDENCE_DB.PUBLIC;

/* ---------------------------------------------------------------------------
   0. Foundation: response joined to its LATEST score
   Underpins most sentiment/positioning views.
   --------------------------------------------------------------------------- */
CREATE OR REPLACE VIEW VW_RESPONSE_LATEST_SCORE AS
SELECT
    r.RESPONSE_ID,
    r.RUN_ID,
    r.LLM_NAME,
    r.PERSONA,
    r.QUESTION_ID,
    r.THERAPEUTIC_AREA,
    /* Only indication-level keys need remapping. Dermatology, Gastroenterology,
       Oncology, Rheumatology and Neuroscience store the area name as the key
       itself, so they fall through ELSE unchanged and need no arm here.
       'Immunology' is retired (split into Dermatology + Gastroenterology); it is
       labelled rather than remapped, because a row that survived
       backfill_therapeutic_area_split.py is one the backfill could NOT resolve,
       and silently folding it into either specialty would invent a fact. */
    CASE r.THERAPEUTIC_AREA
        WHEN 'Endometriosis' THEN 'Women''s Health'
        WHEN 'Uterine Fibroids' THEN 'Women''s Health'
        WHEN 'Central Precocious Puberty' THEN 'Endocrinology'
        WHEN 'Immunology' THEN 'Immunology (legacy)'
        ELSE r.THERAPEUTIC_AREA
    END AS THERAPEUTIC_AREA_GROUP,
    r.BRAND_FOCUS,
    r.DOMAIN,
    r.INTENT_TYPE,
    r.CONSENSUS_LEVEL,
    r.STATUS,
    r.TIMESTAMP_UTC,
    sc.SCORE_ID,
    sc.SCORE_VERSION,
    sc.SENTIMENT_SCORE,
    COALESCE(sc.COMPETITIVE_POSITION, 'NOT_MENTIONED') AS COMPETITIVE_POSITION,
    sc.SCORING_RATIONALE,
    sc.KEY_CLAIMS,
    sc.CREATED_AT AS SCORED_AT
FROM RESPONSES r
JOIN SCORING_RECORDS sc ON sc.RESPONSE_ID = r.RESPONSE_ID
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY sc.RESPONSE_ID ORDER BY sc.SCORE_VERSION DESC
) = 1;

/* ---------------------------------------------------------------------------
   1. KPI summary (the 5 stat cards on the dashboard) — single row
   --------------------------------------------------------------------------- */
CREATE OR REPLACE VIEW VW_KPI_SUMMARY AS
SELECT
    (SELECT COUNT(*) FROM RESPONSES)                                          AS TOTAL_RESPONSES,
    (SELECT COUNT(DISTINCT LLM_NAME) FROM RESPONSES)                          AS LLM_TARGETS,
    (SELECT COUNT(*) FROM ALERTS)                                             AS TOTAL_ALERTS,
    (SELECT COUNT(*) FROM CONSENSUS_RECORDS)                                  AS CONSENSUS_EVALS,
    (SELECT COUNT(*) FROM CONSENSUS_RECORDS WHERE CONSENSUS_LEVEL = 'FULL')    AS CONSENSUS_FULL,
    (SELECT COUNT(*) FROM CONSENSUS_RECORDS WHERE CONSENSUS_LEVEL = 'PARTIAL') AS CONSENSUS_PARTIAL,
    (SELECT COUNT(*) FROM VW_RESPONSE_LATEST_SCORE WHERE SENTIMENT_SCORE > 0.2)  AS SENTIMENT_POSITIVE,
    (SELECT COUNT(*) FROM VW_RESPONSE_LATEST_SCORE WHERE SENTIMENT_SCORE < -0.2) AS SENTIMENT_NEGATIVE;

/* ---------------------------------------------------------------------------
   2. Sentiment by LLM  (Dashboard: "Sentiment by LLM" bar chart)
   --------------------------------------------------------------------------- */
CREATE OR REPLACE VIEW VW_SENTIMENT_BY_LLM AS
SELECT
    LLM_NAME                       AS LLM,
    ROUND(AVG(SENTIMENT_SCORE), 3) AS AVG_SENTIMENT,
    COUNT(SENTIMENT_SCORE)         AS SCORED
FROM VW_RESPONSE_LATEST_SCORE
WHERE SENTIMENT_SCORE IS NOT NULL
GROUP BY LLM_NAME;

/* ---------------------------------------------------------------------------
   3. Sentiment by therapeutic area (analytics: sentiment-distribution.by_ta)
   --------------------------------------------------------------------------- */
CREATE OR REPLACE VIEW VW_SENTIMENT_BY_THERAPEUTIC_AREA AS
SELECT
    THERAPEUTIC_AREA               AS THERAPEUTIC_AREA,
    ROUND(AVG(SENTIMENT_SCORE), 3) AS AVG_SENTIMENT,
    COUNT(SENTIMENT_SCORE)         AS SCORED
FROM VW_RESPONSE_LATEST_SCORE
WHERE SENTIMENT_SCORE IS NOT NULL
GROUP BY THERAPEUTIC_AREA;

/* ---------------------------------------------------------------------------
   3b. Sentiment by therapeutic AREA (specific indications rolled up to parent)
   Endometriosis + Uterine Fibroids -> Women's Health; CPP -> Endocrinology.
   --------------------------------------------------------------------------- */
CREATE OR REPLACE VIEW VW_SENTIMENT_BY_AREA AS
SELECT
    THERAPEUTIC_AREA_GROUP         AS THERAPEUTIC_AREA_GROUP,
    ROUND(AVG(SENTIMENT_SCORE), 3) AS AVG_SENTIMENT,
    COUNT(SENTIMENT_SCORE)         AS SCORED
FROM VW_RESPONSE_LATEST_SCORE
WHERE SENTIMENT_SCORE IS NOT NULL
GROUP BY THERAPEUTIC_AREA_GROUP;

/* ---------------------------------------------------------------------------
   4. Sentiment buckets (KPI "Avg Sentiment" positive/neutral/negative)
   --------------------------------------------------------------------------- */
CREATE OR REPLACE VIEW VW_SENTIMENT_BUCKETS AS
SELECT
    SUM(IFF(SENTIMENT_SCORE > 0.2, 1, 0))                  AS POSITIVE,
    SUM(IFF(SENTIMENT_SCORE BETWEEN -0.2 AND 0.2, 1, 0))   AS NEUTRAL,
    SUM(IFF(SENTIMENT_SCORE < -0.2, 1, 0))                 AS NEGATIVE
FROM VW_RESPONSE_LATEST_SCORE
WHERE SENTIMENT_SCORE IS NOT NULL;

/* ---------------------------------------------------------------------------
   5. Competitive positioning by LLM (Dashboard: stacked bar) — long format
   --------------------------------------------------------------------------- */
CREATE OR REPLACE VIEW VW_POSITIONING_BY_LLM AS
SELECT
    LLM_NAME             AS LLM,
    COMPETITIVE_POSITION AS POSITION,
    COUNT(*)             AS N
FROM VW_RESPONSE_LATEST_SCORE
GROUP BY LLM_NAME, COMPETITIVE_POSITION;

/* ---------------------------------------------------------------------------
   6. Response volume over time (Dashboard: area chart) — by day x status
   --------------------------------------------------------------------------- */
CREATE OR REPLACE VIEW VW_VOLUME_BY_DAY AS
SELECT
    TO_DATE(TIMESTAMP_UTC) AS DAY,
    STATUS                 AS STATUS,
    COUNT(*)               AS N
FROM RESPONSES
GROUP BY TO_DATE(TIMESTAMP_UTC), STATUS;

/* ---------------------------------------------------------------------------
   7. Intent distribution (Dashboard: donut) + by persona
   --------------------------------------------------------------------------- */
CREATE OR REPLACE VIEW VW_INTENT_DISTRIBUTION AS
SELECT
    INTENT_TYPE AS INTENT_TYPE,
    COUNT(*)    AS N
FROM RESPONSES
WHERE INTENT_TYPE IS NOT NULL
GROUP BY INTENT_TYPE;

CREATE OR REPLACE VIEW VW_INTENT_BY_PERSONA AS
SELECT
    PERSONA     AS PERSONA,
    INTENT_TYPE AS INTENT_TYPE,
    COUNT(*)    AS N
FROM RESPONSES
WHERE INTENT_TYPE IS NOT NULL
GROUP BY PERSONA, INTENT_TYPE;

/* ---------------------------------------------------------------------------
   8. Consensus breakdown (Dashboard: horizontal bar) — by level + by LLM
   --------------------------------------------------------------------------- */
CREATE OR REPLACE VIEW VW_CONSENSUS_BY_LEVEL AS
SELECT
    CONSENSUS_LEVEL AS CONSENSUS_LEVEL,
    COUNT(*)        AS N
FROM CONSENSUS_RECORDS
GROUP BY CONSENSUS_LEVEL;

CREATE OR REPLACE VIEW VW_CONSENSUS_BY_LLM AS
SELECT
    LLM_NAME        AS LLM,
    CONSENSUS_LEVEL AS CONSENSUS_LEVEL,
    COUNT(*)        AS N
FROM RESPONSES
WHERE CONSENSUS_LEVEL IS NOT NULL
GROUP BY LLM_NAME, CONSENSUS_LEVEL;

/* ---------------------------------------------------------------------------
   9. Alerts by rule (Dashboard: "Alerts by Rule" list)
   --------------------------------------------------------------------------- */
CREATE OR REPLACE VIEW VW_ALERTS_BY_RULE AS
SELECT
    RULE_TRIGGERED AS RULE_TRIGGERED,
    COUNT(*)       AS N
FROM ALERTS
GROUP BY RULE_TRIGGERED;

/* ---------------------------------------------------------------------------
   10. Per-LLM comparison rollup (Dashboard stat math + LLM comparison)
   --------------------------------------------------------------------------- */
CREATE OR REPLACE VIEW VW_LLM_RESPONSE_COUNTS AS
SELECT
    LLM_NAME AS LLM,
    STATUS   AS STATUS,
    COUNT(*) AS N
FROM RESPONSES
GROUP BY LLM_NAME, STATUS;

CREATE OR REPLACE VIEW VW_LLM_COMPARISON AS
WITH counts AS (
    SELECT LLM_NAME, COUNT(*) AS TOTAL_RESPONSES
    FROM RESPONSES GROUP BY LLM_NAME
),
sent AS (
    SELECT LLM_NAME,
           ROUND(AVG(SENTIMENT_SCORE), 3) AS AVG_SENTIMENT,
           COUNT(SENTIMENT_SCORE)         AS SCORED
    FROM VW_RESPONSE_LATEST_SCORE
    GROUP BY LLM_NAME
)
SELECT
    c.LLM_NAME        AS LLM,
    c.TOTAL_RESPONSES AS TOTAL_RESPONSES,
    s.AVG_SENTIMENT   AS AVG_SENTIMENT,
    s.SCORED          AS SCORED
FROM counts c
LEFT JOIN sent s ON s.LLM_NAME = c.LLM_NAME;

/* ===========================================================================
   CORTEX TAB views (brand-level) — match Dashboard -> Cortex
   =========================================================================== */

/* 11. Average sentiment by brand (Cortex tab bar chart) */
CREATE OR REPLACE VIEW VW_SENTIMENT_BY_BRAND AS
SELECT
    BRAND_FOCUS                    AS BRAND,
    COUNT(*)                       AS SCORED,
    ROUND(AVG(SENTIMENT_SCORE), 3) AS AVG_SENTIMENT,
    ROUND(MIN(SENTIMENT_SCORE), 3) AS MIN_SENTIMENT,
    ROUND(MAX(SENTIMENT_SCORE), 3) AS MAX_SENTIMENT
FROM VW_RESPONSE_LATEST_SCORE
WHERE SENTIMENT_SCORE IS NOT NULL
GROUP BY BRAND_FOCUS;

/* 12. Competitive positioning by brand (Cortex tab table) */
CREATE OR REPLACE VIEW VW_POSITIONING_BY_BRAND AS
SELECT
    BRAND_FOCUS          AS BRAND,
    COMPETITIVE_POSITION AS POSITION,
    COUNT(*)             AS N
FROM VW_RESPONSE_LATEST_SCORE
GROUP BY BRAND_FOCUS, COMPETITIVE_POSITION;

/* 13. Sentiment trend over time by brand (Cortex tab line chart) */
CREATE OR REPLACE VIEW VW_SENTIMENT_TREND AS
SELECT
    TO_DATE(TIMESTAMP_UTC)         AS DAY,
    BRAND_FOCUS                    AS BRAND,
    ROUND(AVG(SENTIMENT_SCORE), 3) AS AVG_SENTIMENT,
    COUNT(*)                       AS N
FROM VW_RESPONSE_LATEST_SCORE
WHERE SENTIMENT_SCORE IS NOT NULL
GROUP BY TO_DATE(TIMESTAMP_UTC), BRAND_FOCUS;

/* ===========================================================================
   PERSONA-LEVEL views (Dashboard: persona comparison + per-tab KPIs)
   Back the /analytics/persona-summary endpoint. Personas: Prospect|Patient|Provider.
   =========================================================================== */

/* 19. Per-persona sentiment summary (avg + buckets + scored + response count).
   RESPONSE_COUNT counts scored responses (rows with a latest score), matching
   the SQLite implementation which aggregates the response+latest-score join. */
CREATE OR REPLACE VIEW VW_PERSONA_SENTIMENT AS
SELECT
    PERSONA                                              AS PERSONA,
    COUNT(*)                                             AS RESPONSE_COUNT,
    ROUND(AVG(SENTIMENT_SCORE), 3)                       AS AVG_SENTIMENT,
    COUNT(SENTIMENT_SCORE)                               AS SCORED,
    SUM(IFF(SENTIMENT_SCORE > 0.2, 1, 0))                AS POSITIVE,
    SUM(IFF(SENTIMENT_SCORE BETWEEN -0.2 AND 0.2, 1, 0)) AS NEUTRAL,
    SUM(IFF(SENTIMENT_SCORE < -0.2, 1, 0))               AS NEGATIVE
FROM VW_RESPONSE_LATEST_SCORE
GROUP BY PERSONA;

/* 20. Per-persona competitive positioning (long format) */
CREATE OR REPLACE VIEW VW_PERSONA_POSITIONING AS
SELECT
    PERSONA              AS PERSONA,
    COMPETITIVE_POSITION AS POSITION,
    COUNT(*)             AS N
FROM VW_RESPONSE_LATEST_SCORE
GROUP BY PERSONA, COMPETITIVE_POSITION;

/* 21. Per-persona consensus breakdown */
CREATE OR REPLACE VIEW VW_PERSONA_CONSENSUS AS
SELECT
    PERSONA         AS PERSONA,
    CONSENSUS_LEVEL AS CONSENSUS_LEVEL,
    COUNT(*)        AS N
FROM RESPONSES
WHERE CONSENSUS_LEVEL IS NOT NULL
GROUP BY PERSONA, CONSENSUS_LEVEL;

/* 22. Per-persona alert counts (alerts joined to their response's persona) */
CREATE OR REPLACE VIEW VW_PERSONA_ALERTS AS
SELECT
    r.PERSONA AS PERSONA,
    COUNT(*)  AS N
FROM ALERTS a
JOIN RESPONSES r ON r.RESPONSE_ID = a.RESPONSE_ID
GROUP BY r.PERSONA;

/* ===========================================================================
   RUN-LEVEL views (Results page run mini-dashboard)
   =========================================================================== */

/* 14. Per-run KPIs (Results stats row) — straight from RUNS rollup columns */
CREATE OR REPLACE VIEW VW_RUN_KPIS AS
SELECT
    RUN_ID,
    "TRIGGER"            AS TRIGGER_TYPE,   -- TRIGGER is a reserved word
    STATUS,
    STARTED_AT,
    ENDED_AT,
    QUESTIONS_ATTEMPTED,
    RESPONSES_SUCCESS,
    RESPONSES_FAILED,
    RESPONSES_TRUNCATED,
    RESPONSES_BLOCKED,
    TOTAL_TOKENS,
    ESTIMATED_COST_USD,
    ALERTS_TRIGGERED,
    CONSENSUS_FULL,
    CONSENSUS_PARTIAL,
    CONSENSUS_MISSING
FROM RUNS;

/* 15. Per-run sentiment by LLM */
CREATE OR REPLACE VIEW VW_RUN_SENTIMENT_BY_LLM AS
SELECT
    RUN_ID,
    LLM_NAME                       AS LLM,
    ROUND(AVG(SENTIMENT_SCORE), 3) AS AVG_SENTIMENT,
    COUNT(SENTIMENT_SCORE)         AS SCORED
FROM VW_RESPONSE_LATEST_SCORE
WHERE SENTIMENT_SCORE IS NOT NULL
GROUP BY RUN_ID, LLM_NAME;

/* 16. Per-run positioning by LLM */
CREATE OR REPLACE VIEW VW_RUN_POSITIONING_BY_LLM AS
SELECT
    RUN_ID,
    LLM_NAME             AS LLM,
    COMPETITIVE_POSITION AS POSITION,
    COUNT(*)             AS N
FROM VW_RESPONSE_LATEST_SCORE
GROUP BY RUN_ID, LLM_NAME, COMPETITIVE_POSITION;

/* 17. Per-run intent distribution */
CREATE OR REPLACE VIEW VW_RUN_INTENT AS
SELECT
    RUN_ID,
    INTENT_TYPE AS INTENT_TYPE,
    COUNT(*)    AS N
FROM RESPONSES
WHERE INTENT_TYPE IS NOT NULL
GROUP BY RUN_ID, INTENT_TYPE;

/* 18. Per-run consensus breakdown */
CREATE OR REPLACE VIEW VW_RUN_CONSENSUS AS
SELECT
    RUN_ID,
    CONSENSUS_LEVEL AS CONSENSUS_LEVEL,
    COUNT(*)        AS N,
    SUM(IFF(GEO_FALLBACK_USED, 1, 0)) AS GEO_FALLBACK_COUNT
FROM CONSENSUS_RECORDS
GROUP BY RUN_ID, CONSENSUS_LEVEL;

/* ===========================================================================
   SOCIAL LISTENING views (complementary surface — Apify posts + comments)
   Comment sentiment is a SEPARATE dimension from post sentiment. Buckets use the
   stored SENTIMENT_LABEL (positive/neutral/negative) so they match the app UI.
   =========================================================================== */

/* 23. Post sentiment by brand (Social: Sentiment by brand) */
CREATE OR REPLACE VIEW VW_SOCIAL_POST_SENTIMENT_BY_BRAND AS
SELECT
    BRAND_FOCUS                                  AS BRAND,
    COUNT(*)                                     AS POSTS,
    ROUND(AVG(SENTIMENT), 3)                     AS AVG_SENTIMENT,
    SUM(IFF(SENTIMENT_LABEL = 'positive', 1, 0)) AS POSITIVE,
    SUM(IFF(SENTIMENT_LABEL = 'neutral', 1, 0))  AS NEUTRAL,
    SUM(IFF(SENTIMENT_LABEL = 'negative', 1, 0)) AS NEGATIVE
FROM SOCIAL_POSTS
GROUP BY BRAND_FOCUS;

/* 24. Post sentiment by channel */
CREATE OR REPLACE VIEW VW_SOCIAL_POST_SENTIMENT_BY_CHANNEL AS
SELECT
    CHANNEL                                      AS CHANNEL,
    COUNT(*)                                     AS POSTS,
    ROUND(AVG(SENTIMENT), 3)                     AS AVG_SENTIMENT,
    SUM(IFF(SENTIMENT_LABEL = 'positive', 1, 0)) AS POSITIVE,
    SUM(IFF(SENTIMENT_LABEL = 'neutral', 1, 0))  AS NEUTRAL,
    SUM(IFF(SENTIMENT_LABEL = 'negative', 1, 0)) AS NEGATIVE
FROM SOCIAL_POSTS
GROUP BY CHANNEL;

/* 25. Comment sentiment by channel (separate dimension from post sentiment) */
CREATE OR REPLACE VIEW VW_SOCIAL_COMMENT_SENTIMENT_BY_CHANNEL AS
SELECT
    CHANNEL                                      AS CHANNEL,
    COUNT(*)                                     AS COMMENTS,
    ROUND(AVG(SENTIMENT), 3)                     AS AVG_SENTIMENT,
    SUM(IFF(SENTIMENT_LABEL = 'positive', 1, 0)) AS POSITIVE,
    SUM(IFF(SENTIMENT_LABEL = 'neutral', 1, 0))  AS NEUTRAL,
    SUM(IFF(SENTIMENT_LABEL = 'negative', 1, 0)) AS NEGATIVE
FROM SOCIAL_COMMENTS
GROUP BY CHANNEL;

/* 26. Comment sentiment by brand (brand attributed via the parent post) */
CREATE OR REPLACE VIEW VW_SOCIAL_COMMENT_SENTIMENT_BY_BRAND AS
SELECT
    p.BRAND_FOCUS            AS BRAND,
    COUNT(*)                 AS COMMENTS,
    ROUND(AVG(c.SENTIMENT), 3) AS AVG_SENTIMENT
FROM SOCIAL_COMMENTS c
JOIN SOCIAL_POSTS p ON p.ID = c.POST_ID
GROUP BY p.BRAND_FOCUS;

/* 27. Share of voice — post count + share by brand (captured sample) */
CREATE OR REPLACE VIEW VW_SOCIAL_SHARE_OF_VOICE AS
SELECT
    BRAND_FOCUS AS BRAND,
    COUNT(*)    AS POSTS,
    ROUND(COUNT(*) / NULLIF((SELECT COUNT(*) FROM SOCIAL_POSTS), 0), 3) AS POST_SHARE
FROM SOCIAL_POSTS
GROUP BY BRAND_FOCUS;

/* 28. Post volume over time by channel */
CREATE OR REPLACE VIEW VW_SOCIAL_VOLUME_BY_DAY AS
SELECT
    TO_DATE(POSTED_AT) AS DAY,
    CHANNEL            AS CHANNEL,
    COUNT(*)           AS N
FROM SOCIAL_POSTS
WHERE POSTED_AT IS NOT NULL
GROUP BY TO_DATE(POSTED_AT), CHANNEL;

/* 29. Top themes/topics (post count + avg sentiment) */
CREATE OR REPLACE VIEW VW_SOCIAL_TOP_TOPICS AS
SELECT
    TOPIC                    AS TOPIC,
    COUNT(*)                 AS N,
    ROUND(AVG(SENTIMENT), 3) AS AVG_SENTIMENT
FROM SOCIAL_POSTS
WHERE TOPIC IS NOT NULL
GROUP BY TOPIC;

/* 30. Adverse-event signals — posts vs comments */
CREATE OR REPLACE VIEW VW_SOCIAL_AE AS
SELECT 'post' AS SOURCE, COUNT(*) AS AE_COUNT FROM SOCIAL_POSTS WHERE AE_FLAG = TRUE
UNION ALL
SELECT 'comment' AS SOURCE, COUNT(*) AS AE_COUNT FROM SOCIAL_COMMENTS WHERE AE_FLAG = TRUE;

/* ===========================================================================
   SOURCE AUTHORITY views (FR-706a) — who AI cites + preferred-source presence
   RESPONSE_CITATIONS joins to SOURCE_DOMAINS (the cached per-domain
   classification) for control_type / display_category / publisher.
   =========================================================================== */

/* 31. Share of voice by ownership (ABBVIE / COMPETITOR / INDEPENDENT / UNKNOWN) */
CREATE OR REPLACE VIEW VW_SOURCE_SHARE_OF_VOICE AS
SELECT
    COALESCE(d.CONTROL_TYPE, 'UNKNOWN') AS CONTROL_TYPE,
    SUM(c.CITATION_COUNT)               AS CITATIONS,
    COUNT(DISTINCT c.RESPONSE_ID)       AS RESPONSES
FROM RESPONSE_CITATIONS c
LEFT JOIN SOURCE_DOMAINS d ON d.DOMAIN_ID = c.DOMAIN_ID
GROUP BY COALESCE(d.CONTROL_TYPE, 'UNKNOWN');

/* 32. Top cited domains (citation frequency + how many answers cited it) */
CREATE OR REPLACE VIEW VW_TOP_CITED_DOMAINS AS
SELECT
    c.AUTHORITY_DOMAIN            AS AUTHORITY_DOMAIN,
    d.CONTROL_TYPE               AS CONTROL_TYPE,
    d.DISPLAY_CATEGORY           AS DISPLAY_CATEGORY,
    d.PUBLISHER_NAME             AS PUBLISHER_NAME,
    SUM(c.CITATION_COUNT)        AS CITATIONS,
    COUNT(DISTINCT c.RESPONSE_ID) AS RESPONSES
FROM RESPONSE_CITATIONS c
LEFT JOIN SOURCE_DOMAINS d ON d.DOMAIN_ID = c.DOMAIN_ID
GROUP BY c.AUTHORITY_DOMAIN, d.CONTROL_TYPE, d.DISPLAY_CATEGORY, d.PUBLISHER_NAME;

/* 33. Preferred-source presence (FR-706a.7): how often each MA-preferred domain
   actually appears in AI-cited sources, per TA x platform. */
CREATE OR REPLACE VIEW VW_PREFERRED_SOURCE_PRESENCE AS
SELECT
    THERAPEUTIC_AREA                                     AS THERAPEUTIC_AREA,
    AUTHORITY_DOMAIN                                     AS AUTHORITY_DOMAIN,
    LLM_NAME                                             AS LLM,
    COUNT(*)                                             AS OBSERVATIONS,
    SUM(IFF(WAS_PRESENT, 1, 0))                          AS PRESENT,
    ROUND(SUM(IFF(WAS_PRESENT, 1, 0)) / NULLIF(COUNT(*), 0), 3) AS PRESENCE_RATE
FROM PREFERRED_SOURCE_OBSERVATIONS
GROUP BY THERAPEUTIC_AREA, AUTHORITY_DOMAIN, LLM_NAME;

/* ===========================================================================
   PROMPT VOLUME views (FR-116) — search-demand proxy + coverage gaps
   =========================================================================== */

/* 34. Demand by therapeutic area (query count + summed volume + uncovered count) */
CREATE OR REPLACE VIEW VW_PROMPT_VOLUME_BY_TA AS
SELECT
    MATCHED_THERAPEUTIC_AREA               AS THERAPEUTIC_AREA,
    COUNT(*)                               AS QUERIES,
    SUM(SEARCH_VOLUME)                     AS TOTAL_VOLUME,
    SUM(IFF(MATCHED_QUESTION_ID IS NULL, 1, 0)) AS UNCOVERED_QUERIES
FROM PROMPT_VOLUME_STAGING
GROUP BY MATCHED_THERAPEUTIC_AREA;

/* 35. Open coverage-gap alerts, ranked by opportunity */
CREATE OR REPLACE VIEW VW_PROMPT_VOLUME_GAPS_OPEN AS
SELECT
    ALERT_ID, TOPIC_KEY, LABEL, THERAPEUTIC_AREA, COMPETITOR,
    COMBINED_VOLUME, OPPORTUNITY_SCORE, QUERY_COUNT, LAST_SEEN_AT
FROM PROMPT_VOLUME_GAP_ALERTS
WHERE STATUS = 'OPEN';

/* ===========================================================================
   GEO INTERVENTION RECOMMENDATIONS (BR-012) + GEO ground truth
   =========================================================================== */

/* 36. Recommendation impact summary by content type x TA */
CREATE OR REPLACE VIEW VW_RECOMMENDATIONS_BY_CONTENT_TYPE AS
SELECT
    CONTENT_TYPE               AS CONTENT_TYPE,
    THERAPEUTIC_AREA           AS THERAPEUTIC_AREA,
    COUNT(*)                   AS RECS,
    ROUND(AVG(IMPACT_SCORE), 2) AS AVG_IMPACT
FROM RECOMMENDATIONS
GROUP BY CONTENT_TYPE, THERAPEUTIC_AREA;

/* 37. The most recent recommendation batch (ranked list the UI shows) */
CREATE OR REPLACE VIEW VW_RECOMMENDATIONS_LATEST AS
SELECT *
FROM RECOMMENDATIONS
WHERE BATCH_ID = (SELECT BATCH_ID FROM RECOMMENDATIONS ORDER BY CREATED_AT DESC LIMIT 1);

/* 38. GEO verified ground-truth corpus (Chairman fallback source of truth) */
CREATE OR REPLACE VIEW VW_GEO_SCHEMAS AS
SELECT
    BRAND, GENERIC_NAME, DRUG_CLASS, DATA_SOURCE, CLINICAL_VALUES_VERIFIED,
    LABEL_SOURCE, LABEL_EFFECTIVE_TIME, SYNCED_AT
FROM GEO_SCHEMAS;

/* ===========================================================================
   MODEL RELEASES (FR-707a) + STAKEHOLDER DIGESTS (BR-008a)
   =========================================================================== */

/* 39. Model release / version-change log */
CREATE OR REPLACE VIEW VW_MODEL_RELEASES AS
SELECT
    TARGET_PLATFORM, VERSION, RELEASE_DATE, EVENT_TYPE, SOURCE, SUMMARY, CONFIDENCE
FROM MODEL_RELEASE_LOG;

/* 40. Digest run history (delivery bookkeeping per role). ROLE is quoted to be safe. */
CREATE OR REPLACE VIEW VW_DIGEST_RUNS AS
SELECT
    "ROLE"           AS ROLE,
    GENERATED_AT     AS GENERATED_AT,
    FINDINGS_COUNT   AS FINDINGS_COUNT,
    DELIVERED_EMAIL  AS DELIVERED_EMAIL,
    DELIVERED_WEBHOOK AS DELIVERED_WEBHOOK
FROM DIGEST_RUNS;

-- Quick sanity check:
-- SELECT * FROM VW_KPI_SUMMARY;
-- SELECT * FROM VW_SENTIMENT_BY_LLM ORDER BY AVG_SENTIMENT DESC;
-- SELECT * FROM VW_SOCIAL_COMMENT_SENTIMENT_BY_CHANNEL ORDER BY AVG_SENTIMENT;
