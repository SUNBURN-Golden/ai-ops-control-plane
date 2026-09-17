# Approval Matrix — JunTae “ask first” standing rule

Additive over project-specific gates. **Stricter safety wins.** Bots cannot grant themselves new permissions or relax this matrix.

When in doubt: treat as the **higher** level and wait for JunTae OK.

---

## Levels (explicit)

| Level | Name | What it covers | Default | Evidence required |
|-------|------|----------------|---------|-------------------|
| **L0** | Read / route | Search, inventory, status, OneDrive **read**, CRM/CASE **read**, propose routing plan, triage notes that do not mutate external systems | **Allowed** without per-action OK | Link/path to source consulted; routing proposal text; “no side effect” statement |
| **L1** | Draft / internal prep | Drafts, checklists, local `/workspace` docs, internal checklists, PR drafts **not merged**, candidate learning notes | **Propose**; obey JunTae standing “ask first” if he requires OK even for drafts | Draft artifact path or diff; sources used; unknowns list; **not** marked Approved/Sent/Submitted |
| **L2** | External message | Customer/agency/court-clerk email or chat send, public social post, outbound call script delivery | **Explicit JunTae OK** before send | Draft body + recipients (redacted in repo); channel; OK timestamp/quote from JunTae; send receipt after |
| **L3** | Legal submit / finance / prod | Court/e-filing submit, fee/settlement mutation, ad spend, secrets use, prod deploy, gated `main` merge, permission changes | **Explicit JunTae OK** | Exact action plan; SoT target; rollback; OK quote; post-action evidence (filing #, deploy SHA, ledger row id) |

### OneDrive write

**Never allowed** at L0–L3. If work seems to need OneDrive write: stop, report to JunTae, do not invent exceptions. See `ONEDRIVE_READ_ONLY_POLICY.md`.

---

## Evidence rules (Draft ≠ Done)

Never equate without evidence:

| Claim | Minimum evidence |
|-------|------------------|
| Draft completed | File/path or message draft id |
| Approved | JunTae OK quote or recorded gate clearance |
| Submitted (legal) | Filing receipt / case number / portal confirmation |
| Message sent | Provider message id / thread link |
| Merged | Commit SHA on target branch |
| Deployed | Deploy id / prod SHA / health check |
| Verified | Explicit verification note + checker |

---

## Routing vs side effects

- **Routing-only** (L0): propose specialist + handoff packet; **stop** until JunTae OK before specialist executes side effects.
- Smoke tests A/B/C in `docs/SMOKE_TESTS.md` are routing-only until OK.

---

## Cross-links

- Constitution: `SYSTEM_CONSTITUTION.md`
- Operational twin: `workflows/approval.md`
- Handoff packet fields: `workflows/handoff.md`
