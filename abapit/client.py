"""HTTP client for the Apple Business API and Apple School API.

Both APIs share the same JSON:API-style shape: collections live under
/v1/<resource>, responses carry items in `data`, related resources in
`included`, and a `links.next` URL for cursor pagination. The Business API
exposes more resource types (users, apps, blueprints, ...) than the School
API, which currently covers devices and device management services.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

from .auth import token_cache
from .config import Org

BASE_URLS = {
    "business": "https://api-business.apple.com",
    "school": "https://api-school.apple.com",
}

# Resource sections available per scope, used to gate navigation and routes.
BUSINESS_SECTIONS = (
    "devices",
    "mdm_servers",
    "mdm_enrolled",
    "users",
    "user_groups",
    "apps",
    "packages",
    "blueprints",
    "configurations",
    "audit_events",
    "changes",
    "coverage",
    "assign",
)
SCHOOL_SECTIONS = ("devices", "mdm_servers", "changes", "coverage", "assign")


def sections_for(scope: str) -> tuple[str, ...]:
    return BUSINESS_SECTIONS if scope == "business" else SCHOOL_SECTIONS


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


class ApiClient:
    """Synchronous client bound to one org profile."""

    is_demo = False

    def __init__(
        self,
        org: Org,
        page_limit: int = 1000,
        max_pages: int = 200,
        transport: httpx.BaseTransport | None = None,
    ):
        self.org = org
        self.base_url = BASE_URLS[org.scope]
        self.page_limit = page_limit
        self.max_pages = max_pages
        # HTTP/2: Apple's HTTP/1.1 endpoints intermittently emit malformed
        # Transfer-Encoding headers under concurrent keep-alive reuse; h2
        # framing avoids that entirely and multiplexes parallel calls.
        self._http = httpx.Client(timeout=60, transport=transport,
                                  http2=transport is None)

    # -- plumbing ---------------------------------------------------------

    def _request(self, url: str, params: dict | None = None,
                 method: str = "GET", json_body: dict | None = None) -> dict:
        token = token_cache.get(self.org)
        refreshed = False
        for attempt in range(5):
            try:
                resp = self._http.request(
                    method, url, params=params, json=json_body,
                    headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError as exc:
                # Transient transport/protocol errors (Apple occasionally
                # emits malformed responses under load). Retry GETs only —
                # a retried POST could double-submit an activity.
                if method == "GET" and attempt < 4:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise ApiError(0, f"network error: {exc}") from exc
            if resp.status_code == 401 and not refreshed:
                # Token may have just expired; refresh once and retry.
                token_cache.invalidate(self.org)
                token = token_cache.get(self.org)
                refreshed = True
                continue
            if resp.status_code == 429 and attempt < 4:
                # Rate limited — honor Retry-After, else back off exponentially.
                # Safe for POSTs too: a 429 means Apple rejected the request
                # before processing it.
                try:
                    delay = float(resp.headers.get("Retry-After", ""))
                except ValueError:
                    delay = 2.0 ** attempt
                time.sleep(min(delay, 60))
                continue
            break
        if resp.status_code >= 400:
            raise ApiError(resp.status_code, _error_message(resp))
        return resp.json()

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request(f"{self.base_url}/v1/{path}", params)

    def list_all(self, path: str, params: dict | None = None) -> list[dict]:
        """Fetch every page of a collection by following links.next."""
        params = dict(params or {})
        params.setdefault("limit", self.page_limit)
        body = self.get(path, params)
        items = list(body.get("data", []))
        pages = 1
        while body.get("links", {}).get("next") and pages < self.max_pages:
            body = self._request(body["links"]["next"])
            items.extend(body.get("data", []))
            pages += 1
        if body.get("links", {}).get("next"):
            print(f"warning: {path} truncated after {pages} pages "
                  f"({len(items)} items) — raise max_pages", file=sys.stderr)
        return items

    # -- devices ----------------------------------------------------------

    def devices(self) -> list[dict]:
        return self.list_all("orgDevices")

    def device(self, device_id: str) -> dict:
        return self.get(f"orgDevices/{device_id}").get("data", {})

    def device_applecare(self, device_id: str) -> list[dict]:
        return self.list_all(f"orgDevices/{device_id}/appleCareCoverage")

    def device_assigned_server(self, device_id: str) -> dict | None:
        try:
            return self.get(f"orgDevices/{device_id}/assignedServer").get("data")
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise

    # -- device management services ----------------------------------------

    def mdm_servers(self) -> list[dict]:
        return self.list_all("mdmServers")

    def mdm_server(self, server_id: str) -> dict:
        return self.get(f"mdmServers/{server_id}").get("data", {})

    def mdm_server_device_ids(self, server_id: str) -> list[str]:
        linkages = self.list_all(f"mdmServers/{server_id}/relationships/devices")
        return [item.get("id", "") for item in linkages]

    def mdm_enrolled_devices(self) -> list[dict]:
        return self.list_all("mdmDevices")

    def create_device_activity(self, activity_type: str, server_id: str,
                               serials: list[str]) -> dict:
        """Submit a batch assign/unassign. activity_type is ASSIGN_DEVICES
        or UNASSIGN_DEVICES. Returns the created activity (id + status)."""
        body = {"data": {
            "type": "orgDeviceActivities",
            "attributes": {"activityType": activity_type},
            "relationships": {
                "mdmServer": {"data": {"type": "mdmServers", "id": server_id}},
                "devices": {"data": [{"type": "orgDevices", "id": serial}
                                     for serial in serials]},
            },
        }}
        return self._request(f"{self.base_url}/v1/orgDeviceActivities",
                             method="POST", json_body=body).get("data", {})

    def device_activity(self, activity_id: str) -> dict:
        return self.get(f"orgDeviceActivities/{activity_id}").get("data", {})

    # -- capability probe ------------------------------------------------------

    READ_PROBES = (
        # (section key, label, path, params, business_only)
        ("devices", "Devices", "orgDevices", {"limit": 1}, False),
        ("mdm_servers", "MDM servers", "mdmServers", {"limit": 1}, False),
        ("mdm_enrolled", "Apple MDM enrolled", "mdmDevices", {"limit": 1}, True),
        ("users", "Users", "users", {"limit": 1}, True),
        ("user_groups", "User groups", "userGroups", {"limit": 1}, True),
        ("apps", "Apps", "apps", {"limit": 1}, True),
        ("packages", "Packages", "packages", {"limit": 1}, True),
        ("blueprints", "Blueprints", "blueprints", {"limit": 1}, True),
        ("configurations", "Configurations", "configurations", {"limit": 1}, True),
        ("audit_events", "Audit events", "auditEvents", None, True),  # params at runtime
    )

    def probe_capabilities(self) -> list[dict]:
        """Empirically map what this API account's role allows.

        Apple exposes no permissions endpoint — the only signal is which
        calls return 403. Reads use limit=1 (one cheap item each). The
        write probe submits an assignment activity with an EMPTY device
        list, which can never change anything: 403 means no write
        permission; a validation error (400/409/422) or 201 means the
        role allows writes.
        """
        now = datetime.now(timezone.utc)
        results = []
        for section, label, path, params, business_only in self.READ_PROBES:
            if business_only and self.org.scope != "business":
                continue
            if path == "auditEvents":
                params = {
                    "filter[startTimestamp]": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "filter[endTimestamp]": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "limit": 1,
                }
            try:
                self.get(path, params)
                status = "ok"
            except ApiError as exc:
                status = "forbidden" if exc.status == 403 else f"error {exc.status}"
            results.append({"section": section, "capability": label,
                            "kind": "read", "status": status})

        try:
            self.create_device_activity("ASSIGN_DEVICES",
                                        "00000000-0000-0000-0000-000000000000", [])
            status = "ok"
        except ApiError as exc:
            if exc.status == 403:
                status = "forbidden"
            elif exc.status in (400, 404, 409, 422):
                status = "ok"  # request was validated, so the role allows writes
            else:
                status = f"error {exc.status}"
        results.append({"section": "assign", "capability": "Device assignment",
                        "kind": "write", "status": status})
        return results

    # -- people (Business API only) -----------------------------------------

    def users(self) -> list[dict]:
        return self.list_all("users")

    def user(self, user_id: str) -> dict:
        return self.get(f"users/{user_id}").get("data", {})

    def user_groups(self) -> list[dict]:
        return self.list_all("userGroups")

    def user_group(self, group_id: str) -> dict:
        return self.get(f"userGroups/{group_id}").get("data", {})

    def user_group_member_ids(self, group_id: str) -> list[str]:
        linkages = self.list_all(f"userGroups/{group_id}/relationships/users")
        return [item.get("id", "") for item in linkages]

    # -- content (Business API only) ------------------------------------------

    def apps(self) -> list[dict]:
        return self.list_all("apps")

    def packages(self) -> list[dict]:
        return self.list_all("packages")

    def blueprints(self) -> list[dict]:
        return self.list_all("blueprints")

    def blueprint(self, blueprint_id: str, include: str = "") -> dict:
        params = {"include": include} if include else None
        return self.get(f"blueprints/{blueprint_id}", params)

    def configurations(self) -> list[dict]:
        return self.list_all("configurations")

    # -- audit (Business API only) ---------------------------------------------

    def audit_events(self, start_iso: str, end_iso: str, event_type: str = "") -> list[dict]:
        params: dict = {
            "filter[startTimestamp]": start_iso,
            "filter[endTimestamp]": end_iso,
        }
        if event_type:
            params["filter[type]"] = event_type
        return self.list_all("auditEvents", params)

    def close(self) -> None:
        self._http.close()


def fetch_applecare_bulk(client, devices: list[dict],
                         max_workers: int = 5) -> tuple[list[dict], list[str]]:
    """Coverage for many devices, one call each — parallel sweep, then a
    sequential retry pass over any failures (Apple intermittently errors
    under burst concurrency but answers the same calls fine one-by-one).
    Returns (rows with serialNumber injected, serials that failed twice)."""
    from concurrent.futures import ThreadPoolExecutor

    def one(serial: str):
        rows = []
        for cov in client.device_applecare(serial):
            attrs = dict(cov.get("attributes", {}))
            attrs["serialNumber"] = serial
            cov_id = cov.get("id") or f"{serial}:{attrs.get('description', '')}"
            rows.append({"type": "applecare", "id": cov_id, "attributes": attrs})
        return rows

    items: list[dict] = []
    failed: list[str] = []
    serials = [d.get("id", "") for d in devices]
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        def attempt(serial):
            try:
                return one(serial), None
            except Exception:
                return [], serial
        for rows, failure in pool.map(attempt, serials):
            items.extend(rows)
            if failure:
                failed.append(failure)

    still_failed: list[str] = []
    for serial in failed:
        time.sleep(0.3)  # gentle, sequential second chance
        try:
            items.extend(one(serial))
        except Exception:
            still_failed.append(serial)
    return items, still_failed


def _error_message(resp: httpx.Response) -> str:
    try:
        errors = resp.json().get("errors", [])
        if errors:
            first = errors[0]
            return f"{first.get('title', 'API error')}: {first.get('detail', '')}".strip(": ")
    except Exception:
        pass
    return f"HTTP {resp.status_code}: {resp.text[:300]}"
