"""OAuth 2.0 client-credentials flow for the Apple Business / School APIs.

Apple's flow (documented at developer.apple.com under "Implementing OAuth for
the Apple School Manager and Apple Business API"):

1. Build a client assertion: an ES256-signed JWT whose `sub` is your client
   ID, `aud` is Apple's token audience, and `exp` is at most 180 days out.
2. POST it to https://account.apple.com/auth/oauth2/token with
   grant_type=client_credentials and scope business.api or school.api.
3. Receive a bearer token valid for one hour; refresh on expiry or 401.
"""

from __future__ import annotations

import time
import uuid

import httpx
import jwt

from .config import Org

TOKEN_URL = "https://account.apple.com/auth/oauth2/token"
ASSERTION_AUDIENCE = "https://account.apple.com/auth/oauth2/v2/token"
# Apple caps assertion validity at 180 days; stay safely under it.
ASSERTION_LIFETIME = 179 * 86400
SCOPES = {"business": "business.api", "school": "school.api"}


class AuthError(Exception):
    pass


def build_client_assertion(org: Org, now: int | None = None) -> str:
    now = int(time.time()) if now is None else now
    payload = {
        "iss": org.issuer,
        "sub": org.client_id,
        "aud": ASSERTION_AUDIENCE,
        "iat": now,
        "exp": now + ASSERTION_LIFETIME,
        "jti": str(uuid.uuid4()),
    }
    try:
        return jwt.encode(
            payload, org.private_key(), algorithm="ES256", headers={"kid": org.key_id}
        )
    except FileNotFoundError as exc:
        raise AuthError(f"Private key file not found: {org.private_key_path}") from exc
    except Exception as exc:
        raise AuthError(f"Could not sign client assertion: {exc}") from exc


def request_access_token(org: Org) -> tuple[str, int]:
    """Exchange a client assertion for a bearer token.

    Returns (token, expires_at_epoch).
    """
    assertion = build_client_assertion(org)
    data = {
        "grant_type": "client_credentials",
        "client_id": org.client_id,
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": assertion,
        "scope": SCOPES[org.scope],
    }
    try:
        resp = httpx.post(TOKEN_URL, data=data, timeout=30)
    except httpx.HTTPError as exc:
        raise AuthError(f"Could not reach Apple's token endpoint: {exc}") from exc
    if resp.status_code != 200:
        raise AuthError(
            f"Token request failed ({resp.status_code}): {resp.text[:500]}. "
            "Check the client ID, key ID, and private key for this org."
        )
    body = resp.json()
    return body["access_token"], int(time.time()) + int(body.get("expires_in", 3600))


class TokenCache:
    """Caches one bearer token per org, refreshing 60s before expiry."""

    def __init__(self) -> None:
        self._tokens: dict[str, tuple[str, int]] = {}

    def get(self, org: Org) -> str:
        cached = self._tokens.get(org.client_id)
        if cached and cached[1] - 60 > time.time():
            return cached[0]
        token, expires_at = request_access_token(org)
        self._tokens[org.client_id] = (token, expires_at)
        return token

    def invalidate(self, org: Org) -> None:
        self._tokens.pop(org.client_id, None)


token_cache = TokenCache()
