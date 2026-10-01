# Protected Fable failure recovery — implementation candidate

Status: source candidate on central #46. User adoption, independent exact-HEAD
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

Missing or malformed `overageStatus` is `OVERAGE_UNVERIFIED`; affirmative evidence
that extra usage is not blocked remains `OVERAGE_NOT_BLOCKED`. Both stop immediately
and preserve fail-closed billing protection. A supported-profile protective stop
records the exact triggering stream event/index/hash and sealed guard-stop archive,
waits for the killed process group to be absent, and proves publication NOT_STARTED.
Only the operator reconciliation path may then settle TERMINAL_FAILED. Missing
guard/archive/process/publication proof remains UNKNOWN. Neither settlement nor
the guard event creates reset evidence or quota eligibility: the automatic wake
still accepts only verified MODEL_RATE_LIMIT failures.

All fixed CLI model-producing commands take a root-owned nonblocking
`account-model.lock` before model context or program admission. The existing
program journal/quota locks remain inner locks. A BUSY caller admitted nothing;
check and reconciliation do not require the model lease. Standalone Claude/Opus
sessions outside aiops-fable are not serialized by this lease.

## Start and completion safeguards

A proposed approved descendant plan cannot bypass an unresolved execution journal
entry, including User-only/nondelegated nodes or a removed Astra delegation flag. Exact semantic USER_REQUIRED bindings
may be superseded only by a revised node definition with a distinct approved
decision pointer; new commit SHA alone is insufficient. Same-scope semantic
decision changes require a separately adopted decision-resolution path.
Execution ambiguity cannot be superseded by scope revision.
Active schema v1 rejects `depends_on_external`, including an empty field, before
materialization or start. The separate external completion capability is not
installed by this change.

Completion/dependency readiness verifies the recorded actual merge SHA on the
default branch. KIX requires terminal successful push-workflow checks bound to
that SHA and a protected profile pinning both locked Git blobs to the existing
repository contract. Missing required proof, pending CI, failure, or a bare skipped
required check holds downstream readiness. Projects without a post-merge phase
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

The external completion/evidence/query and DAG-proposal work in CP-E04–08 remains
separate. A failure recovery candidate alone does not make all 98 product nodes
implemented, externally qualified, accepted or released.
