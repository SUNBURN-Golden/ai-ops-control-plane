# Source of Truth Map

Chat is not done. Bot memory is not the ledger. Pick the SoT for the domain, then write there (except OneDrive — read only).

---

## Map

| Domain | Source of Truth | Notes |
|--------|-----------------|-------|
| Code, bot policy, playbooks, workflows, registry | **GitHub** (this control plane + project repos) | PR/merge = change of record; chat proposals are candidates only |
| Client / case / deadlines / fees / comms state | **CRM/CASE** ledger (Excel → future DB) | Not bot memory; update ledger when facts change |
| Original legal documents (pleadings, evidence scans) | **OneDrive** / Desktop originals | **READ ONLY** on OneDrive — never mutate; copies for analysis stay outside OneDrive |
| Ephemeral working notes | Bot memory / chat | **Auxiliary only** — never sole record of mutable facts |

---

## Pointers (do not duplicate PII here)

- Case facts → CRM/CASE id / spreadsheet row (reference by case id, not full dossier in GitHub)
- Document originals → OneDrive path (document the path; do not commit file contents)
- Policy/code → commit SHA on `main`

---

## Conflict rule

If chat, bot memory, and SoT disagree: **SoT wins**. Correct the SoT with approval if needed; do not “fix” by editing memory alone.

---

## Related

- `ONEDRIVE_READ_ONLY_POLICY.md`
- `SYSTEM_CONSTITUTION.md` § Hard rules 2, 6, 7
- `bots/registry.yaml` `sot:` fields per bot
