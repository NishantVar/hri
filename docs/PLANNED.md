# Planned work

Short list of things we know we want to build but haven't. Delete entries as they land.

## `riview-respond` Claude skill (slice 2e)

A Claude/agent skill (target path on the user's machine: `~/.claude/skills/riview-respond/`) that turns a session id into a one-shot review submission.

Rough shape, to be designed together:

- Input: a session id (and optionally a hint like "focus on the open ambiguities").
- Behaviour: pull the current revision's spec via the daemon (or the CLI), read the markdown + decisions JSON, propose a review delta (new statuses, resolutions for ambiguities, body edits where useful), and POST it back to `/sessions/<id>/review`.
- Output: a short summary of what it decided per node, plus the path of the review JSON it submitted.

Open design questions when we pick this up:

1. **Granularity.** One submission per skill invocation (the simple model), or stream per-card decisions as it goes (matches the per-card submit UX, more LLM calls)?
2. **Reuse vs new code.** The renderer's `validate()` and `apply.py`'s `is_empty_entry()` are already the canonical truth for what makes a delta well-formed; the skill should call them, not re-implement.
3. **Transport.** Daemon-only (POST to `/sessions/<id>/review`), or also support an offline mode that reads a local spec directory and writes a review JSON to disk for `apply.py`? The daemon path is simpler; the offline path keeps the skill useful when the daemon isn't running.

Until then, agents that want to submit reviews use the CLI:
`python3 scripts/riview.py submit-review <id> /path/to/review.json`.
