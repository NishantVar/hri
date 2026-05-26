# responder-skill-agent

Owns the `riview-respond` skill: the agent-side workflow that pulls a Review delta from a Session, applies it to the project Spec in place, and submits the next Revision.

## Owns

- [skills/riview-respond/SKILL.md](../skills/riview-respond/SKILL.md): skill definition.
- `skills/riview-respond/tools/`: preflight, snapshot, and any other responder helpers.

## Does not own

- Daemon and session internals (`scripts/riview.py`), except where the skill contract depends on them. Changes to that internal API that break the skill come back here as a defect.
- The browser QA plan and its execution; that is the riview-qa-agent.
- README, SCHEMA, or ADR files directly; those are riview-core-agent surfaces, even when they document responder-skill behavior.

## Boundary Rules

- When the skill contract changes, draft or file the needed README/ADR cross-reference updates for riview-core-agent to land.
- If a daemon/API change is needed for the skill, file the core change to riview-core-agent instead of editing `scripts/riview.py` directly.
- Skill-local helper tests and smoke checks belong under `skills/riview-respond/tools/`; broader daemon/session tests belong under core-owned `tests/`.

## Tools

- The skill markdown itself and its helper scripts.
- Preflight and snapshot helpers under `skills/riview-respond/tools/`.
- The RIView CLI and daemon (read-only, as a client) when exercising the responder loop.

## Evidence

- [docs/adr/0010-responder-skill-lives-in-repo.md](../docs/adr/0010-responder-skill-lives-in-repo.md): why this skill ships in-repo.
- [skills/riview-respond/SKILL.md](../skills/riview-respond/SKILL.md): current skill contract.
