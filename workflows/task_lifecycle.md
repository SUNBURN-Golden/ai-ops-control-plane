# Task Lifecycle

## States

```
NEW → TRIAGED → ASSIGNED → IN_PROGRESS → READY_FOR_REVIEW
  → APPROVED → EXECUTION_PENDING → EXECUTED → VERIFIED → CLOSED
```

Optional side state: **BLOCKED** (reason + owner + unblock condition required).

---

## Rules

1. **One Owner per stage** (+ optional Reviewer/Advisor). See `handoff.md`.
2. Advance only with evidence — never equate labels without proof (`APPROVAL_MATRIX.md`).
3. L2/L3 actions stay in `APPROVED` / `EXECUTION_PENDING` until JunTae OK is recorded.
4. Routing (L0) may reach `TRIAGED` with a proposed specialist; fan-out execution waits for OK when standing rule requires it.
5. On failure or unsafe surprise → `incident.md`, then optionally `BLOCKED`.

---

## Evidence checklist (minimum)

| Transition | Evidence |
|------------|----------|
| → ASSIGNED | Handoff packet (task id, purpose, SoT refs) |
| → READY_FOR_REVIEW | Draft/artifact path |
| → APPROVED | JunTae OK or allowed L0/L1 standing clearance |
| → EXECUTED | Receipt (send id, filing #, commit SHA, etc.) |
| → VERIFIED | Checker note |
| → CLOSED | SoT updated (CRM/CASE or GitHub), not only chat |

---

## Schema

Structured tasks: `schemas/task.schema.json`.
