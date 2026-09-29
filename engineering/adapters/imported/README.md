# Host builder adapters

These are the host builder wrappers and adapters. The host operator imported them from the installed paths on 2026-09-29, in commit `ae76208` on the branch `ops/import-host-adapters`. `SHA256SUMS.imported` records the installed digests at import time.

| File | Installed path |
|---|---|
| `astra-builder-*` | `/opt/astra/bin/astra-builder-*` (root-owned wrappers, drop to the builder UID) |
| `astra-*-adapter` | `/opt/astra/libexec/astra-*-adapter` (launch contract v2) |

Program mode (`docs/PROGRAM_MODE.md` §4.2) changes `build_prompt` and adds `write_signer`:
- `write_signer` puts a private `signer.py` (0600) in the 0700 session directory. It holds the packet's `review_nonce` or `delivery_nonce` and prints the session's signed marker line: `ASTRA_REVIEW_V1 ... mac=`, `ASTRA_DELIVERY_V1 ... mac=`, or for either role `ASTRA_BLOCKED_V1 kind=... launch=... mac=`. The host verifies the MAC at reap, and refuses to release a keyed session without one of these lines.
- the writer prompt names the task branch `astra/<task id>`; deliveries from any other branch are refused.
- a schema v2 `REVIEWER` packet gets read-only review instructions and the command that prints its verdict line. A reviewer packet without a valid nonce is refused before any session starts (`FAILED_PRESTART`).
- a schema v2 writer gets the command that prints its delivery line; a schema v1 writer keeps the unsigned legacy line.
- the prompt names only the signer path, never the key.

Session termination is **not** taken from the adapters' `status.json`. That file sits in the builder-writable worktree, so the builder could forge it. The host helper `reap` instead verifies that the lane UID has no live process.

`SHA256SUMS` holds the digests of these reviewed sources. The operator installs exactly these bytes after the implementation audit. The supervisors (`/opt/astra/libexec/astra-*-supervisor`) are not imported yet, so they are still unaudited host code.

The keys stay secret only while they stay out of process arguments and environments, because other lanes can read `/proc/<pid>/cmdline` (`reap` refuses `hidepid`). The adapters keep them in files: the packet arrives on stdin, and the signer and prompt are 0600 files. The supervisor import in P3 must confirm the supervisors do the same, and that every lane process descends from the helper's launch (the quiescence precondition).

**Quiescence probe (`--quiescence`).** Every wrapper routes `--quiescence` to its adapter, which runs as the lane identity:
- The adapter sends `kill(-1, SIGSTOP)` to every process of the lane UID. This cannot race a fork: the kernel aborts a fork that sees the pending stop.
- It lists what remains from `/proc`, then sends `SIGCONT`.
- It prints `{"status": "OK", "live": [...]}` and refuses to run as root.
- The host combines this census with its own outside scan. It uses both before a reap, and before accepting an adapter's `FAILED_PRESTART`, because the builder UID can write the adapter's result files.
