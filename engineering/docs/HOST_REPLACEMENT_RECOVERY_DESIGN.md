# Host replacement recovery — design proposal

Status: **DRAFT / NOT_ADOPTED / NOT_READY**. This is a builder-authored proposal,
not an Astra verdict, a new host authorization, or an installation instruction.

Repository-relative paths in this document are relative to `engineering/`.
Paths beginning with `/` are literal host paths. Links to upstream evidence pin
an inspected commit; they do not authorize adopting that commit.

## 1. User-selected outcome and scope

The User's direction selection is recorded in
[HR-D1](HOST_REPLACEMENT_RECOVERY_DECISION_KO.md#hr-d1--2026-10-01).
Its source is the User's 2026-10-01 13:55:08 KST message in this ChatGPT thread,
quoting the four requirements and answering 「이게 좋을듯」. HR-D1 records scope;
it is not cryptographic actor proof or a protected deployment grant.

The selected outcomes are:

1. Keep installation steps in code and reproduce the approved version and hashes.
2. Before an AIOPS command, check installation, restore missing components,
   verify version/permissions/authentication, then run the authorized operation.
3. Retain execution IDs, completion records, deduplication and usage/billing guards.
4. Never blindly restart an interrupted operation; unknown previous execution blocks it.

That selects the recovery direction. It does not authorize a new source pin,
root/sudo exception, credential provisioning, account billing change, state reset,
new retry, merge, or activation. The resulting implementation changes persistence
and privileged admission and therefore requires an exact-HEAD A3 architecture gate
under `AGENTS.md` sections 2, 8, 11 and 13.

The delivery is an on-command deterministic guard. It adds no daemon, polling,
scheduled reinstall or second coordinator heartbeat. A restored installation alone
must never execute a model, dispatch a builder, merge, or unlock a previous request.

## 2. Observed sources and platform limits

Repository main inspected: `7de954fd17f14312ef878d8e67afdd95c9a5cf39`.
Its `docs/CONTROL_PLANE_RUNTIME.md` already forbids rebuilding a lost ledger empty.
Its installer `scripts/control_plane_install.py` is a **disabled-install** tool;
it is not an active-host recovery installer and must not be silently repurposed.

The pending program recovery candidate inspected is
[PR #47](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/47) at
`754fae0136fdb1237bc863f026d7303de531dae0`, based on the pending #44–#46 stack.
Its `docs/PROGRAM_FABLE_RECOVERY.md`, section "Durable host state and migration",
requires all journals, receipts, quota claims, run archives, host ledger and
service authorization to survive restart. Neither that PR nor this proposal is
an accepted installed release. The latest branch is never an installation pin.

Official platform references, checked 2026-10-01:

- [Use the computer and apps](https://docs.x.ai/grok-bot/computer-and-apps):
  `/workspace` is the shared workspace; files are designed to survive normal
  updates/recovery, while manually installed packages are replaceable. Reset
  returns to a saved snapshot and can lose newer changes.
- [Manage computers](https://docs.x.ai/grok-bot/computers): recreated computers
  retain the durable disk but remove manually installed apps/packages.

These statements do **not** prove that `/workspace` is protected against a builder,
preserves Unix ownership/ACLs, supports safe bind mounts, or durably acknowledges
each SQLite/journal write before an abrupt replacement. A same-overlay `findmnt`
result also cannot by itself establish which paths the platform restores.

Host qualification must prove the actual backing and restore behavior. If that
cannot satisfy section 4, keep the host NOT_READY. The inspected upstream
[durability rule](https://github.com/BeautifulMind-JT/ai-ops-control-plane/blob/754fae0136fdb1237bc863f026d7303de531dae0/engineering/docs/PROGRAM_FABLE_RECOVERY.md#durable-host-state-and-migration)
states: "A reboot-resetting Grok VM is not a suitable program-mode host."
That prohibition remains in force. This draft does not reinterpret it as a
conditional deployment permission. A future exception or a determination that
a particular host is outside that prohibited class requires independent Astra
analysis, actual storage/restore evidence and an explicit User decision.

## 3. Installation manifest and bootstrap trust

Keep the non-secret installer source and reproducible artifact descriptions in
GitHub at one accepted commit. `/workspace/aiops-recovery/` is a proposed retained
cache/entrypoint location, not a trusted execution boundary.

A proposed `AIOPS_HOST_RECOVERY_V1` manifest must bind:

- installation identity; accepted repository and full source commit SHA;
- the actual User acceptance and non-author exact-HEAD audit/qualification pointers;
- every executable/support/config/profile/policy artifact and its digest;
- each principal name, numeric UID/GID, home, shell and required directory mode;
- fixed installation destinations, durable state layout and compatibility version;
- exact qualified provider CLI versions/model/effort/subscription routes;
- protected service authorization, activation and existing approval bindings;
- secret references only, never credential values;
- the independent state freshness/fencing authority selected under section 5.

The manifest is a proposed new format, not adopted authorization. Exact required
fields and their validation are part of the independent A3 review.

After replacement, the trusted manifest must be obtained through an authenticated
approved acceptance path outside replaceable guest files. A local SHA marker,
cache file, arbitrary GitHub comment or installer self-report is insufficient.
If that anchor is unavailable, return `BOOTSTRAP_TRUST_UNVERIFIED` without install.

Never execute a mutable workspace script with sudo. Verify the approved source,
then copy from the same verified open file descriptors into a root-protected
staging directory, reject links/special files and path traversal, and atomically
install. Hash-check-then-reopen is not adequate against a replacement race.
No `curl | sh`, `latest`, package auto-update, new CLI/model fallback, general shell
sudo rule or caller-selected destination is introduced.

### Privileged executor and cold-start limit

Only the **User-designated host operator**, using already authorized host
installation privileges, may restore root-owned files, accounts and mounts.
The runner, coordinator, auditor and builder lanes do not become installers.
The operator invokes an accepted fixed recovery helper from its root-protected
installed location, with fixed arguments and verified input descriptors.

If replacement also removed that helper or its approved privilege route, there
is no automatic root bootstrap under the present authority. The operator must
perform a separately authorized trusted initial installation through the
existing host installation procedure; the workspace cache is not executable
root authority. Platform availability of `sudo` alone supplies no deployment
approval. Missing operator authority returns `PRIVILEGED_EXECUTOR_UNAVAILABLE`
and admits no model or task. HR-D1 does not grant new sudo rules or a platform hook.
An unattended cold-start executor remains an A3/User decision, not an assumed
feature of this on-command design. No standing daemon is added to bridge the gap.

Missing artifacts may be restored. Existing files with different bytes, modes,
principals or approval bindings are drift and block, rather than being silently
overwritten. Interrupted installation leaves a durable incomplete receipt and
keeps all operational entrypoints closed until the same manifest completes.

## 4. Canonical durable state and security boundaries

Prefer **direct writes to one qualified protected durable state store**, exposed
at the existing fixed runtime paths. Do not rely on copying `/var/lib` after a
command: replacement can happen after admission but before that copy.

The proposed durable backing location is chosen and pinned during host
qualification. A subtree under `/workspace` is only a candidate. All components,
ancestors, raw backing aliases, ownership/ACLs, rename and replacement behavior
must be qualified before it can hold canonical state. Shared mutable workspace
data cannot be promoted to authority by `chown`, a marker or a matching digest.

Where qualified bind mounts are available, preserve the existing fixed path
contracts by mounting the protected durable subtrees at those paths. Mount
identity must be checked on every admission; mount failure must not fall through
to an empty underlying directory. A symlink is not a substitute: current
protected-path checks reject it. If the platform cannot support this layout,
return `PERSISTENCE_UNQUALIFIED`; an alternate layout/format needs a separate
reviewed design, not a runtime path-check exception.

| State | Required preservation |
| --- | --- |
| `/var/lib/astra/control/` | Original admission SQLite database; materializations; writer/reviewer ownership, attempts, reservations, terminal evidence; transactional side files required by its mode |
| `/var/lib/aiops-fable/` | Complete run archives, publication evidence, program receipts, immutable admissions/outcomes/settlements, quota tickets, consumed one-shot claims, linked child records |
| Protected policy and service authorization | Accepted source/hash/activation/qualification bindings; repository and lane allowlists; limits; exact acceptance evidence |
| Lane session/worktree inventory | Provider session IDs, work in progress, original owner and task binding, any retained supervision evidence; builder-writable status is never trusted release proof |
| Credential references and signature material | Existing approved secret/signature access through its protected source; never source control, chat, argv or shared plaintext cache |
| Recovery installation records | Approved manifest digest, state identity/generation, old/new host epochs, incomplete/complete recovery receipts and reconciliation pointers |

Preserve control/runner/auditor/builder UID separation and current permissions.
Never recursively reassign ownership to make restored state pass a check. If an
old numeric UID now names another principal, block until an explicitly reviewed
mapping/migration proves all ACLs and policy bindings correct.

SQLite integrity, journal-chain validation and full inventory are required, but
do not prove freshness. Locks must be recreated from trusted state with correct
owners; an old `flock` file does not mean its kernel lease survived replacement.
An unlocked account-model file does not authorize another request while a
previous journal admission is unresolved.

## 5. Snapshot rollback, host overlap and freshness

A complete old snapshot can pass every file hash and journal-chain check while
omitting a newer model admission, launch or consumed quota claim. Therefore an
installation ID, boot ID or directory marker cannot establish safe recovery.

Before automatic resumed admission, require an independent durable witness of
the latest canonical state generation and host fencing epoch. It must live
outside the resettable snapshot, bind the approved state identity/digest, be
updated before the corresponding external side effect, and serialize the host
epoch handoff so only one host can admit. A failed/ambiguous witness update blocks
the side effect. Disagreement or restored older state returns
`STATE_FRESHNESS_UNVERIFIED` / `STATE_ROLLBACK_DETECTED`.

The witness backend and authenticated compare-and-swap/write-ahead protocol are
an **unresolved architecture prerequisite** of this proposal. They must be
selected and independently reviewed before implementation enables automatic
recovery. This draft provisions no external service or secret and assumes no
unavailable personal-plan platform hook. GitHub task projections may help
reconcile, but ordinary issue comments are not atomic locks, and no existing
"host pins only" gate is replaced with text from GitHub.

If the current platform offers no qualified independent witness, preparation and
installation diagnostics remain possible, but cross-instance resumed admission
must stop for operator reconciliation. This is a functional limit, not a claim
that unconditional auto-recovery has been achieved.

Old-host death, fencing of every potential sender and external provider session
status must be accounted for. Hostname changes, local PID absence, a reused PID,
timeout, GitHub issue closure or a new VM are not sufficient terminal evidence.
Retain unresolved prior admissions/owners; do not infer that remote sessions died
when the old guest disappeared.

### Replacement and rollback invalidate legacy release evidence

At inspected main, `scripts/control_plane_host.py` method `Ledger.reap` validates
the CONFIRMED reservation, terminal evidence and applicable signed pin, and then
uses `lane_quiescence` on the current host before writing
`RECONCILED/SESSION_TERMINAL_VERIFIED`. It is not an absence-of-processes-only
check, but its local census does not establish old-host or remote-session
termination after replacement. Even a valid retained delivery pin does not do so.

For an old, changed or unverified host epoch, **block legacy `reap` and every
release/reconciliation path**, including operator `reconcile`, program terminal
settlement and quota-child settlement. Do not carry their old release
preconditions into a new epoch. Existing actor roles are retained, but their
old permission is not sufficient cross-instance release authority. No automatic
release, new admission or retry follows from local quiescence on the new guest.

A future replacement-aware reconciliation must bind the original request,
session/owner, old and new epochs, current complete state, independently verified
provider terminal/cancellation evidence, and fencing of every old sender. Its
mechanism and operator grant need their own accepted A3/User decision. Until
they exist, cross-instance holds remain even if a legacy operator command would
otherwise succeed. Absence of a qualified release path means NOT_READY; this
draft changes no installed helper and therefore provides no runtime enforcement.

## 6. On-command guard and outcomes

Cover every admitted mutation/model path, including manual Fable audit/consult,
program bridge, builder dispatch and quota resume. A convenience wrapper alone
is bypassable: the installed fixed helpers must reject calls without a current
qualified recovery generation/epoch. Read-only diagnostics remain available.
Recovery adds no alternate dispatch route: execution still follows the approved
workflow serialization and route fence in `CONTROL_PLANE_RUNTIME.md`.

The replacement hold must also apply before any legacy release command; section
5's restriction is a required change, not an assertion that current main already
enforces host epochs. Complete entrypoint coverage, including runner-originated
calls, remains to be demonstrated in the implementation/A3 review.

| Step | Required behavior |
| --- | --- |
| 1. Validate caller and command | Existing actor/allowlist/argument checks; no caller-selected script/path/retry |
| 2. Serialize recovery | One protected recovery transaction per installation; competitors get BUSY and admit nothing |
| 3. Inspect installation | Check accepted manifest, fixed files, principals, mounts, privileges and actual source digests |
| 4. Restore missing components | Install only missing approved bytes/principals; retain original state; incomplete work stays closed |
| 5. Verify canonical state | Integrity, complete inventory, freshness witness and single-host epoch fence |
| 6. Verify authorization/authentication | Existing activation/service qualification and billing route; reject missing/expired credentials without substituting tokens or API keys |
| 7. Reconcile prior execution | Reuse current task/request/owner records; unresolved old operations stay fenced |
| 8. Hand off once | Invoke only the originally authorized operation through its existing admission path, or return its stored outcome |

`RECOVERY_READY` proves only that the recovery checks passed for the bound
installation generation. It is not an audit verdict, launch permit, retry,
merge command or activation grant. Every existing operation gate runs afterward.
Same packet/request semantics and write-before-side-effect admission are retained.
Recovery must not synthesize a new event/request/attempt to avoid deduplication.

If any required prior state is absent/corrupt/unfresh, return a bounded HOLD/ERROR
with the affected operation IDs and evidence pointers only. No token values,
raw command lines, packets, signing nonces or model transcripts are returned.
Diagnostic reinstall may restore binaries while remaining NOT_READY, but cannot
initialize an empty ledger/journal or create PENDING authorizations as grants.

Authentication checks first use non-model read-only methods. Any qualification
that actually invokes a provider requires its existing scoped authorization;
recovery itself does not silently issue a paid or model probe. Keep current
extra-usage/API/fallback guards and consumed usage claims. No billing-setting
change, quota purchase or new retry is part of restoration.

## 7. Implementation work packages

Do not dispatch these merely because this proposal exists. Use the canonical
task envelope and mechanical lane assignment; each substantive task has one
owner and the required non-author reviewers. Do not invent configured lane IDs.

| Package | Deliverable and dependency |
| --- | --- |
| HR-01 | Read-only host/storage feasibility inventory and approved trust/witness decision; no reboot, secret or installation mutation |
| HR-02 | Exact-pin manifest validator, offline plan and reproducible installer; depends on HR-01 architecture acceptance |
| HR-03 | Durable state migration/verification and crash-consistent witness/epoch fencing; retain original evidence; no empty init |
| HR-04 | On-command recovery guard plus mandatory checks inside every fixed operational entrypoint; no polling or new route |
| HR-05 | Adversarial tests, exact-HEAD independent A3, User adoption and separately authorized stopped host canary |

Proposed code belongs under `engineering/scripts/` with executable entrypoints
installed only after exact acceptance. Proposed schema/example artifacts belong
under `engineering/.github/control-plane/`; runtime manifests, journals,
installation receipts, session state and credentials never belong in Git.
`control_plane_install.py` keeps its disabled-only contract; use a separate
accepted recovery installer rather than weakening it.

## 8. Acceptance and adversarial verification

The following are required implementation tests/evidence, not claims of tests run
for this documentation proposal.

| ID | Scenario | Required result |
| --- | --- | --- |
| T01 | Known missing binary/config/account; complete current state | Restore accepted bytes/identity; original operation admitted at most once |
| T02 | Healthy host; same command delivered twice | No reinstall; existing dedupe result; no second external effect |
| T03 | Two commands encounter a missing installation together | One recovery owner; no competing state/UID/install mutation |
| T04 | Crash at every install/checkpoint stage | Incomplete receipt retained; partial installation cannot admit |
| T05 | Absent/corrupt SQLite, admission journal, quota claim or run archive | HOLD; no empty initialization or fence deletion |
| T06 | Internally valid earlier snapshot after a consumed claim | Rollback/freshness failure; no new automatic quota/model attempt |
| T07 | Witness write timeout or lost acknowledgement | No side effect; ambiguity retained and reconciled before resumption |
| T08 | Old and new host overlap; old sender arrives late | Single accepted epoch; late old-host admission rejected |
| T09 | Retained active/SUBMITTING/UNKNOWN request, local process absent | Owner and reservation retained; no blind resubmit/release |
| T10 | Completed operation or publication acknowledgement lost | Recover protected outcome/evidence; never rerun the model to recreate it |
| T11 | Mutable installer/cache/manifest, traversal, symlink, hardlink or rename race | Reject without privileged execution or authority promotion |
| T12 | Unsupported mount, missing binding or writable backing alias | Reject; no underlying empty-directory fallback |
| T13 | UID reused, broken parent ownership, changed ACL or policy | Reject; no blanket chown/chmod or new sudo capability |
| T14 | Expired credential, changed CLI/model/route or overage enabled | HOLD with existing guard semantics; no token fallback/settings mutation |
| T15 | Direct call bypasses wrapper; stale recovery receipt/epoch | Fixed helper rejects operational admission |
| T16 | Pending/unmerged #47, old audit, changed hash or activation | Reject; no install-latest or carried-over approval |
| T17 | Recovery-only command without operation authorization | No model, launch, retry, merge or enable |
| T18 | Actual stopped canary through normal platform replacement | Whole state inventory, generations, owners, limits, consumed claims and bindings survive; no test/provider launch caused by recovery |
| T19 | Retained CONFIRMED request and valid terminal pin; new guest has no lane processes | Legacy `reap` blocked across old/unverified epoch; reservation retained |
| T20 | Operator invokes legacy reconcile/settlement after replacement or rollback | Old actor permission alone cannot release; replacement-aware evidence/authority required |
| T21 | Cold replacement removed root helper/privilege route | `PRIVILEGED_EXECUTOR_UNAVAILABLE`; operator handoff, no workspace sudo/bootstrap fallback |

Synthetic fixtures cannot establish T18, real provider billing, actual Unix
isolation or platform snapshot durability. Record actual qualification evidence
at the final accepted installed SHA before adoption. Never reset/update a live
three-hour audit just to test recovery.

## 9. Rollout and current-loss handling

First perform HR-01 read-only feasibility work. Keep this proposal DRAFT until
the protected durable backing, authenticated bootstrap anchor, independent
freshness witness and epoch handoff have a concrete reviewed implementation.
No storage scheme earns READY merely because platform files are designed to
survive normal updates and recovery. That design intent is not measured
durability, protected ownership, freshness or a guarantee against snapshot loss.

The reported Grok instance already lost its `/opt`, `/etc`, accounts and
`/var/lib` installation/state. This proposal cannot retroactively recover data
never retained. Inventory retained workspace data and external session/evidence
without rerunning them. If the full original state cannot be recovered/reconciled,
retain the affected holds. Restoring executables does not authorize a fresh empty
program host or a repeat of an interrupted audit.

After accepted code and preserved state exist: stop new admissions; reconcile or
retain prior unresolved execution; migrate with verified crash-consistent writes;
retain the original state; qualify the new boundary and the stopped replacement
canary; obtain normal User adoption/activation through the existing process.
Recovery never changes the enabled state on its own. A failed migration rolls
back installation only after preserving all newer admitted state and fencing
senders; it never rolls the canonical ledger back to a convenient older backup.

This draft changes no runtime code, activation, host pin, sudoers, account,
secret, billing setting, provider session, product repository or KIX CI.

## 10. Open adoption questions and incomplete review follow-up

The User supplied an unfinished intermediate review. It is not a final review
or an independent exact-HEAD Astra verdict. The following questions remain open;
the clarifications above do not claim to resolve their mechanisms:

- Old-host/network-partition fencing: specify the actual enforcement point and
  sender revocation, including external provider sessions.
- Freshness witness: define its contents, write-ahead/local durability ordering,
  interrupted transitions and recovery of authorization, not just a counter.
- Manifest trust: choose a non-circular bootstrap verifier and specify replay,
  rollback, revocation and all runner entry paths.
- Platform/CLI qualification: determine the actual replacement lifecycle,
  restoration race behavior and whether fresh host authentication can use a
  non-model check. If it cannot, existing separately authorized qualification is
  required; do not claim an unsupported non-model authentication operation.

Still-unconfirmed review candidates require original-source/adversarial checking
before a finding or resolution is assigned: full command coverage (including
merge, reap, materialize and preflight), READY/use-time races, result-code/test
coverage, HR-01–05 sequencing, accepted/approved/adopted terminology, and backup
inventory/T10/T14/T18 expectations. They remain follow-up candidates rather than
verified defects, waived conditions or completed acceptance tests.
