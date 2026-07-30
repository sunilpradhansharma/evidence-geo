# Snowflake + Cortex Integration

The backend mirrors all operational data (questions, responses, scores, consensus,
alerts, audit log, themes, harvested questions, runs, and **raw API input/output**) into
Snowflake, and uses **Cortex** for an additional insight layer plus a natural-language
"Ask your data" experience in the dashboard (**Dashboard -> Cortex**).

SQLite stays the fast operational store. Snowflake is the warehouse + Cortex layer; data
reaches it through a batched, idempotent mirror. **Only the backend connects to
Snowflake** (one service identity), so the public app link works for everyone — end-users
never authenticate to Snowflake.

When `SNOWFLAKE_ENABLED=false` (or credentials are missing) every Snowflake call is a
no-op and the app runs exactly as before.

---

## 1. Generate a key-pair (recommended auth)

Key-pair auth avoids MFA prompts and password-expiry, so the unattended prod backend
connects reliably.

```bash
# Unencrypted private key (simplest):
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out rsa_key.p8 -nocrypt
# Matching public key:
openssl rsa -in rsa_key.p8 -pubout -out rsa_key.pub
```

For cloud deploys, base64 the private key and put it in `SNOWFLAKE_PRIVATE_KEY_B64`
instead of shipping the file:

```bash
# Linux/macOS:
base64 -w0 rsa_key.p8
# Windows PowerShell:
[Convert]::ToBase64String([IO.File]::ReadAllBytes("rsa_key.p8"))
```

## 2. One-time Snowflake setup (run as ACCOUNTADMIN or similar)

```sql
-- Warehouse, database, schema
CREATE WAREHOUSE IF NOT EXISTS EVIDENCE_WH
  WAREHOUSE_SIZE = 'XSMALL' AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE;
CREATE DATABASE IF NOT EXISTS EVIDENCE_DB;
CREATE SCHEMA   IF NOT EXISTS EVIDENCE_DB.PUBLIC;

-- Role
CREATE ROLE IF NOT EXISTS EVIDENCE_APP_ROLE;
GRANT USAGE   ON WAREHOUSE EVIDENCE_WH        TO ROLE EVIDENCE_APP_ROLE;
GRANT USAGE   ON DATABASE  EVIDENCE_DB        TO ROLE EVIDENCE_APP_ROLE;
GRANT USAGE   ON SCHEMA    EVIDENCE_DB.PUBLIC TO ROLE EVIDENCE_APP_ROLE;
GRANT CREATE TABLE ON SCHEMA EVIDENCE_DB.PUBLIC TO ROLE EVIDENCE_APP_ROLE;
GRANT SELECT, INSERT, UPDATE, DELETE ON FUTURE TABLES IN SCHEMA EVIDENCE_DB.PUBLIC
  TO ROLE EVIDENCE_APP_ROLE;
-- Cortex functions (SNOWFLAKE.CORTEX.COMPLETE / SENTIMENT / ...)
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE EVIDENCE_APP_ROLE;

-- Service user with the PUBLIC key (paste the contents of rsa_key.pub without the
-- BEGIN/END lines and without newlines):
CREATE USER IF NOT EXISTS EVIDENCE_SVC
  DEFAULT_ROLE = EVIDENCE_APP_ROLE
  DEFAULT_WAREHOUSE = EVIDENCE_WH
  RSA_PUBLIC_KEY = 'MIIBIjANBgkq...';
GRANT ROLE EVIDENCE_APP_ROLE TO USER EVIDENCE_SVC;
```

## 3. Configure `.env`

```env
SNOWFLAKE_ENABLED=true
SNOWFLAKE_ACCOUNT=ab12345.us-east-1     # your account identifier
SNOWFLAKE_USER=EVIDENCE_SVC
SNOWFLAKE_ROLE=EVIDENCE_APP_ROLE
SNOWFLAKE_WAREHOUSE=EVIDENCE_WH
SNOWFLAKE_DATABASE=EVIDENCE_DB
SNOWFLAKE_SCHEMA=PUBLIC
SNOWFLAKE_PRIVATE_KEY_PATH=C:/path/to/rsa_key.p8
# or, for cloud deploy: SNOWFLAKE_PRIVATE_KEY_B64=<base64 of rsa_key.p8>
SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=       # blank if unencrypted
SNOWFLAKE_CORTEX_MODEL=claude-3-5-sonnet
SNOWFLAKE_CORTEX_ANALYST_ENABLED=true
SNOWFLAKE_CAPTURE_EVENTS=true
```

`SNOWFLAKE_CORTEX_MODEL` must be a model available for `CORTEX.COMPLETE` in your account's
region (e.g. `claude-3-5-sonnet`, `mistral-large2`, `llama3.1-70b`). Check the Snowflake
Cortex docs for your region's model list.

## 4. Install deps & run

```bash
pip install -r backend/requirements.txt   # adds snowflake-connector-python + cryptography
python -m uvicorn app.main:app --reload --port 8000
```

On startup the backend creates all mirror tables + `SYNC_STATE` + `APP_EVENTS`
(idempotent), backfills existing SQLite data, and schedules a mirror every 10 minutes. A
mirror pass also runs automatically after each monitoring run.

---

## How it works

| Piece | File | Purpose |
|-------|------|---------|
| Connection | `backend/app/snowflake/client.py` | Async-safe (thread-wrapped) key-pair connection; no-op when disabled. |
| Table spec | `backend/app/snowflake/tables.py` | Single source of truth mapping each SQLite model -> Snowflake table. |
| Schema | `backend/app/snowflake/schema.py` | `CREATE TABLE IF NOT EXISTS` for mirror + control tables. |
| Mirror | `backend/app/snowflake/mirror.py` | Watermark-based incremental `MERGE` (idempotent). |
| Cortex | `backend/app/snowflake/cortex.py` | Sentiment rollups + `CORTEX.COMPLETE` executive briefing. |
| Q&A | `backend/app/snowflake/analyst.py` | Cortex text-to-SQL (read-only guarded) + narrated answer. |
| Events | `backend/app/snowflake/events.py` | Captures every API request/response into `APP_EVENTS`. |
| API | `backend/app/api/cortex.py` | `/snowflake/status`, `/snowflake/sync`, `/cortex/insights`, `/cortex/ask`. |
| UI | `frontend/src/pages/Cortex.tsx` | Dashboard -> Cortex tab. |

## API endpoints

- `GET  /snowflake/status` — connectivity + per-table mirror watermarks.
- `POST /snowflake/sync` — trigger an incremental mirror pass.
- `GET  /cortex/insights` — Cortex sentiment rollups + executive briefing (cached 5 min; `?force=true` to refresh).
- `POST /cortex/ask` — `{ "question": "..." }` → `{ answer, generated_sql, columns, rows }`.

## Notes & future upgrades

- **Read-only guard:** `/cortex/ask` only executes generated `SELECT`/`WITH` statements;
  any DML/DDL or stacked statements are rejected.
- **PII/secrets:** `APP_EVENTS` bodies are credential-redacted before storage; disable
  capture with `SNOWFLAKE_CAPTURE_EVENTS=false`.
- **Full Cortex Analyst:** the current Q&A uses Cortex text-to-SQL, which needs no extra
  setup. To upgrade to managed **Cortex Analyst**, add a semantic model YAML to a stage
  and point `analyst.py` at the Cortex Analyst REST endpoint (JWT from the same key-pair).
