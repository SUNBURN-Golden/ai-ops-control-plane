# ZARI project input

Central dispatch target. deployment_enabled=true as of 2026-09-27. Merge and production release stay separate approvals.



PROJECT: ZARI
REPO: `BeautifulMind-JT/ZARI`
DEFAULT_BRANCH: `main`
SLACK_PROJECT: `#zari`
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

TASK_SPEC_POLICY: `REPO_DOCS_AND_CANONICAL_ISSUE`
A0_POLICY: `EXPLICIT_AUTHORIZATION_ONLY`
POST_MERGE_POLICY: `NONE_UNLESS_REPO_RULE_REQUIRES`

Existing ZARI repository rules remain mandatory, including
`IMPLEMENTATION_STATUS.md` completion reporting.

A0 eligibility is based on approved change kind, not file count.
Mandatory bookkeeping such as IMPLEMENTATION_STATUS updates does not by itself
promote an otherwise authorized documentation typo/format task, but those
bookkeeping updates must still be performed.

Rust authority, preserved source prompts/manifests, unknown handling,
PlanSnapshot consistency and design verification rules remain authoritative.

If PROJECT MAP or required actor/reviewer configuration is missing:
`[BLOCKED] Reason: CONTROL_PLANE_NOT_CONFIGURED`

Do not guess.

