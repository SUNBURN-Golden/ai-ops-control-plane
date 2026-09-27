# Approved task graph and autonomous correction loop

Source feature, disabled by default. Governance remains `../AGENTS.md` and
`../RUNBOOKS/DISPATCH.md`; this file does not authorize installation or activation.
An approved graph chooses bounded tasks. The writer owns investigation,
implementation and test/fix/retest inside each task. Code evaluates transitions.

## Authority and cost

- GitHub stores the User-approved graph/task revisions, decisions, significant
  findings and current-SHA candidate evidence. Comments are records, not locks.
- One host's existing protected flow SQLite database serializes graph delivery.
  Never copy it to another host or create a second active dispatcher. A qualified
  adapter must also enforce existing host-global admission across every caller.
- `max_active_sessions=1` remains mandatory. Independent nodes need no semantic
  edge, but this capacity policy still schedules their execution sequentially.
- One task has one writer session. Feedback carries exact failure pointers,
  acceptance pointer and allowed paths to that SAME session. No builder fallback.
- Read-only reviewers use different confirmed sessions and nonauthor identities.
  A Mac plan may name Cursor CLI/Grok and ZCode/GLM after both interfaces are
  qualified. A CLI version check is insufficient. Do not label Cursor as GLM.
- Grok Bot is not required. One explicit validated event invokes `advance` once,
  performs at most one adapter action, then exits. No polling, standing model,
  transcript reading or model-generated summaries. No raw Slack firehose.
- Astra receives contract decisions and A3/milestone/release gates only, through
  the configured adapter. That adapter may use the existing authenticated Slack
  consumer; a Slack delivery receipt is not an audit session or approval.
- MERGE_CANDIDATE is a notification, never merge authority. Draft status remains
  unchanged. User merges after checking current evidence and repository gates.

## Data and execution

`control_plane_graph.py` evaluates plans; `control_plane_graph_cli.py` reads live
GitHub facts and invokes pinned, owner-only local adapters. Both use stdlib.
Use `../.github/control-plane/graph-policy.example.json` as a disabled schema
example, never as deployment configuration. No live workflow is installed here.

A protected policy binds one canonical plan comment by repo/issue/comment ID,
User identity and exact SHA256 of its body. Body format:

```text
<!-- ASTRA_GRAPH_PLAN_V1 -->
{ ...one JSON object... }
```

Required graph fields: `schema_version=1`, `graph_id`, `revision`,
`approval_pointer` (that comment's URL), `max_active_sessions=1`, `nodes`.
Each node supplies:

| Fields | Meaning |
| --- | --- |
| id, repository, issue, task_id, revision, task_pointer | One canonical product/central task |
| issue_body_sha256, base | Approved task body and 40-character base SHA |
| acceptance_pointer, allowed_paths, locked_paths | Checks written before implementation; exact paths or directory prefixes ending `/` |
| dependencies | Array of `{node, input}`; every edge names the consumed upstream merge SHA |
| writer | `{identity, lane}` |
| reviewers | Ordered independent `{identity, lane, approval_pointer, enabled, read_only_verified}` entries |
| auditor, astra | Designated audit/decision lanes with the same identity fields |
| audit_floor, astra_gate, audit_scope_digest | A1/A2/A3 and NONE/ARCHITECTURE/MILESTONE/RELEASE; milestone/release also pins approved evidence-packet digest |
| required_checks | Required workflow aggregates: name, app_id, workflow_id, events, path, workflow_blob |

Do not split one ticket into competing writers. Bound tasks before dispatch;
changing scope or contract needs a durable approved revision. The graph validates
cycles and task uniqueness. Dependencies wait for User merge plus verified
post-merge checks, including when merging closes the upstream task issue.

The protected mechanical collector publishes an authenticated, registered
`ASTRA_GRAPH_OBSERVATION_V1` comment per node, bound to plan digest, task revision,
input SHAs and current PR HEAD. It attests host ownership, repository prerequisites,
blockers, writer lifecycle, contract-change state, authors and provider lifecycle.
It must re-read authoritative host facts; model status text cannot supply them.
PR/head/base/files/workflow runs and review actors are fetched separately from
GitHub. Incomplete, stale, ambiguous or inaccessible evidence blocks progress.

Review results use native GitHub reviews with `ASTRA_GRAPH_RESULT_V1` JSON:
phase (`review`/`audit`), full subject, request_id, attempt_id, identity,
designation, session_id, result, required_depth, verified_depth, contract_change,
blockers. Audits also bind gate and scope_digest from the request. PASS requires
latest native APPROVED state; a later change request/withdrawal blocks. Results must match a CONFIRMED local outbox receipt and
TERMINAL session, plus collector-confirmed read-only/session evidence.
Shared GitHub accounts cannot masquerade as independent review identities:
qualify distinct publication identities or separately implement and audit a
session-authenticated publisher. This PR does not bypass that requirement.

## Loop and gate

1. Launch only with no prior task owner; persist writer ownership and SUBMITTING
   atomically before the external call. The adapter must recheck current subject
   and existing host admission immediately before starting the provider.
2. Aggregate complete required workflows at current HEAD. Pending matrix jobs
   keep verification pending. CI_FAIL relays its exact run pointer to the writer.
3. Writer yields; each configured reviewer runs read-only, without previous
   review findings included in its request. FAIL returns to the writer.
4. New HEAD invalidates prior semantic results. Re-review current HEAD and scope.
   Reviewer-reported required A3 depth promotes the architecture audit gate.
5. Audit FAIL returns exact evidence to the same writer. Contract changes go to
   the designated Astra decision lane; a User-approved durable revision resumes.
6. Successful gates produce a deduplicated GitHub MERGE_CANDIDATE status with
   exact HEAD. No ready-for-review or merge mutation. After human merge, confirmed
   post-merge verification enables dependent tasks. Its protected receipt binds full
   subject, inputs, PR pointer, actual merge SHA, verified=true and evidence pointer.

No fixed count ends the writer's normal correction loop. Explicit BLOCKED,
STALLED, a configured mechanical budget limit, or unresolved evidence stops
further delivery. Same-HEAD repeated failure with unchanged evidence is not
re-sent. An actual new failure pointer creates a new feedback request.
Learning is a proposed regression test or policy PR, never an automatic rule edit.

## Delivery and recovery

Capacity reservation and potentially-sent SUBMITTING share one transaction.
Capacity-full means WAITING_CAPACITY and leaves the action NOT_STARTED. Feedback
may reach an already active same owner; unrelated or unresolved slots block it.
On the next legitimate terminal/event notification the unsent action is eligible.
There is no timer-based retry or automatic release.

The adapter receives `[executable, operation]` and one JSON request on stdin.
Its path and digest are protected; coordinator tokens are not forwarded. It
returns `{request_id, accepted:true, session_id}` only for a real durable session
(or the same resumed owner for feedback). DECISION execution also reserves capacity and requires a confirmed session;
its prior GitHub projection is the request notification, not session creation. Request IDs must bind provider idempotency where
supported. No detached child process or probe-only adapter qualifies.

Crash, response loss or receipt mismatch retains SUBMITTING/UNKNOWN and capacity.
A changed HEAD or plan does not authorize a replacement. Source CLI intentionally
has no reset/retry/adopt command: reconcile the original request with provider,
existing host admission and durable GitHub evidence under the existing recovery
procedure. Do not edit SQLite manually or dispatch a new attempt to bypass it.
Projection uncertainty also stops delivery; do not duplicate uncertain comments.
No exactly-once claim is made across GitHub, SQLite, Slack and providers.

## Deployment gate (not completed by this source PR)

1. Independent exact-source review; required CI or separately authorized substitute.
2. Qualify the concrete collector and every adapter at exact digest: authentication,
   session creation/resume/termination, read-only isolation, host admission,
   stale-subject rejection and UNKNOWN reconciliation. Existing Mac probes do not
   establish these properties. Billing-blocked CI is not a PASS.
3. Fence existing dispatch routes; preserve flow/admission ledgers and owners.
   Register protected plan/observation identities and adapter evidence. Extend the
   EXISTING flow DB explicitly with `init`; do not start a parallel authority.
4. User-authorized diagnostic roundtrip at the pinned source/install; no product
   work, then verify no duplicates and terminal sessions before opening intake.

Commands on an already qualified host (no automation is created by this PR):

```sh
python3 engineering/scripts/control_plane_graph_cli.py init --policy /protected/graph-policy.json
python3 engineering/scripts/control_plane_graph_cli.py assess --policy /protected/graph-policy.json
python3 engineering/scripts/control_plane_graph_cli.py advance --policy /protected/graph-policy.json --event-id github:event-id
```

`assess` reads current facts without side effects. `advance` remains disabled
unless the protected policy has explicit activation and source-audit evidence.
A trusted webhook/local callback must authenticate the event then invoke it once;
webhook content is only a refresh hint. No event listener is deployed here.
Product flags, activation files, host settings and existing Mac PRs are unchanged.
