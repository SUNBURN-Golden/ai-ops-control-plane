# Shared engineering control plane

Owner: SUNBURN-Golden/ai-ops-control-plane. Root non-engineering policies remain authoritative.
Product contracts, code, task specifications and product CI stay in their repositories.
SOULBOUND is excluded. All four peer targets retain deployment eligibility.

Source import: KIX ec3f6db0d613385bfdf2392a4295f0099be1eec6;
import commit 55240a610688a5fe7f4883e250b0ffab6c126fdb.
source-manifest.json records import-time blobs; provenance/ and history/ are historical evidence,
not current installation digests or transferred deployment approvals.

Central runtime: repository-root .github/workflows/control-plane-runtime.yml.
Nested engineering/.github/workflows are non-executing source copies.
ASTRA_TARGET_REPOSITORY explicitly selects projects.json; no default target.
KIX, ZARI, FILM UNIT and MAEUM_GYEOL retain deployment_enabled=true.
This is target eligibility, not installed-runtime approval. Current activation
is disabled pending evidence for this implementation. Activation, exact runtime
SHA, protected host allowlist and lane gates must all pass before execution.
A new source PR does not inherit an older implementation audit or authorize installation.
Project task/CI inputs are in projects/. Approved contracts remain in products.

Engineering governance: AGENTS.md. Task shape: TASKS/TEMPLATE.md. Procedure: RUNBOOKS/DISPATCH.md.
Builder setup: docs/BUILDER_LANES.md. Direct Astra requests: docs/ASTRA_SLACK.md.
Grok is optional relay; mechanical code dispatches and builders own test/fix/retest.
One task has one writer. Independent review is read-only. User authorizes merge.

Offline checks from repository root:
- python3 -m unittest discover -s tests
- ASTRA_TARGET_REPOSITORY=SUNBURN-Golden/kix-protocol python3 -m unittest discover -s engineering/scripts -p 'test_control_plane*.py'
- bash -n engineering/scripts/control_plane_boundary_hook.sh
- bash -n engineering/scripts/control_plane_boundary_probe.sh

Live activation requires exact source/host verification and evidence under CUTOVER.md.
Never reset existing admission/flow ledgers or replay UNKNOWN while deploying a new source.

Approved graph/correction loop: docs/TASK_GRAPH.md (source-only, disabled).
It uses the existing flow ledger; deployment requires qualified adapters and collector.
