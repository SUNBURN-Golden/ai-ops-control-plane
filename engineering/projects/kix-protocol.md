# KIX project input

Central dispatch target. deployment_enabled=true as of 2026-09-27. Merge and production release stay separate approvals.



PROJECT: KIX
REPO: `BeautifulMind-JT/kix-protocol`
DEFAULT_BRANCH: `main`
SLACK_PROJECT: `#kix`
SLACK_CONTROL: `#ai-control`
SLACK_DECISIONS: `#ai-decisions`
SLACK_AUDIT: `#ai-audit`

OPERATING_MODE: `MANUAL_ONLY`
AUTOMATED_ACTION_ADAPTER: `MECHANICAL`
GROK_EVENT_OVERRIDES: `NONE`

DEFAULT_EXECUTION_CLASS: `BUILDER_STANDARD`
DEFAULT_BUILDER_ID: `CONFIG_REQUIRED`
DEFAULT_AUDIT_FLOOR: `A1`
DEFAULT_ASTRA_GATE: `NONE`
REVIEW_POLICY: `REQUIRED_NON_A0`
REVIEWER_LANE_ID: `CONFIG_REQUIRED`

TASK_SPEC_POLICY: `KIX_DOCS_TASK_REQUIRED`
A0_POLICY: `EXPLICIT_AUTHORIZATION_ONLY; NEVER_LOCKED_FILES`
POST_MERGE_POLICY: `REPOSITORY_RULES_REQUIRED`

The task envelope is only an orchestration envelope.
The immutable execution specification remains the applicable file under
`docs/tasks/` with an exact blob/SHA pointer.

A control-plane decision does not silently rewrite that task document.
If scope/contract changes require a task-spec revision, that revision is created
outside the executing agent session by the already-authorized repository
workflow, then TASK_REVISION is incremented before resume.

Existing KIX locked-file and prohibited-work rules remain absolute.

KIX compatibility rules:

- legacy KIX wording that green exact-head CI is "merge-ready" means only that
  the KIX CI gate itself is satisfied inside this control plane; global
  READY_FOR_MERGE additionally requires independent review + any required Astra gate +
  current-task/current-HEAD predicates;
- legacy instructions to "wait" for queued/in-progress CI do not authorize a
  standing Grok session or polling. The mechanical layer waits for the next
  GitHub event and then resumes the state machine;
- generic same-owner CI feedback applies only before merge. Post-merge KIX
  failure follows the preserved new-task/session/branch/PR rule.

If PROJECT MAP or required actor/reviewer configuration is missing:
`[BLOCKED] Reason: CONTROL_PLANE_NOT_CONFIGURED`

Do not guess.

