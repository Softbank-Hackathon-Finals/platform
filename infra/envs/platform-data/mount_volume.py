"""Run as the host operator with a reviewed volume ID on stdin. No credential IO."""
import json
import re
import subprocess
import sys
from pathlib import Path

MOUNT = Path("/var/lib/bravo-platform-data")
UNIT = Path("/etc/systemd/system/var-lib-bravo\\x2dplatform\\x2ddata.mount")


def run(args):
    return subprocess.check_output(args, text=True).strip()


def select_disk(devices, volume_id):
    if not re.fullmatch(r"vol-[0-9a-f]{8,17}", volume_id):
        raise ValueError("invalid_volume_id")
    matches = [d for d in devices if (d.get("serial") or "").replace("-", "") == volume_id.replace("-", "")]
    if len(matches) != 1:
        raise ValueError("volume_identity_ambiguous")
    disk = matches[0]
    if disk.get("children") or disk.get("type") != "disk":
        raise ValueError("partitioned_or_non_disk_target")
    if any(p and p != str(MOUNT) for p in disk.get("mountpoints", [])):
        raise ValueError("disk_has_other_mount")
    return disk


def main():
    settings = json.load(sys.stdin)
    disks = json.loads(run(["lsblk", "-J", "-b", "-o", "NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS,SERIAL"]))["blockdevices"]
    disk = select_disk(disks, settings["volume_id"])
    device = "/dev/" + disk["name"]
    if int(disk["size"]) != 16 * 1024**3:
        raise ValueError("disk_size_mismatch")
    initialized = False
    if not disk["fstype"]:
        if settings.get("initialize_empty_volume") is not True:
            raise ValueError("empty_volume_requires_explicit_initialization")
        signatures = json.loads(run(["wipefs", "--json", "--no-act", device])).get("signatures", [])
        if signatures:
            raise ValueError("existing_signature_refused")
        subprocess.run(["mkfs.ext4", "-q", device], check=True, capture_output=True)
        initialized = True
    elif disk["fstype"] != "ext4":
        raise ValueError("unexpected_filesystem")
    uuid = run(["blkid", "-s", "UUID", "-o", "value", device])
    if not re.fullmatch(r"[0-9a-f-]{36}", uuid):
        raise ValueError("invalid_filesystem_uuid")
    MOUNT.mkdir(mode=0o700, exist_ok=True)
    if not subprocess.run(["mountpoint", "-q", str(MOUNT)], check=False).returncode:
        if run(["findmnt", "-n", "-o", "UUID", "--target", str(MOUNT)]) != uuid:
            raise ValueError("mount_owned_by_other_volume")
    elif any(MOUNT.iterdir()):
        raise ValueError("nonempty_unmounted_directory")
    content = ("# Managed by T7 platform-data; do not repurpose.\n[Unit]\n"
               "Description=Bravo platform database EBS\nBefore=rke2-server.service\n"
               f"[Mount]\nWhat=/dev/disk/by-uuid/{uuid}\nWhere={MOUNT}\nType=ext4\nOptions=defaults,nodev,nosuid\n"
               "[Install]\nWantedBy=multi-user.target\n")
    if UNIT.exists() and UNIT.read_text() != content:
        raise ValueError("mount_unit_conflict")
    UNIT.write_text(content)
    drop = Path("/etc/systemd/system/rke2-server.service.d/20-platform-data.conf")
    drop.parent.mkdir(exist_ok=True)
    expected = f"# Managed by T7 platform-data\n[Unit]\nRequiresMountsFor={MOUNT}\n"
    if drop.exists() and drop.read_text() != expected:
        raise ValueError("service_dropin_conflict")
    drop.write_text(expected)
    subprocess.run(["systemctl", "daemon-reload"], check=True, capture_output=True)
    subprocess.run(["systemctl", "enable", "--now", UNIT.name], check=True, capture_output=True)
    if run(["findmnt", "-n", "-o", "UUID", "--target", str(MOUNT)]) != uuid:
        raise ValueError("mounted_volume_mismatch")
    data = MOUNT / "postgres"
    data.mkdir(mode=0o700, exist_ok=True)
    # Official CNPG image's postgres identity; never recurse through existing data.
    import os
    os.chown(data, 26, 26)
    print(json.dumps({"mounted": True, "initialized": initialized, "root_volume_reused": False,
                      "rke2_restarted": False, "filesystem": "ext4", "size_gib": 16}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
