"""Blueprint relationship planning: turn a desired add/remove selection into an
explicit, reviewable plan before anything is sent to Apple. Pure logic, no I/O —
mirrors assign.plan() and powers the web preview -> confirm flow."""

from __future__ import annotations

VALID_OPS = ("add", "remove")


def plan_relationship(op: str, selected_ids: list[str], current_ids: list[str],
                      catalog: dict[str, str]) -> dict:
    """Classify each selected id for an add/remove against a blueprint relationship.

    `current_ids` are the members already attached; `catalog` maps id -> display
    label for every valid item of this relationship (used to reject unknowns and
    label the preview). Returns add/remove/noops/unknown lists plus `changes`
    (the subset that will actually be sent).
    """
    if op not in VALID_OPS:
        raise ValueError(f"op must be one of {VALID_OPS}")
    current = set(current_ids)
    add, remove, noops, unknown = [], [], [], []
    seen: set[str] = set()
    for item_id in selected_ids:
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        label = catalog.get(item_id)
        if label is None:
            unknown.append(item_id)
            continue
        row = {"id": item_id, "label": label}
        if op == "add":
            if item_id in current:
                noops.append({**row, "reason": "already attached"})
            else:
                add.append(row)
        else:  # remove
            if item_id in current:
                remove.append(row)
            else:
                noops.append({**row, "reason": "not attached"})
    return {"op": op, "add": add, "remove": remove, "noops": noops,
            "unknown": unknown, "changes": add if op == "add" else remove}
