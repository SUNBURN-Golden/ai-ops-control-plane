"""Immutable program admissions, results and terminal-failure settlements.

The replaceable legacy .json receipt is only a projection after an admission
has been journaled. Deleting it or changing an exact request binding cannot
remove the journal's operation-scope execution fence.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

import control_plane as cp


class ReceiptError(RuntimeError):
    pass


HEX = re.compile(r"[0-9a-f]{64}")
MAX_RECORD = 1 << 20
TOKEN_SHAPES = re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}|gh[opsru]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Receipts:
    def __init__(self, root, tool_sha, secret_values=(), *, trust):
        self.root, self.tool_sha, self.trust = Path(root), tool_sha, trust
        self.secret_values = tuple(secret_values)
        if not self.root.is_absolute() or self.root != Path(os.path.abspath(self.root)):
            raise ReceiptError("program journal root must be a normalized absolute path")
        for directory in (self.root, *self.root.parents):
            self.trust(directory, directory=True)
        self.journal = self.root / "journal"
        try:
            self.journal.mkdir(mode=0o700)
        except FileExistsError:
            pass
        self.trust(self.journal, directory=True)
        self._migrate_legacy()

    def key(self, action, binding):
        return digest({"action": action, "binding": binding, "tool_sha256": self.tool_sha})

    @staticmethod
    def scope(action, binding):
        # Program/node is canonical. The issue fallback conservatively fences
        # pre-journal records and old fixtures that lack that tuple.
        return (binding.get("repository"), binding.get("program"), binding.get("node"),
                None if binding.get("program") and binding.get("node") else binding.get("issue"), action)

    def _read(self, path, *, temporary_links=()):
        try:
            self.trust(path)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(fd)
                self.trust(path, info=info)
                aliases = 0
                for scratch in temporary_links:
                    if scratch.parent != path.parent or not re.fullmatch(r"\.tmp-[A-Za-z0-9_-]{1,64}", scratch.name):
                        raise ReceiptError("invalid temporary journal alias")
                    other = os.lstat(scratch)
                    self.trust(scratch, info=other)
                    if stat.S_ISREG(other.st_mode) and (other.st_dev, other.st_ino) == (info.st_dev, info.st_ino):
                        aliases += 1
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 + aliases or info.st_size > MAX_RECORD:
                    raise ReceiptError("program journal must be a bounded regular file without hard links")
                with os.fdopen(fd, "rb", closefd=False) as handle:
                    raw = handle.read(MAX_RECORD + 1)
                def unique(pairs):
                    result = {}
                    for k, v in pairs:
                        if k in result:
                            raise ReceiptError("program journal has duplicate fields")
                        result[k] = v
                    return result
                value = json.loads(raw, object_pairs_hook=unique)
            finally:
                os.close(fd)
        except (OSError, ValueError):
            raise ReceiptError("program journal or receipt is unreadable") from None
        if not isinstance(value, dict):
            raise ReceiptError("program journal or receipt is invalid")
        return value

    def _sync(self, directory):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _append(self, path, value):
        """Publish a fully fsynced record once; never replace a journal event."""
        raw = canonical(value).encode()
        if len(raw) > MAX_RECORD:
            raise ReceiptError("program journal record is too large")
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw); handle.flush(); os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            try:
                os.link(tmp, path, follow_symlinks=False)
            except FileExistsError:
                if self._read(path) != value:
                    raise ReceiptError("immutable program journal event conflicts") from None
            os.unlink(tmp)
            self._sync(path.parent)
        finally:
            if os.path.lexists(tmp):
                os.unlink(tmp)
        return value

    def _path(self, key, kind):
        if not isinstance(key, str) or not HEX.fullmatch(key):
            raise ReceiptError("invalid program admission id")
        return self.journal / f"{key}.{kind}.json"

    def _migrate_legacy(self):
        # Migration itself is durable before any model admission. An original
        # receipt lost before upgrade cannot be reconstructed and is outside
        # this migration's assurance; rollout must retain its host archive.
        with self.locked("journal-migration.lock") as acquired:
            if not acquired:
                raise ReceiptError("program journal migration is busy; no request admitted")
            for path in self.root.glob("*.json"):
                if not HEX.fullmatch(path.stem):
                    raise ReceiptError("legacy program receipt has an unexpected name")
                admission_path = self._path(path.stem, "admission")
                if os.path.lexists(admission_path):
                    # Existing journal authority never consumes an edited or
                    # deleted projection again. A partial migration remains an
                    # unresolved admission rather than reconstructing a PASS.
                    continue
                value = self._read(path)
                if value.get("action") not in ("audit", "consult") or not isinstance(value.get("binding"), dict) \
                        or not HEX.fullmatch(str(value.get("tool_sha256", ""))) \
                        or value.get("status") not in ("RUNNING", "POSTED", "ERROR", "UNKNOWN") \
                        or digest({k: value.get(k) for k in ("action", "binding", "tool_sha256")}) != path.stem:
                    raise ReceiptError("legacy program receipt binding is invalid")
                admission = {"schema_version": 1, "admission": path.stem,
                             **{k: value[k] for k in ("action", "binding", "tool_sha256")}}
                self._append(admission_path, admission)
                if value["status"] != "RUNNING":
                    result = {k: v for k, v in value.items() if k not in ("action", "binding", "tool_sha256")}
                    self._append(self._path(path.stem, "outcome"), {"schema_version": 1,
                                 "admission": path.stem, "admission_sha256": digest(admission), "result": result})

    def admission(self, key):
        value = self._read(self._path(key, "admission"))
        fields = {"schema_version", "admission", "action", "binding", "tool_sha256"}
        if set(value) != fields or type(value.get("schema_version")) is not int or value["schema_version"] != 1 \
                or value.get("admission") != key or value.get("action") not in ("audit", "consult") \
                or not isinstance(value.get("binding"), dict) or not HEX.fullmatch(str(value.get("tool_sha256", ""))) \
                or digest({k: value[k] for k in ("action", "binding", "tool_sha256")}) != key:
            raise ReceiptError("program admission binding is invalid")
        return value

    def state(self, key):
        admission = self.admission(key)
        outcome, settlement = None, None
        for kind in ("outcome", "settlement"):
            path = self._path(key, kind)
            if os.path.lexists(path):
                event = self._read(path)
                if type(event.get("schema_version")) is not int or event.get("schema_version") != 1 or event.get("admission") != key \
                        or event.get("admission_sha256") != digest(admission):
                    raise ReceiptError("program journal event is not bound to its admission")
                if kind == "outcome":
                    if set(event) != {"schema_version", "admission", "admission_sha256", "result"} \
                            or not isinstance(event.get("result"), dict) \
                            or event["result"].get("status") not in ("POSTED", "ERROR", "UNKNOWN"):
                        raise ReceiptError("program journal outcome is invalid")
                    outcome = event
                else:
                    settlement = event
        if settlement:
            fields = {"schema_version", "admission", "admission_sha256", "outcome_sha256",
                      "resolution", "evidence_sha256", "run", "expected_version"}
            if set(settlement) != fields or not outcome or outcome["result"].get("status") not in ("ERROR", "UNKNOWN") \
                    or settlement.get("outcome_sha256") != digest(outcome) \
                    or settlement.get("resolution") != "TERMINAL_FAILED" \
                    or not HEX.fullmatch(str(settlement.get("evidence_sha256", ""))) \
                    or settlement.get("run") != (outcome["result"].get("failure") or {}).get("run") \
                    or settlement.get("expected_version") != digest({"admission": admission, "outcome": outcome}):
                raise ReceiptError("program terminal settlement is invalid")
        return admission, outcome, settlement

    def read(self, action, binding):
        key = self.key(action, binding)
        if os.path.lexists(self._path(key, "admission")):
            admission, outcome, settlement = self.state(key)
            result = (outcome or {}).get("result", {"status": "RUNNING"})
            value = {"action": action, "binding": binding, "tool_sha256": self.tool_sha, **result,
                    "admission": key, "state_version": digest({"admission": admission, "outcome": outcome}),
                    **({"settlement": "TERMINAL_FAILED"} if settlement else {})}
            return self._fenced_read(action, binding, value)
        path = self.root / (key + ".json")
        if not os.path.lexists(path):
            return self._fenced_read(action, binding, {"status": "MISSING"})
        value = self._read(path)
        if value.get("action") != action or value.get("binding") != binding or value.get("tool_sha256") != self.tool_sha:
            raise ReceiptError("program Astra receipt binding is invalid")
        self._migrate_legacy()
        return self.read(action, binding)

    def _fenced_read(self, action, binding, value):
        ancestor = self.unresolved(action, binding)
        if ancestor and value.get("status") in ("POSTED", "MISSING"):
            return {"status": "UNKNOWN", "ancestor_admission": ancestor,
                    "reason": "an ancestor request requires protected terminal reconciliation"}
        return value

    def write(self, action, binding, result):
        key = self.key(action, binding)
        admission = {"schema_version": 1, "admission": key, "action": action,
                     "binding": binding, "tool_sha256": self.tool_sha}
        self._append(self._path(key, "admission"), admission)
        if result.get("status") != "RUNNING":
            self._append(self._path(key, "outcome"), {"schema_version": 1, "admission": key,
                         "admission_sha256": digest(admission), "result": result})
        # Compatibility projection; all subsequent authority comes from journal.
        value = {"action": action, "binding": binding, "tool_sha256": self.tool_sha, **result}
        fd, temporary = tempfile.mkstemp(dir=self.root)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(canonical(value)); handle.flush(); os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.root / (key + ".json")); self._sync(self.root)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)
        return self.read(action, binding)

    def records(self):
        self._migrate_legacy()
        known = set()
        files = list(self.journal.glob("*.json"))
        if len(files) > 30000:
            raise ReceiptError("program journal needs operator archival")
        for path in files:
            match = re.fullmatch(r"([0-9a-f]{64})\.(admission|outcome|settlement)\.json", path.name)
            if not match or not os.path.lexists(self._path(match.group(1), "admission")):
                raise ReceiptError("program journal has an orphan or unexpected event")
            known.add(match.group(1))
        for key in sorted(known):
            admission, outcome, settlement = self.state(key)
            yield key, admission, (outcome or {}).get("result", {"status": "RUNNING"}), settlement
        for path in self.root.glob("*.json"):
            if not HEX.fullmatch(path.stem):
                raise ReceiptError("program receipt has an unexpected name")
            if path.stem in known:
                continue
            value = self._read(path)
            if value.get("action") not in ("audit", "consult") or not isinstance(value.get("binding"), dict) \
                    or digest({k: value.get(k) for k in ("action", "binding", "tool_sha256")}) != path.stem:
                raise ReceiptError("legacy program receipt binding is invalid")
            yield path.stem, value, value, None

    def unresolved(self, action, binding):
        for key, admission, result, settlement in self.records():
            if self.scope(action, binding) == self.scope(admission["action"], admission["binding"]) \
                    and result.get("status") != "POSTED" and not settlement:
                return key
        return None

    @contextmanager
    def locked(self, name):
        path = self.root / name
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            self.trust(path)
            info = os.fstat(fd)
            self.trust(path, info=info)
            if info.st_nlink != 1:
                raise ReceiptError("program lock must not be hard linked")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                yield True
        finally:
            os.close(fd)

    def execute(self, action, binding, invoke, *, admission_guard=None):
        # A scope lock serializes both admission and settlement across new keys.
        scope_id = digest(list(self.scope(action, binding)))
        with self.locked(scope_id + ".scope.lock") as acquired:
            if not acquired:
                return {"status": "RUNNING"}
            previous = self.read(action, binding)
            if previous["status"] != "MISSING":
                return previous if previous["status"] != "RUNNING" else {**previous, "status": "UNKNOWN"}
            ancestor = self.unresolved(action, binding)
            if ancestor:
                return {"status": "UNKNOWN", "ancestor_admission": ancestor,
                        "reason": "an ancestor request requires protected terminal reconciliation"}
            with self.locked("model.lock") as global_acquired:
                if not global_acquired:
                    return {"status": "BUSY"}
                if admission_guard is not None:
                    held = admission_guard()
                    if held is not None:
                        if not isinstance(held, dict) or held.get("status") not in ("UNKNOWN", "QUEUED", "BUSY"):
                            raise ReceiptError("locked program admission guard returned invalid state")
                        return held
                return self._invoke(action, binding, invoke)

    def _invoke(self, action, binding, invoke):
        self.write(action, binding, {"status": "RUNNING"})
        try:
            result = invoke()
            if result.get("status") != "POSTED" or result.get("program_binding") != binding:
                raise ReceiptError("Fable result is not a bound durable POSTED receipt")
            return self.write(action, binding, result)
        except Exception as exc:
            failure = getattr(exc, "failure", None)
            result = {"status": "ERROR", "reason": cp.program_error_reason(exc, self.secret_values)}
            if isinstance(failure, dict):
                # Full raw archives stay protected on the host. This structured
                # envelope contains only typed identifiers/digests, never text.
                encoded = canonical(failure)
                if any(secret and secret in encoded for secret in self.secret_values) or TOKEN_SHAPES.search(encoded):
                    raise ReceiptError("failure envelope contains credential material") from None
                result["failure"] = failure
            self.write(action, binding, result)
            raise

    def reconcile(self, key, expected_version, verify):
        admission, _, _ = self.state(key)
        scope_id = digest(list(self.scope(admission["action"], admission["binding"])))
        with self.locked(scope_id + ".scope.lock") as acquired:
            if not acquired:
                return {"status": "RUNNING"}
            with self.locked("model.lock") as global_acquired:
                if not global_acquired:
                    return {"status": "BUSY"}
                admission, outcome, settlement = self.state(key)
                version = digest({"admission": admission, "outcome": outcome})
                if not isinstance(expected_version, str) or not HEX.fullmatch(expected_version) or expected_version != version:
                    raise ReceiptError("program reconciliation state version is stale")
                if settlement:
                    return {"status": "RECONCILED_FAILED", "admission": key, "state_version": version}
                failure = ((outcome or {}).get("result", {}).get("failure") or {})
                if not outcome or outcome["result"].get("status") not in ("ERROR", "UNKNOWN") or not failure.get("run"):
                    raise ReceiptError("unproven legacy or ambiguous request remains fenced")
                evidence = verify(failure["run"])
                never_attempted = (evidence.get("model_attempted") is False
                                   and evidence.get("error_code") == "PRE_MODEL_FAILED"
                                   and evidence.get("archive_state") == "SEALED")
                if evidence != failure or evidence.get("program_binding") != admission["binding"] \
                        or evidence.get("terminal_evidence") != "VERIFIED" \
                        or evidence.get("process_terminated") is not True \
                        or evidence.get("publication_state") != "NOT_STARTED" \
                        or evidence.get("kind") != admission["action"] \
                        or not never_attempted and evidence.get("error_code") not in ("MODEL_RATE_LIMIT", "MODEL_EXECUTION_FAILED",
                                                             "OVERAGE_NOT_BLOCKED", "OVERAGE_UNVERIFIED",
                                                             "WRAPPER_TIMEOUT", "STREAM_INVALID", "OUTPUT_LIMIT"):
                    raise ReceiptError("program failure evidence is ambiguous or bound to another request")
                event = {"schema_version": 1, "admission": key, "admission_sha256": digest(admission),
                         "outcome_sha256": digest(outcome), "resolution": "TERMINAL_FAILED",
                         "evidence_sha256": digest(evidence), "run": failure["run"], "expected_version": version}
                self._append(self._path(key, "settlement"), event)
                return {"status": "RECONCILED_FAILED", "admission": key, "state_version": version}

    def decision_status(self, binding):
        for _, admission, result, settlement in self.records():
            # Execution ambiguity survives changed plan/HEAD/tool. Semantic
            # decisions retain the original exact-context governance boundary.
            same_task = self.scope(admission["action"], binding) == self.scope(admission["action"], admission["binding"])
            exact = all(admission["binding"].get(key) == val for key, val in binding.items())
            if same_task and result.get("status") != "POSTED" and not settlement \
                    or exact and (result.get("result") in ("USER_REQUIRED", "DECISION_REQUIRED")
                                  or result.get("scope_result") == "USER_REQUIRED"):
                return {"status": "BLOCKED", "reason": "Fable decision or execution is unresolved"}
        return {"status": "CLEAR"}
