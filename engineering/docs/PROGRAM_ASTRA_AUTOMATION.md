# Program Astra automation — scoped audit, consultation and merge receipts

Status: **Option C candidate; User adoption PENDING; end-to-end rollout NOT_READY**.
The [exact-HEAD Fable review of #44](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/44#issuecomment-5910103843)
was DECISION_REQUIRED. This continuation preserves that result and proposes
restricted corrections; it invents no accepted User decision. M1/M5 and the
existing manual operator path remain operative until the additional governance
decision, independent exact-HEAD A3 audit and actual host qualification. No host
install or activation is claimed.

## Problem and resulting behavior

The old `merge_check` unconditionally held every Astra-gated delivery, even after
a passing architecture or milestone audit. The coordinator also waited for an
operator to invoke Fable consultations. This change adds fixed `astra-audit` and
`astra-consult` operations and lets a specifically delegated plan node pass its
Astra merge condition from a protected current-head receipt after adoption.
Routine CI and review failures still return to the same writer. This restricted
Option C candidate always holds current-head reviewer contract-change YES,
declared RELEASE gates, User-only nodes and consequential scope/permission/cost
decisions for the User, regardless of any passing scope receipt.

The manual bootstrap design/registration PRs are not fabricated program tasks.
They keep their normal User merge authority. The new behavior applies only after
an approved plan is on the default branch, has a real approval pointer and is
recorded by the existing host materialization protocol.

## Explicit plan fields

| Node field | Meaning |
|---|---|
| `astra_auto_merge: true` | The User delegates the executor for this exact node's already approved scope, including its required Astra gate. A protected Fable PASS/PASS_WITH_NOTES plus WITHIN_APPROVED_PLAN is required. |
| absent or false | Existing behavior: an Astra-gated node's merge stays with the User. |
| `user_merge: true` | The node is a User decision/release/risk gate. Automatic merge is forbidden at every depth. |

Both true is invalid. The booleans do not create, waive or downgrade an audit.
Reviewer-required depth can still promote the task to A3. An original RELEASE
gate is preserved and delegation is rejected before promotion; architecture
normalization cannot erase the User release reservation. Any current-head
contract-change YES blocks automatic merge and requires the durable Astra/User
decision reflected in the revised task. Fable PASS alone cannot grant that
decision. Missing approval, new scope/permission/cost and risk acceptance also
retain the User path.

An `approval_pointer` containing PENDING is valid as draft data for `validate_plan`
but is refused by the operational plan loader. It cannot materialize or dispatch.
Replacing the pointer is a registration/approval operation, not an inference from
the existence of a design document or a GitHub PASS-looking comment.

## Fixed operations

| Operation | Arguments in `program_args` | Result |
|---|---|---|
| `astra-audit` | `pr_number`, exact `head` | Protected gate audit for the host-pinned current delivery. |
| `astra-consult` | `question_comment_id` | Protected answer to a current host-pinned decision question. |
| `merge-check` | Existing arguments | Recompute gates; return `ready`, reasons, `astra_status`, `astra_result`, `scope_result`, `astra_audit_allowed`. |
| `merge` | Existing arguments | Recompute the complete predicate and pin the accepted HEAD. |

They run in the existing task workflow concurrency group. Fable invocations are
additionally serialized across all tasks by one protected host model lock. BUSY
means no model request was admitted; the coordinator can try that unadmitted
operation on its next event/heartbeat. RUNNING, UNKNOWN or ERROR for an admitted
request is fenced and requires host reconciliation; it is never automatically
resubmitted. A different binding cannot reuse a passing receipt and is not
reconciliation of an unresolved prior request. The next completed operation
chooses the playbook row; a changed plan/task/writer/PR/HEAD invalidates the
observation. Fixed standalone operator audit/consult/preflight and program model
commands also acquire one protected nonblocking account-model lock before context
creation or admission. A contending command returns BUSY without admitting a run;
the successful command retains the lease for its full existing duration. Read-only
check/reconcile operations remain available. Claude sessions outside this fixed
wrapper are not covered and must still be scheduled separately.

The runtime waits long enough for the existing three-hour Fable model limit plus
archive preparation/transport. It does not cancel an audit after the old 15-minute
workflow timeout. Fable's extra-usage checks, fixed model, restricted tools,
read-only account, secret filtering and no fallback remain in force. User-approved
fix-request item 9 permits only a valid rate-limit info object with status
`allowed`/`allowed_warning`, boolean `isUsingOverage=false` and an absent
`overageStatus` key. Other missing/malformed or contradictory billing proof stays
a failure; actual extra usage still stops immediately. The accepted exception
preserves the missing key and never interprets it as rejected. It does not switch
an account's billing settings or supply the explicit rejected evidence required
for a protected quota readmission. One active job holds its
task concurrency group and runner for the long invocation; queued GitHub runs
are not model admissions. Workflow cancellation does not prove a root child
stopped. Timeout, permission or transport errors remain fenced while the operator
checks the protected journal. Failure receipts and caller reports retain bounded
redacted reasons; they do not print raw stdout/stderr or credentials.

## Authority and binding

`control_plane_program.py` calls only:

```text
sudo -n /opt/aiops/bin/aiops-fable program
```

Its bounded JSON stdin carries the operation, target and GitHub token. The token
does not appear in argv, saved receipt, model input or command output. There is no
shell evaluation, environment preservation, arbitrary path, model choice or
caller-supplied verdict. The sudoers rule admits only this exact command, for the
runner identity; builder identities receive no new root command. This is a
narrow proposed exception to the existing runner-root prohibition and requires
its own explicit User security-boundary adoption. It grants no shell, arbitrary
Python/arguments, init, reconcile, new model or token search permission.

Before GitHub/model operations, the installed service requires protected
`program-astra-authorization.json` beside its central config. Its schema-1
ACTIVE/runtime-enabled record binds the accepted full runtime commit, actual
User decision/independent audit/host qualification pointers, qualification
digest and installed fingerprint. The fingerprint covers Fable, all support
modules, central config/profiles/activation and the seven operative policy documents:
AGENTS, DISPATCH, PROGRAM_MODE, CONTROL_PLANE_RUNTIME, COORDINATOR_PLAYBOOK,
PROGRAM_ASTRA_AUTOMATION and the adoption proposal. Ownership and directory chains
are protected. Missing/PENDING authorization or changed hashes reject direct
stdin entry as well as workflow entry; repository examples and caller assertions
are not grants. Filling pointer strings is not actual adoption/qualification.
The service independently validates the installed activation's enabled state
and requires `approved_commit` to equal its `activated_runtime_sha`. Deactivation
or a different activation SHA invalidates direct entry even if a prior record
was ACTIVE. The activation file is protected and included in the fingerprint.

Installed `control_plane.ROOT` resolves to `/opt/aiops/lib`; the copied service
files are not assumed to be a git checkout. A checkout-only activation check is
insufficient for this privileged entry. The workflow independently retains its
normal activation/attestation check. The coordinator consumes computed service
authorization results and never judges rollout qualification itself.

The root-owned bridge independently recalculates the canonical issue, program,
node, host-recorded plan commit, task revision, current writer launch, released
sessions, required non-author review pins, delivery PR/head and gate/depth. It
checks the live same-repository task branch/default target and the rendered task
envelope. The plan must be on the default branch and the project eligible.

The audit receives both the actual base/head trees and the full immutable approved
plan tree. The proposed HEAD's edits to a plan or policy cannot enlarge the
approved scope. The model returns the usual audit result plus `scope_result`.
The wrapper, not the model, attaches the mechanically obtained binding.

Receipts live under the existing root-owned Fable run directory's `program/`
subdirectory, mode 0700. Admission is written and fsynced before invoking Fable.
Results are atomically replaced and directory-fsynced. Receipts and locks reject
symlinks, non-root ownership and group/other writable state. The receipt key binds
the action, complete context and the installed Fable/bridge/program/core hashes.
A different HEAD, plan, writer attempt or installed version cannot reuse it.

GitHub audit/consult comments are a human projection, never a gate input. Ordinary
manual audit receipts do not carry a program scope binding and cannot be promoted
to one. Within the bridge the protected journal governs deduplication; the normal
tool's comment-level AUDIT_EXISTS/CONSULT_EXISTS check is bypassed once for that
new admitted program request. A later heartbeat reads the same protected result
without running the model again. Manual `aiops-fable audit` behavior is unchanged.

## Consultation path and resume fencing

Opus remains the first answerer under D3. When it escalates, the coordinator uses
`astra-consult` instead of asking an operator to type a Fable command. Writer
questions must be the current released host-pinned DECISION_REQUIRED comment.
Reviewer questions are a task comment beginning with:

```text
ASTRA_REVIEW_QUESTION_V1 review=<current host-pinned review id> head=<reviewed HEAD>
```

The bridge verifies that exact current review's pinned DECISION_REQUIRED or
contract-change verdict. Arbitrary question prose or a forged marker without a
matching host pin grants nothing. The text is input data to Fable, not authority.
The source is pinned to the delivered head for reviewer questions or the approved
plan commit for a blocked writer; the approved plan tree accompanies both.

The operation returns the actual protected ANSWERED/USER_REQUIRED result. ANSWERED
allows the existing same-owner resume path; USER_REQUIRED keeps the User path.
For every prior released writer, `start` checks protected consultations and audits
before changing the issue, pin or writer, including User-only and nondelegated nodes.
The read-only query validates the proposed plan as an approved default-branch
descendant with the same canonical program/node. USER_REQUIRED or an unresolved admitted request prevents resume
even if somebody removes GitHub labels or rewrites a comment. Semantic supersession
requires a same-repository, separately merged plan-decision PR and an exact
`superseding_decisions` record present in its merged plan. Required record keys
are `schema_version` (1), `repository`, `program`, `node`, `issue`,
`writer_launch`, `previous_plan_commit`, `previous_definition_sha256`,
`definition_sha256` and `decision_pointer`. Both node digests are SHA256 of
canonical validated node JSON after recursive string-edge normalization.
The record must bind the protected blocked node/writer and the actual new node;
the pointer cannot be the task issue/comment, a delivery PR or a task branch.
Title-edge whitespace, a URL-looking approval change or an unrelated main commit
cannot supersede the decision. Current-default plan admission refuses removal or
renaming of protected canonical node identities; no ancestry migration is adopted.
The separate User-merged revision requirement is policy, not a claim of unique
human identity under M4's shared credential. Such a revision does not itself
reconcile an unresolved execution. Removing delegation
from the proposed node cannot remove an existing protected execution fence. A
missing or unqualified protected decision service holds this resume path.
Program delivery merge-check also rejects plan/policy changes: `.aiops/`,
`.github/`, `docs/aiops/`, `docs/decisions/`, `RUNBOOKS/`, any `AGENTS.md`, fixed
central policy paths and the local paths bound by `authoritative_doc_pointers`.
Those revisions must follow a separate User-merge path; an ordinary delivery
cannot change the scope/approval that would authorize its own automatic merge.
Deleting a receipt, changing HEAD/plan/tool hash or editing the projection is not
terminal proof. This guard grants no new authority to Opus. Read-only
decision-status may inspect an operator-reconciled terminal writer and derive
its binding from the host despite an edited issue envelope; this relaxation
creates no delivery/review pin. Audit/consult retain verified-release and exact
envelope requirements.

## Recovery candidate and readiness

The protected append-only journal and bounded operator terminal-failure
reconciler are source candidates described in
[PROGRAM_FABLE_RECOVERY](PROGRAM_FABLE_RECOVERY.md). Builder-session reconcile
does not settle an admitted Fable run. Only verified failed termination with
unstarted publication can settle its execution fence; original ERROR remains.
Unproven UNKNOWN, ambiguous publication and lost archive stay fenced. Full
automation is NOT_READY until the final recovery/runtime implementation is
independently audited, installed and actually qualified. A local fixture is
insufficient and no source check is represented as host qualification.

## Rollout on the host

The #46 `a8b7355712c58de8d27c85a535fb241a09a4037c` and historical #47
`94a768e19df12703ea0b9a49e49972feb2f6ef4f` checkpoints lack the external-field
validator required by KIX R-1 §6-2. They must not be adopted alone. Use #47
`e34868c6a12e5488224095b3248e59c8be9128f1` or a verified descendant retaining
the `user_merge` guard and rejection of every `depends_on_external` field,
including an empty field. The actual final accepted HEAD still requires its own
independent A3, User adoption and host qualification; this minimum checkpoint is
not a PASS or authorization pointer.

1. Record the actual User governance decision for restricted Option C and the
   runner-root exception; keep its pointer PENDING until then. Audit the new
   implementation's exact HEAD with ordinary ARCHITECTURE/A3 and obtain normal
   User merge. #44's old DECISION_REQUIRED is not transferred to a new HEAD.
2. Keep active/UNKNOWN task and model requests fenced during rollout. Install
   the audited `control_plane_fable.py` as `/opt/aiops/bin/aiops-fable` and install
   `control_plane_program_astra.py`, `control_plane_program_receipts.py`,
   `control_plane_program_quota.py`, `control_plane_program.py`, `control_plane.py`
   from the same accepted commit into `/opt/aiops/lib/program/`, root-owned and
   not group/other writable. Protect every directory in the path chain.
   Preserve the complete protected state on durable storage and migrate the
   existing ledger/receipts/journals/quota records, never an empty reset, as required
   by `PROGRAM_FABLE_RECOVERY.md` "Durable host state and migration".
3. Copy the same commit's central config and project profiles to
   `/opt/aiops/lib/.github/control-plane/`, with a protected root-owned directory
   chain. Install all fingerprinted policy documents (`POLICY_PATHS`, including
   `docs/PROGRAM_FABLE_RECOVERY.md`) from the same
   accepted commit at their paths beneath `/opt/aiops/lib`. The support's
   `control_plane.ROOT` resolves to `/opt/aiops/lib`.
   Copy the actual accepted activation through its normal operator process;
   its enabled state and activated SHA must agree with the service record.
   Root bridge host calls use the existing installed helper and ledger. No builder
   adapter, actor identity, credential or existing host admission gate is replaced.
4. Qualify rejection of missing/PENDING service authorization and stale code,
   config/profile/policy hashes, including direct stdin entry. Only after actual
   adoption and qualification may the operator apply the exact sudoers candidate
   and validate it with `visudo -cf`; no general root command is added.
5. Run existing Fable preflight. Installation's blocked-billing qualification
   still requires actual extra-usage rejected evidence; the narrow ordinary
   subscription-warning exception above does not supply that evidence.
   OVERAGE_UNVERIFIED means missing or malformed billing proof, not permission to
   change account settings. OVERAGE_NOT_BLOCKED identifies affirmative contradictory
   billing evidence. Retain either failure and diagnose the sealed raw archive;
   do not weaken the check or automatically retry a protective guard stop.
6. Qualify a canonical no-cost fixture task: valid scope audit, duplicate request,
   stale head/revision, missing reviews, User-only node, USER_REQUIRED resume fence,
   failing CI, RELEASE/contract-change holds, root state/permissions, bounded
   stdin/token argv/log absence, cancellation and actual merge race.
   Local fake tests in this PR are not host qualification evidence.
7. Finish and qualify the separate protected Fable reconciliation implementation
   before full rollout. Until then keep service qualification/authorization and
   full activation NOT_READY/PENDING. Record the actual accepted commit,
   decision/audit/qualification pointers, qualification digest and installed
   fingerprint in protected service authorization; never install the PENDING
   source example as a completed grant. Refresh activation through the existing audited
   activation process. This PR changes RUNTIME_PATHS and leaves activation.json
   unchanged; merging alone cannot silently activate these new executable paths.
8. Adopt the product plan revisions' explicit flags only after the central bridge
   is qualified. Keep KIX user_merge decision nodes and its live-funds/chain/public
   restrictions. Keep ZARI screen-baseline approval and film actual production/
   billing/credential/service qualification as their separately scoped decisions.
9. Replace registration PENDING pointers with actual approval/audit/merge evidence,
   retarget stacked registration PRs after their design predecessors merge, and
   use the existing authenticated program start operation. No fake task envelope,
   MAC, session pin, audit result or runtime state is authored to accelerate it.

## Validation and remaining limits

The follow-up [program execution evolution design](PROGRAM_EXECUTION_EVOLUTION_DESIGN_KO.md)
specifies typed terminal quota failures, protected one-shot resumption,
cross-repository evidence and proposal-only plan migration, and shared completion
reporting with separate qualification/acceptance/release facets. Its
[implementation tasks](PROGRAM_EXECUTION_EVOLUTION_TASKS_KO.md) are prospective;
they do not change the ERROR/UNKNOWN fences or APIs implemented by this document.
The follow-up must be separately adopted, implemented and host-qualified.

Regression tests cover the existing program/host-pin workflow and Fable tool.
New tests cover scope delegation, user-only merges, required CI/reviews, exact
bindings, pending approval, receipt dedupe/crash/error/busy fencing, secret
transport, canonical question authority and protected USER_REQUIRED resume.
These run without network, paid resources or live providers.

The configured single GitHub identity/PAT residual under M4 remains; this bridge
does not claim to revoke that token's direct API permissions. Cross-repository
dependencies are still not read by the runtime; existing pending-node plan
revisions remain required. An unavailable service, missing hardware, human screen
or artwork approval, live financial/chain permission or a new consequential
design choice remains an explicit blocker, not a fake completed node.


Recovery implementation status: [PROGRAM_FABLE_RECOVERY](PROGRAM_FABLE_RECOVERY.md) supersedes the earlier unimplemented-reconciler description for source capability only. Installation, independent A3 and actual host qualification remain PENDING; ambiguous UNKNOWN is still fenced.
