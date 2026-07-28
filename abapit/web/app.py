"""FastAPI app: server-rendered pages over the Apple Business/School APIs.

Everything is synchronous and simple: routes call the (cached) API client,
hand plain dicts to Jinja templates, and return HTML. No database, no JS
framework. Collection responses are cached in memory for five minutes per
org so casual browsing doesn't hammer Apple's rate limits.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import __version__, config, history
from ..assign import plan as plan_assignment
from ..auth import AuthError, token_cache
from ..blueprint_plan import plan_relationship
from ..client import ApiError, sections_for
from ..demo import DemoClient
from ..factory import build_client
from ..reports import (
    assignment_summary,
    coverage_report,
    device_stats,
    device_timeline,
    fleet_age_report,
    items_to_csv,
    items_to_rows,
    mosyle_os_breakdown,
    mosyle_stale_devices,
    parse_iso,
    reconcile_enrollments,
)

CACHE_TTL = 300  # seconds
MAX_TABLE_ROWS = 500

NAV = [
    ("Overview", [("dashboard", "/", "Dashboard")]),
    ("Devices", [
        ("devices", "/devices", "Devices"),
        ("mdm_servers", "/mdm-servers", "MDM Servers"),
        ("mdm_enrolled", "/mdm-enrolled", "Apple MDM Enrolled"),
        ("device_groups", "/device-groups", "Device Groups"),
        ("assign", "/assign", "Assign to MDM"),
    ]),
    ("People", [
        ("users", "/users", "Users"),
        ("user_groups", "/user-groups", "User Groups"),
        ("org_units", "/org-units", "Org Units"),
    ]),
    ("Content", [
        ("apps", "/apps", "Apps"),
        ("packages", "/packages", "Packages"),
        ("blueprints", "/blueprints", "Blueprints"),
        ("configurations", "/configurations", "Configurations"),
    ]),
    ("Activity", [
        ("audit_events", "/audit-events", "Audit Events"),
        ("mosyle_logs", "/mosyle-activity", "Activity & Compliance"),
        ("changes", "/changes", "Changes"),
    ]),
    ("Reports", [
        ("coverage", "/reports/coverage", "Coverage"),
        ("fleet_age", "/reports/fleet-age", "Fleet Age"),
        ("mosyle_os_breakdown", "/reports/mosyle-os-breakdown", "Mosyle OS Versions"),
        ("mosyle_stale", "/reports/mosyle-stale", "Mosyle Stale Devices"),
        ("reconciliation", "/reports/reconciliation", "ABM ↔ Mosyle"),
    ]),
]

# Web-cache entries that can be warm-started from the latest snapshot:
# cache name -> snapshot resource name.
SNAPSHOT_RESOURCES = {
    "devices": "devices",
    "mdm_servers": "mdm_servers",
    "server_device_ids": "assignments",
    "mdm_enrolled": "mdm_enrolled",
    "users": "users",
    "user_groups": "user_groups",
    "apps": "apps",
    "packages": "packages",
    "blueprints": "blueprints",
    "configurations": "configurations",
}

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

# Write-capability keys (match ApiClient.WRITE_PROBES); forms render only when
# the active org's role is probed `ok` or not-yet-probed (optimistic).
WRITE_SECTIONS = ("blueprints_write", "configurations_write", "mdm_servers_write")

log = logging.getLogger("abapit")

# Set per-request when a page was served from snapshot data; read by render().
_stale_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "abapit_stale", default=None)

# section key -> (client method name, page title) for generic listings/exports
RESOURCES = {
    "devices": ("devices", "Devices"),
    "mdm-servers": ("mdm_servers", "MDM Servers"),
    "mdm-enrolled": ("mdm_enrolled_devices", "Apple MDM Enrolled Devices"),
    "users": ("users", "Users"),
    "user-groups": ("user_groups", "User Groups"),
    "org-units": ("org_units", "Org Units"),
    "device-groups": ("device_groups", "Device Groups"),
    "apps": ("apps", "Apps"),
    "packages": ("packages", "Packages"),
    "blueprints": ("blueprints", "Blueprints"),
    "configurations": ("configurations", "Configurations"),
}


class NoOrgError(Exception):
    pass


def fmt_date(value, with_time: bool = False):
    parsed = parse_iso(value) if isinstance(value, str) else None
    if not parsed:
        return value or "—"
    return parsed.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def fmt_ago(value):
    """Relative 'last seen' for the device-360 posture strip."""
    parsed = parse_iso(value) if isinstance(value, str) else None
    if not parsed:
        return "—"
    secs = (datetime.now(timezone.utc) - parsed).total_seconds()
    if secs < 0:
        return fmt_date(value, with_time=True)
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{int(secs // 60)} min ago"
    if secs < 129600:
        return f"{int(secs // 3600)} h ago"
    return f"{int(secs // 86400)} d ago"


def fmt_yesno(value):
    """Mosyle returns booleans as '1'/'0' strings; render them readably."""
    text = str(value).strip().lower()
    if text in ("1", "true", "yes"):
        return "Yes"
    if text in ("0", "false", "no", "", "none"):
        return "No"
    return value


def _orgs_by_provider(cfg, provider: str) -> list[tuple[str, object]]:
    return [(slug, org) for slug, org in cfg.orgs.items()
            if org.provider == provider]


def _has_both_providers(cfg) -> bool:
    return bool(_orgs_by_provider(cfg, "apple")) and bool(_orgs_by_provider(cfg, "mosyle"))


def _find_serial(devices: list[dict], device_id: str):
    key = (device_id or "").strip().upper()
    return next((d for d in devices if (d.get("id") or "").strip().upper() == key), None)


def _other_provider_slug(cfg, provider: str):
    """Slug of an org of the OTHER provider, to merge into a device 360."""
    orgs = _orgs_by_provider(cfg, "mosyle" if provider == "apple" else "apple")
    # active_org belongs to the current provider, never the other one, so it's
    # always the first org of the other provider.
    return orgs[0][0] if orgs else None


def _abm_audit(c, serial: str, days: int = 30) -> list[dict]:
    """Best-effort ABM audit events naming this serial (device lifecycle:
    added, assigned/unassigned). Empty if the role can't read audit events."""
    if "audit_events" not in sections_for(c.org.scope, c.org.provider):
        return []
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        events = c.audit_events(start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                end.strftime("%Y-%m-%dT%H:%M:%SZ"))
    except (ApiError, AuthError):
        return []
    key = (serial or "").strip().upper()
    return [e for e in events
            if str(e.get("attributes", {}).get("subjectId", "")).strip().upper() == key
            or str(e.get("attributes", {}).get("subjectName", "")).strip().upper() == key][:20]


def create_app(demo: bool = False,
               allowed_hosts: list[str] | None = None) -> FastAPI:
    app = FastAPI(title="abapit", docs_url=None, redoc_url=None)
    base = Path(__file__).parent
    app.mount("/static", StaticFiles(directory=base / "static"), name="static")
    templates = Jinja2Templates(directory=base / "templates")
    templates.env.filters["dt"] = fmt_date
    templates.env.filters["dtt"] = lambda v: fmt_date(v, with_time=True)
    templates.env.filters["ago"] = fmt_ago
    templates.env.filters["yesno"] = fmt_yesno

    # Reject requests whose Host header isn't ours — blocks DNS-rebinding
    # attacks, where a malicious site points its own domain at 127.0.0.1.
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=allowed_hosts or list(LOCAL_HOSTS))

    @app.middleware("http")
    async def block_cross_origin_posts(request: Request, call_next):
        """Browsers happily fire cross-origin form POSTs at localhost.
        Reject mutations that arrive from another web origin (CSRF);
        same-origin browser posts and non-browser clients are unaffected.

        Both checks compare the FULL origin (scheme + host + port), not just
        the hostname: another app on a different loopback PORT is a different
        origin, and browsers label such a request `same-site` (loopback hosts
        have no registrable domain), so neither a localhost host allow-list nor
        accepting `same-site` would be safe.
        """
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
                return Response("Cross-origin POST blocked.", status_code=403)
            fetch_site = request.headers.get("sec-fetch-site")
            if fetch_site and fetch_site not in ("same-origin", "none"):
                return Response("Cross-site request blocked.", status_code=403)
        return await call_next(request)

    app.state.demo = demo
    app.state.demo_client = DemoClient() if demo else None
    app.state.clients = {}          # org slug -> provider client
    app.state.cache = {}            # (org key, name) -> (timestamp, value)
    app.state.refreshing = set()    # cache keys with a background fetch in flight
    app.state.refresh_lock = threading.Lock()
    app.state.force_live = {}       # org key -> ts; set by Refresh to bypass warm-start

    # ---- helpers ---------------------------------------------------------

    def client():
        if app.state.demo:
            return app.state.demo_client
        cfg = config.load()
        org = cfg.get_active()
        if org is None:
            raise NoOrgError()
        if cfg.active_org not in app.state.clients:
            app.state.clients[cfg.active_org] = build_client(org)
        return app.state.clients[cfg.active_org]

    def cached(name: str, fetch, force: bool = False):
        """Stale-while-revalidate, with snapshot warm-start.

        - fresh in-memory hit: serve it
        - expired in-memory hit: serve it, refetch in the background
        - no hit but a snapshot has this resource: serve the snapshot copy
          (flagged so the page shows a provenance banner), refetch in the
          background — this is what makes a 20k-device org open instantly
        - otherwise (first ever run, or right after Refresh): blocking fetch
        """
        c = client()
        key = (c.org.client_id, name)
        hit = app.state.cache.get(key)
        if not force and hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        forced_live = time.time() - app.state.force_live.get(c.org.client_id, 0) < 120
        if not force and not forced_live:
            if hit:
                _refresh_in_background(key, fetch, c)
                return hit[1]
            warm = _snapshot_value(c, name)
            if warm is not None:
                taken_at, value = warm
                _refresh_in_background(key, fetch, c)
                _stale_ctx.set({"taken_at": taken_at})
                return value
        value = fetch(c)
        app.state.cache[key] = (time.time(), value)
        return value

    def cached_for(slug: str, name: str, fetch):
        """Like cached(), but for an explicitly chosen org (used by the
        cross-org reconciliation report, which can't rely on the single
        active client). Shares app.state.cache keyed on the org's client_id,
        so the active org's `devices` entry is reused rather than refetched."""
        org = config.load().orgs.get(slug)
        if org is None:
            raise NoOrgError()
        if slug not in app.state.clients:
            app.state.clients[slug] = build_client(org)
        c = app.state.clients[slug]
        key = (c.org.client_id, name)
        hit = app.state.cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        value = fetch(c)
        app.state.cache[key] = (time.time(), value)
        return value

    def _snapshot_value(c, name: str):
        """Reconstruct a cache entry from the latest snapshot, if possible."""
        resource = SNAPSHOT_RESOURCES.get(name)
        if resource is None or c.is_demo:
            return None
        found = history.latest_resource(c.org.client_id, resource)
        if found is None:
            return None
        _, taken_at, items = found
        if name == "server_device_ids":
            by_server: dict[str, list[str]] = {}
            for item in items:
                server_id = item["attributes"].get("serverId", "")
                by_server.setdefault(server_id, []).append(item["id"])
            return taken_at, by_server
        return taken_at, items

    def _refresh_in_background(key, fetch, c) -> None:
        """Fetch live data on a daemon thread; single-flight per cache key."""
        with app.state.refresh_lock:
            if key in app.state.refreshing:
                return
            app.state.refreshing.add(key)

        def run():
            try:
                value = fetch(c)
                app.state.cache[key] = (time.time(), value)
            except Exception as exc:
                log.warning("background refresh of %s failed: %s", key[1], exc)
            finally:
                with app.state.refresh_lock:
                    app.state.refreshing.discard(key)

        threading.Thread(target=run, daemon=True, name=f"refresh-{key[1]}").start()

    def render(request: Request, template: str, active: str = "", **ctx):
        c = None
        try:
            c = client()
        except NoOrgError:
            pass
        cfg = config.load() if not app.state.demo else None
        scope = c.org.scope if c else "business"
        provider = c.org.provider if c else "apple"
        allowed = set(sections_for(scope, provider))
        # Reconciliation is cross-org, so it isn't a per-org section: surface
        # it whenever the config has at least one Apple AND one Mosyle org,
        # regardless of which one is currently active.
        if cfg and _has_both_providers(cfg):
            allowed.add("reconciliation")
        nav = [(group, [item for item in items if item[0] in allowed or item[0] == "dashboard"])
               for group, items in NAV]
        nav = [(group, items) for group, items in nav if items]
        # Locks must reflect the config as of THIS request — never the Org
        # snapshot frozen inside a cached ApiClient.
        fresh_org = cfg.get_active() if cfg else None
        caps = fresh_org.capabilities if fresh_org else {}
        writable = {k for k in WRITE_SECTIONS if caps.get(k) != "forbidden"}
        return templates.TemplateResponse(request, template, {
            "active": active,
            "version": __version__,
            "nav": nav,
            "denied": fresh_org.denied_sections() if fresh_org else set(),
            "probed_at": fresh_org.probed_at if fresh_org else "",
            "stale": _stale_ctx.get(),
            "demo": app.state.demo,
            "org": c.org if c else None,
            "orgs": cfg.orgs if cfg else {},
            "active_org": cfg.active_org if cfg else "demo",
            "msg": request.query_params.get("msg", ""),
            "allowed": allowed,
            "writable": writable,
            **ctx,
        })

    def guard(section: str):
        """Redirect to the dashboard if this section isn't available for the
        active org's provider/scope (e.g. users on an Apple School Manager
        org, or anything beyond devices on a Mosyle org)."""
        org = client().org
        if section not in sections_for(org.scope, org.provider):
            raise ApiError(404, f"The {section} section is not available for "
                                f"{org.provider}/{org.scope} orgs.")

    def matches(item: dict, q: str) -> bool:
        needle = q.lower()
        if needle in (item.get("id") or "").lower():
            return True
        return any(needle in str(v).lower()
                   for v in item.get("attributes", {}).values() if v is not None)

    # ---- error handling --------------------------------------------------

    @app.exception_handler(NoOrgError)
    async def no_org(request: Request, exc: NoOrgError):
        return RedirectResponse(
            "/settings?msg=" + quote("Add an org to get started, or run with --demo."),
            status_code=303)

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        hint = {
            401: "Credentials were rejected. Re-check the client ID, key ID and private key in Settings.",
            403: "This API account doesn't have access to that resource.",
            404: "Not found — the resource may not exist in this org.",
            429: "Rate limited by Apple. Wait a minute and use Refresh sparingly.",
        }.get(exc.status, "")
        return render(request, "error.html", title=f"API error {exc.status}",
                      message=str(exc), hint=hint)

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, exc: AuthError):
        return render(request, "error.html", title="Authentication failed",
                      message=str(exc),
                      hint="Verify this org's credentials in Settings, or re-create the API key in Apple Business/School Manager.")

    # ---- pages -----------------------------------------------------------

    def _server_ids(c, servers):
        """One relationships call per server — in parallel; Apple latency is
        ~300-500ms per call, so serializing these is what makes pages slow."""
        if not servers:
            return {}
        with ThreadPoolExecutor(max_workers=min(8, len(servers))) as pool:
            ids = pool.map(lambda s: c.mdm_server_device_ids(s["id"]), servers)
            return {s["id"]: i for s, i in zip(servers, ids)}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        org = client().org
        allowed = sections_for(org.scope, org.provider)
        devices = cached("devices", lambda c: c.devices())
        stats = device_stats(devices)
        # MDM-server/assignment data is an ABM concept; skip it for providers
        # that don't model it (Mosyle), so the page shows device stats only.
        assignment = None
        if "mdm_servers" in allowed:
            servers = cached("mdm_servers", lambda c: c.mdm_servers())
            server_ids = cached("server_device_ids", lambda c: _server_ids(c, servers))
            assignment = assignment_summary(devices, servers, server_ids)
        events = []
        if "audit_events" in allowed:
            try:
                end = datetime.now(timezone.utc)
                start = end - timedelta(days=7)
                events = cached("recent_events", lambda c: c.audit_events(
                    start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    end.strftime("%Y-%m-%dT%H:%M:%SZ")))[:10]
            except (ApiError, AuthError):
                events = []
        return render(request, "dashboard.html", active="dashboard",
                      stats=stats, assignment=assignment, events=events)

    @app.get("/devices", response_class=HTMLResponse)
    def devices_page(request: Request, q: str = "", family: str = "", status: str = ""):
        devices = cached("devices", lambda c: c.devices())
        families = sorted({d["attributes"].get("productFamily") or "" for d in devices} - {""})
        statuses = sorted({d["attributes"].get("status") or "" for d in devices} - {""})
        rows = [d for d in devices
                if (not q or matches(d, q))
                and (not family or d["attributes"].get("productFamily") == family)
                and (not status or d["attributes"].get("status") == status)]
        return render(request, "devices.html", active="devices",
                      devices=rows[:MAX_TABLE_ROWS], total=len(devices),
                      shown=min(len(rows), MAX_TABLE_ROWS), matched=len(rows),
                      q=q, family=family, status=status,
                      families=families, statuses=statuses)

    @app.get("/find")
    def find_device(q: str = ""):
        """Global quick-find: jump straight to a device when the query
        uniquely identifies one (serial, IMEI, MEID, EID, order number,
        MAC address — exact or unique-substring); otherwise fall back to
        the filtered device list."""
        q = q.strip()
        if not q:
            return RedirectResponse("/devices", status_code=303)
        devices = cached("devices", lambda c: c.devices())
        needle = q.lower()
        id_fields = ("serialNumber", "imei", "meid", "eid", "orderNumber",
                     "wifiMacAddress", "bluetoothMacAddress", "ethernetMacAddress")

        def identifiers(device):
            yield device.get("id") or ""
            for field in id_fields:
                yield str(device["attributes"].get(field) or "")

        exact = [d for d in devices
                 if any(value.lower() == needle for value in identifiers(d))]
        hits = exact or [d for d in devices
                         if any(needle in value.lower() for value in identifiers(d))]
        if len(hits) == 1:
            return RedirectResponse(f"/devices/{hits[0]['id']}", status_code=303)
        return RedirectResponse(f"/devices?q={quote(q)}", status_code=303)

    @app.get("/devices/{device_id}", response_class=HTMLResponse)
    def device_page(request: Request, device_id: str):
        """Device 360: the active org's half, merged with the other provider's
        half (when configured) and a digested per-device activity timeline."""
        c = client()

        def abm_detail(cc):
            # Three independent ABM calls — fetch concurrently (~1 RTT, not 3).
            with ThreadPoolExecutor(max_workers=3) as pool:
                f_device = pool.submit(cc.device, device_id)
                f_coverage = pool.submit(cc.device_applecare, device_id)
                f_server = pool.submit(cc.device_assigned_server, device_id)
                return {"device": f_device.result(), "coverage": f_coverage.result(),
                        "server": f_server.result()}

        apple_side = mosyle_side = None
        abm_audit: list = []
        if c.org.provider == "mosyle":
            mosyle_side = _find_serial(cached("devices", lambda cc: cc.devices()), device_id)
        else:
            apple_side = cached(f"device:{device_id}", abm_detail)
            abm_audit = _abm_audit(c, device_id)

        # Merge the other provider's half, if one is configured (not in demo).
        if not app.state.demo:
            cfg = config.load()
            other = _other_provider_slug(cfg, c.org.provider)
            if other and c.org.provider == "apple":
                try:
                    mosyle_side = _find_serial(
                        cached_for(other, "devices", lambda cc: cc.devices()), device_id)
                except (ApiError, AuthError):
                    mosyle_side = None
            elif other:
                try:
                    apple_side = cached_for(other, f"device:{device_id}", abm_detail)
                    abm_audit = _abm_audit(app.state.clients[other], device_id)
                except (ApiError, AuthError):
                    apple_side = None

        abm_device = (apple_side or {}).get("device") or {}
        if not abm_device and not mosyle_side:
            raise ApiError(404, "Device not found in this org.")
        timeline = device_timeline(abm_device.get("attributes"),
                                   (mosyle_side or {}).get("attributes"), abm_audit)
        servers = []
        if "assign" in sections_for(c.org.scope, c.org.provider):
            servers = cached("mdm_servers", lambda cc: cc.mdm_servers())
        return render(request, "device_detail.html", active="devices",
                      device_id=device_id, abm=apple_side, mosyle=mosyle_side,
                      timeline=timeline, servers=servers)

    @app.get("/mdm-servers", response_class=HTMLResponse)
    def mdm_servers_page(request: Request):
        servers = cached("mdm_servers", lambda c: c.mdm_servers())
        server_ids = cached("server_device_ids", lambda c: _server_ids(c, servers))
        counts = {sid: len(ids) for sid, ids in server_ids.items()}
        return render(request, "mdm_servers.html", active="mdm_servers",
                      servers=servers, counts=counts)

    @app.get("/mdm-servers/{server_id}", response_class=HTMLResponse)
    def mdm_server_page(request: Request, server_id: str):
        c = client()
        server = c.mdm_server(server_id)
        serials = c.mdm_server_device_ids(server_id)
        return render(request, "mdm_server_detail.html", active="mdm_servers",
                      server=server, serials=serials)

    @app.get("/mdm-enrolled", response_class=HTMLResponse)
    def mdm_enrolled_page(request: Request, q: str = ""):
        guard("mdm_enrolled")
        items = cached("mdm_enrolled", lambda c: c.mdm_enrolled_devices())
        rows = [i for i in items if not q or matches(i, q)]
        return render(request, "mdm_enrolled.html", active="mdm_enrolled",
                      items=rows[:MAX_TABLE_ROWS], q=q, total=len(items))

    @app.get("/users", response_class=HTMLResponse)
    def users_page(request: Request, q: str = ""):
        guard("users")
        users = cached("users", lambda c: c.users())
        rows = [u for u in users if not q or matches(u, q)]
        # Mosyle users have a different shape than ABM's — render generically
        # rather than through ABM-specific columns.
        if client().org.provider == "mosyle":
            header, table = items_to_rows(rows[:MAX_TABLE_ROWS])
            return render(request, "generic_table.html", active="users",
                          title="Users", header=header, rows=table, export="users")
        return render(request, "users.html", active="users",
                      users=rows[:MAX_TABLE_ROWS], q=q, total=len(users))

    @app.get("/user-groups", response_class=HTMLResponse)
    def groups_page(request: Request):
        guard("user_groups")
        groups = cached("user_groups", lambda c: c.user_groups())
        if client().org.provider == "mosyle":
            header, table = items_to_rows(groups)
            return render(request, "generic_table.html", active="user_groups",
                          title="User Groups", header=header, rows=table,
                          export="user-groups")
        return render(request, "user_groups.html", active="user_groups", groups=groups)

    @app.get("/org-units", response_class=HTMLResponse)
    def org_units_page(request: Request):
        guard("org_units")
        units = cached("org_units", lambda c: c.org_units())
        header, table = items_to_rows(units)
        return render(request, "generic_table.html", active="org_units",
                      title="Organizational Units", header=header, rows=table,
                      export="org-units")

    @app.get("/org-units/{org_unit_id}", response_class=HTMLResponse)
    def org_unit_page(request: Request, org_unit_id: str):
        guard("org_units")
        item = cached(f"org_unit:{org_unit_id}", lambda c: c.org_unit(org_unit_id))
        return render(request, "item_detail.html", active="org_units",
                      title=item.get("attributes", {}).get("name", org_unit_id),
                      back_href="/org-units", back_label="Org Units",
                      plain_attrs=item.get("attributes", {}),
                      payload="", payload_label="")

    @app.get("/device-groups", response_class=HTMLResponse)
    def device_groups_page(request: Request):
        guard("device_groups")
        groups = cached("device_groups", lambda c: c.device_groups())
        header, table = items_to_rows(groups)
        return render(request, "generic_table.html", active="device_groups",
                      title="Device Groups", header=header, rows=table,
                      export="device-groups")

    @app.get("/mosyle-activity", response_class=HTMLResponse)
    def mosyle_activity_page(request: Request, type: str = ""):
        guard("mosyle_logs")
        c = client()
        if not c.org.mosyle_logs_token:
            return render(request, "mosyle_logs.html", active="mosyle_logs",
                          events=None, counts={}, non_compliant=0, type=type)
        # Logs Stream drains on read — drain (cache-throttled) and append to the
        # per-org store, then display the accumulated history. The drain is
        # destructive (Mosyle won't re-serve consumed events), so only do it for
        # a genuine same-origin page view, never a cross-origin GET.
        def _drain_and_store(cc):
            return history.append_mosyle_logs(cc.org.client_id, cc.logs())
        if request.headers.get("sec-fetch-site") in (None, "same-origin", "none"):
            cached("mosyle_logs", _drain_and_store)
        all_events = history.load_mosyle_logs(c.org.client_id)
        counts: dict = {}
        for event in all_events:
            counts[event["kind"]] = counts.get(event["kind"], 0) + 1
        non_compliant = sum(1 for e in all_events if e["kind"] == "compliance"
                            and "lost" in e["status"].lower())
        events = [e for e in all_events if not type or e["kind"] == type]
        return render(request, "mosyle_logs.html", active="mosyle_logs",
                      events=events[:MAX_TABLE_ROWS], counts=counts,
                      non_compliant=non_compliant, type=type, total=len(events),
                      captured=len(all_events),
                      enabled_types=getattr(c, "last_log_types", []))

    @app.get("/user-groups/{group_id}", response_class=HTMLResponse)
    def group_page(request: Request, group_id: str):
        guard("user_groups")
        c = client()
        group = c.user_group(group_id)
        member_ids = set(c.user_group_member_ids(group_id))
        users = cached("users", lambda c: c.users())
        members = [u for u in users if u["id"] in member_ids]
        return render(request, "user_group_detail.html", active="user_groups",
                      group=group, members=members,
                      unresolved=len(member_ids) - len(members))

    @app.get("/apps", response_class=HTMLResponse)
    def apps_page(request: Request, q: str = ""):
        guard("apps")
        apps = cached("apps", lambda c: c.apps())
        rows = [a for a in apps if not q or matches(a, q)]
        return render(request, "apps.html", active="apps", apps=rows, q=q)

    @app.get("/packages", response_class=HTMLResponse)
    def packages_page(request: Request):
        guard("packages")
        packages = cached("packages", lambda c: c.packages())
        header, rows = items_to_rows(packages)
        return render(request, "generic_table.html", active="packages",
                      title="Packages", header=header, rows=rows,
                      export="packages")

    @app.get("/blueprints", response_class=HTMLResponse)
    def blueprints_page(request: Request):
        guard("blueprints")
        blueprints = cached("blueprints", lambda c: c.blueprints())
        return render(request, "blueprints.html", active="blueprints",
                      blueprints=blueprints)

    def _bust_blueprints():
        org_key = client().org.client_id
        app.state.cache = {k: v for k, v in app.state.cache.items()
                           if not (k[0] == org_key and k[1] == "blueprints")}

    def _require_write(key: str):
        """Server-side check — the template also hides the forms, but a POST
        must never depend on the UI having hidden a button."""
        if not _is_writable(key):
            raise ApiError(403, "This API account's role can't make that change "
                                "(re-check Permissions in Settings if the role changed).")

    # NOTE: must be registered before /blueprints/{blueprint_id} or "new" is
    # matched as an id.
    @app.get("/blueprints/new", response_class=HTMLResponse)
    def blueprint_new_form(request: Request):
        guard("blueprints")
        _require_write("blueprints_write")
        return render(request, "blueprint_edit.html", active="blueprints",
                      blueprint=None, error="")

    @app.get("/blueprints/{blueprint_id}/edit", response_class=HTMLResponse)
    def blueprint_edit_form(request: Request, blueprint_id: str):
        guard("blueprints")
        _require_write("blueprints_write")
        bp = client().blueprint(blueprint_id).get("data", {})
        return render(request, "blueprint_edit.html", active="blueprints",
                      blueprint=bp, error="")

    @app.post("/blueprints", response_class=HTMLResponse)
    def blueprint_create(request: Request, name: str = Form(""),
                         description: str = Form("")):
        guard("blueprints")
        _require_write("blueprints_write")
        if not name.strip():
            return render(request, "blueprint_edit.html", active="blueprints",
                          blueprint=None, error="A blueprint needs a name.")
        created = client().create_blueprint({"name": name.strip(),
                                             "description": description.strip()})
        _bust_blueprints()
        msg = f"Created blueprint {name.strip()!r}."
        return RedirectResponse(f"/blueprints/{created.get('id', '')}?msg={quote(msg)}",
                                status_code=303)

    @app.post("/blueprints/{blueprint_id}/edit", response_class=HTMLResponse)
    def blueprint_update(request: Request, blueprint_id: str, name: str = Form(""),
                         description: str = Form("")):
        guard("blueprints")
        _require_write("blueprints_write")
        if not name.strip():
            bp = client().blueprint(blueprint_id).get("data", {})
            return render(request, "blueprint_edit.html", active="blueprints",
                          blueprint=bp, error="A blueprint needs a name.")
        client().update_blueprint(blueprint_id, {"name": name.strip(),
                                                 "description": description.strip()})
        _bust_blueprints()
        msg = "Blueprint updated."
        return RedirectResponse(f"/blueprints/{blueprint_id}?msg={quote(msg)}",
                                status_code=303)

    @app.get("/blueprints/{blueprint_id}/delete", response_class=HTMLResponse)
    def blueprint_delete_confirm(request: Request, blueprint_id: str):
        guard("blueprints")
        _require_write("blueprints_write")
        c = client()
        bp = c.blueprint(blueprint_id).get("data", {})
        name = bp.get("attributes", {}).get("name", blueprint_id)
        # Blast radius: what this blueprint currently drives.
        radius = []
        for rel in BP_RELS:
            try:
                count = len(c.blueprint_relationship_ids(blueprint_id, rel))
            except ApiError:
                continue
            if count:
                radius.append((BP_REL_TITLES[rel], count))
        return render(request, "confirm.html", active="blueprints",
                      title=f"Delete blueprint “{name}”?",
                      warning="Deleting a blueprint is permanent and stops it "
                              "provisioning the devices and people below.",
                      radius=radius, confirm_word=name,
                      action=f"/blueprints/{blueprint_id}/delete",
                      cancel_href=f"/blueprints/{blueprint_id}",
                      submit_label="Delete blueprint")

    @app.post("/blueprints/{blueprint_id}/delete")
    def blueprint_delete(blueprint_id: str, confirm: str = Form("")):
        guard("blueprints")
        _require_write("blueprints_write")
        c = client()
        bp = c.blueprint(blueprint_id).get("data", {})
        name = bp.get("attributes", {}).get("name", blueprint_id)
        if confirm.strip() != name:
            raise ApiError(400, "The typed name didn't match — nothing was deleted.")
        c.delete_blueprint(blueprint_id)
        _bust_blueprints()
        return RedirectResponse(f"/blueprints?msg={quote(f'Deleted blueprint {name!r}.')}",
                                status_code=303)

    BP_RELS = ("apps", "configurations", "packages", "userGroups", "devices", "users")
    BP_REL_TITLES = {"apps": "Apps", "configurations": "Configurations",
                     "packages": "Packages", "userGroups": "User Groups",
                     "devices": "Devices", "users": "Users"}

    def _is_writable(key: str) -> bool:
        if app.state.demo:
            return True
        org = config.load().get_active()
        return bool(org) and org.capabilities.get(key) != "forbidden"

    def _bp_catalog(c, rel: str) -> dict:
        """id -> display label for a relationship's selectable items."""
        if rel == "apps":
            items, label = cached("apps", lambda cc: cc.apps()), (
                lambda a: a["attributes"].get("name") or a["attributes"].get("bundleId") or a["id"])
        elif rel == "configurations":
            items, label = cached("configurations", lambda cc: cc.configurations()), (
                lambda x: x["attributes"].get("name") or x["id"])
        elif rel == "packages":
            items, label = cached("packages", lambda cc: cc.packages()), (
                lambda x: x["attributes"].get("name") or x["id"])
        elif rel == "userGroups":
            items, label = cached("user_groups", lambda cc: cc.user_groups()), (
                lambda g: g["attributes"].get("name") or g["id"])
        elif rel == "devices":
            items, label = cached("devices", lambda cc: cc.devices()), (
                lambda d: (d["attributes"].get("deviceModel") or "").strip() or d["id"])
        else:  # users
            items, label = cached("users", lambda cc: cc.users()), (
                lambda u: u["attributes"].get("managedAppleAccount")
                or u["attributes"].get("email") or u["id"])
        return {i["id"]: label(i) for i in items}

    def _render_blueprint(request: Request, blueprint_id: str, rel_plan=None):
        c = client()
        body = c.blueprint(blueprint_id, include="apps,packages,configurations,userGroups")
        included: dict[str, list] = {}
        for item in body.get("included", []):
            included.setdefault(item.get("type", "other"), []).append(item)
        rels = []
        if _is_writable("blueprints_write"):  # skip heavy catalogs for read-only roles
            for rel in BP_RELS:
                catalog = _bp_catalog(c, rel)
                current = c.blueprint_relationship_ids(blueprint_id, rel)  # not cached: fresh after writes
                rels.append({"key": rel, "title": BP_REL_TITLES[rel],
                             "current": [{"id": i, "label": catalog.get(i, i)} for i in current]})
        return render(request, "blueprint_detail.html", active="blueprints",
                      blueprint=body.get("data", {}), included=included,
                      blueprint_id=blueprint_id, rels=rels, rel_plan=rel_plan)

    @app.get("/blueprints/{blueprint_id}", response_class=HTMLResponse)
    def blueprint_page(request: Request, blueprint_id: str):
        guard("blueprints")
        return _render_blueprint(request, blueprint_id)

    @app.post("/blueprints/{blueprint_id}/relationships", response_class=HTMLResponse)
    def blueprint_rel_submit(request: Request, blueprint_id: str, rel: str = Form(...),
                             op: str = Form("add"), ids: str = Form(""),
                             mode: str = Form("preview")):
        guard("blueprints")
        if rel not in BP_RELS:
            raise ApiError(400, f"unknown blueprint relationship {rel!r}")
        c = client()
        catalog = _bp_catalog(c, rel)
        current = c.blueprint_relationship_ids(blueprint_id, rel)
        selected = ids.replace(",", " ").replace(";", " ").split()
        p = plan_relationship(op, selected, current, catalog)
        if mode == "execute" and p["changes"]:
            change_ids = [row["id"] for row in p["changes"]]
            if op == "add":
                c.add_blueprint_relationship(blueprint_id, rel, change_ids)
            else:
                c.remove_blueprint_relationship(blueprint_id, rel, change_ids)
            org_key = c.org.client_id
            app.state.cache = {k: v for k, v in app.state.cache.items()
                               if not (k[0] == org_key and k[1] == "blueprints")}
            verb = "Added" if op == "add" else "Removed"
            prep = "to" if op == "add" else "from"
            msg = (f"{verb} {len(change_ids)} {BP_REL_TITLES[rel].lower()} "
                   f"{prep} this blueprint.")
            return RedirectResponse(f"/blueprints/{blueprint_id}?msg={quote(msg)}",
                                    status_code=303)
        return _render_blueprint(request, blueprint_id,
                                 rel_plan={"rel": rel, "title": BP_REL_TITLES[rel],
                                           "op": op, "ids": ids, "plan": p})

    @app.get("/configurations", response_class=HTMLResponse)
    def configurations_page(request: Request):
        guard("configurations")
        configurations = cached("configurations", lambda c: c.configurations())
        return render(request, "configurations.html", active="configurations",
                      configurations=configurations)

    @app.get("/configurations/{configuration_id}", response_class=HTMLResponse)
    def configuration_page(request: Request, configuration_id: str):
        guard("configurations")
        item = cached(f"configuration:{configuration_id}",
                      lambda c: c.configuration(configuration_id))
        attrs = dict(item.get("attributes", {}))
        payload = attrs.pop("customSettingsValues", None)
        import json as _json
        return render(request, "item_detail.html", active="configurations",
                      title=attrs.get("name", configuration_id),
                      back_href="/configurations", back_label="Configurations",
                      plain_attrs=attrs,
                      payload=_json.dumps(payload, indent=2) if payload else "",
                      payload_label="Custom settings payload")

    @app.get("/mdm-enrolled/{device_id}", response_class=HTMLResponse)
    def mdm_enrolled_detail_page(request: Request, device_id: str):
        guard("mdm_enrolled")
        item = cached(f"mdm_enrolled:{device_id}",
                      lambda c: c.mdm_enrolled_device(device_id))
        return render(request, "item_detail.html", active="mdm_enrolled",
                      title=item.get("attributes", {}).get("deviceName", device_id),
                      back_href="/mdm-enrolled", back_label="Apple MDM Enrolled",
                      plain_attrs=item.get("attributes", {}),
                      payload="", payload_label="")

    @app.get("/audit-events", response_class=HTMLResponse)
    def audit_page(request: Request, start: str = "", end: str = "", type: str = ""):
        guard("audit_events")
        now = datetime.now(timezone.utc)
        start = start or (now - timedelta(days=7)).strftime("%Y-%m-%d")
        end = end or now.strftime("%Y-%m-%d")
        events = client().audit_events(f"{start}T00:00:00Z", f"{end}T23:59:59Z", type)
        return render(request, "audit_events.html", active="audit_events",
                      events=events[:MAX_TABLE_ROWS], total=len(events),
                      start=start, end=end, type=type)

    # ---- snapshots & changes ----------------------------------------------

    @app.get("/changes", response_class=HTMLResponse)
    def changes_page(request: Request, old: int = 0, new: int = 0):
        org_key = client().org.client_id
        snaps = history.list_snapshots(org_key)
        by_id = {s["id"]: s for s in snaps}
        delta = old_snap = new_snap = None
        if len(snaps) >= 2:
            new_id = new if new in by_id else snaps[0]["id"]
            old_id = old if old in by_id else snaps[1]["id"]
            old_snap, new_snap = by_id[old_id], by_id[new_id]
            delta = history.diff_snapshots(old_id, new_id)
            for changes in delta.values():
                for group in ("added", "removed", "changed"):
                    for item in changes[group]:
                        item["label"] = history.item_label(item["attributes"])
        return render(request, "changes.html", active="changes",
                      snaps=snaps[:20], total_snaps=len(snaps), delta=delta,
                      old_snap=old_snap, new_snap=new_snap,
                      db_path=str(history.db_path()))

    @app.post("/changes/snapshot")
    def changes_snapshot(applecare: str = Form("")):
        snapshot_id, counts, errors = history.take_snapshot(
            client(), include_applecare=bool(applecare))
        msg = (f"Snapshot #{snapshot_id} saved — "
               + ", ".join(f"{count} {name}" for name, count in counts.items()
                           if name in ("devices", "users", "applecare")))
        if errors:
            msg += f". Skipped: {', '.join(errors)}"
        return RedirectResponse(f"/changes?msg={quote(msg)}", status_code=303)

    # ---- assignment (the only write path) -----------------------------------

    def _assignment_context():
        devices = cached("devices", lambda c: c.devices())
        servers = cached("mdm_servers", lambda c: c.mdm_servers())
        server_ids = cached("server_device_ids", lambda c: _server_ids(c, servers))
        return devices, servers, server_ids

    @app.get("/assign", response_class=HTMLResponse)
    def assign_page(request: Request, serials: str = "", server: str = "",
                    action: str = "assign", prefill: str = ""):
        guard("assign")
        devices, servers, server_ids = _assignment_context()
        if prefill == "unassigned":
            summary = assignment_summary(devices, servers, server_ids)
            serials = "\n".join(summary["unassigned"])
        return render(request, "assign.html", active="assign", servers=servers,
                      serials=serials, server=server, action=action,
                      plan=None, error="")

    @app.post("/assign", response_class=HTMLResponse)
    def assign_submit(request: Request, serials: str = Form(""),
                      server: str = Form(""), action: str = Form("assign"),
                      mode: str = Form("preview")):
        guard("assign")
        c = client()
        devices, servers, server_ids = _assignment_context()
        try:
            p = plan_assignment(serials, action, server, devices, servers, server_ids)
        except ValueError as exc:
            return render(request, "assign.html", active="assign",
                          servers=servers, serials=serials, server=server,
                          action=action, plan=None, error=str(exc))
        if mode != "execute" or not p["moves"]:
            return render(request, "assign.html", active="assign",
                          servers=servers, serials=serials, server=server,
                          action=action, plan=p, error="")
        activity = c.create_device_activity(
            "ASSIGN_DEVICES" if action == "assign" else "UNASSIGN_DEVICES",
            server, [m["serial"] for m in p["moves"]])
        # The org just changed: drop device/assignment caches and force the
        # next loads live so the UI reflects reality, not the snapshot.
        org_key = c.org.client_id
        app.state.cache = {k: v for k, v in app.state.cache.items()
                           if k[0] != org_key
                           or (k[1] not in ("devices", "server_device_ids")
                               and not k[1].startswith("device:"))}
        app.state.force_live[org_key] = time.time()
        verb = "to" if action == "assign" else "from"
        msg = (f"Submitted: {action} {len(p['moves'])} device(s) {verb} "
               f"{p['server_name']}.")
        return RedirectResponse(
            f"/activities/{activity.get('id', '')}?msg={quote(msg)}",
            status_code=303)

    @app.get("/activities/{activity_id}", response_class=HTMLResponse)
    def activity_page(request: Request, activity_id: str):
        guard("assign")
        activity = client().device_activity(activity_id)
        attrs = activity.get("attributes", {}) if activity else {}
        return render(request, "activity.html", active="assign",
                      activity=activity, attrs=attrs,
                      pending=attrs.get("status") == "IN_PROGRESS")

    # ---- coverage report ----------------------------------------------------

    def _coverage_source():
        """(taken_at, applecare items, devices) from the latest snapshot —
        or computed live in demo mode. None when no snapshot has coverage."""
        c = client()
        if c.is_demo:
            devices = c.devices()
            items = []
            for device in devices:
                for cov in c.device_applecare(device["id"]):
                    attrs = dict(cov.get("attributes", {}))
                    attrs["serialNumber"] = device["id"]
                    items.append({"type": "applecare", "id": cov.get("id", ""),
                                  "attributes": attrs})
            return None, items, devices
        found = history.latest_resource(c.org.client_id, "applecare")
        if found is None:
            return None
        snapshot_id, taken_at, items = found
        return taken_at, items, history.snapshot_resource(snapshot_id, "devices")

    @app.get("/reports/coverage", response_class=HTMLResponse)
    def coverage_page(request: Request, days: int = 90):
        guard("coverage")
        days = max(1, min(days, 1825))
        source = _coverage_source()
        if source is None:
            return render(request, "coverage.html", active="coverage",
                          report=None, days=days, taken_at=None, device_index={})
        taken_at, items, devices = source
        report = coverage_report(items, devices, days)
        return render(request, "coverage.html", active="coverage",
                      report=report, days=days, taken_at=taken_at,
                      device_index={d["id"]: d for d in devices})

    def _fleet_age(days_param_years: int):
        devices = cached("devices", lambda c: c.devices())
        source = _coverage_source()
        applecare = source[1] if source is not None else None
        return fleet_age_report(devices, applecare, days_param_years)

    @app.get("/reports/fleet-age", response_class=HTMLResponse)
    def fleet_age_page(request: Request, years: int = 4):
        guard("fleet_age")
        years = max(1, min(years, 10))
        return render(request, "fleet_age.html", active="fleet_age",
                      report=_fleet_age(years), years=years)

    @app.get("/reports/mosyle-os-breakdown", response_class=HTMLResponse)
    def mosyle_os_page(request: Request):
        guard("mosyle_os_breakdown")
        devices = cached("devices", lambda c: c.devices())
        return render(request, "mosyle_os.html", active="mosyle_os_breakdown",
                      report=mosyle_os_breakdown(devices))

    @app.get("/reports/mosyle-stale", response_class=HTMLResponse)
    def mosyle_stale_page(request: Request, days: int = 30):
        guard("mosyle_stale")
        days = max(1, min(days, 365))
        devices = cached("devices", lambda c: c.devices())
        return render(request, "mosyle_stale.html", active="mosyle_stale",
                      report=mosyle_stale_devices(devices, days), days=days)

    def _reconciliation_pair(cfg, abm: str, mosyle: str):
        """Resolve the (abm_slug, mosyle_slug) to compare: an explicit query
        choice if valid, else the active org for its provider, else the first
        org of that provider. Returns None if either provider is absent."""
        apple_orgs = _orgs_by_provider(cfg, "apple")
        mosyle_orgs = _orgs_by_provider(cfg, "mosyle")
        if not apple_orgs or not mosyle_orgs:
            return None
        abm_slugs = [s for s, _ in apple_orgs]
        mosyle_slugs = [s for s, _ in mosyle_orgs]
        abm_slug = abm if abm in abm_slugs else (
            cfg.active_org if cfg.active_org in abm_slugs else abm_slugs[0])
        mosyle_slug = mosyle if mosyle in mosyle_slugs else (
            cfg.active_org if cfg.active_org in mosyle_slugs else mosyle_slugs[0])
        return apple_orgs, mosyle_orgs, abm_slug, mosyle_slug

    def _reconcile(abm_slug: str, mosyle_slug: str) -> dict:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_abm = pool.submit(cached_for, abm_slug, "devices", lambda c: c.devices())
            f_mosyle = pool.submit(cached_for, mosyle_slug, "devices", lambda c: c.devices())
            return reconcile_enrollments(f_abm.result(), f_mosyle.result())

    @app.get("/reports/reconciliation", response_class=HTMLResponse)
    def reconciliation_page(request: Request, abm: str = "", mosyle: str = ""):
        cfg = config.load()
        pair = _reconciliation_pair(cfg, abm, mosyle)
        if pair is None:
            return render(request, "reconciliation.html", active="reconciliation",
                          report=None, apple_orgs=_orgs_by_provider(cfg, "apple"),
                          mosyle_orgs=_orgs_by_provider(cfg, "mosyle"),
                          abm_slug="", mosyle_slug="", abm_name="", mosyle_name="")
        apple_orgs, mosyle_orgs, abm_slug, mosyle_slug = pair
        report = _reconcile(abm_slug, mosyle_slug)
        return render(request, "reconciliation.html", active="reconciliation",
                      report=report, apple_orgs=apple_orgs, mosyle_orgs=mosyle_orgs,
                      abm_slug=abm_slug, mosyle_slug=mosyle_slug,
                      abm_name=dict(apple_orgs)[abm_slug].name,
                      mosyle_name=dict(mosyle_orgs)[mosyle_slug].name)

    # ---- exports ----------------------------------------------------------

    @app.get("/export/{resource}.csv")
    def export_csv(request: Request, resource: str, live: int = 0, days: int = 90,
                   years: int = 4, abm: str = "", mosyle: str = ""):
        if resource == "applecare":
            # ?live=1 costs one Apple call per device. A cross-origin <img>/GET
            # must not be able to start that sweep; require a same-origin
            # request (a real click from abapit's own pages).
            if live and request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
                raise ApiError(403, "Live AppleCare export must be started from abapit itself.")
            return _applecare_csv(live=bool(live))
        if resource == "reconciliation":
            pair = _reconciliation_pair(config.load(), abm, mosyle)
            if pair is None:
                raise ApiError(404, "Configure both an Apple and a Mosyle org "
                                    "to reconcile enrollment.")
            _a, _m, abm_slug, mosyle_slug = pair
            report = _reconcile(abm_slug, mosyle_slug)
            rows = [{"type": "reconciliation", "id": row["serial"], "attributes": row}
                    for bucket in ("abm_only", "both", "mosyle_only")
                    for row in report[bucket]]
            return _csv_response(items_to_csv(rows), "reconciliation.csv")
        if resource == "mosyle-os-breakdown":
            guard("mosyle_os_breakdown")
            report = mosyle_os_breakdown(cached("devices", lambda c: c.devices()))
            rows = [{"type": "osVersion", "id": version,
                     "attributes": {"osVersion": version, "count": count, "percent": pct}}
                    for version, count, pct in report["rows"]]
            return _csv_response(items_to_csv(rows), "mosyle-os-breakdown.csv")
        if resource == "mosyle-stale":
            guard("mosyle_stale")
            days = max(1, min(days, 365))
            report = mosyle_stale_devices(cached("devices", lambda c: c.devices()), days)
            rows = [{"type": "stale", "id": row["serial"], "attributes": row}
                    for row in report["rows"]]
            return _csv_response(items_to_csv(rows), f"mosyle-stale-{days}d.csv")
        if resource == "refresh-candidates":
            guard("fleet_age")
            report = _fleet_age(max(1, min(years, 10)))
            rows = [{"type": "refresh", "id": c["serial"], "attributes": {
                "serialNumber": c["serial"], "deviceModel": c["model"],
                "productFamily": c["family"], "ordered": c["ordered"],
                "ageYears": c["age_years"],
                "activeCoverage": c["covered"]}}
                    for c in report["candidates"]]
            return _csv_response(items_to_csv(rows), "refresh-candidates.csv")
        if resource == "coverage-expiring":
            guard("coverage")
            source = _coverage_source()
            if source is None:
                raise ApiError(404, "No snapshot with AppleCare data yet — "
                                    "take one on the Changes page first.")
            taken_at, items, devices = source
            report = coverage_report(items, devices, max(1, min(days, 1825)))
            rows = [{"type": "coverage", "id": r.get("serialNumber", ""),
                     "attributes": {k: r.get(k) for k in (
                         "serialNumber", "description", "paymentType",
                         "startDateTime", "endDateTime", "days_left", "isRenewable")}}
                    for r in report["expiring"]]
            return _csv_response(items_to_csv(rows), f"coverage-expiring-{days}d.csv")
        if resource not in RESOURCES:
            raise ApiError(404, f"Unknown export {resource!r}")
        method, _ = RESOURCES[resource]
        guard_key = resource.replace("-", "_")
        if guard_key != "mdm_servers":
            guard(guard_key if guard_key != "devices" else "devices")
        items = cached(resource, lambda c: getattr(c, method)())
        return _csv_response(items_to_csv(items), f"{resource}.csv")

    def _applecare_csv(live: bool = False):
        """One row per coverage record. Served from the latest snapshot when
        one exists (instant); otherwise — or with ?live=1 — fetched live,
        one API call per device with bounded concurrency."""
        c = client()
        if not live:
            cached_cov = history.latest_applecare(c.org.client_id)
            if cached_cov is not None:
                taken_at, items = cached_cov
                return _csv_response(
                    items_to_csv(items),
                    f"applecare-{taken_at[:10]}.csv")
        from ..client import fetch_applecare_bulk
        devices = cached("devices", lambda cc: cc.devices())
        rows, _failed = fetch_applecare_bulk(c, devices)
        return _csv_response(items_to_csv(rows), "applecare.csv")

    def _csv_response(body: str, filename: str) -> Response:
        return Response(content=body, media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{filename}"'})

    # ---- settings & actions ------------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        return render(request, "settings.html", active="settings",
                      config_path=str(config.config_path()),
                      suggested_roles=config.SUGGESTED_ROLES)

    @app.post("/settings/orgs")
    def settings_add(name: str = Form(...), scope: str = Form(...),
                     client_id: str = Form(...), key_id: str = Form(...),
                     pem: str = Form(""), key_path: str = Form(""),
                     role: str = Form("")):
        try:
            config.add_org(name=name, scope=scope, client_id=client_id,
                           key_id=key_id, private_key_pem=pem.strip(),
                           private_key_path=key_path.strip(), role=role)
            msg = f"Added org {name!r}. Click Permissions to map what this key's role allows."
        except ValueError as exc:
            msg = f"Could not add org: {exc}"
        return RedirectResponse(f"/settings?msg={quote(msg)}", status_code=303)

    @app.post("/settings/orgs/mosyle")
    def settings_add_mosyle(name: str = Form(...), token: str = Form(""),
                            email: str = Form(""), password: str = Form(""),
                            logs_token: str = Form("")):
        try:
            config.add_org(name=name, provider="mosyle", mosyle_token=token.strip(),
                           mosyle_email=email.strip(), mosyle_password=password,
                           mosyle_logs_token=logs_token.strip())
            msg = (f"Added Mosyle org {name!r}. Click Test to verify the "
                   "credentials, then Use to switch to it.")
        except ValueError as exc:
            msg = f"Could not add Mosyle org: {exc}"
        return RedirectResponse(f"/settings?msg={quote(msg)}", status_code=303)

    @app.get("/settings/orgs/{slug}/edit", response_class=HTMLResponse)
    def settings_edit_form(request: Request, slug: str):
        org = config.load().orgs.get(slug)
        if org is None:
            return RedirectResponse(f"/settings?msg={quote('Org not found.')}", status_code=303)
        return render(request, "org_edit.html", active="settings", slug=slug, eorg=org,
                      suggested_roles=config.SUGGESTED_ROLES)

    @app.post("/settings/orgs/{slug}/edit")
    def settings_edit(slug: str, name: str = Form(""), role: str = Form(""),
                      client_id: str = Form(""), key_id: str = Form(""),
                      pem: str = Form(""), token: str = Form(""),
                      email: str = Form(""), password: str = Form(""),
                      logs_token: str = Form("")):
        try:
            config.edit_org(slug, name=name, role=role, client_id=client_id,
                            key_id=key_id, private_key_pem=pem, mosyle_token=token,
                            mosyle_email=email, mosyle_password=password,
                            mosyle_logs_token=logs_token)
            app.state.clients.pop(slug, None)  # rebuild the client with new creds
            org = config.load().orgs.get(slug)
            if org:  # drop cached data fetched under the old credentials
                app.state.cache = {k: v for k, v in app.state.cache.items()
                                   if k[0] != org.client_id}
            msg = f"Updated {name or slug!r}. Click Test to verify."
        except ValueError as exc:
            msg = f"Could not update org: {exc}"
        return RedirectResponse(f"/settings?msg={quote(msg)}", status_code=303)

    @app.post("/settings/orgs/{slug}/activate")
    def settings_activate(slug: str):
        config.set_active(slug)
        app.state.clients.pop(slug, None)
        return RedirectResponse("/", status_code=303)

    @app.post("/settings/orgs/{slug}/delete")
    def settings_delete(slug: str):
        config.remove_org(slug)
        app.state.clients.pop(slug, None)
        return RedirectResponse(
            f"/settings?msg={quote('Org removed.')}", status_code=303)

    @app.post("/settings/orgs/{slug}/probe", response_class=HTMLResponse)
    def settings_probe(request: Request, slug: str):
        """Empirical permission map for one org's API key."""
        if app.state.demo and slug == "demo":
            probe_client = app.state.demo_client
        else:
            cfg = config.load()
            org = cfg.orgs.get(slug)
            if org is None:
                return RedirectResponse(
                    f"/settings?msg={quote('Org not found.')}", status_code=303)
            # Mint a fresh token: a role edited in ABM moments ago may not be
            # reflected in a cached bearer token.
            if org.provider == "apple":
                token_cache.invalidate(org)
            probe_client = build_client(org)
        results = probe_client.probe_capabilities()
        if not (app.state.demo and slug == "demo"):
            config.update_org_capabilities(
                slug, {r["section"]: r["status"] for r in results})
            # Rebuild this org's cached client so it carries the fresh Org.
            app.state.clients.pop(slug, None)
        return render(request, "probe.html", active="settings",
                      results=results, probed_org=probe_client.org)

    @app.post("/settings/orgs/{slug}/test")
    def settings_test(slug: str):
        cfg = config.load()
        org = cfg.orgs.get(slug)
        if org is None:
            msg = "Org not found."
        else:
            try:
                if org.provider == "apple":
                    token_cache.invalidate(org)
                probe = build_client(org)
                probe.ping()
                probe.close()
                msg = f"✅ {org.name}: authentication and device listing work."
            except (ApiError, AuthError, Exception) as exc:  # show, don't crash
                msg = f"❌ {org.name}: {exc}"
        return RedirectResponse(f"/settings?msg={quote(msg)}", status_code=303)

    @app.post("/refresh")
    def refresh(next: str = Form("/")):
        try:
            key = client().org.client_id
            app.state.cache = {k: v for k, v in app.state.cache.items() if k[0] != key}
            # User explicitly asked for fresh data — bypass snapshot
            # warm-start for the next couple of minutes.
            app.state.force_live[key] = time.time()
        except NoOrgError:
            pass
        if not next.startswith("/") or next.startswith("//"):
            next = "/"
        return RedirectResponse(next, status_code=303)

    return app
