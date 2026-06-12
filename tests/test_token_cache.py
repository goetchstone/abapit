import stat
import time

import pytest

import abapit.auth as auth_mod
from abapit.auth import TokenCache
from abapit.config import Org


@pytest.fixture
def org():
    return Org(name="T", scope="business", client_id="BUSINESSAPI.t",
               key_id="k", private_key_path="")


def test_tokens_persist_across_processes(tmp_path, monkeypatch, org):
    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path))
    mints = {"n": 0}

    def fake_mint(o):
        mints["n"] += 1
        return f"tok-{mints['n']}", int(time.time()) + 3600

    monkeypatch.setattr(auth_mod, "request_access_token", fake_mint)

    first = TokenCache()
    assert first.get(org) == "tok-1"
    assert first.get(org) == "tok-1"          # memory hit

    second = TokenCache()                      # simulates a new process
    assert second.get(org) == "tok-1"          # disk hit — no new mint
    assert mints["n"] == 1

    token_file = tmp_path / "tokens.json"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    second.invalidate(org)
    assert TokenCache().get(org) == "tok-2"    # invalidation hits disk too


def test_expired_disk_token_is_replaced(tmp_path, monkeypatch, org):
    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path))
    mints = {"n": 0}

    def fake_mint(o):
        mints["n"] += 1
        # token that is already inside the 60s refresh window
        return f"tok-{mints['n']}", int(time.time()) + 30

    monkeypatch.setattr(auth_mod, "request_access_token", fake_mint)
    cache = TokenCache()
    cache.get(org)
    cache2 = TokenCache()
    cache2.get(org)
    assert mints["n"] == 2  # near-expiry disk token not reused
