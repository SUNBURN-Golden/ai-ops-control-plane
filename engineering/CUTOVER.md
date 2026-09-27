# Runtime cutover — FOUR TARGETS ELIGIBLE; NEW IMPLEMENTATION NOT ACTIVATED

On 2026-09-27 the user authorized `deployment_enabled=true` for every peer
target in `projects.json`: KIX, ZARI, FILM UNIT, and MAEUM_GYEOL.
SOULBOUND stays excluded. There is still no default target.
This passes only the target-eligibility gate for those four repositories.
It does not merge product code, release a service, or rewrite the host
machine's separate repository allow-list.

## Current source integration gate (2026-09-27)

Keep all four deployment flags true. PR #18 changed activated_runtime_sha to
0243f5d440a1e0be76627923950ffbd2c7ff3fa2 while its audit and preflight still
attested 6cc78d44649b161d05879cd043af088cedb3f5f3. Those PASS values cannot
approve the four-target change or the new CP-OPT-002 implementation.
The candidate activation record is therefore runtime_enabled=false with
NOT_APPROVED/PENDING gates and PENDING implementation SHA. This does not revoke
target eligibility or historical approvals for their original scope.
Finding: https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/18#issuecomment-5853840780

Before activation, bind a non-author review and fresh disabled-install/preflight
to the same fixed implementation SHA, installed digests, explicit target scope,
and enabled lanes. Check each target's task actor, credentials, required CI,
Slack routing and host allowlist; KIX-only evidence cannot qualify all peers.
An activation-only commit may follow that SHA; never chase its tip or move an
audited SHA while retaining evidence for a different implementation.
Source tests/review are not live-host evidence. No live host or intake change is
performed by this source integration. Fence/drain/reconcile existing senders
before installing it; do not terminate an active owner or retry UNKNOWN blindly.
Actions quota exhaustion is not CI success or an implicit merge-gate waiver.

## Historical install preparation — 2026-09-25

The stage below is historical; its status is not a current host measurement.

User authorized the operational cutover for CP-EXTRACT-001 on 2026-09-25.
Authorization covers preparing the central production ops path and a disabled
host install only. Activation, flow-policy enablement, product pin merges and
any canary remain NOT_APPROVED pending independent exact-HEAD review
(GROK_BUILD) and a separate bounded-canary decision.

Historical stage: INSTALL_PREP_DISABLED — root central dispatch workflow and
host install/pin tooling are committed for review; nothing is enabled.

The technical gate sequence below still applies and is not waived:

1. Accept exact central source SHA, non-author audit, offline CI and explicit User source merge.
2. Inventory active owners, requests, attempts and unresolved SUBMITTING/UNKNOWN entries on existing host.
3. Under separate User authorization stop intake; drain and fence old senders/dispatchers.
   Never release UNKNOWN on timeout alone. Reconcile with durable provider evidence.
4. Back up protected ledgers outside Git. Preserve TASK_KEY, request/attempt IDs, session ownership
   and replay records. Prove one canonical ledger owner before allowing any new sender.
5. Prepare separately reviewed root central dispatch workflow and least-privilege target credentials.
   Attest control repository/workflow SHA separately from target repository/task/PR HEAD/revision.
   Verify host paths, shell/Python digests, runner audience, Slack signatures and routing allowlists.
   - DONE (pending review): `.github/workflows/control-plane-runtime.yml` is the root
     production workflow; `engineering/.github/workflows/control-plane-runtime.yml` remains
     non-executing audited source. `engineering/scripts/control_plane_install.py` pins the host
     to an exact central SHA and updates host pin fields; `apply` always installs disabled.
6. Install disabled; run exact-install verification with fresh non-author evidence. KIX evidence does not transfer.
   - `control_plane_install.py verify` is the non-mutating check; `apply` refuses unless the
     in-repo activation record is runtime_enabled=false / NOT_APPROVED / PENDING.
7. User separately approves one bounded canary; maintain one writer and one active dispatcher.
   2026-09-25 stopped FILM/MAEUM rollout. 2026-09-27 permits target eligibility for the four peers; host rollout requires its own evidence. SOULBOUND remains excluded.
8. Retire old product execution workflows only after source acceptance and confirmed drain/fence.
9. Rollback: fence the new sender first; reconcile its ambiguous requests; restore compatible code
   and ledger state without losing accepted request IDs; only then authorize the old sender again.
   Never blindly replay a launch or run both dispatchers.

Not performed in this stage: no live host apply, no canary, no flow-policy
enablement, no re-enabling of the KIX Control Plane Runtime workflow, no
product pin/merge updates, no activation of `runtime_enabled`.
