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
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


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
    = + - @ or a tab would execute as a formula when opened in Excel."""
    if value and value[0] in "=+-@\t":
        return "'" + value
    return value


def items_to_csv(items: list[dict]) -> str:
    header, rows = items_to_rows(items)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows([[_csv_safe(cell) for cell in row] for row in rows])
    return buf.getvalue()
