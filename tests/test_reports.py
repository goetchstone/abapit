from datetime import datetime, timedelta, timezone

from abapit.reports import coverage_report

NOW = datetime(2026, 6, 12, tzinfo=timezone.utc)


def cov(serial, status, end_days=None):
    end = (NOW + timedelta(days=end_days)).strftime("%Y-%m-%dT%H:%M:%SZ") \
        if end_days is not None else None
    return {"type": "applecare", "id": f"c-{serial}-{status}-{end_days}",
            "attributes": {"serialNumber": serial, "status": status,
                           "description": "AppleCare+", "endDateTime": end}}


def dev(serial):
    return {"type": "orgDevices", "id": serial,
            "attributes": {"serialNumber": serial}}


def test_csv_export_neutralizes_formula_injection():
    from abapit.reports import items_to_csv
    items = [{"type": "orgDevices", "id": "AAA",
              "attributes": {"deviceModel": "=HYPERLINK(\"http://evil\")",
                             "color": "@SUM(1)", "status": "OK"}}]
    body = items_to_csv(items)
    assert "'=HYPERLINK" in body
    assert "'@SUM" in body
    assert ",OK" in body  # normal values untouched


def test_fleet_age_report_buckets_and_candidates():
    from abapit.reports import fleet_age_report

    def aged(serial, years_old):
        ordered = (NOW - timedelta(days=int(years_old * 365.25))).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        return {"type": "orgDevices", "id": serial,
                "attributes": {"serialNumber": serial, "orderDateTime": ordered,
                               "deviceModel": "Mac", "productFamily": "Mac"}}

    devices = [aged("NEW", 0.5), aged("MID", 2.5), aged("OLD", 4.5),
               aged("ANCIENT", 6.0),
               {"type": "orgDevices", "id": "NODATE", "attributes": {}}]
    coverage = [cov("OLD", "ACTIVE", end_days=100)]

    report = fleet_age_report(devices, coverage, years=4, now=NOW)

    assert dict(report["buckets"])["< 1 yr"] == 1
    assert dict(report["buckets"])["2–3 yrs"] == 1
    assert dict(report["buckets"])["4+ yrs"] == 2
    assert [c["serial"] for c in report["candidates"]] == ["ANCIENT", "OLD"]
    assert report["candidates"][0]["covered"] is False
    assert report["candidates"][1]["covered"] is True
    assert report["uncovered_candidates"] == 1
    assert report["undated"] == 1

    # without coverage data, the column is honestly unknown
    no_cov = fleet_age_report(devices, None, years=4, now=NOW)
    assert no_cov["has_coverage_data"] is False
    assert no_cov["candidates"][0]["covered"] is None
    assert no_cov["uncovered_candidates"] is None


def test_coverage_report_buckets():
    items = [
        cov("D1", "ACTIVE", end_days=10),     # expiring soon
        cov("D2", "ACTIVE", end_days=400),    # active, outside window
        cov("D4", "EXPIRED", end_days=-30),   # lapsed -> D4 uncovered
        cov("D5", "ACTIVE", end_days=5),      # device no longer in org
        cov("D6", "ACTIVE", end_days=None),   # active with no end date
    ]
    devices = [dev("D1"), dev("D2"), dev("D3"), dev("D4"), dev("D6")]

    report = coverage_report(items, devices, days=90, now=NOW)

    assert [r["serialNumber"] for r in report["expiring"]] == ["D5", "D1"]
    assert report["expiring"][0]["days_left"] == 5
    assert {d["id"] for d in report["uncovered"]} == {"D3", "D4"}
    assert report["covered_count"] == 3  # D1, D2, D6 (no-end counts as covered)


# ---- Mosyle reconciliation + posture ----------------------------------------

def _abm(serial, status="ASSIGNED", family="Mac", model="MacBook Air"):
    return {"type": "orgDevices", "id": serial, "attributes": {
        "serialNumber": serial, "status": status, "productFamily": family,
        "deviceModel": model, "addedToOrgDateTime": "2025-01-01T00:00:00Z"}}


def _mos(serial, os_version="15.5", checkin_days=1, user="sarah@acme.com",
         family="Mac", model="MacBook Air"):
    checkin = ((NOW - timedelta(days=checkin_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
               if checkin_days is not None else None)
    return {"type": "orgDevices", "id": serial, "attributes": {
        "serialNumber": serial, "status": "active", "productFamily": family,
        "deviceModel": model, "osVersion": os_version, "lastCheckIn": checkin,
        "currentUser": user, "managedBy": "Mosyle"}}


def test_reconcile_enrollments_buckets_and_normalization():
    from abapit.reports import reconcile_enrollments
    abm = [_abm("A1"), _abm("A2"), _abm("B")]
    # ' a1 ' proves .strip().upper() join; C is Mosyle-only.
    mosyle = [_mos(" a1 "), _mos("C")]
    report = reconcile_enrollments(abm, mosyle)

    assert [r["serial"] for r in report["both"]] == ["A1"]
    assert {r["serial"] for r in report["abm_only"]} == {"A2", "B"}
    assert [r["serial"] for r in report["mosyle_only"]] == ["C"]
    assert report["summary"]["total_unique"] == 4
    assert report["summary"]["both_count"] == 1
    assert report["summary"]["enrollment_rate"] == 25.0  # 1 of 4 known serials


def test_reconcile_both_row_merges_abm_ownership_and_mosyle_posture():
    from abapit.reports import reconcile_enrollments
    abm = [_abm("A1", status="ASSIGNED", family="Mac")]
    mosyle = [_mos("A1", os_version="15.5", user="sarah@acme.com")]
    row = reconcile_enrollments(abm, mosyle)["both"][0]
    assert row["abm_status"] == "ASSIGNED"        # ABM-preferred ownership
    assert row["productFamily"] == "Mac"
    assert row["addedToOrgDateTime"] == "2025-01-01T00:00:00Z"
    assert row["osVersion"] == "15.5"             # Mosyle-preferred posture
    assert row["currentUser"] == "sarah@acme.com"
    assert row["mosyle_status"] == "active"


def test_reconcile_skips_blank_serials_and_dedups():
    from abapit.reports import reconcile_enrollments
    abm = [_abm("A1"), {"type": "orgDevices", "id": "", "attributes": {}},
           _abm("a1")]  # duplicate normalized serial collapses
    report = reconcile_enrollments(abm, [_mos("A1")])
    assert len(report["both"]) == 1
    assert report["summary"]["total_abm"] == 1  # blank skipped, dup collapsed


def test_reconcile_csv_flatten_is_injection_safe():
    from abapit.reports import items_to_csv, reconcile_enrollments
    abm = [_abm("A1", model="=HYPERLINK(\"http://evil\")")]
    report = reconcile_enrollments(abm, [])
    rows = [{"type": "reconciliation", "id": r["serial"], "attributes": r}
            for r in report["abm_only"]]
    assert "'=HYPERLINK" in items_to_csv(rows)


def test_mosyle_os_breakdown_sorted_unknown_last():
    from abapit.reports import mosyle_os_breakdown
    devices = [_mos("A", os_version="17.6"), _mos("B", os_version="17.6"),
               _mos("C", os_version="18.0"),
               {"type": "orgDevices", "id": "D", "attributes": {}}]  # no osVersion
    report = mosyle_os_breakdown(devices)
    versions = [v for v, _, _ in report["rows"]]
    assert versions[0] == "17.6"          # most common first
    assert versions[-1] == "Unknown"      # missing bucket sorts last
    assert report["max_count"] == 2
    assert report["total"] == 4
    assert round(sum(p for _, _, p in report["rows"])) == 100


def test_mosyle_stale_devices_threshold_and_never():
    from abapit.reports import mosyle_stale_devices
    devices = [_mos("FRESH", checkin_days=2), _mos("OLD", checkin_days=40),
               _mos("ANCIENT", checkin_days=100), _mos("NEVER", checkin_days=None)]
    report = mosyle_stale_devices(devices, days=30, now=NOW)
    serials = [r["serial"] for r in report["rows"]]
    assert "FRESH" not in serials                 # within threshold, excluded
    assert serials[0] == "NEVER"                  # never-checked-in sorts stalest
    assert serials[1:] == ["ANCIENT", "OLD"]      # then by days descending
    assert report["never_count"] == 1
    assert report["stale_count"] == 3
    assert report["rows"][0]["days_stale"] is None
