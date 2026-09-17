# Registry Cross-Audit — 2026-09-17 (Asia/Seoul)

**Authority:** Live Grok Bot teammates list from JunTae (COO + LEGAL + CUSTOMER + MARKETING + DEV + OPS/QA).  
**Repo file:** `bots/registry.yaml`  
**Action taken:** Set `status: ACTIVE` for every live bot; added `last_audited: "2026-09-17"`; documented HQ `channels:`.

---

## Coverage check

| Bucket | Live IDs / names | In registry? | Status after audit |
|--------|------------------|--------------|--------------------|
| COO | 대장Bot `c43890b3-75b4-4b8a-853a-dca64ed77840` | Yes | ACTIVE |
| LEGAL | 법무총괄 3615903, 강제집행 3105696, 민사 3615959, 본안소송 3615963, 보전처분 3615965, 형사 3615967, 채권압류 3615970, 유체동산 3615973, 부동산집행 3615976 | Yes (9) | ACTIVE |
| CUSTOMER | 고객응대 3615911, CS 3105707, CRM 3105708 | Yes (3) | ACTIVE |
| MARKETING | 마케팅총괄 3105711, 블로그 3615980, 검색마케팅 3615979, 한국웹마케팅 3616019, 영문웹마케팅 3616020, 애널리틱스 3616022, 브랜드AIEO 3616024 | Yes (7) | ACTIVE |
| DEV | 개발총괄 3105712, KIX 3616030, SOULBOUND 3616033, ZARI 3616034, 마음결 3616038, MVCompiler 3616076 | Yes (6) | ACTIVE |
| OPS/QA | 운영 3615917, 품질지식 3615918 | Yes (2) | ACTIVE |

- **Missing live bots:** none  
- **Phantom bots (in registry, not in live list):** none  
- **Total bots:** 28 ACTIVE  

---

## Channels (not bots)

Documented under `channels:` in registry:

- 법무본부  
- 고객본부  
- 마케팅본부  
- 개발본부  

---

## Audit tags retained

| Tag | Bots | Rationale |
|-----|------|-----------|
| 통합후보 | 고객응대, CS, CRM | Overlapping customer lead surface — keep until JunTae picks merge shape |
| 통합후보 | 강제집행 | Overlaps 법무총괄 leaf routing |
| 통합후보 | 운영 | Overlaps COO reporting routines |
| 수정필요 | SOULBOUND | Repo under `soulbounddao-ADMIN` (other org); confirm access |
| 정상 | All others | Present and ID-matched |

---

## Prior status note

Many entries were `CREATED_NOT_VALIDATED`. Per JunTae’s live-list authority for this audit, they are now **ACTIVE**. Validation of *behavior* remains via smoke A/B/C (routing-only until OK).

---

## Related

- `audits/2026-09-17-CONTROL-PLANE.md`  
- `docs/SMOKE_TESTS.md`  
