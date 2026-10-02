#!/usr/bin/env python3
"""Read-only Mac inventory and native-architecture Linux VM configuration.

This module never installs software, starts a VM, changes sleep settings or
claims that AIOPS is qualified. No account names, serials or credentials in output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess


class HostError(ValueError):
    pass


def architecture(machine):
    aliases = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}
    if machine.lower() not in aliases:
        raise HostError("UNSUPPORTED_ARCHITECTURE")
    return aliases[machine.lower()]


def probe(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def inventory():
    system = platform.system()
    raw_machine = platform.machine()
    translated = probe(["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"]) if system == "Darwin" else None
    # Python running under Rosetta reports x86_64; the physical host is ARM.
    machine = "arm64" if translated == "1" else raw_machine
    try:
        arch = architecture(machine)
    except HostError:
        arch = None
    memory = probe(["/usr/sbin/sysctl", "-n", "hw.memsize"]) if system == "Darwin" else None
    return {
        "schema_version": 1,
        "scope": "HOST_INVENTORY_ONLY",
        "system": system,
        "os_version": platform.mac_ver()[0] if system == "Darwin" else platform.release(),
        "architecture": arch,
        "python_under_rosetta": translated == "1",
        "memory_gib": round(int(memory) / (1024 ** 3), 1) if memory and memory.isdigit() else None,
        "free_disk_gib": round(shutil.disk_usage(Path.home()).free / (1024 ** 3), 1),
        "python_version": platform.python_version(),
        "lima_installed": shutil.which("limactl") is not None,
        "macos_native_aiops_runtime_supported": False,
        "suggested_guest_architecture": arch if system == "Darwin" else None,
        "runtime_qualification": "NOT_CHECKED",
        "notes": ["AIOPS host admission uses Linux /proc and lane isolation.",
                  "A Linux VM is a candidate execution host; this inventory does not authorize execution.",
                  "No recovery page or recovery installer is part of this package."],
    }


def guest_check():
    try:
        release = platform.freedesktop_os_release()
    except (AttributeError, OSError):
        release = {}
    commands = ("python3", "git", "sudo", "setpriv", "setsid", "timeout", "find", "flock", "sha256sum")
    missing = [name for name in commands if shutil.which(name) is None]
    supported = platform.system() == "Linux" and release.get("ID") == "debian" and release.get("VERSION_ID") == "13"
    try:
        arch = architecture(platform.machine())
    except HostError:
        arch = None
    return {"scope": "OS_PREREQUISITES_ONLY", "debian_13": supported, "architecture": arch,
            "proc_available": Path("/proc/self/status").is_file(), "missing_commands": missing,
            "runtime_qualification": "NOT_CHECKED", "lane_qualification": "NOT_CHECKED",
            "model_invoked": False}


def vm_config(image, digest, arch, cpus=4, memory_gib=4, disk_gib=64, system=None, machine=None):
    """Output JSON (also valid YAML); verify a user-selected local Debian cloud image.

    Image architecture/distribution are operator inputs, not inferred from QCOW2.
    Guest checks and installation qualification remain separate.
    """
    if (system or platform.system()) != "Darwin":
        raise HostError("MAC_HOST_REQUIRED")
    host_arch = architecture(machine or inventory()["architecture"] or "unknown")
    guest_arch = architecture(arch)
    if guest_arch != host_arch:
        raise HostError("NATIVE_GUEST_REQUIRED: cross-architecture emulation is not a default")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise HostError("IMAGE_SHA256_REQUIRED")
    for value, lower, upper in ((cpus, 1, 32), (memory_gib, 2, 256), (disk_gib, 16, 4096)):
        if type(value) is not int or not lower <= value <= upper:
            raise HostError("INVALID_VM_RESOURCES")
    path = Path(image).expanduser().absolute()
    # Refuse a symlink at the image boundary; do not trust a stale pre-open stat.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    hasher = hashlib.sha256()
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise HostError("IMAGE_MUST_BE_REGULAR_FILE")
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    if hasher.hexdigest() != digest:
        raise HostError("IMAGE_DIGEST_MISMATCH")
    return {"vmType": "vz" if host_arch == "aarch64" else "qemu", "arch": guest_arch,
            "images": [{"location": str(path), "arch": guest_arch, "digest": "sha256:" + digest}],
            "cpus": cpus, "memory": str(memory_gib) + "GiB", "disk": str(disk_gib) + "GiB",
            "plain": True, "mounts": [], "portForwards": [],
            "containerd": {"system": False, "user": False}, "ssh": {"forwardAgent": False}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("doctor")
    subs.add_parser("guest-check")
    vm = subs.add_parser("vm-config")
    vm.add_argument("--image", required=True)
    vm.add_argument("--image-sha256", required=True)
    vm.add_argument("--arch", choices=("aarch64", "x86_64"), required=True)
    vm.add_argument("--cpus", type=int, default=4)
    vm.add_argument("--memory-gib", type=int, default=4)
    vm.add_argument("--disk-gib", type=int, default=64)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            result = inventory()
        elif args.command == "guest-check":
            result = guest_check()
        else:
            result = vm_config(args.image, args.image_sha256, args.arch, args.cpus, args.memory_gib, args.disk_gib)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (HostError, OSError, ValueError) as exc:
        print(json.dumps({"status": "HOLD", "reason": str(exc) if isinstance(exc, HostError) else type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
