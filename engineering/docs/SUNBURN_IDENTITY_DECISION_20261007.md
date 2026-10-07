## D-SUNBURN-IDENTITY-MIGRATION-20261007 — exact-scope User approval record

This executor records the User's action-time approval relayed from source thread `01a0f57c-0df9-72ce-bb48-d1aaf753d004`. This is a durable approval projection with explicit relay provenance; it does not claim that the User personally typed this GitHub comment or that the approval is a signed host observation.

Approval request message: `Sentinel_f7b674ba9a9c81919e38f187ce65c345`.
User approval message: `Sentinel_e7564964233081918ed2d78f908f3712`.
Exact request text:

> 중앙 신뢰 변경은 보안 설정이라 실행 도구가 정확한 범위 승인을 요구했어. 아래 범위로 승인해줘
> SUNBURN-Golden(조직 ID 338877516)을 중앙 신뢰 대상으로 지정하고, 기존과 동일한 저장소 ID를 가진 AIOPS·KIX·Commerce·ZARI·Film의 registry, control_repository, target 검증을 새 소유자 기준으로 변경한다. 기존 작업·승인·영수증은 보존하고 다른 저장소나 새 토큰 권한은 허용하지 않는다. 비공개 설계·관련 소스를 기존 Z.AI glm-5.3에 보내 독립 A3 검토하는 것도 승인한다
> 이 변경은 이후 자동화가 새 조직을 신뢰하게 만드는 지속 설정이야. 이 범위로 진행해도 될까?

Exact User reply:

> 승인할게

The approved identities are AIOPS `1373567344`, KIX `1365416872`, Commerce `1388268331`, ZARI `1373217962`, Film `1365377662`: old owner `BeautifulMind-JT` / `263336091` to new owner `SUNBURN-Golden` / `338877516`, with the same repository names and IDs. The personal User actor remains unchanged.

Analysis designation for this new scope is `MAC_GLM53_CLAUDE_CLI`, actual model `glm-5.3`, through the existing Z.AI Claude CLI route in an independent read-only session. Record actual model/session/request/evidence SHA. PR82 designation or PASS does not carry over, and this result must never be labelled ASTRA_FABLE or protected aiops-fable output.

Implementation is limited to the approved registry/control repository/current-target identity validation and same-ID append-only subject mapping. Preserve original job/task/generation/plan/spec/owner/request/attempt/binding, approval/receipt bytes and hashes, UNKNOWN/holds, single-writer and replay defenses. Reject foreign IDs/owners, forks, recreated old namespaces and other subjects. Historical pointers remain provenance and cannot create a fresh admission through an old-name alias.

No direct ledger rewrite, force push, main push, new credentials/scopes, additional repository trust, Linux protected file/Fable/parser changes, new receipt producer, automatic merge or gate bypass is authorized. Runtime remains frozen until subsequent verification and qualification. A different consequential contract or out-of-scope change must be separately reported.

Source baseline: main `77327d3a5f177aca81952ca8e24b5c695f086a23`; PR83 current source HEAD `7e8bb71f1ea2581fcb493ce7d990534980436c8b`. Independent design analysis precedes implementation; exact implementation HEAD requires its own review and CI.
