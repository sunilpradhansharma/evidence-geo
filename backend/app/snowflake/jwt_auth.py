"""Key-pair JWT generation for Snowflake REST APIs (Cortex Analyst / Agent).

The Cortex Analyst and Cortex Agent REST endpoints authenticate with a short-lived
RS256 JWT signed by the SAME RSA private key used for the connector's key-pair auth.
This reuses the key already loaded by ``client.py`` and follows Snowflake's JWT spec:

    iss = <ACCOUNT>.<USER>.SHA256:<base64 fingerprint of the public key>
    sub = <ACCOUNT>.<USER>
    iat = now,  exp = now + lifetime

The ACCOUNT identifier is the account locator with any region/cloud suffix stripped
and uppercased (e.g. ``xy12345.us-east-1`` -> ``XY12345``). The REST hostname keeps the
full account (dots preserved, underscores -> dashes).
"""
from __future__ import annotations

import base64
import hashlib
import time

from app.config.settings import get_settings
from app.snowflake import client


def account_identifier(account: str) -> str:
    """Account identifier for the JWT claims: strip region/cloud, uppercase."""
    return account.partition(".")[0].upper()


def rest_base_url(account: str) -> str:
    """Base URL for the Snowflake REST API for this account."""
    host = account.lower().replace("_", "-")
    return f"https://{host}.snowflakecomputing.com"


def _public_key_fingerprint(private_key_der: bytes) -> str:
    """SHA256 fingerprint of the public key derived from the private key (DER)."""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization

    priv = serialization.load_der_private_key(
        private_key_der, password=None, backend=default_backend()
    )
    pub_der = priv.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.sha256(pub_der).digest()
    return "SHA256:" + base64.b64encode(digest).decode("utf-8")


def generate_jwt(lifetime_seconds: int = 3600) -> str:
    """Generate a signed RS256 JWT for Snowflake REST API key-pair auth.

    Raises ``RuntimeError`` when no private key is configured (key-pair auth is required
    for the REST API; the password fallback used by the connector does not apply here).
    """
    import jwt as pyjwt
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization

    s = get_settings()
    der = client._load_private_key()
    if der is None:
        raise RuntimeError(
            "No Snowflake private key configured; key-pair auth is required for the "
            "Cortex REST API. Set SNOWFLAKE_PRIVATE_KEY_PATH or SNOWFLAKE_PRIVATE_KEY_B64."
        )

    account = account_identifier(s.snowflake_account)
    user = s.snowflake_user.upper()
    qualified_user = f"{account}.{user}"
    fingerprint = _public_key_fingerprint(der)

    now = int(time.time())
    payload = {
        "iss": f"{qualified_user}.{fingerprint}",
        "sub": qualified_user,
        "iat": now,
        "exp": now + lifetime_seconds,
    }
    priv = serialization.load_der_private_key(der, password=None, backend=default_backend())
    token = pyjwt.encode(payload, priv, algorithm="RS256")
    # PyJWT >= 2 returns str; older returns bytes — normalize.
    return token.decode("utf-8") if isinstance(token, bytes) else token
