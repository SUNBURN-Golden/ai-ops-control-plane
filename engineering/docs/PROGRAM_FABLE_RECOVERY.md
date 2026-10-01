# Protected Fable failure recovery — implementation candidate

Status: source candidate on central #47 above #46. User adoption, independent exact-HEAD
A3, installed fingerprint and actual no-cost host qualification remain PENDING.
These source changes do not install a timer, grant sudo rights or activate a host.

## Immutable execution evidence

The CLI keeps the public error JSON shape `status`/`reason`. Its protected
`FABLE_FAILURE_V1` envelope preserves actual boolean failure, HTTP status and
raw archive/result hashes instead of describing `subtype=success` as success.
Only the exact supported 2.1.285 profile may supply a candidate typed reset.
The 17:32 historical report does not include a captured machine reset field:
its prose is never interpreted as a timestamp or permission to retry.

The runner records execution identity, waits for its process and checks the
process group. Missing terminal/archive proof remains UNKNOWN. A publication
intent is sealed before the GitHub POST; a lost response is an ambiguous publish,
not proof that no comment exists. A terminal-failure verifier rejects successful
or duplicate terminal output, changed digests, unsafe paths, different bindings,
unsupported profiles and any publication intent/response/comment/result marker.
Failure archives from a different installed wrapper hash remain unqualified.

## Admission, outcome and terminal settlement

`program/journal/` contains immutable admission and outcome events, with linked
digests. A replaceable `.json` receipt is a compatibility projection. Deleting or
editing that projection cannot remove an admitted request or change its verdict.
Validated existing flat receipts are imported before new admission. A receipt
lost before migration cannot be reconstructed; rollout must retain and reconcile
the original host archive, and must not claim that absence proves non-admission.

Execution fences use repository/program/node/operation across HEAD, plan, task,
question and installed fingerprint changes. Even returning to an older exact
PASS cannot bypass a later unresolved execution. Independent current-head review,
scope approval and the normal merge gates remain mandatory.

The separate `program-reconcile` operator entry accepts only the repository,
opaque admission ID, expected journal state hash and stdin GitHub credential.
For a derivative retry, an optional opaque `quota_attempt` incident ID selects
the protected child journal; no caller-controlled path or run ID is accepted.
It derives run/binding/archive paths from protected evidence. It is absent from
runner sudoers and cannot be invoked through ordinary `program` operations.
An immutable `TERMINAL_FAILED` settlement requires verified terminated execution,
same-bound failure archive and definitely unstarted publication. It preserves
the original ERROR. It grants neither PASS nor another model invocation.
UNKNOWN without that evidence stays fenced, including legacy unproven failures.

## Protective stop and fixed-command account lease

User approved fix-request item 9 on 2026-10-01. Durable decision pointer:
[대표님 원문 — 수정 요청 9번 승인](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/47#issuecomment-5927605393). An otherwise valid
`rate_limit_event.rate_limit_info` may omit `overageStatus` only when `status` is
exactly `allowed` or `allowed_warning` and `isUsingOverage` is the boolean `false`.
The realtime guard, terminal output validation and ordinary preflight share that
rule. The accepted evidence preserves the absent key; it never manufactures
`overageStatus=rejected` or claims that billing is disabled. Explicit null,
empty/malformed values, missing/nonboolean `isUsingOverage`, other statuses and
contradictory signals do not qualify for this exception. Actual extra usage,
including `isUsingOverage=true`, still stops immediately. Outside the exception,
missing/malformed billing proof remains `OVERAGE_UNVERIFIED`, and affirmative
unblocked-usage evidence remains `OVERAGE_NOT_BLOCKED`.

A supported-profile protective stop
records the exact triggering stream event/index/hash and sealed guard-stop archive,
waits for the killed process group to be absent, and proves publication NOT_STARTED.
Only the operator reconciliation path may then settle TERMINAL_FAILED. Missing
guard/archive/process/publication proof remains UNKNOWN. Neither settlement nor
the guard event creates reset evidence or quota eligibility: the automatic wake
still accepts only verified MODEL_RATE_LIMIT failures.

Wrapper timeout, invalid/duplicate-field stream and output-limit kills seal typed
`WRAPPER_TIMEOUT`, `STREAM_INVALID` and `OUTPUT_LIMIT` evidence through the same
protected archive mechanism with the CLI-version-independent
`fable-wrapper-stop-v1` profile. Settlement requires the wrapper's recorded SIGKILL,
exit `-SIGKILL`, absent process group, uninterrupted evidence collection, same
binding/archive hashes and definitely unstarted publication. Timeout additionally
requires completed timer collection and `timed_out=true`; invalid-stream and
output-limit evidence require `timed_out=false`. This does not broaden the existing
extra-usage guard-stop verifier's pinned 2.1.285 profile: an unsupported CLI's
actual overage still stops immediately but remains fenced without qualifying proof.
Only operator
reconciliation may settle these terminal failures; they never qualify for quota
retry. Missing proof or an uncertain process/publication remains UNKNOWN.

All fixed CLI model-producing commands take a root-owned nonblocking
`account-model.lock` before model context or program admission. The existing
program journal/quota locks remain inner locks. A BUSY caller admitted nothing;
check and reconciliation do not require the model lease. Standalone Claude/Opus
sessions outside aiops-fable are not serialized by this lease.

## Start and completion safeguards

A proposed approved descendant plan cannot bypass an unresolved execution journal
entry, including User-only/nondelegated nodes or a removed Astra delegation flag.
Current-default plan admission checks the protected canonical materialization
identities, including records whose issue projection was deleted. Removing or
renaming such a node is refused; no fence migration is adopted.

Exact semantic USER_REQUIRED bindings may be superseded only by a separately
merged plan-decision PR with a matching `superseding_decisions` record. The record
binds schema version 1, repository/program/node, canonical issue, old writer
launch and plan commit, previous and new canonical node-definition SHA256, and
the separate decision pointer. The merged plan must contain that exact record
and new definition. Task issues/comments, delivery PRs/task branches, a bare URL
change or trailing title whitespace do not qualify. Recursive string-edge
normalization prevents whitespace-only changes from becoming a new semantic
definition. The separate User-merge path is enforced as policy; M4's shared
GitHub token does not prove cryptographically which human authored or merged
the decision. Same-definition semantic changes need a separately adopted
decision-resolution path.
Execution ambiguity cannot be superseded by scope revision.
Active schema v1 rejects `depends_on_external`, including an empty field, before
materialization or start. The separate external completion capability is not
installed by this change.

The #46 checkpoints `a8b7355712c58de8d27c85a535fb241a09a4037c` and the historical
#47 checkpoint `94a768e19df12703ea0b9a49e49972feb2f6ef4f` do not satisfy KIX
R-1 §6-2's external-dependency rejection requirement and are not adoption targets.
The minimum source checkpoint is #47 `e34868c6a12e5488224095b3248e59c8be9128f1`,
or a verified descendant retaining that rejection and the User-only merge guard.
Source ancestry is not adoption, audit or host qualification.

Completion/dependency readiness verifies the recorded actual merge SHA on the
default branch. KIX requires terminal successful push-workflow checks bound to
that SHA using the distinct protected `program_post_merge_required_checks`
(`protocol`), and a protected profile pinning both locked Git blobs to the existing
repository contract. Missing required proof, pending CI, failure, or a bare skipped
required check holds downstream readiness. The `kernel` check remains mandatory
at the delivery's exact head before merge; its workflow has no push trigger.
Projects without a post-merge phase
retain DISPATCH §20's recorded-merge completion rule. That section still permits the original task to be
closed after durable POST_MERGE_FAILED and a corrective-task pointer; that closure
alone never proves the failed delivery ready for dependants. Cached same-tree
main-workflow shortcuts need their named authoritative verification evidence;
the current reader holds when it cannot establish that evidence.

## Expanded product registration boundary

Expanded product candidates bind the complete plan and pending catalogues in a
review manifest and retain PENDING approval. Their explicit `registration_scope`
marker is rejected by the active loader regardless of its value; changing its
text to APPROVED or substituting an inherited approval pointer grants nothing.
Materialize/start/proposed decision-status read the latest default-branch commit
that changed `.aiops/program.json` and require that commit as their requested
plan. A held latest plan cannot be evaded by supplying an older unmarked ancestor
from the registration stack after merge. Existing materializations additionally
require descent from the recorded plan. Read-only archived/current semantic
queries without a proposed plan retain their original pinned context.
The complete-scope approval reader and actual protected authority remain separate
adoption/qualification work. Removing that marker is a consequential approved
registration-policy change; this source guard does not claim to prevent privileged
tampering through the existing shared GitHub credential.

## One quota wake through the existing heartbeat

The candidate `quota-readiness` query runs no model. `quota-resume` accepts the
original repository/task/delivery-or-question selector; the host derives its
incident, verified failure, UTC reset and original immutable binding. Only an
exact supported `MODEL_RATE_LIMIT` archive is eligible. Generic ERROR, prose-only
reset, unsupported CLI, success/publish ambiguity and changed context cannot
create an automatic attempt.

At the due event or existing hourly heartbeat, a protected scope/shared-model
lock covers the durable one-shot claim, fresh preflight, full context recheck,
linked child admission and actual audit/consult. Fresh preflight must prove PASS
and blocked extra usage. The current immutable plan, host writer, delivery/HEAD,
question pin/body hash, installed/service fingerprint and required CI/reviews
are rechecked. User-only, RELEASE and consequential contract-change decisions
cannot become automatic quota readmission. The same CI predicate serves merge
and audit retry.

The queue orders eligible immutable incidents by UTC due time and parent ID.
Stale/blocked incidents do not starve an unrelated eligible task. Fresh ordinary
admission checks priority again while holding the shared model lock. The first
claim survives reboot/duplicate events; a failed/crashed preflight or second
quota failure cannot create another automatic wake. A child result is an
explicit linked effective receipt and never overwrites the parent's ERROR.
Its unresolved execution fences both audit and consultation, including old PASS.

A childless `BLOCKED_ERROR`, `BLOCKED_POLICY` or `STALE_CONTEXT` may be recorded as
`CONSUMED_NO_CHILD` only when its protected terminal binds the digest of a
completed PASS preflight or a sealed VERIFIED/terminated/NOT_STARTED preflight
failure. This keeps the quota claim permanently consumed, so that binding gets
no further automatic attempt. A new binding may use ordinary admission after
the fixed service confirms there is no unresolved execution; the original ERROR
and claim remain. A legacy terminal without this proof, a claim/preflight crash,
or ambiguous execution/publication remains UNKNOWN and fenced. Ordinary
subscription-warning acceptance does not satisfy quota's explicit rejected
billing evidence and is never converted into such evidence.

No timer is installed. Candidate dispatch operations and playbook rows require
actual final policy adoption/qualification before the coordinator may use them.
The root transport/workflow allows both the existing maximum preflight and audit
runs to finish; cancellation is not permission to recreate their admission.

## Qualification and remaining scope

Candidate tests use fake providers and protected local fixtures. They cannot
establish actual installed CLI wire compatibility, production kill/journal
behavior, account extra-usage status or provider availability. Root permission,
archive/fsync, duplicate admission, migration, cached PASS, process cancellation
and lost-publication cases must be qualified at the final accepted installed SHA.

## Durable host state and migration

Program mode requires storage that retains protected state across reboot and
host restart. This includes the whole `/var/lib/aiops-fable/` tree (run archives,
flat receipt projections, immutable admission/outcome/settlement journals, quota
incidents, one-shot claims and linked child records), and the host control ledger
under `/var/lib/astra/control/`, including its database and required transactional
side files. Persisted service authorization and its binding to the accepted
installation must also be retained. A reboot-resetting Grok VM is not a suitable
program-mode host. Moving code or retaining GitHub projections alone does not
retain admissions or unresolved execution fences.

Before activation, the operator must record the actual persistent backing/mount,
protected ownership and crash-consistent write behavior, and verify a stopped
canary's state/digests survive reboot. This source change performs no host check
or activation, and adds no automatic preflight durability verdict or marker check.
A directory marker may identify the expected state installation,
but marker existence alone cannot establish durable backing or justify dispatch.

Migration is an operator procedure under stopped admissions and reconciled or
retained active/UNKNOWN execution evidence. Transfer the complete original state,
archives, receipts, journal chains, quota claims and host ledger with their
bindings and ownership; verify hashes/chain consistency and the actual external
session state before resuming. Preserve the original state for reconciliation.
Do not initialize an empty ledger, discard a consumed claim, delete ERROR/UNKNOWN
or infer non-admission from a missing projection. If records are lost or cannot
be reconciled, remain NOT_READY and keep affected operations fenced. Only one
host may resume admissions against the migrated state.

The external completion/evidence/query and DAG-proposal work in CP-E04–08 remains
separate. A failure recovery candidate alone does not make all 98 product nodes
implemented, externally qualified, accepted or released.


## Never-attempted operator reconciliation (Fable #47 F4)

An ordinary program admission seals `request-intent.json` before GitHub/archive
work and seals `model-invoke-intent.json` **before** calling the runner. An error
with no attempt marker can produce a protected `PRE_MODEL_FAILED` envelope with
exact bool `model_attempted=false`. The verifier rechecks the intent, exact
binding/tool/run/digests, all protected files and absence of attempt/publication
markers. HEAD_MOVED, GitHub transport and source archive failures may qualify;
missing/unreadable evidence, an attempted runner or ambiguous publication never
qualifies merely because an exception was raised. Legacy unproved ERROR/UNKNOWN
remains fenced. An archive-sealing failure creates no fabricated proof.

Only the separate root-only `aiops-fable program-reconcile` entry may consume
this proof. Use the admission ID and `state_version` reported by the receipt;
the journal appends its settlement without rewriting admission/outcome, granting
PASS or retrying the same binding. The coordinator has no reconciliation
operation. Repeated root calls with the same version are idempotent; stale
versions, another repository/binding, missing service authorization and held
scope/model locks refuse settlement.

For a crash just after quota `claim.json`, the protected ticket already contains
`preflight-intent.json` with `model_attempted=false`. `preflight-start.json` is
sealed before the fresh preflight callback. A claimed ticket without that start
marker, a child, a preflight result or a terminal event reports UNKNOWN with a
`state_version` bound to ticket/claim/intent. The operator submits that version,
`quota_attempt=<incident id>` and `admission=<parent admission id>` to the same
root-only entry (all selectors are opaque IDs, never paths). It verifies the
sealed never-started proof under both locks and appends
`operator-settlement.json` with `CONSUMED_NO_CHILD`. The claim remains consumed:
no second automatic wake, preflight or model run is granted. A child admission
continues to use the existing run-evidence verifier; a claimed legacy ticket
without the new proof, or any preflight-start/child ambiguity, remains fenced.

Private root-owned bounded regular `.tmp-` files inside a quota incident are
unpublished scratch, never journal events. They are retained and ignored. If an
interrupted atomic link publication leaves both the named event and its `.tmp-`
alias, only same-directory protected scratch aliases explaining every descriptor
hard link are accepted; an external hard link, symlink, foreign owner or writable
scratch is refused. This creates no ticket/admission/claim and releases no
UNKNOWN fence. A directory containing only unpublished scratch has admitted
nothing and does not block unrelated program scopes.
