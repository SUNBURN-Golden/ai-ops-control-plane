# macOS Codex coordinator / cloud Work Astra mid

CP-LOCAL-001: https://github.com/BeautifulMind-JT/ai-ops-control-plane/issues/19
User authorized this design on 2026-09-27. Source candidate, not an installed Mac.

## Roles and cost

Mac Codex relays explicit commands, collects pointers and operates the qualified
cloud Work browser. It is not the builder, architect, independent auditor or User.
Default coordinator model: gpt-6-sol / low; explicitly configured gpt-5.6-sol is
an alternative, never an automatic fallback. Verify actual account/CLI support.
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
write is a single authenticated POST to the existing `/astra/claim`; a qualified
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

## Host boundary

This Mac is a coordinator client of the existing central ledger. It does not replace
the current Linux host. Existing systemd/sudo/Linux builder adapters are not native
macOS adapters. A later host migration needs its own drain/fence, ledger continuity,
installation and canary evidence; two independent admission ledgers are forbidden.
All four target deployment flags stay true. Global runtime stays disabled/PENDING.
SOULBOUND remains excluded. Existing host settings and installed sessions are untouched.

Official CLI contract: https://learn.chatgpt.com/docs/non-interactive-mode
