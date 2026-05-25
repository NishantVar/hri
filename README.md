# RIView — interactive spec review

A small Python pipeline that turns a markdown spec + structured decisions sidecar into an interactive HTML review, then applies the reviewer's responses back to produce a new revision of the same files. No dependencies — Python 3.10+ stdlib only, runs everywhere.

## Concept

A spec is two files:

- `<basename>.md` — human-readable narrative.
- `<basename>.decisions.json` — structured graph of nodes (decisions, ambiguities, risks) with stable IDs, statuses, confidences, and per-node metadata.

The default basename is `spec` (the demo uses `sample/spec.md`); pass `--basename mvp` to point at `mvp.md` + `mvp.decisions.json` living next to your other design docs.

The graph is what the agent uses for structured reasoning; the markdown is what humans read. Anchor comments (`<!-- node:<id> -->`) tie them together so the applier can do surgical body edits without disturbing surrounding text.

Reviewing happens in a self-contained HTML page (no server, no deps): per-node form controls (status dropdowns, comment boxes, ambiguity resolvers) feed a "Review Delta" JSON blob that the reviewer pastes or downloads.

The applier ingests that JSON and overwrites `<basename>.md` / `<basename>.decisions.json` in place (atomic write: tempfile → fsync → rename, plus a best-effort parent-directory fsync). Each pass bumps `version`, refreshes per-node `review` metadata, and records an `applied_from_review` audit block. Each file is crash-hardened individually; applying a review is not a transaction across the two files. Git tracks the history.

## Quick start

```bash
# 1. Render the demo spec to interactive HTML
python3 scripts/render.py sample
open sample/spec.html       # macOS; or just open the file in any browser

# 2. In the browser: change statuses, write comments, resolve ambiguities.
#    Click "Export Reviews" → Copy or Download .json

# 3. Apply the reviewer's deltas (replace path with the downloaded file).
#    apply.py overwrites sample/spec.md + sample/spec.decisions.json in place.
python3 scripts/apply.py sample sample/review-demo.json

# 4. Inspect what changed (git is the history)
git diff sample/spec.md sample/spec.decisions.json

# 5. Re-render against the updated spec and review again
python3 scripts/render.py sample
```

`sample/` ships with `review-demo.json` and `review-demo-2.json` to drive the loop, and `sample/expected/` holds canonical outputs (`after-review-1.*`, `after-review-2.*`) so you can diff against your local run before committing.

### Real spec (custom basename)

To review a real spec living next to other docs — for example `path/to/specs/mvp.md` + `mvp.decisions.json`:

```bash
python3 scripts/render.py path/to/specs --basename mvp           # renders path/to/specs/mvp.html
# review in browser, export delta to /tmp/mvp-review.json
python3 scripts/apply.py path/to/specs /tmp/mvp-review.json --basename mvp
python3 scripts/render.py path/to/specs --basename mvp           # renders against the updated spec
```

Each spec dir can hold multiple basenames side by side — `mvp.{md,decisions.json}` and `other.{md,decisions.json}` won't collide. Add a `.gitignore` for the generated `*.html` files; the `.md` + `.decisions.json` pair is the canonical, git-tracked spec.

## Schema

See [SCHEMA.md](SCHEMA.md). The short version:

- Each node has `id`, `kind` (`decision` | `ambiguity` | `risk`), `status`, `confidence`, `depends_on[]`, `source_anchor`.
- Kind-specific fields: decisions carry `rationale` + `alternatives`; ambiguities carry `prompt` + `options[]`; risks carry `severity` + `mitigation`.
- After review, each touched node has a `review` block: `{comment, status_before, status_after, resolution?, body_edited, reviewed_at, review_source}`.

## Workflow

```
   <basename>.md + <basename>.decisions.json
              │
              ▼ render.py [--basename NAME]
        <basename>.html (open in browser)
              │
              ▼ reviewer fills form, exports / posts JSON
        review-*.json
              │
              ▼ apply.py [--basename NAME]  (overwrites originals in place)
   <basename>.md + <basename>.decisions.json   (version bumped)
              │
              ▼ rerender and review again
```

Each `decisions.json` records `applied_from_review.review_path` and `applied_from_review.body_edits`, so you can always trace which JSON produced the current version and which anchors it touched. `git log` (or your editor's diff view) is the per-revision history.

## Session inbox (daemon)

The file-and-path workflow above is fine for one spec at a time, but multiple
agents working in parallel need a shared place to drop reviews into. The
**session inbox** is RIView's cross-project store: each registered review is a
*session* under `<RIVIEW_HOME>/sessions/<session-id>/`, with revisioned spec history
(`revisions/N/source.md` + `decisions.json`) and revisioned review history
(`reviews/N/review.json`).

This README documents the on-disk model, the agent-facing CLI, and the
localhost HTTP daemon that exposes a browser UI on
`http://127.0.0.1:7891/`. The CLI works standalone if you don't want to run
the daemon.

### Layout

```
<RIVIEW_HOME>/    # defaults to <riview_repo>/.riview/
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

Storage root: defaults to **`<riview_repo>/.riview/`** — i.e. the daemon's own checkout. One RIView repo can back many consuming-project agents that submit specs to it. The `.riview/` directory is gitignored. Override with `RIVIEW_HOME=/path/to/dir`; tests use this, and anyone with existing state under `~/.riview/` can keep it by exporting `RIVIEW_HOME=$HOME/.riview`.

### Session lifecycle

- `awaiting_review` — a revision has been submitted but no review exists for it yet. Set by `submit` on every revision (including responder follow-ups).
- `review_submitted` — a review has been recorded against the current revision. Set by `POST /sessions/<id>/review` and the `submit-review` CLI.
- `applied` — the session has been explicitly finalized via `riview applied <id>` (no more responder cycles expected). Not used by the responder loop — submitting a new revision is the per-cycle consumption boundary and leaves status at `awaiting_review` for the new revision.
- `closed` — manually dismissed via `riview dismiss <id>`.

`pull` is **idempotent**: it returns the latest submitted review for the current
revision every call, never consumes. The responder loop advances the session by
submitting a new revision with `--session <id>` after writing the planned edits;
that submit is itself the "consumed N's review" signal, no extra command needed.
See [skills/riview-respond/SKILL.md](skills/riview-respond/SKILL.md) for the
canonical responder loop.

### CLI

```bash
# Register a spec as a new session (prints session_id + URL).
python3 scripts/riview.py submit design --basename mvp

# Idempotent re-submit (identical content) returns the existing revision.
# Changed content advances to revision 2 inside the same session.
python3 scripts/riview.py submit design --basename mvp --session <id>

# Record a review JSON against the session's current revision.
# (In slice 1b this happens via the browser; the CLI hook is here for testing.)
python3 scripts/riview.py submit-review <id> /path/to/review.json

# Print the latest review for the current revision (exits 4 if none).
python3 scripts/riview.py pull <id>

# Block until the next event arrives (new revision or new/updated review).
# Defaults to tail-f: --since = current event_seq. Pass --since 0 to replay.
# --timeout 0 means wait indefinitely (default 0). Daemon must be running.
python3 scripts/riview.py wait <id>
python3 scripts/riview.py wait <id> --since 3 --timeout 30

# Mark a session as applied (after the agent applies a pulled review).
python3 scripts/riview.py applied <id>

# List open sessions across all projects (--all includes closed).
python3 scripts/riview.py list

# Show full meta.json, print the daemon URL, or close the session.
python3 scripts/riview.py status <id>
python3 scripts/riview.py open <id>
python3 scripts/riview.py dismiss <id>
```

Exit codes: `0` ok, `2` bad input, `3` session not found, `4` no review for current revision, `5` daemon unreachable.

### Daemon

A small HTTP daemon serves a browser review UI and accepts review POSTs:

```bash
python3 scripts/riview.py daemon                    # 127.0.0.1:7891
python3 scripts/riview.py daemon --port 7900        # custom port
```

By default the daemon refuses to bind anything other than loopback
(`127.0.0.1` / `localhost` / `::1`). The auth token is embedded in
unauthenticated `GET` pages, so a non-loopback bind would let anyone on
the network read it from `/sessions/<id>` and POST reviews. Override with
`--unsafe-host` if you have a tunnel / restricted LAN and accept that.

Routes:

- `GET /` — index page listing open sessions across all projects.
- `GET /sessions/<id>` — per-session review UI (the existing `render.py` HTML,
  with a "Submit to RIView server" button wired up).
- `POST /sessions/<id>/review` — accept a review JSON for the session's
  current revision. Requires header `X-Riview-Token: <token>`. The body **must**
  include `base_revision` (the session revision the page rendered against);
  the daemon rejects HTTP POSTs without it (400). Reviews are **merged by
  `node_id`** (upsert): the incoming `reviews[]` entries overwrite any prior
  entry for the same node, and untouched nodes from previous POSTs are
  preserved. The merged list is re-sorted by `node_id` before write. This lets
  the UI submit one decision at a time without losing earlier work.

  When `base_revision != current_revision` (the responder rolled the session
  forward while the reviewer was typing), each incoming entry is checked with
  a per-node fingerprint diff against the stored snapshots: matching entries
  are accepted into the current revision's review, mismatching entries are
  returned in `conflicts` and never merged. The response shape is:
  `{accepted: [{node_id}], conflicts: [{node_id, reason, base_revision,
  current_revision}], base_revision, current_revision, review_count,
  event_seq}`. The persisted review file is always normalized to the current
  revision (`spec_version` rewritten; `base_revision` stripped). See
  [ADR-0007](docs/adr/0007-per-node-stale-submit-acceptance.md).
- `GET /sessions/<id>/wait?since=<n>&timeout=<s>` — long-poll. Returns 200
  with the session event snapshot as soon as `meta.event_seq > since`, or
  204 on timeout. Default timeout 25s, max 60s. `event_seq` is a monotonic
  per-session cursor bumped by `_write_revision` and review POSTs.
- `GET /sessions/<id>/events` — Server-Sent Events stream of the same
  snapshot. 15s `:keepalive` comments; reconnect on disconnect. The browser
  uses this to show "Spec updated — Reload" when the agent submits a new
  revision while a reviewer is mid-form.

The token is generated on first daemon start at `<RIVIEW_HOME>/token` (mode 0600)
and is read by the daemon to mint same-origin POSTs from the rendered review
page. The cross-origin POST path is implicitly blocked by browser preflight
on the custom header. Body size is capped at 1 MiB.

Locking: writes (CLI + daemon) take an exclusive flock on
`~/.riview/sessions/<id>/.lock` so concurrent agents can't race on revision
increments or stomp each other's meta updates. On non-POSIX platforms the
lock is a no-op; the daemon is intended for single-user, low-contention use.

Browser UI behavior:

- Cards are ordered topologically by `depends_on`, with ties broken by id.
  Each card shows `depends on` / `affects` chips that scroll to the referenced
  card on click.
- Each card is **prefilled with the node's current applied state** — status
  dropdown shows the persisted status, ambiguity resolution shows the persisted
  choice/freeform. The comment textarea is per-cycle and stays blank. Touched-
  detection diffs against the applied state, so re-selecting the status that's
  already set does not mark the card touched. See
  [ADR-0008](docs/adr/0008-form-prefills-with-applied-state.md).
- A `↑ upstream changed` badge appears on a downstream card the moment any
  direct upstream's form has unsubmitted changes — a hint that the reviewer
  may want to revisit the downstream decision once the upstream lands.
- Each card has a **Submit decision** button that POSTs only that node's
  entry; the server merges it with any prior reviews for the same revision.
  The footer **Submit all** button POSTs every dirty card in one go. Both
  paths are available so the reviewer can pick fast-feedback or batch.
- **Drafts persist in localStorage** keyed by `riview:draft:<sid>:<base_revision>`
  (or `riview:draft:standalone:<spec_id>:<version>`), stored as a sparse diff
  against the applied state. Closing and reopening the tab restores the
  reviewer's unsent edits. Successful submit clears the corresponding entries;
  the latest five revision numbers' drafts are retained per session. See
  [ADR-0009](docs/adr/0009-localstorage-draft-persistence.md).
- The page opens an SSE connection to `/sessions/<id>/events`. When the
  responder submits a new revision (a material upstream change), a "Spec
  updated to revision N — Reload" banner appears. The reload button uses the
  View Transitions API when available and respects `prefers-reduced-motion`.
  If the reviewer submits during a rolled-forward revision, per-node
  fingerprint diffs (ADR-0007) decide which entries still apply; conflicts
  are surfaced inline on the affected cards.

### Responder loop

A responder is an agent that closes the review loop: it waits on a session,
pulls the reviewer's latest delta, edits the project spec in place, and
submits the next revision. The canonical responder is the
[`riview-respond` Claude Code skill](skills/riview-respond/SKILL.md), which
lives in this repo and is installed with a symlink:

```sh
ln -s "$RIVIEW_REPO/skills/riview-respond" ~/.claude/skills/riview-respond
```

`/riview-respond <session_id>` (in any Claude Code terminal) then runs the
loop. Cycle structure: `wait` → `pull` → plan write set → stale-review
re-snapshot guard → preflight drift check → write spec pair in place →
`submit`. See [ADR-0010](docs/adr/0010-responder-skill-lives-in-repo.md) for
why the skill is in the repo, not in `~/.claude/skills/`.

Smoke tests:

```bash
python3 -m unittest tests.test_session          # CLI / session model
python3 -m unittest tests.test_daemon           # HTTP daemon end-to-end
python3 -m unittest tests.test_render_validate  # renderer input hardening
```

## Design choices

- **Anchors are HTML comments.** Invisible when rendered as markdown by any other tool. Surgical body edits possible without touching unrelated text. Comment-only edits guaranteed.
- **The renderer is one file.** Self-contained HTML — inline CSS, inline JS, no CDN. Works offline, prints sensibly, respects dark mode.
- **The applier overwrites in place.** Each apply pass bumps `version` on the sidecar and records `applied_from_review` (the consumed delta path, touched anchors, timestamp). Git is the per-revision history — see ADR-0003.
- **Determinism over polish.** Review deltas sort `reviews[]` by `node_id`. Anchor blocks are rewritten with minimal whitespace churn. Stable IDs are the contract; everything else is a render concern.
- **No spatial graph view.** Cards are linear, real DOM. A spatial/zoomable variant (react-flow, tldraw, or HTML-in-Canvas) is a v2 swap on the same data model — out of MVP scope.

## Limitations / explicit non-goals (MVP)

- No live source-file watcher; review delivery is export/download or daemon POST.
- No multi-user merge — one reviewer at a time.
- No freeform ink/handwriting capture.
- No automated source-spec re-generation; the applier mutates the structured graph + body anchors but doesn't re-run the authoring agent.
- Only the most recent `review` per node is preserved in the sidecar. `apply.py` overwrites `node.review` each pass; older review history lives in git history. A `review_history[]` extension would be a schema-level addition.

## Files

| Path                                  | What it is                                       |
|---------------------------------------|--------------------------------------------------|
| `SCHEMA.md`                           | Data-model reference                             |
| `scripts/render.py`                   | Spec → interactive HTML                          |
| `scripts/apply.py`                    | Review delta → in-place `<basename>.{md,decisions.json}` update |
| `scripts/riview.py`                   | Session inbox CLI (`submit/list/pull/...`)       |
| `tests/test_session.py`               | Smoke tests for the session model                |
| `sample/spec.md`                      | Demo: Pomodoro Timer MVP, markdown view          |
| `sample/spec.decisions.json`          | Demo: structured graph (6 nodes)                 |
| `sample/review-demo.json`             | Demo review delta exercising every code path     |
| `sample/review-demo-2.json`           | Second review for chain testing                  |
| `sample/expected/`                    | Canonical outputs after each demo review pass    |
| `sample/spec.html`                    | Generated locally by the quick-start; not in git |
