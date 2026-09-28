"""Launch boundary regressions; no provider or GitHub network access."""
import contextlib
import io
import json
import hashlib
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import control_plane as cp


def activation_doc(**overrides):
    doc = {
        "schema_version": 1,
        "user_activation_approval": "APPROVED",
        "user_activation_approval_pointer": "https://github.com/owner/repo/issues/1#issuecomment-1",
        "implementation_audit": "PASS",
        "implementation_audit_pointer": "https://github.com/owner/repo/issues/1#issuecomment-2",
        "runner_preflight": "PASS",
        "runner_preflight_pointer": "https://github.com/owner/repo/issues/1#issuecomment-3",
        "runtime_enabled": False,
        "activated_runtime_sha": "PENDING",
    }
    doc.update(overrides)
    return doc


class DispatchBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.record = dict(repository="owner/repo", task_id="T1", task_revision="1",
                           builder_id="DEVIN", launch_request_id="request-1", attempt_id=1,
                           launch_state="SUBMITTING", owner_session_id=None)

    def test_no_control_credentials_or_runner_files_reach_host(self):
        response = subprocess.CompletedProcess([], 0, '{"status":"PASS"}', '')
        with patch.dict(os.environ, {"GITHUB_TOKEN": "sensitive", "GITHUB_OUTPUT": "/runner/out",
                                     "PYTHONPATH": "/unsafe", "HOME": "/runner"}), \
             patch.object(cp.subprocess, "run", return_value=response) as run:
            cp.host_call(["preflight", "--builder-id", "DEVIN"])
        self.assertEqual(run.call_args.kwargs["env"], {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        self.assertEqual(run.call_args.kwargs["cwd"], "/")
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_runtime_workflow_cannot_import_checkout_shadow_modules(self):
        workflow = (cp.ROOT / ".github/workflows/control-plane-runtime.yml").read_text()
        commands = re.findall(r"\bpython3([^\n]*?)scripts/control_plane\.py", workflow)
        self.assertTrue(commands)
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "scripts/control_plane.py"
            script.parent.mkdir()
            script.write_text(Path(cp.__file__).read_text())
            sentinel = Path(tmp) / "injected"
            (script.parent / "argparse.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(sentinel)!r}).write_text('executed before gates')\n"
                "raise RuntimeError('checkout shadow module executed')\n"
            )
            for flags in set(commands):
                result = subprocess.run(["python3", *shlex.split(flags), str(script), "self-test"],
                                        cwd=tmp, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(sentinel.exists())

    def test_old_success_cannot_survive_response_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            packet, result = Path(tmp)/"packet", Path(tmp)/"result"
            packet.write_text(json.dumps(self.record))
            result.write_text(json.dumps({**self.record, "outcome": "CONFIRMED", "session_id": "old"}))
            with patch.object(cp, "require_runtime_enabled"), \
                 patch.object(cp, "host_call", side_effect=cp.ControlPlaneError("response lost")):
                cp.launch_dispatch(packet, result)
            actual = json.loads(result.read_text())
            self.assertEqual(actual["outcome"], "UNKNOWN")
            self.assertEqual(actual["launch_request_id"], "request-1")
            self.assertNotIn("session_id", actual)

    def test_disabled_runtime_never_reaches_launch_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            packet, result = Path(tmp)/"packet", Path(tmp)/"result"
            packet.write_text(json.dumps(self.record))
            with patch.object(cp, "require_runtime_enabled", side_effect=cp.ControlPlaneError("disabled")), \
                 patch.object(cp, "host_call") as host:
                cp.launch_dispatch(packet, result)
            host.assert_not_called()
            self.assertEqual(json.loads(result.read_text())["outcome"], "UNKNOWN")

    def test_wrong_attempt_result_is_unknown(self):
        for key, wrong in (("launch_request_id", "old"), ("attempt_id", 2), ("attempt_id", True),
                           ("task_revision", "2"), ("builder_id", "GLM")):
            result = {**self.record, "outcome": "CONFIRMED", "session_id": "session", key: wrong}
            self.assertEqual(cp.bound_result(result, self.record)["outcome"], "UNKNOWN")

    def test_stale_finalization_cannot_mutate_control_record(self):
        api = Mock()
        api.comments.return_value = [{"id": 1, "user": {"login": "github-actions[bot]"},
                                      "body": cp.render_control_record(self.record)}]
        with patch.object(cp, "load_config", return_value={"repository": "owner/repo", "control_record_actor": "github-actions[bot]"}), \
             patch.object(cp, "GithubApi", return_value=api), patch.dict(os.environ, {"GITHUB_TOKEN": "token"}):
            with self.assertRaises(cp.ControlPlaneError):
                cp.finalize_dispatch(1, Path("absent-result"), "old-request")
        api.update_comment.assert_not_called()

    def test_duplicate_returns_before_preflight(self):
        issue_url = "https://github.com/owner/repo/issues/1"
        envelope = {key: "value" for key in cp.REQUIRED_TASK_FIELDS}
        envelope.update(TASK_ID="T1", TASK_REVISION="1", BUILDER_ID="DEVIN",
                        CANONICAL_TASK_POINTER=issue_url, CONTROL_RECORD_POINTER=issue_url)
        api = Mock()
        api.issue.return_value = {"state": "open", "html_url": issue_url,
                                  "user": {"login": "owner"}, "body": "\n".join(f"{k}: {v}" for k,v in envelope.items())}
        api.comments.return_value = [{"id": 1, "user": {"login": "github-actions[bot]"},
                                      "body": cp.render_control_record({**self.record, "launch_state": "CONFIRMED"})}]
        cfg = {"repository": "owner/repo", "control_record_actor": "github-actions[bot]",
               "allowed_task_actors": ["owner"], "allowed_dispatch_actors": ["owner"]}
        with patch.object(cp, "load_config", return_value=cfg), patch.object(cp, "require_runtime_enabled"), \
             patch.object(cp, "validate_task"), patch.object(cp, "GithubApi", return_value=api), \
             patch.object(cp, "host_preflight") as preflight, patch.object(cp, "write_github_output"), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "token", "GITHUB_ACTOR": "owner", "GITHUB_TRIGGERING_ACTOR": "owner",
                 "EXPECTED_TASK_ID": "T1", "EXPECTED_TASK_REVISION": "1", "EXPECTED_BUILDER_ID": "DEVIN",
                 "EXPECTED_ISSUE_BODY_SHA256": hashlib.sha256(api.issue.return_value["body"].encode()).hexdigest()}):
            cp.prepare_dispatch(1, Path("must-not-write"))
        preflight.assert_not_called()
        api.update_comment.assert_not_called()

    def test_dispatch_binding_rejects_queued_edits_and_missing_authorization(self):
        body = "TASK_ID: T1\nTASK_REVISION: 1\nBUILDER_ID: DEVIN"
        envelope = dict(TASK_ID="T1", TASK_REVISION="1", BUILDER_ID="DEVIN")
        expected = dict(EXPECTED_TASK_ID="T1", EXPECTED_TASK_REVISION="1", EXPECTED_BUILDER_ID="DEVIN",
                        EXPECTED_ISSUE_BODY_SHA256=hashlib.sha256(body.encode()).hexdigest())
        with patch.dict(os.environ, expected, clear=True):
            cp.verify_dispatch_binding(body, envelope)
            for field in envelope:
                with self.assertRaises(cp.ControlPlaneError):
                    cp.verify_dispatch_binding(body, dict(envelope, **{field: "changed"}))
            with self.assertRaises(cp.ControlPlaneError):
                cp.verify_dispatch_binding(body + "\nnew instructions", envelope)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(cp.ControlPlaneError): cp.verify_dispatch_binding(body, envelope)

    def test_changed_runtime_cannot_reuse_old_activation(self):
        activation = {"runtime_enabled": True, "activated_runtime_sha": "a" * 40}
        responses = [subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 1)]
        with patch.object(cp, "load_activation", return_value=activation), patch.object(cp, "validate_repo"), \
             patch.object(cp.subprocess, "run", side_effect=responses):
            with self.assertRaises(cp.ControlPlaneError):
                cp.require_runtime_enabled()

    def test_real_git_binding_covers_cursor_and_astra_receiver_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'engineering'; root.mkdir()
            def git(*args):
                return subprocess.run(['git', '-c', 'user.name=Offline Test', '-c',
                    'user.email=offline@example.invalid', '-c', 'core.hooksPath=/dev/null', *args],
                    cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            git('init', '-q', '..')
            paths = ('scripts/control_plane_cursor.py', 'scripts/control_plane_flow_gateway.py',
                     '.github/control-plane/activation.json')
            for name in paths:
                path = root/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('baseline\n')
            git('add', '.'); git('commit', '-qm', 'audited')
            audited = git('rev-parse', 'HEAD')
            activation = activation_doc(runtime_enabled=True, activated_runtime_sha=audited)
            with patch.object(cp, 'ROOT', root), patch.object(cp, 'load_activation', return_value=activation), \
                 patch.object(cp, 'validate_repo'), patch.object(cp, 'load_config', return_value={'repository':'owner/repo'}):
                # An activation-only commit does not force an implementation-SHA chase.
                (root/paths[2]).write_text('activation only\n')
                git('add', '.'); git('commit', '-qm', 'activation')
                cp.require_runtime_enabled()
                for name in paths[:2]:
                    (root/name).write_text('changed implementation\n')
                    with self.subTest(path=name), self.assertRaises(cp.ControlPlaneError):
                        cp.require_runtime_enabled()
                    git('add', name); git('commit', '-qm', 'unreviewed source')
                    with self.subTest(committed=name), self.assertRaises(cp.ControlPlaneError):
                        cp.require_runtime_enabled()
                    (root/name).write_text('baseline\n')
                    git('add', name); git('commit', '-qm', 'restore fixture')

    def test_dirty_runtime_cannot_reuse_activation(self):
        activation = {"runtime_enabled": True, "activated_runtime_sha": "a" * 40}
        responses = [subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0),
                     subprocess.CompletedProcess([], 1)]
        with patch.object(cp, "load_activation", return_value=activation), patch.object(cp, "validate_repo"), \
             patch.object(cp.subprocess, "run", side_effect=responses):
            with self.assertRaises(cp.ControlPlaneError):
                cp.require_runtime_enabled()


class ActivationGateTests(unittest.TestCase):
    """Fault injection on activation.json parsing and gate ordering."""

    def write_doc(self, doc=None, raw=None):
        # load_json reports errors relative to the repo root, so activation
        # substitutes must live under ROOT as well.
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", dir=cp.ROOT, delete=False)
        self.addCleanup(os.unlink, handle.name)
        handle.write(raw if raw is not None else json.dumps(doc))
        handle.close()
        return Path(handle.name)

    def load(self, doc=None, raw=None):
        with patch.object(cp, "ACTIVATION_PATH", self.write_doc(doc, raw)):
            return cp.load_activation()

    def test_committed_activation_gate_consistency(self):
        # Committed activation may be disabled (pre-C) or enabled (post-C).
        # Shape rules hold either way; require_runtime_enabled follows the flag.
        activation = cp.load_activation()
        sha = activation["activated_runtime_sha"]
        self.assertTrue(
            sha == "PENDING" or (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha)),
            msg=f"unexpected activated_runtime_sha={sha!r}",
        )
        if activation["runtime_enabled"] is False:
            with self.assertRaises(cp.ControlPlaneError):
                cp.require_runtime_enabled()
        else:
            self.assertIs(activation["runtime_enabled"], True)
            self.assertRegex(sha, r"^[0-9a-f]{40}$")
            # Real require_runtime_enabled also runs validate_repo + git gates;
            # those are covered by RuntimeEnabledGateTests with fixtures.
            # Here only assert the committed doc is enable-shaped.
            self.assertEqual(activation["implementation_audit"], "PASS")
            self.assertEqual(activation["runner_preflight"], "PASS")
            self.assertEqual(activation["user_activation_approval"], "APPROVED")

    def test_missing_fields_and_bad_schema_rejected(self):
        doc = activation_doc()
        for field in sorted(doc):
            with self.subTest(missing=field):
                incomplete = {k: v for k, v in doc.items() if k != field}
                with self.assertRaises(cp.ControlPlaneError):
                    self.load(incomplete)
        for version in (0, 2, "1", None):
            with self.subTest(schema_version=version):
                with self.assertRaises(cp.ControlPlaneError):
                    self.load(activation_doc(schema_version=version))

    def test_non_object_and_malformed_json_rejected(self):
        for raw in ("[]", '"x"', "1", "null", "{invalid", ""):
            with self.subTest(raw=raw):
                with self.assertRaises(cp.ControlPlaneError):
                    self.load(raw=raw)
        with patch.object(cp, "ACTIVATION_PATH", cp.ROOT / "nonexistent-activation.json"):
            with self.assertRaises(cp.ControlPlaneError):
                cp.load_activation()

    def test_non_boolean_runtime_enabled_rejected(self):
        for value in ("true", "false", "yes", 1, 0, None, []):
            with self.subTest(runtime_enabled=value):
                with self.assertRaises(cp.ControlPlaneError):
                    self.load(activation_doc(runtime_enabled=value))

    def test_enabled_requires_every_gate_pass(self):
        for field, bad in (("user_activation_approval", "NOT_APPROVED"),
                           ("implementation_audit", "PENDING"),
                           ("runner_preflight", "PENDING")):
            with self.subTest(field=field):
                with self.assertRaises(cp.ControlPlaneError):
                    self.load(activation_doc(runtime_enabled=True, **{field: bad}))
        with self.assertRaises(cp.ControlPlaneError):
            self.load(activation_doc(runtime_enabled=True, user_activation_approval="NOT_APPROVED",
                                     implementation_audit="PENDING", runner_preflight="PENDING"))
        # A disabled record may hold non-passing gates; enabling may not.
        doc = self.load(activation_doc(runtime_enabled=False, user_activation_approval="NOT_APPROVED",
                                       implementation_audit="PENDING", runner_preflight="PENDING"))
        self.assertFalse(doc["runtime_enabled"])

    def test_invalid_gate_values_rejected(self):
        for field in ("user_activation_approval", "implementation_audit", "runner_preflight"):
            for bad in ("PASS_WITH_NOTES", "yes", "", None, 1):
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(cp.ControlPlaneError):
                        self.load(activation_doc(**{field: bad}))

    def test_approved_without_durable_pointer_rejected(self):
        for bad_pointer in (None, "", "PENDING", "PENDING_RUNTIME_PR_POINTER"):
            with self.subTest(pointer=bad_pointer):
                with self.assertRaises(cp.ControlPlaneError):
                    self.load(activation_doc(user_activation_approval_pointer=bad_pointer))


class RuntimeEnabledGateTests(unittest.TestCase):
    """require_runtime_enabled / validate_repo fail-closed ordering."""

    def test_disabled_never_touches_git_or_repo_validation(self):
        # Fixture disabled activation — must not depend on committed enable state.
        disabled = activation_doc(runtime_enabled=False)
        with patch.object(cp, "load_activation", return_value=disabled), \
             patch.object(cp, "validate_repo") as validate, \
             patch.object(cp.subprocess, "run") as run:
            with self.assertRaises(cp.ControlPlaneError):
                cp.require_runtime_enabled()
            validate.assert_not_called()
            run.assert_not_called()

    def test_activated_sha_shape_enforced_before_git(self):
        for bad in ("PENDING", "", "a" * 39, "a" * 41, "a" * 64, "A" * 40,
                    "g" * 40, "a" * 39 + "g", " a" * 20, 123, None, ["a" * 40]):
            with self.subTest(sha=bad):
                activation = activation_doc(runtime_enabled=True, activated_runtime_sha=bad)
                with patch.object(cp, "load_activation", return_value=activation), \
                     patch.object(cp, "validate_repo"), \
                     patch.object(cp.subprocess, "run") as run:
                    with self.assertRaises(cp.ControlPlaneError):
                        cp.require_runtime_enabled()
                    run.assert_not_called()

    def test_non_ancestor_audited_sha_rejected_first(self):
        activation = activation_doc(runtime_enabled=True, activated_runtime_sha="b" * 40)
        fail = subprocess.CompletedProcess([], 1)
        with patch.object(cp, "load_activation", return_value=activation), \
             patch.object(cp, "validate_repo"), \
             patch.object(cp, "load_config", return_value={"repository":"owner/repo"}), \
             patch.object(cp.subprocess, "run", return_value=fail) as run:
            with self.assertRaises(cp.ControlPlaneError):
                cp.require_runtime_enabled()
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][:3], ["git", "merge-base", "--is-ancestor"])

    def test_all_git_gates_pass_returns_activation(self):
        activation = activation_doc(runtime_enabled=True, activated_runtime_sha="b" * 40)
        ok = subprocess.CompletedProcess([], 0)
        with patch.object(cp, "load_activation", return_value=activation), \
             patch.object(cp, "validate_repo"), \
             patch.object(cp, "load_config", return_value={"repository":"owner/repo"}), \
             patch.object(cp.subprocess, "run", return_value=ok) as run:
            self.assertIs(cp.require_runtime_enabled(), activation)
            self.assertEqual(run.call_count, 3)

    def test_enabled_pending_sha_accepted_by_loader_but_denied_later(self):
        # The loader admits runtime_enabled=true once all three gates PASS even
        # with activated_runtime_sha=PENDING; both downstream gates must deny.
        enabled = activation_doc(runtime_enabled=True, activated_runtime_sha="PENDING")
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", dir=cp.ROOT, delete=False)
        self.addCleanup(os.unlink, handle.name)
        handle.write(json.dumps(enabled))
        handle.close()
        with patch.object(cp, "ACTIVATION_PATH", Path(handle.name)):
            doc = cp.load_activation()
        self.assertTrue(doc["runtime_enabled"])
        env = {"GITHUB_REPOSITORY": "BeautifulMind-JT/ai-ops-control-plane"}
        with patch.object(cp, "load_activation", return_value=doc), \
             patch.object(cp, "validate_repo"), \
             patch.object(cp.subprocess, "run") as run:
            with self.assertRaises(cp.ControlPlaneError):
                cp.require_runtime_enabled()
            run.assert_not_called()
        with patch.object(cp, "load_activation", return_value=doc), \
             patch.dict(os.environ, env):
            with self.assertRaises(cp.ControlPlaneError):
                cp.validate_repo()

    def test_validate_repo_enabled_requires_sha_and_pointers(self):
        enabled = activation_doc(runtime_enabled=True, activated_runtime_sha="b" * 40)
        env = {"GITHUB_REPOSITORY": "BeautifulMind-JT/ai-ops-control-plane"}
        for field, bad in (("activated_runtime_sha", "PENDING"),
                           ("activated_runtime_sha", ""),
                           ("activated_runtime_sha", None),
                           ("implementation_audit_pointer", "PENDING"),
                           ("implementation_audit_pointer", ""),
                           ("runner_preflight_pointer", None)):
            with self.subTest(field=field, bad=bad):
                doc = dict(enabled, **{field: bad})
                with patch.object(cp, "load_activation", return_value=doc), \
                     patch.dict(os.environ, env):
                    with self.assertRaises(cp.ControlPlaneError):
                        cp.validate_repo()

    def test_validate_repo_succeeds_while_disabled(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "BeautifulMind-JT/ai-ops-control-plane"}):
            cp.validate_repo()


class PrepareDispatchReplayTests(unittest.TestCase):
    """Existing control records fence redispatch; nothing creates a second writer."""

    def setUp(self):
        self.issue_url = "https://github.com/owner/repo/issues/1"
        self.envelope = {key: "value" for key in cp.REQUIRED_TASK_FIELDS}
        self.envelope.update(TASK_ID="T1", TASK_REVISION="1", BUILDER_ID="DEVIN",
                             CANONICAL_TASK_POINTER=self.issue_url,
                             CONTROL_RECORD_POINTER=self.issue_url)
        self.record = dict(repository="owner/repo", task_id="T1", task_revision="1",
                           builder_id="DEVIN", launch_request_id="request-1",
                           attempt_id=1, launch_state="SUBMITTING", owner_session_id=None)

    def comment(self, record, actor="github-actions[bot]"):
        return {"id": 1, "user": {"login": actor},
                "body": cp.render_control_record(record)}

    def run_prepare(self, comments, create_id=5, create_actor="github-actions[bot]",
                    attempt=None, host_state=None, delete_error=None):
        body = "\n".join(f"{k}: {v}" for k, v in self.envelope.items())
        api = Mock()
        api.issue.return_value = {"state": "open", "html_url": self.issue_url,
                                  "user": {"login": "owner"}, "body": body}
        api.comments.return_value = comments
        api.create_comment.return_value = {"id": create_id, "user": {"login": create_actor}}
        api.delete_comment.side_effect = delete_error
        cfg = {"repository": "owner/repo", "project": "KIX",
               "control_record_actor": "github-actions[bot]",
               "allowed_task_actors": ["owner"], "allowed_dispatch_actors": ["owner"]}
        env = {"GITHUB_TOKEN": "token", "GITHUB_ACTOR": "owner",
               "GITHUB_TRIGGERING_ACTOR": "owner", "EXPECTED_TASK_ID": self.envelope["TASK_ID"],
               "EXPECTED_TASK_REVISION": self.envelope["TASK_REVISION"],
               "EXPECTED_BUILDER_ID": self.envelope["BUILDER_ID"],
               "EXPECTED_ISSUE_BODY_SHA256": hashlib.sha256(body.encode()).hexdigest(),
               "EXPECTED_ATTEMPT_ID": "" if attempt is None else str(attempt)}
        status = lambda args: {"status": "FOUND" if host_state else "NOT_FOUND",
                               "launch_request_id": args[2], "state": host_state}
        result = {"api": api}
        with tempfile.TemporaryDirectory() as tmp:
            packet = Path(tmp) / "packet.json"
            with patch.object(cp, "load_config", return_value=cfg), \
                 patch.object(cp, "require_runtime_enabled"), \
                 patch.object(cp, "validate_task"), \
                 patch.object(cp, "GithubApi", return_value=api), \
                 patch.object(cp, "host_preflight") as preflight, \
                 patch.object(cp, "host_call", side_effect=status) as host, \
                 patch.object(cp, "write_github_output") as output, \
                 patch.dict(os.environ, env):
                try:
                    cp.prepare_dispatch(1, packet)
                    result["error"] = None
                except cp.ControlPlaneError as exc:
                    result["error"] = exc
            result["packet"] = packet.read_text() if packet.exists() else None
        result["preflight"] = preflight
        result["host"] = host
        result["output"] = output
        return result

    def written_records(self, result):
        return [cp.parse_control_record(call.args[1]) for call in result["api"].update_comment.call_args_list]

    def test_unresolved_submitting_and_unknown_block_redispatch(self):
        for state in ("SUBMITTING", "UNKNOWN"):
            with self.subTest(state=state):
                result = self.run_prepare([self.comment({**self.record, "launch_state": state})])
                self.assertIsNotNone(result["error"])
                self.assertIn(state, str(result["error"]))
                result["preflight"].assert_not_called()
                result["api"].update_comment.assert_not_called()
                result["api"].create_comment.assert_not_called()
                self.assertIsNone(result["packet"])

    def test_failed_prestart_requires_explicit_retry_authorization(self):
        result = self.run_prepare([self.comment({**self.record, "launch_state": "FAILED_PRESTART"})])
        self.assertIsNotNone(result["error"])
        result["preflight"].assert_not_called()
        result["api"].update_comment.assert_not_called()
        self.assertIsNone(result["packet"])

    def test_unrecognized_launch_state_rejected(self):
        result = self.run_prepare([self.comment({**self.record, "launch_state": "LAUNCHED"})])
        self.assertIsNotNone(result["error"])
        result["preflight"].assert_not_called()
        self.assertIsNone(result["packet"])

    def test_foreign_actor_marker_cannot_create_record(self):
        result = self.run_prepare([self.comment({**self.record, "launch_state": "CONFIRMED"},
                                              actor="attacker")])
        self.assertIsNotNone(result["error"])
        result["api"].create_comment.assert_not_called()
        result["api"].update_comment.assert_not_called()
        result["preflight"].assert_not_called()
        self.assertIsNone(result["packet"])

    def test_control_record_identity_mismatch_rejected(self):
        for field, wrong in (("task_id", "T2"), ("task_revision", "2"), ("builder_id", "GLM")):
            with self.subTest(field=field):
                record = {**self.record, "launch_state": "NOT_STARTED", field: wrong}
                result = self.run_prepare([self.comment(record)])
                self.assertIsNotNone(result["error"])
                result["preflight"].assert_not_called()
                result["api"].update_comment.assert_not_called()
                self.assertIsNone(result["packet"])

    def test_confirmed_owner_returns_without_launch(self):
        result = self.run_prepare([self.comment({**self.record, "launch_state": "CONFIRMED"})])
        self.assertIsNone(result["error"])
        result["preflight"].assert_not_called()
        result["api"].update_comment.assert_not_called()
        result["output"].assert_called_once_with("launch_required", "false")
        self.assertIsNone(result["packet"])

    def test_not_started_record_moves_to_submitting_once(self):
        result = self.run_prepare([self.comment({**self.record, "launch_state": "NOT_STARTED"})])
        self.assertIsNone(result["error"])
        result["preflight"].assert_called_once_with("DEVIN")
        updated = cp.parse_control_record(result["api"].update_comment.call_args.args[1])
        self.assertEqual(updated["launch_state"], "SUBMITTING")
        packet = json.loads(result["packet"])
        self.assertEqual(packet["launch_request_id"], "request-1")
        self.assertEqual(packet["control_comment_id"], 1)

    def test_fresh_dispatch_persists_not_started_then_submitting(self):
        result = self.run_prepare([])
        self.assertIsNone(result["error"])
        created = cp.parse_control_record(result["api"].create_comment.call_args.args[1])
        self.assertEqual(created["launch_state"], "NOT_STARTED")
        updated = cp.parse_control_record(result["api"].update_comment.call_args.args[1])
        self.assertEqual(updated["launch_state"], "SUBMITTING")
        packet = json.loads(result["packet"])
        self.assertEqual(packet["control_comment_id"], 5)
        self.assertEqual(packet["task_id"], "T1")

    def test_record_written_under_another_login_is_removed_before_launch(self):
        # A PAT writes as the user, not github-actions[bot]; finalize would later
        # reject that record as foreign and strand the task in SUBMITTING.
        result = self.run_prepare([], create_actor="BeautifulMind-JT")
        self.assertIn("control_record_actor", str(result["error"]))
        result["api"].delete_comment.assert_called_once_with(5)
        result["api"].update_comment.assert_not_called()
        result["preflight"].assert_not_called()
        self.assertIsNone(result["packet"])

    def test_undeletable_foreign_record_names_the_comment_to_remove(self):
        result = self.run_prepare([], create_actor="BeautifulMind-JT",
                                  delete_error=cp.ControlPlaneError("GitHub API DELETE failed"))
        self.assertIn("delete NOT_STARTED comment 5 manually", str(result["error"]))
        result["preflight"].assert_not_called()
        self.assertIsNone(result["packet"])

    def test_failed_prestart_error_names_next_attempt(self):
        result = self.run_prepare([self.comment({**self.record, "launch_state": "FAILED_PRESTART"})])
        self.assertIn("expected_attempt_id=2", str(result["error"]))

    def test_explicit_retry_after_failed_prestart_starts_next_attempt(self):
        failed = {**self.record, "launch_state": "FAILED_PRESTART", "last_error": "host max_active_sessions reached"}
        result = self.run_prepare([self.comment(failed)], attempt=2)
        self.assertIsNone(result["error"])
        fresh, submitting = self.written_records(result)
        self.assertEqual((fresh["attempt_id"], fresh["launch_state"]), (2, "NOT_STARTED"))
        self.assertEqual((submitting["attempt_id"], submitting["launch_state"]), (2, "SUBMITTING"))
        request = cp.stable_id("launch", "owner/repo", "T1", "1", 2)
        self.assertEqual(submitting["launch_request_id"], request)
        self.assertNotEqual(request, self.record["launch_request_id"])
        self.assertEqual(submitting["claim_id"], cp.stable_id("claim", "owner/repo", "T1", "1", 2))
        self.assertEqual(submitting["previous_attempts"], [{
            "attempt_id": 1, "launch_request_id": "request-1", "task_revision": "1", "builder_id": "DEVIN",
            "launch_state": "FAILED_PRESTART", "owner_session_id": None,
            "last_error": "host max_active_sessions reached"}])
        packet = json.loads(result["packet"])
        self.assertEqual((packet["attempt_id"], packet["launch_request_id"]), (2, request))
        result["preflight"].assert_called_once_with("DEVIN")
        result["host"].assert_not_called()

    def test_repeated_retry_dispatch_resolves_to_the_same_attempt(self):
        second = {**self.record, "attempt_id": 2, "launch_request_id": "request-2",
                  "previous_attempts": [{"attempt_id": 1}]}
        confirmed = self.run_prepare([self.comment({**second, "launch_state": "CONFIRMED"})], attempt=2)
        self.assertIsNone(confirmed["error"])
        confirmed["output"].assert_called_once_with("launch_required", "false")
        confirmed["api"].update_comment.assert_not_called()
        for state in ("SUBMITTING", "UNKNOWN"):
            with self.subTest(state=state):
                blocked = self.run_prepare([self.comment({**second, "launch_state": state})], attempt=2)
                self.assertIn(state, str(blocked["error"]))
                blocked["api"].update_comment.assert_not_called()
                blocked["preflight"].assert_not_called()
        resumed = self.run_prepare([self.comment({**second, "launch_state": "NOT_STARTED"})], attempt=2)
        self.assertIsNone(resumed["error"])
        self.assertEqual(json.loads(resumed["packet"])["launch_request_id"], "request-2")

    def test_retry_never_displaces_confirmed_owner(self):
        result = self.run_prepare([self.comment({**self.record, "launch_state": "CONFIRMED",
                                                 "owner_session_id": "session-1"})], attempt=2)
        self.assertIn("CONFIRMED owner", str(result["error"]))
        result["api"].update_comment.assert_not_called()
        result["preflight"].assert_not_called()
        self.assertIsNone(result["packet"])

    def test_retry_of_unresolved_attempt_requires_host_fencing(self):
        for state in ("SUBMITTING", "UNKNOWN"):
            for host_state in (None, "SUBMITTING", "UNKNOWN", "CONFIRMED"):
                with self.subTest(state=state, host_state=host_state):
                    result = self.run_prepare([self.comment({**self.record, "launch_state": state})],
                                              attempt=2, host_state=host_state)
                    self.assertIn("not fenced", str(result["error"]))
                    result["host"].assert_called_once_with(["status", "--launch-request-id", "request-1"])
                    result["api"].update_comment.assert_not_called()
                    result["preflight"].assert_not_called()
                    self.assertIsNone(result["packet"])
            for host_state in ("RECONCILED", "FAILED_PRESTART"):
                with self.subTest(state=state, host_state=host_state):
                    result = self.run_prepare([self.comment({**self.record, "launch_state": state})],
                                              attempt=2, host_state=host_state)
                    self.assertIsNone(result["error"])
                    self.assertEqual(json.loads(result["packet"])["attempt_id"], 2)
                    self.assertEqual(self.written_records(result)[-1]["previous_attempts"][0]["launch_state"], state)

    def test_host_status_for_another_request_is_rejected(self):
        with patch.object(cp, "host_call", return_value={"launch_request_id": "other", "state": "RECONCILED"}):
            with self.assertRaisesRegex(cp.ControlPlaneError, "identity mismatch"):
                cp.host_request_state("request-1")
        with patch.object(cp, "host_call", return_value={"launch_request_id": "request-1", "state": 3}):
            with self.assertRaises(cp.ControlPlaneError):
                cp.host_request_state("request-1")

    def test_retry_rebinds_pinned_revision_and_builder_but_not_task(self):
        failed = {**self.record, "launch_state": "FAILED_PRESTART"}
        self.envelope.update(TASK_REVISION="2", BUILDER_ID="GLM")
        result = self.run_prepare([self.comment(failed)], attempt=2)
        self.assertIsNone(result["error"])
        packet = json.loads(result["packet"])
        self.assertEqual((packet["task_revision"], packet["builder_id"], packet["attempt_id"]), ("2", "GLM", 2))
        result["preflight"].assert_called_once_with("GLM")
        # Without the explicit retry the same drift stays an identity mismatch.
        self.assertIsNotNone(self.run_prepare([self.comment(failed)])["error"])
        other_task = self.run_prepare([self.comment({**failed, "task_id": "T2"})], attempt=2)
        self.assertIn("identity", str(other_task["error"]))
        other_task["api"].update_comment.assert_not_called()

    def test_attempt_number_must_name_current_or_next_attempt(self):
        failed = self.comment({**self.record, "launch_state": "FAILED_PRESTART"})
        for attempt in (3, 7):
            with self.subTest(attempt=attempt):
                result = self.run_prepare([failed], attempt=attempt)
                self.assertIn("a retry names attempt 2", str(result["error"]))
                result["api"].update_comment.assert_not_called()
        self.assertIn("expected_attempt_id=2", str(self.run_prepare([failed], attempt=1)["error"]))
        fresh = self.run_prepare([], attempt=2)
        self.assertIn("first attempt", str(fresh["error"]))
        fresh["api"].create_comment.assert_not_called()
        self.assertIsNone(self.run_prepare([], attempt=1)["error"])

    def test_malformed_attempt_inputs_are_rejected_before_any_write(self):
        for raw in ("0", "-1", "abc", "1.0", " 1", "01", "1" * 10):
            with self.subTest(raw=raw), patch.dict(os.environ, {"EXPECTED_ATTEMPT_ID": raw}):
                with self.assertRaises(cp.ControlPlaneError):
                    cp.expected_attempt_id()
        for bad in (0, True, "1", None, 1.0):
            with self.subTest(attempt_id=bad):
                result = self.run_prepare([self.comment({**self.record, "attempt_id": bad,
                                                         "launch_state": "FAILED_PRESTART"})], attempt=2)
                self.assertIn("attempt_id", str(result["error"]))
                result["api"].update_comment.assert_not_called()


class CentralProfileTests(unittest.TestCase):
    """Every central target shares ASTRA_CONTROL_GITHUB_TOKEN, hence one record author."""

    def test_every_committed_profile_loads_with_one_record_actor(self):
        profiles = cp.load_json(cp.CONFIG_PATH.with_name("projects.json"))
        self.assertGreaterEqual(len(profiles), 4)
        actors = set()
        for target in profiles:
            with self.subTest(target=target), patch.dict(os.environ, {"ASTRA_TARGET_REPOSITORY": target}):
                actors.add(cp.load_config()["control_record_actor"])
        self.assertEqual(len(actors), 1)
        cp.validate_central_profiles()

    def write_profiles(self, mutate):
        directory = tempfile.TemporaryDirectory(dir=cp.ROOT)
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "config.json").write_text(cp.CONFIG_PATH.read_text())
        profiles = cp.load_json(cp.CONFIG_PATH.with_name("projects.json"))
        mutate(profiles)
        (root / "projects.json").write_text(json.dumps(profiles))
        return root / "config.json"

    def test_divergent_record_actor_is_rejected(self):
        def diverge(profiles):
            profiles["BeautifulMind-JT/ZARI"]["control_record_actor"] = "github-actions[bot]"
        with patch.object(cp, "CONFIG_PATH", self.write_profiles(diverge)):
            with self.assertRaisesRegex(cp.ControlPlaneError, "control_record_actor must match"):
                cp.validate_central_profiles()

    def test_invalid_non_selected_profile_is_rejected(self):
        mutations = (
            lambda p: p["BeautifulMind-JT/maeum-gyeol"].update(enabled_builders=["UNKNOWN"]),
            lambda p: p["BeautifulMind-JT/maeum-gyeol"].update(repository="BeautifulMind-JT/other"),
            lambda p: p["BeautifulMind-JT/maeum-gyeol"].pop("control_record_actor"),
            lambda p: p.update({"BeautifulMind-JT/extra": []}),
            lambda p: p.clear(),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index), patch.object(cp, "CONFIG_PATH", self.write_profiles(mutate)):
                with self.assertRaises(cp.ControlPlaneError):
                    cp.validate_central_profiles()


class GithubApiErrorTests(unittest.TestCase):
    def test_network_and_decode_failures_become_control_plane_errors(self):
        api = cp.GithubApi("owner/repo", "token")
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"not json"
        for effect in (cp.urllib.error.URLError("dns"), TimeoutError("timed out"), ConnectionResetError("reset")):
            with self.subTest(effect=type(effect).__name__), \
                 patch.object(cp.urllib.request, "urlopen", side_effect=effect):
                with self.assertRaisesRegex(cp.ControlPlaneError, "unavailable"):
                    api.issue(1)
        with patch.object(cp.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(cp.ControlPlaneError, "invalid JSON"):
                api.issue(1)


class DisabledRuntimeTests(unittest.TestCase):
    """When runtime_enabled=false, no launch path reaches the host."""

    def setUp(self):
        self.disabled = activation_doc(runtime_enabled=False)

    def packet(self, directory):
        path = Path(directory) / "packet.json"
        path.write_text(json.dumps({
            "repository": "owner/repo", "task_id": "T1", "task_revision": "1",
            "builder_id": "DEVIN", "launch_request_id": "request-1", "attempt_id": 1}))
        return path

    def test_prepare_consumes_no_authority_while_disabled(self):
        # Fixture disabled activation — independent of committed enable state.
        with tempfile.TemporaryDirectory() as tmp:
            packet = Path(tmp) / "packet.json"
            with patch.object(cp, "load_activation", return_value=self.disabled), \
                 patch.object(cp, "GithubApi") as api, \
                 patch.object(cp, "host_preflight") as preflight, \
                 patch.object(cp, "host_call") as host, \
                 patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(cp.ControlPlaneError):
                    cp.prepare_dispatch(1, packet)
            api.assert_not_called()
            preflight.assert_not_called()
            host.assert_not_called()
            self.assertFalse(packet.exists())

    def test_launch_result_is_unknown_and_claims_no_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            packet, result = self.packet(tmp), Path(tmp) / "result.json"
            with patch.object(cp, "load_activation", return_value=self.disabled), \
                 patch.object(cp, "host_call") as host:
                cp.launch_dispatch(packet, result)
            host.assert_not_called()
            outcome = json.loads(result.read_text())
            self.assertEqual(outcome["outcome"], "UNKNOWN")
            self.assertEqual(outcome["launch_request_id"], "request-1")
            self.assertNotIn("session_id", outcome)


class FinalizeDispatchTests(unittest.TestCase):
    def setUp(self):
        self.record = dict(repository="owner/repo", task_id="T1", task_revision="1",
                           builder_id="DEVIN", launch_request_id="request-1",
                           attempt_id=1, launch_state="SUBMITTING", owner_session_id=None)

    def run_finalize(self, result_payload=None, raw=None):
        api = Mock()
        api.comments.return_value = [{"id": 7, "user": {"login": "github-actions[bot]"},
                                      "body": cp.render_control_record(self.record)}]
        cfg = {"repository": "owner/repo", "control_record_actor": "github-actions[bot]"}
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "result.json"
            result_path.write_text(raw if raw is not None else json.dumps(result_payload))
            with patch.object(cp, "load_config", return_value=cfg), \
                 patch.object(cp, "GithubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "token"}):
                state = cp.finalize_dispatch(1, result_path, "request-1")
        written = api.update_comment.call_args.args[1] if api.update_comment.called else None
        return state, cp.parse_control_record(written) if written else None

    def identity_result(self, **extra):
        return {**{k: self.record[k] for k in cp.LAUNCH_IDENTITY}, **extra}

    def test_confirmed_without_session_id_degrades_to_unknown(self):
        state, record = self.run_finalize(self.identity_result(outcome="CONFIRMED"))
        self.assertEqual(state, "UNKNOWN")
        self.assertEqual(record["launch_state"], "UNKNOWN")
        self.assertIsNone(record["owner_session_id"])

    def test_confirmed_binds_durable_session(self):
        state, record = self.run_finalize(
            self.identity_result(outcome="CONFIRMED", session_id="session-9"))
        self.assertEqual(state, "CONFIRMED")
        self.assertEqual(record["owner_session_id"], "session-9")

    def test_malformed_result_file_is_unknown_not_confirmed(self):
        state, record = self.run_finalize(raw="not json {")
        self.assertEqual(state, "UNKNOWN")
        self.assertEqual(record["launch_state"], "UNKNOWN")

    def test_identity_mismatch_result_is_unknown(self):
        state, record = self.run_finalize(
            self.identity_result(outcome="CONFIRMED", session_id="s", attempt_id=2))
        self.assertEqual(state, "UNKNOWN")
        self.assertIsNone(record["owner_session_id"])


class HostPreflightTests(unittest.TestCase):
    """A PASS-looking host report with wrong metadata must still fail."""

    def setUp(self):
        self.cfg = {"repository": "owner/repo", "enabled_builders": ["DEVIN"]}
        self.report = {
            "host_admission": "ENFORCED",
            "boundary_evidence_pointer": "evidence",
            "helper_sha256": hashlib.sha256(
                (cp.ROOT / "scripts/control_plane_host.py").read_bytes()).hexdigest(),
            "allowed_repositories": ["owner/repo"],
            "status": "PASS",
            "builder_id": "DEVIN",
            "execution_mode": "PERSISTENT_SUPERVISOR",
            "parallel_safe": True,
            "worktree_root": "/work/devin",
        }

    def run_preflight(self, reports, cfg=None, builder="DEVIN"):
        with patch.object(cp, "load_config", return_value=cfg or self.cfg), \
             patch.object(cp, "host_call",
                          side_effect=reports if isinstance(reports, list) else [reports]), \
             contextlib.redirect_stdout(io.StringIO()):
            cp.host_preflight(builder)

    def test_valid_report_passes(self):
        self.run_preflight(self.report)

    def test_metadata_faults_fail(self):
        faults = (
            {"host_admission": "OBSERVED"},
            {"boundary_evidence_pointer": None},
            {"helper_sha256": "0" * 64},
            {"allowed_repositories": ["other/repo"]},
            {"status": "PASS_WITH_NOTES"},
            {"builder_id": "GROK_BUILD"},
            {"execution_mode": "EPHEMERAL"},
            {"parallel_safe": False},
        )
        for fault in faults:
            with self.subTest(fault=fault):
                report = dict(self.report, **fault)
                with self.assertRaises(cp.ControlPlaneError):
                    self.run_preflight(report)

    def test_host_error_and_disabled_builder_fail(self):
        with self.assertRaises(cp.ControlPlaneError):
            self.run_preflight(cp.ControlPlaneError("helper unreachable"))
        with self.assertRaises(cp.ControlPlaneError):
            self.run_preflight(self.report, builder="GLM")

    def test_overlapping_worktree_roots_rejected(self):
        cfg = {"repository": "owner/repo", "enabled_builders": ["DEVIN", "GROK_BUILD"]}
        reports = [dict(self.report, worktree_root="/same"),
                   dict(self.report, builder_id="GROK_BUILD", worktree_root="/same")]
        with self.assertRaises(cp.ControlPlaneError):
            self.run_preflight(reports, cfg=cfg, builder=None)


if __name__ == "__main__":
    unittest.main()
