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

```sh
pipx install abapit        # or: uvx abapit, or pip install in a venv
abapit serve --demo        # full UI with a fake fleet — kick the tires
```

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
| Apple MDM Enrolled | Devices enrolled in Apple's built-in MDM |
| Users & User Groups | Managed Apple Accounts, group membership *(Business only)* |
| Apps & Packages | VPP/custom apps and packages *(Business only)* |
| Blueprints & Configurations | Blueprints with their attached apps/packages/configs *(Business only)* |
| Audit Events | Org audit log with date-range and type filters *(Business only)* |
| Changes | Snapshot-to-snapshot diffs: devices added/removed, MDM assignment moves, field-level attribute changes |
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

Design rule: **live pages stay live.** The database is only read by
explicitly historical features, so the GUI never silently shows stale data.
The one optimization: `applecare.csv` is served instantly from the latest
snapshot when one exists (it's the expensive one-call-per-device report);
add `?live=1` to force a fresh pull.

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
abapit token                             # print a bearer token for curl
abapit orgs                              # list configured orgs
```

## Notes & limits

- **v1 is read-only.** Nothing in this tool can change or break your org.
  Roadmap: device→MDM assignment via `orgDeviceActivities` (with explicit
  confirmations), then blueprint/configuration CRUD.
- Responses are cached in memory for 5 minutes per org (the **Refresh**
  button clears it) to stay friendly with Apple's rate limits.
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
