"""Upload the complete Cortex semantic-model YAML to a Snowflake stage.

The repo's ``cortex_semantic_model.yaml`` models the full evidence-monitoring dataset
(sentiment/brand/positioning + runs + alerts + consensus), unlike the partial native
Semantic View. This stages that YAML so the Cortex Agent chat widget can use it via the
Cortex Analyst REST API (``semantic_model_file``).

Run:  python -m scripts.upload_semantic_model

After it succeeds, set in your .env:
    SNOWFLAKE_SEMANTIC_MODEL_FILE=@EVIDENCE_DB.PUBLIC.SEMANTIC_MODELS/cortex_semantic_model.yaml
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config.settings import PROJECT_ROOT, get_settings  # noqa: E402
from app.snowflake import client  # noqa: E402

STAGE = "EVIDENCE_DB.PUBLIC.SEMANTIC_MODELS"
YAML_NAME = "cortex_semantic_model.yaml"


async def main() -> None:
    s = get_settings()
    if not client.is_enabled():
        print("Snowflake is not enabled (set SNOWFLAKE_ENABLED=true and credentials). Aborting.")
        return

    yaml_path = PROJECT_ROOT / YAML_NAME
    if not yaml_path.exists():
        print(f"Could not find {yaml_path}. Aborting.")
        return

    # Snowflake PUT wants a forward-slash file:// URI (works on Windows too).
    file_uri = "file://" + str(yaml_path).replace("\\", "/")

    print(f"Creating stage {STAGE} (if missing)...")
    await client.execute(
        f"CREATE STAGE IF NOT EXISTS {STAGE} "
        "DIRECTORY = (ENABLE = TRUE) "
        "ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')"
    )

    print(f"Uploading {YAML_NAME} -> @{STAGE} ...")
    rows = await client.execute(
        f"PUT '{file_uri}' @{STAGE} AUTO_COMPRESS=FALSE OVERWRITE=TRUE"
    )
    for r in rows:
        print("  ", r)

    print("\nVerifying stage contents:")
    listing = await client.execute(f"LIST @{STAGE}")
    for r in listing:
        print("  ", r.get("name"), r.get("size"))

    print(
        f"\nDone. Now set in .env:\n"
        f"  SNOWFLAKE_SEMANTIC_MODEL_FILE=@{STAGE}/{YAML_NAME}\n"
        f"and restart the backend."
    )


if __name__ == "__main__":
    asyncio.run(main())
