# RIView — interactive spec review

A small Python pipeline that turns a markdown spec + structured decisions sidecar into an interactive HTML review, then applies the reviewer's responses back to produce a new revision. No dependencies — Python 3.10+ stdlib only, runs everywhere.

## Concept

A spec is two files:

- `<basename>.md` — human-readable narrative.
- `<basename>.decisions.json` — structured graph of nodes (decisions, ambiguities, risks) with stable IDs, statuses, confidences, and per-node metadata.

The default basename is `spec` (the demo uses `sample/spec.md`); pass `--basename mvp` to point at `mvp.md` + `mvp.decisions.json` living next to your other design docs.

The graph is what the agent uses for structured reasoning; the markdown is what humans read. Anchor comments (`<!-- node:<id> -->`) tie them together so the applier can do surgical body edits without disturbing surrounding text.

Reviewing happens in a self-contained HTML page (no server, no deps): per-node form controls (status dropdowns, comment boxes, ambiguity resolvers) feed a "Review Delta" JSON blob that the reviewer pastes or downloads.

The applier ingests that JSON and produces `spec.rev<N>.md` / `spec.rev<N>.decisions.json` alongside the originals. Originals are never mutated. Each touched node gets a `review` metadata block tracing back to the source JSON.

## Quick start

```bash
# 1. Render the demo spec to interactive HTML
python3 scripts/render.py sample
open sample/spec.html       # macOS; or just open the file in any browser

# 2. In the browser: change statuses, write comments, resolve ambiguities.
#    Click "Export Reviews" → Copy or Download .json

# 3. Apply the reviewer's deltas (replace path with the downloaded file)
python3 scripts/apply.py sample sample/review-demo.json

# 4. Inspect the new revision
ls sample/spec.rev1.*
diff sample/spec.md sample/spec.rev1.md

# 5. Re-review against rev1 without copying files
python3 scripts/render.py sample --latest    # renders spec.rev1.html
```

`sample/` ships with `review-demo.json` and `review-demo-2.json` to drive the loop, and `sample/expected/` holds canonical rev outputs so you can diff against your local run.

### Real spec (custom basename)

To review a real spec living next to other docs — for example `habits/design/mvp.md` + `mvp.decisions.json`:

```bash
python3 riview/scripts/render.py design --basename mvp           # renders design/mvp.html
# review in browser, export delta to /tmp/mvp-review.json
python3 riview/scripts/apply.py design /tmp/mvp-review.json --basename mvp
python3 riview/scripts/render.py design --basename mvp --latest  # renders design/mvp.rev1.html
```

Each spec dir can hold multiple basenames side by side — `mvp.rev1.*` and `other.rev1.*` won't collide. Add a `.gitignore` to ignore the generated `*.html` / `*.rev*.{md,decisions.json}` while keeping the base files tracked.

## Schema

See [SCHEMA.md](SCHEMA.md). The short version:

- Each node has `id`, `kind` (`decision` | `ambiguity` | `risk`), `status`, `confidence`, `depends_on[]`, `source_anchor`.
- Kind-specific fields: decisions carry `rationale` + `alternatives`; ambiguities carry `prompt` + `options[]`; risks carry `severity` + `mitigation`.
- After review, each touched node has a `review` block: `{comment, status_before, status_after, resolution?, body_edited, reviewed_at, review_source}`.

## Workflow

```
   <basename>.md + <basename>.decisions.json
              │
              ▼ render.py [--basename NAME] [--rev N | --latest]
        <basename>.html or <basename>.rev<N>.html (open in browser)
              │
              ▼ reviewer fills form, exports JSON
        review-*.json
              │
              ▼ apply.py [--basename NAME]
   <basename>.rev<N>.md + <basename>.rev<N>.decisions.json
              │
              ▼ rerender (render.py --latest) and review again
```

To start a second review cycle on top of rev1: `python3 scripts/render.py sample --latest` renders the newest revision. `--rev N` targets a specific one.

Each rev sidecar records `applied_from_review.review_path` and `applied_from_review.body_edits`, so you can always trace which JSON produced which rev and which anchors it touched. The rev sidecar's `source_path` points at the rev's own markdown (`spec.rev<N>.md`).

## Session inbox (daemon)

The file-and-path workflow above is fine for one spec at a time, but multiple
agents working in parallel need a shared place to drop reviews into. The
**session inbox** is RIView's cross-project store: each registered review is a
*session* under `~/.riview/sessions/<session-id>/`, with revisioned spec history
(`revisions/N/source.md` + `decisions.json`) and revisioned review history
(`reviews/N/review.json`).

This README documents **slice 1a** — the on-disk model and the agent-facing CLI.
The HTTP daemon that exposes a browser UI on `http://127.0.0.1:7891/` lands in
slice 1b; the CLI works standalone without it.

### Layout

```
~/.riview/
  sessions/
    <session-id>/
      meta.json              # session_id, project_path, basename, spec_id, spec_title,
                             # status, current_revision, content hashes per revision, ...
      revisions/
        1/  source.md  decisions.json  submitted_at
        2/  source.md  decisions.json  submitted_at
      reviews/
        1/  review.json  submitted_at
```

Override the storage root with `RIVIEW_HOME=/path/to/dir` (tests use this).

### Session lifecycle

- `awaiting_review` — a revision has been submitted but no review exists for it yet.
- `review_submitted` — a review has been recorded against the current revision.
- `applied` — the agent has picked the review up (either by calling `applied` explicitly or by submitting a newer revision).
- `closed` — manually dismissed.

`pull` is **idempotent**: it returns the latest submitted review for the current
revision every call, never consumes. The agent advances the session forward by
submitting a new revision with `--session <id>` after applying the review.

### CLI

```bash
# Register a spec as a new session (prints session_id + URL).
python3 riview/scripts/riview.py submit design --basename mvp

# Idempotent re-submit (identical content) returns the existing revision.
# Changed content advances to revision 2 inside the same session.
python3 riview/scripts/riview.py submit design --basename mvp --session <id>

# Record a review JSON against the session's current revision.
# (In slice 1b this happens via the browser; the CLI hook is here for testing.)
python3 riview/scripts/riview.py submit-review <id> /path/to/review.json

# Print the latest review for the current revision (exits 4 if none).
python3 riview/scripts/riview.py pull <id>

# Mark a session as applied (after the agent applies a pulled review).
python3 riview/scripts/riview.py applied <id>

# List open sessions across all projects (--all includes closed).
python3 riview/scripts/riview.py list

# Show full meta.json, print the daemon URL, or close the session.
python3 riview/scripts/riview.py status <id>
python3 riview/scripts/riview.py open <id>
python3 riview/scripts/riview.py dismiss <id>
```

Exit codes: `0` ok, `2` bad input, `3` session not found, `4` no review for current revision.

Smoke tests live at `riview/tests/test_session.py`:

```bash
python3 -m unittest riview.tests.test_session
```

## Design choices

- **Anchors are HTML comments.** Invisible when rendered as markdown by any other tool. Surgical body edits possible without touching unrelated text. Comment-only edits guaranteed.
- **The renderer is one file.** Self-contained HTML — inline CSS, inline JS, no CDN. Works offline, prints sensibly, respects dark mode.
- **The applier is non-destructive.** New rev files, never overwrites. Each rev metadata back-references the review JSON that produced it.
- **Determinism over polish.** Review deltas sort `reviews[]` by `node_id`. Anchor blocks are rewritten with minimal whitespace churn. Stable IDs are the contract; everything else is a render concern.
- **No spatial graph view.** Cards are linear, real DOM. A spatial/zoomable variant (react-flow, tldraw, or HTML-in-Canvas) is a v2 swap on the same data model — out of MVP scope.

## Limitations / explicit non-goals (MVP)

- No live file watcher; review delivery is copy/paste or file save.
- No multi-user merge — one reviewer at a time.
- No freeform ink/handwriting capture.
- No automated source-spec re-generation; the applier mutates the structured graph + body anchors but doesn't re-run the authoring agent.
- Only the most recent `review` per node is preserved across revs. `apply.py` overwrites `node.review` each pass; older review history lives in the prior rev files. A `review_history[]` extension would be a schema-level addition.

## Files

| Path                                  | What it is                                       |
|---------------------------------------|--------------------------------------------------|
| `SCHEMA.md`                           | Data-model reference                             |
| `scripts/render.py`                   | Spec → interactive HTML                          |
| `scripts/apply.py`                    | Review delta → `spec.rev<N>.*`                   |
| `scripts/riview.py`                   | Session inbox CLI (`submit/list/pull/...`)       |
| `tests/test_session.py`               | Smoke tests for the session model                |
| `sample/spec.md`                      | Demo: Pomodoro Timer MVP, markdown view          |
| `sample/spec.decisions.json`          | Demo: structured graph (6 nodes)                 |
| `sample/review-demo.json`             | Demo review delta exercising every code path     |
| `sample/review-demo-2.json`           | Second review for chain testing                  |
| `sample/expected/`                    | Canonical outputs for the demo (rev1 + rev2)     |
| `sample/spec.html` / `spec.rev*`      | Generated locally by the quick-start; not in git |
