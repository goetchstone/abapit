import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from abapit.config import Org


@pytest.fixture
def ec_key_pair(tmp_path):
    """A P-256 key pair on disk, as Apple's API accounts use."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(pem)
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return key_path, public_pem


@pytest.fixture
def org(ec_key_pair):
    key_path, _ = ec_key_pair
    return Org(
        name="Test Org",
        scope="business",
        client_id="BUSINESSAPI.test-client",
        key_id="test-key-id",
        private_key_path=str(key_path),
    )
