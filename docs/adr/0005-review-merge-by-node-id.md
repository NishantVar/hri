# `/review` POST merges by `node_id`, last write wins

POST `/sessions/<id>/review` upserts entries into the current revision's review by `node_id`. Later writes for the same node overwrite earlier ones; writes for other nodes are untouched. Entries where every meaningful field is null/empty are silently dropped on the POST path. (When `apply.py` later consumes the merged review, it also drops empties and records the count in `applied_from_review.empty_entries_skipped`.)

This is what lets the per-card "Submit decision" button and the footer "Submit all" share one endpoint without one mode clobbering the other. A per-card submit POSTs a single-entry delta; "Submit all" POSTs many entries; an agent's CLI submission tops them all up. None of these need to know about the others.

Alternative considered: replace-all-on-POST (each POST overwrites the entire review for the revision). Rejected because the per-card UX would silently discard any prior bulk work whenever a user clicked one card's submit button — a hostile default. The merge semantics keep the endpoint idempotent per node and additive across nodes.
