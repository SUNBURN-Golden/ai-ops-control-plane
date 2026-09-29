# CURSOR lane (no systemd)

User decision M3 (2026-09-29) is option (b): an adapter that needs no systemd. It replaces the earlier `scripts/control_plane_cursor.py`, which needed a lingering systemd user manager. This host has no systemd (PID 1 is tini).

The lane follows the same contract as the DEVIN, GROK_BUILD and GLM lanes (`../imported/`):

| File | Installed path | Runs as |
|---|---|---|
| `astra-builder-cursor` | `/opt/astra/bin/astra-builder-cursor` (root-owned wrapper) | control identity, then `sudo -u astra-builder-cursor` |
| `astra-cursor-adapter` | `/opt/astra/libexec/astra-cursor-adapter` | `astra-builder-cursor` |
| `astra-cursor-supervisor` | `/opt/astra/libexec/astra-cursor-supervisor` | `astra-builder-cursor`, detached (`setsid`); tini reaps it |

**Flow**
1. The adapter reads the packet on stdin. It never passes the packet in a process argument or the environment, because other lanes can read `/proc/<pid>/cmdline`.
2. It checks the qualified configuration, the CLI digest, the account fields and the exact model.
3. It creates the 0700 session directory and the private signer (`signer.py`, 0600), and writes `prompt.txt` and `job.json` (0600).
4. It starts the supervisor in its own session and waits for `result.json`.
5. The supervisor clones the repository into the per-request worktree (blob-less partial clone, 45 s cap) and reads the prompt. A failure here is `FAILED_PRESTART`.
6. It runs `create-chat` and persists `cursor-cli:<chat>` as `CONFIRMED`. Any failure from the chat request on is `UNKNOWN`.
7. It runs the agent on that chat to completion, then writes `status.json`. That file is informational only: `reap` uses lane UID quiescence.
8. Every lane process descends from the helper's launch. There is no systemd user unit, so the quiescence precondition holds.

**Qualified configuration: `/etc/astra/cursor-lane.json`** (root-owned, not group- or world-writable; example `../../.github/control-plane/cursor-lane.example.json`):
- `cli`: the root-owned Cursor CLI path;
- `cli_sha256`: its digest, pinned at qualification;
- `model`: the exact model slug from the CLI's `models` listing. `auto`, `default` and placeholders are refused;
- `auth_match`: non-secret fields of `status --format json` that identify the subscription account.

The preflight report carries `harness: CURSOR_CLI` and the exact `model`, which the host preflight requires.

**P3 host work (operator / Grok bot)**
- Create the `astra-builder-cursor` Unix account with no subuid/subgid ranges, and a private HOME.
- Create `/opt/astra/worktrees/cursor` (0700, owned by the lane).
- Install the root-owned CLI and log in the lane account to the Cursor subscription.
- Give the lane account git read access to the target repositories.
- Install these three files and the configuration, and register the wrapper's digest.
- Qualify the CLI flags used by the supervisor on the installed version: `status --format json`, `models`, `create-chat`, `--print --force --trust --output-format json --model --resume --workspace`.
- Run one diagnostic canary. It must show that the Cursor CLI leaves no process behind under the lane UID after the session ends (otherwise `reap` never passes), and that the clone plus `create-chat` finish inside the adapter's 100 s wait.

`SHA256SUMS` holds the digests of these reviewed sources.
