# DISPATCH RUNBOOK v3

This is a deterministic execution contract.
Grok does not extend it mid-session.

NO STANDING ROUTINES.
NO POLLING.
NO BACKGROUND MONITORING.
NO SECOND SEMANTIC REASONING PASS.

## 1. Project map

Shared source owner: BeautifulMind-JT/ai-ops-control-plane.
Resolve the exact target repository in `.github/control-plane/projects.json`.
Project-specific task/verification rules are in `projects/<repository-name>.md`.
Peer targets KIX, ZARI, FILM UNIT and MAEUM_GYEOL have deployment_enabled=true.
This grants target eligibility only; activation/host/lane gates still apply.
No default target. SOULBOUND is excluded.
Never infer target, channel, builder or reviewer. Missing configuration blocks.

## 2. Activation preconditions

Before automation is enabled, the mechanical layer must have:

- configured USER actor identity;
- configured ASTRA actor identity;
- configured action-adapter identities (Grok only for explicit overrides);
- configured identity/adapter for every enabled builder provider;
- configured independent reviewer identity/lane;
- per-TASK_KEY single-writer serialization;
- canonical-control-record read/write support;
- self-event filtering;
- provider launch reconciliation or explicit UNKNOWN handling.

The implementation must receive an independent exact-SHA audit and explicit
User activation after these checks. Documentation approval does not enable it.
Until then the manual protocol in section 8 applies.

## 3. Raw event intake

Allowed raw sources:

1. explicit authenticated USER command;
2. configured Slack workflow/command;
3. GitHub webhook / GitHub Actions event;
4. configured worker/reviewer/provider callback;
5. configured ASTRA audit result channel/action.

The mechanical layer rejects:

- unconfigured actors;
- ordinary chat text masquerading as PASS/DECISION;
- router's own status messages as new task triggers;
- events that cannot be mapped to one canonical task;
- duplicate delivery IDs already recorded with the same effect.

A free-form User request without a canonical task record is an intake request,
not a dispatch event. A cheap deterministic intake form/workflow must first
create or identify the canonical GitHub task and task revision.

## 4. Normalized event contract

Only normalized events may invoke external action adapters. Grok itself is not
the event bus and is not required for deterministic workflow transitions.

Every normalized event contains:

EVENT_ID:
EVENT_TYPE:
SOURCE_ACTOR_ID:
SOURCE_POINTER:
REPO:
TASK_ID:
TASK_REVISION:
CANONICAL_TASK_POINTER:
CONTROL_RECORD_POINTER:
ATTEMPT_ID:
PR_POINTER:
HEAD_SHA:
RUN_OR_RESULT_ID:

Fields that do not apply are explicit N/A; they are not silently omitted.

Result/gate events that refer to code must carry HEAD_SHA.
Decision events must carry TASK_REVISION and durable decision pointer.

Grok may receive only explicit authenticated User commands or normalized
one-shot relay actions. Builder/reviewer selection comes from canonical
configuration, never from Grok semantic judgment.

## 5. TASK_KEY and canonical control record

`TASK_KEY = REPO + TASK_ID`

Each TASK_KEY has exactly one canonical control record, durably projected in
the canonical GitHub task/issue at a fixed machine-owned record pointer.

The GitHub projection is durable evidence, not the serialization primitive.

Control record minimum facts:

- TASK_KEY
- TASK_REVISION
- canonical task/spec pointer
- CANONICAL_SLACK_THREAD (N/A until linked)
- CONTROL_RECORD_VERSION and pending action/delivery IDs
- CLAIM_ID
- CLAIM_STATE
- LAUNCH_REQUEST_ID
- LAUNCH_STATE
- ATTEMPT_ID
- OWNER_WORKER / BUILDER_ID
- OWNER_SESSION_ID
- PR_POINTER
- CURRENT_HEAD_SHA
- verification policy and current-head verification facts
- review policy, configured audit floor, REVIEW_REQUEST_ID, REVIEW_LAUNCH_STATE,
  reviewer lane/session, review attempt ID, VERIFIED_REVIEW_DEPTH,
  VERIFIED_REQUIRED_DEPTH, EFFECTIVE_AUDIT_FLOOR, verified touched areas,
  contract-change flag and current-head result
- ASTRA_GATE, AUDIT_REQUEST_ID, AUDIT_ATTEMPT_ID, AUDIT_REQUEST_STATE,
  ACCEPTED_AUDITOR_IDENTITY, AUDITOR_DESIGNATION_POINTER, verified audit depth,
  audited SHA/evidence SHA and result when an Astra gate applies
- unresolved blocker/decision pointer
- merge SHA
- post-merge result/follow-up pointer
- last accepted event IDs

State is derived from these facts. Arrival order does not blindly overwrite
state.

## 6. Per-task serialization

All mutations of one control record occur under a single-writer serialization
primitive keyed by TASK_KEY.

Two concurrent events for the same task must not both observe "unowned" and
launch two writers.

EVENT_ID deduplicates delivery.
TASK_KEY serialization protects job ownership.

Both are required.

## 7. Claim and launch protocol

Under TASK_KEY serialization:
- Existing active owner: relay eligible feedback to that owner; never launch another.
- Existing unresolved claim/request: reuse it; never create a competing claim.
- Otherwise persist CLAIM_ID, designated executor, stable LAUNCH_REQUEST_ID,
  LAUNCH_STATE=NOT_STARTED and a durable pending dispatch action together.

Before any external launch, atomically consume that action and persist
LAUNCH_STATE=SUBMITTING. Only the executor holding that permission may send.
NOT_STARTED therefore proves no send was authorized; resume the same request, or
start a new attempt only through the explicit retry event below.
SUBMITTING after a crash is potentially sent: reconcile or mark UNKNOWN; never
automatically launch again. Timeout/claim expiry alone does not authorize takeover.

Use LAUNCH_REQUEST_ID as the provider idempotency key when supported.
Confirmed receipt records CONFIRMED + OWNER_SESSION_ID + worker/attempt.
Provider proof of no session permits FAILED_PRESTART; a configured retry event
may reauthorize that same task only after the prior executor is fenced.
The runtime retry event is a dispatch whose `expected_attempt_id` equals the
record's ATTEMPT_ID + 1. It creates a new CLAIM_ID/LAUNCH_REQUEST_ID, keeps the
prior attempt in `previous_attempts`, and is admitted only when the host
ledger reports the prior request as FAILED_PRESTART or RECONCILED (or, for a
NOT_STARTED record, not present). A CONFIRMED owner is never displaced; a
request that never reached the host is fenced first with the operator's
never-admitted reconciliation.
Ambiguous outcome records UNKNOWN and blocks relaunch until provider evidence
or explicit User resolution proves the safe next step. Slack response loss
never undoes a confirmed GitHub owner record.

State mutation and pending-action persistence must be atomic in the serialized
store. GitHub holds the durable projection; projection failure blocks further
launches until reconciled. Pending NOT_STARTED actions may be resumed by an
explicit recovery event, not polling. This is not a claim of exactly-once
external execution.

## 8. Manual dispatch and outage

OPERATING_MODE=MANUAL_ONLY until the activation gate passes. All automatic
dispatchers are disabled. User is the sole launch executor and control-record
writer in this mode; a comment alone is not a concurrent lock.

Before direct User → designated-builder dispatch:

1. identify canonical task/revision, configured BUILDER_ID and existing owner/request;
2. reuse an active owner; block if any unresolved SUBMITTING/UNKNOWN request exists;
3. durably reserve CLAIM_ID/LAUNCH_REQUEST_ID and record SUBMITTING before launch;
4. send only the task pointer/revision to the configured builder adapter and
   record the resulting provider/session ID.

If the GitHub reservation cannot be recorded, do not launch. Response loss
requires reconciliation, not a second session. Do not enable automation while
a manual claim/send is unresolved.

In automated mode manual dispatch requires a serialized MANUAL_CLAIM_ALLOWED
action and the same launch protocol. Outage does not bypass ownership.

Runtime dispatch has exactly one route: the self-hosted `control-plane-runtime.yml`
workflow. An Actions outage or quota block does not authorize another route to
launch or relaunch; a second route needs source pinning and shared
serialization with this one first (docs/CONTROL_PLANE_RUNTIME.md).

Grok outage is not a workflow outage: the same fixed mechanical command may be
invoked by another authenticated caller. Provider outage for the assigned
builder is recorded as a blocker; the control plane must not silently select a
different builder. Reassignment requires an authorized control-record update
and fencing/reconciliation of the prior attempt.

## 9. Task-envelope use

The mechanical layer reads TASKS/TEMPLATE.md fields from the canonical task
record. Grok may relay a pointer but does not semantically interpret the task.

No actor in the dispatch path may:

- write a second task specification;
- paraphrase objectives as a new authority;
- infer omitted contracts/invariants;
- choose execution class, builder, reviewer, audit depth or Astra gate by
  semantic reading.

If the canonical task omits EXECUTION_CLASS, use BUILDER_STANDARD.
A BUILDER_STANDARD task without a configured BUILDER_ID is blocked.
A0/CHEAP requires explicit A0 authorization.

## 10. Writer autonomy and feedback

No routine Astra preflight, plan approval or progress review is required for
an already authorized task. The assigned builder investigates and selects
ordinary implementation details within approved contracts. CI failure has no
arbitrary two-attempt cutoff.

Normal writer feedback always returns to the same OWNER_SESSION_ID.
CI/review/Astra-gate findings do not create a new writer.

The assigned builder owns ordinary implementation/debug/test decisions inside
approved boundaries.

STALLED must be explicitly reported by the builder/provider.
BUDGET_LIMIT_REACHED may be emitted only by a configured mechanical cost guard.
Grok does not infer either condition.

## 11. HEAD and task-revision guards

Code-result events are accepted only when their HEAD_SHA equals
CURRENT_HEAD_SHA for the task/PR, except an accepted `HEAD_CHANGED` event that
advances CURRENT_HEAD_SHA.

On accepted HEAD_CHANGED:

- set CURRENT_HEAD_SHA to the new head;
- clear current-head CI/verification facts;
- clear current-head review facts;
- clear current-head audit facts;
- retain historical evidence only as history.

Late results for an older HEAD are recorded as stale evidence and do not
advance gates.

If a consequential User decision changes task scope/contract:
- persist the decision;
- increment TASK_REVISION;
- update the authoritative task/spec pointer as repository policy requires;
- invalidate approvals tied to the older task revision where applicable;
- only then resume the same owner or start an explicitly authorized new attempt.

HEAD_CHANGED is accepted only after reading the live PR head under task
serialization; delayed events cannot restore an old head. PR_OPEN may bind
the authenticated owner's PR before WRITER_DONE arrives. All result events
must match TASK_REVISION and the current request/run attempt, not only SHA.
A merged/terminal task cannot resume its writer from late pre-merge events.
Post-merge verification uses MERGE_SHA and the post-merge phase, not PR HEAD.

## 12. Verification gate

A single successful check is never equivalent to CI_GATE_PASS.

The mechanical layer aggregates the complete project policy for CURRENT_HEAD_SHA.

Use the selected product profile and authoritative product CI rules.
KIX requires both KTX kernel and KIX protocol verification; this is not the
default gate for other products. Do not infer or waive a missing gate.
A success from an older SHA is stale; a single job is not a workflow gate.

Accepted verification facts must include exact HEAD/evidence SHA and run IDs or
local evidence pointers.

CI/verification failure:
- update current-head verification facts;
- emit one normalized failure event for the new gate state;
- The configured adapter relays exact failure pointers to the same owner;
- Grok does not debug;
- repeated identical raw check events do not repeatedly invoke Grok unless gate
  state materially changes.

## 13. Independent review gate

Reviewer sends use the same NOT_STARTED → SUBMITTING →
CONFIRMED/UNKNOWN protocol as writer launches, keyed by
REVIEW_REQUEST_ID/REVIEW_ATTEMPT_ID. Persist the pending action with the
request; a crash is not permission to resend.

Review dispatch is idempotent and serialized.

Under TASK_KEY serialization, before emitting a review request for the current
task revision + HEAD/evidence SHA:

1. if a matching accepted review result already exists, do not launch another reviewer;
2. if a matching REVIEW_REQUEST_ID/session already exists, reuse it;
3. otherwise create stable REVIEW_REQUEST_ID and REVIEW_ATTEMPT_ID;
4. set REVIEW_LAUNCH_STATE=NOT_STARTED;
5. persist the control record;
6. emit exactly one REVIEW_DISPATCH_ALLOWED.

The configured adapter launches REVIEWER_LANE_ID in read-only mode.
The reviewer must not have authored or modified the reviewed change. A peer
builder is eligible only when it is acting in a distinct non-author review
session and has no write role on that task.

If reviewer launch may have succeeded but the outcome is ambiguous, set
REVIEW_LAUNCH_STATE=UNKNOWN and do not auto-launch another reviewer.

All non-A0 substantive work requires independent read-only review unless a
stricter repository rule applies.

The reviewer must inspect the actual diff/evidence and return, for the exact
task revision and HEAD/evidence SHA:

REVIEW_RESULT: PASS | PASS_WITH_NOTES | FAIL | DECISION_REQUIRED
REVIEWER_IDENTITY_OR_SESSION:
REVIEWED_TASK_REVISION:
REVIEWED_HEAD_OR_EVIDENCE_SHA:
VERIFIED_REVIEW_DEPTH:
VERIFIED_REQUIRED_DEPTH: A1 | A2 | A3
VERIFIED_TOUCHED_AREAS:
VERIFIED_CONTRACT_CHANGE_REQUIRED:
FINDING_POINTERS:

VERIFIED_REQUIRED_DEPTH is the reviewer's semantic classification of the
minimum depth required by the actual diff and authoritative repository rules.
It is independent of the task's predeclared AUDIT_FLOOR.

After accepting the current-head review, the mechanical layer computes:

EFFECTIVE_AUDIT_FLOOR =
  max(configured AUDIT_FLOOR, VERIFIED_REQUIRED_DEPTH)

using A0 < A1 < A2 < A3.

If EFFECTIVE_AUDIT_FLOOR=A3, the control record must set
ASTRA_GATE=ARCHITECTURE for this task revision/current HEAD before any merge
predicate is reevaluated. A predeclared NONE/MILESTONE gate cannot suppress
this promotion. If repository rules require a stronger explicit gate, keep the
stronger gate.

PASS_WITH_NOTES cannot hide an unresolved correctness, contract, invariant,
security or acceptance failure.

Review result is accepted only from the configured reviewer and matching
request/session. A new relevant HEAD invalidates the prior review and its
derived EFFECTIVE_AUDIT_FLOOR.

FAIL:
relay exact findings to the same writer.

DECISION_REQUIRED or VERIFIED_CONTRACT_CHANGE_REQUIRED=YES:
record the blocker and emit the required Astra/decision path; do not derive
READY_FOR_MERGE.

PASS/PASS_WITH_NOTES with no consequential contract change:
- if EFFECTIVE_AUDIT_FLOOR=A3 or another Astra gate is required, emit AUDIT_REQUIRED;
- otherwise reevaluate READY_FOR_MERGE without invoking Astra.

## 14. Astra gate

Astra is not the default routine A1/A2 reviewer.

This section applies only when at least one is true:

- EFFECTIVE_AUDIT_FLOOR=A3;
- ASTRA_GATE is MILESTONE, ARCHITECTURE or RELEASE;
- an architecture exception or consequential contract-change question requires
  Astra analysis;
- a repository-specific authoritative rule explicitly requires Astra.

EFFECTIVE_AUDIT_FLOOR=A3 implies ASTRA_GATE=ARCHITECTURE for the current
task revision/current HEAD.

Astra-request delivery uses the same fail-closed send discipline as writer and
reviewer launch and is serialized by task revision + current HEAD/evidence SHA
(or by an explicitly identified milestone/release evidence packet).

Each request is bound at creation to:

AUDIT_REQUEST_ID
AUDIT_ATTEMPT_ID
ACCEPTED_AUDITOR_IDENTITY
AUDITOR_DESIGNATION_POINTER (N/A for configured Astra)
AUDITED_TASK_REVISION_OR_MILESTONE
AUDITED_HEAD_OR_EVIDENCE_SHA

Before emitting AUDIT_REQUIRED, under the applicable serialization key the
mechanical layer:

1. reuses an existing accepted Astra-gate result only when the request identity,
   accepted auditor/designation, task or milestone identity and exact
   HEAD/evidence SHA all still match;
2. if a matching AUDIT_REQUEST_ID/AUDIT_ATTEMPT_ID is already
   NOT_STARTED/SUBMITTING/CONFIRMED/UNKNOWN, reuses/reconciles it instead of
   creating a second request;
   For NOT_STARTED, resume only the existing pending action; never create
   another request/attempt or enqueue another delivery;
3. otherwise creates stable AUDIT_REQUEST_ID and AUDIT_ATTEMPT_ID, binds the
   accepted auditor identity and designation pointer, sets
   AUDIT_REQUEST_STATE=NOT_STARTED, and atomically persists the pending action;
4. immediately before external send, atomically consumes that pending action
   and persists AUDIT_REQUEST_STATE=SUBMITTING;
5. only the executor holding that consumed action may send the audit request;
6. confirmed receipt records AUDIT_REQUEST_STATE=CONFIRMED.

If the process crashes or response is lost after SUBMITTING, the request is
potentially sent. Reconcile that same request; do not create or resend another
request merely because no receipt was observed. If outcome cannot be proven,
set AUDIT_REQUEST_STATE=UNKNOWN and require reconciliation or explicit User
resolution.

The packet contains exact task/milestone identity, revision, repository,
PR/evidence pointers, base/current SHA where applicable, authoritative
documents, verification facts, independent-review facts,
VERIFIED_REQUIRED_DEPTH, EFFECTIVE_AUDIT_FLOOR, reported touched
areas/contract-change flag and ASTRA_GATE.

Astra independently reads the actual relevant diff/evidence and authoritative
contracts. The accepted Astra-gate result contains:

AUDIT_REQUEST_ID:
AUDIT_ATTEMPT_ID:
AUDIT_RESULT: PASS | PASS_WITH_NOTES | FAIL | DECISION_REQUIRED
AUDITOR_IDENTITY_OR_SESSION:
AUDITOR_DESIGNATION_POINTER: (required when the auditor is not Astra)
AUDITED_TASK_REVISION_OR_MILESTONE:
AUDITED_HEAD_OR_EVIDENCE_SHA:
VERIFIED_AUDIT_DEPTH:
VERIFIED_TOUCHED_AREAS:
VERIFIED_CONTRACT_CHANGE_REQUIRED:
FINDING_POINTERS:

An audit result is accepted only when its authenticated auditor
identity/session matches ACCEPTED_AUDITOR_IDENTITY for that exact outstanding
request, and the designation pointer (when any) is still the same active User
designation bound to the request.

If Astra authored or modified the audited change, User must designate an
independent non-author architecture auditor with a durable scope pointer.
That auditor does not receive Astra's architecture/design authority.

Grok, the author and the mechanical layer may not choose a replacement
architecture auditor by semantic judgment or lower the required gate.

If the accepted auditor designation is revoked, replaced or its scope changes:

- outstanding requests bound to the old designation become invalid;
- prior gate results issued under that designation no longer satisfy current
  READY_FOR_MERGE;
- a new request may be created only after the new designation is durably
  recorded and any prior SUBMITTING/UNKNOWN delivery is safely reconciled.

Self-review by a writer/session that participated in the change is rejected.
A relevant HEAD/task revision change invalidates the prior result.

FAIL returns exact findings to the same writer when a writer fix is appropriate.
DECISION_REQUIRED records a blocker and emits the User decision path.
Architecture decisions remain Astra analysis → User decision → durable pointer.

Re-audit begins with the previous audited SHA/evidence delta and unresolved
findings, then expands as required by affected dependencies/contracts.

## 15. Consequential decision gate

A decision request contains:

- TASK_ID / TASK_REVISION;
- blocking question;
- current authoritative rule;
- exact evidence pointers;
- options identified by the worker/Astra, if any.

Grok adds no preferred option.

Only a configured User decision event may authorize the choice.

The decision must be persisted in GitHub/ADR/task authority before the
mechanical layer emits `DECISION_RECORDED`.

Ordinary Slack text is not a decision event.

## 16. A0 final qualification

Before A0 can complete, the mechanical layer checks the task envelope's
explicit A0 authorization and path contract:

- A0_AUTHORIZATION_POINTER exists;
- A0_CHANGE_KIND is one of the allowed A0 enum values;
- actual changed paths are a subset of A0_ALLOWED_PATHS;
- no actual changed path matches A0_FORBIDDEN_PATHS;
- repository locked/sensitive rules are still satisfied.

Grok does not generate or broaden these path lists.

If A0 qualification fails:
promote to A1 → independent review; invoke Astra only if the resulting task requires an Astra gate.

A0 never bypasses repository-specific evidence/bookkeeping rules.

Path checks alone do not prove A0 semantics. Final A0 qualification also
requires an authenticated User attestation of typo/format-only changes for
the exact revision/HEAD, or an approved deterministic transform verifier.
Otherwise promote to A1. For qualified A0 only, separate review and Astra gate are N/A unless repository rules require them; section 18 predicates apply to substantive work. All verification/blocker/merge gates still apply.

## 17. Derived states

State is computed from control-record facts, not arrival order.

Allowed derived states:

RECEIVED
CLAIMED
LAUNCH_UNKNOWN
RUNNING
BLOCKED
STALLED
BUDGET_BLOCKED
PR_OPEN
VERIFICATION_FAILED
READY_FOR_REVIEW
REVIEW_RUNNING
REVIEW_FAILED
BLOCKED_REVIEW_LANE
READY_FOR_AUDIT
AUDIT_RUNNING
AUDIT_FAILED
DECISION_REQUIRED
READY_FOR_MERGE
MERGED_POST_VERIFY
POST_MERGE_FAILED
DONE
DONE_NO_CHANGE

Important derivations:

- unresolved decision/blocker outranks progress states;
- LAUNCH_UNKNOWN blocks new launch;
- verification/review/audit facts must match current task revision and head;
- READY_FOR_MERGE is computed, never accepted as arbitrary text.

## 18. READY_FOR_MERGE predicate

For PR deliverables, READY_FOR_MERGE is true only when all are true:

- PR exists and is open;
- CURRENT_HEAD_SHA equals PR current head;
- task revision is current;
- no unresolved blocker/decision;
- verification gate is satisfied for CURRENT_HEAD_SHA;
- for non-A0 substantive work, required independent review PASS or
  PASS_WITH_NOTES matches CURRENT_HEAD_SHA;
- VERIFIED_REVIEW_DEPTH satisfies EFFECTIVE_AUDIT_FLOOR for all non-A3 review
  obligations; an A3 requirement additionally requires the Astra architecture
  gate rather than reviewer depth alone;
- VERIFIED_CONTRACT_CHANGE_REQUIRED from the accepted current-head review is NO,
  or the required Astra/User decision has been durably recorded and reflected
  in the current task revision;
- when EFFECTIVE_AUDIT_FLOOR=A3, ASTRA_GATE is ARCHITECTURE and the accepted
  current Astra-gate PASS/PASS_WITH_NOTES matches the applicable
  revision/head/evidence and VERIFIED_AUDIT_DEPTH satisfies A3;
- for any other explicit Astra gate, its accepted PASS/PASS_WITH_NOTES matches
  the applicable revision/head/evidence and required depth;
- when no Astra gate is required and EFFECTIVE_AUDIT_FLOOR is below A3, absence
  of an Astra result does not block merge;
- any auditor designation bound to the accepted Astra result is still active
  and unchanged;
- project-specific merge prerequisites are satisfied.

User still makes the merge decision.

## 19. No-change / non-code completion

A writer may return NO_CHANGE only when DELIVERABLE_MODE allows it.

For A1+ NO_CHANGE:

- provide exact evidence/base SHA;
- perform required independent review of the finding/evidence;
- if the task requires an Astra gate, obtain that gate for the same evidence identity;
- only then may the mechanical predicates derive DONE_NO_CHANGE.

No merge is invented.

NON_CODE_EVIDENCE follows project-specific approval/evidence gates; an
engineering review or Astra gate does not substitute for
production/legal/content approval.

## 20. Merge and DONE

On authenticated `PR_MERGED`:

KIX uses repository-specific post-merge verification from the
preserved KIX governance.

After merge:
- record merge SHA;
- for KIX, perform the required KIX post-merge checks for that merge;
- derived state is MERGED_POST_VERIFY until complete.

If post-merge verification fails:
- record POST_MERGE_FAILED;
- do not patch main or return the failure to the completed writer as an
  ordinary same-ticket CI fix;
- corrective code requires a new task/session/branch/PR for the affected product under its existing
  governance;
- the original task may become DONE only after the failure and follow-up task
  pointer are durably recorded.

For projects without post-merge verification, a correctly recorded merge may
derive DONE.

Grok never merges.

## 21. Fixed action ownership

ACTION_ADAPTER is configured per event below, not selected by Grok.
Automation defaults to MECHANICAL for every row. A User-approved explicit
mapping may select GROK for a limited launch/relay action; never both.
No configuration silently falls back to another AI on error.

| Normalized event | Action | Default adapter |
|---|---|---|
| DISPATCH_ALLOWED | launch designated owner using section 7; record receipt | MECHANICAL |
| WRITER_FEEDBACK_REQUIRED | exact pointer to existing owner | MECHANICAL |
| REVIEW_DISPATCH_ALLOWED | launch configured read-only reviewer | MECHANICAL |
| AUDIT_REQUIRED | exact audit packet to #ai-audit | MECHANICAL |
| DECISION_REQUIRED | exact decision packet to #ai-decisions | MECHANICAL |
| BLOCKED_STATUS | durable blocker + short status/Human action | MECHANICAL |
| READY_FOR_MERGE | project-thread notification only | MECHANICAL |
| DONE / DONE_NO_CHANGE | project-thread final pointers | MECHANICAL |

When explicitly mapped to GROK: one normalized event, one authorized action,
one receipt, end session. State mutation/aggregation stays mechanical.
In MANUAL_ONLY mode User performs delivery with the same durable facts.

## 22. Slack

`#ai-control` — explicit control-plane commands/status
`#ai-decisions` — authenticated User/Astra decision work
`#ai-audit` — authenticated Astra audit request/result
`#kix` — project task/status cockpit

One task → one canonical thread. GitHub task and Slack thread link both ways.
Project statuses are projections of the durable control record, keyed by
CONTROL_RECORD_VERSION; old/repeated projections are suppressed.
Fields: task/revision, state, owner/session, PR/HEAD, verification/review/audit,
blocker pointer, required Human action. Long context stays behind GitHub pointers.
Astra attention is #ai-decisions/#ai-audit, never the project-channel firehose.

AUDIT/DECISION delivery uses the confirmed GitHub action pointer, exact subject,
request/attempt identity and protected channel mapping; see docs/ASTRA_SLACK.md.
No Grok browser login or manual rephrasing of the request is required.

Do not subscribe Grok to every Slack message.
Only explicit commands or normalized workflow events invoke Grok.

Slack decisions/audits are not durable until the corresponding GitHub control
record/decision/audit pointer is written.

## 23. Credentials

Target logical permissions:

- Grok command relay: only enough permission to invoke approved control-plane
  commands and relay narrow status; no source write, PR creation, admin,
  secrets, delete or merge.
- DEVIN / GROK_BUILD / GLM / CURSOR builder adapters: only the assigned repo + task
  branch/PR; no merge/admin. Provider credentials may differ, but each active
  writer remains scoped to one canonical task lineage.
- Cheap writer: only explicitly assigned branch/task.
- Reviewer: repo/PR read + finding comment only; no source write.
- Astra: repo/PR read + architecture/audit/decision evidence write only; no
  source write/merge.
- Mechanical layer: event validation + control-record/claim/status mutation and
  configured builder/reviewer launch only; no source write/merge.
- User: final authority.

Technical enforcement is separate from this document and must be verified
before claiming least privilege is enforced.

A shared build host is one security domain: separate worktrees/processes are
not credential isolation. Do not place production/root/payment secrets on that
host merely because builders use different worktrees.

## 24. Cost discipline

Deduplicate raw events and aggregate CI matrices before any AI invocation.
Unchanged state does not produce another status, review or Astra request.

Astra receives architecture exceptions, explicit milestone/release packets and
A3 gate-ready evidence, not routine progress chatter or every A1/A2 PR.
The assigned builder owns repository investigation and the entire
test/fix/retest loop. Independent reviewers own routine non-author review.

No polling, standing sessions or transcript surveillance. Grok should execute
or relay short deterministic commands rather than read project context.

Measure validated completed-task throughput, per-builder cost, Astra usage,
Grok usage, User interventions, review findings, rework and integration
conflicts. Report unavailable usage metrics as unknown.



## 25. Program mode

Design: `docs/PROGRAM_MODE.md` (A3 design PASS at `424a661`).
Operations: `scripts/control_plane_program.py`.
Coordinator decision table: `docs/COORDINATOR_PLAYBOOK.md`.
Everything in §1–§24 still applies. Program mode changes three things only: who
creates the canonical task, how lanes are chosen, and when a session slot is
released.

Canonical task (`operation=materialize`):
- the plan is `.aiops/program.json` in the product repository at an exact
  `plan_commit`;
- `TASK_ID = <PROGRAM>-<NODE>`;
- the host records a SUBMITTING create request before the GitHub create call;
- an UNKNOWN create is never re-sent because a listing shows no issue;
- the issue carries `ASTRA_TASK_KEY_V1 ... request=<id>`;
- duplicate open issues stop as `DUPLICATE_TASK`.

Authority: every lane posts with the same GitHub account, so GitHub text
(issue bodies, the control record, review bodies) is never a gate input. Gates
read the host ledger, the plan at the host-recorded `plan_commit`, and live PR
state. The control record is a projection.

Writer (`operation=start`):
- `depends_on` nodes must be DONE (their pinned delivery PR merged at the
  delivered head); otherwise `WAITING_ON_DEPENDENCIES`; program nodes are PR
  deliverables only;
- the host plan commit advances only right before the envelope is rewritten;
- a record in SUBMITTING or UNKNOWN whose request the host has fenced
  (FAILED_PRESTART or RECONCILED) resumes as attempt + 1;
- the lane is the first idle program lane (DEVIN, GROK_BUILD, GLM, CURSOR)
  in the fixed order, or, for a resume, the owner lane: the lane
  of the task's first host writer session;
- the envelope is rendered from the plan and pinned by its body hash;
- a stale `plan_commit` is refused (`STALE_PLAN`);
- the launch then follows §6–§8 unchanged.

Session release (`operation=reap`):
- runs after the session posts its deliverable (`ASTRA_DELIVERY_V1`, a review
  verdict, DECISION_REQUIRED, BLOCKED or STALLED);
- the host retires the CONFIRMED row only when the lane UID has no live
  process, by an outside `/proc` scan plus a race-free census taken inside the
  lane (`--quiescence`: freeze every lane process with kill(-1, SIGSTOP),
  list, thaw);
- an adapter's FAILED_PRESTART frees the slot only after the host sees the
  lane empty; otherwise the launch is UNKNOWN and the slot is kept;
- `start` returns DONE, and never redispatches, once the pinned delivery PR is
  merged at its delivered head;
- the control record shows `RELEASED`; ownership is unchanged;
- UNKNOWN or SUBMITTING stays operator-only (§9).

Resume:
- uses the explicit retry of §7 (`attempt_id + 1`) on the same owner lane;
- is admitted only when the host shows the previous request RECONCILED as
  `SESSION_TERMINAL_VERIFIED` (reap) or `SESSION_TERMINAL` (operator reconcile
  with the session id); the host never replays a released request;
- a CONFIRMED record with that host result is a lost projection and is repaired
  first.

Review (`operation=review`, mirrored in `reviews[]`):
- runs only after the host has pinned a signed delivery for the released
  current writer attempt, and the live PR is still at that head;
- the audit floor comes from the plan; the issue body must equal the envelope
  rendered from the host-recorded plan;
- the review id binds the repository, task, delivering writer launch id, head
  and slot;
- the reviewer lane is the first idle program lane, excluding every writer lane
  of the task and the lane of the other slot;
- a slot released without a verdict (the reviewer's own signed blocker, or an
  operator reconcile) is re-dispatched as attempt + 1, at most
  `MAX_REVIEW_SESSIONS` (3) sessions per slot and head, then
  `REVIEW_RETRIES_EXHAUSTED`; launch ids the host already holds are skipped;
- a head change invalidates the review.

Session signatures and pins (`operation=reap`):
- each writer and reviewer packet carries a fresh secret (`delivery_nonce`,
  `review_nonce`), readable only by that session's lane UID and the host
  ledger; the adapter gives the session a private signer that prints
  `ASTRA_DELIVERY_V1 pr=<n> head=<sha> mac=<hmac>` or
  `ASTRA_REVIEW_V1 review=<id> head=<sha> verdict=<...> depth=<A1|A2>
  required=<A1|A2|A3> contract_change=<NO|YES> mac=<hmac>`, where `depth` is
  VERIFIED_REVIEW_DEPTH and `required` is VERIFIED_REQUIRED_DEPTH (§13);
- a blocker is the session's own signed line
  `ASTRA_BLOCKED_V1 kind=<DECISION_REQUIRED|BLOCKED|STALLED> launch=<id> mac=<hmac>`;
  unsigned text never releases a session, and the host refuses to release a
  keyed session without a signed line;
- a writer's evidence is a control-actor comment on this task written after
  the attempt's host reservation, with exactly one signed delivery or blocker
  line; a delivery's PR must come from the task branch `astra/<task id>`;
- a reviewer's evidence is its PR review at the reviewed head, after its host
  reservation, with exactly one signed line for its review id, or a fresh task
  comment with its signed blocker line;
- the task a launch belongs to comes from the issue's task key and the host
  materialization, never from the control record;
- the host verifies the MAC with the stored packet, refuses a PR that is
  already another task's delivery, and pins the result write-once: a repeated
  reap must present the same evidence and line.

Merge readiness (`operation=merge-check`):
- computes §18 for the exact head from host pins: the current writer's pinned
  delivery must name this PR and head, for the current task revision (the host
  row's revision must equal the one derived from the host-recorded plan
  commit); verdicts count only when pinned for this delivery's review ids,
  from distinct lanes that never wrote the task; any active review blocks;
  dependencies must be DONE;
- EFFECTIVE_AUDIT_FLOOR = max(plan AUDIT_FLOOR, every pinned
  VERIFIED_REQUIRED_DEPTH) sets the review count, the required review depth
  and the second review slot; A3 adds ASTRA_GATE=ARCHITECTURE; program mode
  promotes a plan A0 to A1 (no §16 qualification path exists);
- the verification gate requires every check named in the product's
  `program_required_checks` to have succeeded on the head, and no observed run
  to be incomplete or failing; an undeclared list is not ready;
- anything not machine-computable makes it not ready: Astra gates,
  undeclared project merge prerequisites, labels `needs-user`, `blocked` and
  `decision-required`;
- merge stays with the User until decision M1 is recorded.

Scheduling (lanes free up in this priority order):
1. resumes of the lane's owned tasks;
2. pending reviews that lane can take;
3. new builds, only while tasks waiting for review or fix number fewer than
   `max_active_sessions`.
