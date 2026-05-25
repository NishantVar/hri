#!/usr/bin/env python3
"""Preflight drift check for the riview-respond loop.

Verifies that the project dir's spec pair still matches the hashes recorded in
the session's `meta.revisions[<current_revision>]`. If either file has drifted
(user hand-edited mid-loop, prior crash left things inconsistent, etc.) the
helper exits non-zero with a clear message so the responder can abort without
silently stomping the user's edits.

Exit codes:
    0  — both files match the recorded hashes (safe to proceed)
    2  — usage / missing session / unreadable meta
    3  — drift detected (stdout reports which file)

Usage:
    preflight.py <session_id> [--riview-home <path>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session_id")
    ap.add_argument(
        "--riview-home",
        default=os.environ.get("RIVIEW_HOME", str(Path.home() / ".riview")),
        help="Base dir for sessions (default: $RIVIEW_HOME or ~/.riview)",
    )
    args = ap.parse_args(argv)

    meta_path = Path(args.riview_home) / "sessions" / args.session_id / "meta.json"
    if not meta_path.exists():
        sys.stderr.write(f"preflight: meta not found at {meta_path}\n")
        return 2
    try:
        meta = json.loads(meta_path.read_text("utf-8"))
    except json.JSONDecodeError as e:
        sys.stderr.write(f"preflight: meta is not valid JSON: {e}\n")
        return 2

    cur = meta.get("current_revision")
    project_path = meta.get("project_path")
    basename = meta.get("basename")
    if not isinstance(cur, int) or cur < 1:
        sys.stderr.write(f"preflight: session has no current_revision (got {cur!r})\n")
        return 2
    if not project_path or not basename:
        sys.stderr.write("preflight: session meta missing project_path/basename\n")
        return 2

    rev_info = (meta.get("revisions") or {}).get(str(cur))
    if not rev_info:
        sys.stderr.write(f"preflight: meta.revisions[{cur}] missing\n")
        return 2

    md_path = Path(project_path) / f"{basename}.md"
    json_path = Path(project_path) / f"{basename}.decisions.json"
    drifted: list[str] = []
    for label, p, expected_key in (
        ("md", md_path, "md_hash"),
        ("decisions.json", json_path, "json_hash"),
    ):
        expected = rev_info.get(expected_key)
        if not p.exists():
            drifted.append(f"{label}: missing at {p}")
            continue
        actual = file_sha256(p)
        if actual != expected:
            drifted.append(
                f"{label}: hash {actual[:12]}… != recorded {str(expected)[:12]}… "
                f"at {p}"
            )

    if drifted:
        sys.stderr.write(
            "preflight: project files drifted from session meta.revisions["
            f"{cur}]:\n  - " + "\n  - ".join(drifted) + "\n"
            "  Recover by inspecting `git diff` in "
            f"{project_path} and reconciling manually.\n"
        )
        return 3
    sys.stdout.write(
        json.dumps(
            {
                "session_id": args.session_id,
                "current_revision": cur,
                "project_path": project_path,
                "basename": basename,
                "md_hash": rev_info.get("md_hash"),
                "json_hash": rev_info.get("json_hash"),
            },
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
