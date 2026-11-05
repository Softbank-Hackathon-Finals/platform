"""Synthetic local TLS identities; no product credential or network access."""

import ipaddress
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

TEST_CA_PEM = Path(certifi.where()).read_text()


def identity(root, *, correct_name=True, expired=False):
    root.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-broker")])
    name = (x509.IPAddress(ipaddress.ip_address("127.0.0.1")) if correct_name
            else x509.DNSName("different.invalid"))
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(days=2))
            .not_valid_after(datetime.now(UTC) + timedelta(hours=-1 if expired else 1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.SubjectAlternativeName([name]), critical=False)
            .sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    (root / "broker.crt").write_text(pem)
    private = root / "broker.key"
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    private.chmod(0o600)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(root / "broker.crt", private)
    return tls, pem
