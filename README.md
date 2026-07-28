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

> **⚠️ Use at your own risk.** abapit is a community tool, not affiliated
> with or endorsed by Apple. Most of it is read-only, but the assignment
> feature **modifies your organization** (device↔MDM moves affect
> enrollment). Every write is gated behind a dry-run preview and explicit
> confirmation — review previews carefully. Provided as-is, without
> warranty (MIT).

## Quick start (no credentials needed)

Needs Python 3.10+ (`brew install python` if your Mac doesn't have it).

```sh
git clone https://github.com/goetchstone/abapit && cd abapit
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/abapit serve --demo   # full UI with a fake fleet — kick the tires
```

Or as a one-liner without cloning, if you have pipx:
`pipx install git+https://github.com/goetchstone/abapit`

## Run it like an app (no terminal)

It's a web app under the hood, but nobody should have to babysit a terminal:

```sh
abapit install-app
```

That registers a login service (abapit runs in the background on
`127.0.0.1:8866`, restarts if it crashes, logs to `~/Library/Logs/abapit.log`)
and creates **`~/Applications/abapit.app`** — click it like any Mac app, drag
it to your Dock. For a standalone window with its own icon, open
`http://127.0.0.1:8866` in Safari and choose **File → Add to Dock**.
After pulling new code, run `abapit restart-app` so the service loads it.
`abapit uninstall-app` removes both.

`install-app` reuses whatever Python you ran it with (it works against the
3.9 that ships with macOS Command Line Tools — abapit supports 3.9+).

### Self-contained app (bundled Python)

To get an `abapit.app` that depends on **no** system Python — so it runs on a
clean Mac, and isn't tied to Apple's aging 3.9 — build a bundle with its own
CPython:

```sh
scripts/build_app.sh                 # -> ~/Applications/abapit.app + login service
APP_DEST=./dist scripts/build_app.sh # -> ./dist/abapit.app, no login service
```

It pulls a relocatable CPython from
[python-build-standalone](https://github.com/astral-sh/python-build-standalone),
installs abapit + deps into it, assembles the `.app`, and points the login
service at the bundled interpreter (~110 MB, all in
`Contents/Resources/python`). Run it once per architecture you ship
(`arm64`, `x86_64`). The bundle is **not** code-signed — fine for the machine
that built it, but to distribute it to other Macs, codesign it with a
Developer ID and notarize it, or Gatekeeper will block it.

## Logging

Every Apple API call is logged with status and latency, along with token
mints, rate-limit backoffs, and background-refresh failures:

```
2026-06-12 10:23:34 INFO GET https://api-business.apple.com/v1/orgDevices -> 200 (861 ms)
```

With the login service, logs land in `~/Library/Logs/abapit.log`; in a
terminal they go to stderr. Set `ABAPIT_LOG=debug` (or `warning`) to change
verbosity. If you ever wonder whether abapit is hammering Apple:
`grep minting ~/Library/Logs/abapit.log` — you should see at most one per
org per hour.

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
| Fleet Age | Refresh planning: age distribution from order dates, devices older than N years, cross-referenced with coverage ("old **and** uncovered — replace first"), CSV for the budget meeting |
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
  and cross-origin browser POSTs are refused (blocks CSRF). The origin check
  compares the **full** origin including port — another app on a different
  loopback port is a different origin, and browsers label such requests
  `same-site`, so those are rejected too. Binding beyond localhost keeps Host
  checking on (restricted to this machine's names) and prints a loud warning —
  the app deliberately has no login of its own.
- **Credentials**: Apple private keys are stored as separate files under
  `~/.config/abapit/keys/` with `0600` permissions in a `0700` directory;
  they are validated and canonicalized at add time. **Mosyle credentials
  (API access token, admin email + password, and the optional Logs Stream
  token) are stored in plaintext in `~/.config/abapit/config.json` (`0600`)** —
  Mosyle's API requires the password to mint its 24-hour bearer. Apple bearer
  tokens are cached in `~/.config/abapit/tokens.json` (`0600`) so separate
  processes share one token per hour. No secret is ever logged, echoed back
  into a settings form, or placed in a URL.
- **Data at rest**: the snapshot database (your full inventory) and the Mosyle
  Logs Stream store are `0600` in `0700` directories.
- **Egress**: the only hosts ever contacted are `account.apple.com`,
  `api-business.apple.com` / `api-school.apple.com`, and — for a Mosyle org —
  `businessapi.mosyle.com` plus `businessapilogs.mosyle.com`.
- **Honest limits**: anything running as *your user* can read the config and
  key files — the same trust model as `~/.ssh`; secrets are not in the
  Keychain. The Apple API account can read inventory, reassign devices, and
  (with a permitted role) manage blueprints; revoke/rotate keys any time in
  Apple Business Manager, and rotate the Mosyle token/password in Mosyle.

## Notes & limits

- **Writes never fire blind**: device↔MDM assignment and blueprint membership
  changes are planned first (unknowns and no-ops are filtered out and shown),
  the dry-run preview is the default everywhere, and execution requires an
  explicit confirm (web) or `--yes` (CLI). The **Permissions** probe maps what
  your API account's role actually allows and hides writes it can't do.
  Mosyle is **read-only** — abapit never writes to your MDM. Roadmap:
  configuration and MDM-server CRUD.
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
