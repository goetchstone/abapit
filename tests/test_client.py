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


def test_blueprint_relationship_add_sends_jsonapi(org, fake_tokens):
    import json
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(204)  # relationship writes return No Content

    client = ApiClient(org, transport=httpx.MockTransport(handler))
    client.add_blueprint_relationship("BP1", "apps", ["A1", "A2"])
    assert captured["method"] == "POST"
    assert captured["path"].endswith("/blueprints/BP1/relationships/apps")
    assert captured["body"] == {"data": [{"type": "apps", "id": "A1"},
                                         {"type": "apps", "id": "A2"}]}


def test_blueprint_orgdevices_relationship_path_and_type(org, fake_tokens):
    """Apple's segment is /relationships/orgDevices (NOT /devices) and the
    member type is "orgDevices" — verified against the published reference."""
    import json
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(204)

    client = ApiClient(org, transport=httpx.MockTransport(handler))
    client.remove_blueprint_relationship("BP1", "devices", ["S1"])
    assert captured["method"] == "DELETE"
    assert captured["path"].endswith("/blueprints/BP1/relationships/orgDevices")
    assert captured["body"] == {"data": [{"type": "orgDevices", "id": "S1"}]}


def test_blueprint_include_falls_back_when_role_cant_read_a_relationship(org, fake_tokens):
    """A Content Manager role 403s on users/userGroups, and Apple then rejects
    the whole blueprint fetch with 400. The blueprint itself is readable, so we
    retry without includes rather than failing the page."""
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "include=" in str(request.url):
            return httpx.Response(400, json={"errors": [{"title": "Bad Request"}]})
        return httpx.Response(200, json={"data": {"type": "blueprints", "id": "BP1"}})

    client = ApiClient(org, transport=httpx.MockTransport(handler))
    body = client.blueprint("BP1", include="apps,users,userGroups")
    assert body["data"]["id"] == "BP1"        # page still renders
    assert len(calls) == 2 and "include=" not in calls[1]


def test_org_units_use_organizationalunits_path(org, fake_tokens):
    """The resource path is /v1/organizationalUnits — "orgUnits" 404s."""
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"data": []})

    client = ApiClient(org, transport=httpx.MockTransport(handler))
    client.org_units()
    client.org_unit("OU1")
    client.org_unit_user_ids("OU1")
    assert paths[0].endswith("/v1/organizationalUnits")
    assert paths[1].endswith("/v1/organizationalUnits/OU1")
    assert paths[2].endswith("/v1/organizationalUnits/OU1/relationships/users")


def test_blueprint_crud_sends_correct_verbs_and_bodies(org, fake_tokens):
    import json
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path,
                     json.loads(request.content) if request.content else None))
        return httpx.Response(200, json={"data": {"type": "blueprints", "id": "BP9"}})

    client = ApiClient(org, transport=httpx.MockTransport(handler))
    client.create_blueprint({"name": "Kiosk"})
    client.update_blueprint("BP9", {"name": "Kiosk 2"})
    client.delete_blueprint("BP9")

    method, path, body = seen[0]
    assert method == "POST" and path.endswith("/v1/blueprints")
    assert body == {"data": {"type": "blueprints", "attributes": {"name": "Kiosk"}}}
    method, path, body = seen[1]
    assert method == "PATCH" and path.endswith("/v1/blueprints/BP9")
    assert body["data"]["id"] == "BP9"          # id required in a JSON:API update
    method, path, _ = seen[2]
    assert method == "DELETE" and path.endswith("/v1/blueprints/BP9")


def test_configuration_and_mdm_server_writes_use_right_resources(org, fake_tokens):
    import json
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path,
                     json.loads(request.content) if request.content else None))
        return httpx.Response(200, json={"data": {"id": "X"}})

    client = ApiClient(org, transport=httpx.MockTransport(handler))
    client.create_configuration({"name": "VPN", "type": "CUSTOM_SETTING"})
    client.delete_configuration("C1")
    client.create_mdm_server({"serverName": "Jamf"})
    client.update_mdm_server("S1", {"serverName": "Jamf 2"})

    assert seen[0][0] == "POST" and seen[0][1].endswith("/v1/configurations")
    assert seen[0][2]["data"]["type"] == "configurations"
    assert seen[1][0] == "DELETE" and seen[1][1].endswith("/v1/configurations/C1")
    assert seen[2][0] == "POST" and seen[2][1].endswith("/v1/mdmServers")
    assert seen[2][2]["data"]["type"] == "mdmServers"
    assert seen[3][0] == "PATCH" and seen[3][1].endswith("/v1/mdmServers/S1")


def test_write_probes_classify_forbidden_and_ok(org, fake_tokens):
    def handler(request):
        # blueprint-relationship write probe forbidden; the rest allowed (404)
        if "/blueprints/" in request.url.path:
            return httpx.Response(403, json={"errors": [{"title": "Forbidden"}]})
        return httpx.Response(404, json={"errors": [{"title": "Not found"}]})

    client = ApiClient(org, transport=httpx.MockTransport(handler))
    status = {r["section"]: r["status"] for r in client.probe_capabilities()}
    assert status["blueprints_write"] == "forbidden"
    assert status["configurations_write"] == "ok"   # 404 => role allows the write
    assert status["mdm_servers_write"] == "ok"


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
