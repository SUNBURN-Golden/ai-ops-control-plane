## Owner decision D-2026-10-07-SUNBURN-MAEUM-GYEOL-CONTROL-REPO

Decision owner: JunTae Park (repository owner), 2026-10-07 11:05 KST.
Standing direction: prefer the least restrictive option the auditor offers ("최대한 느슨하게").

In response to the GLM-5.3 DESIGN A3 audit on PR #83 head `7e8bb71f1ea2581fcb493ce7d990534980436c8b` (issuecomment-6029179194, DECISION_REQUIRED, finding GLM53-N1), I approve option **A**.

1. **maeum-gyeol control repository (GLM53-N1):** update the `BeautifulMind-JT/maeum-gyeol` central profile's `control_repository` from `BeautifulMind-JT/ai-ops-control-plane` to `SUNBURN-Golden/ai-ops-control-plane`, the same control repository (repo ID 1373567344) at its new address. The SUNBURN-Golden control plane becomes the validating authority for maeum-gyeol as well. maeum-gyeol itself stays at `BeautifulMind-JT/maeum-gyeol` and is not transferred by this decision. The single control repository invariant in `control_plane.py` is unchanged.
2. **Historical task pointers (GLM53-N4):** for tasks in the approved transferred repos, a historical envelope that pins the old `BeautifulMind-JT/<repo>` URL/REPO may be accepted when the live object has the same issue/PR number and the same repo ID under the new `SUNBURN-Golden/<repo>` name. Original envelope text is not rewritten. This is what lets the existing KIX job `20531604498943e1` resume after the transfer.

Notes GLM53-N2 (update the KIX post-merge pin to the new full name, with a regression test) and GLM53-N3 (check repo ID and owner ID at the transfer seam) are accepted as implementation fixes, not decisions.

Accepted risk: the new org's control plane validates a repo that stays in the personal account, and a historical pointer is matched by repo ID plus number instead of exact URL text. The author records this comment's id and body SHA-256 in the migration decision record, fixes `test_governance_registry` and N2/N3, pushes a new head, lets CI judge, reruns the GLM-5.3 A3 audit, and merges head-pinned on PASS/PASS_WITH_NOTES.
