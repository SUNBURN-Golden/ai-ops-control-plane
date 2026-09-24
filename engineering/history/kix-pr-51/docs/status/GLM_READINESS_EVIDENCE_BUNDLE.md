# GLM readiness evidence bundle — NOT ENABLE

- Time (KST / Asia/Seoul): 2026-09-24 ~11:00
- Authority: Astra Midcoord E2 Gate B=**NO** (do not enable); E path = GLM readiness only
- Repo: BeautifulMind-JT/kix-protocol
- Result label: **GLM_ENABLE_READY_PENDING_GATE** (evidence collected; enable deferred)

## 1. Identity

| Check | Result |
|---|---|
| `builder_id` | `GLM` |
| Wrapper | `/opt/astra/bin/astra-builder-glm` (root-owned; drops to `astra-builder-glm`) |
| Inner adapter | `/opt/astra/libexec/astra-glm-adapter` |
| Host UID map | `builder_uids.GLM = 994` |
| `--preflight` (wrapper) | `{"builder_id":"GLM","execution_mode":"PERSISTENT_SUPERVISOR","harness":"opencode","launch_contract_version":2,"parallel_safe":true,"provider_plan":"Z.AI Coding Plan","status":"PASS","worktree_root":"/opt/astra/worktrees/glm"}` |

## 2. Exact model / builder mapping

| Field | Value |
|---|---|
| Repo `builder_wrappers.GLM` | `/opt/astra/bin/astra-builder-glm` |
| Host `wrapper_paths.GLM` | same |
| Harness | Official OpenCode CLI only (`/opt/astra/libexec/opencode`) — **not** zcode |
| Provider plan | Z.AI Coding Plan via OpenCode auth |
| Session id prefix | `opencode-cli:<id>` |
| Worktree root | `/opt/astra/worktrees/glm` |
| Launch contract | version **2** |
| Model pin | `ASTRA_GLM_MODEL` env (optional); unset → OpenCode default after auth. Exact pin is operator/User choice. |

## 3. Env / admission contract

| Layer | GLM listed? | Notes |
|---|---|---|
| Repo `allowed_builders` | **yes** | candidate adapter |
| Repo `enabled_builders` | **no** | **blocker for production dispatch** |
| Host `enabled_builders` | **yes** | host already admits GLM |
| Host `allowed_repositories` | `BeautifulMind-JT/kix-protocol` only | |
| `max_active_sessions` | 1 | |
| Prior host preflight (ba366de) | GLM **PASS** | `/workspace/astra-host-evidence/ba366de-host/PREFLIGHT_REPORT.md` |

Evidence dir: `/workspace/astra-kix-work/glm-readiness/` (`config.json`, `host_policy_redacted.json`, `astra-builder-glm-preflight.txt`, `enablement_gap.json`, `glm_adapter_head.txt`).

## 4. Dispatch request validation (expected; not live-launched)

Production path requires builder ∈ repo `enabled_builders`. With current main config, a `BUILDER_ID=GLM` dispatch must fail authorization the same way E1 rejected wrong pin (DEVIN vs GROK_BUILD) — see run https://github.com/BeautifulMind-JT/kix-protocol/actions/runs/35944185111.

**This session did not** add GLM to `enabled_builders` and did **not** fire a GLM production canary (Astra B=NO).

## 5. Result / finalize binding

Shared control-record path (`ASTRA_CONTROL_RECORD_V1`, actor `github-actions[bot]`) is builder-agnostic once launch is admitted. Proven end-to-end for GROK_BUILD on #49 (`launch_request_id=ae5dc6e6456d26f7572a9bb3`, CONFIRMED). GLM uses the same finalize machinery after enable — not re-proven under GLM id this burn. Adapter identity fields: `repository`, `task_id`, `task_revision`, `builder_id`, …

## 6. Duplicate suppression

E1 duplicate probe (GROK_BUILD): success with `existing confirmed owner; no second launch` (run 35944192990). Admission ledger `/var/lib/astra/control/admission.sqlite3` is shared across builders; GLM inherits the same one-owner invariant after enable.

## 7. Failure behavior

| Mode | Evidence |
|---|---|
| Wrapper preflight | Current: **PASS** (JSON status) |
| Host-control as wrong UID | `HOST_CONTROL_ERROR: helper must run as the configured non-root control identity` (observed as box user) |
| Missing OpenCode / Z.AI auth | Adapter fail-closed (`opencode Z.AI Coding Plan auth check failed`) |
| Repo enable missing | Dispatch authorize reject (analogous to E1 BUILDER_ID mismatch; not live-tested for GLM id) |
| Prior independent audits | Multiple `GLM_AUDIT_*.md` PASS / PASS_WITH_NOTES (reviewer lane), not production enable proofs |

## Explicit non-actions

- Did **not** modify `.github/control-plane/config.json` `enabled_builders`
- Did **not** undraft #35/#38
- Did **not** close #33/#37/#40
- Did **not** raise paid quota; Devin unused (`BLOCKED_LOCAL_QUOTA`)

## Next gate (for Astra / User later)

Only after an ordinary PR adding `GLM` to repo `enabled_builders` + delta audit + User merge: single bounded GLM canary under a new issue. Until then remain **GLM_ENABLE_READY_PENDING_GATE**.
