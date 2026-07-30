"""List Bedrock foundation models + inference profiles available in the account/region.

Run: python -m scripts.list_bedrock_models
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3  # noqa: E402
from app.config.settings import get_settings  # noqa: E402

s = get_settings()
session = boto3.Session(
    aws_access_key_id=s.aws_access_key_id or None,
    aws_secret_access_key=s.aws_secret_access_key or None,
    region_name=s.aws_region,
)
bedrock = session.client("bedrock")

print(f"\n=== Region: {s.aws_region} ===\n")

print("--- ON-DEMAND TEXT MODELS (ACTIVE) ---")
resp = bedrock.list_foundation_models(byOutputModality="TEXT")
for m in resp.get("modelSummaries", []):
    status = m.get("modelLifecycle", {}).get("status", "")
    inference = ",".join(m.get("inferenceTypesSupported", []))
    if status == "ACTIVE":
        print(f"{m['modelId']:60s} [{inference}]")

print("\n--- INFERENCE PROFILES (cross-region, newer models often need these) ---")
try:
    prof = bedrock.list_inference_profiles()
    for p in prof.get("inferenceProfileSummaries", []):
        print(f"{p['inferenceProfileId']:55s} {p.get('inferenceProfileName','')}")
except Exception as e:  # noqa: BLE001
    print(f"(could not list inference profiles: {e})")

print("\n\n=== RECOMMENDED PICKS (Anthropic / Amazon / Meta only) ===")
KEYWORDS = ("claude", "nova", "llama")
prof = bedrock.list_inference_profiles().get("inferenceProfileSummaries", [])
us_profiles = sorted(
    p["inferenceProfileId"] for p in prof if p["inferenceProfileId"].startswith("us.")
)
for pid in us_profiles:
    if any(k in pid for k in KEYWORDS):
        print(pid)
