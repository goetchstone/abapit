"""HTTP client for the Mosyle Business API — read-only, for now.

Where the Apple Business/School APIs are JSON:API (GET /v1/<resource>,
items under `data`, cursor pagination), Mosyle's is operation-style: you
POST to one endpoint per object (/v1/devices, /v1/users, ...) with a body
``{"operation": "list", "options": {...}}`` and an ``accesstoken`` header
carrying the API token from Mosyle's API Integration profile.

This client adapts Mosyle's responses into the same
``{"type", "id", "attributes"}`` shape the rest of abapit already speaks —
templates, reports, CSV export, history — so the existing device pages
render against Mosyle data unchanged. It deliberately mirrors the subset
of ``client.ApiClient``'s public interface that the device-listing pages
call; sections Mosyle doesn't cover are gated off in the navigation
(see ``MOSYLE_SECTIONS`` in client.py), so they're never invoked.

NOTE: Mosyle's official API reference is gated behind a customer login.
The base URL, the auth header, the ``list`` operation, and the response
envelope below reflect the best public documentation and community tooling
as of 2026-06. Confirm them against a live tenant with Settings → Test and
adjust the constants here if a real response differs.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import httpx

from .client import ApiError
from .config import Org

log = logging.getLogger("abapit")

MOSYLE_BUSINESS_BASE = "https://businessapi.mosyle.com/v1"
# Header carrying the API access token. Mosyle also historically accepted an
# admin Basic-auth pair; the token alone is the current documented path.
TOKEN_HEADER = "accesstoken"

# Mosyle's `os` value (and device_model, as a tiebreaker) -> abapit's
# canonical productFamily, so the existing family filter/charts work.
_OS_FAMILY = {
    "mac": "Mac", "macos": "Mac", "osx": "Mac",
    "tvos": "AppleTV", "atv": "AppleTV",
    "watchos": "Watch",
    "visionos": "Vision",
}

# Mosyle snake_case device field -> abapit canonical camelCase. Keeping the
# names the templates/reports/quick-find already expect means no UI changes.
_FIELD_MAP = {
    "serial_number": "serialNumber",
    "device_name": "deviceName",
    "device_model": "deviceModel",
    "model_name": "modelName",
    "osversion": "osVersion",
    "status": "status",
    "userid": "userId",
    "battery": "batteryLevel",
    "total_disk": "totalDiskBytes",
    "available_disk": "availableDiskBytes",
    "is_supervised": "isSupervised",
    "lostmode_status": "lostModeStatus",
    "imei": "imei",
    "meid": "meid",
    "wifi_mac_address": "wifiMacAddress",
    "bluetooth_mac_address": "bluetoothMacAddress",
    "ethernet_mac_address": "ethernetMacAddress",
    "enrollment_type": "enrollmentType",
    "managementstatus": "managementStatus",
    "username": "userName",
    # Mosyle returns the signed-in user under a few casings depending on
    # endpoint/platform; map them all to the canonical currentUser.
    "CurrentConsoleManagedUser": "currentUser",
    "currentconsolemanageduser": "currentUser",
}

# Epoch-seconds fields -> ISO 8601 (what abapit's date filters parse).
_DATE_MAP = {
    "date_last_beat": "lastCheckIn",
    "date_last_push": "lastPush",
    "date_app_info": "appInfoUpdated",
}


def _to_iso(value) -> str | None:
    """Normalize a Mosyle timestamp to an ISO-8601-ish string.

    Mosyle usually sends Unix epoch seconds, but some fields/tenants return a
    date or datetime string already. Keep date-ish strings as-is (a leading
    4-digit year with a dash — reports.parse_iso reads both date and datetime
    forms); convert epoch ints/numeric strings; return None for anything else
    so junk never reaches a date column or CSV cell.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str) and value[:4].isdigit() and "-" in value:
        return value  # already a date / datetime string
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def _family(raw: dict) -> str:
    os_value = str(raw.get("os") or "").lower()
    if os_value in _OS_FAMILY:
        return _OS_FAMILY[os_value]
    model = str(raw.get("device_model") or raw.get("model_name") or "").lower()
    device_type = str(raw.get("device_type") or "").lower()
    if "ipad" in model or "ipad" in device_type:
        return "iPad"
    if "iphone" in model or "iphone" in device_type:
        return "iPhone"
    if os_value == "ios":
        return "iPhone"  # default the remaining iOS devices to iPhone
    return ""


def adapt_device(raw: dict) -> dict:
    """One Mosyle device record -> a JSON:API-shaped item."""
    attrs: dict = {}
    consumed = set()
    for src, dst in _FIELD_MAP.items():
        if src in raw:
            attrs[dst] = raw[src]
            consumed.add(src)
    for src, dst in _DATE_MAP.items():
        if src in raw:
            attrs[dst] = _to_iso(raw[src])
            consumed.add(src)
    if not attrs.get("deviceModel"):
        attrs["deviceModel"] = raw.get("model_name", "")
    attrs["productFamily"] = _family(raw)
    attrs["managedBy"] = "Mosyle"
    consumed.update({"os", "device_type"})
    # Preserve every other field Mosyle sent (so CSV export loses nothing),
    # without clobbering the canonical names above.
    for key, value in raw.items():
        if key not in consumed:
            attrs.setdefault(key, value)
    device_id = raw.get("serial_number") or raw.get("deviceudid") or ""
    return {"type": "orgDevices", "id": device_id, "attributes": attrs}


class MosyleClient:
    """Synchronous, read-only client bound to one Mosyle Business org."""

    is_demo = False

    def __init__(self, org: Org, base_url: str | None = None,
                 max_pages: int = 200,
                 transport: httpx.BaseTransport | None = None):
        self.org = org
        self.base_url = (base_url or MOSYLE_BUSINESS_BASE).rstrip("/")
        self.max_pages = max_pages
        self._http = httpx.Client(timeout=60, transport=transport)
        self._devices_cache: list[dict] | None = None

    # -- plumbing ---------------------------------------------------------

    def _post(self, path: str, operation: str, options: dict | None = None) -> dict:
        body: dict = {"operation": operation}
        if options:
            body["options"] = options
        url = f"{self.base_url}/{path}"
        headers = {TOKEN_HEADER: self.org.mosyle_token}
        started = time.perf_counter()
        for attempt in range(5):
            try:
                resp = self._http.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                # All our operations are reads ("list"), so retrying is safe.
                if attempt < 4:
                    log.warning("network error on POST %s (attempt %d): %s — retrying",
                                url, attempt + 1, exc)
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise ApiError(0, f"network error: {exc}") from exc
            if resp.status_code == 429 and attempt < 4:
                try:
                    delay = float(resp.headers.get("Retry-After", ""))
                except ValueError:
                    delay = 2.0 ** attempt
                log.warning("429 from Mosyle on %s — backing off %.0fs (attempt %d/5)",
                            url, min(delay, 60), attempt + 1)
                time.sleep(min(delay, 60))
                continue
            break
        elapsed_ms = (time.perf_counter() - started) * 1000
        log.info("POST %s [%s] -> %d (%.0f ms)", url, operation,
                 resp.status_code, elapsed_ms)
        if resp.status_code >= 400:
            raise ApiError(resp.status_code, _error_message(resp))
        return resp.json()

    @staticmethod
    def _rows(body: dict) -> list[dict]:
        """Pull the device list out of a Mosyle list response, tolerating the
        couple of envelope shapes seen in the wild."""
        if not isinstance(body, dict):
            return []
        for key in ("devices", "response", "data", "rows"):
            value = body.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict) and isinstance(value.get("devices"), list):
                return value["devices"]
        return []

    # -- devices ----------------------------------------------------------

    PAGE_SIZE = 1000

    def devices(self) -> list[dict]:
        """Every device, adapted to JSON:API shape. Requests a large page_size
        and pages by incrementing `page`, stopping when a page surfaces no new
        serials — so it terminates whether Mosyle paginates by `page` or
        returns the whole fleet at once.

        Caveat (see module docstring): if a tenant ignores `page` and uses
        offset/cursor paging instead, this fetches only the first page. The
        per-page count is logged so that's noticeable; confirm against a real
        tenant with a >page_size fleet and adjust if a response differs.
        """
        items: list[dict] = []
        seen: set[str] = set()
        page = 1
        while page <= self.max_pages:
            body = self._post("devices", "list",
                              {"page": page, "page_size": self.PAGE_SIZE})
            rows = self._rows(body)
            fresh = [r for r in rows
                     if (r.get("serial_number") or r.get("deviceudid")) not in seen]
            log.info("Mosyle devices page %d: %d rows, %d new", page, len(rows), len(fresh))
            if not fresh:
                break
            for row in fresh:
                seen.add(row.get("serial_number") or row.get("deviceudid"))
                items.append(adapt_device(row))
            page += 1
        else:
            log.warning("Mosyle device list hit max_pages=%d (%d devices) — "
                        "raise max_pages", self.max_pages, len(items))
        self._devices_cache = items
        return items

    def device(self, device_id: str) -> dict:
        if self._devices_cache is None:
            self.devices()
        return next((d for d in (self._devices_cache or []) if d["id"] == device_id), {})

    # AppleCare/warranty and ABM assignment have no Mosyle equivalent; return
    # empty so the (gated) device-detail template renders without crashing.
    def device_applecare(self, device_id: str) -> list[dict]:
        return []

    def device_assigned_server(self, device_id: str) -> dict | None:
        return None

    # -- MDM servers: Mosyle *is* the MDM, so there are no ABM-style servers.
    # The dashboard and device-detail pages call these unconditionally; empty
    # values keep them working (the dashboard's MDM panel is gated off).
    def mdm_servers(self) -> list[dict]:
        return []

    def mdm_server_device_ids(self, server_id: str) -> list[str]:
        return []

    # -- health / capabilities -------------------------------------------

    def ping(self) -> None:
        """Cheap auth/connectivity check for Settings → Test."""
        self._post("devices", "list", {"page": 1})

    def probe_capabilities(self) -> list[dict]:
        """Mosyle has no permissions endpoint either; the signal is whether a
        list call succeeds with this token."""
        try:
            self.ping()
            status = "ok"
        except ApiError as exc:
            status = "forbidden" if exc.status in (401, 403) else f"error {exc.status}"
        return [{"section": "devices", "capability": "Devices",
                 "kind": "read", "status": status}]

    def close(self) -> None:
        self._http.close()


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        for key in ("error", "errorMessage", "message"):
            if body.get(key):
                return f"Mosyle: {body[key]}"
    except Exception:
        pass
    return f"HTTP {resp.status_code}: {resp.text[:300]}"
