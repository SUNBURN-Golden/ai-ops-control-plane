# Builder lanes

This is source registration, not host qualification or activation. Task ownership,
launch identity, UNKNOWN reconciliation and the protected admission limit remain
mandatory. One task has one writer. `max_active_sessions=1` remains unchanged.

| Builder ID | Execution surface | Qualification |
|---|---|---|
| DEVIN | Existing Devin adapter | Exact installed adapter and durable session evidence |
| GROK_BUILD | Official xAI Grok Build CLI | Dedicated builder identity; native xAI session and quota |
| GLM | Explicitly approved GLM harness | Supported model/harness and subscription evidence; no implicit OpenCode replacement |
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

1. Install/authenticate the official CLI under a dedicated builder Unix identity.
   Record CLI version, binary/wrapper digest, subscription account and model listing
   without secrets. Choose the exact supported Grok model/effort from `agent models`
   or `agent --list-models`; `auto`, aliases and guessed slugs are not qualification.
2. Install `/opt/astra/bin/astra-builder-cursor` using the existing wrapper contract.
   This PR registers that path; it does not ship or install a supervisor adapter.
   Until the real adapter exists, admission/preflight must fail closed.
3. Prove isolated task workspace, durable execution after wrapper/runner teardown,
   exact session ownership, duplicate launch rejection and UNKNOWN reconciliation.
   `create-chat` / `--resume` alone are not process-lifetime evidence.
4. Prove credential separation and reviewer read-only boundaries at the OS/workspace
   level. CLI prompt wording or absence of `--force` alone is insufficient.
5. Qualify the exact report with `control_plane_flow_cli.py qualify-lane` against the
   audited implementation SHA. CURSOR requires `harness=CURSOR_CLI` and an explicit
   `model` field containing that exact model ID. Pin model effort, billing mode and wrapper in the approved report.
   The optional task `EXECUTION_PROFILE_POINTER` points there; it grants no authority.
6. Independently review source and installed adapter, then update protected lane,
   runtime and flow policy through the existing approval gate. Run one diagnostic
   canary, close its session and reconcile its ledger before general intake.

Legacy three-lane host configuration stays valid. Enabling an unregistered lane is
rejected. Source enum membership never implies authentication or production readiness.
FILM/MAEUM rollout and SOULBOUND remain outside this change.

Official interfaces (verify installed/account support at qualification):
- https://cursor.com/docs/cli/reference/parameters
- https://cursor.com/docs/cli/reference/authentication
- https://cursor.com/docs/models/grok-4-7
- https://cursor.com/docs/models-and-pricing
- https://docs.x.ai/build/overview
