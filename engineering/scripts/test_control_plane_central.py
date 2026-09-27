import json, os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import control_plane as cp

class CentralIdentityTests(unittest.TestCase):
    def test_peer_eligibility_does_not_bypass_disabled_activation(self):
        for repo in ["kix-protocol", "ZARI", "film-unit-mv-studio", "maeum-gyeol"]:
            with self.subTest(repo=repo), tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"ASTRA_TARGET_REPOSITORY": "BeautifulMind-JT/" + repo}
            ), patch.object(cp, "GithubApi") as api, patch.object(cp, "host_call") as host:
                self.assertIs(cp.load_config()["deployment_enabled"], True)
                root = Path(directory)
                packet = root / "packet.json"
                with self.assertRaisesRegex(cp.ControlPlaneError, "runtime_enabled=false"):
                    cp.prepare_dispatch(1, packet)
                self.assertFalse(packet.exists())
                identity = dict(repository="BeautifulMind-JT/" + repo, task_id="DIAG",
                    task_revision="1", builder_id="DEVIN", launch_request_id="r1", attempt_id=1)
                packet.write_text(json.dumps(identity))
                result = root / "result.json"
                cp.launch_dispatch(packet, result)
                self.assertEqual(json.loads(result.read_text())["outcome"], "UNKNOWN")
                api.assert_not_called()
                host.assert_not_called()

    def test_flipping_only_global_boolean_cannot_restore_stale_pass(self):
        # Exercise the actual candidate record: stale audit/preflight/approval
        # evidence was cleared, so changing only runtime_enabled must fail.
        activation = cp.load_activation()
        activation["runtime_enabled"] = True
        with patch.object(cp, "load_json", return_value=activation):
            with self.assertRaisesRegex(cp.ControlPlaneError, "without all activation gates"):
                cp.load_activation()

    def test_explicit_target_required(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(cp.ControlPlaneError): cp.load_config()
    def test_unknown_target_rejected(self):
        with patch.dict(os.environ, {"ASTRA_TARGET_REPOSITORY":"BeautifulMind-JT/beautiful-mind"}):
            with self.assertRaises(cp.ControlPlaneError): cp.load_config()
    def test_profiles_are_peers_and_deployment_enabled(self):
        for repo in ["kix-protocol","ZARI","film-unit-mv-studio","maeum-gyeol"]:
            with self.subTest(repo=repo), patch.dict(os.environ, {"ASTRA_TARGET_REPOSITORY":"BeautifulMind-JT/"+repo}):
                cfg=cp.load_config()
                self.assertEqual(cfg["repository"],"BeautifulMind-JT/"+repo)
                self.assertEqual(cfg["control_repository"],"BeautifulMind-JT/ai-ops-control-plane")
                self.assertIn(cfg["control_record_actor"],
                              ["github-actions[bot]", *cfg["allowed_task_actors"]])
                self.assertIs(cfg["deployment_enabled"], True)
    def test_unapproved_target_blocks_even_if_global_gate_is_enabled(self):
        blocked={"control_repository":"BeautifulMind-JT/ai-ops-control-plane","deployment_enabled":False}
        with patch.object(cp,"load_activation",return_value={"runtime_enabled":True}), patch.object(cp,"validate_repo"), patch.object(cp,"load_config",return_value=blocked), patch.object(cp.subprocess,"run") as run:
            with self.assertRaisesRegex(cp.ControlPlaneError,"target deployment not approved"):
                cp.require_runtime_enabled()
            run.assert_not_called()
