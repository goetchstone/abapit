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
from pathlib import Path


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

    @property
    def issuer(self) -> str:
        return self.team_id or self.client_id

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
    key_path = keys_dir() / f"{slug}.pem"
    key_path.write_text(pem.strip() + "\n")
    key_path.chmod(0o600)
    return key_path


def add_org(
    name: str,
    scope: str,
    client_id: str,
    key_id: str,
    private_key_pem: str = "",
    private_key_path: str = "",
    team_id: str = "",
) -> str:
    """Add an org profile and make it active if it is the first one."""
    if scope not in ("business", "school"):
        raise ValueError(f"scope must be 'business' or 'school', got {scope!r}")
    if not private_key_pem and not private_key_path:
        raise ValueError("provide either a pasted private key or a path to one")

    if not private_key_pem:
        try:
            private_key_pem = Path(private_key_path).expanduser().read_text()
        except OSError as exc:
            raise ValueError(f"could not read key file: {exc}") from exc

    cfg = load()
    slug = slugify(name)
    base, n = slug, 2
    while slug in cfg.orgs:
        slug = f"{base}-{n}"
        n += 1

    # Always store our own validated, normalized copy with 0600 perms.
    private_key_path = str(save_private_key(slug, normalize_private_key(private_key_pem)))

    cfg.orgs[slug] = Org(
        name=name,
        scope=scope,
        client_id=client_id.strip(),
        key_id=key_id.strip(),
        private_key_path=private_key_path,
        team_id=team_id.strip(),
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
