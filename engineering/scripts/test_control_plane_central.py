import os, unittest
from unittest.mock import patch
import control_plane as cp

class CentralIdentityTests(unittest.TestCase):
    def test_explicit_target_required(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(cp.ControlPlaneError): cp.load_config()
    def test_unknown_target_rejected(self):
        with patch.dict(os.environ, {"ASTRA_TARGET_REPOSITORY":"BeautifulMind-JT/beautiful-mind"}):
            with self.assertRaises(cp.ControlPlaneError): cp.load_config()
    def test_profiles_are_peers_and_kix_only_enabled(self):
        for repo in ["kix-protocol","ZARI","film-unit-mv-studio","maeum-gyeol"]:
            with self.subTest(repo=repo), patch.dict(os.environ, {"ASTRA_TARGET_REPOSITORY":"BeautifulMind-JT/"+repo}):
                cfg=cp.load_config()
                self.assertEqual(cfg["repository"],"BeautifulMind-JT/"+repo)
                self.assertEqual(cfg["control_repository"],"BeautifulMind-JT/ai-ops-control-plane")
                self.assertIn(cfg["control_record_actor"],
                              ["github-actions[bot]", *cfg["allowed_task_actors"]])
                self.assertEqual(cfg["deployment_enabled"], repo == "kix-protocol")
    def test_target_profile_blocks_even_if_global_gate_is_enabled(self):
        with patch.dict(os.environ, {"ASTRA_TARGET_REPOSITORY":"BeautifulMind-JT/ZARI"}), patch.object(cp,"load_activation",return_value={"runtime_enabled":True}), patch.object(cp,"validate_repo"), patch.object(cp.subprocess,"run") as run:
            with self.assertRaisesRegex(cp.ControlPlaneError,"target deployment not approved"):
                cp.require_runtime_enabled()
            run.assert_not_called()
