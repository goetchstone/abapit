"""Command-line interface.

    abapit serve [--demo] [--port N] [--no-browser]   launch the web UI
    abapit export <resource> [-o FILE] [--demo]       dump a resource as CSV
    abapit snapshot [--skip-applecare] [--keep N]     save org state to history.sqlite
    abapit changes [--json]                           diff the two latest snapshots
    abapit token [--org SLUG]                         print a bearer token
    abapit orgs                                       list configured orgs
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser

from . import __version__, config, history
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

    app = create_app(demo=args.demo)
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
        print(f"{marker} {slug:20s} {org.scope:8s} {org.name}")


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

    p_token = sub.add_parser("token", help="print a bearer token (for curl/scripts)")
    p_token.add_argument("--org", default="")
    p_token.set_defaults(func=cmd_token)

    p_orgs = sub.add_parser("orgs", help="list configured orgs")
    p_orgs.set_defaults(func=cmd_orgs)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
