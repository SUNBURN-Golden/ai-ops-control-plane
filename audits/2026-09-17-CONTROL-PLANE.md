# Control Plane Audit Summary — 2026-09-17 (Asia/Seoul)

**Repo:** https://github.com/BeautifulMind-JT/ai-ops-control-plane  
**Scope:** Governance polish, registry ACTIVE sync, smoke-test docs, no OneDrive touch, no new agents, no bot messaging.

---

## What is live on GitHub

Control plane on `main` includes:

- Constitution, SoT map, approval matrix (L0–L3 + evidence), security boundaries, OneDrive READ ONLY policy  
- `bots/registry.yaml` — 28 live bots + 4 HQ channels  
- Workflows: task lifecycle, handoff, approval, incident  
- Knowledge promotion playbook  
- Schemas / routines / skills stubs  
- Docs: smoke A/B/C, inventories  
- This audits folder  

**Not in repo (by design):** client PII, RRN, bank accounts, court originals, secrets.

---

## Registry counts

| Status | Count |
|--------|------:|
| ACTIVE | 28 |
| CREATED_NOT_VALIDATED | 0 |
| PLANNED / DISABLED / DEPRECATED | 0 |
| **Total bots** | **28** |
| HQ channels (not bots) | 4 |

### Audit tag counts

| audit | Count |
|-------|------:|
| 정상 | 22 |
| 통합후보 | 4 (고객응대, CS, CRM, 강제집행) + 운영 = **5** |
| 수정필요 | 1 (SOULBOUND) |
| 오류 | 0 |

---

## Open risks

1. **통합후보 — Customer triad** (고객응대 / CS / CRM): overlapping ownership until JunTae picks merge shape.  
2. **통합후보 — 강제집행 vs 법무총괄 leaf routing:** keep both until consolidation decision.  
3. **통합후보 — 운영 vs COO routines:** reporting overlap.  
4. **SOULBOUND org** (`soulbounddao-ADMIN`): access/ownership confirmation needed (`수정필요`).  
5. **ChatGPT ban:** shared-box browser may be logged in; use only after JunTae explicit OK (`SECURITY_BOUNDARIES.md`).  
6. **Approval gate:** JunTae “ask first” — L2/L3 always; smoke tests must not fan out side effects without OK.  
7. **Shared box:** files/logins are not isolation between bots — process discipline only.

---

## Next — Smoke A / B / C (routing-only)

See `docs/SMOKE_TESTS.md`.

| Smoke | Route | Stop condition |
|-------|-------|----------------|
| **A** Legal | 대장Bot → 법무총괄 → leaf (민사/강제집행/…) | Wait JunTae OK before specialist side effect |
| **B** Customer | 대장Bot → 고객응대 → CS/CRM | Wait JunTae OK; note 통합후보 |
| **C** Dev | 대장Bot → 개발총괄 → leaf (KIX/ZARI/…) | Wait JunTae OK; flag SOULBOUND if chosen |

Do not treat routing pass as permission to send, submit, merge, or deploy.

---

## Change set (this polish)

- Expanded README, APPROVAL_MATRIX, SOURCE_OF_TRUTH, SECURITY_BOUNDARIES  
- Coherent workflows + KNOWLEDGE_PROMOTION  
- Registry ACTIVE sync + channels + last_audited  
- `docs/SMOKE_TESTS.md`  
- `audits/2026-09-17-registry-audit.md` + this file  
