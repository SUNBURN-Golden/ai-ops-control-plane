# Native Mac host preparation: isolated diagnostic partition

The Mac is an execution host. This repository is shared source/policy, not an
implicitly required remote server. Existing-host SSH, a remote claim endpoint,
and Work Astra connectivity are **not prerequisites** for local installation,
offline builder diagnostics, or isolation development. User decision/intake:
<https://github.com/BeautifulMind-JT/ai-ops-control-plane/issues/21#issuecomment-5856520598>.

## Deliberately limited installed runtime

`engineering/scripts/control_plane_mac.py` installs a native, non-root diagnostic
runtime into the current account's `~/.astra-mac`. It does not modify the Linux
protected host contract, existing host, provider credentials, shell aliases,
deployment eligibility, relay policies, or `~/Library/LaunchAgents`.

The installed commands support ONLY these fixed, credentialless diagnostics:

| Lane | Actual executable diagnostic | Not demonstrated by this check |
| --- | --- | --- |
| DEVIN | resolved installed Devin CLI `--version` | subscription inference / remote session |
| GROK_BUILD | native Grok `--no-auto-update --version` | model inference / quota |
| CURSOR | installed Cursor CLI `--version` with isolated HOME | authenticated model inference |
| GLM | ZCode's bundled CLI, Node `doctor --json` | OAuth, Coding Plan entitlement, GLM inference |
| ISOLATION | own-task read/write, public control/other-task canary denial, symlink escape, loopback denial | adversarial production isolation |
| LIFETIME | fixed 30-second local process | a resumable provider session |

There is no prompt, arbitrary-command, existing-task import, provider dispatch,
network-enable, production-enable, or handoff option. ZCode `--prompt` defaults
to yolo in the observed version; this runtime never invokes it. GLM is ZCode,
not an implicit Claude Code/OpenCode/Cursor replacement.

Install after reviewing the exact source snapshot (a partial/existing install
is never replaced or repaired automatically):

```sh
python3 engineering/scripts/control_plane_mac.py install \
  --confirm-local-diagnostic-install \
  --evidence 'https://github.com/BeautifulMind-JT/ai-ops-control-plane/issues/21#issuecomment-5856520598'
```

Use the absolute interpreter recorded in `policy.json` and the installed script
for subsequent commands; do not run operational commands from a working checkout:

```text
<pinned-python> -I <home>/.astra-mac/bin/control_plane_mac.py new ISOLATION
<pinned-python> -I <home>/.astra-mac/bin/control_plane_mac.py start <request-uuid>
<pinned-python> -I <home>/.astra-mac/bin/control_plane_mac.py recover <request-uuid>
<pinned-python> -I <home>/.astra-mac/bin/control_plane_mac.py stop <request-uuid>
<pinned-python> -I <home>/.astra-mac/bin/control_plane_mac.py release <request-uuid> --evidence <published-terminal-evidence-url>
```

`new` creates a fresh workspace and UUID-based
`macdiag:<installation-uuid>:<request-uuid>` task. No existing task IDs are
accepted. Private control files, logs, receipt and SQLite ledger stay outside
Git. Public canaries contain no credential material. No source checkout or user
document is exposed to a diagnostic child. Source/interpreter and adapter entry
digests are recorded and checked; digest changes fail rather than auto-upgrade.

## Ownership, persistence and recovery

Admission uses the existing `Ledger` primitive with SQLite `BEGIN IMMEDIATE`,
`synchronous=FULL`, a one-active-owner index, capacity one, and an in-flight
`flock`. GitHub comments are evidence, **not a lock**. This newly partitioned
ledger covers only minted local diagnostics; it is not a copy of an existing
host ledger and does not claim distributed ownership over that host's tasks.

The durable `SUBMITTING` row precedes `launchctl bootstrap`. An acknowledged
`CONFIRMED` result means an offline local diagnostic unit was registered, NOT
that any provider session/model succeeded. Bootstrap errors/timeouts are
`UNKNOWN`. A cached request (including UNKNOWN or reconciled requests) is never
submitted again. A permanent `O_EXCL` worker marker also rejects a manual second
worker/kickstart. Missing/corrupt ledgers are never recreated during recovery.

Launchd runs a one-shot trusted coordinator, independently of the submitter.
Plists remain in the private control directory, with `KeepAlive=false`, no
standing scheduler and no login/reboot installation. The coordinator starts a
fixed diagnostic in `sandbox-exec`, with isolated HOME/TMPDIR, an allowlisted
environment, read-only CLI/runtime files, no network rule, no credential access,
and write access only to its workspace. It kills remaining child process-group
members before recording a terminal receipt, observing group disappearance for
at most five seconds so delayed descendant exit is not immediately sealed as a
permanent unresolved receipt. Still-unconfirmed cleanup retains the slot.
SIGTERM requests cancellation;
it does not itself prove cleanup or release the slot.

`recover` is a read-only status operation. No retry, restart, new owner,
timeout-based expiry, or fallback occurs. Release requires a matching terminal
receipt, absent child process group, unchanged report digest, an explicitly
exited/absent launchd unit, successful unit removal, and a published GitHub
evidence URL. Launchctl permission, domain, unfamiliar-state, and timeout errors
are not treated as absence. A killed coordinator without a terminal receipt
leaves its slot unresolved; there is intentionally no automatic reset command.
Only a NEW diagnostic task may be launched after explicit terminal release.

## Security and qualification limits

This is an **offline diagnostic partition under the existing user account**,
not the protected multi-UID production admission boundary. The account owner
can modify the runtime/ledger/policy, impersonate a receipt, or launch other
programs outside it. File metadata remains readable, and CLI bundles may have
mutable dependencies beyond the pinned entry files (notably Cursor chunks).
Entry hashes are drift detection, not an immutable software supply-chain proof.
No defense against another malicious same-UID process is claimed. Process-group
cleanup of these fixed CLI probes is not a proof against a hostile process that
escapes the group. Native Seatbelt is an additional diagnostic restriction, not
a promise that all future provider tools or networked children will be safe.

No stored authentication is copied into the sandbox. Failing under the offline
profile is a diagnostic failure, not permission to relax it, enable networking,
use an API key, or change models. Authentication, included quota, provider/tool
session identity, bounded real inference, stop/resume semantics, and independent
reviewer read-only isolation require separate qualification. Mac production
activation still requires its reviewed execution boundary and explicit decision.

Existing-task handoff is separately blocked until canonical owner, every
unresolved/active session, sender fencing, and a single shared admission
authority (or verified exclusive transfer) are proven. Existing-host
unreachability is not a blocker for fresh local diagnostics, but cannot be
interpreted as proof that its workers are stopped. Work Astra round-trip remains
separate. Local tests, CLI startup, login, and true end-to-end acceptance must
never be reported as interchangeable.

## Source verification

```sh
python3 -m unittest discover -s engineering/scripts -p 'test_control_plane_mac.py' -v
ASTRA_TARGET_REPOSITORY=BeautifulMind-JT/kix-protocol python3 engineering/scripts/control_plane.py validate-repo
```

The fault tests cover UUID scoping, extra-prompt/import rejection, atomic
reservation, concurrent duplicates, crash/UNKNOWN retention, missing-ledger
failure, one-shot worker markers, altered packets/plists/receipts, launchctl
ambiguity, and terminal release preserving dedupe. Native runtime evidence and
exact-source non-author review belong in the canonical issue/PR, not committed
runtime logs or secrets. These tests do not qualify production dispatch.
