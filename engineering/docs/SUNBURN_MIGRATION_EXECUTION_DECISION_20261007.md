# SUNBURN migration execution scope and preserved historical evidence

The exact approval projection is [6029021289](https://github.com/SUNBURN-Golden/ai-ops-control-plane/pull/83#issuecomment-6029021289). Its unchanged API body is copied in `SUNBURN_IDENTITY_DECISION_20261007.md`; identity/hash/bytes/timestamps and relay provenance are recorded separately. These copies are audit evidence, not substitutes for authenticated live verification.

Independent GLM5.3 design analysis [6029179194](https://github.com/SUNBURN-Golden/ai-ops-control-plane/pull/83#issuecomment-6029179194) returned **DECISION_REQUIRED**, not PASS. Actual model `glm-5.3`, session `f4bdd59c-027a-4ed0-aab3-04751126d692`, source HEAD `7e8bb71f1ea2581fcb493ce7d990534980436c8b`, base `77327d3a5f177aca81952ca8e24b5c695f086a23`, design SHA-256 `94d43549fe127cc97be2ffacea992c318184ea2fda1f15280f11a1321ec9ff07`, evidence SHA-256 `4ccf5b17635f4fa43f9854eaa52dd54b2d6c8f3245e43baf1c39e45ca04252b6`, raw output SHA-256 `5085d62cf6a62b20a512744081d7b081a1ff94b25ade860b7b56f48fa036f67c`. This result is not ASTRA_FABLE or protected aiops-fable output.

Its consequential question concerned the existing maeum-gyeol profile's **AIOPS control address**. The subsequent exact User instruction from source thread `01a0f57c-0df9-72ce-bb48-d1aaf753d004`, message `Sentinel_6b452465121081918cb75db0ff6b1347`, is recorded with relay provenance:

> KIX보다 SUNBURN-Golden 이전 작업부터 먼저 끝내.
> 1) projects.json 등 컨트롤 플레인 설정과 문서·예시·워크플로에 남은 BeautifulMind-JT/ai-ops-control-plane, BeautifulMind-JT/kix-protocol 참조를 전부 SUNBURN-Golden으로 바꿔서 #83에 push해. CI의 "config repository does not match GITHUB_REPOSITORY" 오류부터 해결해.
> 2) 러너·시크릿·브랜치 보호·맥 앱 설정 중 이전 때문에 깨진 게 있으면 같이 확인하고 목록으로 남겨.
> 3) #83 CI 통과하면 맥 glm-5.3으로 A3 감사하고, PASS 계열이면 head 고정 merge commit으로 병합해.
> 4) 병합 후 main CI 통과까지 확인한 다음에 KIX job 20531604498943e1 재개해.

This explicit update of all AIOPS control references includes maeum-gyeol.control_repository, which still denotes the same AIOPS repository ID1373567344. The existing maeum-gyeol target/key/owner and actor/gates remain unchanged. It is not added to the five-repository migration identity map or transferred. The design result remains DECISION_REQUIRED as originally returned; this later instruction records the chosen control-pointer update, while the completed implementation still requires its own exact-HEAD independent A3 and CI.

Current central configuration pins AIOPS1373567344, KIX1365416872, Commerce1388268331, ZARI1373217962 and Film1365377662 to SUNBURN-Golden/338877516. Personal User/record actors stay BeautifulMind-JT/263336091. Old names cannot create a fresh migrated-target admission. Existing task/plan/spec/generation/request/attempt/binding/receipt bytes and derived IDs are not rewritten. UNKNOWN/hold and single-writer fences remain.

The original design input `SUNBURN_IDENTITY_MIGRATION_PROPOSAL_KO.md` is retained as the exact audited snapshot at the above design hash; its pending-state wording describes its input-time status. New decisions and current implementation status are recorded here rather than altering that snapshot.

Historical User comments, decision/receipt copies and record hashes, original program/plan/approval pointers, provenance/history/source manifests, KIX locked blob authority and single-subject Mac pins retain their original bytes. Remaining old names in those locations are intentional provenance, not current defaults or permission aliases.

Protected Linux `control_plane_host*`, hostpack, boundary, sudoers and Fable, existing receipt parsers/producers, new credential/scope changes and direct ledger rewrite remain excluded. No force/main push or gate bypass. Runtime stays frozen pending subsequent verification and required qualification. Source CI or this GLM design analysis does not establish host qualification or authorize KIX execution through a blocked gate.
