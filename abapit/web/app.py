"""FastAPI app: server-rendered pages over the Apple Business/School APIs.

Everything is synchronous and simple: routes call the (cached) API client,
hand plain dicts to Jinja templates, and return HTML. No database, no JS
framework. Collection responses are cached in memory for five minutes per
org so casual browsing doesn't hammer Apple's rate limits.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import config, history
from ..auth import AuthError, token_cache
from ..client import ApiClient, ApiError, sections_for
from ..demo import DemoClient
from ..reports import (assignment_summary, device_stats, items_to_csv,
                       items_to_rows, parse_iso)

CACHE_TTL = 300  # seconds
MAX_TABLE_ROWS = 500

NAV = [
    ("Overview", [("dashboard", "/", "Dashboard")]),
    ("Devices", [
        ("devices", "/devices", "Devices"),
        ("mdm_servers", "/mdm-servers", "MDM Servers"),
        ("mdm_enrolled", "/mdm-enrolled", "Apple MDM Enrolled"),
    ]),
    ("People", [
        ("users", "/users", "Users"),
        ("user_groups", "/user-groups", "User Groups"),
    ]),
    ("Content", [
        ("apps", "/apps", "Apps"),
        ("packages", "/packages", "Packages"),
        ("blueprints", "/blueprints", "Blueprints"),
        ("configurations", "/configurations", "Configurations"),
    ]),
    ("Activity", [
        ("audit_events", "/audit-events", "Audit Events"),
        ("changes", "/changes", "Changes"),
    ]),
]

# section key -> (client method name, page title) for generic listings/exports
RESOURCES = {
    "devices": ("devices", "Devices"),
    "mdm-servers": ("mdm_servers", "MDM Servers"),
    "mdm-enrolled": ("mdm_enrolled_devices", "Apple MDM Enrolled Devices"),
    "users": ("users", "Users"),
    "user-groups": ("user_groups", "User Groups"),
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


def create_app(demo: bool = False) -> FastAPI:
    app = FastAPI(title="abapit", docs_url=None, redoc_url=None)
    base = Path(__file__).parent
    app.mount("/static", StaticFiles(directory=base / "static"), name="static")
    templates = Jinja2Templates(directory=base / "templates")
    templates.env.filters["dt"] = fmt_date
    templates.env.filters["dtt"] = lambda v: fmt_date(v, with_time=True)

    app.state.demo = demo
    app.state.demo_client = DemoClient() if demo else None
    app.state.clients = {}          # org slug -> ApiClient
    app.state.cache = {}            # (org key, name) -> (timestamp, value)

    # ---- helpers ---------------------------------------------------------

    def client():
        if app.state.demo:
            return app.state.demo_client
        cfg = config.load()
        org = cfg.get_active()
        if org is None:
            raise NoOrgError()
        if cfg.active_org not in app.state.clients:
            app.state.clients[cfg.active_org] = ApiClient(org)
        return app.state.clients[cfg.active_org]

    def cached(name: str, fetch, force: bool = False):
        c = client()
        key = (c.org.client_id, name)
        hit = app.state.cache.get(key)
        if not force and hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        value = fetch(c)
        app.state.cache[key] = (time.time(), value)
        return value

    def render(request: Request, template: str, active: str = "", **ctx):
        c = None
        try:
            c = client()
        except NoOrgError:
            pass
        cfg = config.load() if not app.state.demo else None
        scope = c.org.scope if c else "business"
        allowed = set(sections_for(scope))
        nav = [(group, [item for item in items if item[0] in allowed or item[0] == "dashboard"])
               for group, items in NAV]
        nav = [(group, items) for group, items in nav if items]
        return templates.TemplateResponse(request, template, {
            "active": active,
            "nav": nav,
            "demo": app.state.demo,
            "org": c.org if c else None,
            "orgs": cfg.orgs if cfg else {},
            "active_org": cfg.active_org if cfg else "demo",
            "msg": request.query_params.get("msg", ""),
            "allowed": allowed,
            **ctx,
        })

    def guard(section: str):
        """Redirect to the dashboard if this section isn't available for the
        active org's scope (e.g. users on an Apple School Manager org)."""
        if section not in sections_for(client().org.scope):
            scope = client().org.scope
            raise ApiError(404, f"The {section} section is not available for "
                                f"{scope} orgs.")

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

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        devices = cached("devices", lambda c: c.devices())
        servers = cached("mdm_servers", lambda c: c.mdm_servers())
        server_ids = cached("server_device_ids", lambda c: {
            s["id"]: c.mdm_server_device_ids(s["id"]) for s in servers})
        stats = device_stats(devices)
        assignment = assignment_summary(devices, servers, server_ids)
        events = []
        if "audit_events" in sections_for(client().org.scope):
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
        c = client()
        device = c.device(device_id)
        coverage = c.device_applecare(device_id)
        server = c.device_assigned_server(device_id)
        return render(request, "device_detail.html", active="devices",
                      device=device, coverage=coverage, server=server)

    @app.get("/mdm-servers", response_class=HTMLResponse)
    def mdm_servers_page(request: Request):
        servers = cached("mdm_servers", lambda c: c.mdm_servers())
        server_ids = cached("server_device_ids", lambda c: {
            s["id"]: c.mdm_server_device_ids(s["id"]) for s in servers})
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
        return render(request, "users.html", active="users",
                      users=rows[:MAX_TABLE_ROWS], q=q, total=len(users))

    @app.get("/user-groups", response_class=HTMLResponse)
    def groups_page(request: Request):
        guard("user_groups")
        groups = cached("user_groups", lambda c: c.user_groups())
        return render(request, "user_groups.html", active="user_groups", groups=groups)

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

    @app.get("/blueprints/{blueprint_id}", response_class=HTMLResponse)
    def blueprint_page(request: Request, blueprint_id: str):
        guard("blueprints")
        body = client().blueprint(
            blueprint_id, include="apps,packages,configurations,userGroups")
        included: dict[str, list] = {}
        for item in body.get("included", []):
            included.setdefault(item.get("type", "other"), []).append(item)
        return render(request, "blueprint_detail.html", active="blueprints",
                      blueprint=body.get("data", {}), included=included)

    @app.get("/configurations", response_class=HTMLResponse)
    def configurations_page(request: Request):
        guard("configurations")
        configurations = cached("configurations", lambda c: c.configurations())
        return render(request, "configurations.html", active="configurations",
                      configurations=configurations)

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

    # ---- exports ----------------------------------------------------------

    @app.get("/export/{resource}.csv")
    def export_csv(resource: str, live: int = 0):
        if resource == "applecare":
            return _applecare_csv(live=bool(live))
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
        devices = cached("devices", lambda cc: cc.devices())
        rows = []
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = pool.map(
                lambda d: (d, c.device_applecare(d["id"])), devices)
            for device, coverages in results:
                for cov in coverages:
                    a = cov.get("attributes", {})
                    rows.append({
                        "type": "applecare", "id": cov.get("id", ""),
                        "attributes": {
                            "serialNumber": device["id"],
                            "deviceModel": device["attributes"].get("deviceModel"),
                            "description": a.get("description"),
                            "status": a.get("status"),
                            "paymentType": a.get("paymentType"),
                            "startDateTime": a.get("startDateTime"),
                            "endDateTime": a.get("endDateTime"),
                            "isRenewable": a.get("isRenewable"),
                        }})
        return _csv_response(items_to_csv(rows), "applecare.csv")

    def _csv_response(body: str, filename: str) -> Response:
        return Response(content=body, media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{filename}"'})

    # ---- settings & actions ------------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        return render(request, "settings.html", active="settings",
                      config_path=str(config.config_path()))

    @app.post("/settings/orgs")
    def settings_add(name: str = Form(...), scope: str = Form(...),
                     client_id: str = Form(...), key_id: str = Form(...),
                     pem: str = Form(""), key_path: str = Form("")):
        try:
            config.add_org(name=name, scope=scope, client_id=client_id,
                           key_id=key_id, private_key_pem=pem.strip(),
                           private_key_path=key_path.strip())
            msg = f"Added org {name!r}."
        except ValueError as exc:
            msg = f"Could not add org: {exc}"
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

    @app.post("/settings/orgs/{slug}/test")
    def settings_test(slug: str):
        cfg = config.load()
        org = cfg.orgs.get(slug)
        if org is None:
            msg = "Org not found."
        else:
            try:
                token_cache.invalidate(org)
                probe = ApiClient(org)
                probe.get("orgDevices", {"limit": 1})
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
        except NoOrgError:
            pass
        if not next.startswith("/") or next.startswith("//"):
            next = "/"
        return RedirectResponse(next, status_code=303)

    return app
