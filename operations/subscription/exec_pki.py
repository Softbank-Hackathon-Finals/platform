"""Issue the T42 exec channel certificates: broker client and T15 runner server, two private CAs.

    python operations/subscription/exec_pki.py OUT_DIR --runner-dns rehearsal-exec.bravo-rehearsal-control.svc

Writes PEM files into a new 0700 directory (keys 0600) and prints only SHA-256 fingerprints.
Two separate CAs, as the contract requires: the runner trusts only the broker CA, the broker
trusts only the runner CA. No system roots are involved. Load the files into platform OpenBao
and delete the directory; nothing here is committed or logged.
"""

import argparse
import datetime
import ipaddress
import json
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

BROKER_NAME = "bravo-broker"  # the CN the runner pins (subject_common_name)


def _name(common):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


def _builder(subject, issuer, key, days):
    now = datetime.datetime.now(datetime.UTC)
    return (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=days)))


def _ca(common, days):
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (_builder(_name(common), _name(common), key, days)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                         crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    return key, cert


def _leaf(common, ca_key, ca_cert, days, *, server_dns=None):
    key = ec.generate_private_key(ec.SECP256R1())
    usage = ExtendedKeyUsageOID.SERVER_AUTH if server_dns else ExtendedKeyUsageOID.CLIENT_AUTH
    builder = (_builder(_name(common), ca_cert.subject, key, days)
               .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
               .add_extension(x509.ExtendedKeyUsage([usage]), critical=False))
    if server_dns:
        names = []
        for name in server_dns:
            try:
                names.append(x509.IPAddress(ipaddress.ip_address(name)))
            except ValueError:
                names.append(x509.DNSName(name))
        builder = builder.add_extension(x509.SubjectAlternativeName(names), critical=False)
    return key, builder.sign(ca_key, hashes.SHA256())


def issue(out: Path, runner_dns: list[str], days: int = 30) -> dict:
    if not runner_dns or not 1 <= days <= 90:
        raise ValueError("runner DNS name and 1-90 day validity required")
    out.mkdir(mode=0o700)  # must be new; never overwrite an earlier issuance
    broker_ca_key, broker_ca = _ca("bravo-exec-broker-ca", days)
    runner_ca_key, runner_ca = _ca("bravo-exec-runner-ca", days)
    broker_key, broker = _leaf(BROKER_NAME, broker_ca_key, broker_ca, days)
    runner_key, runner = _leaf(runner_dns[0], runner_ca_key, runner_ca, days, server_dns=runner_dns)
    files = {"broker-ca.crt": broker_ca, "runner-ca.crt": runner_ca, "broker.crt": broker, "runner.crt": runner,
             "broker.key": broker_key, "runner.key": runner_key}
    fingerprints = {}
    for name, item in files.items():
        if name.endswith(".key"):
            data = item.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption())
        else:
            data = item.public_bytes(serialization.Encoding.PEM)
            fingerprints[name] = item.fingerprint(hashes.SHA256()).hex()
        fd = os.open(out / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    # CA keys are not kept: rotation means a new issuance, not re-signing.
    return {"expires_days": days, "broker_name": BROKER_NAME, "runner_dns": runner_dns,
            "sha256": fingerprints}


def issue_docker(out: Path, server_names: list[str], days: int = 30) -> dict:
    """Agent sandbox session VM: dockerd server cert (IP/DNS SANs) and the broker's client cert.

    Server files go to SSM SecureString (the VM reads them at boot); client files to the broker's
    sandbox_docker DOCKER_CERT_PATH as ca.pem / cert.pem / key.pem (the Docker client layout).
    """
    if not server_names or not 1 <= days <= 90:
        raise ValueError("server name and 1-90 day validity required")
    out.mkdir(mode=0o700)
    ca_key, ca = _ca("bravo-agent-sandbox-docker-ca", days)
    server_key, server = _leaf(server_names[0], ca_key, ca, days, server_dns=server_names)
    client_key, client = _leaf(BROKER_NAME, ca_key, ca, days)
    files = {"ca.pem": ca, "server-cert.pem": server, "server-key.pem": server_key,
             "cert.pem": client, "key.pem": client_key}
    fingerprints = {}
    for name, item in files.items():
        if name.endswith("key.pem"):
            data = item.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption())
        else:
            data = item.public_bytes(serialization.Encoding.PEM)
            fingerprints[name] = item.fingerprint(hashes.SHA256()).hex()
        fd = os.open(out / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    return {"expires_days": days, "server_names": server_names, "sha256": fingerprints}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out", type=Path)
    parser.add_argument("--runner-dns", action="append")
    parser.add_argument("--docker-server", action="append",
                        help="issue the sandbox session VM Docker TLS set instead (IP or DNS)")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args(argv)
    if bool(args.runner_dns) == bool(args.docker_server):
        parser.error("choose exactly one of --runner-dns or --docker-server")
    summary = (issue_docker(args.out, args.docker_server, args.days) if args.docker_server
               else issue(args.out, args.runner_dns, args.days))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
