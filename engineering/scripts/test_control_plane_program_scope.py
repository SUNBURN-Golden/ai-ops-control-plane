"""Candidate registration boundaries; fake GitHub only, no host/provider calls."""
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import control_plane as cp
import control_plane_program as prog


REPOSITORY = "BeautifulMind-JT/ZARI"
PLAN_COMMIT = "a" * 40
OLD_PLAN_COMMIT = "b" * 40
CONFIG = {"repository": REPOSITORY, "project": "ZARI"}


def legacy_plan():
    return {"schema_version": 1, "program": "zari", "repository": REPOSITORY, "project": "ZARI",
            "approval_pointer": "https://github.com/BeautifulMind-JT/ZARI/pull/35",
            "authoritative_doc_pointers": "docs/DEVIN_EXECUTION_PLAN.md",
            "nodes": [{"id": "sp-1", "title": "Approved original node", "spec": "Original scope.",
                       "audit_floor": "A1", "astra_gate": "NONE"}]}


class PlanAPI:
    def __init__(self, plan):
        self.plan = plan
        self.reads = []

    def _request(self, method, path):
        if method == "GET" and path == "":
            return {"default_branch": "main"}
        if method == "GET" and path == f"/compare/main...{PLAN_COMMIT}":
            return {"status": "behind"}
        if method == "GET" and path.startswith("/commits?sha=main&path="):
            return [{"sha": PLAN_COMMIT}]
        if method != "GET" or path != f"/contents/{prog.PLAN_PATH}?ref={PLAN_COMMIT}":
            raise AssertionError("unexpected API access beyond the exact plan read")
        self.reads.append(path)
        return {"content": base64.b64encode(json.dumps(self.plan).encode()).decode()}


class RegistrationScopeTests(unittest.TestCase):
    def test_any_scope_marker_refuses_before_validation_including_fabricated_approval(self):
        for marker in ({"state": "PENDING", "manifest_path": ".aiops/registration-scope.json"},
                       {"state": "APPROVED", "manifest_sha256": "f" * 64},
                       {"state": "ADOPTED"}, None, False, True, "", "APPROVED", [], {}):
            api = PlanAPI({**legacy_plan(), "registration_scope": marker})
            with self.subTest(marker=marker), patch.object(prog, "validate_plan") as validate:
                with self.assertRaisesRegex(prog.ProgramError, "full-scope registration reader is not adopted"):
                    prog.load_plan(api, CONFIG, PLAN_COMMIT)
                validate.assert_not_called()
                self.assertEqual(len(api.reads), 1)

    def test_replacing_only_approval_pointer_cannot_activate_marked_candidate(self):
        for pointer in (legacy_plan()["approval_pointer"], "APPROVED BY USER", "User-approved exact scope",
                        "https://github.com/BeautifulMind-JT/ai-ops-control-plane/issues/999"):
            api = PlanAPI({**legacy_plan(), "approval_pointer": pointer,
                           "registration_scope": {"state": "PENDING", "manifest_sha256": "c" * 64}})
            with self.subTest(pointer=pointer), self.assertRaisesRegex(prog.ProgramError, "registration_scope"):
                prog.load_plan(api, CONFIG, PLAN_COMMIT)

    def test_unmarked_legacy_plan_keeps_existing_validation_and_pending_hold(self):
        value = prog.load_plan(PlanAPI(legacy_plan()), CONFIG, PLAN_COMMIT)
        self.assertEqual(value["program"], "zari")
        self.assertEqual(value["nodes"][0]["id"], "sp-1")
        self.assertEqual(value["nodes"][0]["audit_floor"], "A1")
        with self.assertRaisesRegex(prog.ProgramError, "approval is pending"):
            prog.load_plan(PlanAPI({**legacy_plan(), "approval_pointer": "PENDING"}), CONFIG, PLAN_COMMIT)

    def test_marked_candidate_cannot_materialize_issue_or_change_host_ledger(self):
        api = PlanAPI({**legacy_plan(), "registration_scope": {"state": "APPROVED"}})
        with patch.object(cp, "load_config", return_value=CONFIG), \
                patch.object(cp, "require_runtime_enabled"), patch.object(prog, "api_for", return_value=api), \
                patch.object(prog, "host") as host:
            with self.assertRaisesRegex(prog.ProgramError, "registration_scope"):
                prog.materialize("zari", "sp-1", PLAN_COMMIT)
            host.assert_not_called()
        self.assertEqual(len(api.reads), 1)

    def test_marked_candidate_cannot_advance_start_or_call_provider_preflight(self):
        api = PlanAPI({**legacy_plan(), "registration_scope": {"state": "PENDING"}})
        status = {"status": "CREATED", "issue": 42, "plan_commit": OLD_PLAN_COMMIT, "request": "d" * 24}
        preflight = Mock(side_effect=AssertionError("provider preflight must not run"))
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(cp, "load_config", return_value=CONFIG), \
                patch.object(cp, "require_runtime_enabled"), patch.object(prog, "api_for", return_value=api), \
                patch.object(prog, "host", return_value=copy.deepcopy(status)) as host:
            packet = Path(directory) / "packet.json"
            with self.assertRaisesRegex(prog.ProgramError, "registration_scope"):
                prog.start(42, "zari", "sp-1", PLAN_COMMIT, packet, preflight=preflight)
            host.assert_called_once_with(["materialize-status", "--program", "zari", "--node", "sp-1"])
            preflight.assert_not_called()
            self.assertFalse(packet.exists())
        self.assertEqual(len(api.reads), 1)


if __name__ == "__main__":
    unittest.main()
