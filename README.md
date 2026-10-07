# ai-ops-control-plane

**Lawhelpers / BeautifulMind AI Ops Control Plane** — governance, bot registry, workflows, and knowledge pipeline for JunTae's Grok Bot org.

This repo is the **operating system layer**: who may do what, where truth lives, how work is handed off, and how knowledge is promoted. It is not a case file store and not a CRM.

**AIOPS Mac development candidate:** [설치·사용 안내](engineering/mac_app/README_KO.md) · [자율 개발 모드의 범위와 사용자 결정](engineering/docs/MAC_APP_AUTONOMY_KO.md). A local app UI, configurable Codex/Claude/Cursor/GLM/Grok Build/Devin planning/build/review/inspection roles, and a bot CLI/MCP drive work toward final human acceptance. Actual Mac installation and provider/host qualification remain required; this does not activate or replace the existing protected Linux runtime.

[Mac 호스트 채택의 조건](engineering/docs/MAC_HOST_ADOPTION_KO.md): VM runner routing remains disabled until a shared admission authority is implemented and qualified. Mac generations bind the durable User decision and reject open canonical Linux ownership; this source candidate still requires its new-head A3 audit.

---

## 목적 / Purpose

- Single place for org constitution, approval gates, Source-of-Truth map, and bot inventory
- Make routing and approval **operable** (not tribal knowledge)
- Keep client PII, court originals, and secrets **out** of GitHub

---

## Source of Truth map (pointer)

See [`SOURCE_OF_TRUTH.md`](./SOURCE_OF_TRUTH.md):

| Domain | SoT |
|--------|-----|
| Code / policy / playbooks | **GitHub** (this control plane + project repos) |
| Client / case / deadlines / comms state | **CRM/CASE** ledger |
| Original legal documents | **OneDrive** — absolute **READ ONLY** |
| Bot memory | **Auxiliary only** — never sole record of mutable facts |

Also: [`ONEDRIVE_READ_ONLY_POLICY.md`](./ONEDRIVE_READ_ONLY_POLICY.md)

---

## Core governance links

| Doc | Role |
|-----|------|
| [`SYSTEM_CONSTITUTION.md`](./SYSTEM_CONSTITUTION.md) | Roles, hard rules, promotion criteria |
| [`APPROVAL_MATRIX.md`](./APPROVAL_MATRIX.md) | L0–L3 gates + required evidence |
| [`SECURITY_BOUNDARIES.md`](./SECURITY_BOUNDARIES.md) | Shared box, least privilege, ChatGPT ban |
| [`bots/registry.yaml`](./bots/registry.yaml) | Live bot inventory (authoritative IDs/status) |
| [`workflows/`](./workflows/) | Task lifecycle, handoff, approval, incident |
| [`knowledge/playbooks/KNOWLEDGE_PROMOTION.md`](./knowledge/playbooks/KNOWLEDGE_PROMOTION.md) | Observation → Promotion pipeline |
| [`docs/SMOKE_TESTS.md`](./docs/SMOKE_TESTS.md) | Routing-only smoke A/B/C (wait for JunTae OK) |
| [`audits/`](./audits/) | Cross-audit snapshots |
| [`engineering/`](./engineering/README.md) | Engineering control plane for product repos: builder dispatch, host admission, review gates ([`AGENTS.md`](./engineering/AGENTS.md), target profiles in `engineering/.github/control-plane/projects.json`) |

---

## How to propose a change

1. **Draft** the change in a branch or as a clear PR description (policy text, registry edit, workflow gap-fill).
2. Link the affected SoT docs (constitution / approval / registry / workflow).
3. State **approval level** (L0–L3) and evidence you will attach.
4. **Do not** merge policy that relaxes gates, grants new bot permissions, or adds PII — JunTae explicit OK required for L2/L3 and for any permission change.
5. After merge to `main`, note the commit SHA in the relevant audit or task record.

Chat proposals are not done until they land in GitHub (or the designated SoT).

---

## No-PII / secrets rule (hard)

**NEVER** put in this repo:

- Client PII, names+contact dumps, RRN (주민등록번호), bank accounts
- Court originals or full pleadings
- API keys, PATs, passwords, session cookies, `.env` contents

Use redacted examples, fake IDs, or pointers to CRM/CASE / OneDrive paths (read-only) outside the repo.

---

## Layout (quick)

```
bots/registry.yaml     # ACTIVE bots + channels
workflows/             # operational playbooks
knowledge/playbooks/   # promotion rules
schemas/               # Task Object schema
routines/              # scheduled jobs registry
docs/                  # smoke tests, inventories
audits/                # dated cross-audits
skills/                # domain skill stubs
engineering/           # engineering control plane (dispatch, host admission, gates)
tests/                 # offline checks for tools/ and the registries
```

---

## OneDrive

Absolute **READ ONLY** for every bot/worker/routine. Document only; never mutate. See `ONEDRIVE_READ_ONLY_POLICY.md`.

---

*Owner: JunTae Park · Repo: SUNBURN-Golden/ai-ops-control-plane*
