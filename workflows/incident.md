# Incident Workflow

## Immediate

1. **Stop** external mutation (sends, submits, deploys, ledger writes)
2. Capture **symptom + evidence** (screenshots, paths, SHAs — redact PII)
3. Notify **대장Bot** and **품질지식**
4. Record under `audits/` (or `audits/failures/` when that folder is used)
5. Set task to **BLOCKED** if work must pause

## After stabilize

6. Root cause note (process vs tooling vs SoT mismatch)
7. **Candidate learning** only — do not auto-promote to global policy
8. Follow `knowledge/playbooks/KNOWLEDGE_PROMOTION.md` if a lasting rule is warranted
9. Close incident when verified fix + SoT updated

## Never

- Hide incidents in bot memory only
- “Fix” by writing to OneDrive
- Bypass L2/L3 gates “because emergency” without JunTae OK
