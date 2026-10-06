## Owner decision D-2026-10-06-MAC-A3-RECEIPT-2

Decision owner: JunTae Park (repository owner), 2026-10-06 20:25 KST.
Amends: D-2026-10-06-MAC-A3-RECEIPT (PR #78, issuecomment-6011271646), Mac host only. Linux gate rules are unchanged.

In response to the A3 audit on PR #80 head `4b735b596c3b660ef2fddc6539cf66d6ecfcb820` (issuecomment-6014644349, result DECISION_REQUIRED), I approve option **A**: all three relaxations in PR #80.

1. **Ordering slack (F1):** the Mac receipt ordering check (request recorded → audit run started → comment posted) may allow up to 30 seconds of clock skew. An audit run that started up to 30 seconds before the request was recorded may be accepted. This supersedes the earlier "no new ordering policy" wording for the Mac host.
2. **Other gate / lower depth results (F2):** for the same head, audit results on a different gate, or at a lower depth than the required one (including FAIL, DECISION_REQUIRED, or contract change YES), are ignored when evaluating the required gate/depth receipt. Only results at the required gate and depth conflict. This amends item 3 ("reject conflicting results for the same head") accordingly.
3. **Legacy hold migration (F3):** the receipt database migration may move existing UNVERIFIED holds, CHANGED holds stored without evidence, PASS vs PASS_WITH_NOTES conflict holds, and lower-depth holds into historical records so they no longer block. This migration is itself the explicit user decision required to release them.

Accepted risk: a lower-depth or other-gate negative audit on the same head will not block the Mac A3 receipt, and a skewed Mac clock within 30 seconds can admit a slightly early audit. The author must record this comment's id and body SHA-256 in `engineering/mac_app/MAC_A3_RECEIPT_KO.md` and the decision doc, then push a new head for re-audit.
