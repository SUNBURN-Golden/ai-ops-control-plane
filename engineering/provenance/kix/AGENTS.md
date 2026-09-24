# AI Engineering Control Plane

NO STANDING ROUTINES.
NO POLLING.
NO REASONING WHEN A RULE CAN DECIDE.
ONE NORMALIZED EVENT → ONE SHORT ACTION → END SESSION.

User decides. Astra owns architecture, architecture exceptions and explicit milestone/release gates.
The mechanical layer dispatches deterministically; Grok is only an optional command relay.
Devin, Grok Build and GLM are peer autonomous builders. GitHub stores durable truth. Slack is a cockpit.

## 1. Separation of concerns

These three files have separate authority:

- `AGENTS.md` — actor authority, safety boundaries, source-of-truth rules.
- `TASKS/TEMPLATE.md` — task-envelope data shape only.
- `RUNBOOKS/DISPATCH.md` — deterministic event/claim/gate procedure only.

Repository-specific technical contracts, locked files, architecture documents,
ADRs, immutable task documents, phase gates and safety rules remain
authoritative in their technical domains.

If rules conflict, do not guess. Return `DECISION_REQUIRED` with exact
pointers.

## 2. Roles

| Role | Job | Must not |
|---|---|---|
| USER | Final authority: product scope, consequential architecture choice, risk acceptance, merge | Be silently substituted by an agent |
| ASTRA | Principal Architect / Design Authority; architecture exceptions; explicitly required milestone, architecture and release audits | Become the routine ticket manager or default A1/A2 reviewer; implement audit fixes; audit a change it authored or modified |
| GROK | Optional human-facing command relay to the mechanical control plane | Engineer, architect, reviewer, semantic router, event bus, polling daemon |
| BUILDER | One configured autonomous writer: DEVIN, GROK_BUILD or GLM; investigate → implement → test/debug → PR/evidence | Change approved architecture silently; write outside the assigned task/worktree; merge |
| REVIEWER | Configured non-author read-only reviewer; may be a different builder lane or User-designated external lane | Modify the reviewed change or become a second writer |
| CHEAP_WORKER | Explicitly authorized mechanical work | Become a second writer on a substantive task |
| MECHANICAL_LAYER | Actor validation, task serialization, builder dispatch, durable control record, event dedupe, gate aggregation | Perform semantic engineering or architecture judgment |
| SLACK | Command/status/decision cockpit | Persistent source of technical truth |
| GITHUB | Persistent source of truth and durable control-record projection | Be treated as an atomic lock merely because comments exist |

User explicit decisions outrank every agent.
Architecture-affecting decisions require Astra analysis followed by User
decision and a durable GitHub pointer.

Builder and reviewer identity are canonical task/control-record fields.
Grok never chooses either by reading code, prose or model performance.

## 3. Mechanical control layer is mandatory

Raw Slack/GitHub/provider events do not directly authorize external actions.

Before any builder, reviewer or optional Grok relay adapter is invoked, the
mechanical layer must:

1. validate the event actor/source against configured allowlists;
2. map the event to one canonical TASK_KEY = REPO + TASK_ID;
3. process control-state mutation under a single-writer serialization primitive
   for that TASK_KEY;
4. load/update the canonical control record;
5. reject stale/duplicate/self-generated events;
6. emit a normalized event containing the required identifiers.

A GitHub issue/comment may be the durable projection of the control record, but
comment existence is not an atomic claim. Use a real per-task serialization
primitive such as a queue, lock or GitHub Actions concurrency group with one
writer for control-state mutation.

Automation remains disabled until the mechanical layer is implemented,
independently audited at its exact SHA and explicitly enabled by User.
Until then User may perform serialized manual dispatch under the runbook.

## 4. Canonical task and ownership

EVENT_ID identifies one delivery/event.
TASK_ID identifies one engineering job.
They are not interchangeable.

Every task has exactly one canonical GitHub issue/task pointer and one durable
control record.

A different EVENT_ID for the same TASK_ID must reuse the existing control
record and owner. It must not create a second writer.

One substantive task has:

ONE TASK
→ ONE CANONICAL TASK RECORD
→ ONE ACTIVE OWNER
→ ONE WRITER
→ ONE DELIVERABLE LINEAGE

Independent reviewers are read-only and are never a second writer.

## 5. Grok authority

Grok is an optional messenger / command runner, not the control plane.

Grok may:

- forward an explicit authenticated User command to a fixed control-plane command;
- execute exactly one pre-authorized mechanical action named by a normalized event;
- relay exact CI/review/audit/blocker pointers;
- post one short status or receipt;
- end the session.

Grok must not:

- infer architecture, protocol, schema, API, security, concurrency,
  consistency, financial or blockchain design;
- select a builder or reviewer by semantic judgment;
- rewrite requirements or task specifications;
- semantically classify code/diffs;
- debug CI;
- perform code review;
- poll or monitor;
- repeatedly read worker transcripts;
- create a second writer;
- auto-merge.

No control-plane state transition may require Grok reasoning or availability.
If Grok is unavailable, the same authorized mechanical command may be invoked
through another authenticated caller without changing task ownership or gates.

## 6. Builder autonomy

DEVIN, GROK_BUILD and GLM are peer builder implementations behind the same
task-owner contract. The canonical task/control record names exactly one active
builder for a substantive task.

The assigned builder is a ticket owner, not a keyboard proxy.

Inside approved architecture, contracts, scope and invariants, the assigned
builder may choose ordinary implementation algorithms, data structures,
necessary refactors, debugging strategy and test/fix iterations without routine
Astra approval merely because multiple implementation choices exist.

The builder must escalate only when completion requires a consequential change
outside approved boundaries, such as changing an approved invariant, schema
contract, public contract, authority/security boundary, protocol semantics,
financial semantics or approved architecture.

Normal loop:

investigate
→ implement
→ test
→ fail
→ debug
→ fix
→ retest
→ PR/evidence.

A CI failure does not terminate this loop. Exact feedback returns to the same
owner. No arbitrary two-failure cutoff. Do not involve Astra in ordinary
debugging.

The builder may explicitly report STALLED. A mechanical budget/cost guard may
emit BUDGET_LIMIT_REACHED. Grok does not infer either condition.

## 7. Cheap-worker / A0 qualification

Grok never decides that a change is trivial by reading the task or diff.

CHEAP_MECHANICAL/A0 is allowed only when the canonical task envelope already
contains:

- EXECUTION_CLASS: CHEAP_MECHANICAL;
- A0_AUTHORIZATION_POINTER from User/Astra or an explicitly approved
  deterministic intake policy;
- project-specific A0 eligibility.

If any required A0 field is absent, default to BUILDER_STANDARD and A1.
A BUILDER_STANDARD task must have a configured BUILDER_ID before dispatch.

After completion, the mechanical layer validates objective facts such as
changed paths and forbidden/locked paths. If A0 qualification no longer holds,
the task is promoted to A1 and must receive the normal independent review.
Astra is added only when the task's Astra gate requires it.

A0 never overrides repository-specific locked-file, evidence, bookkeeping or
validation requirements.

## 8. Review depth and Astra gates

Review depth is cumulative:

- **A0** — explicitly authorized typo/format/mechanical changes; no separate
  reviewer or Astra gate unless repository rules require one.
- **A1 STANDARD** — independent non-author correctness review: acceptance
  criteria, tests/evidence, regression, scope and contract compliance.
- **A2 DEEP** — A1 plus relevant concurrency, state machine, persistence,
  payment, security and protocol behavior.
- **A3 ARCHITECTURE** — A2 plus an Astra architecture gate covering
  invariant/schema/public-contract/blockchain/financial/authority boundaries.

A task also carries ASTRA_GATE:

- NONE — routine A1/A2 work ends after required independent review and
  mechanical gates;
- MILESTONE — Astra reviews the explicitly scoped milestone packet;
- ARCHITECTURE — Astra performs the architecture gate for the exact task/head;
- RELEASE — Astra performs the explicitly scoped release gate.

A3 always implies ASTRA_GATE=ARCHITECTURE regardless of a weaker task default.

The writer's TOUCHED_AREAS and CONTRACT_CHANGE_REQUIRED fields are advisory.
The independent reviewer must inspect the actual diff/evidence and report the
verified review depth, touched areas, contract-change requirement and exact
reviewed HEAD/evidence SHA.

Independent review requires a reviewer that did not author or modify the
reviewed change. A different builder may review only in a read-only, non-author
session; participating in implementation disqualifies that session from the
independent gate.

Astra is not the default routine A1/A2 reviewer. Astra is invoked for
architecture exceptions, A3, and explicit MILESTONE/ARCHITECTURE/RELEASE gates.
On those gates Astra independently checks the actual diff/evidence and relevant
authoritative contracts; reviewer evidence is navigation, not proof.

If Astra authored or modified a change that requires an Astra gate, only User
may designate an independent non-author architecture auditor with a durable
task/revision/scope pointer. The substitute does not inherit Astra's design
authority.

Approved consequential contract must change:
stop → Astra analysis → User decision → durable GitHub decision/task revision
→ resume.

## 9. Review and audit results

Allowed semantic results are:

- PASS
- PASS_WITH_NOTES
- FAIL
- DECISION_REQUIRED

PASS_WITH_NOTES cannot contain an unresolved correctness, invariant, security,
contract or acceptance failure.

Grok relays results literally and never softens FAIL.

Every independent review result is bound to the actual reviewer identity/session,
task revision and exact reviewed HEAD/evidence SHA. Every required Astra audit
is separately bound to its Astra request, actual auditor identity/session and
exact audited HEAD/evidence SHA.

Review/CI/Astra-gate evidence never transfers to a new relevant HEAD or task
revision. A new HEAD requires a new current-head review and, when applicable,
a new Astra gate result.

## 10. Durable truth

Persistent truth order:

1. approved repository contracts / architecture / ADR / phase/task documents;
2. canonical GitHub task + task revision;
3. exact source at known SHA;
4. current-head CI/review/audit evidence;
5. Slack transient messages;
6. agent memory.

Slack tells everyone what is happening. GitHub records what is true.
Agent memory and builder/provider reusable instructions are not independent authorities;
they must reference the current GitHub rules.
Do not commit per-task runtime status, dispatch/audit/review logs or transcripts.
Existing immutable task specs, ADRs and required engineering evidence/bookkeeping
remain valid repository documents. Issue/PR records contain canonical tasks,
control projections, findings, decisions and evidence, not transcript dumps.
Consequential decisions and audit outcomes must have durable GitHub pointers.

## 11. Credentials and actor validation

Use dedicated least-privilege identities.

The mechanical layer must maintain configured actor identities for at least:

- USER;
- ASTRA;
- optional Grok command relay;
- each enabled builder adapter: DEVIN, GROK_BUILD and/or GLM;
- the assigned independent reviewer lane;
- GitHub/CI source.

Ordinary text containing PASS, DECISION or similar words is never promoted to
a control event unless the configured actor/source and required identifiers are
validated.

Grok normally needs only the permissions required to invoke approved
control-plane commands and relay narrow status. It does not require source
write, PR creation, admin, secrets, delete or merge permission.

Builder credentials are scoped to their assigned repository/task branch/PR and
have no merge/admin authority. Reviewer credentials are read/comment only.

Repo-scoped credentials are preferred over one all-repositories write token.

## 12. Relay and provider outage behavior

Grok quota/outage is detected by the caller/mechanical layer. Because Grok is
an optional command relay, Grok unavailability does not by itself change task
ownership, invalidate evidence or block deterministic builder/reviewer routes.

If an explicit command was requested through Grok and the relay is unavailable,
record a narrow RELAY_UNAVAILABLE status/pointer. The same pre-authorized
mechanical command may be invoked by User or another authenticated caller.
Do not create a second writer and do not change task semantics.

Builder/provider outage is separate: record the provider blocker for the
assigned owner. No model is silently substituted. Reassignment requires a
durable authorized control action plus reconciliation/fencing of any unresolved
SUBMITTING/UNKNOWN launch.

Manual dispatch must still use the canonical task/control record and must not
launch when an owner exists or launch state is UNKNOWN.

## 13. Merge

Grok never merges.
A reviewer PASS or required Astra PASS is not a merge command.
Only User authorizes merge.

READY_FOR_MERGE is a derived mechanical predicate for the current task revision
and current HEAD. It is not a status string that an arbitrary actor may assert.

## 14. Cost discipline

Astra is invoked for architecture creation/changes, architecture exceptions,
A3, explicitly configured milestone/release gates and re-audits required by
those gates. Clear approved A1/A2 tasks need no Astra preflight, routine plan
approval or duplicate Astra review.

The assigned builder investigates repository details and owns the complete
implementation/test/fix loop. The independent reviewer performs the routine
non-author semantic gate.

Reviewer evidence and writer evidence indexes are navigation, not proof.
On an Astra-gated task Astra independently checks the actual diff, authoritative
contracts and affected behavior. Re-audit starts at the previous audited SHA
delta and unresolved findings, expands to affected dependencies, and issues a
new result for the current revision/HEAD.

Do not use a premium builder for status/grep/typo when an authorized cheap lane
exists. Do not use Grok for semantic routing, code reading, transcript
surveillance or polling. Deterministic delivery uses the mechanical adapter;
Grok is an optional command relay, not a mandatory hop.

Keep existing repository-specific review and safety gates. Measure validated
task throughput, per-builder cost, Astra usage, Grok usage, User interventions,
review findings and rework separately; do not claim savings without observations.

---

# Repository-specific engineering rules (preserved)

# KIX Protocol — Agent Governance

These rules apply to Devin and every other coding agent working in this repository. They are repository-wide execution constraints. A task document may narrow scope further. If a task document, repository state, and these rules conflict or are ambiguous, stop and report the conflict instead of guessing.

## 1. Session start and source of truth

Before editing anything:

1. Run `git fetch origin main`.
2. Record the exact `origin/main` SHA as the observed current main.
3. Record the task branch HEAD.
4. Read, in this order:
   - `README.md`
   - `docs/DEVELOPMENT_PLAN.md`
   - `docs/status/BASELINES.md`
   - `docs/decisions/AUTHORITY_MODEL_1.md`
   - the current task document under `docs/tasks/`
   - any contracts/evidence explicitly named by that task.

`docs/DEVELOPMENT_PLAN.md` is the current authority for approved, deferred and prohibited implementation scope. Historical roadmaps are evidence, not current authorization.

If the task's stated base differs from current `origin/main`, report the delta. Do not silently rebase, merge main into the task branch, or change the task base.

## 2. One session = one task

One agent session handles exactly one task.

- Do not reuse a completed session for a new task.
- A new task uses a new task document, new branch and new agent session.
- Recommended naming:
  - task: `Task NNN`
  - branch: `agent/task-NNN-<short-name>`
  - validation: `validation/<date>-task-NNN-<short-name>/`

Do not begin the next task as cleanup, follow-up or "while here" work in the current session.

## 3. Task documents are immutable inputs

Files under `docs/tasks/` that define the current task are instructions, not agent-owned outputs.

Agents must not edit, rewrite, reinterpret, broaden or "clarify" the current task document.

If the task is ambiguous, contradictory, incompatible with repository state, or appears to require work outside its authorization:

- stop;
- report the exact ambiguity/conflict;
- ask for human clarification.

A task document may be created or revised only outside the executing agent session by an explicitly authorized human/orchestrator workflow.

## 4. Absolute locked files

Do not modify, reformat, rename, regenerate, or transiently mutate these files:

- `runtime/crates/kix-kernel/src/lib.rs`
  - required Git blob: `69564b166f0c27f9af5d8422f0a466b18d74c20f`
- `runtime/crates/kix-kernel/tests/quarantine_capacity.rs`
  - required Git blob: `b607996c83a119c349f1cc90469ac1ba82764e20`

Verify both blobs before and after work.

If a contract violation can only be fixed by changing a locked file:

- do not change the locked file;
- create the smallest useful reproduction using allowed files;
- report the blocker;
- do not propose merge.

The lock changes only through a separate explicit human decision.

## 5. Work not authorized by default

Unless a later explicit human authorization specifically permits it, do not perform:

- R2, custom replication, consensus, storage-engine or log-engine implementation;
- integration (a) of PR #11 wire/local-journal experiments onto v4;
- lifecycle release/GC, new terminal release behavior, evidence reclamation, review release or index changes;
- production PG/bank calls, live-money execution, or live Sui production execution;
- bulk KTX→KIX renaming or unrelated Cargo/workflow/lint/code hygiene sweeps;
- moving or deleting existing tags;
- force-moving, force-pushing or deleting existing branches;
- direct pushes to `main`;
- repository settings, branch protection, secrets, permissions or environment changes.

Creating a new task-specific branch is allowed.

Model 1 (chain authority / off-chain delegated execution) is the approved authority model, but the current v4 kernel is not a complete implementation of it.

## 6. Change discipline

- Prefer the smallest patch that proves the requested point.
- Do not rewrite historical evidence to make it look current.
- Do not claim that CI from an older SHA applies to a newer SHA.
- Do not convert source-informed tests into claims of clean-room independence.
- Do not infer production durability, distributed fencing, bank exactly-once, chain finality, legal compliance or production readiness from in-memory tests.
- Preserve stable operation identity, immutable first-result semantics, UNKNOWN handling, retained evidence and bounded-capacity distinctions unless the task explicitly changes an authorized contract.
- Never force-push or rebase reviewed history unless an explicit human instruction authorizes that exact operation.
- Test-count growth is not itself a completion criterion.

## 7. Existing coverage first

Before adding a new test, inspect and map the existing test/evidence coverage relevant to the requested behavior.

Classify each task requirement as:

- sufficiently covered;
- partially covered;
- not covered.

If existing deterministic regression coverage is sufficient, cite the exact test/path instead of duplicating it.

New tests should cover a documented gap, contract predicate, minimal reproduction or explicitly labeled characterization.

For `kix-kernel` tests:

- prefer public APIs and externally observable state;
- keep contract-invariant tests separate from the source-informed differential model where practical;
- avoid asserting private layout or implementation trivia unless the task explicitly requires characterization of that implementation.

## 8. Contract/implementation discrepancy rule

Classify a discovered behavior before changing anything:

### A. Contract-defined and matching

Proceed normally.

### B. Contract undefined

Record it as an observed characterization and remaining uncertainty. Do not invent semantics or amend authoritative contracts inside the task unless explicitly authorized.

The task may otherwise proceed.

### C. Explicit contract violation

Create the smallest useful reproduction.

Do not silently redefine the contract or change a locked file.

Stop the merge path and report a blocker. A PR containing diagnostic evidence may remain open, but the agent must not report it as merge-ready.

## 9. Testing and local verification

Run the narrow relevant tests first, then the broader repository verification required by the task.

For Rust work, use the `runtime/` workspace. At minimum, run relevant crate tests and the workspace tests covering changed code, plus Clippy/formatting when applicable.

Inspect the repository's GitHub Actions definitions and run the closest practical local equivalents.

Do not edit CI merely to make the current task pass.

Record commands actually run and their actual results. Do not fabricate or infer results.

## 10. Exact-head CI gate

After all task changes are committed, identify the exact final task head.

The agent may propose merge only after both workflows for that exact head are:

- KTX kernel verification: `status=completed`, `conclusion=success`
- KIX protocol verification: `status=completed`, `conclusion=success`

Rules:

- CI success from an earlier SHA does not transfer to a later SHA.
- If either workflow is `queued` or `in_progress`, report that exact state and wait. Do not propose merge.
- If either workflow fails, diagnose the failure. Make only a minimal in-task fix if authorized; otherwise report a blocker.

## 11. Avoid CI evidence self-reference loops

Do not create an infinite loop by committing CI run IDs for the same "final" head and thereby creating a new final head.

Use this pattern:

1. Implementation/evidence content may record CI for an earlier implementation head if useful.
2. Once the true final head is established, verify its exact-head CI.
3. Record the final-head CI result in the PR closure report/comment, not by creating another repository commit solely to record that final-head result.

Do not make a new commit merely to record the CI of the previous "final" commit unless a human explicitly asks for it.

## 12. Human-only merge authorization

Green CI means `merge-ready`; it does not authorize merge.

Without explicit human approval, the agent must not:

- mark a draft PR ready for review;
- merge a PR;
- squash or rebase-merge;
- create/move/delete tags;
- delete branches;
- push directly to `main`.

When human merge approval is given, the default is an ordinary merge commit preserving reviewed commit lineage. Squash, rebase merge or history rewriting require separate explicit approval.

## 13. Post-merge verification

After an authorized merge:

1. Record the merge commit SHA on `main`.
2. Verify the locked blobs again on the merge commit.
3. Verify the main/push CI for that merge commit through its terminal state.
4. End the task only after the required post-merge checks are complete.

If post-merge CI fails:

- do not patch `main` directly;
- report the failure and diagnosis;
- any corrective change must use a new task, new branch, new session and new PR.

## 14. Required evidence report

Every agent task report must include, as applicable:

- Task ID and task document path;
- exact observed `origin/main` / base SHA;
- implementation head SHA;
- exact final task head SHA;
- merge commit SHA if merged;
- complete changed-path list;
- locked blob verification before/after/final merge;
- existing coverage mapping;
- new tests/evidence and what gap each addresses;
- commands actually run and actual results;
- exact-head KTX CI run ID/status/conclusion;
- exact-head KIX protocol CI run ID/status/conclusion;
- post-merge main CI run ID/status/conclusion when merged;
- contract violations found;
- contract-undefined characterizations found;
- explicit non-claims / what was not tested;
- remaining uncertainty and follow-up candidates.

Do not fabricate test counts, performance numbers, hashes, CI run IDs or external verification.

## 15. Agent self-assessment limits

Report concrete acceptance criteria and evidence, not broad assurances.

Acceptable:
- "67 tests passed; locked blobs match; exact-head CI is green."

Not acceptable without separate evidence and authorization:
- "The kernel is safe."
- "This is production-ready."
- "Durability is solved."
- "The protocol is fully verified."

Agents should report `merge-ready`, `blocked`, `in_progress` or equivalent evidence-backed task state rather than an unqualified project-level verdict.

## 16. Security and credentials

Do not commit private keys, proving keys, credentials, non-public notes, local chain databases or secrets.

Do not broaden repository permissions or external-service access as part of an ordinary task.

Prefer separation between the coding agent and GitHub publication credentials where automation permits it.
