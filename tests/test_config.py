import stat

from abapit import config

PEM = "-----BEGIN EC PRIVATE KEY-----\nfake\n-----END EC PRIVATE KEY-----"


def test_add_load_activate_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path))

    slug = config.add_org(
        name="Acme Corp", scope="business",
        client_id="BUSINESSAPI.abc", key_id="kid-1", private_key_pem=PEM)
    assert slug == "acme-corp"

    cfg = config.load()
    assert cfg.active_org == "acme-corp"  # first org becomes active
    org = cfg.orgs["acme-corp"]
    assert org.scope == "business"
    assert org.issuer == "BUSINESSAPI.abc"

    key_path = tmp_path / "keys" / "acme-corp.pem"
    assert key_path.read_text().strip() == PEM
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600

    slug2 = config.add_org(
        name="School District", scope="school",
        client_id="SCHOOLAPI.xyz", key_id="kid-2", private_key_pem=PEM)
    config.set_active(slug2)
    assert config.load().active_org == slug2

    config.remove_org(slug2)
    cfg = config.load()
    assert slug2 not in cfg.orgs
    assert cfg.active_org == "acme-corp"
    assert not (tmp_path / "keys" / f"{slug2}.pem").exists()


def test_duplicate_names_get_unique_slugs(tmp_path, monkeypatch):
    monkeypatch.setenv("ABAPIT_CONFIG_DIR", str(tmp_path))
    a = config.add_org(name="Acme", scope="business", client_id="x",
                       key_id="k", private_key_pem=PEM)
    b = config.add_org(name="Acme", scope="business", client_id="y",
                       key_id="k", private_key_pem=PEM)
    assert a != b and a in config.load().orgs and b in config.load().orgs
