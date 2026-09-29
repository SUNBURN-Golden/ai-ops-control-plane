# Host builder adapters

These are the host builder wrappers and adapters. The host operator imported them from the installed paths on 2026-09-29, in commit `ae76208` on the branch `ops/import-host-adapters`. `SHA256SUMS.imported` records the installed digests at import time.

| File | Installed path |
|---|---|
| `astra-builder-*` | `/opt/astra/bin/astra-builder-*` (root-owned wrappers, drop to the builder UID) |
| `astra-*-adapter` | `/opt/astra/libexec/astra-*-adapter` (launch contract v2) |

Program mode (`docs/PROGRAM_MODE.md`) changes only `build_prompt`:
- a schema v2 `REVIEWER` packet gets read-only review instructions and the `ASTRA_REVIEW_V1` verdict line, which ends with the packet's secret `review_nonce`;
- a writer gets the `ASTRA_DELIVERY_V1` delivery protocol.

Session termination is **not** taken from the adapters' `status.json`. That file sits in the builder-writable worktree, so the builder could forge it. The host helper `reap` instead verifies that the lane UID has no live process.

`SHA256SUMS` holds the digests of these reviewed sources. The operator installs exactly these bytes after the implementation audit. The supervisors (`/opt/astra/libexec/astra-*-supervisor`) are not imported yet, so they are still unaudited host code.

The adapters write the prompt to a mode 0600 `prompt.txt` in the session directory. The review nonce is secret only if a supervisor never passes the prompt text in a process argument or environment variable (`/proc/<pid>/cmdline` is readable by other lanes, since `reap` refuses `hidepid`). The supervisor import in P3 must confirm this.
