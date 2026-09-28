# Independent Mac and Grok-computer execution

User decision: 2026-09-28. [Correction record](https://github.com/BeautifulMind-JT/ai-ops-control-plane/issues/19#issuecomment-5863851980).

Mac Codex runs its own qualified builders and Work browser. The Grok computer
runs its own builders. Neither is the other's required execution gateway.
GitHub stores shared source, canonical tasks, assignment, action grants and results.
Each host owns its own flow/admission databases, workspaces and provider sessions.
Local admission permits the central repository, kix-protocol, kix-commerce-apps,
ZARI, film-unit-mv-studio and maeum-gyeol. SOULBOUND/unknown targets are rejected.
User added kix-commerce-apps on 2026-09-28 for independent Mac development with
KIX protocol and ZARI. This does not change legacy projects.json registrations,
product deployment flags or any existing host. A target entry alone does not
grant ownership or qualify a provider: exact issue/host assignment, local source
review, adapter qualification and shared action admission remain mandatory.
Slack projects status; no Slack message or Issue comment is an atomic lock.

## Shared task ownership

An exact User-pinned `ASTRA_HOST_ASSIGNMENT_V1` comment enrolls a **fresh canonical
issue** on one host/installation instance. Fields:

```json
{
  "active": true,
  "repository": "BeautifulMind-JT/ai-ops-control-plane",
  "issue": 123,
  "github_issue_id": 123456,
  "github_issue_created_at": "2026-09-28T00:00:00Z",
  "task_id": "MAC-WORK-001",
  "host_id": "mac",
  "instance_id": "00000000-0000-0000-0000-000000000000",
  "admission": "FRESH_TASK",
  "operations": ["WORK_DIAGNOSTIC"],
  "pointer": "https://github.com/BeautifulMind-JT/ai-ops-control-plane/issues/123#issuecomment-123"
}
```

Example values grant nothing. Resolve real issue ID, creation time, installed
host instance and comment URL before pinning its body SHA256 in private policy.
The enrollment is a User assertion that the issue is new work, not a renamed
legacy task. An absent ownership ref alone never proves legacy work is idle.
Use a new diagnostic issue; Issue19 remains the umbrella/history record.

`heads/aiops-ownership/<sha256(lowercase repository, issue)>` is the shared record.
The control branch has one constant `.astra-control-format` marker and minimal
JSON commit messages (GitHub rejects empty create-tree requests);
it is never merged to main and never contains transcripts, secrets or live-ledger
dumps. Initial ref creation chooses one owner. Subsequent transitions use a fresh
unique mutation nonce, a commit whose sole parent is the observed tip, and
`force=false`. Competing proposals are siblings; the losing update cannot launch.
This uses GitHub ref serialization, **not** comment existence or an exactly-once
provider claim. Losing responses remain unresolved even if the write succeeded.

The record retains every action's SUBMITTING/terminal state. A new action ID,
HEAD or revision cannot bypass an unresolved prior action. Local intent persists
before GitHub writes, then the confirmed shared grant precedes provider/browser
effects. No write retry, rebase of a losing proposal, delete, force push, expiry
or automatic takeover. Terminating one action preserves its owner and tombstone.
Missing local state or a deleted known ownership ref blocks; never recreate it.

GitHub availability is required for **new starts**, not the other compute host.
An existing builder may keep its normal local debug/test loop during an outage;
new starts, cross-host takeover and uncertain control effects remain blocked.

## Credential / trust boundary

The protected controller needs GitHub Contents write for the reserved control
refs and Issues write for its evidence, with read access to assigned task repos.
Only the owner/operator may supply this credential. Builders, reviewers and the
Work browser must not receive it or have access to its files/keychain context.
GitHub does not provide branch-specific Contents token scopes: restrict token
installation/repository scope, protect source branches, restrict the reserved
namespace against unauthorized force/deletion, and qualify these controls before
live use. These are not permissions granted to a model by its prompt.

The registry trusts the protected controller and pinned User enrollment, not
commit author text. An administrator/token holder capable of rewriting refs is
outside the cooperative-controller concurrency guarantee. No claim of malicious
administrator resistance or automatic disaster recovery is made.

## Native install and Work diagnostic

For new Mac installations the default is `execution_transport=APP_SCREEN`:
Codex operates the actual logged-in app/browser with desktop computer-use tools.
It does not need to start a second Codex CLI or prove a ZCode headless interface.
See **Mac app-screen transport** below for the executable protocol. The existing
CLI transport remains explicit/compatible for older installations and other
hosts. This does not change Grok Bot, the Grok computer, or any existing policy.

Run as the dedicated local controller account. A private user-owned installation
is supported; root, sudo, a listening server and the old VM are not required.
Do not share this account's control credentials with untrusted model processes.

```sh
# Create a private parent outside product worktrees; command never replaces it.
mkdir -m 700 "$HOME/.astra-independent"
python3 engineering/scripts/control_plane_local_host.py init \
  --root "$HOME/.astra-independent/mac" --host-id mac
python3 "$HOME/.astra-independent/mac/bin/control_plane_local_host.py" check \
  --policy "$HOME/.astra-independent/mac/policy.json"
```

`init` creates a disabled private policy, exact source digests, a unique instance
ID and its own SQLite partition. It never opens or copies the old host ledger or
the existing Mac diagnostic admission.sqlite. A partial/existing root is not
silently repaired. Inspect existing local sessions before qualifying this host;
new partition creation is not proof that other manually launched local writers
have stopped. `max_active_sessions=1` applies inside this controller.

For the existing CLI transport, fill the private policy with actual values:
- `enabled`, source-review PASS/evidence, `user_actors`, projection actor;
- `assignment_binding`: repository, issue, comment_id, actor, SHA256;
- `diagnostic`: enabled, source-review evidence, explicit request authorization,
  fixed document/question request and `authorization_expires_at`;
- `relay`: existing qualified Codex binary/catalog/home and owner-only state
  directory equal to `<root>/relay`, browser qualification/evidence, no claim URL.

Generate the diagnostic with `flow.diagnostic_request(..., issue=<new issue>,
task_id=<assigned task>)`; its designation points to the User-pinned diagnostic
authorization comment in that same issue. Same fixed document/question and narrow
DIAGNOSTIC_RESULT contract as LOCAL_CODEX.md; no audit or decision authority.
Keep `live_acceptance=PENDING`. No HMAC provisioning or `create_app` root-service
installation is required: the protected controller invokes local ports directly.

Inject `ASTRA_HOST_GITHUB_TOKEN` into the controller from the dedicated local
secret mechanism, never prompts/policy/GitHub. Slack credentials are not required. The
browser child inherits only PATH/HOME/CODEX_HOME/LANG; same-UID file/keychain/MCP
access must still be independently restricted and qualified. Browser-only tool
access cannot be established by filesystem read-only mode or prompt text alone.

```sh
python3 <root>/bin/control_plane_local_host.py work-diagnostic --policy <private-policy>
# Only when the same request explicitly returned WAITING:
python3 <root>/bin/control_plane_local_host.py collect-diagnostic --policy <private-policy>
```

No polling, standing routine, repeated send or automatic model replacement.
Collection performs one read of the same Work conversation with the persisted
model/effort. Work result publication leaves ownership active until verified
terminal/fence evidence. Another host cannot resume this browser conversation.

## Builders and independent review

`run-adapter --policy ... --binding <private action-binding>` verifies a pinned
User `ASTRA_HOST_ACTION_V1` comment containing `active:true, packet:{...}`. Packet
fields: repository, issue, task_id, revision, head, host_id, instance_id,
builder_id, operation (`BUILDER` or `REVIEW`); review also names pr and read_only.
Bindings stay on the assigned canonical issue. The packet is the exact authorized
work, not requirements rewritten by Codex. One builder owns implementation,
test/debug/retest and PR creation without per-step approval.

Each DEVIN/GROK_BUILD/CURSOR/GLM adapter must be independently qualified on this
host/instance, exact executable hash, allowed operations and durable execution
mode. Version/doctor tests do not qualify it. CURSOR is a harness; specify its
actual model. A CLI-mode GLM adapter needs a supported headless interface;
Mac APP_SCREEN uses the actual ZCode screen instead. GUI presence alone still
does not prove a completed request or credential/workspace isolation.
No automatic provider fallback or subscription purchase. Reviewers are read-only
and non-author; existing review/A3/merge gates remain in force.

The controller creates one isolated workspace, reserves local/global ownership,
then calls `[adapter, builder|review]` with the bound JSON packet plus request_id
and worktree_root. The adapter must return the same request_id,
`outcome=CONFIRMED`, and an actual durable session_id. A bad/lost response remains
UNKNOWN. Neither wrapper exit nor CI green proves that the session ended.

`finish --binding ...` requires a User-authenticated `ASTRA_HOST_TERMINAL_V1`
comment bound to the task/host/instance/action with sender_fenced, session_ended,
publication_resolved. Work publication and local consumer fences are checked too.
Only then does it free the local slot and mark the shared action terminal.
No owner-transfer command is implemented; legacy handoff remains blocked until a
separately reviewed fenced transfer is supplied. This does not block fresh Mac work.

## Mac app-screen transport

This is a split-phase desktop coordinator protocol, not a Python GUI driver or
an unattended daemon. The installed controller validates ownership and state;
Codex's computer-use tools read/type/click the actual selected app. There is no
shell/AppleScript UI bypass and no hidden CLI/model fallback. Logged-in app
subscriptions are used without copying their credentials. Grok Bot is not a
builder or browser driver and is not changed by this route.

New Mac `init` policies remain **disabled** with empty `desktop` registrations.
Existing installs are never migrated or activated implicitly. After reviewed
source installation, explicit APP_SCREEN configuration names `desktop.WORK`
or a builder lane (`DEVIN`, `GROK_BUILD`, `CURSOR`, `GLM`). Each registration has:

- `execution_mode: APP_SCREEN`, exact `host_id` / `instance_id`, `operations`;
- `target`: exactly `app`, `account` (non-secret account label), `session`
  (stable conversation/session locator), `model` and `effort`;
- `isolation_evidence`: GitHub pointer to verified controller-credential and
  task-workspace isolation; reviewers also require `read_only_enforced: true`;
- `coordinator`: `{model: gpt-5.6-sol, effort: max}`, or the approved `xhigh`
  alternative chosen **before claim**; `supported_coordinators` records pairs
  actually observed in the dedicated coordinator environment's model list.

These are protected operator configuration assertions, not cryptographic proof
or a sandbox implementation. An app that has unrestricted access to controller
credentials is not qualified merely because it is logged in. A review prompt
saying "read-only" does not enforce read-only rights. No privileged settings,
Docker-socket exposure or sandbox removal is authorized to satisfy these fields.
Do not invent evidence, supported models, sessions or account labels.

WORK target `session` must be the diagnostic's dedicated Work URL, with displayed
`GPT-6 Astra` / `medium`. Internal model ID remains unverified/null. Builder action
authorization additionally contains `ui_target` equal to that lane's target.
CURSOR retains CURSOR identity even when its selected model is Grok; never call
it native GROK_BUILD. Reviewer sessions must not have participated in writing.

The existing assignment, diagnostic authorization, source-review and protected
GitHub credential requirements still apply. No CLI binary/hash/catalog, HTTP
gateway, VM, HMAC credential or Slack delivery is required for APP_SCREEN.
General intake, graph activation, product permissions and merge gates do not
change. Do not enable them to test this transport.

One invocation at each step; no polling or standing model session:

1. Inspect the actual app/account/model and existing sessions. Select a dedicated
   session without submitting the job. Prepare its exact binding and isolation.
2. Run `prepare-ui --policy <private-policy>` for Work, or add
   `--binding <private-action-binding>` for a builder/reviewer. The ordinary
   `work-diagnostic` / `run-adapter` entrypoints also return this preparation when
   APP_SCREEN is selected. Preparation reserves existing local/shared admission,
   projects/claims Work when applicable, and creates a unique empty task directory.
   It returns `UI_READY`, the authorized packet and target including `workspace`.
   **This is not permission to send.** Provision/check the task checkout and
   enforced reviewer rights within that directory; never reuse another workspace.
3. Read the actual app again using desktop tools. Verify app, account, exact
   existing session, model/effort, task workspace and available input. Save a
   private observation containing `action_id`, the exact returned `target`, and
   `input_ready: true`. Do not synthesize this from configuration without reading
   the screen. Run `send-ui --policy ... --observation <private-observation>`.
4. Only a **newly returned** `send_allowed: true` permits entering/submitting the
   exact packet once with desktop tools. The controller has already committed
   UNKNOWN before returning this one-shot permit. Verify the target is unchanged
   immediately before typing; if focus/navigation changed, stop and reconcile,
   not retry. Do not expose private policy, auth material or control paths to the
   app; only the job packet and isolated workspace are app-facing.
5. Read the same session. Run `collect-ui --policy ... --observation ...` with
   `action_id`, identical `target`, and `outcome`: UNKNOWN (ambiguous), WAITING
   (visible in-progress request), SESSION_OBSERVED (builder/reviewer), or ANSWER
   (Work only, plus the existing `diagnostic_result` schema). Session/request
   visibility and the exact submitted packet must be checked by the coordinator;
   the Python layer cannot authenticate a screenshot or identify what was typed.
   Capture result pointers in the canonical GitHub task. Work publication uses
   the existing authenticated collector and durable outbox, not copied text
   posted as a fabricated success receipt.
6. WAITING permits a later bounded read of the same session, not another send.
   UNKNOWN permits evidence-based observation of the existing session, never
   resend/new ID/model replacement. A crash during RESULT_SUBMITTING requires
   separate reconciliation; do not replay collection based on elapsed time.
   Existing `finish` with exact sender/session/publication fence evidence is
   still required; collecting an answer never releases ownership automatically.

The one-shot guarantee covers controller-issued permits for cooperative desktop
coordinators, not arbitrary replay of a previously printed permit or manual app
use outside this protocol. Lost command output consumes its permit. Persisted
policy/authorization bindings cannot be swapped after preparation. Concurrent
send requests get at most one permit; successful result replay is idempotent and
changed results are rejected. Browser observations grant no audit PASS, User
decision, builder authority, merge or live-acceptance promotion.

Source tests exercise this protocol with fake UI observations and GitHub. They
do not establish that the actual apps were driven, read-only isolation exists,
or a live Work roundtrip passed. A3 architecture review and per-host qualification
remain separate gates before live use.

## Graph and deployment status

The existing graph code retains its legacy route; any actual deployment still
requires that host's qualification evidence. It is not silently activated on Mac. An independent-host graph adapter must use the
same shared ownership/action admission; unqualified direct graph routes are
blocked. Until that bridge is qualified, use the local-host controller for explicit
task dispatch. Deterministic graph display does not grant execution rights.

Source tests use temporary databases, fake GitHub ref operations and fake browser
receipts. Live API ref protection, actual Mac installation, authenticated builder
sessions and Work roundtrip require host evidence; no source PASS claims them.
Existing host configuration, product flags and global activation remain unchanged.

Shared control history must be protected against ref deletion/force updates by
non-controller credentials. Known local checkpoints additionally reject rollback,
truncation and removed action history. Qualification of that permission boundary
is required before live multi-host use; source tests do not establish permissions.
Local Work delivery uses its verified GitHub projection directly and needs no
Slack token. Slack is an optional notification surface, never a launch prerequisite.
An unclaimed prestart failure can be closed with exact User terminal evidence
including `failed_prestart=true` and `downstream_never_started=true`; unresolved
publication still blocks closure. Terminal reconciliation also works after the
canonical issue is closed. It preserves shared tombstones and never relaunches.
