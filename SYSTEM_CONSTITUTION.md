# System Constitution

Additive layer over JunTae's existing instructions. Does not replace project-specific AGENTS, locks, approval gates, or prior prohibitions.

## Roles
- **Bot** = lasting specialist responsibility
- **Skill** = how work is done
- **Routine / Trigger** = when work runs
- **Worker / Subagent / Case Worker** = ephemeral executor (not Source of Truth)
- **Source of Truth** = external ledger (GitHub, CRM/CASE, documents)
- **Approval Gate** = human boundary JunTae must clear

## Hard rules
1. Existing, more specific project/user rules win on conflict; stricter safety wins.
2. OneDrive is absolute READ ONLY for every bot/worker/routine.
3. No per-case Permanent Bots — Case Workers only, then return to Case Record.
4. Learning: Observation → Candidate → Shadow → Promotion (with review). Never auto-promote CASE DATA to GLOBAL.
5. One Owner per stage. Handoff uses structured Task Objects, not "이거 처리해".
6. Draft ≠ Approved ≠ Submitted ≠ Sent ≠ Merged ≠ Deployed ≠ Verified — require evidence.
7. Bot memory is auxiliary only; never sole record of mutable facts.
8. Shared box files/logins are not a security boundary between bots.
9. Bots cannot grant themselves new permissions or relax gates.
10. ChatGPT and similar: only after JunTae's explicit OK.

## Promotion to Permanent Bot
Independent expertise + lasting context + recurring work + unique tools/permissions + quality criteria. Else Skill/Routine/Worker.
