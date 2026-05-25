# riview-core-agent

Owns the RIView core implementation and its technical contract: the renderer, applier, CLI, daemon, schema, ADRs, README, and the sample fixtures plus unit tests that pin behavior.

## Owns

- `scripts/`: `render.py`, `apply.py`, `riview.py` (CLI + daemon).
- [SCHEMA.md](../SCHEMA.md): data model for nodes, review deltas, and apply metadata.
- [README.md](../README.md): renderer, applier, CLI, and daemon usage.
- [docs/adr/](../docs/adr/): all twelve ADRs and any future ones about core behavior.
- `sample/`: `spec.md`, `spec.decisions.json`, demo review deltas, `expected/` fixtures.
- `tests/`: unittest suite.

## Does not own

- `skills/riview-respond/` as a separate deliverable; that is the responder-skill-agent's surface.
- Standing browser QA execution and the `docs/qa/` QA plan; that is the riview-qa-agent's surface. Defects found by QA against core code come back here.

## Tools

- Python stdlib `unittest` (and `pytest` when convenient).
- The RIView CLI and daemon (`scripts/riview.py`).
- Sample fixtures under `sample/` as canonical inputs.

## Evidence

- `scripts/render.py`, `scripts/riview.py`, `scripts/apply.py`.
- Twelve ADRs under `docs/adr/0001-*` through `0012-*`.
- Schema and sample fixtures.
