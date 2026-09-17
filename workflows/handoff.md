# Handoff Protocol

When Bot A hands work to Bot B, send a **structured packet** — never bare “이거 처리해”.

## Required fields

1. **Task ID**
2. **Purpose** (one sentence)
3. **Facts confirmed + sources** (SoT pointers: GitHub SHA, CRM/CASE id, OneDrive path — no PII dumps)
4. **Unknowns**
5. **Done so far**
6. **Needed next**
7. **Deadline** (Asia/Seoul)
8. **Approval requirement** (L0–L3 + whether JunTae OK already obtained)
9. **Return destination** (who gets the result)

## Ownership

- **One Owner** per stage
- Optional Reviewer / Advisor (non-owner)
- COO (대장Bot) routes; specialists own domain execution after OK

## Routing-only handoffs

Smoke A/B/C and similar audits may hand off a **proposed** packet and wait for JunTae OK before any specialist side effect. See `docs/SMOKE_TESTS.md`.
