"""Build the pinned KubeBao release with only the reviewed go-jose security fix."""
import argparse
import hashlib
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

from prepare import ARCHIVE_SHA, REVISION, fetch

CHECKSUMS = (
    "github.com/go-jose/go-jose/v4 v4.1.6 h1:sNRkftxFJxwsILiQukqp5z83QBSmntVwmgRY7AqBDO4=",
    "github.com/go-jose/go-jose/v4 v4.1.6/go.mod h1:x4oUasVrzR7071A4TnHLGSPpNOm2a21K9Kf04k1rs08=",
)
BASE = "gcr.io/distroless/static:nonroot@sha256:516757106de35205efb70b91ba03f6f1298424a0678c603255cf2852a7000087"


def build(output):
    output.mkdir(mode=0o700)
    archive = fetch("https://codeload.github.com/kubebao/openbao-operator/tar.gz/" + REVISION, ARCHIVE_SHA)
    with tarfile.open(fileobj=io.BytesIO(archive)) as package:
        package.extractall(output, filter="data")
    source = output / ("openbao-operator-" + REVISION)
    mod = source / "go.mod"
    text = mod.read_text()
    old = "github.com/go-jose/go-jose/v4 v4.1.4"
    if text.count(old) != 1:
        raise ValueError("reviewed_dependency_source_changed")
    mod.write_text(text.replace(old, "github.com/go-jose/go-jose/v4 v4.1.6"))
    sums = source / "go.sum"
    sums.write_text("\n".join(sorted(set(sums.read_text().splitlines()) | set(CHECKSUMS))) + "\n")
    env = dict(os.environ, GOTOOLCHAIN="go1.27.3", GOMAXPROCS="3", CGO_ENABLED="0")
    def run(args, *, directory=source, environment=env):
        subprocess.run(args, cwd=directory, env=environment, check=True)
    run(["go", "test", "-mod=readonly", "github.com/go-jose/go-jose/v4/..."])
    manifests = []
    for name, entry, package, user in (
        ("operator", "manager", "cmd/main.go", "65532:65532"),
        ("backup", "backup-executor", "./cmd/bao-backup", "1000:1000"),
        ("upgrade", "upgrade-executor", "./cmd/bao-upgrade", "1000:1000"),
    ):
        context = output / name
        context.mkdir()
        binary = context / entry
        run(["go", "build", "-p", "3", "-mod=readonly", "-trimpath", "-buildvcs=false",
             "-ldflags=-buildid=", "-o", str(binary), package],
            environment=dict(env, GOOS="linux", GOARCH="amd64"))
        (context / "Dockerfile").write_text(
            f"FROM {BASE}\nCOPY --chmod=555 {entry} /{entry}\nUSER {user}\n"
            f'LABEL org.opencontainers.image.source="https://github.com/Softbank-Hackathon-Finals/platform" '
            f'org.opencontainers.image.version="0.6.0-bravo.1" '
            f'org.opencontainers.image.revision="{REVISION}+go-jose-4.1.6"\n'
            f'ENTRYPOINT ["/{entry}"]\n')
        (context / "LICENSE").write_bytes((source / "LICENSE").read_bytes())
        # Retain upstream Apache-2.0 attribution in the delivered image.
        with (context / "Dockerfile").open("a") as file:
            file.write("COPY LICENSE /usr/share/licenses/openbao-operator/LICENSE\n")
        image = "localhost/bravo-openbao-" + name + ":0.6.0-bravo.1"
        run(["docker", "buildx", "build", "--platform", "linux/amd64", "--provenance=false",
             "--sbom=false", "--output", "type=oci,dest=" + str(output / (name + ".tar")),
             "-t", image, str(context)])
        manifests.append({"component": name, "source": REVISION,
                          "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "tag": image})
    (output / "build.json").write_text(json.dumps(manifests, indent=2) + "\n")
    return manifests


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(build(parser.parse_args().output.resolve())))
