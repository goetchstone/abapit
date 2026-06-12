import json
import sqlite3

import pytest

from abapit import history
from abapit.config import Org


class StubClient:
    """Minimal client whose fleet we can mutate between snapshots."""

    is_demo = True

    def __init__(self, devices, assignments=None, applecare=None):
        self.org = Org(name="Stub Org", scope="business",
                       client_id="BUSINESSAPI.stub", key_id="k",
                       private_key_path="")
        self._devices = devices
        self._assignments = assignments or {}   # serial -> (serverId, serverName)
        self._applecare = applecare or {}       # serial -> [coverage items]

    def devices(self):
        return self._devices

    def mdm_servers(self):
        servers = {sid: name for sid, name in self._assignments.values()}
        return [{"type": "mdmServers", "id": sid,
                 "attributes": {"serverName": name}}
                for sid, name in servers.items()]

    def mdm_server_device_ids(self, server_id):
        return [serial for serial, (sid, _) in self._assignments.items()
                if sid == server_id]

    def device_applecare(self, serial):
        return self._applecare.get(serial, [])

    def users(self): return []
    def user_groups(self): return []
    def apps(self): return []
    def packages(self): return []
    def blueprints(self): return []
    def configurations(self): return []
    def mdm_enrolled_devices(self): return []


def device(serial, **attrs):
    return {"type": "orgDevices", "id": serial,
            "attributes": {"serialNumber": serial, **attrs}}


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ABAPIT_DATA_DIR", str(tmp_path))
    return tmp_path


def test_snapshot_diff_detects_adds_removes_changes_and_moves():
    before = StubClient(
        devices=[device("AAA", status="ASSIGNED", deviceModel="MacBook Air"),
                 device("BBB", status="ASSIGNED", deviceModel="iPad Air")],
        assignments={"AAA": ("srv-1", "Intune"), "BBB": ("srv-1", "Intune")})
    after = StubClient(
        devices=[device("AAA", status="UNASSIGNED", deviceModel="MacBook Air"),
                 device("CCC", status="ASSIGNED", deviceModel="Mac mini")],
        assignments={"AAA": ("srv-2", "Jamf Pro"), "CCC": ("srv-1", "Intune")})

    old_id, _, errors_old = history.take_snapshot(before, include_applecare=False)
    new_id, counts, errors_new = history.take_snapshot(after, include_applecare=False)
    assert errors_old == {} and errors_new == {}
    assert counts["devices"] == 2

    delta = history.diff_snapshots(old_id, new_id)

    devices_delta = delta["devices"]
    assert [i["id"] for i in devices_delta["added"]] == ["CCC"]
    assert [i["id"] for i in devices_delta["removed"]] == ["BBB"]
    changed = devices_delta["changed"][0]
    assert changed["id"] == "AAA"
    assert {"field": "status", "old": "ASSIGNED", "new": "UNASSIGNED"} in changed["fields"]

    moves = delta["assignments"]["changed"][0]
    assert moves["id"] == "AAA"
    fields = {f["field"]: (f["old"], f["new"]) for f in moves["fields"]}
    assert fields["serverName"] == ("Intune", "Jamf Pro")


def test_identical_snapshots_diff_empty():
    client = StubClient(devices=[device("AAA", status="ASSIGNED")])
    a, _, _ = history.take_snapshot(client, include_applecare=False)
    b, _, _ = history.take_snapshot(client, include_applecare=False)
    assert history.diff_snapshots(a, b) == {}


def test_latest_applecare_serves_coverage_cache():
    coverage = [{"type": "appleCareCoverage", "id": "AC-AAA",
                 "attributes": {"description": "AppleCare+", "status": "ACTIVE"}}]
    client = StubClient(devices=[device("AAA")], applecare={"AAA": coverage})

    assert history.latest_applecare("BUSINESSAPI.stub") is None
    history.take_snapshot(client, include_applecare=True)
    result = history.latest_applecare("BUSINESSAPI.stub")
    assert result is not None
    taken_at, items = result
    assert items[0]["attributes"]["serialNumber"] == "AAA"
    assert items[0]["attributes"]["description"] == "AppleCare+"


def test_prune_keeps_newest_and_cascades_items():
    client = StubClient(devices=[device("AAA")])
    ids = [history.take_snapshot(client, include_applecare=False)[0]
           for _ in range(4)]
    removed = history.prune("BUSINESSAPI.stub", keep=2)
    assert removed == 2
    remaining = [s["id"] for s in history.list_snapshots("BUSINESSAPI.stub")]
    assert remaining == [ids[3], ids[2]]
    conn = sqlite3.connect(history.db_path())
    orphans = conn.execute(
        "SELECT COUNT(*) FROM items WHERE snapshot_id NOT IN "
        "(SELECT id FROM snapshots)").fetchone()[0]
    conn.close()
    assert orphans == 0


def test_partial_snapshot_records_errors():
    client = StubClient(devices=[device("AAA")])
    client.users = lambda: (_ for _ in ()).throw(RuntimeError("403 nope"))
    snapshot_id, counts, errors = history.take_snapshot(client, include_applecare=False)
    assert "users" in errors
    assert counts["devices"] == 1
    snaps = history.list_snapshots("BUSINESSAPI.stub")
    assert json.loads(snaps[0]["errors"])["users"]


def test_snapshots_are_org_scoped():
    history.take_snapshot(StubClient(devices=[device("AAA")]),
                          include_applecare=False)
    assert history.list_snapshots("BUSINESSAPI.other") == []
