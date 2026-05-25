#!/usr/bin/env python3
"""Snapshot session state for the responder's stale-review guard.

Prints a JSON object capturing the session's current revision, event_seq, and
the SHA-256 of the pulled review.json (if present). The responder records this
snapshot before starting generation and re-reads it just before writing the new
revision; if either the revision has advanced or the review hash has changed,
the in-progress generation is discarded and the cycle restarts.

Exit codes:
    0  — snapshot printed
    2  — usage / missing session

Usage:
    snapshot.py <session_id> [--riview-home <path>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session_id")
    ap.add_argument(
        "--riview-home",
        default=os.environ.get("RIVIEW_HOME", str(Path.home() / ".riview")),
    )
    args = ap.parse_args(argv)

    session_dir = Path(args.riview_home) / "sessions" / args.session_id
    meta_path = session_dir / "meta.json"
    if not meta_path.exists():
        sys.stderr.write(f"snapshot: meta not found at {meta_path}\n")
        return 2
    try:
        meta = json.loads(meta_path.read_text("utf-8"))
    except json.JSONDecodeError as e:
        sys.stderr.write(f"snapshot: meta is not valid JSON: {e}\n")
        return 2

    cur = meta.get("current_revision") or 0
    review_path = session_dir / "reviews" / str(cur) / "review.json"
    review_hash = None
    if review_path.exists():
        review_hash = hashlib.sha256(review_path.read_bytes()).hexdigest()
    sys.stdout.write(
        json.dumps(
            {
                "session_id": args.session_id,
                "current_revision": cur,
                "event_seq": int(meta.get("event_seq", 0)),
                "status": meta.get("status"),
                "review_hash": review_hash,
            },
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
