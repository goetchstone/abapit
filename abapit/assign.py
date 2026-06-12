"""Assignment planning: turn a raw serial list into an explicit, reviewable
plan before anything is sent to Apple. Pure logic, no I/O — shared by the
web preview and the CLI dry-run."""

from __future__ import annotations

import re

VALID_ACTIONS = ("assign", "unassign")


def parse_serials(raw: str) -> list[str]:
    """Split pasted text on whitespace/commas/semicolons; dedupe, keep order."""
    seen, result = set(), []
    for token in re.split(r"[\s,;]+", raw.strip()):
        token = token.strip().upper()
        if token and token not in seen:
            seen.add(token)
            result.append(token)
    return result


def plan(raw_serials: str, action: str, server_id: str,
         devices: list[dict], servers: list[dict],
         server_device_ids: dict[str, list[str]]) -> dict:
    """Classify every requested serial: a real move, a no-op, or unknown.

    Only `moves` are ever submitted to Apple.
    """
    if action not in VALID_ACTIONS:
        raise ValueError(f"action must be one of {VALID_ACTIONS}")
    device_index = {d.get("id", "").upper(): d for d in devices}
    server_names = {s["id"]: s.get("attributes", {}).get("serverName", s["id"])
                    for s in servers}
    if server_id not in server_names:
        raise ValueError("unknown device management service")
    current: dict[str, str] = {}
    for sid, assigned in server_device_ids.items():
        for serial in assigned:
            current[serial.upper()] = sid

    moves, noops, unknown = [], [], []
    for serial in parse_serials(raw_serials):
        device = device_index.get(serial)
        if device is None:
            unknown.append(serial)
            continue
        current_id = current.get(serial)
        row = {
            "serial": device["id"],
            "model": device.get("attributes", {}).get("deviceModel", ""),
            "from_id": current_id,
            "from_name": server_names.get(current_id, "(unassigned)") if current_id else "(unassigned)",
        }
        if action == "assign":
            if current_id == server_id:
                noops.append({**row, "reason": "already assigned to this service"})
            else:
                moves.append(row)
        else:  # unassign
            if current_id != server_id:
                noops.append({**row, "reason": "not assigned to this service"})
            else:
                moves.append(row)
    return {
        "action": action,
        "server_id": server_id,
        "server_name": server_names[server_id],
        "moves": moves,
        "noops": noops,
        "unknown": unknown,
    }
