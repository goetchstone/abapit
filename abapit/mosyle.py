"""HTTP client for the Mosyle Business API — read-only, for now.

Where the Apple Business/School APIs are JSON:API (GET /v1/<resource>, items
under `data`, cursor pagination), Mosyle's is operation-style: you POST to one
endpoint per object (/v1/devices, /v1/usergroups, ...) with a body
``{"operation": "list", "options": {...}}``.

Auth (per Mosyle's docs; Basic auth is deprecated): POST the admin email +
password to ``/login`` with the API access token in the ``accessToken`` header;
the response carries a Bearer JWT in the ``Authorization`` header that lasts 24
hours. Every other request sends BOTH ``accessToken`` and
``Authorization: Bearer <jwt>``.

The device list response is nested:
``{"status":"OK","response":[{"devices":[...], "rows":N, "page":1, "page_size":50}]}``
and the list REQUIRES an ``os`` (ios|mac|tvos|visionos), so we iterate the
platforms and merge. Each device is adapted to the same
``{"type","id","attributes"}`` shape the rest of abapit speaks, so the existing
device pages, reports, and CSV export work unchanged.
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
# Logs Stream is a separate service with its own access token + login.
MOSYLE_LOGS_BASE = "https://businessapilogs.mosyle.com/v1"
TOKEN_HEADER = "accessToken"
# The device list requires an `os`; iterate the platforms and merge.
OS_VALUES = ("ios", "mac", "tvos", "visionos")
PAGE_SIZE = 500
JWT_TTL = 23 * 3600  # Mosyle Bearer tokens last 24h; refresh a little early.

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
    "deviceudid": "deviceUdid",
    "device_name": "deviceName",
    "device_model": "deviceModelId",       # e.g. "iPad12,1"
    "device_model_name": "deviceModel",     # e.g. "iPad (9th generation)"
    "model_name": "modelName",              # e.g. "iPad"
    "osversion": "osVersion",
    "status": "status",
    "enrollment_type": "enrollmentType",
    "ManagementStatus": "managementStatus",
    "userid": "userId",
    "useremail": "userEmail",
    "username": "userName",
    "CurrentConsoleManagedUser": "currentUser",
    "currentconsolemanageduser": "currentUser",
    "battery": "batteryLevel",
    "total_disk": "totalDiskGB",
    "available_disk": "availableDiskGB",
    "is_supervised": "isSupervised",
    "lostmode_status": "lostModeStatus",
    "asset_tag": "assetTag",
    "imei": "imei",
    "meid": "meid",
    "wifi_mac_address": "wifiMacAddress",
    "bluetooth_mac_address": "bluetoothMacAddress",
    "ethernet_mac_address": "ethernetMacAddress",
}

# Epoch-seconds (or ISO) fields -> the camelCase names abapit reads.
_DATE_MAP = {
    "date_last_beat": "lastCheckIn",
    "date_last_push": "lastPush",
    "date_enroll": "enrolledAt",
    "date_checkin": "lastMdmCheckIn",
    "date_app_info": "appInfoUpdated",
}


def _to_iso(value) -> str | None:
    """Normalize a Mosyle timestamp to an ISO-8601-ish string.

    Mosyle usually sends Unix epoch seconds (as strings), but some fields/
    tenants return a date or datetime string already. Keep date-ish strings
    (a leading 4-digit year with a dash — reports.parse_iso reads both date
    and datetime forms); convert epochs; return None for anything else so
    junk never reaches a date column or CSV cell.
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
    model = str(raw.get("device_model") or raw.get("model_name")
                or raw.get("device_model_name") or "").lower()
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
        attrs["deviceModel"] = raw.get("device_model") or raw.get("model_name", "")
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


def adapt_user(raw: dict) -> dict:
    """One Mosyle user -> JSON:API item (id = Mosyle user id / identifier)."""
    attrs = {
        "name": raw.get("name", ""),
        "email": raw.get("email") or raw.get("managedappleid", ""),
        "identifier": raw.get("identifier", ""),
        "userType": raw.get("type") or raw.get("usertype", ""),
    }
    for key, value in raw.items():
        attrs.setdefault(key, value)
    user_id = str(raw.get("iduser") or raw.get("id") or raw.get("identifier") or "")
    return {"type": "mosyleUsers", "id": user_id, "attributes": attrs}


def adapt_group(raw: dict, kind: str) -> dict:
    """One Mosyle user/device group -> JSON:API item."""
    members = raw.get("device_numbers")
    if members is None:
        primary = raw.get("idusers_primary")
        members = len(primary) if isinstance(primary, list) else raw.get("members")
    attrs = {
        "name": raw.get("name", ""),
        "identifier": raw.get("identifier", ""),
        "memberCount": members,
        "os": raw.get("os", ""),
    }
    for key, value in raw.items():
        attrs.setdefault(key, value)
    group_id = str(raw.get("idusergroup") or raw.get("iddevicegroup")
                   or raw.get("id") or "")
    return {"type": kind, "id": group_id, "attributes": attrs}


def normalize_logs(response: dict) -> list[dict]:
    """Flatten Mosyle Logs Stream's per-type nested response into one reverse-
    chronological event list: {when, kind, label, status, device, serial, user}.
    Tolerant of the differing per-type shapes (compliance by platform,
    zero_trust under Events, action_logs/av under Logs, dns as a bare list)."""
    events: list[dict] = []
    if not isinstance(response, dict):
        return events

    def add(kind, when, label, status="", device="", serial="", user=""):
        events.append({
            "kind": kind,
            "when": _to_iso(when) or (str(when) if when else ""),
            "label": label or "", "status": status or "",
            "device": device or "", "serial": serial or "", "user": user or "",
        })

    compliance = response.get("compliance")
    if isinstance(compliance, dict):
        for sub in compliance.values():
            for log in (sub.get("Logs") if isinstance(sub, dict) else None) or []:
                add("compliance", log.get("Timestamp"), log.get("RuleName"),
                    log.get("Status"), log.get("DeviceName"),
                    log.get("SerialNumber"), log.get("E-mail"))

    zero_trust = response.get("zero_trust")
    zt_logs = []
    if isinstance(zero_trust, dict):
        events_block = zero_trust.get("Events")
        zt_logs = ((events_block.get("Logs") if isinstance(events_block, dict) else None)
                   or zero_trust.get("Logs") or [])
    for log in zt_logs:
        add("zero_trust", log.get("Timestamp"),
            log.get("Application") or log.get("FileName"), log.get("Action"),
            log.get("Device"), "", log.get("Source"))

    action_logs = response.get("action_logs")
    for log in (action_logs.get("Logs") if isinstance(action_logs, dict) else None) or []:
        add("action", log.get("ActionDate"), log.get("Action"), "",
            "", "", log.get("UserName") or log.get("E-mail"))

    for key in ("dns", "av"):
        block = response.get(key)
        block_logs = (block.get("Logs") if isinstance(block, dict)
                      else block if isinstance(block, list) else None)
        for log in block_logs or []:
            if isinstance(log, dict):
                add(key, log.get("Timestamp"),
                    log.get("Domain") or log.get("FileName") or log.get("RuleName"),
                    log.get("Action") or log.get("Status"),
                    log.get("Device") or log.get("DeviceName"), log.get("SerialNumber"))

    events.sort(key=lambda e: e["when"], reverse=True)
    return events


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
        self._jwt: str | None = None
        self._jwt_exp = 0.0
        self._logs_jwt: str | None = None
        self._logs_jwt_exp = 0.0
        self._devices_cache: list[dict] | None = None

    # -- auth -------------------------------------------------------------

    def _login(self) -> None:
        """Exchange admin email/password (+ accessToken header) for a Bearer
        JWT returned in the Authorization response header (valid ~24h)."""
        if not self.org.mosyle_email or not self.org.mosyle_password:
            return  # token-only mode (best effort; most tenants require login)
        url = f"{self.base_url}/login"
        try:
            resp = self._http.post(
                url, json={"email": self.org.mosyle_email,
                           "password": self.org.mosyle_password},
                headers={TOKEN_HEADER: self.org.mosyle_token,
                         "Content-Type": "application/json"})
        except httpx.HTTPError as exc:
            raise ApiError(0, f"network error reaching Mosyle /login: {exc}") from exc
        if resp.status_code != 200:
            raise ApiError(resp.status_code,
                           f"Mosyle /login failed: {_error_message(resp)}")
        auth = resp.headers.get("Authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else auth.strip()
        if not token:  # some stacks echo it in the body
            try:
                body = resp.json()
                token = str(body.get("Authorization") or body.get("token") or "")
                if token[:7].lower() == "bearer ":
                    token = token[7:].strip()
            except Exception:
                token = ""
        if not token:
            raise ApiError(resp.status_code,
                           "Mosyle /login did not return a Bearer token — check "
                           "the email, password, and access token.")
        self._jwt = token
        self._jwt_exp = time.time() + JWT_TTL
        log.info("Mosyle: obtained Bearer token for %s", self.org.name)

    def _auth_headers(self) -> dict:
        headers = {TOKEN_HEADER: self.org.mosyle_token,
                   "Content-Type": "application/json"}
        if self.org.mosyle_email and self.org.mosyle_password:
            if not self._jwt or self._jwt_exp - 60 < time.time():
                self._login()
            if self._jwt:
                headers["Authorization"] = f"Bearer {self._jwt}"
        return headers

    # -- plumbing ---------------------------------------------------------

    def _post(self, path: str, operation: str, options: dict | None = None) -> dict:
        body: dict = {"operation": operation}
        if options:
            body["options"] = options
        url = f"{self.base_url}/{path}"
        started = time.perf_counter()
        relogged = False
        for attempt in range(5):
            headers = self._auth_headers()  # logs in / refreshes the JWT as needed
            try:
                resp = self._http.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                if attempt < 4:  # list operations are reads — safe to retry
                    log.warning("network error on POST %s (attempt %d): %s — retrying",
                                url, attempt + 1, exc)
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise ApiError(0, f"network error: {exc}") from exc
            if resp.status_code in (401, 403) and not relogged and self.org.mosyle_email:
                log.info("%d from Mosyle %s — refreshing Bearer token",
                         resp.status_code, url)
                self._jwt = None
                relogged = True
                continue
            if resp.status_code == 429 and attempt < 4:
                try:
                    delay = float(resp.headers.get("Retry-After", ""))
                except ValueError:
                    delay = 2.0 ** attempt
                log.warning("429 from Mosyle on %s — backing off %.0fs", url, min(delay, 60))
                time.sleep(min(delay, 60))
                continue
            break
        log.info("POST %s [%s] -> %d (%.0f ms)", url, operation, resp.status_code,
                 (time.perf_counter() - started) * 1000)
        if resp.status_code >= 400:
            raise ApiError(resp.status_code, _error_message(resp))
        return resp.json()

    @staticmethod
    def _rows(body: dict) -> list[dict]:
        """Devices from a list response. Real shape is
        {"status":"OK","response":[{"devices":[...], "rows":N, ...}]}; error or
        empty entries (DEVICES_NOTFOUND, MISSING_DATA) carry no "devices" key
        and yield []."""
        if not isinstance(body, dict):
            return []
        resp = body.get("response")
        if isinstance(resp, list):
            for entry in resp:
                if isinstance(entry, dict) and isinstance(entry.get("devices"), list):
                    return entry["devices"]
            return []
        if isinstance(resp, dict) and isinstance(resp.get("devices"), list):
            return resp["devices"]
        if isinstance(body.get("devices"), list):  # tolerate a flatter shape
            return body["devices"]
        return []

    # -- devices ----------------------------------------------------------

    def devices(self) -> list[dict]:
        """Every device across all platforms, adapted to JSON:API shape. The
        list requires an `os`, so iterate ios/mac/tvos/visionos and page each
        (stop when a page is short); dedupe by serial as a safety net."""
        items: list[dict] = []
        seen: set[str] = set()
        for os_value in OS_VALUES:
            page = 1
            while page <= self.max_pages:
                body = self._post("devices", "list",
                                  {"os": os_value, "page": page, "page_size": PAGE_SIZE})
                rows = self._rows(body)
                added = 0
                for raw in rows:
                    key = raw.get("serial_number") or raw.get("deviceudid")
                    if key and key in seen:
                        continue
                    if key:
                        seen.add(key)
                    items.append(adapt_device(raw))
                    added += 1
                log.info("Mosyle %s page %d: %d rows, %d new", os_value, page,
                         len(rows), added)
                # Stop on a short page, or when a page adds nothing new (guards
                # against a tenant that ignores `page` and re-returns the set).
                if len(rows) < PAGE_SIZE or added == 0:
                    break
                page += 1
            else:
                log.warning("Mosyle %s list hit max_pages=%d — raise max_pages",
                            os_value, self.max_pages)
        self._devices_cache = items
        return items

    def device(self, device_id: str) -> dict:
        if self._devices_cache is None:
            self.devices()
        return next((d for d in (self._devices_cache or []) if d["id"] == device_id), {})

    # -- people & groups (read-only inventory) ----------------------------

    @staticmethod
    def _extract(body: dict, keys: tuple) -> list[dict]:
        """Pull a named list (users/usergroups/devicegroups) out of the
        response[] envelope, tolerating a dict-or-list `response`."""
        resp = body.get("response") if isinstance(body, dict) else None
        entries = resp if isinstance(resp, list) else ([resp] if isinstance(resp, dict) else [])
        for entry in entries:
            if isinstance(entry, dict):
                for key in keys:
                    if isinstance(entry.get(key), list):
                        return entry[key]
        return []

    def _list_objects(self, path: str, operation: str, keys: tuple,
                      options: dict | None = None) -> list[dict]:
        items: list[dict] = []
        page = 1
        while page <= self.max_pages:
            opts = dict(options or {})
            opts.update({"page": page, "page_size": PAGE_SIZE})
            rows = self._extract(self._post(path, operation, opts), keys)
            items.extend(rows)
            if len(rows) < PAGE_SIZE:
                break
            page += 1
        return items

    def users(self) -> list[dict]:
        return [adapt_user(u) for u in
                self._list_objects("users", "list_users", ("users",))]

    def user_groups(self) -> list[dict]:
        return [adapt_group(g, "userGroups") for g in
                self._list_objects("usergroups", "list_usergroup", ("usergroups", "groups"))]

    def device_groups(self) -> list[dict]:
        return [adapt_group(g, "deviceGroups") for g in
                self._list_objects("devicegroups", "list_devicegroup",
                                   ("devicegroups", "groups", "device_groups"))]

    # -- logs stream (separate host + token; the device "status channel") --

    def logs_configured(self) -> bool:
        return bool(self.org.mosyle_logs_token)

    def _logs_login(self) -> str:
        if self._logs_jwt and self._logs_jwt_exp - 60 > time.time():
            return self._logs_jwt
        if not (self.org.mosyle_email and self.org.mosyle_password):
            raise ApiError(0, "Mosyle Logs Stream needs the admin email/password to log in.")
        try:
            resp = self._http.post(
                f"{MOSYLE_LOGS_BASE}/login",
                json={"email": self.org.mosyle_email, "password": self.org.mosyle_password},
                headers={TOKEN_HEADER: self.org.mosyle_logs_token,
                         "Content-Type": "application/json"})
        except httpx.HTTPError as exc:
            raise ApiError(0, f"network error reaching Mosyle Logs /login: {exc}") from exc
        if resp.status_code != 200:
            raise ApiError(resp.status_code, f"Mosyle Logs /login failed: {_error_message(resp)}")
        auth = resp.headers.get("Authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else auth.strip()
        if not token:
            raise ApiError(resp.status_code, "Mosyle Logs /login did not return a Bearer token.")
        self._logs_jwt = token
        self._logs_jwt_exp = time.time() + JWT_TTL
        return token

    def logs(self, log_types: list[str] | None = None, page: int = 1) -> list[dict]:
        """Logs Stream events (compliance, zero-trust, admin actions, av, dns),
        normalized + flattened. Empty if Logs Stream isn't configured."""
        if not self.org.mosyle_logs_token:
            return []
        types = log_types or ["compliance", "zero_trust", "action_logs", "av", "dns"]
        body = {"LogType": types, "page": page}
        headers = {TOKEN_HEADER: self.org.mosyle_logs_token,
                   "Content-Type": "application/json",
                   "Authorization": f"Bearer {self._logs_login()}"}
        url = f"{MOSYLE_LOGS_BASE}/logsstream"
        try:
            resp = self._http.post(url, json=body, headers=headers)
            if resp.status_code in (401, 403):  # token expired — re-login once
                self._logs_jwt = None
                headers["Authorization"] = f"Bearer {self._logs_login()}"
                resp = self._http.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ApiError(0, f"network error: {exc}") from exc
        if resp.status_code >= 400:
            raise ApiError(resp.status_code, _error_message(resp))
        return normalize_logs(resp.json().get("response", {}))

    # AppleCare/warranty and ABM assignment have no Mosyle equivalent; return
    # empty so the (gated) device-detail template renders without crashing.
    def device_applecare(self, device_id: str) -> list[dict]:
        return []

    def device_assigned_server(self, device_id: str) -> dict | None:
        return None

    # Mosyle *is* the MDM, so there are no ABM-style servers. The dashboard and
    # device-detail pages call these unconditionally; empty values keep them
    # working (the dashboard's MDM panel is gated off for Mosyle).
    def mdm_servers(self) -> list[dict]:
        return []

    def mdm_server_device_ids(self, server_id: str) -> list[str]:
        return []

    # -- health / capabilities -------------------------------------------

    def ping(self) -> None:
        """Auth + connectivity check for Settings → Test: forces a login (if
        credentials are set) and one tiny list call. Auth failures surface as
        an ApiError; a tenant with no Macs just returns no rows."""
        self._post("devices", "list", {"os": "mac", "page": 1, "page_size": 1})

    def probe_capabilities(self) -> list[dict]:
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
        for key in ("error", "errorMessage", "message", "info"):
            if body.get(key):
                return f"Mosyle: {body[key]}"
        # operation-level error nested in response[]
        resp_list = body.get("response")
        if isinstance(resp_list, list) and resp_list:
            entry = resp_list[0]
            if isinstance(entry, dict) and entry.get("info"):
                return f"Mosyle: {entry.get('status', '')} {entry['info']}".strip()
    except Exception:
        pass
    return f"HTTP {resp.status_code}: {resp.text[:300]}"
