import httpx
import pytest

import abapit.client as client_mod
from abapit.assign import parse_serials, plan
from abapit.client import ApiClient


def dev(serial, model="MacBook Air"):
    return {"type": "orgDevices", "id": serial,
            "attributes": {"serialNumber": serial, "deviceModel": model}}


SERVERS = [
    {"type": "mdmServers", "id": "srv-1", "attributes": {"serverName": "Jamf"}},
    {"type": "mdmServers", "id": "srv-2", "attributes": {"serverName": "Intune"}},
]
DEVICES = [dev("AAA"), dev("BBB"), dev("CCC")]
CURRENT = {"srv-1": ["AAA"], "srv-2": ["BBB"]}  # CCC unassigned


def test_parse_serials_dedupes_and_normalizes():
    assert parse_serials("aaa, BBB\n ccc;aaa\tbbb") == ["AAA", "BBB", "CCC"]


def test_plan_assign_classifies_moves_noops_unknown():
    p = plan("AAA BBB CCC NOPE", "assign", "srv-1", DEVICES, SERVERS, CURRENT)
    assert [m["serial"] for m in p["moves"]] == ["BBB", "CCC"]
    assert p["moves"][0]["from_name"] == "Intune"
    assert p["moves"][1]["from_name"] == "(unassigned)"
    assert p["noops"][0]["serial"] == "AAA"  # already on srv-1
    assert p["unknown"] == ["NOPE"]
    assert p["server_name"] == "Jamf"


def test_plan_unassign_requires_current_assignment():
    p = plan("AAA BBB CCC", "unassign", "srv-1", DEVICES, SERVERS, CURRENT)
    assert [m["serial"] for m in p["moves"]] == ["AAA"]
    assert {n["serial"] for n in p["noops"]} == {"BBB", "CCC"}


def test_plan_rejects_bad_inputs():
    with pytest.raises(ValueError, match="action"):
        plan("AAA", "destroy", "srv-1", DEVICES, SERVERS, CURRENT)
    with pytest.raises(ValueError, match="unknown device management service"):
        plan("AAA", "assign", "srv-999", DEVICES, SERVERS, CURRENT)


def test_probe_capabilities_maps_403_and_validation_errors(org, monkeypatch):
    class FakeTokens:
        def get(self, org): return "tok"
        def invalidate(self, org): pass
    monkeypatch.setattr(client_mod, "token_cache", FakeTokens())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            # validation error, not 403 -> role allows writes
            return httpx.Response(422, json={"errors": [{"title": "invalid"}]})
        if "/v1/users" in request.url.path or "/v1/userGroups" in request.url.path:
            return httpx.Response(403, json={"errors": [{"title": "Forbidden"}]})
        return httpx.Response(200, json={"data": []})

    client = ApiClient(org, transport=httpx.MockTransport(handler))
    results = {r["capability"]: r["status"] for r in client.probe_capabilities()}
    assert results["Devices"] == "ok"
    assert results["Users"] == "forbidden"
    assert results["User groups"] == "forbidden"
    assert results["Audit events"] == "ok"
    assert results["Device assignment"] == "ok"  # 422 means authorized


def test_probe_write_denied(org, monkeypatch):
    class FakeTokens:
        def get(self, org): return "tok"
        def invalidate(self, org): pass
    monkeypatch.setattr(client_mod, "token_cache", FakeTokens())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(403, json={"errors": [{"title": "Forbidden"}]})
        return httpx.Response(200, json={"data": []})

    client = ApiClient(org, transport=httpx.MockTransport(handler))
    results = {r["capability"]: r["status"] for r in client.probe_capabilities()}
    assert results["Device assignment"] == "forbidden"


def test_create_device_activity_request_shape(org, monkeypatch):
    class FakeTokens:
        def get(self, org): return "tok"
        def invalidate(self, org): pass
    monkeypatch.setattr(client_mod, "token_cache", FakeTokens())
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = request.read().decode()
        return httpx.Response(201, json={"data": {
            "type": "orgDeviceActivities", "id": "act-1",
            "attributes": {"status": "IN_PROGRESS", "subStatus": "SUBMITTED"}}})

    client = ApiClient(org, transport=httpx.MockTransport(handler))
    activity = client.create_device_activity("ASSIGN_DEVICES", "srv-1", ["AAA", "BBB"])

    assert activity["id"] == "act-1"
    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/orgDeviceActivities"
    import json
    body = json.loads(captured["body"])
    assert body["data"]["attributes"]["activityType"] == "ASSIGN_DEVICES"
    assert body["data"]["relationships"]["mdmServer"]["data"] == {
        "type": "mdmServers", "id": "srv-1"}
    assert body["data"]["relationships"]["devices"]["data"] == [
        {"type": "orgDevices", "id": "AAA"}, {"type": "orgDevices", "id": "BBB"}]
