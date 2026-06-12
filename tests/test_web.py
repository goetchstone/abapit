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
                 "/settings", "/audit-events", "/reports/coverage"):
        assert web.get(path).status_code == 200, path


# ---- security middleware ---------------------------------------------------

def test_unknown_host_header_rejected(web):
    assert web.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_cross_origin_post_blocked(web):
    resp = web.post("/refresh", data={"next": "/"},
                    headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403


def test_cross_site_fetch_post_blocked(web):
    resp = web.post("/refresh", data={"next": "/"},
                    headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403


def test_same_origin_and_plain_posts_allowed(web):
    assert web.post("/refresh", data={"next": "/"},
                    headers={"Origin": "http://127.0.0.1"}).status_code == 303
    assert web.post("/refresh", data={"next": "/"}).status_code == 303  # curl-style


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
    monkeypatch.setattr(app_mod, "ApiClient",
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
