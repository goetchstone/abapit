"""Command-line interface.

    abapit serve [--demo] [--port N] [--no-browser]   launch the web UI
    abapit export <resource> [-o FILE] [--demo]       dump a resource as CSV
    abapit snapshot [--skip-applecare] [--keep N]     save org state to history.sqlite
    abapit changes [--json]                           diff the two latest snapshots
    abapit assign --server X [--unassign] [--yes]     move devices between MDMs (dry-run by default)
    abapit token [--org SLUG]                         print a bearer token
    abapit orgs                                       list configured orgs
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import webbrowser

from . import __version__, config, history
from .assign import plan as plan_assignment
from .auth import AuthError, request_access_token

EXPORT_RESOURCES = {
    "devices": "devices",
    "mdm-servers": "mdm_servers",
    "mdm-enrolled": "mdm_enrolled_devices",
    "users": "users",
    "user-groups": "user_groups",
    "apps": "apps",
    "packages": "packages",
    "blueprints": "blueprints",
    "configurations": "configurations",
}


def _client(args):
    if getattr(args, "demo", False):
        from .demo import DemoClient
        return DemoClient()
    cfg = config.load()
    slug = getattr(args, "org", "") or cfg.active_org
    org = cfg.orgs.get(slug)
    if org is None:
        sys.exit("No org configured. Run `abapit serve` and add one in Settings, "
                 "or use --demo for fake data.")
    from .client import ApiClient
    return ApiClient(org)


def cmd_serve(args) -> None:
    import uvicorn
    from .web.app import create_app

    # Host-header protection can only enumerate hosts we know; if the user
    # deliberately binds wide, disable it (they get the warning below).
    allowed_hosts = None if args.host in ("127.0.0.1", "localhost") else ["*"]
    app = create_app(demo=args.demo, allowed_hosts=allowed_hosts)
    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    print(f"abapit {__version__} — {url}  (Ctrl-C to stop)")
    if args.host not in ("127.0.0.1", "localhost"):
        print("WARNING: binding beyond localhost exposes your org data and "
              "settings page to the network. Only do this if you know why.")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def cmd_export(args) -> None:
    from .reports import items_to_csv

    client = _client(args)
    items = getattr(client, EXPORT_RESOURCES[args.resource])()
    body = items_to_csv(items)
    if args.output:
        with open(args.output, "w", newline="") as fh:
            fh.write(body)
        print(f"Wrote {len(items)} rows to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(body)


def cmd_snapshot(args) -> None:
    client = _client(args)
    snapshot_id, counts, errors = history.take_snapshot(
        client, include_applecare=not args.skip_applecare,
        progress=lambda message: print(message, file=sys.stderr))
    summary = ", ".join(f"{name}={count}" for name, count in counts.items())
    print(f"Snapshot #{snapshot_id} saved to {history.db_path()} ({summary})")
    for name, error in errors.items():
        print(f"warning: skipped {name}: {error}", file=sys.stderr)
    if args.keep:
        removed = history.prune(client.org.client_id, args.keep)
        if removed:
            print(f"Pruned {removed} old snapshot(s), keeping {args.keep}.",
                  file=sys.stderr)


def cmd_changes(args) -> None:
    client = _client(args)
    snaps = history.list_snapshots(client.org.client_id)
    if len(snaps) < 2:
        sys.exit(f"Need at least two snapshots for {client.org.name} "
                 f"({len(snaps)} found). Run `abapit snapshot` first.")
    new, old = snaps[0], snaps[1]
    delta = history.diff_snapshots(old["id"], new["id"])
    if args.json:
        print(json.dumps({
            "org": client.org.name,
            "old": {"id": old["id"], "taken_at": old["taken_at"]},
            "new": {"id": new["id"], "taken_at": new["taken_at"]},
            "changes": delta,
        }, indent=2))
        return
    print(f"{client.org.name}: snapshot #{old['id']} ({old['taken_at']}) "
          f"→ #{new['id']} ({new['taken_at']})")
    if not delta:
        print("No differences.")
        return
    for resource, changes in delta.items():
        print(f"\n{resource}: +{len(changes['added'])} added, "
              f"-{len(changes['removed'])} removed, "
              f"~{len(changes['changed'])} changed")
        for item in changes["added"]:
            print(f"  + {item['id']}  {history.item_label(item['attributes'])}")
        for item in changes["removed"]:
            print(f"  - {item['id']}  {history.item_label(item['attributes'])}")
        for item in changes["changed"]:
            fields = "; ".join(f"{f['field']}: {f['old']} -> {f['new']}"
                               for f in item["fields"][:6])
            print(f"  ~ {item['id']}  {fields}")


def cmd_assign(args) -> None:
    client = _client(args)
    serial_text = " ".join(args.serial)
    if args.file:
        source = sys.stdin if args.file == "-" else open(args.file)
        serial_text += " " + source.read()
    if not serial_text.strip():
        sys.exit("No serials given — use --serial, or --file (use '-' for stdin).")

    servers = client.mdm_servers()
    target = next(
        (s for s in servers
         if s["id"] == args.server
         or s["attributes"].get("serverName", "").lower() == args.server.lower()),
        None)
    if target is None:
        names = ", ".join(repr(s["attributes"].get("serverName", s["id"]))
                          for s in servers)
        sys.exit(f"No device management service matches {args.server!r}. "
                 f"Known: {names}")

    devices = client.devices()
    server_ids = {s["id"]: client.mdm_server_device_ids(s["id"]) for s in servers}
    action = "unassign" if args.unassign else "assign"
    p = plan_assignment(serial_text, action, target["id"], devices, servers, server_ids)

    for move in p["moves"]:
        after = p["server_name"] if action == "assign" else "(unassigned)"
        print(f"  ~ {move['serial']}  {move['from_name']} -> {after}")
    for noop in p["noops"]:
        print(f"  = {noop['serial']}  skipped: {noop['reason']}")
    for serial in p["unknown"]:
        print(f"  ? {serial}  not found in this org")
    if not p["moves"]:
        print("Nothing to do.")
        return
    if not args.yes:
        print(f"\nDry run: would {action} {len(p['moves'])} device(s) "
              f"{'to' if action == 'assign' else 'from'} {p['server_name']!r}. "
              "Re-run with --yes to execute.")
        return

    activity = client.create_device_activity(
        "ASSIGN_DEVICES" if action == "assign" else "UNASSIGN_DEVICES",
        target["id"], [m["serial"] for m in p["moves"]])
    activity_id = activity.get("id", "")
    print(f"Submitted activity {activity_id}; waiting for Apple…")
    attrs = activity.get("attributes", {})
    for _ in range(60):
        if attrs.get("status") not in ("", None, "IN_PROGRESS"):
            break
        time.sleep(5)
        attrs = client.device_activity(activity_id).get("attributes", {})
    print(f"{attrs.get('status', 'UNKNOWN')} "
          f"({attrs.get('subStatus', '')}) at {attrs.get('completedDateTime', '?')}")
    if attrs.get("downloadUrl"):
        print(f"Result log: {attrs['downloadUrl']}")
    if attrs.get("status") != "COMPLETED":
        sys.exit(1)


def cmd_probe(args) -> None:
    client = _client(args)
    if not getattr(args, "demo", False):
        from .auth import token_cache
        token_cache.invalidate(client.org)  # role edits need a fresh token
    print(f"Key capabilities for {client.org.name} ({client.org.scope}) — "
          "probed empirically; Apple has no permissions API:")
    results = client.probe_capabilities()
    for result in results:
        mark = {"ok": "+", "forbidden": "x"}.get(result["status"], "?")
        print(f"  {mark} {result['capability']:22s} {result['kind']:6s} {result['status']}")
    if not getattr(args, "demo", False):
        cfg = config.load()
        slug = args.org or cfg.active_org
        if slug in cfg.orgs:
            config.update_org_capabilities(
                slug, {r["section"]: r["status"] for r in results})
    print("Permissions come from the API account's role in ABM/ASM "
          "(Access Management > Roles). Edit the role there, or add a second "
          "API account as another org profile for tiered access.")


def cmd_token(args) -> None:
    cfg = config.load()
    slug = args.org or cfg.active_org
    org = cfg.orgs.get(slug)
    if org is None:
        sys.exit("No org configured.")
    try:
        token, _ = request_access_token(org)
    except AuthError as exc:
        sys.exit(str(exc))
    print(token)


def cmd_orgs(args) -> None:
    cfg = config.load()
    if not cfg.orgs:
        print("No orgs configured. Run `abapit serve` and add one in Settings.")
        return
    for slug, org in cfg.orgs.items():
        marker = "*" if slug == cfg.active_org else " "
        role = f"  [{org.role}]" if org.role else ""
        print(f"{marker} {slug:20s} {org.scope:8s} {org.name}{role}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="abapit",
        description="A local web GUI + CLI for the Apple Business/School Manager APIs.")
    parser.add_argument("--version", action="version", version=f"abapit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="launch the web UI")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8866)
    p_serve.add_argument("--demo", action="store_true", help="use fake data, no credentials needed")
    p_serve.add_argument("--no-browser", action="store_true", help="don't open the browser")
    p_serve.set_defaults(func=cmd_serve)

    p_export = sub.add_parser("export", help="export a resource as CSV")
    p_export.add_argument("resource", choices=sorted(EXPORT_RESOURCES))
    p_export.add_argument("-o", "--output", help="write to a file instead of stdout")
    p_export.add_argument("--org", default="", help="org slug (default: active org)")
    p_export.add_argument("--demo", action="store_true")
    p_export.set_defaults(func=cmd_export)

    p_snapshot = sub.add_parser(
        "snapshot", help="save the org's current state to history.sqlite")
    p_snapshot.add_argument("--org", default="", help="org slug (default: active org)")
    p_snapshot.add_argument("--demo", action="store_true")
    p_snapshot.add_argument("--skip-applecare", action="store_true",
                            help="skip per-device AppleCare lookups (faster on big fleets)")
    p_snapshot.add_argument("--keep", type=int, default=0,
                            help="after saving, keep only the newest N snapshots")
    p_snapshot.set_defaults(func=cmd_snapshot)

    p_changes = sub.add_parser(
        "changes", help="show what changed between the two latest snapshots")
    p_changes.add_argument("--org", default="", help="org slug (default: active org)")
    p_changes.add_argument("--demo", action="store_true")
    p_changes.add_argument("--json", action="store_true", help="machine-readable output")
    p_changes.set_defaults(func=cmd_changes)

    p_assign = sub.add_parser(
        "assign", help="assign/unassign devices to an MDM service (dry-run unless --yes)")
    p_assign.add_argument("--server", required=True,
                          help="device management service, by name or id")
    p_assign.add_argument("--serial", action="append", default=[],
                          help="serial number (repeatable)")
    p_assign.add_argument("--file", default="",
                          help="file of serials, '-' for stdin")
    p_assign.add_argument("--unassign", action="store_true",
                          help="unassign from the service instead of assigning")
    p_assign.add_argument("--yes", action="store_true",
                          help="actually execute (default is a dry run)")
    p_assign.add_argument("--org", default="", help="org slug (default: active org)")
    p_assign.add_argument("--demo", action="store_true")
    p_assign.set_defaults(func=cmd_assign)

    p_probe = sub.add_parser(
        "probe", help="empirically map what the API key's role allows")
    p_probe.add_argument("--org", default="", help="org slug (default: active org)")
    p_probe.add_argument("--demo", action="store_true")
    p_probe.set_defaults(func=cmd_probe)

    p_token = sub.add_parser("token", help="print a bearer token (for curl/scripts)")
    p_token.add_argument("--org", default="")
    p_token.set_defaults(func=cmd_token)

    p_orgs = sub.add_parser("orgs", help="list configured orgs")
    p_orgs.set_defaults(func=cmd_orgs)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
