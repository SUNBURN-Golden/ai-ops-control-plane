"""Fixed public-canary probe; no provider credentials, prompts or external network."""
import errno
import json
from pathlib import Path
import socket
import sys
import time


def main():
    lane, raw_workspace, raw_control = sys.argv[1:]
    if lane == "LIFETIME":
        print(json.dumps({"status": "STARTED", "fixed_sleep_seconds": 30}), flush=True)
        time.sleep(30)
        print(json.dumps({"status": "FINISHED"}), flush=True)
        return 0
    if lane != "ISOLATION":
        raise ValueError("not a fixed diagnostic")
    workspace, control = Path(raw_workspace), Path(raw_control)
    checks = {}
    own = workspace / "own-canary"
    own.write_text("PUBLIC WORKSPACE CANARY\n")
    checks["own_workspace_read_write"] = own.read_text() == "PUBLIC WORKSPACE CANARY\n"

    def denied(action):
        try:
            action()
        except OSError as exc:
            return exc.errno in {errno.EPERM, errno.EACCES}
        return False

    checks["control_read_denied"] = denied(control.read_bytes)
    checks["control_write_denied"] = denied(lambda: control.open("ab").close())
    other = workspace.parent / "other-task-canary"
    checks["other_task_read_denied"] = denied(other.read_bytes)
    checks["other_task_write_denied"] = denied(lambda: other.open("ab").close())
    link = workspace / "escape-link"
    link.symlink_to(control)
    checks["symlink_escape_denied"] = denied(link.read_bytes)
    with socket.socket() as client:
        client.settimeout(1)
        # ECONNREFUSED is not accepted as evidence of network denial.
        checks["loopback_denied"] = denied(lambda: client.connect(("127.0.0.1", 9)))
    print(json.dumps({"status": "PASS" if all(checks.values()) else "FAIL", "checks": checks}), flush=True)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
