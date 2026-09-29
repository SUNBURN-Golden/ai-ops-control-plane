# Builder lanes

This is source registration, not host qualification or activation. Task ownership,
launch identity, UNKNOWN reconciliation and the protected admission limit remain
mandatory. One task has one writer. Program mode (`docs/PROGRAM_MODE.md`) allows one
session per lane (host index `one_active_per_lane`) and raises `max_active_sessions`
up to the number of enabled lanes; outside program mode keep `max_active_sessions=1`.

| Builder ID | Execution surface | Qualification |
|---|---|---|
| DEVIN | Existing Devin adapter | Exact installed adapter and durable session evidence |
| GROK_BUILD | Official xAI Grok Build CLI | Dedicated builder identity; native xAI session and quota |
| GLM | Explicitly approved GLM harness | Supported model/harness and subscription evidence; no implicit OpenCode replacement. The installed host harness is OpenCode `1.18.32` with the Z.AI Coding Plan, explicitly approved by User decision M2 (2026-09-29) |
| CURSOR | Official Cursor CLI (`agent`) | `harness=CURSOR_CLI`, exact model ID from installed CLI, isolated durable supervisor |

CURSOR with a Grok model is not GROK_BUILD. Credentials, session IDs, model IDs,
subscription pools and adapter hashes remain separate. CURSOR is not a GLM alias.
No CLI/model is selected because its display name resembles another lane.

## Useful work per token

- Grok Bot is an optional command relay. Mechanical intake, current facts, CI
  aggregation, request dedupe and Slack delivery require no Grok reasoning.
- The assigned builder owns investigation, implementation choices within approved
  contracts, regression tests, debugging and retesting through PR evidence.
  Ordinary test failures stay with the same owner; no arbitrary two-failure stop.
- Spend available subscription capacity on relevant edge cases, adversarial tests
  and non-author review. Default: one writer and one independent reviewer. A second
  targeted review needs a concrete unresolved risk; do not create duplicate builds
  or ask Astra to synthesize competing implementations by default.
- A1/A2 use the non-author review gate. A3, an architecture exception or an existing
  release/milestone obligation invokes the designated Astra auditor. Review must
  independently classify actual changed areas; author self-classification is not
  sufficient. An author conflict requires a designated non-author auditor.
- Existing included quota may be used fully for this useful work. No unapproved
  on-demand billing, purchase, quota increase, account cycling or automatic fallback.
  An unavailable lane reports its exact blocker. Other registered lanes do not
  silently take over an in-flight ticket.

## CURSOR host acceptance

User decision M3 (2026-09-29) is option (b): CURSOR runs without systemd, under the
same wrapper → adapter → detached supervisor contract as the other lanes. Sources and
the host checklist are in `adapters/cursor/README.md`.

1. Install and authenticate the official CLI under the dedicated `astra-builder-cursor`
   Unix identity. That identity has a private HOME and no subuid/subgid ranges. Record
   the CLI version, binary digest, subscription account and model listing, without secrets.
   Choose the exact supported model/effort from the CLI's `models` listing. `auto`,
   aliases and guessed slugs are not qualification.
2. Write `/etc/astra/cursor-lane.json` (root-owned; example
   `.github/control-plane/cursor-lane.example.json`). It holds the CLI path, its digest,
   the exact model and the non-secret account fields of `status --format json`.
3. Install `astra-builder-cursor` at `/opt/astra/bin/`, and `astra-cursor-adapter` and
   `astra-cursor-supervisor` at `/opt/astra/libexec/`, all root-owned. Add the narrow sudo
   rule that lets the control identity run the adapter as the lane identity. No systemd
   user manager or linger is used. The supervisor starts in its own session, and the
   host init (tini) reaps it.
4. Prove the following:
   - the task workspace is isolated;
   - execution survives wrapper and runner teardown;
   - session ownership is exact (`cursor-cli:<chat>`);
   - a duplicate launch is rejected, and UNKNOWN is reconciled;
   - every lane process descends from the helper's launch.

   `create-chat` / `--resume` alone are not process-lifetime evidence.
5. Prove credential separation at the OS and workspace level. Also prove that the packet
   and the signing key never reach a process argument or environment. CLI prompt
   wording alone is insufficient.
6. Qualify the exact report with `control_plane_flow_cli.py qualify-lane` against the
   audited implementation SHA. CURSOR requires `harness=CURSOR_CLI` and an explicit
   `model` field containing that exact model ID. Pin model effort, billing mode and wrapper in the approved report.
   The optional task `EXECUTION_PROFILE_POINTER` points there; it grants no authority.
7. Review the installed adapter independently against `adapters/cursor/SHA256SUMS`.
   Then update protected lane, runtime and flow policy through the existing approval gate.
   Run one diagnostic canary, close its session and reconcile its ledger before general intake.

Legacy three-lane host configuration stays valid. Enabling an unregistered lane is
rejected. Source enum membership never implies authentication or production readiness.
FILM/MAEUM rollout and SOULBOUND remain outside this change.

The Cursor adapter requires observed official `status --format json` account fields
in `auth_match`, the exact `models` entry, current binary/wrapper digests and independent
host proofs. It creates a separate persistent clone/branch per full packet digest,
records actual checkout/chat/unit identity and starts a `systemd-run --user` service
with `Restart=no`. Runner teardown does not own that service. Any ambiguity is UNKNOWN;
an existing packet directory is never reused to launch. Reconcile before changing
attempt/revision. Admission, not the clone directory, is the global writer lock.

Headless writing uses the official `--print --force --trust` flags only after explicit
host qualification of `headless_write_authorized`. OS credential/workspace boundaries
are mandatory; these CLI flags are not a sandbox or reviewer read-only guarantee.
This adapter is a writer, not an independently qualified reviewer. Its tests use a
fake CLI/supervisor. They do not establish installed authentication, included quota,
Linux isolation, durable live execution or supported account models.

Launch uses a detached control-UID custodian with an owner-only `custodian_root`.
It retains `ASTRA_HOST_INFLIGHT_FD` through public-wrapper death; sudo, provider and
builder UID never inherit that descriptor. A normal, identity-matched CONFIRMED send
releases it. Timeout, nonzero, malformed result or bookkeeping failure retains the
fence indefinitely: inspect the private sender record, fence all possible senders,
then terminate the recorded custodian and perform evidence-based host reconciliation.
Do not auto-expire it or delete a record to retry. These process-boundary tests use
real subprocesses, but do not replace a live teardown test on the target host.
The adapter does not make provider credentials inaccessible to their own builder UID;
qualification must verify the approved provider/egress boundary and document that
trust assumption. Neither a forged environment nor direct worker entry is admission.

Official interfaces (verify installed/account support at qualification):
- https://cursor.com/docs/cli/reference/parameters
- https://cursor.com/docs/cli/reference/authentication
- https://cursor.com/docs/models/grok-4-7
- https://cursor.com/docs/models-and-pricing
- https://docs.x.ai/build/overview
