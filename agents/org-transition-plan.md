# RIView org transition plan

This document records the approved target agent org for this RIView repo, the active bridge between the current state and that target, the roles and folders we have explicitly deferred, and the moves already completed under this transition.

The canonical agent-instructions file for this repo is [AGENTS.md](../AGENTS.md); [CLAUDE.md](../CLAUDE.md) is a symlink to it. Agent definitions live under [agents/](./).

## Approved target - starter roster

Three starter agents, no further splits yet.

### 1. riview-core-agent

- **Definition:** [riview-core-agent.md](riview-core-agent.md)
- **Owns:** RIView core implementation and technical contract: `scripts/`, [SCHEMA.md](../SCHEMA.md), [README.md](../README.md), [docs/adr/](../docs/adr/), `sample/`, `tests/`.
- **Boundaries:** does not own `skills/riview-respond/` as a separate deliverable; does not own standing browser QA execution.
- **Tools:** unittest/pytest, RIView CLI, daemon, fixtures.
- **Evidence:** `scripts/render.py`, `scripts/riview.py`, `scripts/apply.py`, twelve ADRs, schema, sample fixtures.

### 2. responder-skill-agent

- **Definition:** [responder-skill-agent.md](responder-skill-agent.md)
- **Owns:** `skills/riview-respond/` and its helper tools.
- **Boundaries:** does not own daemon/session internals except where the skill contract depends on them.
- **Tools:** skill docs, preflight/snapshot helpers, RIView CLI.
- **Evidence:** [docs/adr/0010-responder-skill-lives-in-repo.md](../docs/adr/0010-responder-skill-lives-in-repo.md), [skills/riview-respond/SKILL.md](../skills/riview-respond/SKILL.md).

### 3. riview-qa-agent

- **Definition:** [riview-qa-agent.md](riview-qa-agent.md)
- **Owns:** execution and maintenance of the browser/daemon QA plan: `docs/qa/`, `docs/qa/qa-plan.html` (after rehome), QA reports under `tmp/` unless later promoted, `tmp/chrome-qa`, `tmp/node-cdp` experiments.
- **Boundaries:** files implementation defects to riview-core-agent; files responder-skill defects to responder-skill-agent.
- **May:** update `docs/qa/qa-plan.md` wording and scenarios, including replacing "Human-driven" with "Agent-executed".
- **Tools:** browser/CDP automation, curl, pytest/unittest, RIView daemon.
- **Evidence:** `docs/qa/qa-plan.md`, `docs/qa/qa-plan.html`, `tmp/chrome-qa/`, `tmp/node-cdp/`.

## Active bridge items

- `agents/` is now the visible local agent config directory. A symlink `.agents -> agents` exists for tooling that expects the dot-prefixed name.
- The global convention also wants `.claude -> agents`. That symlink is **not** created here because this repo already has a real `.claude/` directory (used for worktrees), and the approved plan forbids replacing it. Tools that read `.claude/` for agent config will need either project-local configuration pointing at `agents/`, or an out-of-tree convention update.
- `docs/qa/qa-plan.html` has been rehomed from `humans/qa-plan.html`. The empty `humans/` directory has been removed.
- `tmp/` is now gitignored. Its contents (`tmp/chrome-qa/`, `tmp/node-cdp/`, QA driver reports, this transition's scratch files) remain on disk but stay out of git history.

## Operating rules for ownership boundaries

These rules are part of the org design. Update them before taking work that would otherwise cross an unclear boundary.

### Org artifacts

- `agents/org-transition-plan.md` is owned by Maya, the org-designer. It records approved role boundaries, bridge items, deferrals, and completed org moves.
- Files under `agents/<role>.md` are owned by the role they define for factual upkeep, but changes that alter ownership boundaries, create or retire roles, or move artifact homes require Maya review and an org-transition-plan update.
- Ari, the agent-architect-agent, implements approved agent scaffolding and role-file changes. Ari does not independently redefine ownership.

### Cross-surface edits

- The file's primary owner owns the final edit, even when the content discusses another role's surface.
- A role whose contract changes must file or draft the needed cross-reference updates for the owning role. Example: responder-skill-agent owns the `riview-respond` contract, but README and ADR updates about that contract land through riview-core-agent because those docs are core-owned.
- If two roles disagree about whether a file belongs to one surface or another, pause the implementation and update this plan instead of letting the next code change decide by accident.

### Tests and QA findings

- `tests/` is a core-owned tracked regression suite. riview-core-agent owns its structure and long-term maintenance.
- riview-qa-agent may propose or draft tests that encode QA findings, but tracked test additions under `tests/` should be reviewed and accepted by riview-core-agent.
- Browser/daemon scenario execution, QA notes, and one-off run reports stay with riview-qa-agent under `tmp/` unless promoted.
- Promote a QA artifact out of `tmp/` only when it becomes repeatable project documentation or a reusable harness. Repeatable procedures belong under `docs/qa/`; reusable automation may justify a future `qa/harness/` bridge item.

### Documentation overlap

- README, SCHEMA, and ADRs are core-owned even when they describe responder-skill or QA behavior.
- The subject-matter role is accountable for noticing stale cross-references in core-owned docs and filing the correction to riview-core-agent.
- `docs/qa/` is QA-owned. Core and responder-skill agents may file defects against it when implementation behavior changes, but riview-qa-agent owns the plan's wording and scenario shape.

## Explicit deferrals

The following are intentionally **not** created or moved in this pass:

- New top-level folders: `product/`, `specs/`, `design/`, `qa/harness/`, `release/`, `agents/decisions/`.
- No move of `docs/adr/`; ADRs stay where they are.
- No further role splits yet: `renderer-ui-agent`, `runtime-daemon-agent`, `schema-steward-agent`, `docs-agent`, `release-agent`, and `research-agent` remain folded into the three starter roles above.
- No code refactors as part of this transition.

These are deferred, not rejected. They can be revisited once the starter roster has run for a while and we can see which boundaries actually chafe.

## Completed moves

- Created `agents/` with starter definitions for the three roles above and this plan.
- Created `.agents -> agents` symlink.
- Did **not** create `.claude -> agents` symlink (conflict with existing real `.claude/`).
- Moved `humans/qa-plan.html` to `docs/qa/qa-plan.html`.
- Removed empty `humans/` directory.
- Added `tmp/` to `.gitignore`.

No commit was made as part of the transition write; staging is left for the user.
