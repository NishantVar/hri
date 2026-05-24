#!/usr/bin/env python3
"""riview — session inbox CLI for the RIView review tool.

Stores cross-project review sessions under ~/.riview/sessions/. Each session
has a numbered revision history (spec markdown + sidecar JSON) and a numbered
review history. Submitting identical content is idempotent; changed content
advances the revision number inside the same session.

Override the storage root with the RIVIEW_HOME environment variable.

Exit codes:
  0  success
  2  bad input / missing files / invalid JSON / malformed session id /
     cross-spec contamination (mismatched basename or spec_id)
  3  session not found (well-formed id, no matching session)
  4  no review available for the session's current revision
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_PORT = 7891  # daemon will listen here in slice 1b; CLI uses it to print URLs.

SESSION_ID_RE = re.compile(r"^[0-9a-f]{12}$")


class SessionNotFound(Exception):
    pass


class InvalidSessionId(Exception):
    pass


def validate_session_id(sid: str) -> None:
    if not isinstance(sid, str) or not SESSION_ID_RE.fullmatch(sid):
        raise InvalidSessionId(sid)


def riview_home() -> Path:
    override = os.environ.get("RIVIEW_HOME")
    return Path(override) if override else Path.home() / ".riview"


def sessions_root() -> Path:
    p = riview_home() / "sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_session_id() -> str:
    return secrets.token_hex(6)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix or "")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def session_dir(sid: str) -> Path:
    validate_session_id(sid)
    return sessions_root() / sid


def load_meta(sid: str) -> dict:
    p = session_dir(sid) / "meta.json"
    if not p.exists():
        raise SessionNotFound(sid)
    return json.loads(p.read_text("utf-8"))


def save_meta(sid: str, meta: dict) -> None:
    meta["updated_at"] = now_iso()
    body = json.dumps(meta, indent=2, sort_keys=True) + "\n"
    atomic_write_text(session_dir(sid) / "meta.json", body)


def read_spec(dir_: Path, basename: str):
    md_path = dir_ / f"{basename}.md"
    json_path = dir_ / f"{basename}.decisions.json"
    if not md_path.exists() or not json_path.exists():
        sys.stderr.write(
            f"error: expected {basename}.md and {basename}.decisions.json in {dir_}\n"
        )
        sys.exit(2)
    md = md_path.read_bytes()
    js = json_path.read_bytes()
    try:
        sidecar = json.loads(js)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"error: {json_path} is not valid JSON: {e}\n")
        sys.exit(2)
    return md, js, sidecar


def session_url(sid: str) -> str:
    return f"http://127.0.0.1:{DEFAULT_PORT}/sessions/{sid}"


class CommandError(Exception):
    """Raised to short-circuit a command with a specific exit code."""

    def __init__(self, code: int):
        super().__init__(f"exit {code}")
        self.code = code


def resolve_session(sid: str) -> dict:
    """Load and return a session's meta.json; raise CommandError on failure.

    Exit 2 = malformed session id, exit 3 = well-formed but missing.
    """
    try:
        return load_meta(sid)
    except InvalidSessionId:
        sys.stderr.write(
            f"error: invalid session id {sid!r} (expected 12 hex chars)\n"
        )
        raise CommandError(2)
    except SessionNotFound:
        sys.stderr.write(f"error: session {sid} not found\n")
        raise CommandError(3)


# === commands ===


def cmd_submit(args) -> int:
    dir_ = Path(args.dir).resolve()
    md_bytes, json_bytes, sidecar = read_spec(dir_, args.basename)
    md_hash = sha256_hex(md_bytes)
    json_hash = sha256_hex(json_bytes)

    if args.session is not None:
        meta = resolve_session(args.session)
        sid = args.session
        # Cross-spec contamination guards: an existing session is pinned to a
        # specific (basename, spec_id). Reject mismatched submits.
        if args.basename != meta.get("basename"):
            sys.stderr.write(
                f"error: session {sid} is for basename {meta.get('basename')!r}, "
                f"not {args.basename!r}\n"
            )
            return 2
        incoming_spec_id = sidecar.get("spec_id")
        if incoming_spec_id != meta.get("spec_id"):
            sys.stderr.write(
                f"error: session {sid} is for spec_id {meta.get('spec_id')!r}, "
                f"not {incoming_spec_id!r}\n"
            )
            return 2
        cur = meta.get("current_revision", 0)
        cur_hashes = meta.get("revisions", {}).get(str(cur), {})
        if (
            cur
            and cur_hashes.get("md_hash") == md_hash
            and cur_hashes.get("json_hash") == json_hash
        ):
            _emit({
                "session_id": sid,
                "revision": cur,
                "status": meta["status"],
                "idempotent": True,
                "url": session_url(sid),
            })
            return 0
        new_rev = cur + 1
    else:
        sid = new_session_id()
        meta = {
            "session_id": sid,
            "project_path": str(dir_),
            "basename": args.basename,
            "spec_id": sidecar.get("spec_id"),
            "spec_title": sidecar.get("spec_title"),
            "source_origin": str(dir_ / f"{args.basename}.md"),
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "current_revision": 0,
            "status": "awaiting_review",
            "revisions": {},
        }
        new_rev = 1

    rev_dir = session_dir(sid) / "revisions" / str(new_rev)
    rev_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(rev_dir / "source.md", md_bytes)
    atomic_write_bytes(rev_dir / "decisions.json", json_bytes)
    atomic_write_text(rev_dir / "submitted_at", now_iso() + "\n")

    meta["current_revision"] = new_rev
    meta["status"] = "awaiting_review"
    meta.setdefault("revisions", {})[str(new_rev)] = {
        "md_hash": md_hash,
        "json_hash": json_hash,
        "submitted_at": now_iso(),
    }
    save_meta(sid, meta)

    _emit({
        "session_id": sid,
        "revision": new_rev,
        "status": "awaiting_review",
        "url": session_url(sid),
    })
    return 0


def cmd_submit_review(args) -> int:
    meta = resolve_session(args.session)
    review_path = Path(args.review_path)
    if not review_path.exists():
        sys.stderr.write(f"error: review file {review_path} not found\n")
        return 2
    review_bytes = review_path.read_bytes()
    try:
        review = json.loads(review_bytes)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"error: {review_path} is not valid JSON: {e}\n")
        return 2
    cur = meta["current_revision"]
    if cur < 1:
        sys.stderr.write(f"error: session {args.session} has no revisions yet\n")
        return 2
    # Validate that this review actually belongs to the current revision.
    if review.get("spec_id") != meta.get("spec_id"):
        sys.stderr.write(
            f"error: review spec_id {review.get('spec_id')!r} does not match "
            f"session spec_id {meta.get('spec_id')!r}\n"
        )
        return 2
    cur_decisions_path = (
        session_dir(args.session) / "revisions" / str(cur) / "decisions.json"
    )
    cur_sidecar = json.loads(cur_decisions_path.read_text("utf-8"))
    if review.get("spec_version") != cur_sidecar.get("version"):
        sys.stderr.write(
            f"error: review spec_version {review.get('spec_version')!r} does "
            f"not match session current revision (rev {cur}) version "
            f"{cur_sidecar.get('version')!r}\n"
        )
        return 2
    rev_dir = session_dir(args.session) / "reviews" / str(cur)
    rev_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(rev_dir / "review.json", review_bytes)
    atomic_write_text(rev_dir / "submitted_at", now_iso() + "\n")
    meta["status"] = "review_submitted"
    save_meta(args.session, meta)
    _emit({
        "session_id": args.session,
        "revision": cur,
        "status": "review_submitted",
    })
    return 0


def cmd_pull(args) -> int:
    meta = resolve_session(args.session)
    cur = meta["current_revision"]
    review_path = session_dir(args.session) / "reviews" / str(cur) / "review.json"
    if not review_path.exists():
        sys.stderr.write(
            f"no review for session {args.session} current revision (rev {cur})\n"
        )
        return 4
    body = review_path.read_text("utf-8")
    sys.stdout.write(body)
    if not body.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def cmd_applied(args) -> int:
    meta = resolve_session(args.session)
    meta["status"] = "applied"
    save_meta(args.session, meta)
    _emit({"session_id": args.session, "status": "applied"})
    return 0


def cmd_dismiss(args) -> int:
    meta = resolve_session(args.session)
    meta["status"] = "closed"
    save_meta(args.session, meta)
    _emit({"session_id": args.session, "status": "closed"})
    return 0


def cmd_list(args) -> int:
    rows = []
    for d in sorted(sessions_root().iterdir()):
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text("utf-8"))
        if not args.all and meta.get("status") == "closed":
            continue
        rows.append({
            "session_id": meta["session_id"],
            "project_path": meta["project_path"],
            "basename": meta["basename"],
            "spec_title": meta.get("spec_title"),
            "status": meta["status"],
            "current_revision": meta["current_revision"],
            "updated_at": meta["updated_at"],
            "url": session_url(meta["session_id"]),
        })
    rows.sort(key=lambda r: r["updated_at"], reverse=True)
    sys.stdout.write(json.dumps(rows, indent=2) + "\n")
    return 0


def cmd_status(args) -> int:
    meta = resolve_session(args.session)
    sys.stdout.write(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    return 0


def cmd_open(args) -> int:
    resolve_session(args.session)
    sys.stdout.write(session_url(args.session) + "\n")
    return 0


def _emit(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="riview",
        description="Session inbox CLI for RIView. Stores specs + reviews under ~/.riview/sessions/.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser(
        "submit",
        help="Register a spec (md + sidecar) as a new session or a new revision of one.",
    )
    s.add_argument("dir", help="Directory containing the spec files.")
    s.add_argument(
        "--basename", default="spec", help="Spec basename (default: spec)."
    )
    s.add_argument(
        "--session",
        help="Existing session ID. If omitted, a new session is created.",
    )
    s.set_defaults(func=cmd_submit)

    s = sub.add_parser(
        "submit-review",
        help="Record a review JSON against the current revision of a session.",
    )
    s.add_argument("session", help="Session ID.")
    s.add_argument("review_path", help="Path to review JSON file.")
    s.set_defaults(func=cmd_submit_review)

    s = sub.add_parser(
        "pull",
        help="Print the latest review for the session's current revision; exit 4 if none.",
    )
    s.add_argument("session", help="Session ID.")
    s.set_defaults(func=cmd_pull)

    s = sub.add_parser(
        "applied",
        help="Mark a session as applied (agent has consumed the review).",
    )
    s.add_argument("session", help="Session ID.")
    s.set_defaults(func=cmd_applied)

    s = sub.add_parser("dismiss", help="Close a session (status: closed).")
    s.add_argument("session", help="Session ID.")
    s.set_defaults(func=cmd_dismiss)

    s = sub.add_parser(
        "list", help="List sessions (open by default; --all includes closed)."
    )
    s.add_argument("--all", action="store_true", help="Include closed sessions.")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("status", help="Print full meta.json for a session.")
    s.add_argument("session", help="Session ID.")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser(
        "open", help="Print the daemon URL for a session (does not launch a browser)."
    )
    s.add_argument("session", help="Session ID.")
    s.set_defaults(func=cmd_open)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CommandError as e:
        return e.code


if __name__ == "__main__":
    sys.exit(main())
