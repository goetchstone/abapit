import httpx
import pytest

import abapit.client as client_mod
from abapit.client import ApiClient, ApiError
from abapit.demo import DemoClient


class FakeTokenCache:
    def __init__(self):
        self.gets = 0
        self.invalidations = 0

    def get(self, org):
        self.gets += 1
        return f"token-{self.gets}"

    def invalidate(self, org):
        self.invalidations += 1


@pytest.fixture
def fake_tokens(monkeypatch):
    fake = FakeTokenCache()
    monkeypatch.setattr(client_mod, "token_cache", fake)
    return fake


def test_pagination_follows_links_next(org, fake_tokens):
    def handler(request: httpx.Request) -> httpx.Response:
        if "cursor=page2" in str(request.url):
            return httpx.Response(200, json={
                "data": [{"type": "orgDevices", "id": "SERIAL2"}]})
        return httpx.Response(200, json={
            "data": [{"type": "orgDevices", "id": "SERIAL1"}],
            "links": {"next": "https://api-business.apple.com/v1/orgDevices?cursor=page2"},
        })

    client = ApiClient(org, transport=httpx.MockTransport(handler))
    items = client.devices()
    assert [i["id"] for i in items] == ["SERIAL1", "SERIAL2"]


def test_401_invalidates_token_and_retries_once(org, fake_tokens):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.headers["Authorization"] == "Bearer token-1":
            return httpx.Response(401, json={"errors": [{"title": "expired"}]})
        return httpx.Response(200, json={"data": []})

    client = ApiClient(org, transport=httpx.MockTransport(handler))
    assert client.devices() == []
    assert fake_tokens.invalidations == 1
    assert calls["n"] == 2


def test_429_backs_off_and_retries(org, fake_tokens, monkeypatch):
    sleeps = []
    monkeypatch.setattr(client_mod.time, "sleep", sleeps.append)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"data": [{"type": "orgDevices", "id": "S1"}]})

    client = ApiClient(org, transport=httpx.MockTransport(handler))
    assert [i["id"] for i in client.devices()] == ["S1"]
    assert calls["n"] == 3
    assert sleeps == [7.0, 7.0]  # honored Retry-After


def test_429_gives_up_after_retries(org, fake_tokens, monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    client = ApiClient(org, transport=httpx.MockTransport(
        lambda request: httpx.Response(429, json={"errors": [{"title": "rate limited"}]})))
    with pytest.raises(ApiError) as exc:
        client.devices()
    assert exc.value.status == 429


def test_transient_network_error_retried_on_get(org, fake_tokens, monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RemoteProtocolError("multiple Transfer-Encoding headers")
        return httpx.Response(200, json={"data": []})

    client = ApiClient(org, transport=httpx.MockTransport(handler))
    assert client.devices() == []
    assert calls["n"] == 2


def test_network_error_not_retried_on_post(org, fake_tokens):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = ApiClient(org, transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError) as exc:
        client.create_device_activity("ASSIGN_DEVICES", "srv", ["AAA"])
    assert "network error" in str(exc.value)


def test_api_error_surfaces_apple_error_detail(org, fake_tokens):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"errors": [
            {"title": "Forbidden", "detail": "No access to this resource."}]})

    client = ApiClient(org, transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError) as exc:
        client.devices()
    assert exc.value.status == 403
    assert "No access" in str(exc.value)


def test_demo_client_mirrors_api_client_interface():
    api_methods = {name for name in dir(ApiClient)
                   if not name.startswith("_")
                   and callable(getattr(ApiClient, name))}
    demo_methods = {name for name in dir(DemoClient) if not name.startswith("_")}
    missing = api_methods - demo_methods - {"get", "list_all"}
    assert not missing, f"DemoClient is missing: {missing}"


def test_demo_data_is_coherent():
    demo = DemoClient()
    devices = demo.devices()
    assert len(devices) > 50
    serial = devices[0]["id"]
    assert demo.device(serial)["id"] == serial
    assert demo.device_applecare(serial)
    servers = demo.mdm_servers()
    assigned = {s for srv in servers for s in demo.mdm_server_device_ids(srv["id"])}
    assert assigned <= {d["id"] for d in devices}
