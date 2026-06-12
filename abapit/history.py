"""Snapshot history: point-in-time copies of org state in one SQLite file.

Design rule: live pages stay live — this database is only ever read by
explicitly-historical features (the Changes page, `abapit changes`, and the
AppleCare report cache). One database holds snapshots for all orgs, scoped
by the org's client ID.

Schema is deliberately generic: a `snapshots` row per run, and an `items`
row per resource item with its attributes as JSON. SQL views expose the
most-queried resources with real columns for sqlite3/Datasette users.
"""

from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at TEXT NOT NULL,
    org_client_id TEXT NOT NULL,
    org_name TEXT NOT NULL,
    org_scope TEXT NOT NULL,
    abapit_version TEXT NOT NULL,
    errors TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS items (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    resource TEXT NOT NULL,
    item_id TEXT NOT NULL,
    attributes TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, resource, item_id)
);

CREATE VIEW IF NOT EXISTS devices_view AS
SELECT s.id AS snapshot_id, s.taken_at, s.org_name,
       i.item_id AS serial,
       json_extract(i.attributes, '$.deviceModel') AS model,
       json_extract(i.attributes, '$.productFamily') AS family,
       json_extract(i.attributes, '$.status') AS status,
       json_extract(i.attributes, '$.addedToOrgDateTime') AS added_to_org
FROM items i JOIN snapshots s ON s.id = i.snapshot_id
WHERE i.resource = 'devices';

CREATE VIEW IF NOT EXISTS applecare_view AS
SELECT s.id AS snapshot_id, s.taken_at, s.org_name,
       json_extract(i.attributes, '$.serialNumber') AS serial,
       json_extract(i.attributes, '$.description') AS description,
       json_extract(i.attributes, '$.status') AS status,
       json_extract(i.attributes, '$.endDateTime') AS end_date
FROM items i JOIN snapshots s ON s.id = i.snapshot_id
WHERE i.resource = 'applecare';
"""

# Business-only collections beyond the device-related ones every scope gets.
BUSINESS_COLLECTORS = (
    ("users", "users"),
    ("user_groups", "user_groups"),
    ("apps", "apps"),
    ("packages", "packages"),
    ("blueprints", "blueprints"),
    ("configurations", "configurations"),
    ("mdm_enrolled", "mdm_enrolled_devices"),
)


def data_dir() -> Path:
    env = os.environ.get("ABAPIT_DATA_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "share" / "abapit"


def db_path() -> Path:
    return data_dir() / "history.sqlite"


def connect() -> sqlite3.Connection:
    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    # The snapshot DB is a full inventory of the org — owner-only access.
    os.chmod(db_path(), 0o600)
    return conn


def _collect(client, include_applecare: bool, progress, errors: dict) -> dict:
    say = progress or (lambda message: None)
    out: dict[str, list[dict]] = {}

    def grab(name: str, fetch):
        try:
            say(f"fetching {name}…")
            out[name] = fetch()
        except Exception as exc:
            errors[name] = str(exc)

    grab("devices", client.devices)
    grab("mdm_servers", client.mdm_servers)

    try:
        say("fetching assignments…")
        assignments = []
        for server in out.get("mdm_servers", []):
            name = server.get("attributes", {}).get("serverName", server.get("id"))
            for serial in client.mdm_server_device_ids(server["id"]):
                assignments.append({
                    "type": "assignments", "id": serial,
                    "attributes": {"serverId": server["id"], "serverName": name},
                })
        out["assignments"] = assignments
    except Exception as exc:
        errors["assignments"] = str(exc)

    if client.org.scope == "business":
        for name, method in BUSINESS_COLLECTORS:
            grab(name, getattr(client, method))

    if include_applecare and out.get("devices"):
        try:
            say(f"fetching AppleCare coverage ({len(out['devices'])} devices, "
                "one call each)…")
            from .client import fetch_applecare_bulk
            rows, failed = fetch_applecare_bulk(client, out["devices"])
            out["applecare"] = rows
            if failed:
                errors["applecare_partial"] = (
                    f"{len(failed)} device(s) failed: {', '.join(failed[:10])}")
        except Exception as exc:
            errors["applecare"] = str(exc)

    return out


def take_snapshot(client, include_applecare: bool = True,
                  progress=None) -> tuple[int, dict, dict]:
    """Fetch all readable resources and store them as one snapshot.

    Returns (snapshot_id, counts_per_resource, errors_per_resource).
    Individual resource failures are recorded, not fatal — a partial
    snapshot of a real org beats no snapshot.
    """
    errors: dict[str, str] = {}
    resources = _collect(client, include_applecare, progress, errors)
    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO snapshots (taken_at, org_client_id, org_name, "
                "org_scope, abapit_version, errors) VALUES (?, ?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 client.org.client_id, client.org.name, client.org.scope,
                 __version__, json.dumps(errors)))
            snapshot_id = cur.lastrowid
            for resource, items in resources.items():
                conn.executemany(
                    "INSERT OR REPLACE INTO items VALUES (?, ?, ?, ?)",
                    [(snapshot_id, resource, item.get("id", ""),
                      json.dumps(item.get("attributes", {}), sort_keys=True))
                     for item in items])
    finally:
        conn.close()
    counts = {resource: len(items) for resource, items in resources.items()}
    return snapshot_id, counts, errors


def list_snapshots(org_client_id: str, limit: int = 100) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM items i WHERE i.snapshot_id = s.id "
            "AND i.resource = 'devices') AS device_count "
            "FROM snapshots s WHERE s.org_client_id = ? "
            "ORDER BY s.id DESC LIMIT ?", (org_client_id, limit)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _load_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> dict:
    data: dict[str, dict[str, dict]] = {}
    for row in conn.execute(
            "SELECT resource, item_id, attributes FROM items WHERE snapshot_id = ?",
            (snapshot_id,)):
        data.setdefault(row["resource"], {})[row["item_id"]] = json.loads(row["attributes"])
    return data


def diff_snapshots(old_id: int, new_id: int) -> dict:
    """Field-level diff between two snapshots.

    Returns {resource: {added: [...], removed: [...], changed: [...]}} with
    only the resources that actually differ.
    """
    conn = connect()
    try:
        old, new = _load_snapshot(conn, old_id), _load_snapshot(conn, new_id)
    finally:
        conn.close()

    result: dict[str, dict] = {}
    for resource in sorted(set(old) | set(new)):
        o, n = old.get(resource, {}), new.get(resource, {})
        added = [{"id": key, "attributes": n[key]} for key in sorted(n.keys() - o.keys())]
        removed = [{"id": key, "attributes": o[key]} for key in sorted(o.keys() - n.keys())]
        changed = []
        for key in sorted(o.keys() & n.keys()):
            if o[key] == n[key]:
                continue
            fields = [{"field": field, "old": o[key].get(field), "new": n[key].get(field)}
                      for field in sorted(set(o[key]) | set(n[key]))
                      if o[key].get(field) != n[key].get(field)]
            changed.append({"id": key, "attributes": n[key], "fields": fields})
        if added or removed or changed:
            result[resource] = {"added": added, "removed": removed, "changed": changed}
    return result


def snapshot_resource(snapshot_id: int, resource: str) -> list[dict]:
    """Items of one resource from one snapshot, in JSON:API shape."""
    conn = connect()
    try:
        return [{"type": resource, "id": r["item_id"],
                 "attributes": json.loads(r["attributes"])}
                for r in conn.execute(
                    "SELECT item_id, attributes FROM items "
                    "WHERE snapshot_id = ? AND resource = ? ORDER BY item_id",
                    (snapshot_id, resource))]
    finally:
        conn.close()


def latest_resource(org_client_id: str, resource: str) -> tuple[int, str, list[dict]] | None:
    """The most recent stored copy of a resource for an org.

    Returns (snapshot_id, taken_at, items) or None if no snapshot has it.
    """
    conn = connect()
    try:
        row = conn.execute(
            "SELECT DISTINCT s.id, s.taken_at FROM snapshots s "
            "JOIN items i ON i.snapshot_id = s.id AND i.resource = ? "
            "WHERE s.org_client_id = ? ORDER BY s.id DESC LIMIT 1",
            (resource, org_client_id)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return row["id"], row["taken_at"], snapshot_resource(row["id"], resource)


def latest_applecare(org_client_id: str) -> tuple[str, list[dict]] | None:
    """Coverage rows from the most recent snapshot that includes AppleCare.

    Returns (taken_at, items in JSON:API shape) or None.
    """
    found = latest_resource(org_client_id, "applecare")
    if found is None:
        return None
    _, taken_at, items = found
    return taken_at, items


def prune(org_client_id: str, keep: int) -> int:
    """Keep only the newest `keep` snapshots for an org. Returns rows removed."""
    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                "DELETE FROM snapshots WHERE org_client_id = ? AND id NOT IN ("
                "SELECT id FROM snapshots WHERE org_client_id = ? "
                "ORDER BY id DESC LIMIT ?)",
                (org_client_id, org_client_id, keep))
            return cur.rowcount
    finally:
        conn.close()


def item_label(attributes: dict) -> str:
    """Best human-readable name for a snapshot item."""
    for key in ("deviceModel", "name", "serverName", "managedAppleAccount", "description"):
        value = attributes.get(key)
        if value:
            return str(value)
    return ""
