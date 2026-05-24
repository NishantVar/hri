# Overwrite in place — git is the per-revision history

`apply.py` overwrites `<basename>.md` and `<basename>.decisions.json` in place (atomic tempfile → fsync → rename, plus best-effort parent-dir fsync). Each pass bumps `version` on the sidecar and refreshes `applied_from_review` with the path of the consumed review delta, the touched anchors, and the apply timestamp. We deliberately do **not** keep numbered revisions on disk (no `spec.rev1.md`, `spec.rev2.md`, …).

The expectation is that specs live in a git repo and the user commits after each apply pass. `git log <basename>.md` and `git diff` are then the per-revision history — well-understood tools the user already has. A parallel `revN/` tree on disk would duplicate git's job, drift from it (you'd have to remember to clean it up), and make the spec directory noisy. The `applied_from_review` block inside the sidecar carries the audit trail that doesn't belong in the markdown.

The daemon's session inbox is a separate concern: it *does* keep numbered revisions, because there is no git repo around it.
