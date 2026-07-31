"""Dashboard statistics and CSV flattening helpers."""

from __future__ import annotations

import csv
import io
from collections import Counter
from datetime import datetime, timedelta, timezone


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Mosyle can return a naive date/datetime string; treat it as UTC so
    # arithmetic against tz-aware "now" (in fmt_ago, the reports) never throws.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def device_stats(devices: list[dict]) -> dict:
    """Cheap aggregate stats computed from a single orgDevices listing."""
    now = datetime.now(timezone.utc)
    by_family: Counter = Counter()
    by_status: Counter = Counter()
    by_model: Counter = Counter()
    added_30 = added_90 = 0
    by_month: Counter = Counter()

    for device in devices:
        attrs = device.get("attributes", {})
        by_family[attrs.get("productFamily") or "Unknown"] += 1
        by_status[attrs.get("status") or "Unknown"] += 1
        by_model[attrs.get("deviceModel") or "Unknown"] += 1
        added = parse_iso(attrs.get("addedToOrgDateTime"))
        if added:
            age = now - added
            if age <= timedelta(days=30):
                added_30 += 1
            if age <= timedelta(days=90):
                added_90 += 1
            by_month[added.strftime("%Y-%m")] += 1

    # Last 12 calendar months, oldest first, zero-filled.
    months = []
    year, month = now.year, now.month
    for _ in range(12):
        months.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    months.reverse()
    added_by_month = [(m, by_month.get(m, 0)) for m in months]
    month_max = max((count for _, count in added_by_month), default=0)

    return {
        "total": len(devices),
        "by_family": by_family.most_common(),
        "by_status": by_status.most_common(),
        "top_models": by_model.most_common(10),
        "added_30": added_30,
        "added_90": added_90,
        "added_by_month": added_by_month,
        "month_max": month_max,
        "family_max": max(by_family.values(), default=0),
    }


def assignment_summary(devices: list[dict], servers: list[dict],
                       server_device_ids: dict[str, list[str]]) -> dict:
    """Which devices belong to which MDM server, and which belong to none."""
    all_serials = {d.get("id") for d in devices}
    assigned: set = set()
    per_server = []
    for server in servers:
        ids = set(server_device_ids.get(server.get("id", ""), []))
        assigned |= ids
        per_server.append({
            "server": server,
            "count": len(ids),
        })
    unassigned = sorted(s for s in all_serials - assigned if s)
    return {"per_server": per_server, "unassigned": unassigned,
            "assigned_count": len(assigned & all_serials)}


def coverage_report(applecare_items: list[dict], devices: list[dict],
                    days: int, now: datetime | None = None) -> dict:
    """Coverage expiry analysis from snapshot (or live) data.

    Returns active-coverage counts, coverages expiring within `days`
    (soonest first), and devices with no active coverage at all.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days)
    expiring = []
    covered_serials: set = set()
    for item in applecare_items:
        attrs = item.get("attributes", {})
        if attrs.get("status") != "ACTIVE":
            continue
        serial = attrs.get("serialNumber")
        covered_serials.add(serial)
        end = parse_iso(attrs.get("endDateTime"))
        if end and now <= end <= cutoff:
            expiring.append({**attrs, "days_left": (end - now).days})
    expiring.sort(key=lambda row: row["days_left"])
    uncovered = [d for d in devices if d.get("id") not in covered_serials]
    return {
        "expiring": expiring,
        "uncovered": uncovered,
        "covered_count": len(covered_serials & {d.get("id") for d in devices}),
        "days": days,
    }


def fleet_age_report(devices: list[dict], applecare_items: list[dict] | None,
                     years: int, now: datetime | None = None) -> dict:
    """Refresh planning: device age buckets and replacement candidates.

    Age comes from orderDateTime (falling back to addedToOrgDateTime).
    A device is a refresh candidate when it's at least `years` old; when
    coverage data is available, candidates without active coverage are the
    strongest signals. ABM itself offers none of this.
    """
    now = now or datetime.now(timezone.utc)
    covered: set | None = None
    if applecare_items is not None:
        covered = {item.get("attributes", {}).get("serialNumber")
                   for item in applecare_items
                   if item.get("attributes", {}).get("status") == "ACTIVE"}

    bucket_labels = ["< 1 yr", "1–2 yrs", "2–3 yrs", "3–4 yrs", "4+ yrs"]
    buckets = {label: 0 for label in bucket_labels}
    candidates, undated = [], 0
    for device in devices:
        attrs = device.get("attributes", {})
        basis = parse_iso(attrs.get("orderDateTime")) or parse_iso(
            attrs.get("addedToOrgDateTime"))
        if basis is None:
            undated += 1
            continue
        age_years = (now - basis).days / 365.25
        buckets[bucket_labels[min(int(age_years), 4)]] += 1
        if age_years >= years:
            candidates.append({
                "serial": device.get("id", ""),
                "model": attrs.get("deviceModel", ""),
                "family": attrs.get("productFamily", ""),
                "ordered": attrs.get("orderDateTime") or attrs.get("addedToOrgDateTime"),
                "age_years": round(age_years, 1),
                "covered": (device.get("id") in covered) if covered is not None else None,
            })
    candidates.sort(key=lambda row: -row["age_years"])
    return {
        "buckets": [(label, buckets[label]) for label in bucket_labels],
        "bucket_max": max(buckets.values(), default=0),
        "candidates": candidates,
        "uncovered_candidates": (
            sum(1 for c in candidates if c["covered"] is False)
            if covered is not None else None),
        "undated": undated,
        "years": years,
        "has_coverage_data": covered is not None,
    }


def _norm_serial(serial: str | None) -> str:
    """ABM serials are uppercase; Mosyle casing varies. Normalize for joins
    while callers keep the original for display."""
    return (serial or "").strip().upper()


def reconcile_enrollments(abm_devices: list[dict],
                          mosyle_devices: list[dict]) -> dict:
    """Join an Apple Business org's devices against a Mosyle org's by serial.

    Surfaces the gap neither tool shows alone: devices owned in ABM but not
    enrolled in this Mosyle tenant, devices enrolled in Mosyle but unknown to
    ABM, and devices present in both. For `both`, ownership fields come from
    ABM (source of truth for procurement) and live-posture fields from Mosyle.
    """
    abm_by: dict[str, dict] = {}
    for device in abm_devices:
        key = _norm_serial(device.get("id"))
        if key:
            abm_by[key] = device
    mosyle_by: dict[str, dict] = {}
    for device in mosyle_devices:
        key = _norm_serial(device.get("id"))
        if key:
            mosyle_by[key] = device

    def abm_row(device: dict) -> dict:
        attrs = device.get("attributes", {})
        return {"serial": device.get("id", ""), "presence": "abm_only",
                "abm_status": attrs.get("status", ""), "mosyle_status": "",
                "productFamily": attrs.get("productFamily", ""),
                "deviceModel": attrs.get("deviceModel", ""),
                "osVersion": "", "lastCheckIn": "",
                "addedToOrgDateTime": attrs.get("addedToOrgDateTime", ""),
                "currentUser": ""}

    def mosyle_row(device: dict) -> dict:
        attrs = device.get("attributes", {})
        return {"serial": device.get("id", ""), "presence": "mosyle_only",
                "abm_status": "", "mosyle_status": attrs.get("status", ""),
                "productFamily": attrs.get("productFamily", ""),
                "deviceModel": attrs.get("deviceModel", ""),
                "osVersion": attrs.get("osVersion", ""),
                "lastCheckIn": attrs.get("lastCheckIn", ""),
                "addedToOrgDateTime": "",
                "currentUser": attrs.get("currentUser", "")}

    def both_row(key: str) -> dict:
        abm = abm_by[key].get("attributes", {})
        mosyle = mosyle_by[key].get("attributes", {})
        return {"serial": abm_by[key].get("id", ""), "presence": "both",
                "abm_status": abm.get("status", ""),
                "mosyle_status": mosyle.get("status", ""),
                "productFamily": abm.get("productFamily") or mosyle.get("productFamily", ""),
                "deviceModel": abm.get("deviceModel") or mosyle.get("deviceModel", ""),
                "osVersion": mosyle.get("osVersion", ""),
                "lastCheckIn": mosyle.get("lastCheckIn", ""),
                "addedToOrgDateTime": abm.get("addedToOrgDateTime", ""),
                "currentUser": mosyle.get("currentUser", "")}

    abm_keys, mosyle_keys = set(abm_by), set(mosyle_by)
    both_keys = abm_keys & mosyle_keys
    abm_only = sorted((abm_row(abm_by[k]) for k in abm_keys - mosyle_keys),
                      key=lambda r: r["serial"])
    mosyle_only = sorted((mosyle_row(mosyle_by[k]) for k in mosyle_keys - abm_keys),
                         key=lambda r: r["serial"])
    both = sorted((both_row(k) for k in both_keys), key=lambda r: r["serial"])
    total_unique = len(abm_keys | mosyle_keys)
    summary = {
        "total_abm": len(abm_keys), "total_mosyle": len(mosyle_keys),
        "total_unique": total_unique, "both_count": len(both_keys),
        "abm_only_count": len(abm_only), "mosyle_only_count": len(mosyle_only),
        "enrollment_rate": round(len(both_keys) / total_unique * 100, 1)
                           if total_unique else 0.0,
    }
    return {"abm_only": abm_only, "mosyle_only": mosyle_only,
            "both": both, "summary": summary}


def device_timeline(abm_attrs: dict | None, mosyle_attrs: dict | None,
                    audit_events: list[dict] | None = None) -> list[dict]:
    """Digest one device's signals from both sources into a single reverse-
    chronological activity list — the per-device 'what happened' view neither
    ABM nor Mosyle gives alone. Each item: {when, kind, label, source}."""
    abm = abm_attrs or {}
    mosyle = mosyle_attrs or {}
    items: list[dict] = []

    def add(when, kind, label, source):
        if when:
            items.append({"when": when, "kind": kind, "label": label, "source": source})

    add(abm.get("orderDateTime"), "ordered", "Ordered", "ABM")
    add(abm.get("addedToOrgDateTime"), "added", "Added to Apple Business", "ABM")
    for event in (audit_events or []):
        attrs = event.get("attributes", {})
        label = attrs.get("type", "event").replace("_", " ").title()
        outcome = attrs.get("outcome", "")
        add(attrs.get("eventDateTime"), "audit",
            f"{label} · {outcome}".strip(" ·") if outcome else label, "ABM")

    add(mosyle.get("enrolledAt"), "enrolled", "Enrolled in Mosyle", "Mosyle")
    add(mosyle.get("lastMdmCheckIn"), "checkin", "MDM check-in", "Mosyle")
    add(mosyle.get("lastPush"), "push", "Last MDM push", "Mosyle")
    add(mosyle.get("lastCheckIn"), "beat", "Last seen (heartbeat)", "Mosyle")

    items.sort(key=lambda i: i["when"], reverse=True)
    return items


def mosyle_os_breakdown(devices: list[dict]) -> dict:
    """OS-version distribution across a Mosyle fleet — patch posture ABM
    can't see. Missing versions bucket as 'Unknown', sorted last."""
    counts: Counter = Counter()
    for device in devices:
        counts[device.get("attributes", {}).get("osVersion") or "Unknown"] += 1
    total = sum(counts.values())
    rows = sorted(counts.items(),
                  key=lambda vc: (vc[0] == "Unknown", -vc[1], vc[0]))
    return {
        "rows": [(version, count, round(count / total * 100, 1) if total else 0.0)
                 for version, count in rows],
        "total": total,
        "max_count": max(counts.values(), default=0),
    }


def mosyle_stale_devices(devices: list[dict], days: int = 30,
                         now: datetime | None = None) -> dict:
    """Mosyle devices that haven't checked in within `days` (or never have).

    A device assigned in ABM but silent in Mosyle is the strongest 'is this
    actually managed?' signal. lastCheckIn is already ISO (adapt_device
    converts Mosyle's epoch); a missing/unparseable one counts as never
    checked in and sorts as the stalest.
    """
    now = now or datetime.now(timezone.utc)
    rows, never = [], 0
    for device in devices:
        attrs = device.get("attributes", {})
        checkin = parse_iso(attrs.get("lastCheckIn"))
        if checkin is None:
            days_stale, include = None, True
            never += 1
        else:
            days_stale = (now - checkin).days
            include = days_stale >= days
        if include:
            rows.append({
                "serial": device.get("id", ""),
                "deviceModel": attrs.get("deviceModel", ""),
                "productFamily": attrs.get("productFamily", ""),
                "osVersion": attrs.get("osVersion", ""),
                "lastCheckIn": attrs.get("lastCheckIn", ""),
                "currentUser": attrs.get("currentUser", ""),
                "days_stale": days_stale,
            })
    # Never-checked-in first, then by days stale descending.
    rows.sort(key=lambda r: (r["days_stale"] is not None, -(r["days_stale"] or 0)))
    return {"rows": rows, "total": len(devices), "stale_count": len(rows),
            "never_count": never, "days": days}


def items_to_rows(items: list[dict]) -> tuple[list[str], list[list]]:
    """Flatten JSON:API items to (header, rows). `id` first, then the union
    of attribute keys in first-seen order."""
    columns: list[str] = []
    for item in items:
        for key in item.get("attributes", {}):
            if key not in columns:
                columns.append(key)
    header = ["id"] + columns
    rows = []
    for item in items:
        attrs = item.get("attributes", {})
        rows.append([item.get("id", "")] + [_cell(attrs.get(col)) for col in columns])
    return header, rows


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return "; ".join(str(v) for v in value) if isinstance(value, list) else str(value)
    return str(value)


def _csv_safe(value: str) -> str:
    """Neutralize spreadsheet formula injection: a cell starting with
    = + - @, tab, or CR would execute as a formula when opened in Excel."""
    if value and value[0] in "=+-@\t\r":
        return "'" + value
    return value


def items_to_csv(items: list[dict]) -> str:
    header, rows = items_to_rows(items)
    buf = io.StringIO()
    writer = csv.writer(buf)
    # Headers are attribute names from the API (Mosyle passes unmapped keys
    # through), so they get the same treatment as data cells.
    writer.writerow([_csv_safe(col) for col in header])
    writer.writerows([[_csv_safe(cell) for cell in row] for row in rows])
    return buf.getvalue()
