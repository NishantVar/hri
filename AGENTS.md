# RIView

RIView turns a markdown spec + structured decisions JSON into an interactive HTML review, lets a human or agent submit a review delta, and applies that delta back into the spec in place. Git is the per-revision history.

Entry points:
- [README.md](README.md) — how to use the renderer, applier, CLI, and daemon
- [SCHEMA.md](SCHEMA.md) — data model for nodes, review deltas, and apply metadata
- [docs/adr/](docs/adr/) — decisions and the trade-offs behind them

## Language

**Spec**:
A `<basename>.md` + `<basename>.decisions.json` pair. The unit RIView operates on.
_Avoid_: document, file

**Basename**:
The shared stem for a spec's two files (default `spec`; passed via `--basename mvp` to point at `mvp.md` + `mvp.decisions.json`).

**Sidecar**:
Shorthand for the `<basename>.decisions.json` half of a spec.

**Node**:
A single entry in the sidecar — one decision, ambiguity, or risk — keyed by a stable ID.
_Avoid_: card (the renderer's word), item, entry

**Anchor**:
The HTML-comment marker pair `<!-- node:<id> -->` … `<!-- /node:<id> -->` in the markdown that delimits a node's prose body. The applier replaces content between matched anchors.

**Decision / Ambiguity / Risk**:
The three node kinds. Each has its own status enum: a `decision` is `ai-confident | confirmed | rejected | needs-work`; an `ambiguity` is `open | resolved | deferred`; a `risk` is `open | accepted | mitigated | dismissed`.

**Review delta**:
The JSON file a reviewer produces — a list of per-node updates (`new_status`, `comment`, `resolution`, `body_edit`) targeting a specific `spec_id` + `spec_version`. Consumed by `apply.py` or POSTed to the daemon.
_Avoid_: review (ambiguous — could mean the per-node `review` block on a node)

**Apply pass**:
One run of `apply.py` that merges a review delta into a spec, bumps `version`, refreshes `applied_from_review`, and overwrites both files in place.

**Session**:
A registered review thread for one spec (one `spec_id` + basename), stored under `~/.riview/sessions/<session-id>/`. Carries the spec's submitted revisions and the per-revision review JSON.

**Revision**:
A numbered snapshot of a spec inside a session (`revisions/N/source.md` + `decisions.json`). Distinct from `version` on the sidecar — `version` is the applier's bumped counter; `revision` is the daemon's submission counter.

**Session inbox**:
The daemon's cross-project store at `~/.riview/`. Holds many sessions (from many projects) and serves them on one localhost port.

**Daemon**:
The localhost HTTP server bound to `127.0.0.1:7891` that exposes the browser UI and the `/sessions/<id>/{review,events,wait}` endpoints. Optional — the CLI works without it.

## Relationships

- A **Spec** has many **Nodes**; each node has exactly one **Anchor** in the markdown.
- A **Review delta** targets one **Spec** at one `spec_version`.
- An **Apply pass** consumes one **Review delta** and produces the next `spec_version` of one **Spec**.
- A **Session** has many **Revisions** (spec snapshots) and many reviews (one per revision). POSTing a review merges by `node_id` into the current revision's review.

## Flagged ambiguities

- "review" alone is ambiguous: it can mean (a) the **review delta** file, (b) the per-node `review` block written by the applier on each node, or (c) the act of reviewing. Prefer the qualified terms.
- "version" vs "revision": `version` is the applier's monotonic counter on the sidecar; `revision` is the daemon's per-session submission counter. Different counters, different rates of change.
