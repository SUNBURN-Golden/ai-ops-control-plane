"""One protected quota wake on the existing coordinator event/heartbeat.

This module installs no timer and grants no activation. Trusted host callbacks
verify archives, run a fresh preflight and rebuild current authority. Retry
admissions live in a protected child journal with the original program binding;
the parent's ERROR, admission and terminal settlement are never replaced.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import time

import control_plane_program_receipts as journal


class Quota:
    def __init__(self, receipts, verify, fresh_preflight, refresh_context, *, clock_ms=None):
        self.store = receipts
        self.verify = verify
        self.fresh_preflight = fresh_preflight
        self.refresh_context = refresh_context
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.root = self.store.root / "quota"
        self._directory(self.root)

    def _directory(self, path):
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        self.store.trust(path, directory=True)
        self.store._sync(path.parent)

    def _now(self):
        now = self.clock_ms()
        if type(now) is not int or not 0 <= now <= 2 ** 53:
            raise journal.ReceiptError("quota clock is invalid")
        return now

    def _current(self, action, binding, fingerprint):
        try:
            current = self.refresh_context(action, binding)
        except Exception:
            return False
        return isinstance(current, dict) and current.get("binding") == binding \
            and current.get("tool_sha256") == fingerprint

    def _candidate(self, key):
        admission, outcome, settlement = self.store.state(key)
        if admission["tool_sha256"] != self.store.tool_sha or not outcome \
                or outcome["result"].get("status") != "ERROR":
            return None
        evidence = outcome["result"].get("failure")
        if not isinstance(evidence, dict) or evidence.get("error_code") != "MODEL_RATE_LIMIT":
            return None
        try:
            actual = self.verify(evidence.get("run"))
        except Exception:
            return None
        reset = evidence.get("reset_at_epoch_ms")
        if actual != evidence or evidence.get("schema") != "FABLE_FAILURE_V1" \
                or type(evidence.get("schema_version")) is not int or evidence["schema_version"] != 1 \
                or evidence.get("kind") != admission["action"] \
                or evidence.get("program_binding") != admission["binding"] \
                or evidence.get("terminal_evidence") != "VERIFIED" \
                or evidence.get("process_terminated") is not True \
                or evidence.get("model_attempted") is not True \
                or evidence.get("archive_state") != "SEALED" \
                or evidence.get("publication_state") != "NOT_STARTED" \
                or evidence.get("adapter_profile") != "claude-cli-2.1.285-observed-v1" \
                or type(evidence.get("api_error_status")) is not int or evidence["api_error_status"] != 429 \
                or evidence.get("limit_type") not in ("five_hour", "seven_day") \
                or type(reset) is not int or not 0 < reset <= 2 ** 53 or reset % 1000 \
                or not isinstance(evidence.get("extra_usage"), dict) \
                or evidence["extra_usage"].get("overageStatus") != "rejected" \
                or evidence["extra_usage"].get("isUsingOverage") is not False:
            return None
        # The verifier proves the pinned adapter's bounded UTC reset against
        # sealed observation time. A caller never supplies a reset or profile.
        version = journal.digest({"admission": admission, "outcome": outcome})
        expected_settlement = {"schema_version": 1, "admission": key,
                               "admission_sha256": journal.digest(admission),
                               "outcome_sha256": journal.digest(outcome), "resolution": "TERMINAL_FAILED",
                               "evidence_sha256": journal.digest(evidence), "run": evidence["run"],
                               "expected_version": version}
        if settlement is not None and settlement != expected_settlement:
            return None
        incident = journal.digest({"schema": "FABLE_QUOTA_RETRY_V1", "parent": key,
                                   "settlement": expected_settlement})
        return {"schema_version": 1, "incident": incident, "parent_admission": key,
                "parent_admission_sha256": journal.digest(admission),
                "parent_outcome_sha256": journal.digest(outcome),
                "parent_settlement_sha256": journal.digest(expected_settlement),
                "action": admission["action"], "binding": admission["binding"],
                "tool_sha256": admission["tool_sha256"], "reset_at_epoch_ms": reset}, expected_settlement

    def _path(self, incident):
        if not isinstance(incident, str) or not journal.HEX.fullmatch(incident):
            raise journal.ReceiptError("invalid quota incident")
        return self.root / incident

    def _tickets(self):
        paths = list(self.root.iterdir())
        if len(paths) > 10000:
            raise journal.ReceiptError("quota journal needs operator archival")
        for path in sorted(paths):
            if not journal.HEX.fullmatch(path.name):
                raise journal.ReceiptError("quota journal has an unexpected entry")
            self.store.trust(path, directory=True)
            ticket_path = path / "ticket.json"
            if not os.path.lexists(ticket_path):
                # A crash after mkdir and before ticket creation admitted nothing.
                if any(not self._temporary(child) for child in path.iterdir()):
                    raise journal.ReceiptError("quota ticket is incomplete")
                continue
            ticket = self._record(ticket_path)
            fields = {"schema_version", "incident", "parent_admission", "parent_admission_sha256",
                      "parent_outcome_sha256", "parent_settlement_sha256", "action", "binding",
                      "tool_sha256", "reset_at_epoch_ms"}
            if set(ticket) != fields or type(ticket.get("schema_version")) is not int \
                    or ticket["schema_version"] != 1 or ticket.get("incident") != path.name:
                raise journal.ReceiptError("quota ticket is invalid")
            admission, outcome, settlement = self.store.state(ticket["parent_admission"])
            if not outcome or not settlement \
                    or ticket["parent_admission_sha256"] != journal.digest(admission) \
                    or ticket["parent_outcome_sha256"] != journal.digest(outcome) \
                    or ticket["parent_settlement_sha256"] != journal.digest(settlement) \
                    or ticket["action"] != admission["action"] or ticket["binding"] != admission["binding"] \
                    or ticket["tool_sha256"] != admission["tool_sha256"] \
                    or ticket["reset_at_epoch_ms"] != outcome["result"].get("failure", {}).get("reset_at_epoch_ms") \
                    or ticket["incident"] != journal.digest({"schema": "FABLE_QUOTA_RETRY_V1",
                                                               "parent": ticket["parent_admission"],
                                                               "settlement": settlement}):
                raise journal.ReceiptError("quota ticket parent binding is invalid")
            allowed = {"ticket.json", "claim.json", "preflight.json", "terminal.json", "attempt",
                       "preflight-intent.json", "preflight-start.json", "operator-settlement.json"}
            if any(child.name not in allowed and not self._temporary(child) for child in path.iterdir()):
                raise journal.ReceiptError("quota ticket has an unexpected event")
            yield ticket

    def _temporary(self, path):
        """Unpublished atomic-write scratch is not an event or a retry grant."""
        if not re.fullmatch(r"\.tmp-[A-Za-z0-9_-]{1,64}", path.name):
            return False
        info = os.lstat(path)
        self.store.trust(path, info=info)
        # A crash between link publication and tmp unlink can leave nlink=2.
        # Scratch is ignored as authority. The named event reader separately
        # accounts for protected same-inode scratch aliases without deleting them.
        if not stat.S_ISREG(info.st_mode) or info.st_size > journal.MAX_RECORD:
            raise journal.ReceiptError("quota temporary file is not protected bounded scratch")
        return True

    def _record(self, path):
        # An interrupted link()/unlink() publication leaves the named immutable
        # event and a root-owned .tmp alias with the same inode. Accept only
        # fully enumerated scratch aliases in this incident directory. An
        # external hard link still fails the journal's descriptor check.
        aliases = [child for child in path.parent.iterdir() if self._temporary(child)]
        if len(aliases) > 64:
            raise journal.ReceiptError("quota scratch inventory needs operator archival")
        return self.store._read(path, temporary_links=aliases)

    def _never_started_proof(self, ticket):
        directory = self._path(ticket["incident"])
        intent = self._record(directory / "preflight-intent.json")
        expected = {"schema_version": 1, "incident": ticket["incident"],
                    "ticket_sha256": journal.digest(ticket), "model_attempted": False}
        if intent != expected or intent.get("model_attempted") is not False \
                or type(intent.get("schema_version")) is not int or self._child(ticket) is not None \
                or any(os.path.lexists(directory / name) for name in
                       ("preflight-start.json", "preflight.json", "terminal.json")):
            raise journal.ReceiptError("quota wake is not proven never attempted")
        if not self._claimed(ticket):
            raise journal.ReceiptError("quota wake has no consumed claim")
        claim = self._record(directory / "claim.json")
        proof = {"ticket": ticket, "claim": claim, "intent": intent}
        return proof, journal.digest(proof)

    def operator_reconcile_ticket(self, incident, admission, expected_version):
        """Root entry's childless selector: append settlement, never another wake."""
        matches = [ticket for ticket in self._tickets() if ticket["incident"] == incident
                   and ticket["parent_admission"] == admission]
        if len(matches) != 1:
            raise journal.ReceiptError("operator quota parent selector is invalid")
        ticket = matches[0]
        scope = journal.digest(list(self.store.scope(ticket["action"], ticket["binding"])))
        with self.store.locked(scope + ".scope.lock") as acquired:
            if not acquired:
                return {"status": "RUNNING"}
            with self.store.locked("model.lock") as acquired:
                if not acquired:
                    return {"status": "BUSY"}
                proof, version = self._never_started_proof(ticket)
                if expected_version != version:
                    raise journal.ReceiptError("operator quota state version is stale")
                event = {"schema_version": 1, "incident": incident, "parent_admission": admission,
                         "resolution": "CONSUMED_NO_CHILD", "model_attempted": False,
                         "evidence_sha256": journal.digest(proof), "expected_version": version}
                self.store._append(self._path(incident) / "operator-settlement.json", event)
                return {"status": "RECONCILED_FAILED", "quota_attempt": incident,
                        "parent_admission": admission, "state_version": version}

    def _claimed(self, ticket):
        path = self._path(ticket["incident"]) / "claim.json"
        if not os.path.lexists(path):
            return False
        claim = self._record(path)
        expected = {"schema_version": 1, "incident": ticket["incident"],
                    "ticket_sha256": journal.digest(ticket),
                    "nonce": journal.digest({"incident": ticket["incident"], "wake": 1})}
        if claim != expected:
            raise journal.ReceiptError("quota one-shot claim is invalid")
        return True

    def _child(self, ticket, *, create=False):
        path = self._path(ticket["incident"]) / "attempt"
        if not os.path.lexists(path):
            if not create:
                return None
            self._directory(path)
        self.store.trust(path, directory=True)
        return journal.Receipts(path, ticket["tool_sha256"], self.store.secret_values, trust=self.store.trust)

    def _effective_ticket(self, ticket):
        if not self._claimed(ticket):
            return None
        child = self._child(ticket)
        operator_path = self._path(ticket["incident"]) / "operator-settlement.json"
        if os.path.lexists(operator_path):
            proof, version = self._never_started_proof(ticket)
            event = self._record(operator_path)
            if event.get("model_attempted") is not False or type(event.get("schema_version")) is not int \
                    or event != {"schema_version": 1, "incident": ticket["incident"],
                         "parent_admission": ticket["parent_admission"], "resolution": "CONSUMED_NO_CHILD",
                         "model_attempted": False, "evidence_sha256": journal.digest(proof),
                         "expected_version": version}:
                raise journal.ReceiptError("operator quota settlement is invalid")
            return {"status": "BLOCKED_ERROR", "resolution": "CONSUMED_NO_CHILD",
                    "reason": "operator settled a sealed never-attempted quota wake; no retry",
                    "quota_attempt": ticket["incident"], "parent_admission": ticket["parent_admission"]}
        terminal_path = self._path(ticket["incident"]) / "terminal.json"
        terminal = None
        if os.path.lexists(terminal_path):
            terminal = self._record(terminal_path)
            fields = {"schema_version", "incident", "status", "reason"}
            consumed_fields = fields | {"resolution", "preflight_sha256"}
            if set(terminal) not in (fields, consumed_fields) \
                    or type(terminal.get("schema_version")) is not int or terminal["schema_version"] != 1 \
                    or terminal.get("incident") != ticket["incident"] \
                    or terminal.get("status") not in ("UNKNOWN", "BLOCKED_POLICY", "BLOCKED_ERROR", "STALE_CONTEXT"):
                raise journal.ReceiptError("quota terminal event is invalid")
            # Terminal events are written only before an audit/consult child is
            # created. A conflicting child cannot turn a no-child proof into a
            # PASS or an execution-fence release.
            if child is not None:
                raise journal.ReceiptError("quota child contradicts its preflight terminal")
            if set(terminal) == consumed_fields:
                if terminal["resolution"] != "CONSUMED_NO_CHILD" or terminal["status"] == "UNKNOWN" \
                        or terminal["preflight_sha256"] != self._preflight_proof(ticket)["preflight_sha256"]:
                    raise journal.ReceiptError("quota childless completion proof is invalid")
            elif terminal["status"] != "UNKNOWN":
                # Earlier source versions did not bind terminal events to a
                # completed preflight. Preserve those admissions as UNKNOWN;
                # migration never fabricates the missing proof.
                terminal = {**terminal, "terminal_status": terminal["status"], "status": "UNKNOWN"}
        result = child.read(ticket["action"], ticket["binding"]) if child else {"status": "MISSING"}
        if result.get("status") == "MISSING":
            if terminal is not None:
                result = terminal
            else:
                result = {"status": "UNKNOWN", "reason": "claimed quota wake has no durable terminal result"}
                try:
                    _, version = self._never_started_proof(ticket)
                except journal.ReceiptError:
                    pass
                else:
                    result["state_version"] = version
        elif result.get("status") == "RUNNING":
            result = {**result, "status": "UNKNOWN"}
        if result.get("status") == "POSTED":
            self._preflight_proof(ticket)
        return {**result, "quota_attempt": ticket["incident"], "parent_admission": ticket["parent_admission"]}

    def _preflight_proof(self, ticket):
        proof = self._record(self._path(ticket["incident"]) / "preflight.json")
        if set(proof) != {"schema_version", "incident", "preflight_sha256"} \
                or type(proof.get("schema_version")) is not int or proof["schema_version"] != 1 \
                or proof.get("incident") != ticket["incident"] \
                or not journal.HEX.fullmatch(str(proof.get("preflight_sha256", ""))):
            raise journal.ReceiptError("quota retry has no bound protected preflight")
        return proof

    @staticmethod
    def _consumed_no_child(result):
        return result is not None and result.get("resolution") == "CONSUMED_NO_CHILD"

    def operator_store(self, incident, admission):
        """Derive a child journal for the separate root-only reconciliation API.

        Selectors are bounded opaque IDs, never paths. This helper grants no
        settlement: the operator entry still calls the ordinary verifier/CAS.
        """
        self._path(incident)
        if not isinstance(admission, str) or not journal.HEX.fullmatch(admission):
            raise journal.ReceiptError("invalid quota child admission")
        matches = [ticket for ticket in self._tickets() if ticket["incident"] == incident]
        if len(matches) != 1 or not self._claimed(matches[0]):
            raise journal.ReceiptError("quota child ticket is missing or unclaimed")
        ticket = matches[0]
        child = self._child(ticket)
        if child is None or child.key(ticket["action"], ticket["binding"]) != admission:
            raise journal.ReceiptError("quota child admission is bound to another incident")
        record = child.admission(admission)
        if record["binding"] != ticket["binding"] or record["action"] != ticket["action"] \
                or record["tool_sha256"] != ticket["tool_sha256"]:
            raise journal.ReceiptError("quota child admission binding is invalid")
        return child

    def effective(self, action, binding):
        key = self.store.key(action, binding)
        matches = [ticket for ticket in self._tickets() if ticket["parent_admission"] == key]
        if len(matches) > 1:
            raise journal.ReceiptError("quota incident has multiple automatic attempts")
        result = self._effective_ticket(matches[0]) if matches else None
        if result is None:
            result = self.store.read(action, binding)
        if result.get("status") in ("POSTED", "MISSING"):
            ancestor = self._ancestor(binding, key)
            if ancestor:
                return {"status": "UNKNOWN", "ancestor_admission": ancestor,
                        "reason": "another task attempt has an unresolved protected execution"}
        return result

    def unresolved(self, action, binding):
        for ticket in self._tickets():
            if self.store.scope(action, binding) != self.store.scope(ticket["action"], ticket["binding"]):
                continue
            result = self._effective_ticket(ticket)
            if result is None or result.get("status") == "POSTED" or self._consumed_no_child(result):
                continue
            child = self._child(ticket)
            if child:
                key = child.key(ticket["action"], ticket["binding"])
                if os.path.lexists(child._path(key, "admission")) and child.state(key)[2] is not None:
                    continue  # operator release, never a second automatic ticket
            return ticket["incident"]
        return None

    def decision_status(self, binding):
        for action in ("audit", "consult"):
            incident = self.unresolved(action, binding)
            if incident:
                return {"status": "BLOCKED", "reason": "quota retry requires protected host reconciliation",
                        "quota_attempt": incident}
        for ticket in self._tickets():
            if not all(ticket["binding"].get(key) == value for key, value in binding.items()):
                continue
            result = self._effective_ticket(ticket)
            if result and (result.get("result") in ("USER_REQUIRED", "DECISION_REQUIRED")
                           or result.get("scope_result") == "USER_REQUIRED"):
                return {"status": "BLOCKED", "reason": "quota retry returned a protected User decision"}
        return {"status": "CLEAR"}

    def _fences(self, records=None, tickets=None):
        """One bounded scan for all scopes, including derivative child journals."""
        records = list(self.store.records()) if records is None else records
        tickets = list(self._tickets()) if tickets is None else tickets
        blocked = {}
        def add(action, binding, key):
            scope = self.store.scope(action, binding)[:-1]  # ambiguity crosses audit/consult
            blocked.setdefault(scope, set()).add(key)
        for key, admission, result, settlement in records:
            if result.get("status") != "POSTED" and not settlement:
                add(admission["action"], admission["binding"], key)
        for ticket in tickets:
            result = self._effective_ticket(ticket)
            if result is None or result.get("status") == "POSTED" or self._consumed_no_child(result):
                continue
            child = self._child(ticket)
            if child:
                key = child.key(ticket["action"], ticket["binding"])
                if os.path.lexists(child._path(key, "admission")) and child.state(key)[2] is not None:
                    continue
            add(ticket["action"], ticket["binding"], ticket["incident"])
        return blocked

    def _ancestor(self, binding, except_key=None):
        blocked = self._fences().get(self.store.scope("audit", binding)[:-1], set()) - {except_key}
        return min(blocked) if blocked else None

    def _queue(self, now):
        tickets = list(self._tickets())
        claimed = {ticket["incident"] for ticket in tickets if self._claimed(ticket)}
        records = list(self.store.records())
        blocked = self._fences(records, tickets)
        queue = []
        candidates = [(key, admission, result) for key, admission, result, _ in records
                      if result.get("status") == "ERROR" and admission.get("tool_sha256") == self.store.tool_sha
                      and (result.get("failure") or {}).get("error_code") == "MODEL_RATE_LIMIT"]
        if len(candidates) > 512:
            raise journal.ReceiptError("quota evidence inventory needs operator archival")
        for key, admission, result in candidates:
            if result.get("status") != "ERROR" or admission.get("tool_sha256") != self.store.tool_sha \
                    or not os.path.lexists(self.store._path(key, "admission")):
                continue
            candidate = self._candidate(key)
            if candidate is None:
                continue
            ticket, _ = candidate
            if ticket["incident"] in claimed or ticket["reset_at_epoch_ms"] > now \
                    or not self._current(ticket["action"], ticket["binding"], ticket["tool_sha256"]) \
                    or blocked.get(self.store.scope(ticket["action"], ticket["binding"])[:-1], set()) - {key}:
                continue
            queue.append(ticket)
        return sorted(queue, key=lambda ticket: (ticket["reset_at_epoch_ms"], ticket["parent_admission"]))

    def fair_blocked(self, action, binding):
        """Fresh admission yields only to a due retry for the same task.

        Audit and consult share the task fence; unrelated products/nodes do
        not wait for somebody else's quota-resume. Model serialization and
        the quota-resume queue order remain unchanged.
        """
        scope = self.store.scope(action, binding)[:-1]
        queue = [ticket for ticket in self._queue(self._now())
                 if self.store.scope(ticket["action"], ticket["binding"])[:-1] == scope]
        return queue[0]["incident"] if queue else None

    def readiness(self, action, binding):
        result = self.effective(action, binding)
        if result.get("quota_attempt"):
            return result
        if result.get("status") != "ERROR":
            return result
        key = self.store.key(action, binding)
        if not os.path.lexists(self.store._path(key, "admission")):
            return result
        candidate = self._candidate(key)
        if candidate is None:
            return {"status": "BLOCKED_ERROR", "parent_admission": key,
                    "reason": "no exact verified terminal quota evidence"}
        ticket, _ = candidate
        if not self._current(action, binding, self.store.tool_sha):
            return {"status": "STALE_CONTEXT", "parent_admission": key}
        ancestor = self._ancestor(binding, key)
        if ancestor:
            return {"status": "UNKNOWN", "ancestor_admission": ancestor}
        now = self._now()
        if now < ticket["reset_at_epoch_ms"]:
            return {"status": "WAITING_QUOTA", "parent_admission": key,
                    "reset_at_epoch_ms": ticket["reset_at_epoch_ms"]}
        queue = self._queue(now)
        return {"status": "DUE" if queue and queue[0]["incident"] == ticket["incident"] else "QUEUED",
                "parent_admission": key, "incident": ticket["incident"],
                "reset_at_epoch_ms": ticket["reset_at_epoch_ms"]}

    def _terminal(self, ticket, status, reason, *, completed_preflight=None):
        result = {"schema_version": 1, "incident": ticket["incident"], "status": status, "reason": reason}
        if completed_preflight is not None:
            if status not in ("BLOCKED_POLICY", "BLOCKED_ERROR", "STALE_CONTEXT") \
                    or self._child(ticket) is not None:
                raise journal.ReceiptError("quota childless completion cannot release this state")
            digest = journal.digest(completed_preflight)
            self.store._append(self._path(ticket["incident"]) / "preflight.json", {
                "schema_version": 1, "incident": ticket["incident"], "preflight_sha256": digest})
            result.update(resolution="CONSUMED_NO_CHILD", preflight_sha256=digest)
        self.store._append(self._path(ticket["incident"]) / "terminal.json", result)
        return {**result, "quota_attempt": ticket["incident"], "parent_admission": ticket["parent_admission"]}

    def resume(self, action, binding, invoke):
        scope_id = journal.digest(list(self.store.scope(action, binding)))
        with self.store.locked(scope_id + ".scope.lock") as acquired:
            if not acquired:
                return {"status": "BUSY"}
            with self.store.locked("model.lock") as acquired:
                if not acquired:
                    return {"status": "BUSY"}
                readiness = self.readiness(action, binding)
                if readiness.get("status") != "DUE":
                    return readiness
                key = self.store.key(action, binding)
                candidate = self._candidate(key)
                if candidate is None:
                    return {"status": "BLOCKED_ERROR", "reason": "quota evidence changed before admission"}
                ticket, settlement = candidate
                # Any other unresolved ancestor survives this one known incident.
                ancestor = self._ancestor(binding, key)
                if ancestor:
                    return {"status": "UNKNOWN", "ancestor_admission": ancestor}
                self.store._append(self.store._path(key, "settlement"), settlement)
                directory = self._path(ticket["incident"])
                self._directory(directory)
                self.store._append(directory / "ticket.json", ticket)
                claim = {"schema_version": 1, "incident": ticket["incident"],
                         "ticket_sha256": journal.digest(ticket),
                         "nonce": journal.digest({"incident": ticket["incident"], "wake": 1})}
                self.store._append(directory / "preflight-intent.json", {
                    "schema_version": 1, "incident": ticket["incident"],
                    "ticket_sha256": journal.digest(ticket), "model_attempted": False})
                self.store._append(directory / "claim.json", claim)
                # Absence of this durable marker proves a crash just after claim
                # did not invoke preflight. After it, ambiguity remains fenced.
                self.store._append(directory / "preflight-start.json", {
                    "schema_version": 1, "incident": ticket["incident"], "model_attempted": True})
                try:
                    preflight = self.fresh_preflight()
                except Exception as exc:
                    failure = getattr(exc, "failure", {}) or {}
                    try:
                        verified = self.verify(failure.get("run")) == failure
                    except Exception:
                        verified = False
                    terminal = failure.get("schema") == "FABLE_FAILURE_V1" \
                        and type(failure.get("schema_version")) is int and failure["schema_version"] == 1 \
                        and failure.get("archive_state") == "SEALED" \
                        and failure.get("terminal_evidence") == "VERIFIED" \
                        and failure.get("process_terminated") is True and failure.get("publication_state") == "NOT_STARTED" \
                        and failure.get("kind") == "preflight" and verified
                    status = "UNKNOWN" if not terminal else "BLOCKED_POLICY" \
                        if failure.get("error_code") == "OVERAGE_NOT_BLOCKED" else "BLOCKED_ERROR"
                    return self._terminal(ticket, status, "fresh protected preflight did not pass",
                                          completed_preflight=failure if terminal else None)
                if not isinstance(preflight, dict) or preflight.get("status") != "PASS":
                    return self._terminal(ticket, "UNKNOWN", "fresh preflight returned no completed result")
                if not isinstance(preflight.get("extra_usage"), dict) \
                        or preflight["extra_usage"].get("overageStatus") != "rejected" \
                        or preflight["extra_usage"].get("isUsingOverage", False) is not False:
                    return self._terminal(ticket, "BLOCKED_POLICY", "fresh preflight has no blocked extra-usage proof",
                                          completed_preflight=preflight)
                # Store a digest, not caller- or model-supplied prose/credentials.
                self.store._append(directory / "preflight.json", {"schema_version": 1,
                                   "incident": ticket["incident"], "preflight_sha256": journal.digest(preflight)})
                if not self._current(action, binding, self.store.tool_sha):
                    return self._terminal(ticket, "STALE_CONTEXT", "authority changed during fresh preflight",
                                          completed_preflight=preflight)
                current = self._candidate(key)
                if current is None or current[0] != ticket or current[1] != settlement:
                    return self._terminal(ticket, "UNKNOWN", "parent quota evidence changed during fresh preflight")
                child = self._child(ticket, create=True)
                try:
                    child._invoke(action, binding, invoke)
                except Exception:
                    # Its immutable child journal carries the redacted error.
                    pass
                return self.effective(action, binding)
