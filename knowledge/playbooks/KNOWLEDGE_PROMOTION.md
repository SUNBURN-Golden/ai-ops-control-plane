# Knowledge Promotion Pipeline

## Pipeline

```
EXPERIENCE
  → OBSERVATION
  → CANDIDATE LEARNING
  → SOURCE CHECK
  → GENERALIZATION CHECK
  → PRIVACY / CASE STRIP
  → SHADOW
  → REVIEW
  → PROMOTION
  → VERSIONED KNOWLEDGE (GitHub control plane or domain playbook)
  → MONITOR
  → ROLLBACK (if harmful)
```

---

## Hard rules

1. **CASE DATA must not auto-promote** to GLOBAL POLICY or DOMAIN KNOWLEDGE.
2. Strip PII, RRN, accounts, party names, and court originals before Candidate → Shadow.
3. Promotion lands in **GitHub** (this repo or project docs) — chat memory is not promotion.
4. Shadow means: use as advisory only; do not change L2/L3 behavior until Review + JunTae OK if gates affected.
5. Quality owner: **품질지식** (with domain specialist input); COO may route.

---

## Privacy / case strip checklist

- [ ] No client names/contacts unless already public and necessary
- [ ] No RRN, bank accounts, full addresses
- [ ] No raw pleadings — cite case id / OneDrive path only
- [ ] Examples are synthetic or heavily redacted

---

## Related

- `SYSTEM_CONSTITUTION.md` hard rule 4
- `SOURCE_OF_TRUTH.md`
- `SECURITY_BOUNDARIES.md`
