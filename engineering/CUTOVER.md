# Separate runtime cutover gate — NOT AUTHORIZED

Source acceptance and product cleanup PRs do not authorize operational transfer.

1. Accept exact central source SHA, non-author audit, offline CI and explicit User source merge.
2. Inventory active owners, requests, attempts and unresolved SUBMITTING/UNKNOWN entries on existing host.
3. Under separate User authorization stop intake; drain and fence old senders/dispatchers.
   Never release UNKNOWN on timeout alone. Reconcile with durable provider evidence.
4. Back up protected ledgers outside Git. Preserve TASK_KEY, request/attempt IDs, session ownership
   and replay records. Prove one canonical ledger owner before allowing any new sender.
5. Prepare separately reviewed root central dispatch workflow and least-privilege target credentials.
   Attest control repository/workflow SHA separately from target repository/task/PR HEAD/revision.
   Verify host paths, shell/Python digests, runner audience, Slack signatures and routing allowlists.
6. Install disabled; run exact-install verification with fresh non-author evidence. KIX evidence does not transfer.
7. User separately approves one bounded canary; maintain one writer and one active dispatcher.
   FILM/MAEUM rollout remains stopped; SOULBOUND remains excluded.
8. Retire old product execution workflows only after source acceptance and confirmed drain/fence.
9. Rollback: fence the new sender first; reconcile its ambiguous requests; restore compatible code
   and ledger state without losing accepted request IDs; only then authorize the old sender again.
   Never blindly replay a launch or run both dispatchers.

No live host verification, new production workflow, credential migration or canary was performed here.
