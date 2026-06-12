import stat

import pytest

from abapit import config


@pytest.fixture
def pem(ec_key_pair):
    key_path, _ = ec_key_pair
    return key_path.read_text()


def test_add_load_activate_remove(tmp_path, monkeypatch, pem):
    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path))

    slug = config.add_org(
        name="Acme Corp", scope="business",
        client_id="BUSINESSAPI.abc", key_id="kid-1", private_key_pem=pem)
    assert slug == "acme-corp"

    cfg = config.load()
    assert cfg.active_org == "acme-corp"  # first org becomes active
    org = cfg.orgs["acme-corp"]
    assert org.scope == "business"
    assert org.issuer == "BUSINESSAPI.abc"

    key_path = tmp_path / "keys" / "acme-corp.pem"
    assert "BEGIN PRIVATE KEY" in key_path.read_text()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600

    slug2 = config.add_org(
        name="School District", scope="school",
        client_id="SCHOOLAPI.xyz", key_id="kid-2", private_key_pem=pem)
    config.set_active(slug2)
    assert config.load().active_org == slug2

    config.remove_org(slug2)
    cfg = config.load()
    assert slug2 not in cfg.orgs
    assert cfg.active_org == "acme-corp"
    assert not (tmp_path / "keys" / f"{slug2}.pem").exists()


def test_duplicate_names_get_unique_slugs(tmp_path, monkeypatch, pem):
    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path))
    a = config.add_org(name="Acme", scope="business", client_id="x",
                       key_id="k", private_key_pem=pem)
    b = config.add_org(name="Acme", scope="business", client_id="y",
                       key_id="k", private_key_pem=pem)
    assert a != b and a in config.load().orgs and b in config.load().orgs


def test_key_path_is_copied_and_normalized(tmp_path, monkeypatch, ec_key_pair):
    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path / "cfg"))
    key_path, _ = ec_key_pair
    slug = config.add_org(name="PathOrg", scope="business", client_id="z",
                          key_id="k", private_key_path=str(key_path))
    stored = config.load().orgs[slug].private_key_path
    assert stored != str(key_path)          # our own managed copy
    assert "BEGIN PRIVATE KEY" in config.load().orgs[slug].private_key()


def test_mislabeled_sec1_header_is_fixed(pem):
    """PKCS#8 body wrapped in SEC1 'EC PRIVATE KEY' headers — as seen with
    real AxM keys — must normalize to a loadable PKCS#8 PEM."""
    mislabeled = (pem.replace("BEGIN PRIVATE KEY", "BEGIN EC PRIVATE KEY")
                     .replace("END PRIVATE KEY", "END EC PRIVATE KEY")
                     .replace("\n", "\r\n"))
    fixed = config.normalize_private_key(mislabeled)
    assert "BEGIN PRIVATE KEY" in fixed
    assert "\r" not in fixed
    # round-trips through the strict parser
    assert config.normalize_private_key(fixed) == fixed


def test_garbage_key_is_rejected_at_add_time(tmp_path, monkeypatch):
    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="not a usable private key"):
        config.add_org(name="Bad", scope="business", client_id="x", key_id="k",
                       private_key_pem="-----BEGIN EC PRIVATE KEY-----\nZmFrZQ==\n-----END EC PRIVATE KEY-----")


def test_missing_key_file_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="could not read key file"):
        config.add_org(name="NoFile", scope="business", client_id="x",
                       key_id="k", private_key_path="/nonexistent/key.pem")
