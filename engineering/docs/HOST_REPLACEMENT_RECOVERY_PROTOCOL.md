# Host replacement recovery — normative protocol proposal

Status: **DRAFT / NOT_ADOPTED / NOT_READY**. This document specifies proposed
behavior; it is neither an installed mechanism nor independent audit evidence.
It supplements [the design](HOST_REPLACEMENT_RECOVERY_DESIGN.md).
Repository paths below are relative to `engineering/`.

## 1. Profiles, authority and terminology

Two explicit profiles prevent unavailable infrastructure from becoming an
implicit success. An operator selects no profile at runtime: the accepted
manifest and protected deployment grant bind it.

| Profile | Allowed recovery behavior |
| --- | --- |
| OPERATOR_HOLD | Verify/restore accepted installation through an existing privileged operator; after replacement, rollback or unknown continuity, retain execution and release holds. No automatic cross-instance admission. This is the default when external qualification is absent. |
| QUALIFIED_RESUME | Additionally resume only after the external state authority, non-bypassable sender fence, complete state recovery, authentication and every existing operation gate have passed. No profile downgrade or offline admission fallback. |

Both profiles require protected durable state. OPERATOR_HOLD is not permission
to activate a reboot-resetting program host or use a shared writable directory.
The upstream prohibition remains authoritative. Losing protected state means
NOT_READY even if binaries can be restored.

"Proposed" means authored for review. "Audited" means a non-author verdict at the
exact reviewed source/task revision. "Accepted" means the corresponding explicit
User design/source decision, with a durable pointer. "Qualified" means actual
host/provider tests at the bound installation and boundary. "Installed" means
verified accepted bytes on that host. "Activated" is the separate existing
activation grant. "Adopted" requires all applicable gates; none substitutes for
another. A User request to fix this draft authorizes draft edits, not these grants.

## 2. Non-circular bootstrap and manifest lifetime

The cold-start trust root is a verifier plus trusted acceptance authority
provisioned by the authorized operator through a channel independent of restored
guest files. Its identity/key and verifier digest cannot be learned from the
manifest being verified. If the verifier or privilege route was removed,
automatic restoration ends; use the separately authorized initial-install path.
This proposal grants no new sudo rule or platform hook.

The versioned acceptance record binds the installation/state IDs, profile,
manifest digest, full source SHA, support artifact digests, qualified CLI/model/
effort and billing route, allowed commands/principals/destinations, policy and
activation digests, audit and User decision pointers, external authority identity,
generation, validity interval and revocation revision. The validator rejects
unknown/duplicate fields, unbounded input, mismatched identities and missing
required bindings. Canonical serialization and signature format must be selected
in HR-01; implementations cannot invent incompatible encodings.

Before install and again before operational handoff, obtain authenticated current
acceptance/revocation state. Local wall clock, an old signed document, or a GitHub
pointer alone cannot prove it is still current. Without this online authority,
QUALIFIED_RESUME holds. A manifest for another installation/epoch, revoked source,
old acceptance generation, changed artifact or expired qualification is rejected.
An explicit rollback is a new acceptance revision; it preserves the newest
execution state and does not roll that state back with the code.

Workspace/cache contents are untrusted transport. The fixed privileged verifier
opens inputs without following links, validates every ancestor, type, ownership,
mode and descriptor identity, hashes the same descriptors used for copying, and
copies only fixed destinations into a protected same-filesystem staging tree.
Reject outside hard links, traversal, devices and changed inode/bytes during copy.
Fsync staged files and directories before atomic publication; recheck the final
installed descriptors/digests. Existing mismatching components are drift, not
"missing". No overwrite or identity remapping happens as a repair convenience.

## 3. Complete state and authenticated recovery authority

For QUALIFIED_RESUME, the external authority must be outside the guest snapshot
and outside builder/runner write authority. Required backend capabilities are:
authenticated linearizable read/CAS, immutable durable payload storage, atomic
publication of a head referencing already durable payload, independent access
control, and preserved latest-head/revocation state across guest reset.
Eventually consistent reads or a lone monotonic counter are insufficient.

A versioned head binds installation/state IDs, accepted manifest/policy digests,
active epoch and fencing revision, monotonically increasing state version,
previous head digest, complete checkpoint inventory/digest, operation intents,
consumed permits/claims, unresolved holds and outcome/publication evidence.
Payload objects require authenticated integrity and confidentiality with keys and
access controls anchored outside the guest snapshot; no transcript/state payload
is stored in GitHub comments or the source repository. Payload includes original ledger, immutable journals/archives/receipts and every
required side file from design section 4. A count/hash with no recoverable payload
cannot restore a lost approval, consumed claim or request. Secret values remain
in the independently protected approved secret store; payload contains references
and access bindings, not a new plaintext secret backup.

Generate each checkpoint while admissions/state writers are serialized. Use a
consistent SQLite backup or fully stopped database with its required transactional
files; never copy an actively changing SQLite file and WAL independently.
Validate file inventory, hashes, lengths, formats, journal chains, request/owner
bindings, consumed claims, UID/GID/ACL mapping, retained worktree/session inventory,
secret-reference accessibility and newest-head completeness. Missing keys or
payload prevent restoration; corruption must not fall back to an older generation.

The authority's published head plus recovery journal is the cross-instance
authority. Local runtime files are its bound installed checkpoint; local SQLite
alone never acts as a distributed lock. An external consumed permit that has no
local outcome is recovered into a separate protected UNKNOWN hold, without
rewriting or fabricating an original signed admission/outcome. Implementation
must supply a reviewed compatibility adapter so every existing gate sees these
holds. Without that adapter, QUALIFIED_RESUME is unavailable.

## 4. Write ordering, ambiguity and one operation

This protocol applies to every external mutation: model/provider invocation,
builder launch, GitHub issue/comment/merge and terminal/release/claim changes.
An admitted operation uses its original request/event/task/owner identity; each
effect has a deterministic ID bound to that identity and effect kind. Reusing
the ID with different inputs fails. A new epoch never supplies a new retry ID.

1. Under the established lock order, validate actor, acceptance, current epoch,
   authorization, dedupe, prior holds and existing billing/operation gates.
   Persist the local reserved intent and full required state; fsync files and
   parent directories as required by the qualified storage mode.
2. Upload immutable complete payload and verify durable acknowledgement. CAS the
   external head with the expected previous head/epoch/version to publish PREPARED.
   No effect has occurred. Persist the acknowledged binding locally.
3. At the actual side-effect boundary, the qualified sender enforcement point
   checks current epoch/acceptance and atomically consumes the one-shot effect
   permit against that head. Consumption is durably recorded before sending.
   Local READY, an environment variable or an earlier check cannot replace this.
4. The enforcement point forwards the effect once, or the destination provides
   an equivalent atomic epoch/permit check. The guest does not receive a reusable
   bearer permit for a later unchecked direct send. A sender crash after consumption
   cannot retry that send; repeating an effect ID returns its stored state only.
   Persist original result/terminal/publication evidence locally,
   then publish a new complete payload/head with that evidence. A lost response
   remains UNKNOWN until authoritative evidence reconciles that exact effect.
   Reconciliation can append evidence; it cannot unconsume a permit or retry it.

| Interrupted boundary | Recovery requirement |
| --- | --- |
| Before PREPARED is durably published | No effect permit; preserve local intent. Prove no permit/effect at the authority before any existing authorized reconciliation. |
| PREPARED acknowledgement is lost | Read exact head/effect ID from authority; unavailability means HOLD. Do not publish a new request. |
| After PREPARED, before permit consumption | Retain reservation; revocation may cancel an unused permit by CAS with explicit evidence. Cancellation grants no retry. |
| Consumption response lost or host dies before send | Treat as possibly sent; consumed permit remains UNKNOWN. No resubmit even if this sacrifices availability. |
| Send succeeded, outcome or publication acknowledgement lost | Query protected/provider evidence for original ID; preserve hold if unavailable. Never rerun a model or blindly repost a comment. |
| Outcome recorded locally, external head update ambiguous | Retain newer local evidence, reconcile exact head, publish only a verified append. Never replace local state with a convenient older snapshot. |

A payload/version disagreement blocks admission. Local-newer evidence is retained
for reconciliation, not automatically trusted; external-newer state must be
restored in full and validated before handoff. Concurrent heads, failed CAS,
unknown acknowledgement and missing payload never count as success.
Exact-once external execution is not claimed for providers without that contract;
the guarantee sought here is no blind repeat after ambiguous send.

## 5. Actual old-host fencing and epoch handoff

An epoch token helps only if checked where an effect can actually leave.
For QUALIFIED_RESUME, all relevant sends must pass an independently protected
enforcement point that checks and consumes the current permit and that guest
processes cannot bypass. No retained direct provider/GitHub credential or
unrestricted egress may allow an old process to send independently. Qualification
must test the old guest alive, network partition, stale cached permit and late
send. A host-local lock or "check CAS then call CLI" is not this fence.

The enforcement point blocks new sends for a revoked epoch and durably tracks
already consumed/in-flight permits. Epoch change does not cancel a request already
accepted by a remote provider. All its unresolved session/owner/reservation
records survive into the new epoch. Terminal evidence/cancellation plus sender
fencing are required before replacement-aware release; timeout, PID absence,
hostname change and GitHub closure remain insufficient.

Handoff procedure: stop/fence old admissions at the enforcement point; preserve
all consumed/in-flight effects; validate the complete latest state; CAS active
epoch to the new authorized host; restore and verify that checkpoint; qualify
bindings; then enable only operations whose normal gates are satisfied.
Competing hosts cannot both win the epoch CAS. A new host cannot bypass an
unresolved old operation by choosing another ID/revision or using reconciliation.

This draft does not assert that current subscription CLIs support such an
enforcement point or that a proxy preserves their qualified billing route.
If any lane cannot be fenced without changing that route/security boundary,
that lane stays OPERATOR_HOLD after replacement. An alternative is separately
authorized physical old-host termination plus verified revocation of every old
sender credential and reconciliation of all remote sessions; platform prose
alone is not that proof. Revocation/reissuance is not performed by recovery.

## 6. READY lifetime, locks and entrypoint coverage

Recovery takes an exclusive installation barrier; normal operations use its
shared side and validate a protected generation binding. Recovery's own bounded diagnosis/install receipt may be written while operations
are held; it cannot grant or settle a runtime admission. No original runtime file
is mutated merely by diagnostics. Fixed lock order is:
installation barrier → account/model lock where applicable → scope/lane/request
lock → short ledger transaction → external CAS. No helper calls recovery while
holding a downstream lock. Never hold a SQLite transaction through a network call;
persist its intent, end it, and recheck expected versions after the reply.
The implementation must map this order to actual current locks and test contention.

READY binds boot/installation/state identity, epoch, installed/artifact/manifest/
policy/activation digests, authority head version, original operation identity
and qualification revision. It is a diagnostic result, not a transferable permit.
The actual helper revalidates those bindings under the barrier before mutation,
and the external sender revalidates/consumes at send time. Reboot, drift,
revocation, epoch/version change, lost mount or unknown authority invalidates it.
Commands holding only an old receipt or starting between check and use fail.

The following map is derived from inspected main and candidate #47 command
parsers. It is a minimum inventory, not a claim that future code is covered.

| Surface | Required check before first mutation/effect |
| --- | --- |
| Host helper launch; builder adapter preflight | Fixed host admission boundary and adapter launch boundary; "preflight" is not presumed free of effects. |
| Host init/migrate/materialize-begin/finish/resolve/plan | Recovery/current-state guard; lost-state init prohibited. Migrations require separate accepted operator procedure. |
| Host reap/reconcile | Block old/unverified epoch legacy release; only separately accepted replacement-aware reconciliation may append settlement. |
| Program materialize/start/review/finalize-review | Guard issue creation, record writes, packets, dispatch and result publication, including library calls below CLI. |
| Program merge-check/merge | Computed checks require current bound state; merge rechecks exact PR HEAD and current generation immediately before its existing authorized PUT. |
| Fable audit/consult/preflight | Guard before run/archive creation and provider execution; preflight invokes the model and needs separate qualification authority. |
| Candidate #47 astra-audit/astra-consult; Fable program/program-reconcile | Guard bridge, immutable admission/settlement and root operator entry; existing PA-1 grant does not imply recovery release authority. |
| Candidate quota-readiness/quota-resume, quota child settlement | Readiness is diagnostic only; guard claim/preflight/child admission/settlement. Preserve original parent/child bindings and consumed claims. |
| Status/lanes/task-status/materialize-status and diagnostics | Bounded read only; mark stale/unverified epoch. Any projection write/publication becomes guarded mutation. |
| Runner workflow, adapter, direct helper/imported internal call | Same installed guard; no checkout script, environment option or bypass switch is an authorization route. Unlisted mutation defaults to denied. |

HR-04 must enumerate actual executable and library call sites for the accepted
implementation SHA. Parser additions must fail coverage review until classified
and exercised directly without the convenience wrapper. Root can alter installed
code under the existing operator trust boundary; this is not resistance to a
malicious authorized root operator.

## 7. Authentication and recovery outcomes

At inspected main, Fable `preflight` calls `ctx.run_model` to prove model/login/
confinement. A token file and `--version` establish neither login nor billing.
Recovery must not call that preflight automatically or declare authentication
passed from token presence. Non-model checks verify only their demonstrated
scope. When no qualified non-model method can establish required authentication,
return AUTH_QUALIFICATION_REQUIRED. Separately authorized qualification first
requires current state/fencing and all existing usage guards; it does not resume
the original operation. Its bound evidence can then be used for a fresh check.

Public diagnostics retain existing `status`/`reason` semantics. The following
proposed codes are bounded recovery reasons, not new semantic audit verdicts.
READY/BUSY/HOLD/ERROR never mean PASS, a task is DONE, or a merge is authorized.
Return operation IDs and evidence pointers; redact secrets, nonces and raw packets.

| Reason | Operational behavior | Test |
| --- | --- | --- |
| RECOVERY_READY | Existing operation gates still required; no transferable permit | T02,T15,T22 |
| RECOVERY_BUSY | Admit nothing; caller may later inspect original request, no automatic loop | T03,T23 |
| BOOTSTRAP_TRUST_UNVERIFIED / MANIFEST_REJECTED | No privileged install or effect | T11,T16,T24 |
| PRIVILEGED_EXECUTOR_UNAVAILABLE | Authorized operator initial-install handoff only | T21 |
| INSTALLATION_DRIFT / RECOVERY_INCOMPLETE | No operational handoff; preserve incomplete receipt | T04,T11,T13 |
| PERSISTENCE_UNQUALIFIED / STATE_INCOMPLETE / STATE_CORRUPT | No empty DB, destructive repair or older-backup fallback | T05,T12,T18,T27 |
| STATE_FRESHNESS_UNVERIFIED / STATE_ROLLBACK_DETECTED | HOLD; reconcile full authority payload | T06,T07,T25 |
| HOST_EPOCH_UNVERIFIED / SENDER_FENCE_UNQUALIFIED | No automatic admission or legacy release | T08,T19,T20,T26 |
| AUTH_QUALIFICATION_REQUIRED / AUTHORIZATION_UNVERIFIED | No model probe or credential/settings fallback | T14,T17,T28 |
| PRIOR_EXECUTION_UNRESOLVED / EFFECT_UNKNOWN | Keep owner, reservation, consumed permit and retry state | T09,T10,T25,T26 |
| RECOVERY_BINDING_STALE / ENTRYPOINT_UNCOVERED | Revalidate or hold; no effect via bypass | T15,T22,T29 |

Unknown/internal failures become bounded ERROR with no admission. No error code
authorizes retry, release, profile downgrade, init, new credentials or activation.

## 8. Additional acceptance cases

These are future implementation/qualification requirements, not executed tests.

| ID | Scenario | Required result |
| --- | --- | --- |
| T22 | Revoke acceptance, change epoch/mount/state after READY but before use | Helper and sender reject stale binding; no mutation/send. |
| T23 | Recovery and account/scope/ledger locks contend in opposing callers | Single fixed lock order, bounded BUSY, no deadlock or network-held DB transaction. |
| T24 | Manifest self-signs new trust root; old valid manifest replayed; source revoked | Reject each; trusted anchor and current revocation cannot come from restored input. |
| T25 | Crash/lost acknowledgement at every section 4 transition | Preserve original identity and complete payload; consumed/ambiguous permit never sent twice. |
| T26 | Old host partitioned, direct credentials retained, stale permit, remote task still active | Automatic profile refused if bypass exists; in-flight remote owner survives epoch change. |
| T27 | Missing checkpoint blob/key/side file, valid older backup, UID/ACL mismatch | HOLD; no older fallback, invented admission, blanket chown or state reset. |
| T28 | Fable has token/version but no qualified non-model login proof | AUTH_QUALIFICATION_REQUIRED; zero automatic model calls; separate qualification cannot act as original task retry. |
| T29 | Call every inventory entry directly, via runner and via internal library; add unclassified mutation | Guards precede mutation/send; coverage failure blocks acceptance. |
| T30 | Each declared reason and unknown exception | Same bounded status/reason contract, no secrets/effects, no audit PASS/merge/enable implied. |

Real provider qualification, storage crash durability and root/UID isolation
require actual stopped-host evidence. Synthetic fixtures cannot satisfy them.
A normal platform replacement is tested only on an explicitly authorized stopped
canary with no model/provider execution triggered by recovery. If it loses the
checkpoint or lacks fencing/authentication, its expected result is NOT_READY.
