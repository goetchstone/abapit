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
