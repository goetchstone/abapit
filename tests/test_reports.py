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
