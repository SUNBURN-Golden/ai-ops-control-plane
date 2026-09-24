# CP-EXTRACT-001 — source extraction and cutover boundary

User decision: [central issue #1](https://github.com/BeautifulMind-JT/ai-ops-control-plane/issues/1).

## Pinned inputs

- Source: `BeautifulMind-JT/kix-protocol@ec3f6db0d613385bfdf2392a4295f0099be1eec6`.
- Source tree: `b328572528dc0c2d52b6df9ace39b376a25bd93e`.
- Destination baseline: `e9386f4b1c30c62d1caf10e6784ba98ee3bb5f66`.
- Migration branch in each participating repository: `ops/cp-extract-001`.

## What moves

Shared control-plane Python/shell implementation and tests, policy examples, workflow source, dispatch/boundary runbooks and engineering governance move into `engineering/`. The import records original paths/Git blobs and destination blobs. Transformed originals are preserved under `engineering/provenance/kix/`. Activation is reset rather than copied as a valid approval.

Product code, product CI, locked files, project-local task template fields, immutable task documents, and historical validation remain with their original repositories. Historical task/validation provenance points to the pinned original commit; history is not fabricated or rewritten. Source-informed tests remain source-informed.

## Required sequence

1. Prepare and inspect this PR and one-shot tool.
2. Import exact source, verify destination blobs remotely, prepare draft consumer/extraction PRs.
3. Review imported code, retained local governance, source manifest and every deletion. Run narrow runtime tests and each affected product's required exact-HEAD CI. Obtain a non-author architecture review. No self-audit PASS.
4. User merge of the disabled source import; no activation implied.
5. Implement and separately review control/target repository identity separation and deployment integration. Existing root-relative Git paths, workflow trust pins, API credentials and KIX-specific routing must be explicitly adapted, not silently reused.
6. Reconcile pending launches and sessions; preserve the old admission/flow ledger and stable request identities. Drain/fence the old dispatcher. Obtain fresh host installation/permissions/workflow attestation, route and event-authentication evidence. Never copy secrets or live databases into Git.
7. User-approved bounded cutover/canary. No paid quota increase, fallback, repeated dispatch, or unattended polling.
8. Only after verified import and legacy dispatch fencing may the User merge a product-side removal that retires an old runtime. Product cleanup does not itself activate a new central runtime.

## Related PR inventory

These are historical references, not renewed authorization. A migration notice is added to the relevant PR discussions; closed/merged history stays closed/merged.

| Repository | Shared-infrastructure PRs |
|---|---|
| kix-protocol | #29, #31, #32, #34, #38, #39, #41, #42, #43, #44, #45, #46, #47, #48, #50, #51 |
| ZARI | #1, #3, #4, #6, #8, #16 |
| film-unit-mv-studio | #4, #5, #6, #9, #10, #11 |
| maeum-gyeol | #3, #4, #5, #8, #9, #10 |
| beautiful-mind / SOULBOUND | #1, #2, #3, #4 — historical notice only; no rollout/source migration |

FILM UNIT #11 is merged. ZARI #16 and 마음결 #10 are closed unmerged. A merged diff cannot be changed retroactively: use a successor PR; preserve old audit and source identities. Unmerged superseded pointers are not reopened.

KIX #50 and #51 must not land new shared operations notes in the product repository. Their original source remains available at its immutable PR head and is indexed in `history/README.md`; it is historical evidence, not current central activation approval. Any errors/stale scope in an original record remain visible and are corrected by an adjacent note, not by falsifying original evidence.

## Protected items

- KIX issues #33/#37/#40 stay open until their actual acceptance criteria are met; migration is not completion.
- KIX #38 stays a do-not-merge historical probe fixture. Do not change its event-bound evidence diff.
- KIX #35 and unrelated product PRs are untouched.
- KIX locked blobs: `runtime/crates/kix-kernel/src/lib.rs` = `69564b166f0c27f9af5d8422f0a466b18d74c20f`; `runtime/crates/kix-kernel/tests/quarantine_capacity.rs` = `b607996c83a119c349f1cc90469ac1ba82764e20`.
- No R2/storage/replication implementation, production bank/chain calls, OneDrive writes, settings/secret changes, force/main pushes, or self-merge.
- FILM UNIT/마음결 source-reference cleanup does not restart the stopped rollout. SOULBOUND remains excluded.

## Non-claims

A destination repository, documentation PR or source copy is not a deployed control plane. The current preparation does not prove imported runtime test success, latest host status, central cross-project execution, ledger migration, independent audit, production readiness, or safe retirement of the current KIX runtime.
