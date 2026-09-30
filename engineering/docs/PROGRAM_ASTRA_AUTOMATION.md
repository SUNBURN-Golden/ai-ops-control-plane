# Program Astra automation — scoped audit, consultation and merge receipts

Status: implementation candidate; independent exact-HEAD A3 audit, User merge and
host rollout are required. This document records the continuation of the User's
2026-09-30 request to prepare the large plans so one Grok start command can carry
their approved development through completion. No host install or activation is
claimed by this change.

## Problem and resulting behavior

The old `merge_check` unconditionally held every Astra-gated delivery, even after
a passing architecture or milestone audit. The coordinator also waited for an
operator to invoke Fable consultations. This change adds fixed `astra-audit` and
`astra-consult` operations and lets a specifically delegated plan node pass its
Astra merge condition from a protected current-head receipt. Routine CI and
review failures still return to the same writer. New consequential decisions
still require the User.

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
Reviewer-required depth can still promote the task to A3. A contract change is
allowed through the delegated merge condition only when that A3/gated delivery
has the passing protected scope audit. Missing approval, a new scope, a permission,
paid resource, schema/invariant beyond the approved blueprint, release or risk
acceptance must produce USER_REQUIRED/DECISION_REQUIRED. A non-gated contract
change still stops for the existing decision path.

An `approval_pointer` containing PENDING is valid as draft data for `validate_plan`
but is refused by the operational plan loader. It cannot materialize or dispatch.
Replacing the pointer is a registration/approval operation, not an inference from
the existence of a design document or a GitHub PASS-looking comment.

## Fixed operations

| Operation | Arguments in `program_args` | Result |
|---|---|---|
| `astra-audit` | `pr_number`, exact `head` | Protected gate audit for the host-pinned current delivery. |
| `astra-consult` | `question_comment_id` | Protected answer to a current host-pinned decision question. |
| `merge-check` / `merge` | Existing arguments | Recompute ordinary gates plus protected current-head scope receipt for delegated nodes. |

They run in the existing task workflow concurrency group. Fable invocations are
additionally serialized across all tasks by one protected host model lock. BUSY
means no model request was admitted; the coordinator can try that unadmitted
operation on its next event/heartbeat. RUNNING, UNKNOWN or ERROR for an admitted
request is fenced and requires host reconciliation; it is never automatically
resubmitted. Changing a head or approved plan revision creates a distinct request.

The runtime waits long enough for the existing three-hour Fable model limit plus
archive preparation/transport. It does not cancel an audit after the old 15-minute
workflow timeout. Fable's extra-usage checks, fixed model, restricted tools,
read-only account, secret filtering and no fallback remain in force. A missing
`overageStatus` stays a failure. This change does not switch an account's billing
settings or interpret the missing field as rejected.

## Authority and binding

`control_plane_program.py` calls only:

```text
sudo -n /opt/aiops/bin/aiops-fable program
```

Its bounded JSON stdin carries the operation, target and GitHub token. The token
does not appear in argv, saved receipt, model input or command output. There is no
shell evaluation, environment preservation, arbitrary path, model choice or
caller-supplied verdict. The sudoers rule admits only this exact command, for the
runner identity; builder identities receive no new root command.

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
For delegated nodes, `start` checks protected consultations and audits of the current writer
attempt. USER_REQUIRED or an unresolved admitted request prevents resume
even if somebody removes GitHub labels or rewrites a comment. A new approved plan
revision is the durable way to settle the decision; deleting a receipt or editing
the projection is not. This guard does not grant new authority to Opus.

## Rollout on the host

1. Audit this implementation's exact HEAD with ordinary ARCHITECTURE/A3, using
   the existing fixed tool. Only a normal User merge can adopt this bootstrap.
2. Keep active/UNKNOWN task and model requests fenced during rollout. Install
   the audited `control_plane_fable.py` as `/opt/aiops/bin/aiops-fable` and install
   `control_plane_program_astra.py`, `control_plane_program.py`, `control_plane.py`
   from the same accepted commit into `/opt/aiops/lib/program/`, root-owned and
   not group/other writable. Protect every directory in the path chain.
3. Copy the same commit's central config and project profiles to
   `/opt/aiops/lib/.github/control-plane/`, with a protected root-owned directory
   chain. The support's `control_plane.ROOT` resolves to `/opt/aiops/lib`.
   Root bridge host calls use the existing installed helper and ledger. No builder
   adapter, actor identity, credential or existing host admission gate is replaced.
4. Apply only the exact runner sudoers rule in the example and validate it with
   `visudo -cf`. Keep installed hashes/commit and actual qualification evidence.
5. Run existing Fable preflight. Require its actual extra-usage rejected evidence.
   If it returns OVERAGE_NOT_BLOCKED, retain the failure and fix/qualify the account
   through the already authorized host/operator process; do not weaken the check.
6. Qualify a canonical no-cost fixture task: valid scope audit, duplicate request,
   stale head/revision, missing reviews, User-only node, USER_REQUIRED resume fence,
   failing CI, root state/permissions, token argv/log absence and actual merge race.
   Local fake tests in this PR are not host qualification evidence.
7. Refresh the runtime attestation/activation pointer through the existing audited
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
