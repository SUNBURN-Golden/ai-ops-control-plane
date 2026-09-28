# macOS Codex coordinator / cloud Work Astra mid

CP-LOCAL-001: https://github.com/BeautifulMind-JT/ai-ops-control-plane/issues/19
User authorized this design on 2026-09-27. Source candidate, not an installed Mac.

## Roles and cost

Mac Codex relays explicit commands, collects pointers and operates the qualified
cloud Work browser. It is not the builder, architect, independent auditor or User.
User explicitly selected gpt-5.6-sol / max on 2026-09-27, superseding the earlier
gpt-6-sol / ultra selection ([User decision](https://github.com/BeautifulMind-JT/ai-ops-control-plane/issues/19#issuecomment-5856142120)).
The disabled example records that choice; it is not proof
of installed availability. Verify the exact model and max effort in the intended
CLI/account's `model/list` response. User also explicitly authorized one alternative:
gpt-5.6-sol / xhigh (Extra high). Only before claim, select it when max is unsupported
and xhigh is explicitly supported in the qualified complete catalog. Both pairs use
the same model entry; a missing or hidden gpt-5.6-sol blocks both choices.
Malformed or ambiguous catalog entries block; they do not trigger fallback.
Both pairs require their exact effort to be listed. Persist the chosen model/effort
with the request before network effects. No post-claim switch, automatic retry,
quota-error fallback or new conversation after a failure/UNKNOWN. This exception
applies only to the coordinator, not builders or cloud Astra.
Cloud Work Astra mid uses the verified Astra model and medium effort in its own
session. Normal builder debugging never goes through Astra. Consequential matters
outside durable User delegation remain User decisions; a relay cannot expand it.
GitHub remains authoritative. No extra paid API route or quota purchase.

One owner investigates/implements/tests/fixes, commits and opens a PR. A non-author
reviews the exact SHA; additional review is risk-scoped. A3 invokes its required
architecture gate. Author conflicts require a designated non-author substitute.
Codex delivers findings literally; the same builder fixes them. Merge authority
remains User or an explicit scoped delegation, not a browser answer.

## Implemented client

`scripts/control_plane_codex_relay.py` is portable stdlib Python. It does not install
a runner, start builders, copy ledgers or change host settings. Its only network
write in operational mode is a single authenticated POST to the existing `/astra/claim`; a qualified
Codex browser session performs the user-authorized Work message delivery.

1. The existing mechanical layer creates and durably projects AUDIT/DECISION,
   and confirms its Slack notification. Codex consumes this existing request ID;
   it does not create a competing request or become a second event bus.
2. Protected configuration explicitly maps request ID to a fresh dedicated Work
   conversation URL. Creating/identifying that conversation must occur before
   enabling delivery. No automatic browser login, conversation search or model guess.
3. Persist CLAIMING locally, then ask the existing central ledger for one analysis
   start. The real Work URL is the consumer session ID, not a fabricated Astra ID.
   The gateway authenticates the designated identity, current SHA/revision/scope,
   confirmed GitHub projection and source delivery. Denial means no model call.
4. Persist SUBMITTING before invoking `codex exec` once, using a read-only sandbox,
   subscription auth and a dedicated Codex/browser environment. Prompt contains
   pointers/identity only and instructs one exact send to cloud Work Astra mid.
5. Codex returns a transport observation. Even RESULT_POINTER is not audit PASS.
   The existing authenticated GitHub collector must verify result identity, scope,
   revision and SHA; consequential decisions still need the proper approval record.
   This client never automatically resumes or merges work.

Local tombstones are an extra duplicate guard, not the global writer lock. Claim
or send response loss, interruption or ambiguous output stays fenced. No automatic
retry, expiry, new conversation or takeover after a timeout. Restart does not erase
the request. Another Mac/old host still encounters the central single-consumer claim.
After verified completion/fencing, reconcile the central consumer using its real
Work URL and durable evidence under the existing operator procedure. Do not delete
the local tombstone to resend. A still-running answer is WAITING, not failure.

## Mac setup and acceptance

There is no authenticated Mac execution in the current cloud session. The inspected
cloud browser was logged out of ChatGPT; this is not evidence about the user's Mac.
No Mac install or live cloud Work round-trip has been claimed.

### Installed model qualification

In the intended dedicated Codex home, use the pinned CLI's `codex app-server`
stdio interface: `initialize`, `initialized`, then `model/list` with
`includeHidden=false`. Follow `nextCursor` pagination to completion without
starting a thread or turn. This is model discovery, not an inference or Work send.
Store the complete returned `data`, `nextCursor: null`, a timezone-qualified
`observed_at`, the exact `codex_sha256` and `codex_home` in the protected policy's
`model_catalog`. Set its `source` to `codex app-server model/list`.

The validator requires the explicitly selected model to occur exactly once,
be visible (`hidden=false`), and list the chosen pair's effort (max or xhigh) under
`supportedReasoningEfforts`. Missing catalogs, incomplete pagination, unknown
models/efforts without a qualified approved alternative, or a different CLI digest
or Codex home block before any central claim or local reservation. The client never
chooses `isDefault`, an upgrade suggestion or an unconfigured alternative.
Source examples and another user's model cache
are not installed-environment evidence.

The protected catalog is an operator-recorded observation, not remote attestation
or a fresh availability check on every send. Requalify after CLI, account, Codex
home, provider configuration or model availability changes. A listed model does
not prove subscription billing, browser access or successful inference. Those
remain part of live qualification; do not mark live_acceptance PASS from a listing.
Keep the saved local disabled draft as historical evidence; prepare any revised
policy separately and leave it disabled until its existing gates are satisfied.

Use a dedicated macOS coordinator account and browser/Codex home. Keep personal
files, OneDrive, builder credentials and repository mutation tools out of that
environment. The allowed browser capability must actually be present and qualified;
the prompt and `read-only` sandbox alone do not enforce browser or MCP permissions.
Qualify tool allowlists, account/workspace identity, Work mode/Astra/medium selection,
no cookie export, no User-identity approval impersonation and no paid API fallback.
Do not copy the Grok Bot's personal browser credentials. No anti-bot bypass.

Keep the claim credential in the protected transport process, not Codex/browser
configuration or prompts. Use the existing provisioned consumer credential only
after its transport scope is approved. Child environment strips API/GitHub/claim
credentials. This is not isolation from a hostile process with the same OS identity;
host acceptance must enforce the actual credential boundary.

After those checks, an operator installs the reviewed script, creates owner-only
policy/state/Codex-home paths outside the repository, and fills the disabled example
with the exact CLI digest, request/session binding and live acceptance evidence.
Commands (from the checked-out central repository):

```sh
python3 engineering/scripts/control_plane_codex_relay.py check-config --policy /absolute/private/policy.json
python3 engineering/scripts/control_plane_codex_relay.py deliver --policy /absolute/private/policy.json --request-id <64hex>
```

The protected transport supplies ASTRA_FLOW_ASTRA_CONSUMER_SECRET; never paste it
into chat, a task, the policy or Git. `check-config` is not live preflight. The
default example refuses execution. Source tests use fake Codex/browser and HTTP.
The first diagnostic must prove claim→one Work send→bound GitHub result ingestion,
including duplicate delivery, wrong account/model/Work mode, stale SHA and response
loss. Human observations and evidence must distinguish notifications from verdicts.

No standing routine, periodic page refresh or Slack firehose. Use explicit command
or qualified completion event. If a Work answer is unfinished, return its pointer
and keep the claim. Completion collection transport is still a deployment gate;
do not invent an event callback or keep a Codex reasoning session watching the page.

## Issue 19 diagnostic-only bootstrap (source, disabled until qualified)

User separately authorized the non-product `DIAGNOSTIC` request/result contract.
It fixes Issue #19 / task CP-LOCAL-001, the exact source SHA, revision, consumer,
fresh dedicated Work URL, `engineering/docs/LOCAL_CODEX.md`, and the diagnostic
question. Its deterministic request ID includes those bindings and the explicit
Issue #19 authorization pointer. Only read-document, answer and record-evidence
capabilities exist. It cannot be changed into AUDIT/DECISION, issue a product
PASS or User decision, launch a builder, resume work, merge or activate anything.

This mode does **not** require prior `live_acceptance=PASS`. It requires reviewed
source, explicit per-request User authorization, protected policy and dedicated
authentication instead. The operational policy validator still requires live
PASS. Neither a diagnostic answer nor its collector receipt changes that value.

### Existing authority, not a second Mac ledger

The existing `control_plane_flow_gateway.Ingress` serves two additional routes:

- `POST /astra/diagnostic/claim`: permission for exactly one bound Work send.
- `POST /astra/diagnostic/result`: one authenticated observation to Issue #19.

Only those routes can operate with the global flow `enabled=false`. Every normal
webhook, Slack command and operational claim remains behind the original gate.
An operator calls `control_plane_flow_cli.py prepare-diagnostic --policy <protected-flow-policy>`
explicitly; the consumer credential cannot prepare/register requests or run the
operator's reconciliation command. Preparation uses the **existing** flow ledger,
outbox and `astra_claims` table, never `Store(..., initialize=True)`. Missing ledger
or unmigrated consumer table blocks. The existing operator may initialize the
consumer table once while intake is fenced; no ingress performs schema upgrades.
Do not clone a ledger, create a competing authority or use a GitHub comment lock.
Diagnostic admission checks pending ordinary/diagnostic deliveries, consumer
claims and, when present, graph writer ownership. Existing host admission and
sessions must also be checked by the operator; the event ledger does not replace
the host's admission authority or authorize taking over its jobs.

### Protected configuration and authentication

Keep the existing flow policy, ledger path, source installation checks and
GitHub/Slack transport credentials. Leave all global/lane activation fields
unchanged. Its new `diagnostic` block defaults disabled and requires:

- `source_review=PASS` and `source_review_pointer`: actual independent source
  review, not a fabricated live-acceptance result;
- `request`: the complete output of `diagnostic-request --revision ... --head ...
  --identity ... --session https://chatgpt.com/c/... --designation <Issue19-comment>`;
- `authorization`: `{actor, comment_id, sha256}` pinning an explicitly authorized
  User comment in Issue #19. Its body is `<!-- ASTRA_DIAGNOSTIC_AUTHORIZATION_V1 -->`
  followed by newline and JSON `{active:true, request:<exact request>, source_review_pointer:<pointer>}`;
- `credential_expires_at`: an integer UTC Unix expiry; `slack_channel`: the
  dedicated notification channel, not an audit-approval or auto-consumption trigger.

To resolve the self-pointer, an operator can reserve the authorization comment
first, render the binding with its actual URL, then save the final authorized body
and pin its SHA-256. A placeholder is not authorization. A body/actor/issue edit,
revocation, closed Issue #19 or moved remote main SHA blocks preparation, claim
and collection. Each actual invocation re-reads that GitHub authority.

Provision `ASTRA_FLOW_DIAGNOSTIC_CONSUMER_SECRET` only into the existing protected
gateway and relay transport processes (at least 32 bytes, distinct from the
operational consumer key). It is scoped by protected policy to **one exact request**,
consumer and Work URL and permits only claim/result. Do not place it in policy,
model/browser environment, prompts or GitHub. Authentication uses HMAC-SHA256 of
`path + "\n" + timestamp + "." + raw_json`; the existing timestamp/signature
headers are used. Timestamp age is limited to 300 seconds. Path binding prevents
cross-operation replay. Credential expiry never expires/releases the claim.

The separate owner-only relay policy uses `mode=DIAGNOSTIC`,
`diagnostic_source_review=PASS`, `source_review_pointer`, `diagnostic_request`,
the diagnostic claim URL and exactly one matching `work_sessions` entry. Other
binary, subscription, model/catalog, state-directory and browser qualification
requirements are unchanged. Keep `live_acceptance=PENDING` and the historical
disabled draft unchanged. Both policy flags stay disabled until deployment and
the one-request authorization are actually qualified. This document is not either.

### One-shot execution, collection and uncertainty

Preparation reserves the request under SQLite serialization, projects the exact
action to GitHub and verifies its actor/body/issue, then confirms normal Slack
delivery. Only then can the diagnostic consumer claim. No fake receipts or manual
ledger inserts are a supported preparation path. Relay persists CLAIMING/model
before claim, SUBMITTING before browser send, and RESULT_SUBMITTING before result
submission. The model/browser never receives the transport credential or writes
the GitHub result; the protected gateway publishes it.

An immediate answer is submitted once. An unfinished answer records WAITING.
Only an explicit `collect-diagnostic --policy ... --request-id ...` performs one
read-only inspection of that same conversation with the persisted coordinator
model/effort; it does not claim, send, continue, reload, poll or start a new Work
conversation. Failure/UNKNOWN never chooses another model, request or session.
Actual browser/tool restrictions remain an installation qualification gate; the
prompt and a filesystem sandbox are not browser permission enforcement.

The collector validates request ID, complete subject/revision/SHA, authenticated
consumer, claimed Work URL, current User binding and document evidence pointers.
The only result type is `DIAGNOSTIC_RESULT`, projected as an Issue #19 comment with
marker `<!-- ASTRA_DIAGNOSTIC_RESULT_V1 -->`. The outbox result key is deterministically
derived from the original `diagnostic_request_id`; it cannot become a PR review.
An identical duplicate returns existing state without publishing twice; changed
content is rejected. Provenance is **an authenticated consumer's observation of
the designated conversation**, not provider attestation or cryptographic proof of
model identity. Record visible `GPT-6 Astra` / `medium`; `internal_model_id=null`
means unverified. The receipt explicitly grants nothing and leaves live acceptance
unchanged, even if free-form answer text contains the word PASS.

Lost claim/send/result responses remain UNKNOWN. A process killed before it can
write UNKNOWN leaves its pre-effect marker intact; subsequent diagnostic calls
report UNKNOWN with that `durable_state` and never repeat the side effect. This
also applies to a killed GitHub/Slack publication. No timer, expiry or disabled
credential releases ownership. Confirmed collection also does **not** auto-release
the consumer: the operator must verify the actual session ended and reconcile
using its bound Work URL and durable evidence. Do not fence a still-running or
UNKNOWN consumer. Preserve all tombstones and disable the dedicated diagnostic
flags after completion; Issue #19 stays open until the full acceptance is verified.

### Live prerequisites still required after this source PR

An operator must install the independently reviewed source in the **existing
authority**, pin its hashes and ledger, supply its scoped GitHub/Slack credentials,
provision the expiring diagnostic transport secret on both endpoints, record the
exact authorization binding, qualify the dedicated browser/Codex transport and
create/identify a supported fresh Work conversation. Then perform the single live
diagnostic and verify its GitHub result. No remote SSH address is intrinsically
required. Missing execution principal/credential or a missing original authority
is a deployment blocker, not permission to construct a replacement ledger/server.
Offline tests/source review are not live acceptance or an activation command.

## Host boundary

This Mac is a coordinator client of the existing central ledger. It does not replace
the current Linux host. Existing systemd/sudo/Linux builder adapters are not native
macOS adapters. A later host migration needs its own drain/fence, ledger continuity,
installation and canary evidence; two independent admission ledgers are forbidden.
All four target deployment flags stay true. Global runtime stays disabled/PENDING.
SOULBOUND remains excluded. Existing host settings and installed sessions are untouched.

Official CLI contract: https://learn.chatgpt.com/docs/non-interactive-mode
Official model discovery: https://learn.chatgpt.com/docs/app-server#list-models-modellist
