# Planned work

Short list of things we know we want to build but haven't. Delete entries as they land.

## Body-edit UI in the rendered form

The schema and `apply.py` both support `body_edit` (a per-node body markdown override that lands inside the anchor block), but the rendered review form has no widget to author one. Adding it would need:

- A textarea per card (collapsed by default; opens on a "Edit body" button so it doesn't dominate the card layout).
- Touched-detection that diffs against the node's current `body_md` from the anchor (the renderer already extracts these via `parse_anchored_bodies`).
- A way to preview the markdown before submitting — the prose body is wider than a comment, and reviewers will want to see it rendered, not just typed.
- A draft-persistence story consistent with ADR-0009 (sparse diff against applied body_md, not against empty).

Reviewer-as-agent already produces `body_edit` programmatically via the CLI; this item is only about giving human reviewers parity.
