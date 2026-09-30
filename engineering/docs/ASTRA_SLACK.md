# Direct Slack requests to Astra

> **User decision M5 (2026-09-30):** the Astra role is held by Claude Fable, run on the
> host through `aiops-fable` (`docs/CONTROL_PLANE_RUNTIME.md`, Astra on the host). The
> ChatGPT receiver below is retired and must not be configured. The sender remains a
> notification path only; a Slack message never starts or replaces an `aiops-fable` run.

Slack carries requests and notifications. GitHub records the authoritative task,
decision, exact SHA and audit. Grok Bot is not required to ask Astra or relay its
answer. No shared browser session, raw Slack firehose or standing LLM routine.

## Implemented sender

1. Mechanical flow reads current GitHub gates. Ordinary A1/A2 review does not wake
   Astra; the existing architecture/decision/release/milestone rules still apply.
2. Reserve the exact action in the durable outbox. Project it to the canonical
   GitHub issue and confirm the receipt before Slack delivery.
3. Verify that exact comment, its protected `projection_actor`, issue and complete
   action body. Send one fields-only request to the protected audit/decision channel:
   subject (repo/task/revision/HEAD/base/policy/task digest), request/attempt,
   designated identity/scope, task/PR and durable control-record pointers.
4. Slack must return `ok`, matching channel and message timestamp. Response loss or
   ambiguous delivery leaves UNKNOWN; do not resend or launch another auditor.
   A confirmed delivery only proves notification delivery, not that Astra processed it.

Protected flow policy fixes `control_repository`, `runtime_workflow`,
`runtime_workflow_ref`, `projection_actor` and channel IDs. The runtime dispatch
endpoint is central; `target_repository` remains the product task's repository.
Missing mappings fail closed. The disabled example is not a configured installation.

## Receiver setup and remaining gate

The normal ChatGPT-in-Slack private sidebar is not a shared autonomous callback.
Use an actually available Workspace Agent or Work Slack-event automation only after
checking this workspace/account's support and exact connected Astra identity.

For the available Work Slack event shape, configure **request-only channel IDs**,
the exact **mechanical sender ID**, and `include_thread_replies=false`. Include the
ChatGPT app in those channels. Its current trigger has no content-prefix filter;
do not subscribe it to general project chatter. Route results through a different
sender or thread, never back into the request trigger. Prefixes/metadata are routing
data, not authentication. An unrelated `[AUDIT_RESULT]` text cannot change a gate.

The gateway implements `POST /astra/claim`, backed by the protected flow ledger.
Before expensive analysis, the connected receiver's narrow transport must authenticate
and claim the request. Only a response with `start_allowed=true` starts analysis.
Duplicate delivery, even with another session ID, returns false. A lost claim response
may stall the task; it never justifies restarting. This is at-most-once start permission,
not an exactly-once completion promise.

Initialize the added table once with `control_plane_flow_cli.py init-consumer-ledger
--ledger <protected-flow-ledger>` while intake is fenced. There is no automatic schema
upgrade on ingress. Enable only after provisioning a dedicated transport credential
`ASTRA_FLOW_ASTRA_CONSUMER_SECRET` (32 bytes minimum) and protected `astra_consumer`
identity mapping. Never put that credential in Slack, model prompts or repository files.
The receiver transport signs raw JSON `{request_id,session_id}` using HMAC-SHA256 over
`timestamp + "." + raw_body`; headers are `X-Astra-Timestamp` and `X-Astra-Signature`.
It must be a supported narrow connector/transport callable by the actual Astra account.
An event trigger alone does not provide this capability or install a receiver.

The gateway verifies confirmed sender/projection rows, the exact remote GitHub projection,
current task/revision/HEAD/designation and protected recipient identity before atomic
claim. Only one unresolved consumer per task is admitted. `CLAIMED` survives crashes;
no lease timer releases it. After the actual session stops (including successful analysis),
an operator records its durable evidence with `reconcile-consumer --request-id ...
--actor ... --session ... --evidence <GitHub-pointer> --consumer-fenced`. This retires
the consumption slot; it grants no audit PASS or decision authority. Do not fence an
active analyst just to start a replacement. Until the receiver transport and end-to-end
test exist, notification mode remains usable and a designated human starts Astra from
the single posted GitHub pointer without Grok mediation.

The auditor reads GitHub directly and verifies current subject, designation and
non-authorship. It emits findings/result against the exact SHA. The existing collector
accepts only authenticated native GitHub reviews with its exact result binding;
Slack text or a connector that cannot publish that review cannot satisfy the gate.
Keep results pending until the designated identity can publish the verified record.
Decision analysis goes to Astra; consequential approval remains the User's explicit
action and durable GitHub record. Resume the same owner only after the updated task
revision/contract is recorded.

## Activation evidence

- Resolved request channel(s), mechanical sender and connected Astra identity.
- Consumer serialized claim/dedupe and bot/self-event exclusion verified.
- Exact-head audit request delivered once; duplicate/stale delivery creates no audit.
- GitHub result ingestion verified; Slack PASS alone rejected.
- Ordinary A1/A2, matrix CI jobs, status replies and chatter do not wake Astra/Grok.
- Lost send/receive response reconciles without blind resend or credential leakage.

No channel IDs, receiver capability or completed audit are inferred. Current source
tests use mocked endpoints; live Slack/account/host activation remains a separate gate.

Official capability references:
- https://help.openai.com/en/articles/12462158-using-chatgpt-in-slack
- https://help.openai.com/en/articles/20001199-chatgpt-agents-app-in-slack
- https://help.openai.com/en/articles/12525822-using-slack-in-chatgpt
