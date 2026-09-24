# Control Plane Production Bring-up — Operational Record (E4)

**Docs only.** Describes observed production state as of 2026-09-24 (KST). Does not add features, change activation, or widen builder allowlists.

| Field | Value |
|---|---|
| Record time (KST) | 2026-09-24 ~10:50–11:10 |
| Authority | Astra Midcoord E2 → E4 docs |
| Repo | `BeautifulMind-JT/kix-protocol` |
| main HEAD (at record) | `ec3f6db0d613385bfdf2392a4295f0099be1eec6` (merge of #48) |

## Production runtime state

| Item | State |
|---|---|
| Production runner | `astra-kix-box-1` **id 22** — online; labels `self-hosted`, `Linux`, `X64`, `astra-control-plane` |
| `runtime_enabled` | **true** |
| `activated_runtime_sha` | `c106fc7a8427583bbc4a085055413c6d334b3712` |
| `enabled_builders` | **DEVIN**, **GROK_BUILD** |
| GLM | **NOT ENABLED** (present in `allowed_builders` / wrappers only; absent from `enabled_builders`) |
| Enablement PR | https://github.com/BeautifulMind-JT/kix-protocol/pull/48 |

## First production canary (GROK_BUILD)

| Field | Value |
|---|---|
| Issue | https://github.com/BeautifulMind-JT/kix-protocol/issues/49 (CP-CANARY-001) — **closed** acceptance met |
| Dispatch run | https://github.com/BeautifulMind-JT/kix-protocol/actions/runs/35944111388 |
| Result | **CONFIRMED** |
| `launch_request_id` | `ae5dc6e6456d26f7572a9bb3` |
| `owner_session_id` | `grok-cli:8afdf854-3f27-4118-aec6-1ba682dc9de3` |
| Runner | id **22** |

### E1 fail-closed probes (same session)

| Case | Run | Outcome |
|---|---|---|
| Wrong issue body sha | https://github.com/BeautifulMind-JT/kix-protocol/actions/runs/35944177467 | reject — body mismatch |
| Wrong builder pin (DEVIN) | https://github.com/BeautifulMind-JT/kix-protocol/actions/runs/35944185111 | reject — BUILDER_ID mismatch |
| Duplicate correct pins | https://github.com/BeautifulMind-JT/kix-protocol/actions/runs/35944192990 | `existing confirmed owner; no second launch` |

## Slack gateway

| Item | State |
|---|---|
| Gateway | live (8787 + path-filter 8788 observed in post-enable session) |
| Policy | gateway `enabled=true`; builder lanes GROK_BUILD / GLM / DEVIN **execution still `enabled:false`** |
| Slack → builder launch | **none** — status/refresh/dispatch accepted as non-exec ephemeral; bad sig/channel/stale → 403 |
| Evidence | `/workspace/astra-host-evidence/post-enable-canary/` · `POST_ENABLE_CANARY_SESSION.md` |

## Rollback / disable path

1. Ordinary PR to `.github/control-plane/activation.json`: set `runtime_enabled=false` (and/or retarget `activated_runtime_sha` only with Astra/User gate).
2. Ordinary PR to `.github/control-plane/config.json`: remove a builder from `enabled_builders` (do not widen without gate).
3. Host: stop/disable runner 22 or revoke host-policy lane if emergency containment is required (operator action; not automated).
4. Do **not** force-push, direct-push main, or self-merge as a builder.

## Fail-closed invariants (observed)

- Wrong body / wrong builder pin → authorize fail **before** launch.
- Duplicate dispatch with existing CONFIRMED owner → **no second launch**.
- One-event / one-dispatch / no uncontrolled retry (E1 duplicate probe).
- Builder does not self-merge; User authorizes merge.
- GLM remains disabled despite being an allowed adapter candidate.
- Sibling trees must not treat local copied control-plane files as production SoT (see E2 pointers).

## E2 sibling pointer status (docs only)

| Target | Repo | PR |
|---|---|---|
| ZARI | `BeautifulMind-JT/ZARI` | https://github.com/BeautifulMind-JT/ZARI/pull/16 |
| FILM-UNIT | `BeautifulMind-JT/film-unit-mv-studio` | https://github.com/BeautifulMind-JT/film-unit-mv-studio/pull/11 |
| SOULBOUND | **missing** (no org repo) | — |
| 마음결 | `BeautifulMind-JT/maeum-gyeol` | https://github.com/BeautifulMind-JT/maeum-gyeol/pull/10 |

Umbrella https://github.com/BeautifulMind-JT/kix-protocol/issues/40 remains **open** (full adoption not met).

## Related session artifacts (operator box)

- `/workspace/astra-kix-work/POST_ENABLE_CANARY_SESSION.md`
- `/workspace/astra-kix-work/ACTIVATED_SHA_AND_C_SESSION.md`
- `/workspace/astra-kix-work/E2_E4_SESSION.md`
- `/workspace/astra-host-evidence/post-enable-canary/`
