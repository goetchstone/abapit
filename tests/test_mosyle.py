import json

import httpx
import pytest

from abapit import config
from abapit.client import ApiError, sections_for
from abapit.config import Org
from abapit.mosyle import MosyleClient, adapt_device


def mosyle_org(token="tok-123"):
    return Org(name="Acme Mosyle", scope="business", client_id="mosyle.acme",
               key_id="", private_key_path="", provider="mosyle",
               mosyle_token=token)


SAMPLE = {
    "serial_number": "C02XJABCDEF",
    "device_name": "Sarah's MacBook",
    "device_model": "MacBook Air",
    "os": "mac",
    "osversion": "15.5",
    "status": "active",
    "battery": 82,
    "total_disk": 500107862016,
    "available_disk": 214748364800,
    "is_supervised": True,
    "userid": "sarah@acme.com",
    "date_last_beat": 1718000000,
    "wifi_mac_address": "AA:BB:CC:DD:EE:FF",
    "tags": ["finance"],
}


def test_adapt_device_maps_to_jsonapi_shape():
    item = adapt_device(SAMPLE)
    assert item["type"] == "orgDevices"
    assert item["id"] == "C02XJABCDEF"
    attrs = item["attributes"]
    assert attrs["serialNumber"] == "C02XJABCDEF"
    assert attrs["deviceModel"] == "MacBook Air"
    assert attrs["productFamily"] == "Mac"
    assert attrs["status"] == "active"          # the live per-device status
    assert attrs["osVersion"] == "15.5"
    assert attrs["isSupervised"] is True
    assert attrs["managedBy"] == "Mosyle"
    assert attrs["wifiMacAddress"] == "AA:BB:CC:DD:EE:FF"  # matches quick-find
    assert attrs["lastCheckIn"].startswith("2024") and attrs["lastCheckIn"].endswith("Z")
    assert attrs["tags"] == ["finance"]         # unmapped fields preserved


@pytest.mark.parametrize("os_value,model,expected", [
    ("mac", "MacBook Air", "Mac"),
    ("ios", "iPhone 16", "iPhone"),
    ("ios", "iPad Air (M2)", "iPad"),
    ("tvos", "Apple TV 4K", "AppleTV"),
])
def test_family_derivation(os_value, model, expected):
    item = adapt_device({"serial_number": "S", "os": os_value, "device_model": model})
    assert item["attributes"]["productFamily"] == expected


def test_devices_paginates_until_no_new_serials():
    pages = {
        1: {"devices": [{"serial_number": "A", "os": "mac"},
                        {"serial_number": "B", "os": "ios", "device_model": "iPhone 16"}]},
        2: {"devices": [{"serial_number": "C", "os": "mac"}]},
        3: {"devices": []},
    }
    seen_pages = []

    def handler(request):
        page = json.loads(request.content)["options"]["page"]
        seen_pages.append(page)
        return httpx.Response(200, json=pages.get(page, {"devices": []}))

    client = MosyleClient(mosyle_org(), transport=httpx.MockTransport(handler))
    assert [d["id"] for d in client.devices()] == ["A", "B", "C"]
    assert seen_pages == [1, 2, 3]


def test_devices_stops_when_pages_repeat():
    # If Mosyle ignores `page` and returns the whole fleet every time, the
    # no-new-serials guard must stop us rather than loop forever.
    def handler(request):
        return httpx.Response(200, json={"devices": [{"serial_number": "A", "os": "mac"}]})

    client = MosyleClient(mosyle_org(), transport=httpx.MockTransport(handler))
    assert [d["id"] for d in client.devices()] == ["A"]


def test_rows_tolerates_alternate_envelopes():
    assert MosyleClient._rows({"response": [{"serial_number": "X"}]})[0]["serial_number"] == "X"
    assert MosyleClient._rows({"data": {"devices": [{"serial_number": "Y"}]}})[0]["serial_number"] == "Y"
    assert MosyleClient._rows({"nope": 1}) == []


def test_token_header_is_sent():
    captured = {}

    def handler(request):
        captured["token"] = request.headers.get("accesstoken")
        return httpx.Response(200, json={"devices": []})

    client = MosyleClient(mosyle_org("secret-tok"), transport=httpx.MockTransport(handler))
    client.devices()
    assert captured["token"] == "secret-tok"


def test_device_lookup_filters_the_listing():
    def handler(request):
        page = json.loads(request.content)["options"]["page"]
        if page == 1:
            return httpx.Response(200, json={"devices": [
                {"serial_number": "A", "os": "mac"},
                {"serial_number": "B", "os": "mac"}]})
        return httpx.Response(200, json={"devices": []})

    client = MosyleClient(mosyle_org(), transport=httpx.MockTransport(handler))
    assert client.device("B")["id"] == "B"
    assert client.device("missing") == {}


def test_ping_raises_apierror_on_403():
    client = MosyleClient(mosyle_org(), transport=httpx.MockTransport(
        lambda r: httpx.Response(403, json={"error": "forbidden"})))
    with pytest.raises(ApiError) as exc:
        client.ping()
    assert exc.value.status == 403


def test_probe_capabilities_reads_ok_and_forbidden():
    ok = MosyleClient(mosyle_org(), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"devices": []})))
    assert ok.probe_capabilities()[0]["status"] == "ok"
    bad = MosyleClient(mosyle_org(), transport=httpx.MockTransport(
        lambda r: httpx.Response(401, json={"error": "bad token"})))
    assert bad.probe_capabilities()[0]["status"] == "forbidden"


def test_no_mdm_servers_for_mosyle():
    client = MosyleClient(mosyle_org())
    assert client.mdm_servers() == []
    assert client.device_assigned_server("anything") is None
    assert client.device_applecare("anything") == []


def test_sections_for_provider():
    assert sections_for("business", "mosyle") == ("devices",)
    assert "users" in sections_for("business", "apple")
    assert "users" not in sections_for("school", "apple")


def test_add_mosyle_org(tmp_path, monkeypatch):
    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path))
    slug = config.add_org(name="Acme Mosyle", provider="mosyle", mosyle_token="tok-1")
    org = config.load().orgs[slug]
    assert org.provider == "mosyle"
    assert org.mosyle_token == "tok-1"
    assert org.client_id == f"mosyle.{slug}"
    assert org.is_mosyle
    assert not (tmp_path / "keys").exists()  # no private key for Mosyle


def test_add_mosyle_org_requires_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        config.add_org(name="No Token", provider="mosyle", mosyle_token="")


def test_web_renders_mosyle_through_templates(tmp_path, monkeypatch):
    """End-to-end: a Mosyle org renders the real device pages, gated nav,
    and the provider-aware dashboard/detail — backed by a mocked transport."""
    from fastapi.testclient import TestClient

    import abapit.web.app as app_mod

    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("ABAPIT_DATA_DIR", str(tmp_path / "data"))
    slug = config.add_org(name="Acme Mosyle", provider="mosyle", mosyle_token="tok")
    config.set_active(slug)

    def handler(request):
        page = json.loads(request.content)["options"]["page"]
        if page == 1:
            return httpx.Response(200, json={"devices": [{
                "serial_number": "MOSY-1", "os": "mac",
                "device_model": "MacBook Air", "status": "active",
                "osversion": "15.5", "date_last_beat": 1718000000}]})
        return httpx.Response(200, json={"devices": []})

    monkeypatch.setattr(app_mod, "build_client",
                        lambda o: MosyleClient(o, transport=httpx.MockTransport(handler)))
    client = TestClient(app_mod.create_app(), base_url="http://127.0.0.1",
                        follow_redirects=False)

    devices = client.get("/devices")
    assert devices.status_code == 200
    assert b"MOSY-1" in devices.content and b"MacBook Air" in devices.content
    # Nav is gated to Mosyle's sections: no People/Content/Assign.
    assert b'href="/users"' not in devices.content
    assert b'href="/assign"' not in devices.content

    home = client.get("/")
    assert home.status_code == 200
    assert b"Not assigned to any MDM" not in home.content  # ABM-only panel hidden

    detail = client.get("/devices/MOSY-1")
    assert detail.status_code == 200
    assert b"Managed by" in detail.content   # not the MDM-assignment panel
