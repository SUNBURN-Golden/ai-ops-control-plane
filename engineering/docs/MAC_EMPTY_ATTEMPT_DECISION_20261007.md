D-2026-10-07-MAC-EMPTY-ATTEMPT-RECOVERY

Provenance: USER_DIRECT_APPROVAL_RELAYED_FROM_SOURCE_THREAD. This is an executor relay of the direct User approval, not a claim that User personally typed this GitHub comment or a host signature.
Source thread: 01a0f57c-0df9-72ce-bb48-d1aaf753d004.

Parent question Sentinel_e70eba775b688191a04567bb4243f6bf:
“설치 차단 원인을 찾았어. 리뷰어 CLI를 찾지 못한 실행 전 실패가 빈 폴더 하나를 남겼고, 실제 실행·원장 참조는 없어. 이 정확한 폴더만 잠금 아래 재검증해 증거와 함께 격리하는 복구 기능을 추가하고 적용해도 될까? 삭제하거나 설치 검사를 우회하지 않고 복구 가능하게 보존할게”
Parent clarification Sentinel_9735d7bbb5608191a44e0c1adc622993 explicitly included “빈 attempt 폴더의 증거 보존·격리”.
User Sentinel_3a7fe84075cc8191bde000897d5e5ffc: “응 승인할게”.

Scope: only existing Mac job20531604498943e1 and empty attempt directory d8d194e55af64b0b8a44a487bd41fd94. Add and apply a supported, reversible quarantine under the existing installer/service locks after rechecking service stopped, no active attempts, all canonical/native executions terminal, directory empty/private/non-symlink, no reference to that UUID anywhere in the existing app/astra ledgers, and preservation of original completed receipts and job state. Preserve original path, metadata and evidence; use an atomic same-filesystem move, not deletion. Fail closed on changed or missing evidence. Ordinary installer validation remains intact. No calls/ledger/installer-guard edits, arbitrary deletion, other-directory cleanup, new product builder, credentials/scopes or protected Linux changes. A separate branch/PR with required CI and independent review precedes application. Do not install concurrently with PR86 owner; report the exact validated install target to the parent after recovery.

Evidence provenance: product owner observed directory creation2026-10-07T04:58:54.219293Z, followed about2ms later by reviewing/MISSING_PROVIDER. core._launch creates the directory before checking the reviewer CLI. No event directly names this UUID, so the causal attribution is an inference from combined evidence, not a recorded UUID-to-event link. The command must revalidate current facts under lock rather than trust this narrative.
