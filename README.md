# abapit — the **A**pple **B**usiness **API** **T**ool

A local web GUI (plus a small CLI) for the **Apple Business Manager** and
**Apple School Manager** APIs, built for Mac admins. Browse your device
inventory, MDM server assignments, AppleCare coverage, users, groups, apps,
blueprints, and audit events — organized by category, with dashboards and
one-click CSV exports.

- **KISS**: one Python package, server-rendered HTML, no database, no JS
  framework, runs entirely on your Mac.
- **Open source, internal-IT-shaped**: not a SaaS. `abapit serve` starts a
  local web app bound to `127.0.0.1` and opens your browser.
- **Plays nice with scripts**: `abapit export devices -o devices.csv` and
  `abapit token` (prints a bearer token for `curl`) fit munki/autopkg-style
  automation.

## Quick start (no credentials needed)

Needs Python 3.10+ (`brew install python` if your Mac doesn't have it).

```sh
git clone https://github.com/goetchstone/abapit && cd abapit
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/abapit serve --demo   # full UI with a fake fleet — kick the tires
```

Or as a one-liner without cloning, if you have pipx:
`pipx install git+https://github.com/goetchstone/abapit`

## Connecting your real org

1. In [Apple Business Manager](https://business.apple.com) (or Apple School
   Manager), sign in as an Administrator and go to **your account name →
   Preferences → API**.
2. Create an **API account** and download the **private key** (a `.pem` file —
   you can only download it once). Note the **Client ID** and **Key ID**.
3. Run `abapit serve`, open **Settings**, and add the org: paste the key (or
   point at the file), enter the Client ID and Key ID, pick Business or
   School, then hit **Test**.

Multiple orgs are supported — add another credential set and switch from the
dropdown in the header. Config lives in `~/.config/abapit/config.json`;
private keys are stored separately in `~/.config/abapit/keys/` with `0600`
permissions.

### How auth works

abapit implements Apple's OAuth client-credentials flow: it signs an ES256
JWT client assertion with your private key (`sub` = client ID, `aud` =
`https://account.apple.com/auth/oauth2/v2/token`), exchanges it at
`https://account.apple.com/auth/oauth2/token` for a one-hour bearer token
(scope `business.api` or `school.api`), and refreshes automatically on expiry
or 401. Nothing is sent anywhere except `account.apple.com` and
`api-business.apple.com` / `api-school.apple.com`.

## What you get

| Category | Contents |
|---|---|
| Dashboard | Fleet counts, devices added per month, product family/status breakdowns, devices per MDM server, **devices not assigned to any MDM**, recent audit events |
| Devices | Searchable inventory, per-device detail incl. **AppleCare/warranty coverage** and assigned MDM server |
| MDM Servers | Device management services with assigned-device lists |
| Assign to MDM | The one write: move devices between MDM services — paste serials (or prefill all unassigned), **dry-run preview** of exactly what changes, explicit confirm, then live tracking of Apple's batch activity |
| Apple MDM Enrolled | Devices enrolled in Apple's built-in MDM |
| Users & User Groups | Managed Apple Accounts, group membership *(Business only)* |
| Apps & Packages | VPP/custom apps and packages *(Business only)* |
| Blueprints & Configurations | Blueprints with their attached apps/packages/configs *(Business only)* |
| Audit Events | Org audit log with date-range and type filters *(Business only)* |
| Changes | Snapshot-to-snapshot diffs: devices added/removed, MDM assignment moves, field-level attribute changes |
| Coverage | AppleCare/warranty expiry report from the latest snapshot — "what expires in 30/60/90/180/365 days" plus devices with no active coverage; instant at any fleet size |
| CSV everywhere | `/export/devices.csv`, `applecare.csv`, users, apps, … and the same via `abapit export` |

Apple School Manager orgs see the device-related sections (that's what
Apple's School API exposes today).

## Snapshots & change tracking

Apple's API only shows the *current* state of your org. Snapshots give you
history: each one stores a full point-in-time copy of your org in a single
SQLite database (`~/.local/share/abapit/history.sqlite`, override with
`$ABAPIT_DATA_DIR`).

```sh
abapit snapshot                  # save current state (add --skip-applecare on huge fleets)
abapit changes                   # what changed between the two latest snapshots
abapit changes --json            # machine-readable, for scripts/alerts
abapit snapshot --keep 26        # retention: keep the newest 26 snapshots
```

The **Changes** page in the GUI shows the same diffs — devices added/removed,
MDM assignment moves (`Intune → Jamf Pro`), status and coverage changes —
and lets you compare any two snapshots. Cron it weekly and you have a fleet
history:

```
0 7 * * 1  /usr/local/bin/abapit snapshot --keep 52
```

Design rule: **stale data is never silent.** Live pages serve live data;
when snapshots exist they also enable **warm start** — on a cold cache the
GUI renders instantly from the latest snapshot with a visible "snapshot data
from <time> — refreshing in the background" banner while live data loads
behind it. That's what makes a 20,000-device org open in milliseconds
instead of a minute. The **Refresh** button always forces a true live fetch.
`applecare.csv` is served from the latest snapshot when one exists (it's the
expensive one-call-per-device report); add `?live=1` to force a fresh pull.

The file is plain SQLite — query it directly with `sqlite3` or
[Datasette](https://datasette.io); `devices_view` and `applecare_view` expose
the common fields as real columns.

## CLI

```sh
abapit serve [--demo] [--port 8866] [--no-browser]
abapit export devices -o devices.csv     # any resource: users, apps, blueprints…
abapit export devices --demo | head      # works against demo data too
abapit snapshot [--skip-applecare] [--keep N]
abapit changes [--json]
abapit assign --server "Jamf Pro" --file serials.txt          # DRY RUN: prints the plan
abapit assign --server "Jamf Pro" --file serials.txt --yes    # executes, tracks to completion
abapit probe                             # empirically map what the key's role allows
abapit token                             # print a bearer token for curl
abapit orgs                              # list configured orgs
```

## Key permissions

Apple has **no per-key scopes and no permissions API** — a key inherits the
role of its API account, set in ABM/ASM under Access Management → Roles. So
abapit maps permissions empirically: the **Permissions** button in Settings
(or `abapit probe`) makes one cheap read per category and a can-never-change-
anything write check, and shows you exactly what the key's role allows.

To run tiered access, create two API accounts in ABM — a read-mostly one for
daily use and a device-manager one for migrations — and add both as org
profiles; switch from the header dropdown.

## Security model

- **Network**: binds `127.0.0.1` only by default. Requests with an
  unrecognized `Host` header are rejected (blocks DNS-rebinding attacks),
  and cross-origin browser POSTs are refused (blocks CSRF against the
  settings/snapshot endpoints from malicious websites). Binding to anything
  other than localhost disables Host checking and prints a loud warning —
  the app deliberately has no login of its own.
- **Credentials**: private keys are stored as separate files under
  `~/.config/abapit/keys/` with `0600` permissions in a `0700` directory;
  the config JSON holds only paths. Keys are validated and canonicalized at
  add time. Bearer tokens live in memory only and are never logged.
- **Data at rest**: the snapshot database (your full inventory) is
  `0600` in a `0700` directory.
- **Egress**: the only hosts ever contacted are `account.apple.com` and
  `api-business.apple.com` / `api-school.apple.com`.
- **Honest limits**: anything running as *your user* can read the key files
  — the same trust model as `~/.ssh`. The API account can read inventory
  and reassign devices between MDM services (the tool's only write);
  revoke/rotate keys any time in Apple Business Manager.

## Notes & limits

- **The only write is device↔MDM assignment**, and it never fires blind:
  every run is planned first (unknown serials and no-ops are filtered out
  and shown), the dry-run preview is the default everywhere, and execution
  requires an explicit confirm (web) or `--yes` (CLI). Everything else is
  read-only. Roadmap: blueprint/configuration CRUD.
- Responses are cached in memory for 5 minutes per org (the **Refresh**
  button clears it) to stay friendly with Apple's rate limits; 429s are
  retried automatically with backoff, honoring `Retry-After`.
- Scale: listings page at 1,000 items per API call, so a 5,000-device org
  cold-loads in seconds and a 20,000-device org in under a minute (then
  it's cached). Tested patterns hold to ~200k items per resource.
- The AppleCare bulk report is one API call per device — fine for hundreds of
  devices, slow for tens of thousands. Take a snapshot and it's instant
  thereafter.
- Keep the server on `127.0.0.1` (the default). It has no login of its own —
  binding it to a network interface would expose your org data.

## Development

```sh
git clone … && cd abapit
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/python -m abapit.cli serve --demo
```

The demo fleet (`abapit/demo.py`) mirrors the real client's interface, so UI
work never needs live credentials. MIT licensed — issues and PRs welcome.
