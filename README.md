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
- The renderer doesn't yet display past `review` blocks (only fresh review forms). Re-reviewing a rev shows fresh-form UI; prior comments live in the JSON.

## Files

| Path                                  | What it is                                       |
|---------------------------------------|--------------------------------------------------|
| `SCHEMA.md`                           | Data-model reference                             |
| `scripts/render.py`                   | Spec → interactive HTML                          |
| `scripts/apply.py`                    | Review delta → `spec.rev<N>.*`                   |
| `sample/spec.md`                      | Demo: Pomodoro Timer MVP, markdown view          |
| `sample/spec.decisions.json`          | Demo: structured graph (6 nodes)                 |
| `sample/review-demo.json`             | Demo review delta exercising every code path     |
| `sample/review-demo-2.json`           | Second review for chain testing                  |
| `sample/expected/`                    | Canonical outputs for the demo (rev1 + rev2)     |
| `sample/spec.html` / `spec.rev*`      | Generated locally by the quick-start; not in git |
