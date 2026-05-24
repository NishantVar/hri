# RIView Schema

The data model for interactive spec/plan review. Two files form one logical spec:

- `<basename>.md` — human-readable markdown narrative.
- `<basename>.decisions.json` — structured graph of nodes (decisions, ambiguities, risks) keyed by stable IDs.

The default basename is `spec`; pass `--basename mvp` to render/apply scripts to point at `mvp.md` + `mvp.decisions.json` (and `mvp.rev<N>.*` for revisions). Multiple basenames can coexist in the same directory — rev discovery is basename-scoped.

The applier produces `<basename>.rev<N>.md` and `<basename>.rev<N>.decisions.json` after each review pass; it never mutates originals in place.

## Markdown anchor convention

Each node's body in `<basename>.md` is wrapped in matched HTML comments. The applier replaces content between matched anchors when a review includes a `body_edit`.

```markdown
<!-- node:<id> -->
Body markdown for this node.
<!-- /node:<id> -->
```

IDs must be unique within the spec and stable across revisions. Convention: `<kind-prefix>-<slug>` where prefix is `deci` / `amb` / `risk` (e.g. `deci-platform`, `amb-sync`, `risk-bg`).

## `<basename>.decisions.json`

```jsonc
{
  "spec_id": "string",                 // stable across revisions
  "spec_title": "string",
  "source_path": "<basename>.md",      // relative to this file; "<basename>.rev<N>.md" in rev sidecars
  "version": 1,                        // bumped by the applier on each rev
  "generated_at": "ISO-8601",
  "applied_from_review": null,         // set on rev files; see "Rev metadata"
  "nodes": [ { ... }, ... ]
}
```

### Common node fields

| Field           | Type              | Notes                                                          |
|-----------------|-------------------|----------------------------------------------------------------|
| `id`            | string            | Stable, unique within file. Used by anchors and review deltas. |
| `kind`          | enum              | `decision` \| `ambiguity` \| `risk`                            |
| `title`         | string            | Short headline shown in the card.                              |
| `status`        | enum              | See per-kind status table below.                               |
| `confidence`    | enum              | `high` \| `medium` \| `low` (AI confidence, not human).        |
| `depends_on`    | string[]          | Node IDs this one depends on. May be empty.                    |
| `source_anchor` | string            | Anchor ID in `spec.md`. Usually equals `id`.                   |
| `review`        | object \| null    | Set by the applier when reviews are merged. See below.         |

### Per-kind fields

**`decision`** — a choice the spec encodes.

| Field          | Notes                                              |
|----------------|----------------------------------------------------|
| `status`       | `ai-confident` \| `confirmed` \| `rejected` \| `needs-work` |
| `rationale`    | Short string, why the choice was made.             |
| `alternatives` | string[]                                           |

**`ambiguity`** — a question the spec couldn't answer alone.

| Field        | Notes                                                                     |
|--------------|---------------------------------------------------------------------------|
| `status`     | `open` \| `resolved` \| `deferred`                                        |
| `prompt`     | The question, phrased for the human.                                      |
| `options`    | Optional array of `{id, label, body}` — pre-suggested choices.            |
| `resolution` | Set by applier: `{choice_id?, freeform?, by: "human" \| "agent"}` \| null |

**`risk`** — something that could go wrong.

| Field        | Notes                                                  |
|--------------|--------------------------------------------------------|
| `status`     | `open` \| `accepted` \| `mitigated` \| `dismissed`     |
| `severity`   | `high` \| `medium` \| `low`                            |
| `mitigation` | Short string. May be `"TBD"`.                          |

### `review` field (post-apply)

Each node's `review` is `null` until at least one review delta has touched it. After the applier merges, it looks like:

```jsonc
{
  "comment": "freeform reviewer note or null",
  "status_before": "ai-confident",
  "status_after": "confirmed",
  "resolution": null,                       // for ambiguities only
  "body_edited": false,
  "reviewed_at": "ISO-8601",
  "review_source": "review-2026-05-23T19-12-00Z.json"
}
```

## Review delta format (paste-back JSON)

Produced by the renderer's "Export Reviews" button. Consumed by `apply.py`.

```jsonc
{
  "spec_id": "pomodoro-mvp",              // must match the spec's spec_id
  "spec_version": 1,                      // version the reviewer was looking at
  "reviewed_at": "2026-05-23T19:12:00Z",
  "reviewer": null,                       // optional free text
  "reviews": [
    {
      "node_id": "deci-platform",
      "new_status": "confirmed",          // optional; valid for the node's kind
      "comment": "Agree, ship iOS first.",
      "body_edit": null                   // optional; replacement markdown body
    },
    {
      "node_id": "amb-sync",
      "new_status": "resolved",
      "resolution": { "choice_id": "local", "freeform": null },
      "comment": "Local-only for v1; revisit later."
    }
  ]
}
```

Rules (the applier rejects the whole delta if any of these fail, listing every failure at once):

- `delta.spec_id` must equal `spec.spec_id`.
- `delta.spec_version` must equal the current `spec.version` — re-render against the latest spec, or pass `--force` to apply against a different version.
- `node_id` must match a node in the spec.
- `new_status` must be a valid status for that node's kind.
- `resolution` is only valid on `ambiguity` nodes (fatal elsewhere).
- A `resolution` requires either:
  - `choice_id` that matches one of `node.options[].id`, or
  - `freeform` that is a nonblank string.
- `resolution` requires (existing or new) `status == "resolved"`.
- `new_status == "resolved"` on an ambiguity requires a valid `resolution`.
- A `body_edit`'s anchor must occur exactly once in `<basename>.md`.
- Entries with all fields null/empty are dropped silently (counted as `empty_entries_skipped`).
- `reviews` is sorted by `node_id` in the renderer's export to make diffs deterministic.

## Rev metadata

When the applier produces a rev, it sets:

```jsonc
{
  "version": <prev + 1>,
  "source_path": "<basename>.rev<N>.md",
  "applied_from_review": {
    "review_path": "review-...json",
    "reviewed_at": "...",
    "applied_at": "...",
    "review_count": 7,
    "empty_entries_skipped": 0,
    "body_edits": ["deci-platform"]   // anchors touched by body_edit
  }
}
```

Each touched node gets its `review` field updated as shown above. Untouched nodes are copied through unchanged.

## Determinism

- Node order in `nodes[]` is preserved across revs.
- `reviews[]` in the delta is sorted by `node_id`.
- Anchor blocks in `<basename>.md` are rewritten without touching surrounding whitespace.
- `generated_at` and `reviewed_at` are the only fields expected to change between equivalent runs.
