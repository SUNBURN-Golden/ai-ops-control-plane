# MAEUM_GYEOL project input

Central dispatch target. deployment_enabled=true as of 2026-09-27. Merge and production release stay separate approvals.



PROJECT: MAEUM_GYEOL
REPO: `BeautifulMind-JT/maeum-gyeol`
DEFAULT_BRANCH: `main`
SLACK_PROJECT: `#maeum`
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

TASK_SPEC_POLICY: `BLUEPRINT_AND_CANONICAL_ISSUE`
A0_POLICY: `EXPLICIT_AUTHORIZATION_ONLY`
POST_MERGE_POLICY: `NONE_UNLESS_REPO_RULE_REQUIRES`

Substantive work must point to `README.md`, `docs/BLUEPRINT.md`, and the
applicable release/implementation document.

Auth/RLS, private-original/public-copy separation, server drafts,
idempotency/retry/response-loss recovery, storage/photo privacy, deletion and
migration rules remain authoritative.

Implementing an existing RLS/security policy is A2-capable work inside approved
boundaries. Changing who may read/write data or changing an authority/privacy
boundary is a consequential contract change and requires pre-implementation
Astra analysis + User decision (A3/authority boundary).

If PROJECT MAP or required actor/reviewer configuration is missing:
`[BLOCKED] Reason: CONTROL_PLANE_NOT_CONFIGURED`

Do not guess.

