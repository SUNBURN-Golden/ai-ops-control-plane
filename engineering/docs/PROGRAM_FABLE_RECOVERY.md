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
