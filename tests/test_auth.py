import base64

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from kalshibot.auth import KalshiSigner, build_message, signing_path


def _pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def ed_key():
    return ed25519.Ed25519PrivateKey.generate()


def test_signing_path_strips_query_and_keeps_prefix():
    url = "https://api.elections.kalshi.com/trade-api/v2/portfolio/orders?status=resting&limit=5"
    assert signing_path(url) == "/trade-api/v2/portfolio/orders"
    assert signing_path("/trade-api/v2/markets?x=1") == "/trade-api/v2/markets"
    assert signing_path("/trade-api/v2/portfolio/balance") == "/trade-api/v2/portfolio/balance"


def test_message_format():
    msg = build_message("1700000000123", "get", "/trade-api/v2/portfolio/balance?x=1")
    assert msg == b"1700000000123GET/trade-api/v2/portfolio/balance"


def test_rsa_pss_signature_verifies(rsa_key):
    s = KalshiSigner("key-123", _pem(rsa_key))
    h = s.headers("POST", "https://demo-api.kalshi.co/trade-api/v2/portfolio/events/orders?a=b",
                  timestamp_ms=1700000000123)
    assert h["KALSHI-ACCESS-KEY"] == "key-123"
    assert h["KALSHI-ACCESS-TIMESTAMP"] == "1700000000123"
    sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
    expected_msg = b"1700000000123POST/trade-api/v2/portfolio/events/orders"
    # salt length = digest length (32 bytes for SHA-256), as Kalshi specifies
    rsa_key.public_key().verify(sig, expected_msg,
                                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                                hashes.SHA256())
    with pytest.raises(InvalidSignature):
        rsa_key.public_key().verify(sig, expected_msg + b"?a=b",
                                    padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                                    hashes.SHA256())
    assert s.key_type == "rsa"


def test_ed25519_signature_verifies(ed_key):
    s = KalshiSigner("k", _pem(ed_key))
    h = s.headers("GET", "/trade-api/v2/portfolio/balance", timestamp_ms=42)
    ed_key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
                               b"42GET/trade-api/v2/portfolio/balance")
    assert s.key_type == "ed25519"


def test_timestamp_is_milliseconds(ed_key):
    s = KalshiSigner("k", _pem(ed_key), clock=lambda: 1700000000.5)
    assert s.headers("GET", "/x")["KALSHI-ACCESS-TIMESTAMP"] == "1700000000500"


def test_repr_hides_secrets(rsa_key):
    s = KalshiSigner("abcdef-secret-id", _pem(rsa_key))
    assert "secret-id" not in repr(s)
    assert "PRIVATE" not in repr(s)


def test_rejects_empty_key_id(ed_key):
    with pytest.raises(ValueError):
        KalshiSigner("", _pem(ed_key))
