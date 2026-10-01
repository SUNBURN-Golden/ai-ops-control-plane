"""Program inspector host reads: three read verbs of the host ledger, nothing else.

The read-only program inspector (User decision M7, docs/INSPECTOR.md) reads the host ledger
only through the installed helper, called by sudo from the dedicated non-root account
aiops-inspect-ledger. The helper's own identity check accepts that caller for every verb,
so the OS allowlist .github/control-plane/sudoers-aiops-inspector.example is what limits the
inspector to three reads. The argv builders below use the same anchored regular expressions
as that file, so the tool never even asks for anything else, and it never calls the helper
as root.
"""
from __future__ import annotations

import re
import subprocess
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from control_plane_inspect_core import ACCOUNT_RE, HELPER_RE, InspectError, loads_strict, redact

DEFAULT_HELPER = "/opt/astra/bin/astra-host-control"
DEFAULT_ACCOUNT = "aiops-inspect-ledger"
RUN_AS = "astra-control"
SUDO = "/usr/bin/sudo"
CLEAN_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
MAX_OUTPUT = 4 << 20
MAX_DETAIL = 300

# The argument regexes of sudoers-aiops-inspector.example, character for character. sudo
# matches each against the space-joined arguments; the parity test parses the file and
# compares. Keep both in lock step: a change here without the file is refused by sudo.
SUDOERS_ARG_RULES: Tuple[str, ...] = (
    r"^status --lanes$",
    r"^task-status --repository [A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+ --task [A-Z0-9][A-Z0-9._-]*$",
    r"^materialize-status --program [A-Za-z0-9][A-Za-z0-9._-]* --node [A-Za-z0-9][A-Za-z0-9._-]*$",
)
_RULES = tuple(re.compile(rule) for rule in SUDOERS_ARG_RULES)

# Per-field shapes: the sudoers fragments plus the helper's own length limits
# (control_plane_host.REPOSITORY_RE, SAFE_KEY and task_status's 256-character cap).
# CONTRACT NOTE: the length caps, the no-leading-'-' rule and the '.'/'..' refusal are stricter
# than the sudoers file; every argv they allow still matches it, and option-like values or
# dot-only names are never produced.
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_TASK = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,255}")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

HOST_REASONS = ("HOST_ARGV", "HOST_REFUSED", "HOST_TIMEOUT", "HOST_BUDGET", "HOST_OUTPUT", "HOST_UNAVAILABLE")

Runner = Callable[..., Any]


def _field(value: Any, shape: "re.Pattern[str]", name: str) -> str:
    if not isinstance(value, str) or not shape.fullmatch(value):
        raise InspectError("HOST_ARGV", f"invalid {name}")
    if value.startswith("-") or "/-" in value or any(part in (".", "..") for part in value.split("/")):
        raise InspectError("HOST_ARGV", f"invalid {name}")
    return value


def _allowed(argv: Sequence[str]) -> List[str]:
    """Final check: the joined arguments must match one sudoers rule, as sudo will check."""
    if not argv or any(not isinstance(arg, str) or not arg or any(c.isspace() for c in arg) or "\0" in arg
                       for arg in argv):
        raise InspectError("HOST_ARGV", "argument shape")
    line = " ".join(argv)
    if not any(rule.fullmatch(line) for rule in _RULES):
        raise InspectError("HOST_ARGV", "argv outside the sudoers allowlist")
    return list(argv)


def _detail(text: Any) -> str:
    """One redacted, capped line for an error detail (helper reasons are host text, not ours)."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if not isinstance(text, str):
        return ""
    line = next((part for part in text.splitlines() if part.strip()), "")
    return redact(" ".join(line.split()))[0][:MAX_DETAIL]


class HostReader:
    """Host-ledger reader: ``lanes()``, ``task_status()`` and ``materialize_status()`` only.

    ``runner`` is ``subprocess.run`` or a test fake with the same keyword arguments. Every call
    counts against ``max_calls`` (``self.calls``), whatever its outcome.
    """

    def __init__(self, helper: str = DEFAULT_HELPER, account: str = DEFAULT_ACCOUNT,
                 runner: Optional[Runner] = None, timeout: float = 10, max_calls: int = 200):
        if (not isinstance(helper, str) or len(helper) > 200 or not HELPER_RE.fullmatch(helper)
                or "/../" in helper + "/" or "/./" in helper + "/"):
            raise InspectError("CONFIG", "host_helper must be an absolute path")
        # CONTRACT NOTE: root is refused here too; the inspector never calls the helper as root.
        if not isinstance(account, str) or not ACCOUNT_RE.fullmatch(account) or account == "root":
            raise InspectError("CONFIG", "ledger_account must be a non-root account name")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
            raise InspectError("CONFIG", "host timeout out of range")
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1:
            raise InspectError("CONFIG", "host call budget out of range")
        self.helper = helper
        self.account = account
        self.timeout = timeout
        self.max_calls = max_calls
        self.calls = 0
        self._runner: Runner = runner if runner is not None else subprocess.run

    # ------------------------------------------------------------------ public verbs

    def lanes(self) -> Dict[str, Any]:
        """The host lane board (``status --lanes``)."""
        board = self._call(self._lanes_argv())
        lanes = board.get("lanes")
        if (not isinstance(lanes, list) or type(board.get("active_total")) is not int
                or type(board.get("max_active_sessions")) is not int):
            raise InspectError("HOST_OUTPUT", "host lane board is malformed")
        for entry in lanes:
            if (not isinstance(entry, dict) or not isinstance(entry.get("lane"), str)
                    or not isinstance(entry.get("enabled"), bool) or not isinstance(entry.get("active"), list)
                    or not all(isinstance(row, dict) for row in entry["active"])):
                raise InspectError("HOST_OUTPUT", "host lane entry is malformed")
        return board

    def task_status(self, repository: str, task: str) -> Dict[str, Any]:
        """Every host launch row of one task (``task-status``), oldest first."""
        report = self._call(self._task_status_argv(repository, task))
        rows = report.get("rows")
        if ((report.get("repository"), report.get("task")) != (repository, task) or not isinstance(rows, list)
                or not all(isinstance(row, dict) for row in rows)):
            raise InspectError("HOST_OUTPUT", "host task status is malformed")
        return report

    def materialize_status(self, program: str, node: str) -> Dict[str, Any]:
        """The host materialization record of one plan node; ``status`` may be NOT_FOUND."""
        report = self._call(self._materialize_status_argv(program, node))
        # CONTRACT NOTE: the helper always echoes program and node; a mismatch is malformed output.
        if (not isinstance(report.get("status"), str) or not report["status"]
                or (report.get("program"), report.get("node")) != (program, node)):
            raise InspectError("HOST_OUTPUT", "host materialization status is malformed")
        return report

    # ------------------------------------------------------------------ argv builders (private)

    @staticmethod
    def _lanes_argv() -> List[str]:
        return _allowed(["status", "--lanes"])

    @staticmethod
    def _task_status_argv(repository: Any, task: Any) -> List[str]:
        return _allowed(["task-status", "--repository", _field(repository, _REPOSITORY, "repository"),
                         "--task", _field(task, _TASK, "task")])

    @staticmethod
    def _materialize_status_argv(program: Any, node: Any) -> List[str]:
        return _allowed(["materialize-status", "--program", _field(program, _KEY, "program"),
                         "--node", _field(node, _KEY, "node")])

    # ------------------------------------------------------------------ one helper call

    def _call(self, argv: List[str]) -> Dict[str, Any]:
        argv = _allowed(argv)
        if self.calls >= self.max_calls:
            raise InspectError("HOST_BUDGET", f"more than {self.max_calls} host calls")
        self.calls += 1
        command = [SUDO, "-n", "-u", RUN_AS, self.helper, *argv]
        try:
            # CONTRACT NOTE: cwd="/" as in control_plane.host_call, so the dropped account never
            # depends on the caller's working directory.
            completed = self._runner(command, user=self.account, group=self.account, extra_groups=[],
                                     env=dict(CLEAN_ENV), stdin=subprocess.DEVNULL, capture_output=True,
                                     timeout=self.timeout, check=False, cwd="/")
        except subprocess.TimeoutExpired:
            raise InspectError("HOST_TIMEOUT", f"{argv[0]} exceeded {self.timeout}s") from None
        except (OSError, KeyError, ValueError, subprocess.SubprocessError) as exc:
            raise InspectError("HOST_UNAVAILABLE", type(exc).__name__) from None
        return self._parse(argv[0], completed)

    @staticmethod
    def _parse(verb: str, completed: Any) -> Dict[str, Any]:
        code = getattr(completed, "returncode", None)
        stdout = getattr(completed, "stdout", None)
        if stdout is None:
            stdout = b""
        if isinstance(stdout, str):
            stdout = stdout.encode("utf-8", "surrogatepass")
        if not isinstance(stdout, bytes) or not isinstance(code, int):
            raise InspectError("HOST_OUTPUT", f"{verb}: no result")
        if len(stdout) > MAX_OUTPUT:
            raise InspectError("HOST_OUTPUT", f"{verb}: output over {MAX_OUTPUT} bytes")
        try:
            text = stdout.decode("utf-8")
        except UnicodeDecodeError:
            raise InspectError("HOST_OUTPUT", f"{verb}: output is not UTF-8") from None
        lines = [line for line in text.splitlines() if line.strip()]
        report: Any = None
        if lines:
            try:
                report = loads_strict(lines[-1])
            except ValueError:
                report = None
        if isinstance(report, dict) and report.get("status") == "ERROR":
            raise InspectError("HOST_REFUSED", _detail(report.get("reason")))
        if code != 0:
            # sudo itself refused (no rule, password required) or the helper failed without a result.
            stderr = _detail(getattr(completed, "stderr", ""))
            raise InspectError("HOST_REFUSED", f"exit {code}" + (f": {stderr}" if stderr else ""))
        if not isinstance(report, dict):
            raise InspectError("HOST_OUTPUT", f"{verb}: last line is not a JSON object")
        return report
