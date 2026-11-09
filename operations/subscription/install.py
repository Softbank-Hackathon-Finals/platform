"""Install an operator-reviewed immutable source archive on the designated VM."""

import argparse
import base64
import json
import re
from pathlib import Path

import boto3


def installation(sha, archive_digest, settings):
    if not re.fullmatch("[a-f0-9]{40}", sha) or not re.fullmatch("[a-f0-9]{64}", archive_digest):
        raise ValueError("immutable source and archive digest required")
    if set(settings) != {"secret_arn", "runtime_host_id", "broker_role_arn", "model", "region"}:
        raise ValueError("metadata-only settings required; never include tokens")
    if settings["region"] != "ap-northeast-2":
        raise ValueError("unsupported region")
    settings = {**settings, "source_sha": sha, "worker": "/opt/bravo-subscription/worker",
                "state_dir": "/var/lib/bravo-t11-subscription/state"}
    encoded = base64.b64encode(json.dumps(settings).encode()).decode()
    return f'''set -eu
test "$(systemctl is-active bravo-subscription.service || true)" != active
ROOT=/opt/bravo-subscription
install -d -m 755 "$ROOT" /etc/bravo-subscription
curl -fsSL https://codeload.github.com/Softbank-Hackathon-Finals/platform/tar.gz/{sha} -o "$ROOT/source.tar.gz"
echo '{archive_digest}  /opt/bravo-subscription/source.tar.gz' | sha256sum -c -
tar -xzf "$ROOT/source.tar.gz" -C "$ROOT"
curl -fsSL https://github.com/astral-sh/uv/releases/download/0.10.2/uv-x86_64-unknown-linux-gnu.tar.gz -o "$ROOT/uv.tar.gz"
echo '6aa4576c31f791c0b9d4739e256d07358d45e7535695287fec03cf6839e25512  /opt/bravo-subscription/uv.tar.gz' | sha256sum -c -
tar -xzf "$ROOT/uv.tar.gz" -C "$ROOT"
cd "$ROOT/platform-{sha}/services/agent-worker"
UV_CACHE_DIR="$ROOT/cache" "$ROOT/uv-x86_64-unknown-linux-gnu/uv" sync --frozen --python /usr/bin/python3
chmod -R a-w "$ROOT/platform-{sha}"
ln -sfn "$ROOT/platform-{sha}/services/agent-worker" "$ROOT/worker"
printf '%s' '{encoded}' | base64 -d > /etc/bravo-subscription/settings.json
chmod 644 /etc/bravo-subscription/settings.json
cp "$ROOT/platform-{sha}/operations/subscription/bravo-subscription.service" /etc/systemd/system/bravo-subscription.service
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/bravo-subscription.service
echo '{{"installed":true,"enabled_at_boot":false,"source_sha":"{sha}"}}'
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--instance-id", required=True)
    args = parser.parse_args()
    if not re.fullmatch("i-[a-f0-9]{17}", args.instance_id):
        raise ValueError("exact instance required")
    script = installation(args.source_sha, args.archive_sha256, json.loads(args.settings.read_text()))
    result = boto3.client("ssm", region_name="ap-northeast-2").send_command(
        InstanceIds=[args.instance_id], DocumentName="AWS-RunShellScript",
        Parameters={"commands": [script], "executionTimeout": ["300"]},
        Comment="Install pinned Bravo subscription runtime")
    print(json.dumps({"command_id": result["Command"]["CommandId"], "source_sha": args.source_sha}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"state":"INSTALLATION_REQUEST_FAILED"}')
        raise SystemExit(1) from None
