"""Org profile configuration.

Profiles live in ~/.config/abapit/config.json (override the directory with
$ABAPIT_CONFIG_DIR). Each org holds the credentials for one Apple Business
Manager or Apple School Manager API account. Private keys are stored as
separate .pem files with 0600 permissions, never inside the JSON.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Common ABM/ASM role names, offered as suggestions only — orgs can define
# custom roles, and the capability probe is always the source of truth.
SUGGESTED_ROLES = (
    "Administrator",
    "IT Administrator",
    "Device Enrollment Manager",
    "People Manager",
    "Content Manager",
    "Custom",
)


def config_dir() -> Path:
    env = os.environ.get("ABAPIT_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "abapit"


def config_path() -> Path:
    return config_dir() / "config.json"


def keys_dir() -> Path:
    return config_dir() / "keys"


@dataclass
class Org:
    name: str
    scope: str  # "business" or "school"
    client_id: str
    key_id: str
    private_key_path: str
    team_id: str = ""  # defaults to client_id when empty
    role: str = ""  # the API account's ABM/ASM role — bookkeeping only
    capabilities: dict = field(default_factory=dict)  # section -> probe status
    probed_at: str = ""
    # "apple" (Business/School Manager) or "mosyle" (Mosyle Business MDM).
    # The Apple credential fields above are unused for mosyle orgs. Mosyle's
    # JWT auth needs the API access token PLUS an admin email/password, which
    # are exchanged at /login for a 24h Bearer token (Basic auth is deprecated).
    provider: str = "apple"
    mosyle_token: str = ""
    mosyle_email: str = ""
    mosyle_password: str = ""
    # Mosyle Logs Stream is a SEPARATE service (businessapilogs.mosyle.com) with
    # its own access token, enabled under Organization > Integrations. Optional.
    mosyle_logs_token: str = ""

    @property
    def issuer(self) -> str:
        return self.team_id or self.client_id

    @property
    def is_mosyle(self) -> bool:
        return self.provider == "mosyle"

    def denied_sections(self) -> set:
        return {section for section, status in self.capabilities.items()
                if status == "forbidden"}

    def private_key(self) -> str:
        return Path(self.private_key_path).expanduser().read_text()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "scope": self.scope,
            "client_id": self.client_id,
            "key_id": self.key_id,
            "private_key_path": self.private_key_path,
            "team_id": self.team_id,
            "role": self.role,
            "capabilities": self.capabilities,
            "probed_at": self.probed_at,
            "provider": self.provider,
            "mosyle_token": self.mosyle_token,
            "mosyle_email": self.mosyle_email,
            "mosyle_password": self.mosyle_password,
            "mosyle_logs_token": self.mosyle_logs_token,
        }


@dataclass
class Config:
    active_org: str = ""
    orgs: dict[str, Org] = field(default_factory=dict)

    def get_active(self) -> Org | None:
        return self.orgs.get(self.active_org)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "org"


def load() -> Config:
    path = config_path()
    if not path.exists():
        return Config()
    raw = json.loads(path.read_text())
    orgs = {key: Org(**value) for key, value in raw.get("orgs", {}).items()}
    return Config(active_org=raw.get("active_org", ""), orgs=orgs)


def save(cfg: Config) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    data = {
        "active_org": cfg.active_org,
        "orgs": {key: org.to_dict() for key, org in cfg.orgs.items()},
    }
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(0o600)


def normalize_private_key(pem: str) -> str:
    """Validate a private key and fix common wrapper problems.

    Keys generated for AxM API accounts sometimes arrive with CRLF line
    endings or a PKCS#8 body mislabeled with SEC1 'EC PRIVATE KEY' headers
    (OpenSSL tolerates the mismatch; the cryptography library does not).
    Returns a canonical PKCS#8 PEM, or raises ValueError if unusable.
    """
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, load_pem_private_key)

    text = pem.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
    candidates = [text]
    if "BEGIN EC PRIVATE KEY" in text:
        candidates.append(text.replace("BEGIN EC PRIVATE KEY", "BEGIN PRIVATE KEY")
                              .replace("END EC PRIVATE KEY", "END PRIVATE KEY"))
    elif "BEGIN PRIVATE KEY" in text:
        candidates.append(text.replace("BEGIN PRIVATE KEY", "BEGIN EC PRIVATE KEY")
                              .replace("END PRIVATE KEY", "END EC PRIVATE KEY"))

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            key = load_pem_private_key(candidate.encode(), password=None)
            return key.private_bytes(
                Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
        except (ValueError, TypeError) as exc:
            last_error = exc
    raise ValueError(
        f"not a usable private key ({last_error}). Expected the PEM private "
        "key generated for your Apple Business/School Manager API account.")


def save_private_key(slug: str, pem: str) -> Path:
    """Write a pasted PEM to the keys dir with restrictive permissions."""
    keys_dir().mkdir(parents=True, exist_ok=True)
    keys_dir().chmod(0o700)
    key_path = keys_dir() / f"{slug}.pem"
    key_path.write_text(pem.strip() + "\n")
    key_path.chmod(0o600)
    return key_path


def update_org_capabilities(slug: str, capabilities: dict) -> None:
    """Persist a probe result so the UI can gate navigation per key."""
    cfg = load()
    org = cfg.orgs.get(slug)
    if org is None:
        return
    org.capabilities = capabilities
    org.probed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    save(cfg)


def _unique_slug(cfg: Config, name: str) -> str:
    slug = slugify(name)
    base, n = slug, 2
    while slug in cfg.orgs:
        slug = f"{base}-{n}"
        n += 1
    return slug


def _assert_unique_client_id(cfg: Config, client_id: str) -> None:
    """The client_id keys the token cache, the request cache, and snapshot
    history — so two orgs sharing one would silently cross-contaminate each
    other's data. Reject a duplicate at add time (e.g. the same ABM API
    account pasted twice, or an Apple org set to a Mosyle org's id)."""
    if any(org.client_id == client_id for org in cfg.orgs.values()):
        raise ValueError(
            f"client ID {client_id!r} is already used by another org. Each "
            "org needs a distinct client ID — it keys the token cache, the "
            "request cache, and snapshot history.")


def edit_org(slug: str, **fields) -> None:
    """Update an existing org's editable fields in place. Blank token/key/
    password values are treated as 'keep current'; other fields are set to
    what's submitted (the edit form pre-fills them, so blanks are intentional)."""
    cfg = load()
    org = cfg.orgs.get(slug)
    if org is None:
        raise ValueError(f"no org named {slug!r}")
    if fields.get("name"):
        org.name = fields["name"].strip()
    if fields.get("role") is not None:
        org.role = fields["role"].strip()
    if org.provider == "mosyle":
        if fields.get("mosyle_token", "").strip():
            org.mosyle_token = fields["mosyle_token"].strip()
        if fields.get("mosyle_email") is not None:
            org.mosyle_email = fields["mosyle_email"].strip()
        if fields.get("mosyle_password"):  # blank = keep current
            org.mosyle_password = fields["mosyle_password"]
        # Secrets are never rendered back into the edit form, so a blank field
        # means "keep current" — never "clear it".
        if fields.get("mosyle_logs_token", "").strip():
            org.mosyle_logs_token = fields["mosyle_logs_token"].strip()
    else:
        client_id = fields.get("client_id", "").strip()
        if client_id and client_id != org.client_id:
            _assert_unique_client_id(cfg, client_id)
            org.client_id = client_id
        if fields.get("key_id", "").strip():
            org.key_id = fields["key_id"].strip()
        if fields.get("team_id") is not None:
            org.team_id = fields["team_id"].strip()
        if fields.get("private_key_pem", "").strip():
            org.private_key_path = str(
                save_private_key(slug, normalize_private_key(fields["private_key_pem"])))
    save(cfg)


def add_org(
    name: str,
    scope: str = "business",
    client_id: str = "",
    key_id: str = "",
    private_key_pem: str = "",
    private_key_path: str = "",
    team_id: str = "",
    role: str = "",
    provider: str = "apple",
    mosyle_token: str = "",
    mosyle_email: str = "",
    mosyle_password: str = "",
    mosyle_logs_token: str = "",
) -> str:
    """Add an org profile and make it active if it is the first one."""
    if provider == "mosyle":
        return _add_mosyle_org(name, mosyle_token, mosyle_email, mosyle_password,
                               role, mosyle_logs_token)
    if provider != "apple":
        raise ValueError(f"provider must be 'apple' or 'mosyle', got {provider!r}")
    if scope not in ("business", "school"):
        raise ValueError(f"scope must be 'business' or 'school', got {scope!r}")
    if not client_id or not key_id:
        raise ValueError("client ID and key ID are required for an Apple org")
    if not private_key_pem and not private_key_path:
        raise ValueError("provide either a pasted private key or a path to one")

    if not private_key_pem:
        try:
            private_key_pem = Path(private_key_path).expanduser().read_text()
        except OSError as exc:
            raise ValueError(f"could not read key file: {exc}") from exc

    cfg = load()
    _assert_unique_client_id(cfg, client_id.strip())
    slug = _unique_slug(cfg, name)

    # Always store our own validated, normalized copy with 0600 perms.
    private_key_path = str(save_private_key(slug, normalize_private_key(private_key_pem)))

    cfg.orgs[slug] = Org(
        name=name,
        scope=scope,
        client_id=client_id.strip(),
        key_id=key_id.strip(),
        private_key_path=private_key_path,
        team_id=team_id.strip(),
        role=role.strip(),
    )
    if not cfg.active_org:
        cfg.active_org = slug
    save(cfg)
    return slug


def _add_mosyle_org(name: str, mosyle_token: str, mosyle_email: str = "",
                    mosyle_password: str = "", role: str = "",
                    mosyle_logs_token: str = "") -> str:
    """Add a Mosyle Business org. No private key — the API access token from
    Mosyle's API Integration profile, plus the admin email/password that
    /login exchanges for a Bearer token. The client_id is synthetic (it only
    needs to be stable and unique, since it keys the cache and history)."""
    if not mosyle_token.strip():
        raise ValueError("provide the Mosyle API access token")
    cfg = load()
    slug = _unique_slug(cfg, name)
    client_id = f"mosyle.{slug}"
    _assert_unique_client_id(cfg, client_id)  # guard against an Apple org squatting it
    cfg.orgs[slug] = Org(
        name=name,
        scope="business",
        provider="mosyle",
        client_id=client_id,
        key_id="",
        private_key_path="",
        mosyle_token=mosyle_token.strip(),
        mosyle_email=mosyle_email.strip(),
        mosyle_password=mosyle_password,
        mosyle_logs_token=mosyle_logs_token.strip(),
        role=role.strip(),
    )
    if not cfg.active_org:
        cfg.active_org = slug
    save(cfg)
    return slug


def remove_org(slug: str) -> None:
    cfg = load()
    org = cfg.orgs.pop(slug, None)
    if org is None:
        return
    # Only delete key files we manage; leave user-supplied paths alone.
    key_path = Path(org.private_key_path).expanduser()
    if key_path.parent == keys_dir() and key_path.exists():
        key_path.unlink()
    if cfg.active_org == slug:
        cfg.active_org = next(iter(cfg.orgs), "")
    save(cfg)


def set_active(slug: str) -> None:
    cfg = load()
    if slug not in cfg.orgs:
        raise KeyError(f"no org named {slug!r}")
    cfg.active_org = slug
    save(cfg)
