import pytest
from fastapi.testclient import TestClient

from abapit.web.app import create_app


@pytest.fixture(scope="module")
def web():
    app = create_app(demo=True)
    return TestClient(app, follow_redirects=False)


def test_pages_render(web):
    for path in ("/", "/devices", "/mdm-servers", "/users", "/blueprints",
                 "/settings", "/audit-events"):
        assert web.get(path).status_code == 200, path


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
