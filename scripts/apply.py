#!/usr/bin/env python3
"""Apply a RIView review delta to a spec, producing <basename>.rev<N>.{md,decisions.json}.

Usage:
    python3 apply.py <spec-dir> <review.json> [--basename NAME] [--force] [--dry-run]

The applier never mutates originals in place. It always writes a new
<basename>.rev<N>.md / <basename>.rev<N>.decisions.json pair next to the
source, where N is the next integer after any existing revisions for that
basename. Default basename is "spec"; pass --basename mvp to target mvp.md.

Invariants enforced:
    - delta.spec_version must equal the current spec.version (use --force to override).
    - delta.spec_id must equal spec.spec_id.
    - new_status must be valid for the node's kind.
    - resolution is only valid on ambiguity nodes.
    - new_status=resolved on an ambiguity requires a resolution with either:
        * a choice_id matching one of node.options, or
        * a nonblank freeform string.
    - resolution requires (current or new) status=resolved.
    - body_edit replaces exactly one anchor block in the source markdown.
    - Entries with no meaningful content are dropped silently.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

VALID_STATUS = {
    "decision": {"ai-confident", "confirmed", "rejected", "needs-work"},
    "ambiguity": {"open", "resolved", "deferred"},
    "risk": {"open", "accepted", "mitigated", "dismissed"},
}

def find_latest(spec_dir: Path, basename: str) -> tuple[Path, Path, int]:
    """Return (md_path, json_path, current_version_n) for the latest revision."""
    md = spec_dir / f"{basename}.md"
    js = spec_dir / f"{basename}.decisions.json"
    rev_re = re.compile(rf"^{re.escape(basename)}\.rev(\d+)\.decisions\.json$")
    latest_n = 0
    for child in spec_dir.iterdir():
        m = rev_re.match(child.name)
        if m:
            n = int(m.group(1))
            if n > latest_n:
                latest_n = n
                js = child
                md = spec_dir / f"{basename}.rev{n}.md"
    return md, js, latest_n


def replace_anchor(md_text: str, anchor: str, new_body: str) -> str:
    pattern = re.compile(
        rf"(<!--\s*node:{re.escape(anchor)}\s*-->\s*\n)(.*?)(\n\s*<!--\s*/node:{re.escape(anchor)}\s*-->)",
        re.DOTALL,
    )
    matches = pattern.findall(md_text)
    if not matches:
        raise ValueError(f"anchor not found in source markdown: {anchor}")
    if len(matches) > 1:
        raise ValueError(f"anchor occurs {len(matches)} times in source markdown (must be unique): {anchor}")
    return pattern.sub(lambda m: m.group(1) + new_body.rstrip() + m.group(3), md_text)


def is_empty_entry(entry: dict) -> bool:
    """Entries with no meaningful content are dropped silently."""
    new_status = entry.get("new_status")
    resolution = entry.get("resolution")
    comment = entry.get("comment")
    body_edit = entry.get("body_edit")
    has_status = bool(new_status)
    has_resolution = isinstance(resolution, dict) and (
        bool(resolution.get("choice_id"))
        or (isinstance(resolution.get("freeform"), str) and resolution["freeform"].strip())
    )
    has_comment = isinstance(comment, str) and comment.strip()
    has_body_edit = body_edit is not None
    return not (has_status or has_resolution or has_comment or has_body_edit)


def validate_entry(node: dict, entry: dict) -> list[str]:
    """Return list of error messages. Empty list means the entry is valid."""
    errs: list[str] = []
    kind = node["kind"]
    new_status = entry.get("new_status")
    resolution = entry.get("resolution")

    if new_status:
        allowed = VALID_STATUS.get(kind, set())
        if new_status not in allowed:
            errs.append(
                f"node {node['id']}: invalid status {new_status!r} for kind {kind!r} "
                f"(allowed: {sorted(allowed)})"
            )

    if resolution is not None:
        if not isinstance(resolution, dict):
            errs.append(f"node {node['id']}: resolution must be an object")
            return errs
        if kind != "ambiguity":
            errs.append(
                f"node {node['id']}: resolution given but kind is {kind!r} (only valid on ambiguity)"
            )
        else:
            choice_id = resolution.get("choice_id")
            freeform = resolution.get("freeform")
            if not choice_id and not (isinstance(freeform, str) and freeform.strip()):
                errs.append(f"node {node['id']}: resolution must have choice_id or nonblank freeform")
            if choice_id:
                option_ids = {o["id"] for o in (node.get("options") or [])}
                if choice_id not in option_ids:
                    errs.append(
                        f"node {node['id']}: choice_id {choice_id!r} is not in node.options "
                        f"(valid: {sorted(option_ids)})"
                    )
            effective_status = new_status or node.get("status")
            if effective_status != "resolved":
                errs.append(
                    f"node {node['id']}: resolution provided but effective status is "
                    f"{effective_status!r} (must be 'resolved')"
                )

    if new_status == "resolved" and kind == "ambiguity":
        if resolution is None or not isinstance(resolution, dict):
            errs.append(f"node {node['id']}: status=resolved on ambiguity requires a resolution")

    return errs


def apply_review(
    spec: dict,
    md_text: str,
    delta: dict,
    review_path: Path,
    rev_n: int,
    force: bool,
    basename: str = "spec",
) -> tuple[dict, str, list[str], list[str]]:
    fatal: list[str] = []
    warnings: list[str] = []

    if spec.get("spec_id") != delta.get("spec_id"):
        fatal.append(
            f"spec_id mismatch: spec={spec.get('spec_id')!r}, delta={delta.get('spec_id')!r}"
        )

    if delta.get("spec_version") != spec.get("version"):
        msg = (
            f"version mismatch: spec is v{spec.get('version')}, "
            f"delta reviewed v{delta.get('spec_version')}"
        )
        if force:
            warnings.append(msg + " (allowed by --force)")
        else:
            fatal.append(msg + " (re-render against the latest spec, or pass --force)")

    if fatal:
        return spec, md_text, fatal, warnings

    nodes_by_id = {n["id"]: n for n in spec["nodes"]}
    applied = 0
    skipped_empty = 0
    body_edit_anchors: list[str] = []

    raw_entries = delta.get("reviews", []) or []

    # First pass: validate everything. Collect all errors so the user sees them at once.
    for entry in raw_entries:
        nid = entry.get("node_id")
        if not nid:
            fatal.append("review entry missing node_id")
            continue
        if nid not in nodes_by_id:
            fatal.append(f"node not found: {nid}")
            continue
        if is_empty_entry(entry):
            skipped_empty += 1
            continue
        errs = validate_entry(nodes_by_id[nid], entry)
        fatal.extend(errs)

    if fatal:
        return spec, md_text, fatal, warnings

    # Second pass: apply.
    reviewed_at = delta.get("reviewed_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    source_name = review_path.name

    for entry in raw_entries:
        nid = entry.get("node_id")
        if nid not in nodes_by_id:
            continue
        if is_empty_entry(entry):
            continue
        node = nodes_by_id[nid]
        status_before = node.get("status")
        status_after = status_before

        new_status = entry.get("new_status")
        if new_status:
            node["status"] = new_status
            status_after = new_status

        resolution = entry.get("resolution")
        if resolution:
            node["resolution"] = {
                "choice_id": resolution.get("choice_id"),
                "freeform": resolution.get("freeform"),
                "by": "human",
            }

        body_edited = False
        body_edit = entry.get("body_edit")
        if body_edit is not None:
            anchor = node.get("source_anchor", nid)
            md_text = replace_anchor(md_text, anchor, body_edit)
            body_edit_anchors.append(anchor)
            body_edited = True

        node["review"] = {
            "comment": entry.get("comment"),
            "status_before": status_before,
            "status_after": status_after,
            "resolution": node.get("resolution") if node["kind"] == "ambiguity" else None,
            "body_edited": body_edited,
            "reviewed_at": reviewed_at,
            "review_source": source_name,
        }
        applied += 1

    new_rev_n = rev_n + 1
    spec["version"] = spec.get("version", 1) + 1
    spec["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    spec["source_path"] = f"{basename}.rev{new_rev_n}.md"
    spec["applied_from_review"] = {
        "review_path": source_name,
        "reviewed_at": reviewed_at,
        "applied_at": spec["generated_at"],
        "review_count": applied,
        "empty_entries_skipped": skipped_empty,
        "body_edits": body_edit_anchors,
    }

    return spec, md_text, [], warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec_dir", type=Path)
    parser.add_argument("review_path", type=Path)
    parser.add_argument("--basename", default="spec",
                        help="Spec file basename (default: spec). Must match the rendered spec.")
    parser.add_argument("--force", action="store_true",
                        help="Allow version mismatch (delta reviewed a different spec version).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the new spec and md to stdout instead of writing files.")
    args = parser.parse_args(argv)

    if not args.spec_dir.is_dir():
        print(f"not a directory: {args.spec_dir}", file=sys.stderr)
        return 2
    if not args.review_path.is_file():
        print(f"review file not found: {args.review_path}", file=sys.stderr)
        return 2

    md_path, json_path, latest_n = find_latest(args.spec_dir, args.basename)
    if not md_path.exists() or not json_path.exists():
        print(f"missing {md_path.name} or {json_path.name} in {args.spec_dir}", file=sys.stderr)
        return 2
    spec = json.loads(json_path.read_text())
    md_text = md_path.read_text()
    delta = json.loads(args.review_path.read_text())

    new_spec, new_md, fatal, warnings = apply_review(
        spec, md_text, delta, args.review_path, latest_n, args.force, args.basename,
    )

    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    if fatal:
        print("apply failed:", file=sys.stderr)
        for e in fatal:
            print(f"  - {e}", file=sys.stderr)
        return 3

    next_n = latest_n + 1
    out_json = args.spec_dir / f"{args.basename}.rev{next_n}.decisions.json"
    out_md = args.spec_dir / f"{args.basename}.rev{next_n}.md"

    if args.dry_run:
        print(json.dumps(new_spec, indent=2))
        print(f"--- {args.basename}.md ---")
        print(new_md)
        return 0

    out_json.write_text(json.dumps(new_spec, indent=2) + "\n")
    out_md.write_text(new_md)
    info = new_spec["applied_from_review"]
    print(f"wrote {out_json}")
    print(f"wrote {out_md}")
    print(
        f"applied {info['review_count']} review(s)"
        + (f", skipped {info['empty_entries_skipped']} empty"
           if info['empty_entries_skipped'] else "")
        + f"; new version: v{new_spec['version']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
