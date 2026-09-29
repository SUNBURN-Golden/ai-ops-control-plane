# KIX_COMMERCE project input

Central dispatch target registered on 2026-09-29 (User decision D7, `docs/PROGRAM_MODE.md` §9). `deployment_enabled=true` means the target is eligible. Host admission still requires `BeautifulMind-JT/kix-commerce-apps` in the host policy `allowed_repositories`. Merge and production release stay separate approvals.

PROJECT: KIX_COMMERCE
REPO: `BeautifulMind-JT/kix-commerce-apps`
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

TASK_SPEC_POLICY: `REPO_DOCS_AND_CANONICAL_ISSUE`
A0_POLICY: `EXPLICIT_AUTHORIZATION_ONLY`
POST_MERGE_POLICY: `REPOSITORY_RULES_REQUIRED`

Repository rules in `kix-commerce-apps` remain mandatory. So do its locked files and every contract shared with `kix-protocol`, such as payment, settlement and protocol interfaces.
Any change that touches a `kix-protocol` contract is at least A2. It needs Astra analysis and a User decision whenever the approved protocol contract would change.

If PROJECT MAP or required actor/reviewer configuration is missing:
`[BLOCKED] Reason: CONTROL_PLANE_NOT_CONFIGURED`

Do not guess.
