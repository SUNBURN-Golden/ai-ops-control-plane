# AI Engineering Control Plane

NO STANDING ROUTINES (sole exception: the program-mode coordinator, event-triggered plus one hourly heartbeat).
NO POLLING (the heartbeat recomputes state from GitHub; it does not watch sessions).
NO REASONING WHEN A RULE CAN DECIDE.
ONE NORMALIZED EVENT → ONE SHORT ACTION → END SESSION.

Program mode (User decisions of 2026-09-29, `docs/PROGRAM_MODE.md`, A3 design PASS at `424a661`)
runs every registered product from one User "start" command through the same gates below.

User decides. Astra owns architecture, architecture exceptions and explicit milestone/release gates.
Since User decision M5 (2026-09-30) the Astra role is held by Claude Fable (`claude-fable-5-1`),
run read-only on the host through the fixed `aiops-fable` tool (`docs/CONTROL_PLANE_RUNTIME.md`).
The mechanical layer dispatches deterministically; Grok is only an optional command relay.
Devin, native Grok Build, GLM and Cursor CLI are peer autonomous builders. GitHub stores durable truth. Slack is a cockpit.

## 1. Separation of concerns

These three files have separate authority:

- `AGENTS.md` — actor authority, safety boundaries, source-of-truth rules.
- `TASKS/TEMPLATE.md` — task-envelope data shape only.
- `RUNBOOKS/DISPATCH.md` — deterministic event/claim/gate procedure only.

Repository-specific technical contracts, locked files, architecture documents,
ADRs, immutable task documents, phase gates and safety rules remain
authoritative in their technical domains.

If rules conflict, do not guess. Return `DECISION_REQUIRED` with exact
pointers.

## 2. Roles

| Role | Job | Must not |
|---|---|---|
| USER | Final authority: product scope, consequential architecture choice, risk acceptance, merge | Be silently substituted by an agent |
| ASTRA | Principal Architect / Design Authority; architecture exceptions; explicitly required milestone, architecture and release audits. Held by Claude Fable (M5), run through `aiops-fable audit` and `aiops-fable consult`; the protected `aiops-fable program` policy is adopted but its installation/qualification is pending (§13) | Become the routine ticket manager or default A1/A2 reviewer; implement audit fixes; audit a change it authored or modified; write code or documents; run outside the fixed tool |
| GROK | Optional human-facing command relay to the mechanical control plane; host operator, including running the fixed `aiops-fable` commands (M5) | Engineer, architect, reviewer, semantic router, event bus, polling daemon; edit an Astra prompt, argument meaning or result |
| BUILDER | One configured autonomous writer: DEVIN, GROK_BUILD, GLM or CURSOR; investigate → implement → test/debug → PR/evidence | Change approved architecture silently; write outside the assigned task/worktree; merge |
| REVIEWER | Configured non-author read-only reviewer; may be a different builder lane or User-designated external lane | Modify the reviewed change or become a second writer |
| CHEAP_WORKER | Explicitly authorized mechanical work | Become a second writer on a substantive task |
| COORDINATOR | Program mode only (Claude Sonnet): follow `docs/COORDINATOR_PLAYBOOK.md`; call fixed operations (materialize, start, review, reap, merge-check, merge); after separate policy adoption and protected service authorization, call the candidate astra-audit/astra-consult operations; route questions; post progress | Write or review code; judge design or service qualification; choose a lane (the mechanical layer computes it); create task issues directly; touch the host; merge other than through `operation=merge` (User decision M1) |
| OPUS | Program mode only (Claude Opus): first answerer for DECISION_REQUIRED; may approve a *small design exception* (`docs/PROGRAM_MODE.md` §5) | Author code in the task it rules on; audit a design it drafted; approve anything outside the small-exception definition |
| MECHANICAL_LAYER | Actor validation, task serialization, builder dispatch, durable control record, event dedupe, gate aggregation, lane selection in the fixed order | Perform semantic engineering or architecture judgment |
| SLACK | Command/status/decision cockpit | Persistent source of technical truth |
| GITHUB | Persistent source of truth and durable control-record projection | Be treated as an atomic lock merely because comments exist |

User explicit decisions outrank every agent.
Architecture-affecting decisions require Astra analysis followed by User
decision and a durable GitHub pointer.

Builder and reviewer identity are canonical task/control-record fields.
Grok never chooses either by reading code, prose or model performance.

## 3. Mechanical control layer is mandatory

Raw Slack/GitHub/provider events do not directly authorize external actions.

Before any builder, reviewer or optional Grok relay adapter is invoked, the
mechanical layer must:

1. validate the event actor/source against configured allowlists;
2. map the event to one canonical TASK_KEY = REPO + TASK_ID;
3. process control-state mutation under a single-writer serialization primitive
   for that TASK_KEY;
4. load/update the canonical control record;
5. reject stale/duplicate/self-generated events;
6. emit a normalized event containing the required identifiers.

A GitHub issue/comment may be the durable projection of the control record, but
comment existence is not an atomic claim. Use a real per-task serialization
primitive such as a queue, lock or GitHub Actions concurrency group with one
writer for control-state mutation.

Automation remains disabled until the mechanical layer is implemented,
independently audited at its exact SHA and explicitly enabled by User.
Until then User may perform serialized manual dispatch under the runbook.

## 4. Canonical task and ownership

EVENT_ID identifies one delivery/event.
TASK_ID identifies one engineering job.
They are not interchangeable.

Every task has exactly one canonical GitHub issue/task pointer and one durable
control record.

A different EVENT_ID for the same TASK_ID must reuse the existing control
record and owner. It must not create a second writer.

One substantive task has:

ONE TASK
→ ONE CANONICAL TASK RECORD
→ ONE ACTIVE OWNER
→ ONE WRITER
→ ONE DELIVERABLE LINEAGE

Independent reviewers are read-only and are never a second writer.

## 5. Grok authority

Grok is an optional messenger / command runner, not the control plane.

Grok may:

- forward an explicit authenticated User command to a fixed control-plane command,
  including the User's program-mode "start" command;
- execute exactly one pre-authorized mechanical action named by a normalized event;
- relay exact CI/review/audit/blocker pointers;
- run `aiops-fable audit` or `aiops-fable consult` with exactly the arguments a
  request names, and relay the result the tool posts, literally (User decision M5).
  Running the tool is not code review by Grok: Grok reads no diff and forms no verdict;
- post one short status or receipt;
- end the session.

Grok must not:

- infer architecture, protocol, schema, API, security, concurrency,
  consistency, financial or blockchain design;
- select a builder or reviewer by semantic judgment;
- rewrite requirements or task specifications;
- semantically classify code/diffs;
- debug CI;
- perform code review;
- poll or monitor;
- repeatedly read worker transcripts;
- create a second writer;
- auto-merge.

No control-plane state transition may require Grok reasoning or availability.
If Grok is unavailable, the same authorized mechanical command may be invoked
through another authenticated caller without changing task ownership or gates.

## 6. Builder autonomy

DEVIN, GROK_BUILD, GLM and CURSOR are peer builder implementations behind the same
task-owner contract. The canonical task/control record names exactly one active
builder for a substantive task.

The assigned builder is a ticket owner, not a keyboard proxy.

Inside approved architecture, contracts, scope and invariants, the assigned
builder may choose ordinary implementation algorithms, data structures,
necessary refactors, debugging strategy and test/fix iterations without routine
Astra approval merely because multiple implementation choices exist.

The builder must escalate only when completion requires a consequential change
outside approved boundaries, such as changing an approved invariant, schema
contract, public contract, authority/security boundary, protocol semantics,
financial semantics or approved architecture.

Normal loop:

investigate
→ implement
→ test
→ fail
→ debug
→ fix
→ retest
→ PR/evidence.

A CI failure does not terminate this loop. Exact feedback returns to the same
owner. No arbitrary two-failure cutoff. Do not involve Astra in ordinary
debugging.

The builder may explicitly report STALLED. A mechanical budget/cost guard may
emit BUDGET_LIMIT_REACHED. Grok does not infer either condition.

## 7. Cheap-worker / A0 qualification

Grok never decides that a change is trivial by reading the task or diff.

CHEAP_MECHANICAL/A0 is allowed only when the canonical task envelope already
contains:

- EXECUTION_CLASS: CHEAP_MECHANICAL;
- A0_AUTHORIZATION_POINTER from User/Astra or an explicitly approved
  deterministic intake policy;
- project-specific A0 eligibility.

If any required A0 field is absent, default to BUILDER_STANDARD and A1.
A BUILDER_STANDARD task must have a configured BUILDER_ID before dispatch.

After completion, the mechanical layer validates objective facts such as
changed paths and forbidden/locked paths. If A0 qualification no longer holds,
the task is promoted to A1 and must receive the normal independent review.
Astra is added only when the task's Astra gate requires it.

A0 never overrides repository-specific locked-file, evidence, bookkeeping or
validation requirements.

## 8. Review depth and Astra gates

Review depth is cumulative:

- **A0** — explicitly authorized typo/format/mechanical changes; no separate
  reviewer or Astra gate unless repository rules require one.
- **A1 STANDARD** — independent non-author correctness review: acceptance
  criteria, tests/evidence, regression, scope and contract compliance.
- **A2 DEEP** — A1 plus relevant concurrency, state machine, persistence,
  payment, security and protocol behavior.
- **A3 ARCHITECTURE** — A2 plus an Astra architecture gate covering
  invariant/schema/public-contract/blockchain/financial/authority boundaries.

A task also carries ASTRA_GATE:

- NONE — routine A1/A2 work ends after required independent review and
  mechanical gates;
- MILESTONE — Astra reviews the explicitly scoped milestone packet;
- ARCHITECTURE — Astra performs the architecture gate for the exact task/head;
- RELEASE — Astra performs the explicitly scoped release gate.

A3 requires architecture-depth audit regardless of a weaker task default;
ordinary gates promote to ARCHITECTURE. A declared RELEASE gate stays RELEASE,
retains its A3 depth when applicable and remains User-reserved. Promotion never
erases that release authority.

The writer's TOUCHED_AREAS and CONTRACT_CHANGE_REQUIRED fields are advisory.
The independent reviewer must inspect the actual diff/evidence and report the
verified review depth, touched areas, contract-change requirement and exact
reviewed HEAD/evidence SHA.

Independent review requires a reviewer that did not author or modify the
reviewed change. A different builder may review only in a read-only, non-author
session; participating in implementation disqualifies that session from the
independent gate.

Astra is not the default routine A1/A2 reviewer. Astra is invoked for
architecture exceptions, A3, and explicit MILESTONE/ARCHITECTURE/RELEASE gates.
On those gates Astra independently checks the actual diff/evidence and relevant
authoritative contracts; reviewer evidence is navigation, not proof.

If Astra authored or modified a change that requires an Astra gate, only User
may designate an independent non-author architecture auditor with a durable
task/revision/scope pointer. The substitute does not inherit Astra's design
authority.

Program-mode question path (User decision 2026-09-29): DECISION_REQUIRED goes to
OPUS first, then ASTRA when Opus escalates, then USER when Astra requires it.
OPUS may approve only a small design exception: a choice inside the approved
blueprint that changes no approved invariant, schema or public contract,
authority or security boundary, protocol or financial semantics, persistence
format or product scope, and that is reversible within the task. Everything else
keeps the Astra → User path below.

Approved consequential contract must change:
stop → Astra analysis → User decision → durable GitHub decision/task revision
→ resume.

A current-head review with VERIFIED_CONTRACT_CHANGE_REQUIRED=YES blocks automatic
merge even when a plan node has `astra_auto_merge=true` and its scope audit passes.
Record the consequential decision and revise the task through the authorized
path; a Fable receipt alone cannot settle that decision. RELEASE-gated nodes
cannot delegate their merge. Check the original declared gate before A3 promotion
so ARCHITECTURE normalization cannot erase the release reservation.

## 9. Review and audit results

Allowed semantic results are:

- PASS
- PASS_WITH_NOTES
- FAIL
- DECISION_REQUIRED

PASS_WITH_NOTES cannot contain an unresolved correctness, invariant, security,
contract or acceptance failure.

Grok relays results literally and never softens FAIL.

Every independent review result is bound to the actual reviewer identity/session,
task revision and exact reviewed HEAD/evidence SHA. Every required Astra audit
is separately bound to its Astra request, actual auditor identity/session and
exact audited HEAD/evidence SHA.

Review/CI/Astra-gate evidence never transfers to a new relevant HEAD or task
revision. A new HEAD requires a new current-head review and, when applicable,
a new Astra gate result.

## 10. Durable truth

Persistent truth order:

1. approved repository contracts / architecture / ADR / phase/task documents;
2. canonical GitHub task + task revision;
3. exact source at known SHA;
4. current-head CI/review/audit evidence;
5. Slack transient messages;
6. agent memory.

Slack tells everyone what is happening. GitHub records what is true.
Agent memory and builder/provider reusable instructions are not independent authorities;
they must reference the current GitHub rules.
Do not commit per-task runtime status, dispatch/audit/review logs or transcripts.
Existing immutable task specs, ADRs and required engineering evidence/bookkeeping
remain valid repository documents. Issue/PR records contain canonical tasks,
control projections, findings, decisions and evidence, not transcript dumps.
Consequential decisions and audit outcomes must have durable GitHub pointers.

## 11. Credentials and actor validation

Use dedicated least-privilege identities.

The mechanical layer must maintain configured actor identities for at least:

- USER;
- ASTRA;
- optional Grok command relay;
- each enabled builder adapter: DEVIN, GROK_BUILD, GLM and/or CURSOR;
- the assigned independent reviewer lane;
- GitHub/CI source.

Ordinary text containing PASS, DECISION or similar words is never promoted to
a control event unless the configured actor/source and required identifiers are
validated.

Grok normally needs only the permissions required to invoke approved
control-plane commands and relay narrow status. It does not require source
write, PR creation, admin, secrets, delete or merge permission.

Builder credentials are scoped to their assigned repository/task branch/PR and
have no merge/admin authority. Reviewer credentials are read/comment only.
Since M5, Astra has no GitHub identity: `aiops-fable` posts the Astra result with the
operator's token, and the model never holds a GitHub credential.
Program mode decision M4 (User, 2026-09-29) departs from this: lanes do not get
separate GitHub identities, so a lane token can merge or push directly. User
accepts that residual (`docs/PROGRAM_MODE.md` §13). Linux program gates do not read GitHub text
as authority; those gates read host pins, the host-recorded plan and live PR state
(`docs/PROGRAM_MODE.md` §4.2).

User decision [D-2026-10-06-MAC-A3-RECEIPT](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/78#issuecomment-6011271646)
records a Mac-only exception for `authority_kind=MAC_LOCAL`. Its exact UTF-8
decision body is [recorded in the repository](docs/MAC_A3_RECEIPT_DECISION_20261006.md):
SHA-256 `46dc2cf0fdd02eeabbc2ac234ffcda0d410820f169d6e87f2f904d7ad939f132`,
comment ID `6011271646`, actor `BeautifulMind-JT` / numeric ID `263336091`,
created/updated `2026-10-06T07:10:27Z`. The Linux protected fixed `aiops-fable`
remains the producer. Mac re-reads that decision and the tool's marked PR comment
through authenticated GitHub API, verifies actor, exact repo/PR/delivery HEAD,
result/depth/schema and immutable body hash, and binds the receipt to its private
task/revision/request before consumption, supervision, inspection and User merge.
This exception trusts the designated operator-account comment as fixed-tool output:
a holder of that account's token can forge a matching comment; the body hash proves
unchanged content, not protected-producer identity. It establishes no new key,
credential, actor, cross-host admission authority or Linux gate exception.
Existing review, current-HEAD CI, RELEASE reservation and User merge requirements
remain in force. See [the Mac receipt contract](mac_app/MAC_A3_RECEIPT_KO.md)
for fail-closed conditions and implementation limits. A repository decision copy
is audit evidence and never replaces the runtime's authenticated live verification.

User decision [D-2026-10-06-MAC-FAILED-PRESTART-NONOWNERSHIP](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/81#issuecomment-6016578811)
approves option A following the [PR81 A3 DECISION_REQUIRED](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/81#issuecomment-6016559549).
It records a separate Mac-only `MAC_LOCAL` generation admission exception:
the open-issue hold for `BeautifulMind-JT/kix-protocol#92`,
`KIX-AGENTS-SCOPE-SYNC`, revision `pc85e614245eb-CURSOR`, request
`077849e0e68f521245e7175f`, CURSOR attempt 1 may be removed using the exact
FAILED_PRESTART control projection with explicit null owner/session together
with the User's pinned confirmation. Requested and projected program/node
tuples must both match `kix/agents-scope-sync`; other issues, revisions,
UNKNOWN/SUBMITTING/CONFIRMED, owners/sessions and conflicting or incomplete
records remain fenced. This exception amends the otherwise conservative
[Mac ownership guard](docs/MAC_OWNERSHIP_GUARD_KO.md) only for that implemented scope.

The designated owner-account confirmation is trusted as non-ownership evidence
without an authenticated Mac read of the Linux ledger. Its original User
thread/message provenance is retained as `USER_DIRECT_CHECK_RELAYED`,
`mac_verified_host_read=false`, `signed_host_receipt=false`; GitHub body hashes
prove unchanged content, not host provenance or a machine signature. User
accepts this trust limit; a holder of the account token can forge comments.
Exact API-body copies of control `5956874897`, confirmation `6016190250` and
decision `6016578811`, including directly recomputed SHA-256, actor and URL
metadata, are [recorded separately](docs/MAC_FAILED_PRESTART_DECISION_20261006_RECORD.md).
Decision body SHA-256 is `1fad73dade5435130c0576315939310e7559250d3813359fd21077aaedf103e7`.
These copies are audit inputs, not replacement runtime observations. Linux
ledger, signatures, protected fixed tools and audit receipts are unchanged;
this establishes no cross-host atomic lock. Exact-HEAD review/CI/Astra gates,
RELEASE reservation and User merge remain required. Product installation,
ownership operations and execution remain with the product owner.

User explicitly approved the Mac checkpoint source repair in message
`Sentinel_18579c8bc67c8191a70e193d38fbd27e`, then pinned the actual
[product approval 6018278031](https://github.com/BeautifulMind-JT/kix-protocol/issues/92#issuecomment-6018278031)
in message `Sentinel_189daa51ad4481918cf45ff2f1306cc0`. The implemented exception
permits only `MAC_LOCAL`, `kix/agents-scope-sync`, job `20531604498943e1`, branch
`aiops/native-2972272cbeb64071`, and the exact AGENTS.md §5 before/after blobs
at plan commit `7481b0e16ce9b903abbffa62249bb91cd9e63cfe`. The original plan blob
and spec are additional narrowing constraints, not fields invented in that short
comment. Exact whole-file bytes and a sole AGENTS.md change are required; other
authority edits, jobs, revisions, mixed changes and UNKNOWN/active attempts stay
blocked. The runtime re-reads the immutable User-account approval at checkpoint
and before push, then rechecks the local binding/scope/file. Checkpoint output
and publication must match the approved committed blob and non-executable tree
mode, irrespective of working-tree filters/index flags. Bounded object reads
disable replacement refs and verify the actual original plan blob/base ancestry.
Only those read processes ignore legacy graft input and commit-graph cache;
repository/global settings and other command environments are unchanged. Publication reads the
approval after other pre-push API reads, checks the exact job HEAD/clean tree,
and pushes that fixed SHA. Its 348-byte UTF-8
body SHA-256 is `000fa33005152da022795f01ef3ab91b3d4d085a81454b41907f8fd44488e951`,
actor `BeautifulMind-JT` / `263336091` / `User`, created/updated
`2026-10-06T14:18:45Z`. [The exact API body](docs/MAC_AGENTS_SCOPE_APPROVAL_6018278031.md)
and [metadata](docs/MAC_AGENTS_SCOPE_APPROVAL_6018278031_RECORD.json) are audit
evidence, never substitutes for live verification. This trusts the designated
account's approval, not a host signature or Linux ledger observation; a holder
of its token can forge comments. It grants no general authority override,
cross-host admission or atomic lock. Existing completed-builder checkpoint
recovery, A3 audit, exact-HEAD CI/reviews and User merge remain required.
See [the bounded checkpoint contract](docs/MAC_AGENTS_SCOPE_CHECKPOINT_KO.md).
Product installation and normal owner recovery remain with the product owner.

Repo-scoped credentials are preferred over one all-repositories write token.

## 12. Relay and provider outage behavior

Grok quota/outage is detected by the caller/mechanical layer. Because Grok is
an optional command relay, Grok unavailability does not by itself change task
ownership, invalidate evidence or block deterministic builder/reviewer routes.

If an explicit command was requested through Grok and the relay is unavailable,
record a narrow RELAY_UNAVAILABLE status/pointer. The same pre-authorized
mechanical command may be invoked by User or another authenticated caller.
Do not create a second writer and do not change task semantics.

Builder/provider outage is separate: record the provider blocker for the
assigned owner. No model is silently substituted. Reassignment requires a
durable authorized control action plus reconciliation/fencing of any unresolved
SUBMITTING/UNKNOWN launch.

Manual dispatch must still use the canonical task/control record and must not
launch when an owner exists or launch state is UNKNOWN.

## 13. Merge

User recorded A / restricted Option C policy adoption on 2026-10-01 in
`docs/PROGRAM_ASTRA_ADOPTION_PROPOSAL_KO.md`, §“PA-1 채택 결정 — 2026-10-01 (A, Option C)”.
Existing M1/M4/M5 records remain intact. Program Astra automation
(`docs/PROGRAM_ASTRA_AUTOMATION.md`) is still a deployment candidate: the F1–F3
fixes, final #47 exact-HEAD independent re-audit, actual installation and host
qualification are required before its fixed `astra-audit` and `astra-consult`
operations or scoped Astra merge delegation become operative. Until then use the
operational M5 Slack request/operator consultation path. The protected
service authorization record must bind that adoption to the accepted runtime,
installed code/config/profile hashes and qualification evidence; missing or
PENDING authorization refuses direct entry as well as workflow entry.

After adoption, a plan node must explicitly set `astra_auto_merge=true` to
delegate the executor for its already approved ordinary scope. Its current-head
protected Fable PASS/PASS_WITH_NOTES and WITHIN_APPROVED_PLAN, all required
independent reviews and computed verification/product gates remain mandatory.
`user_merge=true`, a declared RELEASE gate, or any current-head reviewer
contract-change YES forbids this automatic merge, regardless of the receipt.
Unspecified flags preserve the User merge requirement for Astra-gated work.
No User choice outside the approved scope is delegated.

The candidate's runner-root command is a narrow exception requiring its own
User security-boundary adoption and protected authorization, not a general
root/shell/Python grant. Admitted ERROR/UNKNOWN requests remain fenced. The
protected receipt journal and bounded operator terminal-failure reconciler are
source candidates documented in `docs/PROGRAM_FABLE_RECOVERY.md`. Unproven
UNKNOWN remains fenced; no generic operator override or PASS is created. They
are not installed or qualified. End-to-end automation stays NOT_READY until the
accepted recovery/runtime scope is independently audited and actually qualified. A new plan, HEAD, tool hash or deleted projection is not reconciliation.

Grok never merges.
A reviewer PASS or required Astra PASS is not a merge command.
Only User authorizes merge. Program mode decision M1 (User, 2026-09-29) delegates
only the merge executor to the coordinator, through `operation=merge`. The merge
still requires the computed READY_FOR_MERGE of `RUNBOOKS/DISPATCH.md` §18, pinned
to the exact head, and anything not computable goes to User. Outside program mode,
User merges.

READY_FOR_MERGE is a derived mechanical predicate for the current task revision
and current HEAD. It is not a status string that an arbitrary actor may assert.

## 14. Cost discipline

Astra is invoked for architecture creation/changes, architecture exceptions,
A3, explicitly configured milestone/release gates and re-audits required by
those gates. Clear approved A1/A2 tasks need no Astra preflight, routine plan
approval or duplicate Astra review.

The assigned builder investigates repository details and owns the complete
implementation/test/fix loop. The independent reviewer performs the routine
non-author semantic gate.

Reviewer evidence and writer evidence indexes are navigation, not proof.
On an Astra-gated task Astra independently checks the actual diff, authoritative
contracts and affected behavior. Re-audit starts at the previous audited SHA
delta and unresolved findings, expands to affected dependencies, and issues a
new result for the current revision/HEAD.

Do not use a premium builder for status/grep/typo when an authorized cheap lane
exists. Do not use Grok for semantic routing, code reading, transcript
surveillance or polling. Deterministic delivery uses the mechanical adapter;
Grok is an optional command relay, not a mandatory hop.

Use available subscription capacity for useful implementation, adversarial tests,
regression and non-author review. Do not spend tokens merely to exhaust a quota.
Default: one autonomous owner and one independent reviewer; an additional
read-only review is scoped to an unresolved risk, not a second implementation.
Program mode (User decision 2026-09-29) assigns lanes mechanically. The builder is
the first idle lane in the order DEVIN → GROK_BUILD → GLM → CURSOR. The reviewer
is the first idle lane in the same order, excluding the task's owner lane.
Rules:
- one session per lane, and at most one session per enabled lane in total;
- A1 gets one reviewer; A2 and above get two;
- an owner lane never changes once a task is confirmed.
No additional paid usage, quota purchase, account cycling or automatic fallback.
Grok quota is never a reason to interrupt the builder's own fix/retest loop.

CURSOR identifies the Cursor CLI harness, not a model or native GROK_BUILD.
Its exact installed model/effort slug and subscription route are qualified
separately. Do not label Cursor as GLM or reuse another lane's provider approval.

Since M5 an Astra request is a GitHub pointer (a pull request at an exact head, or a
question comment) that the host operator runs through `aiops-fable`; the tool posts
the result. Slack may notify, and Slack delivery is not an audit or User decision.
Grok is not a browser driver and does not rephrase requests.
Only scoped architecture/A3/milestone/release requests invoke Astra. This User
instruction authorizes engineering Astra consultation; unrelated root-domain
approval rules remain in force.
See `docs/CONTROL_PLANE_RUNTIME.md` (Astra on the host); `docs/ASTRA_SLACK.md` keeps
the Slack sender, whose ChatGPT receiver M5 retired.

Keep existing repository-specific review and safety gates. Measure validated
task throughput, per-builder cost, Astra usage, Grok usage, User interventions,
review findings and rework separately; do not claim savings without observations.

---

User decision [D-2026-10-07-MAC-GLM-PRODUCT-AUDIT](https://github.com/SUNBURN-Golden/ai-ops-control-plane/pull/84#issuecomment-6031603785)
authorizes a distinct independent `MAC_GLM53` / actual `glm-5.3` product auditor
for existing KIX job `20531604498943e1`, PR121, ARCHITECTURE/A3. The earlier Mac
Fable-only producer limitation is amended only for this scoped alternative;
Linux protected aiops-fable and the original Fable channel remain unchanged.
The [original User question/reply and relay provenance](docs/MAC_GLM_PRODUCT_AUDIT_DECISION_20261007.md)
are pinned to comment6031603785, actor263336091/BeautifulMind-JT/User,
created=updated2026-10-07T05:27:40Z, body SHA256
`8bb3a81d2cfd90d229f4c33d92f4ce74785df9225fd445321a460986bae1ce4f`.
Original canonical requests/digests, writer, completed builder,29-line result,
independent reviews, live exact-HEAD CI, negative holds and User merge remain.
A new producer record binds the original request to actual private Mac execution
and an unchanged authenticated GitHub comment. Text-only PASS is not proof.
The existing Mac host account and private execution records are trusted, not a
new signature or protected Linux identity. No new credential/scope, Linux
protected file change, manual ledger rewrite or fresh product builder is granted.
See [the Mac GLM product audit contract](docs/MAC_GLM_PRODUCT_AUDIT_KO.md).

User decision [D-2026-10-07-MAC-EMPTY-ATTEMPT-RECOVERY](https://github.com/SUNBURN-Golden/ai-ops-control-plane/pull/85#issuecomment-6032340624)
approves only a reversible, evidence-preserving quarantine of empty Mac attempt
`d8d194e55af64b0b8a44a487bd41fd94` under existing job `20531604498943e1`.
The [direct approval relay](docs/MAC_EMPTY_ATTEMPT_DECISION_20261007.md) has actor
263336091/BeautifulMind-JT/User, created=updated2026-10-07T06:28:48Z and SHA256
`60fda2c6fc6c8e99c118b6d05e6a09adc3e30388f2d4c6580d74b8256c255699`.
The supported command rechecks live approval, stopped service, original job and
completed receipts, all admitted executions terminal, empty private directory
metadata and absence of UUID references in both ledgers under installer/service
locks. It preserves path/metadata/evidence and atomically moves without replacing
any destination. Existing installer guards and ledger/calls remain unchanged;
a failed post-move normal idle check rolls back. The causal link to MISSING_PROVIDER
is an inference from combined observations, not a UUID-bearing runtime event.
Other folders, deletion, new builders, credentials and protected Linux files are
outside this exception. See [the recovery contract](docs/MAC_EMPTY_ATTEMPT_RECOVERY_KO.md).
