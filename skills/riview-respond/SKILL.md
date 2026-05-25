---
name: riview-respond
description: 'Close the RIView review loop: wait on a session, consume the next reviewer-submitted delta, produce the next spec revision in response, submit. Bidirectional — reviewer drives intent, responder turns intent into structured spec edits.'
---

## Parameters

- **session_id**: The 12-char hex session id (from `riview list`). Required.
- **mode**: `watch` (default — loop indefinitely on `wait`) or `no-watch` (one-shot: pull → respond → submit → exit).
- **riview_repo**: Path to the cloned `hri` repo so the skill can locate `scripts/riview.py`. Default: `$RIVIEW_REPO`, falling back to `~/genesis/hri`.

## Slogan

**Read broadly, write narrowly, grow forward.**

You read the whole spec to keep edits globally coherent, but the write set is bounded: only the nodes the reviewer touched, plus their transitive downstream via `depends_on`, plus any new nodes that follow as a direct consequence. The spec grows append-only — never delete, never rename.

## Instructions

### Context

- **inputs-and-outputs**

  Per cycle: one reviewer delta in (`pull` output), one new spec revision out (`submit` snapshot). The reviewer drives semantic intent; you translate that intent into structured edits across the spec pair (`<basename>.md` + `<basename>.decisions.json`). Every cycle's durable state already lives in the session inbox — if the terminal dies idle, nothing is lost; restart with the same `session_id`.

- **mutation-rights**

  You may edit existing nodes (status, rationale, alternatives, body markdown, resolution, mitigation, severity, etc.) and append new ones. You may **not** delete or rename. Existing `id` and `source_anchor` values are immutable contracts — they are referenced from anchor blocks, prior reviews, downstream `depends_on`, and external git history. Display `title` may change; the stable reference cannot.

- **append-ordering**

  New nodes append to `nodes[]` (existing order preserved). New markdown anchor blocks insert at a deterministic location — preferring just after the spawning parent's block when that can be done without reflowing unrelated prose, otherwise at the end of an explicit "follow-up" section. Existing anchor blocks never move.

- **deterministic-new-ids**

  New node IDs are `<kind-prefix>-<slug>` (e.g. `deci-stale-flag-cleanup`, `risk-orphan-anchor`). The slug is a short normalized form of the node's intent. On collision, append a numeric suffix. Determinism matters because crash-resume can re-run the same cycle: identical inputs must produce identical IDs, otherwise resumed runs accumulate duplicate nodes.

- **cascade-via-status**

  When a reviewer rejection invalidates downstream nodes, do not delete them. Cascade decisions to `needs-work`, ambiguities to `open` or `deferred`, risks to `dismissed` or `open` — whichever fits — with explanatory `review` / `comment` / body text describing what changed and why the downstream node now needs reconsideration. If a clean replacement is appropriate, append a new node alongside the marked-stale one. The dead-end branch stays visible in git and the UI; nothing is silently dropped. Reviewer-submitted reviews against now-invalidated downstream nodes are **not** auto-cancelled — the invalidating change is communicated through the target node's updated body and status; the reviewer decides whether to retract or revise their earlier mark.

- **pure-status-confirm-noop**

  A reviewer entry that flips status from `ai-confident` to `confirmed` (decisions) or to `accepted` / `mitigated` (risks) and carries no `body_edit` or non-trivial `comment` is a no-op beyond applying the status change. The decision didn't change; only the reviewer's endorsement did. Walking the `depends_on` graph and regenerating downstream nodes in that case wastes a cycle. Short-circuit: apply the status change, log the no-op, re-enter wait. Rejections, `needs-work`, body edits, comments, and ambiguity resolutions always trigger the full impact walk — they signal real semantic change. (Ambiguity `resolved` is deliberately not in the pure-approval set: the schema requires a `resolution` for `resolved`, so "approve a resolved ambiguity with no resolution" isn't a real category.)

- **write-target**

  Write to the project dir recovered from `meta.project_path`: `<project_path>/<basename>.md` and `<project_path>/<basename>.decisions.json`. That dir is the canonical, git-tracked copy (ADR-0003). Then call `riview submit <project_path> --basename <b> --session <id>` to snapshot the new revision into the session inbox. Treat yourself as a normal author: `git diff` after every cycle just works. Writes must follow crash-hardened individual-file discipline — `apply.py` is the reference (tempfile → fsync → rename per file, plus best-effort parent-directory fsync).

- **no-auto-commit**

  Do **not** `git commit`. Commit cadence is a personal-style choice; auto-commit would hide the diff from the reviewer before they have a chance to look at it. Each cycle prints `cd <project_path>` and `git diff -- <basename>.md <basename>.decisions.json` as a hint, then leaves the working tree dirty for the user to stage at their own pace.

- **submit-is-consumption**

  Successful `submit` of revision N+1 is the consumption boundary for revision N's review — no separate "applied" call. `submit` already advances `current_revision` to N+1 and flips `meta.status` to `"awaiting_review"` for the new revision. The reviewer's next POST against rev N+1 flips status to `"review_submitted"` and the loop continues. If generation fails, the process dies, or `submit` errors, revision N's review remains `pull`-able and the loop resumes from exactly where it stopped. (`riview applied <session_id>` exists as an explicit session-terminal command — for finalizing a session with no more responder cycles expected — and is **not** part of the per-cycle loop.)

- **no-website-lock**

  The website does not lock during your cycles. The reviewer may keep editing and submitting in parallel; the daemon's per-node merge handles it, and the SSE "Spec updated to revision N — Reload" banner handles the visible race. Your correctness comes from the stale-review guard below, not from any cooperating locks.

- **stale-review-guard**

  Before writing, re-snapshot the session (`tools/snapshot.py <id>`). If `current_revision` advanced past the snapshot you opened the cycle with, abort the write and re-pull. If `current_revision` is unchanged but `review_hash` differs (the reviewer merged additional entries into the same revision's review), discard your in-progress generation and restart the cycle from the merged latest review. No locks, no daemon state — correctness lives entirely on your side.

- **preflight-drift-check**

  Before writing the new revision, run `tools/preflight.py <id>`. It verifies the project dir's spec pair still matches `meta.revisions[<current_revision>].md_hash` and `json_hash`. Exit 3 means the user hand-edited mid-loop (or a prior crash left things inconsistent). Abort with a clear message; do **not** silently stomp the user's edits. Unrelated dirty files in the project dir are fine — only the two tracked spec files matter.

### Steps

1. **Resolve riview command.** Set `RIVIEW_REPO=${RIVIEW_REPO:-$HOME/genesis/hri}` and define a shell function: `riview() { python3 "$RIVIEW_REPO/scripts/riview.py" "$@"; }`. (A plain `RIVIEW="python3 .../riview.py"` env var won't word-split as one command in zsh — use the function form.) If `$RIVIEW_REPO/scripts/riview.py` doesn't exist, ask the user where the `hri` repo lives and `export RIVIEW_REPO=<path>` for the rest of the session. All subsequent steps invoke `riview` (the function), not `$RIVIEW`.

2. **Verify session and announce intent.** Run `riview status <session_id>` to print the session's meta. Confirm `project_path`, `basename`, and current `revision`. Print the resume hint up-front:

   ```
   Watching session <id> (rev <N>, basename=<b>) at <project_path>.
   Resume after any interruption with: riview-respond <id>
   ```

3. **Open the cycle:**

   a. **Snapshot** the session: `python3 "$RIVIEW_REPO/skills/riview-respond/tools/snapshot.py" <session_id>`. Capture `current_revision = N0`, `event_seq = E0`, `review_hash = R0`.

   b. **Wait** for the next review: `riview wait <session_id> --since <E0>`. The CLI long-polls in the background; the harness wakes you on exit. (If you started the loop and a review is already pending — `R0` is non-null on a fresh open — skip the wait and proceed.) Default `--timeout 0` means no limit; if you pass `--timeout N` and it expires (exit 5), simply re-enter the wait.

   c. **Pull** the review: `riview pull <session_id>` → reviewer's delta as JSON. If the daemon reports no review (exit 4), restart at 3a — the wait fired on a non-review event.

4. **Plan the write set.**

   a. Read the full spec pair from `<project_path>/<basename>.{md,decisions.json}` as read-only context. You need the whole graph to avoid locally coherent / globally contradictory edits.

   b. Classify each reviewer entry:
      - **pure-status-confirm** (per the pure-status-confirm-noop rule): apply status change only.
      - **material**: status to `rejected` / `needs-work`, ambiguity `resolved` with a new resolution, ambiguity `deferred`, any `body_edit`, any non-trivial `comment`, any resolution change. Triggers the full impact walk.

   c. For each material entry, compute the **write set**: the touched node plus its transitive downstream via `depends_on`. Append new nodes as needed (cascading replacements, follow-up risks/ambiguities the change exposes). Apply cascade-via-status to downstream that the change invalidates.

   d. Draft the edits in your head. Do **not** write to disk yet.

5. **Re-snapshot before writing (stale-review guard).** Run `python3 "$RIVIEW_REPO/skills/riview-respond/tools/snapshot.py" <session_id>` again. Compare with the snapshot from 3a:
   - `current_revision != N0`: the responder thought it had the loop to itself but a new revision landed. **Discard the planned write** and restart at step 3 (the loop will pull the merged-newer state).
   - `current_revision == N0` but `review_hash != R0`: the reviewer merged more entries into this revision's review. **Discard** and restart at step 3c (pull again, plan against the merged review).
   - Both match: safe to proceed.

6. **Preflight drift check.** Run `python3 "$RIVIEW_REPO/skills/riview-respond/tools/preflight.py" <session_id>`. If exit 3 (drift), abort the loop with the helper's message — point the user at `git diff` in the project dir and stop. Do not write.

7. **Write the new revision in place.** Edit `<project_path>/<basename>.md` and `<project_path>/<basename>.decisions.json` per the planned write set. Use crash-hardened individual-file writes (tempfile → fsync → rename + best-effort parent-dir fsync) — the same discipline `apply.py` uses. Each file is crash-hardened individually; the two-file write is not transactional. Bump `decisions.json:version` by 1.

8. **Submit.** `riview submit <project_path> --basename <basename> --session <session_id>` — snapshots the new revision into the session inbox. On non-zero exit, report the error and stop the cycle; the loop can be re-entered (revision N's review is still pull-able and the same edits will replay deterministically). On success, `current_revision` is now N+1 with `meta.status = "awaiting_review"`; the reviewer's next POST against rev N+1 flips status to `"review_submitted"` and the next cycle begins. Do **not** call `riview applied` — that command is for explicit session finalization, not per-cycle bookkeeping.

9. **Print the diff hint.** Print exactly:

   ```
   cd <project_path>
   git diff -- <basename>.md <basename>.decisions.json
   ```

   Do not run `git diff` for the user — the diff is for them to read. Do not `git add`, `git commit`, or `git stash` anything.

10. **Loop or exit.**
    - `mode=watch` (default): return to step 3 with `E0 = post-submit event_seq` (re-run `tools/snapshot.py` to read the fresh event_seq after step 8).
    - `mode=no-watch`: print "one-shot cycle complete; exiting" and stop.

### Constraints

- **Require:** Only edit existing nodes' mutable fields or append new nodes. Never delete a node. Never rename a node `id` or change its `source_anchor`. Existing markdown anchor blocks never move.
- **Require:** Never call `riview applied` inside the per-cycle loop. `submit` is the consumption boundary; `applied` is reserved for explicit session finalization by the user.
- **Require:** Run the stale-review guard (re-snapshot) and the preflight drift check immediately before writing. Skipping either silently corrupts user state under concurrent edits.
- **Require:** Each write is one file at a time, tempfile → fsync → rename, plus a best-effort parent-dir fsync. Match the discipline in `apply.py`. Do not write both files with a single shutil call.
- **Avoid:** Walking the `depends_on` graph and regenerating downstream nodes on a pure-status-confirm entry. It spurious-diffs the spec and burns a cycle for no information gain.
- **Avoid:** Auto-committing, auto-staging, or auto-stashing in the project dir. The reviewer reads the diff before deciding what to commit; hiding the diff defeats the loop.
- **Avoid:** Locking, daemon state, or any new endpoints for responder lifecycle. The website explicitly does not gain a `respond_in_progress` flag.
- **Avoid:** Falling back to a session-local write if the project dir is gone. The responder's job is to surface the loss of the project dir (with the session id and original `project_path` in the message), not to paper over it.

### Procedure: pure-status-confirm fast path

1. For the entry: write the status change into the sidecar's node (e.g. `nodes[i].status = "confirmed"`). Do not touch body, rationale, alternatives, resolution, downstream.
2. Bump `decisions.json:version` by 1.
3. Optionally append a tiny `review` block on the node summarizing the endorsement (`status_before` → `status_after`, `comment`, `reviewed_at`), matching what `apply.py` would emit for the same entry.
4. Fall through to step 5 of the main loop (re-snapshot, preflight, write, submit).

### Procedure: cascade-via-status

1. Identify the rejected / `needs-work` / resolution-changed source node.
2. BFS forward via `depends_on` (forward graph: `forward[X]` = ids that depend on X). For each reachable downstream node `D`:
   - **decision** D: status → `needs-work`. Append a `review.comment` (or extend body) explaining what about the upstream change invalidates D's previous resolution.
   - **ambiguity** D: status → `open` if the upstream change reopens the question; `deferred` if the question is moot under the new upstream.
   - **risk** D: status → `dismissed` if the upstream change removes the risk; `open` if it merely refocuses it.
3. If a clean replacement node is the right move, append a new node (deterministic id, status `ai-confident`) alongside the cascaded node, with a `depends_on` link back to the source.
4. Do **not** delete the cascaded node. Do **not** edit reviewer-submitted reviews on the cascaded node — communicate the invalidation through the node's own body / status / responder-side review, and let the reviewer decide whether to retract.

### Procedure: drift-recovery (preflight exit 3)

1. Print the preflight helper's stderr verbatim (it names which file drifted and where).
2. Print:

   ```
   The project dir's spec pair no longer matches what the session recorded for revision <N>.
   Reconcile manually before resuming the loop:
     cd <project_path>
     git status
     git diff -- <basename>.md <basename>.decisions.json
   Once the working tree matches the recorded hashes, re-run riview-respond <session_id>.
   ```

3. Exit the loop cleanly. Do not write, do not call submit, do not call applied.

### Procedure: resume-after-interrupt

1. Re-run `riview-respond <session_id>`. The skill re-enters at step 1 with no special flag.
2. `pull` is idempotent — if the prior cycle died after pull but before write, the same review re-pulls and the same edits replay (deterministic new IDs ensure no duplicate appends).
3. If the prior cycle died after `submit` succeeded, the new revision is already in the session inbox and `meta.status = "awaiting_review"` for N+1. The next loop entry sees no pending review for the prior revision and waits on the next reviewer POST against N+1.
4. If the prior cycle died before `submit` but the project files were partially written, preflight on the next run will catch the drift (revision N's hash won't match) and recovery is via `git diff` + `git checkout --` as in drift-recovery.

## Notes on tooling

- **`$RIVIEW_REPO/skills/riview-respond/tools/preflight.py <session_id>`** — drift check; exit 0 means safe to write. Reads `$RIVIEW_HOME` (defaults `~/.riview`).
- **`$RIVIEW_REPO/skills/riview-respond/tools/snapshot.py <session_id>`** — JSON snapshot of `{current_revision, event_seq, status, review_hash}`. Used by the stale-review guard.
- This skill is model-agnostic. The LLM call each cycle is the calling agent's own turn (Claude Code today; Codex or Gemini tomorrow). Do not hardcode a model recommendation.

## Installation

This skill lives at `<hri-repo>/skills/riview-respond/`. To make it invocable as a Claude Code skill, symlink it into your home skills dir:

```sh
ln -s "$RIVIEW_REPO/skills/riview-respond" ~/.claude/skills/riview-respond
```

After symlinking, `/riview-respond <session_id>` works in any Claude Code terminal. The skill self-resolves its helper scripts via `$RIVIEW_REPO`, so you can keep the repo anywhere.
