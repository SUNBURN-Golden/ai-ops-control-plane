# Phase 0 Inventory — 2026-09-17 (Asia/Seoul context; box clock UTC)

## Existing Grok Bots (live)
| Name | Role today | Maps to target org |
|------|------------|-------------------|
| Bot (this) | Orchestrator | COO / Chief of Staff |
| 강제집행 | Enforcement specialty | LEGAL → Enforcement leaf (to expand to LEGAL Director later) |
| CS | Kakao/CS inbox | CUSTOMER OPS (merge candidate with CRM) |
| CRM | Case/fee/settlement xlsx | CUSTOMER OPS (merge candidate with CS) |
| 마케팅 | Marketing | MARKETING Director |
| 개발 | BeautifulMind-JT / GitHub | DEV Director |

No custom sidebar sections yet.
Agent folders on box: 강제집행, CS, CRM, 마케팅, 개발, Bot.

## Existing routines
- CS 폴더 새 서류 확인 — weekdays 09:30 & 14:30 KST; Desktop\CS watched folders

## Connected systems
- Gmail MCP (user-Gmail) — connected
- Lawhelpers2 machine — registered; **disconnected at inventory time**
- GitHub via `gh` on box — **not logged in** (개발 bot previously used PAT in its sessions; do not invent credentials)
- ChatGPT box browser — logged in as justice.parkit@gmail.com; **use only on JunTae explicit OK**
- OneDrive (법무법인형원) — on Lawhelpers2; **READ ONLY by master command**

## Standing rules to PRESERVE (not replaced)
- JunTae explicit permission before specialist actions (incl. research/drafts/sends)
- Bot proposes routing; wait for OK before fan-out execution
- ChatGPT: no use without explicit OK
- No per-case permanent bots (master + prior agreement)
- Learning: Observation → Candidate → review → Promotion (not auto memory)
- OneDrive absolute READ ONLY (new master rule)

## Known GitHub / projects (from prior memory; verify when gh available)
BeautifulMind-JT orbit (names as remembered; re-verify):
- kix-protocol
- maeum-gyeol / 마음결
- film-unit-mv-studio (MV Compiler)
- beautiful-mind (dormant note)
- SOULBOUND / ZARI — referenced in master plan; confirm existence before Maintainer bots

## Data stores (Source of Truth candidates)
- GitHub: code / PR / CI
- Desktop Excel + OneDrive case folders: case/CRM facts (OneDrive read-only)
- Desktop\CS: Kakao exports inbox
- Bot memory: auxiliary only

## Not yet connected
- ai-ops-control-plane GitHub repo (draft only on COO box `/workspace/ai-ops-control-plane`)
- CRM/CASE structured DB beyond Excel
- Unified dashboard
- Quality & Knowledge bot
- OPS bot
- Domain leaf bots (Civil, Criminal, Garnishment, etc.)

## Risks
- SuperGrok Grok Bot usage was ~76% with reset ~Sep 22 — mass bot creation burns quota
- Lawhelpers2 offline blocks live Desktop/OneDrive reads until reconnected
- Shared box browser/logins are not security boundaries between bots
