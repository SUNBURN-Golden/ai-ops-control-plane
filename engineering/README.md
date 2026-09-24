# Shared engineering control plane

Migration: [CP-EXTRACT-001](https://github.com/BeautifulMind-JT/ai-ops-control-plane/issues/1).

## Scope and current state

This repository, not KIX, is the destination for shared AI engineering governance, dispatch, runtime, boundary helpers and tests. KIX, ZARI, FILM UNIT and 마음결 are product repositories, not infrastructure owners.

**PREPARED_SOURCE_IMPORT_PENDING.** This PR prepares a reproducible extraction and records the new ownership boundary. It does not yet contain the full imported runtime and does not claim completion, independent audit, production cutover, or activation.

Existing root-level Lawhelpers/CRM/knowledge/OneDrive policies remain unchanged. Engineering source belongs under `engineering/`; product code and project-specific contracts remain in their own repositories. No customer data, secrets or runtime databases are imported.

## One-time source import

Use an already authenticated development host with Python 3.10+ and GitHub CLI (`gh`). No model/builder session or new paid service is needed.

```sh
gh auth status
gh repo clone BeautifulMind-JT/ai-ops-control-plane ai-ops-control-plane-migration -- --branch ops/cp-extract-001
cd ai-ops-control-plane-migration
python3 -m unittest discover -s tests -v
python3 tools/extract_control_plane.py
python3 tools/extract_control_plane.py --apply
```

The default command only plans. `--apply` imports the pinned source into this migration branch, verifies every remote destination blob, then creates/updates draft extraction PRs for the four product repositories. It never merges, modifies a default branch, enables a runtime, attaches a runner, invokes a builder, changes credentials/settings, moves a live ledger, or retries a failed API write. It stops if pinned KIX main moved or an existing migration lineage is closed/review-ready/unrecognized.

Run this once; do not schedule it. A failed or uncertain write requires inspection, not automatic replay. Do not put access tokens in chat or source. Use existing local `gh` authentication with access to the five relevant private repositories.

## Verification boundary

The preparation script has author-side synthetic unit tests. Those are not the imported runtime's tests, not integration proof, and not a non-author review. Required exact-HEAD CI and independent review remain open. No old KIX audit/activation result transfers to a new repository.

## Source versus deployment

Source import and deployment are separate. The imported activation is reset to NOT_APPROVED/PENDING/false. Nested `engineering/.github/workflows/` files are source, not installed GitHub workflows. Existing KIX-oriented fixtures and root-relative Git assumptions are preserved as provenance; the import alone is not a working cross-repository dispatcher.

A separate implementation/cutover gate must distinguish control-repository/workflow identity from target-repository/task identity, preserve task/owner/request and ledger continuity, fence/drain old dispatch before retiring it, verify runner permissions and fresh exact-source attestation, and obtain independent review plus User authorization for a bounded canary. Never operate two live dispatchers for the same task.

Read [MIGRATION.md](MIGRATION.md) before reviewing or merging. The author cannot self-approve this architecture change.
