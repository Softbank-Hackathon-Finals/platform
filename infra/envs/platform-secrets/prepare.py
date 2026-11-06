"""Fetch the exact reviewed upstream chart and CRDs without registry credentials."""
import argparse
import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path

REVISION = "17453ab48de175b9d00723a08e1af9c3e669b62b"
ARCHIVE_SHA = "40267a0f27c39a43680d7e6c9c0b36ee4aea9ff68f342abd2f03e975e31a8107"
CRDS_SHA = "70268cdd55c5010b1c50d4588289aec216b30acee6d3fa838b6976ad9f089628"
ESO_SHA = "0a6d9e91b1ff50adfdbf24b50b582babfa7c5b7df42b21974ca4b3fb88ee5e34"


def fetch(url, expected):
    with urllib.request.urlopen(url, timeout=60) as response:
        value = response.read(32 * 1024 * 1024 + 1)
    if len(value) > 32 * 1024 * 1024 or hashlib.sha256(value).hexdigest() != expected:
        raise ValueError("upstream_artifact_digest_changed")
    return value


def prepare(output):
    output.mkdir(mode=0o700)
    archive = fetch("https://codeload.github.com/kubebao/openbao-operator/tar.gz/" + REVISION, ARCHIVE_SHA)
    prefix = "openbao-operator-" + REVISION + "/charts/openbao-operator/"
    with tarfile.open(fileobj=io.BytesIO(archive)) as package:
        for member in package.getmembers():
            if not member.name.startswith(prefix) or member.isdir():
                continue
            path = Path(member.name.removeprefix(prefix))
            if not member.isfile() or path.is_absolute() or ".." in path.parts:
                raise ValueError("unexpected_chart_member")
            target = output / "openbao-operator" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(package.extractfile(member).read())
    (output / "crds.yaml").write_bytes(fetch(
        "https://github.com/kubebao/openbao-operator/releases/download/0.6.0/crds.yaml", CRDS_SHA))
    (output / "external-secrets-2.14.0.tgz").write_bytes(fetch(
        "https://github.com/external-secrets/external-secrets/releases/download/helm-chart-2.14.0/external-secrets-2.14.0.tgz", ESO_SHA))
    manifest = {"operator_source": REVISION, "source_sha256": ARCHIVE_SHA,
                "crds_sha256": CRDS_SHA, "eso_chart_sha256": ESO_SHA}
    (output / "sources.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(prepare(parser.parse_args().output)))
