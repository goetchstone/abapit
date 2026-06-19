import json

import httpx
import pytest

from abapit import config
from abapit.client import ApiError, sections_for
from abapit.config import Org
from abapit.mosyle import MosyleClient, adapt_device


def mosyle_org(token="tok-123", email="", password=""):
    return Org(name="Acme Mosyle", scope="business", client_id="mosyle.acme",
               key_id="", private_key_path="", provider="mosyle",
               mosyle_token=token, mosyle_email=email, mosyle_password=password)


def devices_envelope(devices):
    """The real Mosyle list response shape."""
    return {"status": "OK", "response": [
        {"devices": devices, "rows": len(devices), "page": 1, "page_size": 50}]}


def not_found_envelope():
    return {"status": "OK", "response": [
        {"devices_notfound": [], "status": "DEVICES_NOTFOUND", "info": "No devices found"}]}


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


def test_devices_iterates_os_and_merges():
    # The list requires `os`; devices() iterates ios/mac/tvos/visionos.
    per_os = {
        "ios": [{"serial_number": "P1", "os": "ios", "device_model": "iPhone16,1"}],
        "mac": [{"serial_number": "M1", "os": "mac", "device_model": "Mac15,12"}],
    }
    seen_os = []

    def handler(request):
        os_value = json.loads(request.content)["options"]["os"]
        seen_os.append(os_value)
        rows = per_os.get(os_value)
        return httpx.Response(200, json=devices_envelope(rows) if rows else not_found_envelope())

    client = MosyleClient(mosyle_org(), transport=httpx.MockTransport(handler))
    assert sorted(d["id"] for d in client.devices()) == ["M1", "P1"]
    assert seen_os == ["ios", "mac", "tvos", "visionos"]  # all platforms queried


def test_devices_paginates_within_an_os(monkeypatch):
    monkeypatch.setattr("abapit.mosyle.PAGE_SIZE", 2)
    pages = {1: [{"serial_number": "A", "os": "mac"}, {"serial_number": "B", "os": "mac"}],
             2: [{"serial_number": "C", "os": "mac"}]}

    def handler(request):
        opts = json.loads(request.content)["options"]
        if opts["os"] != "mac":
            return httpx.Response(200, json=not_found_envelope())
        return httpx.Response(200, json=devices_envelope(pages.get(opts["page"], [])))

    client = MosyleClient(mosyle_org(), transport=httpx.MockTransport(handler))
    assert sorted(d["id"] for d in client.devices()) == ["A", "B", "C"]


def test_devices_stops_when_pages_repeat(monkeypatch):
    # If Mosyle ignores `page` and re-returns a full page, the no-new guard
    # must stop us rather than loop to max_pages.
    monkeypatch.setattr("abapit.mosyle.PAGE_SIZE", 1)

    def handler(request):
        if json.loads(request.content)["options"]["os"] != "mac":
            return httpx.Response(200, json=not_found_envelope())
        return httpx.Response(200, json=devices_envelope([{"serial_number": "A", "os": "mac"}]))

    client = MosyleClient(mosyle_org(), max_pages=50, transport=httpx.MockTransport(handler))
    assert [d["id"] for d in client.devices()] == ["A"]


def test_rows_parses_real_envelope_and_empties():
    body = devices_envelope([{"serial_number": "X"}])
    assert MosyleClient._rows(body)[0]["serial_number"] == "X"
    assert MosyleClient._rows(not_found_envelope()) == []
    assert MosyleClient._rows({"response": [{"status": "MISSING_DATA"}]}) == []
    assert MosyleClient._rows({"devices": [{"serial_number": "Y"}]})[0]["serial_number"] == "Y"
    assert MosyleClient._rows({"nope": 1}) == []


def test_access_token_header_is_sent():
    captured = {}

    def handler(request):
        captured["token"] = request.headers.get("accessToken")
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=not_found_envelope())

    client = MosyleClient(mosyle_org("secret-tok"), transport=httpx.MockTransport(handler))
    client.devices()
    assert captured["token"] == "secret-tok"
    assert captured["auth"] is None  # token-only org: no Bearer without creds


def test_login_obtains_and_sends_bearer():
    logins = []
    seen = []

    def handler(request):
        if request.url.path.endswith("/login"):
            logins.append(json.loads(request.content))
            return httpx.Response(200, headers={"Authorization": "Bearer JWT-XYZ"},
                                  json={"UserID": "1", "email": "admin@acme.com"})
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200, json=not_found_envelope())

    client = MosyleClient(mosyle_org("tok", "admin@acme.com", "pw"),
                          transport=httpx.MockTransport(handler))
    client.devices()
    assert logins and logins[0] == {"email": "admin@acme.com", "password": "pw"}
    assert seen and all(h == "Bearer JWT-XYZ" for h in seen)  # JWT cached + reused
    assert len(logins) == 1                                   # logged in once


def test_login_failure_surfaces_apierror():
    client = MosyleClient(mosyle_org("tok", "admin@acme.com", "bad"),
                          transport=httpx.MockTransport(
                              lambda r: httpx.Response(401, json={"info": "invalid login"})))
    with pytest.raises(ApiError) as exc:
        client.ping()
    assert exc.value.status == 401


def test_users_and_groups_parse_envelope_and_adapt():
    def handler(request):
        op = json.loads(request.content)["operation"]
        if op == "list_users":
            return httpx.Response(200, json={"status": "OK", "response": [{"users": [
                {"iduser": "10", "name": "Sarah", "email": "sarah@acme.com", "type": "ENDUSER"}]}]})
        if op == "list_usergroup":
            return httpx.Response(200, json={"status": "OK", "response": [{"usergroups": [
                {"idusergroup": "1000", "name": "Sales", "idusers_primary": [1, 2]}]}]})
        if op == "list_devicegroup":
            # device groups require an os (real shape: response is a dict)
            if json.loads(request.content)["options"].get("os") != "ios":
                return httpx.Response(200, json=not_found_envelope())
            return httpx.Response(200, json={"status": "OK", "response": {"devicegroups": [
                {"id": "3510", "name": "Front Desk", "device_numbers": 7, "os": "ios"}]}})
        return httpx.Response(200, json=not_found_envelope())

    client = MosyleClient(mosyle_org(), transport=httpx.MockTransport(handler))
    user = client.users()[0]
    assert user["type"] == "mosyleUsers" and user["id"] == "10"
    assert user["attributes"]["name"] == "Sarah" and user["attributes"]["userType"] == "ENDUSER"
    ug = client.user_groups()[0]
    assert ug["type"] == "userGroups" and ug["id"] == "1000"
    assert ug["attributes"]["memberCount"] == 2          # idusers_primary length
    dg = client.device_groups()[0]
    assert dg["type"] == "deviceGroups" and dg["id"] == "3510"
    assert dg["attributes"]["memberCount"] == 7 and dg["attributes"]["name"] == "Front Desk"


LOGS_RESPONSE = {
    "av": {"Logs": [], "Page": 1, "TotalLogs": 0},
    "zero_trust": {"Events": {"TotalLogs": 1, "Logs": [
        {"Device": "MacBook Air", "Application": "com.google.Chrome",
         "Action": "Trusted", "Source": "Manual", "Timestamp": "1710264696"}]}},
    "dns": [],
    "compliance": {"macOS": {"Logs": []}, "iOS": {"Logs": [
        {"Status": "Lost Compliance", "RuleName": "Cookies allowed only from visited sites",
         "Timestamp": "1710260000", "DeviceName": "iPad 100",
         "SerialNumber": "123456CD78", "E-mail": "test@mail.com"}]}},
    "action_logs": {"Logs": [
        {"UserName": "Jane Smith", "Action": "Save Device Group",
         "ActionDate": "1710267184", "IP": "::1", "E-mail": "jane@acme.com"}]},
}


def test_normalize_logs_flattens_all_types():
    from abapit.mosyle import normalize_logs
    events = normalize_logs(LOGS_RESPONSE)
    kinds = [e["kind"] for e in events]
    assert {"compliance", "zero_trust", "action"} <= set(kinds)
    comp = next(e for e in events if e["kind"] == "compliance")
    assert comp["status"] == "Lost Compliance" and comp["serial"] == "123456CD78"
    assert comp["device"] == "iPad 100"
    zt = next(e for e in events if e["kind"] == "zero_trust")
    assert zt["status"] == "Trusted" and zt["device"] == "MacBook Air"
    act = next(e for e in events if e["kind"] == "action")
    assert act["user"] == "Jane Smith" and "Save Device Group" in act["label"]
    whens = [e["when"] for e in events if e["when"]]
    assert whens == sorted(whens, reverse=True)        # newest first
    assert normalize_logs({}) == []


def test_append_mosyle_logs_accumulates_and_dedups(tmp_path, monkeypatch):
    monkeypatch.setenv("ABAPIT_DATA_DIR", str(tmp_path))
    from abapit import history
    e1 = [{"kind": "action", "when": "2025-06-01T10:00:00Z", "label": "A", "user": "jane"}]
    e2 = [{"kind": "action", "when": "2025-06-01T11:00:00Z", "label": "B", "user": "joe"}]
    history.append_mosyle_logs("mosyle.x", e1)
    history.append_mosyle_logs("mosyle.x", e2)
    history.append_mosyle_logs("mosyle.x", e1)   # duplicate drain
    history.append_mosyle_logs("mosyle.x", [])   # empty drain must keep history
    stored = history.load_mosyle_logs("mosyle.x")
    assert [s["label"] for s in stored] == ["B", "A"]  # newest first, deduped, retained


def test_logs_returns_empty_without_token():
    client = MosyleClient(mosyle_org("dtok"),
                          transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert client.logs() == []  # no logs token configured -> no call made


def test_logs_logs_in_on_separate_host_with_logs_token():
    seen = {}

    def handler(request):
        if request.url.path.endswith("/login"):
            seen["login_host"] = request.url.host
            seen["login_token"] = request.headers.get("accessToken")
            return httpx.Response(200, headers={"Authorization": "Bearer LJWT"},
                                  json={"UserID": "1"})
        seen["logs_host"] = request.url.host
        seen["bearer"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"status": "OK", "response": LOGS_RESPONSE})

    org = mosyle_org("dtok", "admin@acme.com", "pw")
    org.mosyle_logs_token = "ltok"
    client = MosyleClient(org, transport=httpx.MockTransport(handler))
    events = client.logs()
    assert seen["login_host"] == "businessapilogs.mosyle.com"   # separate host
    assert seen["login_token"] == "ltok"                        # the LOGS token, not the device token
    assert seen["bearer"] == "Bearer LJWT"
    assert any(e["kind"] == "compliance" for e in events)


def test_device_lookup_filters_the_listing():
    def handler(request):
        if json.loads(request.content)["options"]["os"] != "mac":
            return httpx.Response(200, json=not_found_envelope())
        return httpx.Response(200, json=devices_envelope([
            {"serial_number": "A", "os": "mac"}, {"serial_number": "B", "os": "mac"}]))

    client = MosyleClient(mosyle_org(), transport=httpx.MockTransport(handler))
    assert client.device("B")["id"] == "B"
    assert client.device("missing") == {}


def test_ping_raises_apierror_on_403():
    client = MosyleClient(mosyle_org(), transport=httpx.MockTransport(
        lambda r: httpx.Response(403, json={"info": "forbidden"})))
    with pytest.raises(ApiError) as exc:
        client.ping()
    assert exc.value.status == 403


def test_probe_capabilities_reads_ok_and_forbidden():
    ok = MosyleClient(mosyle_org(), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=not_found_envelope())))
    assert ok.probe_capabilities()[0]["status"] == "ok"
    bad = MosyleClient(mosyle_org(), transport=httpx.MockTransport(
        lambda r: httpx.Response(401, json={"info": "bad token"})))
    assert bad.probe_capabilities()[0]["status"] == "forbidden"


def test_no_mdm_servers_for_mosyle():
    client = MosyleClient(mosyle_org())
    assert client.mdm_servers() == []
    assert client.device_assigned_server("anything") is None
    assert client.device_applecare("anything") == []


def test_sections_for_provider():
    mosyle_sections = sections_for("business", "mosyle")
    assert "devices" in mosyle_sections
    assert "mosyle_os_breakdown" in mosyle_sections
    assert "mosyle_stale" in mosyle_sections
    assert "users" in mosyle_sections               # Mosyle inventory (Phase 2)
    assert "device_groups" in mosyle_sections
    assert "assign" not in mosyle_sections          # ABM-only write
    assert "apps" not in mosyle_sections            # ABM-only content
    assert "reconciliation" not in mosyle_sections  # cross-org, gated in render()
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


def test_edit_mosyle_org_keeps_blanks_and_updates_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path))
    slug = config.add_org(name="Acme", provider="mosyle", mosyle_token="tok",
                          mosyle_email="old@acme.com", mosyle_password="old")
    config.edit_org(slug, name="Acme Renamed", mosyle_token="", mosyle_email="new@acme.com",
                    mosyle_password="", mosyle_logs_token="ltok")
    org = config.load().orgs[slug]
    assert org.name == "Acme Renamed"
    assert org.mosyle_email == "new@acme.com"
    assert org.mosyle_password == "old"        # blank password = keep current
    assert org.mosyle_token == "tok"           # blank token = keep current
    assert org.mosyle_logs_token == "ltok"     # newly added


def test_web_edit_org_prefills_and_saves(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import abapit.web.app as app_mod

    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("ABAPIT_DATA_DIR", str(tmp_path / "data"))
    slug = config.add_org(name="Acme", provider="mosyle", mosyle_token="tok",
                          mosyle_email="a@acme.com", mosyle_password="pw")
    client = TestClient(app_mod.create_app(), base_url="http://127.0.0.1",
                        follow_redirects=False)
    form = client.get(f"/settings/orgs/{slug}/edit")
    assert form.status_code == 200 and b'value="tok"' in form.content   # pre-filled
    saved = client.post(f"/settings/orgs/{slug}/edit",
                        data={"name": "Acme", "token": "tok", "email": "a@acme.com",
                              "logs_token": "ltok"})
    assert saved.status_code in (200, 303)
    assert config.load().orgs[slug].mosyle_logs_token == "ltok"


def test_to_iso_normalization():
    from abapit.mosyle import _to_iso
    assert _to_iso(1718000000).startswith("2024-06-10")        # epoch int
    assert _to_iso("1718000000").startswith("2024-06-10")      # epoch numeric string
    assert _to_iso("2024-06-10T12:00:00Z") == "2024-06-10T12:00:00Z"  # ISO passthrough
    assert _to_iso("2024-06-10") == "2024-06-10"               # date-only kept
    assert _to_iso("not a date") is None                       # junk dropped, per contract
    assert _to_iso(None) is None and _to_iso("") is None


def test_duplicate_client_id_rejected(tmp_path, monkeypatch, ec_key_pair):
    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path))
    key_path, _ = ec_key_pair
    config.add_org(name="ABM One", scope="business", client_id="BUSINESSAPI.dup",
                   key_id="k", private_key_path=str(key_path))
    with pytest.raises(ValueError, match="already used"):
        config.add_org(name="ABM Two", scope="business", client_id="BUSINESSAPI.dup",
                       key_id="k2", private_key_path=str(key_path))


def test_apple_org_cannot_squat_mosyle_synthetic_id(tmp_path, monkeypatch, ec_key_pair):
    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path))
    key_path, _ = ec_key_pair
    mosyle_slug = config.add_org(name="Acme", provider="mosyle", mosyle_token="tok")
    with pytest.raises(ValueError, match="already used"):
        config.add_org(name="Sneaky", scope="business",
                       client_id=f"mosyle.{mosyle_slug}", key_id="k",
                       private_key_path=str(key_path))


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
        if json.loads(request.content)["options"]["os"] != "mac":
            return httpx.Response(200, json=not_found_envelope())
        return httpx.Response(200, json=devices_envelope([{
            "serial_number": "MOSY-1", "os": "mac", "device_model_name": "MacBook Air",
            "status": "IN", "osversion": "15.5", "date_last_beat": 1718000000}]))

    monkeypatch.setattr(app_mod, "build_client",
                        lambda o: MosyleClient(o, transport=httpx.MockTransport(handler)))
    client = TestClient(app_mod.create_app(), base_url="http://127.0.0.1",
                        follow_redirects=False)

    devices = client.get("/devices")
    assert devices.status_code == 200
    assert b"MOSY-1" in devices.content and b"MacBook Air" in devices.content
    # Nav is gated to Mosyle's sections: no ABM-only write/content sections,
    # but Mosyle inventory (device groups) is present.
    assert b'href="/assign"' not in devices.content
    assert b'href="/apps"' not in devices.content
    assert b'href="/device-groups"' in devices.content

    home = client.get("/")
    assert home.status_code == 200
    assert b"Not assigned to any MDM" not in home.content  # ABM-only panel hidden

    detail = client.get("/devices/MOSY-1")
    assert detail.status_code == 200
    assert b"Managed in Mosyle" in detail.content   # single-provider 360
    assert b"15.5" in detail.content                # live OS from Mosyle


# ---- reconciliation + posture report pages ----------------------------------

class _FakeClient:
    is_demo = False

    def __init__(self, org, devices):
        self.org = org
        self._devices = devices

    def devices(self):
        return self._devices

    def mdm_servers(self):
        return []

    def mdm_server_device_ids(self, server_id):
        return []

    def device(self, device_id):
        return next((d for d in self._devices if d["id"] == device_id), {})

    def device_applecare(self, device_id):
        return []

    def device_assigned_server(self, device_id):
        return None

    def audit_events(self, start_iso, end_iso, event_type=""):
        return []

    def ping(self):
        return None

    def close(self):
        return None


def _abm_item(serial, status="ASSIGNED"):
    return {"type": "orgDevices", "id": serial, "attributes": {
        "serialNumber": serial, "status": status, "productFamily": "Mac",
        "deviceModel": "MacBook Air", "addedToOrgDateTime": "2025-01-01T00:00:00Z"}}


def _mos_item(serial, days=1):
    from datetime import datetime, timedelta, timezone
    checkin = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"type": "orgDevices", "id": serial, "attributes": {
        "serialNumber": serial, "status": "active", "productFamily": "Mac",
        "deviceModel": "MacBook Air", "osVersion": "15.5", "lastCheckIn": checkin,
        "currentUser": "sarah@acme.com", "managedBy": "Mosyle"}}


def _fake_build(org):
    if org.provider == "mosyle":
        return _FakeClient(org, [_mos_item("A1"), _mos_item("M1", days=99)])
    return _FakeClient(org, [_abm_item("A1"), _abm_item("A2")])


def _two_org_client(tmp_path, monkeypatch, ec_key_pair, both=True):
    """Configure an Apple org (+ optionally a Mosyle org) and return a
    TestClient with build_client faked. Apple org is active."""
    from fastapi.testclient import TestClient

    import abapit.web.app as app_mod

    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("ABAPIT_DATA_DIR", str(tmp_path / "data"))
    key_path, _ = ec_key_pair
    abm_slug = config.add_org(name="Acme ABM", scope="business",
                              client_id="BUSINESSAPI.x", key_id="k",
                              private_key_path=str(key_path))
    mosyle_slug = None
    if both:
        mosyle_slug = config.add_org(name="Acme Mosyle", provider="mosyle",
                                     mosyle_token="tok")
    monkeypatch.setattr(app_mod, "build_client", _fake_build)
    client = TestClient(app_mod.create_app(), base_url="http://127.0.0.1",
                        follow_redirects=False)
    return client, abm_slug, mosyle_slug


def test_reconciliation_page_buckets_and_nav(tmp_path, monkeypatch, ec_key_pair):
    client, _abm, _mosyle = _two_org_client(tmp_path, monkeypatch, ec_key_pair)
    resp = client.get("/reports/reconciliation")
    assert resp.status_code == 200
    body = resp.content
    assert b"A2" in body and b"M1" in body          # abm_only + mosyle_only shown
    assert b"/reports/reconciliation" in body        # nav link present
    assert b"%" in body                              # enrollment-rate KPI rendered


def test_reconciliation_requires_both_providers(tmp_path, monkeypatch, ec_key_pair):
    client, _abm, _none = _two_org_client(tmp_path, monkeypatch, ec_key_pair, both=False)
    resp = client.get("/reports/reconciliation")
    assert resp.status_code == 200
    assert b"Configure" in resp.content and b"both" in resp.content  # banner, not a crash


def test_mosyle_posture_pages_render_for_mosyle_org(tmp_path, monkeypatch, ec_key_pair):
    client, _abm, mosyle_slug = _two_org_client(tmp_path, monkeypatch, ec_key_pair)
    config.set_active(mosyle_slug)
    os_page = client.get("/reports/mosyle-os-breakdown")
    assert os_page.status_code == 200 and b"15.5" in os_page.content
    stale = client.get("/reports/mosyle-stale?days=30")
    assert stale.status_code == 200 and b"M1" in stale.content  # 99d stale shown
    assert b"A1" not in stale.content or b"Never" in stale.content  # A1 (1d) excluded


def test_mosyle_posture_gated_off_apple_org(tmp_path, monkeypatch, ec_key_pair):
    client, _abm, _mosyle = _two_org_client(tmp_path, monkeypatch, ec_key_pair)
    # Apple org is active → Mosyle-only sections are not available.
    assert b"not available" in client.get("/reports/mosyle-os-breakdown").content
    assert b"not available" in client.get("/reports/mosyle-stale").content
    assert b"not available" in client.get("/device-groups").content


def test_web_renders_mosyle_people_and_groups(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import abapit.web.app as app_mod

    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("ABAPIT_DATA_DIR", str(tmp_path / "data"))
    slug = config.add_org(name="Acme Mosyle", provider="mosyle", mosyle_token="tok")
    config.set_active(slug)

    def handler(request):
        op = json.loads(request.content)["operation"]
        if op == "list_users":
            return httpx.Response(200, json={"status": "OK", "response": [{"users": [
                {"iduser": "10", "name": "Sarah Chen", "email": "sarah@acme.com"}]}]})
        if op == "list_usergroup":
            return httpx.Response(200, json={"status": "OK", "response": [{"usergroups": [
                {"idusergroup": "1000", "name": "Sales"}]}]})
        if op == "list_devicegroup":
            return httpx.Response(200, json={"status": "OK", "response": [{"devicegroups": [
                {"id": "3510", "name": "Front Desk", "device_numbers": 7}]}]})
        return httpx.Response(200, json=not_found_envelope())

    monkeypatch.setattr(app_mod, "build_client",
                        lambda o: MosyleClient(o, transport=httpx.MockTransport(handler)))
    client = TestClient(app_mod.create_app(), base_url="http://127.0.0.1",
                        follow_redirects=False)
    assert b"Sarah Chen" in client.get("/users").content
    assert b"Sales" in client.get("/user-groups").content
    assert b"Front Desk" in client.get("/device-groups").content
    assert b"/device-groups" in client.get("/").content   # nav entry for Mosyle orgs


def test_device_360_merges_both_providers(tmp_path, monkeypatch, ec_key_pair):
    # Active org is the Apple one; A1 exists in both ABM and Mosyle.
    client, _abm, _mosyle = _two_org_client(tmp_path, monkeypatch, ec_key_pair)
    resp = client.get("/devices/A1")
    assert resp.status_code == 200
    body = resp.content
    assert b"Owned &amp; managed" in body          # present in both
    assert b"ASSIGNED" in body                      # ABM ownership half
    assert b"15.5" in body                          # Mosyle live half (osVersion)
    assert b"Activity" in body                      # digested per-device timeline


def test_device_360_unknown_serial_is_404(tmp_path, monkeypatch, ec_key_pair):
    client, _abm, mosyle_slug = _two_org_client(tmp_path, monkeypatch, ec_key_pair)
    config.set_active(mosyle_slug)  # active Mosyle org returns None for unknown serials
    resp = client.get("/devices/NOPE-999")
    assert b"not found" in resp.content.lower()  # 404 page, not a blank misleading record


def test_web_mosyle_activity_configured_and_not(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import abapit.web.app as app_mod

    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("ABAPIT_DATA_DIR", str(tmp_path / "data"))

    def handler(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, headers={"Authorization": "Bearer LJWT"}, json={})
        return httpx.Response(200, json={"status": "OK", "response": LOGS_RESPONSE})

    monkeypatch.setattr(app_mod, "build_client",
                        lambda o: MosyleClient(o, transport=httpx.MockTransport(handler)))
    client = TestClient(app_mod.create_app(), base_url="http://127.0.0.1",
                        follow_redirects=False)

    no_logs = config.add_org(name="Acme Mosyle", provider="mosyle", mosyle_token="tok",
                             mosyle_email="a@acme.com", mosyle_password="pw")
    config.set_active(no_logs)
    resp = client.get("/mosyle-activity")
    assert resp.status_code == 200 and b"isn't configured" in resp.content

    with_logs = config.add_org(name="Acme Logs", provider="mosyle", mosyle_token="tok2",
                               mosyle_email="a@acme.com", mosyle_password="pw",
                               mosyle_logs_token="ltok")
    config.set_active(with_logs)
    resp = client.get("/mosyle-activity")
    assert resp.status_code == 200
    assert b"Lost Compliance" in resp.content and b"iPad 100" in resp.content


def test_report_csv_exports(tmp_path, monkeypatch, ec_key_pair):
    client, abm_slug, mosyle_slug = _two_org_client(tmp_path, monkeypatch, ec_key_pair)
    recon = client.get(f"/export/reconciliation.csv?abm={abm_slug}&mosyle={mosyle_slug}")
    assert recon.status_code == 200
    assert recon.headers["content-type"].startswith("text/csv")
    assert "attachment" in recon.headers["content-disposition"]
    assert b"M1" in recon.content and b"A2" in recon.content

    config.set_active(mosyle_slug)
    os_csv = client.get("/export/mosyle-os-breakdown.csv")
    assert os_csv.status_code == 200 and os_csv.headers["content-type"].startswith("text/csv")
    stale_csv = client.get("/export/mosyle-stale.csv?days=30")
    assert stale_csv.status_code == 200 and b"M1" in stale_csv.content
