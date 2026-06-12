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

import json
import logging
import time
import uuid

import httpx
import jwt

from .config import Org, config_dir

log = logging.getLogger("abapit")

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
    log.info("minting new access token for %s (%s)", org.name, org.client_id)
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
    if resp.status_code == 429:
        raise AuthError(
            "Apple's token service is rate-limiting us (HTTP 429) — too many "
            "fresh tokens in a short window. Wait a minute and try again.")
    if resp.status_code != 200:
        raise AuthError(
            f"Token request failed ({resp.status_code}): {resp.text[:500]}. "
            "Check the client ID, key ID, and private key for this org."
        )
    body = resp.json()
    return body["access_token"], int(time.time()) + int(body.get("expires_in", 3600))


class TokenCache:
    """One bearer token per org, refreshed 60s before expiry.

    Tokens are also persisted (0600) so separate processes — the web server,
    CLI runs, cron snapshots — share one token per hour instead of each
    minting their own; Apple rate-limits the token endpoint.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, tuple[str, int]] = {}

    def _disk_path(self):
        return config_dir() / "tokens.json"

    def _load_disk(self) -> dict:
        try:
            return json.loads(self._disk_path().read_text())
        except (OSError, ValueError):
            return {}

    def _write_disk(self, data: dict) -> None:
        path = self._disk_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        path.chmod(0o600)

    def get(self, org: Org) -> str:
        now = time.time()
        cached = self._tokens.get(org.client_id)
        if cached and cached[1] - 60 > now:
            return cached[0]
        entry = self._load_disk().get(org.client_id)
        if entry and entry.get("expires_at", 0) - 60 > now:
            self._tokens[org.client_id] = (entry["token"], entry["expires_at"])
            return entry["token"]
        token, expires_at = request_access_token(org)
        self._tokens[org.client_id] = (token, expires_at)
        data = self._load_disk()
        data[org.client_id] = {"token": token, "expires_at": expires_at}
        self._write_disk(data)
        return token

    def invalidate(self, org: Org) -> None:
        self._tokens.pop(org.client_id, None)
        data = self._load_disk()
        if org.client_id in data:
            del data[org.client_id]
            self._write_disk(data)


token_cache = TokenCache()
