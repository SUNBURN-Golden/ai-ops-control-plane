# Smoke Tests A / B / C — routing only

**Purpose:** Verify COO → HQ director → leaf routing paths without executing specialist side effects.

**Standing rule:** These tests **must wait for JunTae OK** before any specialist takes an action that drafts-to-send, mutates CRM/CASE, opens PRs to merge, or otherwise leaves L0.

Until OK: produce only a **routing proposal + handoff packet** (see `workflows/handoff.md`), then stop.

---

## Common preconditions

- Registry: `bots/registry.yaml` (ACTIVE bots)
- Approval: L0 for read/route; anything beyond = wait
- No PII in packets committed to GitHub
- OneDrive: read only if needed for existence checks — no writes
- Do **not** message other bots to “start work” beyond the agreed routing probe unless JunTae OK

---

## Smoke A — Legal route

| Step | Actor | Action | Side effect? |
|------|-------|--------|--------------|
| A1 | 대장Bot | Classify sample as LEGAL; propose 법무총괄 | No |
| A2 | 법무총괄 (proposed) | Propose leaf (e.g. 민사 → 본안소송 **or** 강제집행 → 채권압류) | No |
| A3 | — | Package handoff; **STOP for JunTae OK** | No |
| A4 | Specialist | Only after OK: L1 draft or further work per matrix | Yes — gated |

**Pass criteria (routing-only):** Correct HQ + leaf named; approval_level noted; no send/submit.

---

## Smoke B — Customer route

| Step | Actor | Action | Side effect? |
|------|-------|--------|--------------|
| B1 | 대장Bot | Classify sample as CUSTOMER; propose 고객응대 | No |
| B2 | 고객응대 (proposed) | Propose CS and/or CRM (note 통합후보 overlap) | No |
| B3 | — | Package handoff; **STOP for JunTae OK** | No |
| B4 | Specialist | Only after OK: L1 draft or L2 message prep per matrix | Yes — gated |

**Pass criteria:** Route names 고객응대 + CS/CRM; no outbound message.

---

## Smoke C — Dev route

| Step | Actor | Action | Side effect? |
|------|-------|--------|--------------|
| C1 | 대장Bot | Classify sample as DEV; propose 개발총괄 | No |
| C2 | 개발총괄 (proposed) | Propose leaf (KIX / ZARI / 마음결 / MVCompiler / SOULBOUND) | No |
| C3 | — | Package handoff; flag SOULBOUND `수정필요` (other org) if selected; **STOP for JunTae OK** | No |
| C4 | Specialist | Only after OK: L1 code/docs work; L3 for prod deploy | Yes — gated |

**Pass criteria:** Correct maintainer bot; repo URL from registry; no merge/deploy.

---

## Recording results

Log outcomes under `audits/` (date-stamped). Mark `ROUTING_PASS` vs `BLOCKED_WAITING_OK` vs `FAIL`.
