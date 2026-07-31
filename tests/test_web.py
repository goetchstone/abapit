import time

import pytest
from fastapi.testclient import TestClient

from abapit import config, history
from abapit.config import Org
from abapit.web.app import create_app


@pytest.fixture(scope="module")
def web():
    app = create_app(demo=True)
    # base_url must be a host TrustedHostMiddleware accepts
    return TestClient(app, base_url="http://127.0.0.1", follow_redirects=False)


def test_pages_render(web):
    for path in ("/", "/devices", "/mdm-servers", "/users", "/blueprints",
                 "/org-units", "/settings", "/audit-events", "/reports/coverage",
                 "/reports/fleet-age"):
        assert web.get(path).status_code == 200, path


def test_blueprint_management_forms_show_in_demo(web):
    r = web.get("/blueprints/demo-blueprint-0")
    assert r.status_code == 200
    assert b'name="rel"' in r.content and b'value="users"' in r.content  # management forms present


def test_blueprint_relationship_preview_flags_unknown(web):
    r = web.post("/blueprints/demo-blueprint-0/relationships",
                 data={"rel": "apps", "op": "add", "ids": "NOPE-ID", "mode": "preview"})
    assert r.status_code == 200 and b"NOPE-ID" in r.content  # unknown id flagged, nothing sent


def test_blueprint_relationship_execute_adds_then_removes():
    # fresh app so mutation doesn't pollute the module-scoped demo state
    client = TestClient(create_app(demo=True), base_url="http://127.0.0.1",
                        follow_redirects=False)
    bp = "demo-blueprint-0"
    client.post(f"/blueprints/{bp}/relationships",
                data={"rel": "users", "op": "add", "ids": "demo-user-0", "mode": "execute"})
    assert b"demo-user-0" in client.get(f"/blueprints/{bp}").content
    client.post(f"/blueprints/{bp}/relationships",
                data={"rel": "users", "op": "remove", "ids": "demo-user-0", "mode": "execute"})
    assert b"demo-user-0" not in client.get(f"/blueprints/{bp}").content


def test_blueprint_create_edit_delete_flow():
    client = TestClient(create_app(demo=True), base_url="http://127.0.0.1",
                        follow_redirects=False)
    # create
    created = client.post("/blueprints", data={"name": "Kiosk Fleet",
                                               "description": "front desk"})
    assert created.status_code == 303
    bp_id = created.headers["location"].split("?")[0].rsplit("/", 1)[-1]
    assert b"Kiosk Fleet" in client.get("/blueprints").content

    # Apple requires BOTH name and description — neither may be sent blank.
    assert b"name and a description" in client.post(
        "/blueprints", data={"name": " ", "description": "x"}).content
    assert b"name and a description" in client.post(
        "/blueprints", data={"name": "No Desc", "description": " "}).content

    # edit
    client.post(f"/blueprints/{bp_id}/edit", data={"name": "Kiosk Fleet v2",
                                                   "description": "front desk"})
    assert b"Kiosk Fleet v2" in client.get("/blueprints").content

    # delete requires the exact typed name
    wrong = client.post(f"/blueprints/{bp_id}/delete", data={"confirm": "nope"})
    assert b"didn" in wrong.content                       # mismatch => refused
    assert b"Kiosk Fleet v2" in client.get("/blueprints").content   # still there
    gone = client.post(f"/blueprints/{bp_id}/delete", data={"confirm": "Kiosk Fleet v2"})
    assert gone.status_code == 303
    assert b"Kiosk Fleet v2" not in client.get("/blueprints").content


def test_configuration_create_edit_delete_flow():
    client = TestClient(create_app(demo=True), base_url="http://127.0.0.1",
                        follow_redirects=False)
    profile = '<?xml version="1.0"?><plist version="1.0"><dict/></plist>'

    # Apple REQUIRES configurationProfile on create — refuse before sending.
    bad = client.post("/configurations", data={"name": "X", "profile": ""})
    assert b"profile" in bad.content and b"required" in bad.content
    # filename, when given, must end in .mobileconfig
    bad2 = client.post("/configurations", data={
        "name": "X", "profile": profile, "filename": "wrong.txt"})
    assert b".mobileconfig" in bad2.content

    created = client.post("/configurations", data={
        "name": "Corp VPN", "platforms": ["PLATFORM_MACOS"],
        "profile": profile, "filename": "vpn.mobileconfig"})
    assert created.status_code == 303
    cid = created.headers["location"].split("?")[0].rsplit("/", 1)[-1]

    detail = client.get(f"/configurations/{cid}")
    assert b"configurationProfile" in detail.content   # real object shape round-trips
    assert b"vpn.mobileconfig" in detail.content
    assert b"Corp VPN" in client.get("/configurations").content

    # update is a partial PATCH: a blank profile means "leave it alone"
    client.post(f"/configurations/{cid}/edit", data={
        "name": "Corp VPN v2", "platforms": ["PLATFORM_MACOS", "PLATFORM_IOS"],
        "profile": ""})
    assert b"Corp VPN v2" in client.get("/configurations").content
    assert b"configurationProfile" in client.get(f"/configurations/{cid}").content

    wrong = client.post(f"/configurations/{cid}/delete", data={"confirm": "nope"})
    assert b"didn" in wrong.content
    client.post(f"/configurations/{cid}/delete", data={"confirm": "Corp VPN v2"})
    assert b"Corp VPN v2" not in client.get("/configurations").content


def test_mdm_server_create_edit_delete_shows_blast_radius():
    client = TestClient(create_app(demo=True), base_url="http://127.0.0.1",
                        follow_redirects=False)
    # Apple REQUIRES serverCertificate on create — a name-only POST must be
    # refused here rather than sent as a call that can only fail.
    no_cert = client.post("/mdm-servers", data={"server_name": "Jamf Test"})
    assert no_cert.status_code == 200 and b"certificate" in no_cert.content

    created = client.post("/mdm-servers", data={
        "server_name": "Jamf Test", "cert_name": "jamf.cer",
        "cert_data": "MIIDXTCCAkWgAwIBAgIJALx"})
    assert created.status_code == 303
    sid = created.headers["location"].split("?")[0].rsplit("/", 1)[-1]
    assert b"Jamf Test" in client.get("/mdm-servers").content

    client.post(f"/mdm-servers/{sid}/edit", data={"server_name": "Jamf Test v2"})
    assert b"Jamf Test v2" in client.get("/mdm-servers").content

    # a server WITH assigned devices must surface the enrollment warning
    seeded = client.get("/mdm-servers/demo-server-0/delete")
    assert b"Assigned devices" in seeded.content
    assert b"enrollment" in seeded.content

    wrong = client.post(f"/mdm-servers/{sid}/delete", data={"confirm": "nope"})
    assert b"didn" in wrong.content
    client.post(f"/mdm-servers/{sid}/delete", data={"confirm": "Jamf Test v2"})
    assert b"Jamf Test v2" not in client.get("/mdm-servers").content


def test_blueprint_writes_blocked_when_role_forbids(tmp_path, monkeypatch, ec_key_pair):
    """The template hides the buttons, but the POST itself must also refuse."""
    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("ABAPIT_DATA_DIR", str(tmp_path / "data"))
    key_path, _ = ec_key_pair
    slug = config.add_org(name="RO Org", scope="business", client_id="BUSINESSAPI.ro",
                          key_id="k", private_key_path=str(key_path))
    config.update_org_capabilities(slug, {"blueprints": "ok",
                                          "blueprints_write": "forbidden"})
    import abapit.web.app as app_mod
    monkeypatch.setattr(app_mod, "build_client",
                        lambda org: StubFleet([_device("AAA")], org=org))
    client = TestClient(create_app(), base_url="http://127.0.0.1",
                        follow_redirects=False)
    assert b"role can" in client.post("/blueprints", data={"name": "X"}).content
    assert b"role can" in client.get("/blueprints/new").content


def test_org_units_list_and_detail_and_csv(web):
    listing = web.get("/org-units")
    assert listing.status_code == 200 and b"Headquarters" in listing.content
    detail = web.get("/org-units/demo-ou-0")
    assert detail.status_code == 200 and b"Headquarters" in detail.content
    csv = web.get("/export/org-units.csv")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")


# ---- security middleware ---------------------------------------------------

def test_unknown_host_header_rejected(web):
    assert web.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_cross_origin_post_blocked(web):
    resp = web.post("/refresh", data={"next": "/"},
                    headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403


def test_other_localhost_port_origin_is_blocked(web):
    """Another app on a different loopback PORT is a different origin — and
    browsers label it `same-site` for localhost, so both checks must reject it."""
    resp = web.post("/refresh", data={"next": "/"},
                    headers={"Origin": "http://localhost:3000"})
    assert resp.status_code == 403
    resp = web.post("/refresh", data={"next": "/"},
                    headers={"Sec-Fetch-Site": "same-site"})
    assert resp.status_code == 403


def test_cross_site_fetch_post_blocked(web):
    resp = web.post("/refresh", data={"next": "/"},
                    headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403


def test_same_origin_and_plain_posts_allowed(web):
    assert web.post("/refresh", data={"next": "/"},
                    headers={"Origin": "http://127.0.0.1"}).status_code == 303
    assert web.post("/refresh", data={"next": "/"}).status_code == 303  # curl-style


# ---- assignment write flow -----------------------------------------------------

def test_assign_preview_then_execute_demo(web):
    demo = web.app.state.demo_client
    serial = demo.devices()[0]["id"]
    current = demo.device_assigned_server(serial)
    target = next(s["id"] for s in demo.mdm_servers()
                  if current is None or s["id"] != current["id"])

    preview = web.post("/assign", data={
        "serials": serial, "server": target, "action": "assign", "mode": "preview"})
    assert preview.status_code == 200
    assert serial.encode() in preview.content
    assert b"Confirm" in preview.content
    # preview must not change anything
    assert serial not in demo.mdm_server_device_ids(target)

    execute = web.post("/assign", data={
        "serials": serial, "server": target, "action": "assign", "mode": "execute"})
    assert execute.status_code == 303
    location = execute.headers["location"]
    assert "/activities/demo-activity-" in location

    assert serial in demo.mdm_server_device_ids(target)  # the write happened
    activity_id = location.split("?")[0].rsplit("/", 1)[1]
    page = web.get(f"/activities/{activity_id}")
    assert page.status_code == 200
    assert b"COMPLETED" in page.content


def test_assign_execute_with_nothing_to_do_does_not_submit(web):
    demo = web.app.state.demo_client
    serial = demo.devices()[1]["id"]
    current = demo.device_assigned_server(serial)
    if current is None:  # ensure it has a current server for the no-op case
        target = demo.mdm_servers()[0]["id"]
        demo.create_device_activity("ASSIGN_DEVICES", target, [serial])
        current = demo.device_assigned_server(serial)
    before = demo._activity_seq
    resp = web.post("/assign", data={
        "serials": serial, "server": current["id"], "action": "assign",
        "mode": "execute"})
    assert resp.status_code == 200  # rendered preview, no redirect
    assert demo._activity_seq == before  # no activity created


# ---- role-based navigation gating ----------------------------------------------

def test_denied_sections_lock_navigation(tmp_path, monkeypatch, ec_key_pair):
    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("ABAPIT_DATA_DIR", str(tmp_path / "data"))
    key_path, _ = ec_key_pair
    slug = config.add_org(name="Locked Org", scope="business",
                          client_id="BUSINESSAPI.locked", key_id="k",
                          private_key_path=str(key_path),
                          role="Device Enrollment Manager")
    config.update_org_capabilities(
        slug, {"devices": "ok", "users": "forbidden", "apps": "forbidden"})

    import abapit.web.app as app_mod
    monkeypatch.setattr(app_mod, "build_client",
                        lambda org: StubFleet([_device("AAA")], org=org))
    client = TestClient(create_app(), base_url="http://127.0.0.1",
                        follow_redirects=False)
    resp = client.get("/devices")
    assert resp.status_code == 200
    assert b"nav-link locked" in resp.content
    assert b'href="/users"' not in resp.content   # locked: a span, not a link
    assert b'href="/apps"' not in resp.content
    assert b'href="/mdm-servers"' in resp.content  # un-probed stays clickable
    assert b"re-check" in resp.content            # one-click re-probe affordance


# ---- snapshot warm-start ------------------------------------------------------

class StubFleet:
    """Non-demo stub so the warm-start path engages."""

    is_demo = False

    def __init__(self, devices, org=None):
        self.org = org or Org(name="Warm Org", scope="business",
                              client_id="BUSINESSAPI.warm", key_id="k",
                              private_key_path="")
        self._devices = devices

    def devices(self):
        return self._devices

    def mdm_servers(self): return []
    def mdm_server_device_ids(self, server_id): return []
    def device_applecare(self, serial): return []
    def users(self): return []
    def user_groups(self): return []
    def apps(self): return []
    def packages(self): return []
    def blueprints(self): return []
    def configurations(self): return []
    def mdm_enrolled_devices(self): return []


def _device(serial):
    return {"type": "orgDevices", "id": serial,
            "attributes": {"serialNumber": serial, "deviceModel": "Test Mac"}}


def test_warm_start_serves_snapshot_then_live(tmp_path, monkeypatch, ec_key_pair):
    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("ABAPIT_DATA_DIR", str(tmp_path / "data"))
    key_path, _ = ec_key_pair
    config.add_org(name="Warm Org", scope="business",
                   client_id="BUSINESSAPI.warm", key_id="k",
                   private_key_path=str(key_path))
    # Yesterday's snapshot knows device AAA; the live API knows BBB.
    history.take_snapshot(StubFleet([_device("AAA")]), include_applecare=False)

    import abapit.web.app as app_mod
    monkeypatch.setattr(app_mod, "build_client",
                        lambda org: StubFleet([_device("BBB")], org=org))
    client = TestClient(create_app(), base_url="http://127.0.0.1",
                        follow_redirects=False)

    first = client.get("/devices")
    assert first.status_code == 200
    assert b"AAA" in first.content                     # instant, from snapshot
    assert b"Showing snapshot data" in first.content   # with honest provenance

    deadline = time.time() + 5
    while (("BUSINESSAPI.warm", "devices") not in client.app.state.cache
           and time.time() < deadline):
        time.sleep(0.02)

    second = client.get("/devices")
    assert b"BBB" in second.content                    # background refresh landed
    assert b"Showing snapshot data" not in second.content


def test_configuration_detail_shows_custom_settings_payload(web):
    config_id = web.app.state.demo_client.configurations()[0]["id"]
    resp = web.get(f"/configurations/{config_id}")
    assert resp.status_code == 200
    # the payload only exists on the detail call — null in list responses
    assert b"Custom settings payload" in resp.content
    assert b"com.apple.wifi.managed" in resp.content


def test_mdm_enrolled_detail_page(web):
    device_id = web.app.state.demo_client.mdm_enrolled_devices()[0]["id"]
    resp = web.get(f"/mdm-enrolled/{device_id}")
    assert resp.status_code == 200
    assert device_id.encode() in resp.content


def test_find_unique_serial_jumps_to_device(web):
    serial = web.app.state.demo_client.devices()[0]["id"]
    resp = web.get(f"/find?q={serial}")
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/devices/{serial}"
    # and the detail page it lands on includes coverage automatically
    detail = web.get(f"/devices/{serial}")
    assert detail.status_code == 200
    assert b"AppleCare" in detail.content


def test_find_partial_unique_serial_also_jumps(web):
    serial = web.app.state.demo_client.devices()[0]["id"]
    resp = web.get(f"/find?q={serial[:8]}")  # 8 chars of a 10-char serial: unique in demo
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/devices/")


def test_find_ambiguous_falls_back_to_list(web):
    resp = web.get("/find?q=A")
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/devices?q=")


def test_find_by_imei(web):
    device = next(d for d in web.app.state.demo_client.devices()
                  if d["attributes"].get("imei"))
    resp = web.get(f"/find?q={device['attributes']['imei']}")
    assert resp.headers["location"] == f"/devices/{device['id']}"


def test_subscription_and_lifecycle_views_render(web):
    for path in ("/subscriptions", "/device-lifecycle", "/subscriptions?days=90"):
        assert web.get(path).status_code == 200, path
    body = web.get("/subscriptions").content
    assert b"no subscription endpoint" in body   # the honest explanation is shown


def test_audit_views_inherit_audit_events_permission(tmp_path, monkeypatch, ec_key_pair):
    """They're filtered views over audit events, so a role denied audit events
    must not see them in the nav."""
    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("ABAPIT_DATA_DIR", str(tmp_path / "data"))
    key_path, _ = ec_key_pair
    slug = config.add_org(name="No Audit", scope="business", client_id="BUSINESSAPI.na",
                          key_id="k", private_key_path=str(key_path))
    config.update_org_capabilities(slug, {"devices": "ok", "audit_events": "forbidden"})
    import abapit.web.app as app_mod
    monkeypatch.setattr(app_mod, "build_client",
                        lambda org: StubFleet([_device("AAA")], org=org))
    client = TestClient(create_app(), base_url="http://127.0.0.1",
                        follow_redirects=False)
    nav = client.get("/devices").content
    assert b'href="/subscriptions"' not in nav
    assert b'href="/device-lifecycle"' not in nav
