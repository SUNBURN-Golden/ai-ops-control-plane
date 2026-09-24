# FILM_UNIT project input

Source-only profile; no deployment authorization.



PROJECT: FILM_UNIT
REPO: `BeautifulMind-JT/film-unit-mv-studio`
DEFAULT_BRANCH: `main`
SLACK_PROJECT: `#film-unit`
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

Production/content approvals are separate from engineering gates.
Lyrics/subtitle timing, LOCK, asset provenance, renderer/budget approval,
build-history/reproducibility and Preview/Final rules remain authoritative.
An engineering PASS does not authorize paid rendering, asset submission, timing
LOCK, or Final production approval.

If PROJECT MAP or required actor/reviewer configuration is missing:
`[BLOCKED] Reason: CONTROL_PLANE_NOT_CONFIGURED`

Do not guess.

