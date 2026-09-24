# Historical KIX operating records

These are immutable source pointers, not current central state. The original PR texts and audits remain in KIX to preserve provenance; their future operational ownership belongs to `ai-ops-control-plane`.

| Record | Original PR | Exact source | Git blob |
|---|---|---|---|
| E4 production bring-up | [KIX #50](https://github.com/BeautifulMind-JT/kix-protocol/pull/50) | [original](https://github.com/BeautifulMind-JT/kix-protocol/blob/eaab0cfd13f064c5fb73297a7c0d1a4462fec4d9/docs/status/CONTROL_PLANE_PRODUCTION_BRINGUP_E4.md) | `5fde6fae37ede27c4dd70f1cdbe6b402fb0d17c7` |
| GLM readiness, NOT enable | [KIX #51](https://github.com/BeautifulMind-JT/kix-protocol/pull/51) | [original](https://github.com/BeautifulMind-JT/kix-protocol/blob/c9909e6be128cc34913e1bc3219cb2e9d1544a4c/docs/status/GLM_READINESS_EVIDENCE_BUNDLE.md) | `67d290ae4e29effd6a4f0ae3e1ee2ce64e050f4a` |

## Corrections / boundaries recorded 2026-09-24

- E4 describes a KIX-hosted historical deployment, not the current deployment of this central repository.
- The E2 pointer PR outcomes differ: FILM UNIT #11 merged; ZARI #16 and 마음결 #10 closed without merge. A later rollout stop is not evidence that a previously merged PR was unmerged.
- SOULBOUND is represented by the accessible `BeautifulMind-JT/beautiful-mind` repository; the old E4 'missing' row is not a verified claim about that repository. Its rollout remains excluded.
- GLM readiness and another builder's successful canary do not establish a GLM production launch. No enablement or prior audit transfers to a central import.
- Historical host paths are provenance, not an instruction to access them, move a live database, copy credentials or change an endpoint.

Future corrections and residual acceptance belong to [CP-EXTRACT-001](https://github.com/BeautifulMind-JT/ai-ops-control-plane/issues/1). Original source is retained rather than rewritten to look current.
