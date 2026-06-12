import time

import jwt

from abapit.auth import (ASSERTION_AUDIENCE, ASSERTION_LIFETIME,
                         build_client_assertion)


def test_assertion_lifetime_under_apple_cap():
    assert ASSERTION_LIFETIME < 180 * 86400


def test_client_assertion_claims_and_signature(org, ec_key_pair):
    _, public_pem = ec_key_pair
    now = int(time.time())
    token = build_client_assertion(org, now=now)

    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256"
    assert header["kid"] == "test-key-id"

    claims = jwt.decode(
        token, public_pem, algorithms=["ES256"], audience=ASSERTION_AUDIENCE)
    assert claims["sub"] == "BUSINESSAPI.test-client"
    # team_id defaults to client_id when unset
    assert claims["iss"] == "BUSINESSAPI.test-client"
    assert claims["iat"] == now
    assert claims["exp"] - claims["iat"] == ASSERTION_LIFETIME
    assert claims["jti"]


def test_issuer_uses_team_id_when_set(org, ec_key_pair):
    _, public_pem = ec_key_pair
    org.team_id = "TEAMID123"
    token = build_client_assertion(org)
    claims = jwt.decode(
        token, public_pem, algorithms=["ES256"], audience=ASSERTION_AUDIENCE)
    assert claims["iss"] == "TEAMID123"
