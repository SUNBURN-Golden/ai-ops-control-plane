# Shared engineering control plane — source candidate

Owner: BeautifulMind-JT/ai-ops-control-plane. Root organization policies remain authoritative.
KIX, ZARI, FILM UNIT and MAEUM_GYEOL are product consumers. SOULBOUND is excluded.

Source imported from KIX ec3f6db0d613385bfdf2392a4295f0099be1eec6.
Import commit: 55240a610688a5fe7f4883e250b0ffab6c126fdb.
source-manifest.json records import-time blobs; later adaptation is a separate commit.
Original transformed files are in provenance/kix; historical PR notes are indexed in history/manifest.json.
These are historical evidence, never transferred deployment authorization.

Central config separates control_repository from the explicit ASTRA_TARGET_REPOSITORY.
Only projects.json targets are accepted. Every target deployment_enabled is false.
No default target, enabled central route, production installation or cutover is claimed.
Project task/CI inputs are in projects/. Approved product contracts always remain in products.

Repository-root Engineering source CI runs offline Python and shell checks.
Nested .github/workflows are archived workflow source, not active dispatch workflows.
Their original KIX paths/identity are historical; do not install them as a central runner workflow.
A reviewed central dispatch workflow, target credential scopes and fresh host/workflow attestation
remain separate integration/cutover gates. The current sources cannot authorize production dispatch.

Run from repository root:
- python3 -m unittest discover -s tests
- ASTRA_TARGET_REPOSITORY=BeautifulMind-JT/kix-protocol python3 -m unittest discover -s engineering/scripts -p 'test_control_plane*.py'
- bash -n engineering/scripts/control_plane_boundary_hook.sh
- bash -n engineering/scripts/control_plane_boundary_probe.sh

No main changes, merge, runner registration, Slack/webhook routing, ledger migration,
paid provider calls or renewed FILM/MAEUM rollout are part of source extraction.
See CUTOVER.md for the separate approval gate. Issue: https://github.com/BeautifulMind-JT/ai-ops-control-plane/issues/1
