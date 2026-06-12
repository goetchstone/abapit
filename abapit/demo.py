"""Demo client: a seeded fake fleet so the whole UI works without credentials.

Mirrors the public interface of `client.ApiClient`, returning items in the
same JSON:API shape ({"type", "id", "attributes"}). Also serves as fixture
data for contributors.
"""

from __future__ import annotations

import random
import string
from datetime import datetime, timedelta, timezone

from .config import Org

MODELS = [
    # (deviceModel, productFamily, productType, capacities, colors)
    ("MacBook Air 13-inch (M3)", "Mac", "Mac15,12", ["256GB", "512GB"], ["Midnight", "Starlight", "Silver"]),
    ("MacBook Pro 14-inch (M4)", "Mac", "Mac16,1", ["512GB", "1TB"], ["Space Black", "Silver"]),
    ("Mac mini (M4)", "Mac", "Mac16,10", ["256GB", "512GB"], ["Silver"]),
    ("iMac 24-inch (M4)", "Mac", "Mac16,2", ["512GB"], ["Blue", "Green", "Silver"]),
    ("iPhone 16", "iPhone", "iPhone17,3", ["128GB", "256GB"], ["Black", "White", "Teal"]),
    ("iPhone 15", "iPhone", "iPhone15,4", ["128GB"], ["Black", "Blue"]),
    ("iPad Air 11-inch (M2)", "iPad", "iPad14,8", ["128GB", "256GB"], ["Space Gray", "Blue"]),
    ("iPad Pro 13-inch (M4)", "iPad", "iPad16,5", ["256GB", "512GB"], ["Space Black"]),
    ("Apple TV 4K (3rd gen)", "AppleTV", "AppleTV14,1", ["128GB"], ["Black"]),
    ("Apple Vision Pro", "Vision", "RealityDevice14,1", ["512GB"], ["Silver"]),
]

FIRST_NAMES = ["Avery", "Blake", "Casey", "Devon", "Emery", "Finley", "Gray", "Harper",
               "Indra", "Jules", "Kai", "Logan", "Morgan", "Noor", "Oakley", "Parker",
               "Quinn", "Riley", "Sasha", "Tatum", "Uma", "Vesper", "Wren", "Yael"]
LAST_NAMES = ["Anderson", "Brooks", "Chen", "Diaz", "Ellis", "Fontaine", "Garcia",
              "Hughes", "Ito", "Jensen", "Khan", "Lopez", "Murphy", "Nakamura",
              "O'Brien", "Patel", "Quintero", "Rivera", "Singh", "Tran", "Ueda",
              "Vargas", "Williams", "Zhang"]
DEPARTMENTS = ["Engineering", "Design", "IT", "Sales", "Marketing", "Finance", "People"]

APPS = [
    ("Slack", "com.tinyspeck.slackmacgap", "SUPPORTED_OS_MACOS"),
    ("Google Chrome", "com.google.Chrome", "SUPPORTED_OS_MACOS"),
    ("1Password", "com.1password.1password", "SUPPORTED_OS_MACOS"),
    ("Microsoft Word", "com.microsoft.Word", "SUPPORTED_OS_MACOS"),
    ("Microsoft Excel", "com.microsoft.Excel", "SUPPORTED_OS_MACOS"),
    ("Zoom", "us.zoom.xos", "SUPPORTED_OS_MACOS"),
    ("Keynote", "com.apple.iWork.Keynote", "SUPPORTED_OS_MACOS"),
    ("Pages", "com.apple.iWork.Pages", "SUPPORTED_OS_MACOS"),
    ("Acme Field App", "com.acme.field", "SUPPORTED_OS_IOS"),
    ("Mobile Iron Go", "com.mobileiron.go", "SUPPORTED_OS_IOS"),
    ("Notability", "com.gingerlabs.Notability", "SUPPORTED_OS_IOS"),
    ("GoodNotes 6", "com.goodnotesapp.x", "SUPPORTED_OS_IOS"),
]

PACKAGES = ["munkitools-6.6.0.pkg", "Nudge-2.0.0.pkg", "osquery-5.12.pkg",
            "Santa-2024.6.pkg", "AcmePrinterDrivers-3.1.pkg"]

CONFIGURATIONS = [
    ("Corp Wi-Fi", "CUSTOM_SETTING", ["PLATFORM_MACOS", "PLATFORM_IOS"]),
    ("FileVault Enforcement", "CUSTOM_SETTING", ["PLATFORM_MACOS"]),
    ("AirDrop Restrictions", "AIR_DROP", ["PLATFORM_MACOS", "PLATFORM_IOS"]),
    ("Passcode Policy", "CUSTOM_SETTING", ["PLATFORM_IOS"]),
    ("Screen Time Limits", "CUSTOM_SETTING", ["PLATFORM_IOS"]),
    ("Software Update Deferral", "CUSTOM_SETTING", ["PLATFORM_MACOS"]),
]

AUDIT_TYPES = [
    ("DEVICE_ADDED_TO_ORG", "DEVICE_MANAGEMENT"),
    ("DEVICE_ASSIGNED_TO_MDM", "DEVICE_MANAGEMENT"),
    ("DEVICE_UNASSIGNED_FROM_MDM", "DEVICE_MANAGEMENT"),
    ("USER_SIGNED_IN", "ACCOUNTS"),
    ("ROLE_UPDATED", "ACCOUNTS"),
    ("APP_LICENSE_PURCHASED", "CONTENT"),
]


def _serial(rng: random.Random) -> str:
    return "".join(rng.choices(string.ascii_uppercase + string.digits, k=10))


def _mac(rng: random.Random) -> str:
    return ":".join(f"{rng.randrange(256):02X}" for _ in range(6))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DemoClient:
    """Same interface as ApiClient, backed by generated data."""

    is_demo = True

    def __init__(self, device_count: int = 84, seed: int = 42):
        self.org = Org(
            name="Demo Org (fake data)", scope="business",
            client_id="BUSINESSAPI.demo", key_id="demo", private_key_path="",
        )
        rng = random.Random(seed)
        now = datetime.now(timezone.utc)

        self._devices: list[dict] = []
        self._applecare: dict[str, list[dict]] = {}
        for _ in range(device_count):
            model, family, ptype, capacities, colors = rng.choice(MODELS)
            serial = _serial(rng)
            ordered = now - timedelta(days=rng.randrange(20, 1100))
            added = ordered + timedelta(days=rng.randrange(1, 14))
            attrs = {
                "serialNumber": serial,
                "deviceModel": model,
                "productFamily": family,
                "productType": ptype,
                "deviceCapacity": rng.choice(capacities),
                "color": rng.choice(colors),
                "status": "ASSIGNED" if rng.random() < 0.93 else "UNASSIGNED",
                "orderDateTime": _iso(ordered),
                "addedToOrgDateTime": _iso(added),
                "updatedDateTime": _iso(added + timedelta(days=rng.randrange(0, 90))),
                "orderNumber": f"DEMO{rng.randrange(10**7):07d}",
                "partNumber": f"{rng.choice('MZ')}{rng.randrange(10**4):04d}LL/A",
                "purchaseSourceType": rng.choice(["APPLE", "RESELLER"]),
                "purchaseSourceUid": f"{rng.randrange(10**6):06d}",
                "imei": (f"35{rng.randrange(10**13):013d}" if family in ("iPhone", "iPad") else None),
                "meid": None,
                "eid": (f"890{rng.randrange(10**29):029d}" if family == "iPhone" else None),
                "wifiMacAddress": _mac(rng),
                "bluetoothMacAddress": _mac(rng),
                "ethernetMacAddress": (_mac(rng) if family == "Mac" else None),
            }
            self._devices.append({"type": "orgDevices", "id": serial, "attributes": attrs})

            coverage = [{
                "type": "appleCareCoverage", "id": f"LW-{serial}",
                "attributes": {
                    "description": "Limited Warranty", "status":
                        "ACTIVE" if ordered + timedelta(days=365) > now else "EXPIRED",
                    "paymentType": "NONE", "agreementNumber": None,
                    "startDateTime": _iso(ordered),
                    "endDateTime": _iso(ordered + timedelta(days=365)),
                    "isRenewable": False, "isCanceled": False,
                    "contractCancelDateTime": None,
                },
            }]
            if rng.random() < 0.4:
                coverage.append({
                    "type": "appleCareCoverage", "id": f"AC-{serial}",
                    "attributes": {
                        "description": "AppleCare+", "status": "ACTIVE",
                        "paymentType": "SUBSCRIPTION",
                        "agreementNumber": f"{rng.randrange(10**9):09d}",
                        "startDateTime": _iso(added),
                        "endDateTime": _iso(added + timedelta(days=rng.choice([365, 730, 1095]))),
                        "isRenewable": True, "isCanceled": False,
                        "contractCancelDateTime": None,
                    },
                })
            self._applecare[serial] = coverage

        self._servers = [
            {"type": "mdmServers", "id": f"demo-server-{i}", "attributes": {
                "serverName": name, "serverType": stype,
                "createdDateTime": _iso(now - timedelta(days=age)),
                "updatedDateTime": _iso(now - timedelta(days=rng.randrange(1, 30))),
            }}
            for i, (name, stype, age) in enumerate([
                ("Jamf Pro (Production)", "MDM", 900),
                ("Microsoft Intune", "MDM", 500),
                ("Apple Configurator 2", "APPLE_CONFIGURATOR", 1200),
            ])
        ]
        self._assignments: dict[str, list[str]] = {s["id"]: [] for s in self._servers}
        self._assigned_server_of: dict[str, dict] = {}
        weights = [0.6, 0.3, 0.1]
        for device in self._devices:
            if rng.random() < 0.88:
                server = rng.choices(self._servers, weights)[0]
                self._assignments[server["id"]].append(device["id"])
                self._assigned_server_of[device["id"]] = server

        self._users = []
        for i in range(24):
            first, last = FIRST_NAMES[i], LAST_NAMES[i]
            self._users.append({"type": "users", "id": f"demo-user-{i}", "attributes": {
                "firstName": first, "lastName": last, "middleName": None,
                "managedAppleAccount": f"{first.lower()}.{last.lower().replace(chr(39), '')}@example.com",
                "email": f"{first.lower()}.{last.lower().replace(chr(39), '')}@example.com",
                "status": "ACTIVE" if rng.random() < 0.95 else "DEACTIVATED",
                "isExternalUser": False,
                "department": rng.choice(DEPARTMENTS),
                "jobTitle": rng.choice(["Engineer", "Designer", "Manager", "Analyst", "Admin"]),
                "employeeNumber": f"E{1000 + i}",
                "createdDateTime": _iso(now - timedelta(days=rng.randrange(30, 900))),
                "updatedDateTime": _iso(now - timedelta(days=rng.randrange(0, 30))),
            }})

        group_names = ["Engineering", "Design", "IT", "Everyone"]
        self._groups, self._group_members = [], {}
        for i, gname in enumerate(group_names):
            gid = f"demo-group-{i}"
            members = ([u["id"] for u in self._users] if gname == "Everyone"
                       else [u["id"] for u in self._users
                             if u["attributes"]["department"] == gname or rng.random() < 0.1])
            self._group_members[gid] = members
            self._groups.append({"type": "userGroups", "id": gid, "attributes": {
                "name": gname, "type": "STANDARD", "status": "ACTIVE",
                "ouId": f"OU-{i:03d}", "totalMemberCount": len(members),
                "createdDateTime": _iso(now - timedelta(days=600)),
                "updatedDateTime": _iso(now - timedelta(days=rng.randrange(0, 60))),
            }})

        self._apps = [{"type": "apps", "id": f"demo-app-{i}", "attributes": {
            "name": name, "bundleId": bundle, "supportedOS": [oses],
            "version": f"{rng.randrange(1, 30)}.{rng.randrange(10)}.{rng.randrange(10)}",
            "isCustomApp": "acme" in bundle,
            "appStoreUrl": f"https://apps.apple.com/app/{bundle}",
            "websiteUrl": None,
        }} for i, (name, bundle, oses) in enumerate(APPS)]

        self._packages = [{"type": "packages", "id": f"demo-pkg-{i}", "attributes": {
            "name": name, "platform": "PLATFORM_MACOS",
            "createdDateTime": _iso(now - timedelta(days=rng.randrange(10, 400))),
            "updatedDateTime": _iso(now - timedelta(days=rng.randrange(0, 10))),
        }} for i, name in enumerate(PACKAGES)]

        self._configurations = [{"type": "configurations", "id": f"demo-config-{i}", "attributes": {
            "name": name, "type": ctype, "configuredForPlatforms": platforms,
            "customSettingsValues": None,
            "createdDateTime": _iso(now - timedelta(days=rng.randrange(30, 500))),
            "updatedDateTime": _iso(now - timedelta(days=rng.randrange(0, 30))),
        }} for i, (name, ctype, platforms) in enumerate(CONFIGURATIONS)]

        blueprint_defs = [
            ("Standard Mac Build", "Baseline apps and settings for all corporate Macs"),
            ("Kiosk iPad", "Locked-down iPads for the front desk"),
            ("Executive iPhone", "Phones for the leadership team"),
        ]
        self._blueprints = [{"type": "blueprints", "id": f"demo-blueprint-{i}", "attributes": {
            "name": name, "description": desc, "status": "ACTIVE",
            "appLicenseDeficient": i == 1,
            "createdDateTime": _iso(now - timedelta(days=rng.randrange(60, 400))),
            "updatedDateTime": _iso(now - timedelta(days=rng.randrange(0, 45))),
        }} for i, (name, desc) in enumerate(blueprint_defs)]
        self._blueprint_includes = {
            bp["id"]: (rng.sample(self._apps, 4) + rng.sample(self._configurations, 2)
                       + rng.sample(self._packages, 1))
            for bp in self._blueprints
        }

        self._audit_events = []
        for i in range(40):
            etype, category = rng.choice(AUDIT_TYPES)
            user = rng.choice(self._users)["attributes"]
            device = rng.choice(self._devices)
            self._audit_events.append({"type": "auditEvents", "id": f"demo-event-{i}", "attributes": {
                "eventDateTime": _iso(now - timedelta(hours=rng.randrange(1, 14 * 24))),
                "type": etype, "category": category, "outcome": "SUCCESS",
                "actorType": "USER", "actorId": user["employeeNumber"],
                "actorName": f"{user['firstName']} {user['lastName']}",
                "subjectType": "DEVICE" if "DEVICE" in etype else "USER",
                "subjectId": device["id"] if "DEVICE" in etype else user["employeeNumber"],
                "subjectName": (device["attributes"]["deviceModel"]
                                if "DEVICE" in etype else f"{user['firstName']} {user['lastName']}"),
            }})
        self._audit_events.sort(key=lambda e: e["attributes"]["eventDateTime"], reverse=True)

    # -- interface mirror of ApiClient -------------------------------------

    def devices(self) -> list[dict]:
        return self._devices

    def device(self, device_id: str) -> dict:
        return next((d for d in self._devices if d["id"] == device_id), {})

    def device_applecare(self, device_id: str) -> list[dict]:
        return self._applecare.get(device_id, [])

    def device_assigned_server(self, device_id: str) -> dict | None:
        return self._assigned_server_of.get(device_id)

    def mdm_servers(self) -> list[dict]:
        return self._servers

    def mdm_server(self, server_id: str) -> dict:
        return next((s for s in self._servers if s["id"] == server_id), {})

    def mdm_server_device_ids(self, server_id: str) -> list[str]:
        return self._assignments.get(server_id, [])

    def mdm_enrolled_devices(self) -> list[dict]:
        return [{"type": "mdmDevices", "id": d["id"], "attributes": {
            "serialNumber": d["id"],
            "deviceName": f"{d['attributes']['deviceModel']} ({d['id'][:4]})",
            "productFamily": d["attributes"]["productFamily"],
            "enrolledUserId": None,
        }} for d in self._devices[:12]]

    def users(self) -> list[dict]:
        return self._users

    def user(self, user_id: str) -> dict:
        return next((u for u in self._users if u["id"] == user_id), {})

    def user_groups(self) -> list[dict]:
        return self._groups

    def user_group(self, group_id: str) -> dict:
        return next((g for g in self._groups if g["id"] == group_id), {})

    def user_group_member_ids(self, group_id: str) -> list[str]:
        return self._group_members.get(group_id, [])

    def apps(self) -> list[dict]:
        return self._apps

    def packages(self) -> list[dict]:
        return self._packages

    def blueprints(self) -> list[dict]:
        return self._blueprints

    def blueprint(self, blueprint_id: str, include: str = "") -> dict:
        data = next((b for b in self._blueprints if b["id"] == blueprint_id), {})
        return {"data": data, "included": self._blueprint_includes.get(blueprint_id, [])}

    def configurations(self) -> list[dict]:
        return self._configurations

    def audit_events(self, start_iso: str, end_iso: str, event_type: str = "") -> list[dict]:
        events = [e for e in self._audit_events
                  if start_iso <= e["attributes"]["eventDateTime"] <= end_iso]
        if event_type:
            events = [e for e in events if e["attributes"]["type"] == event_type]
        return events

    def close(self) -> None:
        pass
