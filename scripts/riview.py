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
import html as html_lib
import json
import os
import re
import secrets
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

try:
    import fcntl  # POSIX only; daemon + CLI locking degrade to no-op elsewhere.
except ImportError:
    fcntl = None  # type: ignore[assignment]

# render.py lives next to this file; import it without depending on packaging.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import render  # noqa: E402

DEFAULT_PORT = 7891  # daemon listens here; CLI uses it to print URLs.
MAX_REVIEW_BYTES = 1 * 1024 * 1024  # 1 MiB cap on /review POST bodies.

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


@contextmanager
def session_write_lock(sid: str):
    """Serialise concurrent writers (CLI + daemon) on a per-session lock file.

    Atomic writes already protect readers from torn files. The lock prevents
    two writers from racing on `current_revision` increments or stomping each
    other's meta updates.
    """
    sd = session_dir(sid)
    sd.mkdir(parents=True, exist_ok=True)
    lock_path = sd / ".lock"
    if fcntl is None:
        # Non-POSIX fallback: skip locking. Single-user, low contention.
        yield
        return
    with open(lock_path, "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def token_path() -> Path:
    return riview_home() / "token"


def ensure_token() -> str:
    """Read ~/.riview/token, creating it with 0o600 perms on first call."""
    p = token_path()
    if p.exists():
        return p.read_text("utf-8").strip()
    p.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(24)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token


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
        # Validate the id first so a bad input fails fast without taking a lock.
        resolve_session(args.session)
        sid = args.session
        with session_write_lock(sid):
            meta = load_meta(sid)
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
            _write_revision(sid, new_rev, md_bytes, json_bytes, md_hash, json_hash, meta)
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
        with session_write_lock(sid):
            _write_revision(sid, new_rev, md_bytes, json_bytes, md_hash, json_hash, meta)

    _emit({
        "session_id": sid,
        "revision": new_rev,
        "status": "awaiting_review",
        "url": session_url(sid),
    })
    return 0


def _write_revision(
    sid: str,
    new_rev: int,
    md_bytes: bytes,
    json_bytes: bytes,
    md_hash: str,
    json_hash: str,
    meta: dict,
) -> None:
    """Persist a new revision and update meta. Caller holds session_write_lock."""
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


class ReviewError(Exception):
    """Raised by record_review_for_session for any validation/write failure."""

    def __init__(self, message: str, http_status: int = 400):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


def record_review_for_session(sid: str, review_bytes: bytes) -> dict:
    """Validate + persist a review for a session's current revision.

    Shared by `submit-review` CLI and the HTTP POST endpoint. Holds the
    per-session write lock around the meta update.

    Raises ReviewError on bad input. Returns the emit-ready payload on success.
    """
    try:
        review = json.loads(review_bytes)
    except json.JSONDecodeError as e:
        raise ReviewError(f"invalid JSON: {e}", http_status=400)
    if not isinstance(review, dict):
        raise ReviewError("review must be a JSON object", http_status=400)

    with session_write_lock(sid):
        meta = load_meta(sid)  # raises SessionNotFound, handled at call sites
        cur = meta["current_revision"]
        if cur < 1:
            raise ReviewError(f"session {sid} has no revisions yet", http_status=409)
        if review.get("spec_id") != meta.get("spec_id"):
            raise ReviewError(
                f"review spec_id {review.get('spec_id')!r} does not match "
                f"session spec_id {meta.get('spec_id')!r}",
                http_status=409,
            )
        cur_decisions_path = (
            session_dir(sid) / "revisions" / str(cur) / "decisions.json"
        )
        cur_sidecar = json.loads(cur_decisions_path.read_text("utf-8"))
        if review.get("spec_version") != cur_sidecar.get("version"):
            raise ReviewError(
                f"review spec_version {review.get('spec_version')!r} does not "
                f"match session current revision (rev {cur}) version "
                f"{cur_sidecar.get('version')!r}",
                http_status=409,
            )
        rev_dir = session_dir(sid) / "reviews" / str(cur)
        rev_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(rev_dir / "review.json", review_bytes)
        atomic_write_text(rev_dir / "submitted_at", now_iso() + "\n")
        meta["status"] = "review_submitted"
        save_meta(sid, meta)
        return {
            "session_id": sid,
            "revision": cur,
            "status": "review_submitted",
        }


def cmd_submit_review(args) -> int:
    resolve_session(args.session)  # exit 2/3 on bad id / missing session
    review_path = Path(args.review_path)
    if not review_path.exists():
        sys.stderr.write(f"error: review file {review_path} not found\n")
        return 2
    review_bytes = review_path.read_bytes()
    try:
        payload = record_review_for_session(args.session, review_bytes)
    except ReviewError as e:
        sys.stderr.write(f"error: {e.message}\n")
        return 2
    _emit(payload)
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
    resolve_session(args.session)
    with session_write_lock(args.session):
        meta = load_meta(args.session)
        meta["status"] = "applied"
        save_meta(args.session, meta)
    _emit({"session_id": args.session, "status": "applied"})
    return 0


def cmd_dismiss(args) -> int:
    resolve_session(args.session)
    with session_write_lock(args.session):
        meta = load_meta(args.session)
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


def _list_session_rows(include_closed: bool) -> list[dict]:
    rows = []
    for d in sorted(sessions_root().iterdir()):
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text("utf-8"))
        if not include_closed and meta.get("status") == "closed":
            continue
        rows.append(meta)
    rows.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
    return rows


def _render_index_html(rows: list[dict]) -> str:
    cells = []
    for m in rows:
        sid = m["session_id"]
        cells.append(
            "<tr>"
            f"<td><a href='/sessions/{html_lib.escape(sid)}'>{html_lib.escape(sid)}</a></td>"
            f"<td>{html_lib.escape(m.get('spec_title') or '')}</td>"
            f"<td>{html_lib.escape(m.get('basename') or '')}</td>"
            f"<td>{html_lib.escape(m.get('status') or '')}</td>"
            f"<td>rev {html_lib.escape(str(m.get('current_revision') or ''))}</td>"
            f"<td>{html_lib.escape(m.get('updated_at') or '')}</td>"
            f"<td>{html_lib.escape(m.get('project_path') or '')}</td>"
            "</tr>"
        )
    body = "".join(cells) or "<tr><td colspan='7'><em>No open sessions.</em></td></tr>"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>RIView sessions</title>"
        "<style>"
        "body{font-family:system-ui,sans-serif;margin:2rem;color:#1a1a1a;}"
        "h1{font-size:1.25rem;}table{border-collapse:collapse;width:100%;}"
        "th,td{padding:.5rem .75rem;border-bottom:1px solid #d8dee5;text-align:left;font-size:.9rem;}"
        "th{background:#f4f6f9;}a{color:#2452a3;text-decoration:none;}a:hover{text-decoration:underline;}"
        "</style></head><body>"
        "<h1>RIView sessions</h1>"
        "<table><thead><tr>"
        "<th>session</th><th>title</th><th>basename</th><th>status</th>"
        "<th>revision</th><th>updated</th><th>project</th>"
        "</tr></thead><tbody>"
        f"{body}"
        "</tbody></table></body></html>"
    )


def _render_session_html(sid: str, meta: dict, submit_token: str) -> str:
    cur = meta.get("current_revision", 0)
    if cur < 1:
        return (
            "<!doctype html><html><body><p>session "
            f"{html_lib.escape(sid)} has no revisions yet.</p></body></html>"
        )
    rev_dir = session_dir(sid) / "revisions" / str(cur)
    md_text = (rev_dir / "source.md").read_text("utf-8")
    spec = json.loads((rev_dir / "decisions.json").read_text("utf-8"))
    bodies = render.parse_anchored_bodies(md_text)
    return render.build_html(
        spec,
        bodies,
        submit_url=f"/sessions/{sid}/review",
        submit_token=submit_token,
    )


class RIViewHandler(BaseHTTPRequestHandler):
    server_version = "RIView/1.0"

    # quiet down the default stderr access log
    def log_message(self, format, *args):  # noqa: A002
        pass

    def _write(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, status: int, msg: str) -> None:
        self._write(status, (msg + "\n").encode("utf-8"), "text/plain; charset=utf-8")

    def _json(self, status: int, payload: dict) -> None:
        body = (json.dumps(payload) + "\n").encode("utf-8")
        self._write(status, body, "application/json; charset=utf-8")

    def _html(self, status: int, body: str) -> None:
        self._write(status, body.encode("utf-8"), "text/html; charset=utf-8")

    def do_GET(self):  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/" or path == "/index.html":
            rows = _list_session_rows(include_closed=False)
            self._html(HTTPStatus.OK, _render_index_html(rows))
            return
        m = re.fullmatch(r"/sessions/([0-9a-f]{12})", path)
        if m:
            sid = m.group(1)
            try:
                meta = resolve_session(sid)
            except CommandError as e:
                self._text(
                    HTTPStatus.NOT_FOUND if e.code == 3 else HTTPStatus.BAD_REQUEST,
                    f"session {sid}: {'not found' if e.code == 3 else 'invalid id'}",
                )
                return
            token = getattr(self.server, "riview_token", "")
            self._html(HTTPStatus.OK, _render_session_html(sid, meta, token))
            return
        self._text(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self):  # noqa: N802
        path = urlsplit(self.path).path
        m = re.fullmatch(r"/sessions/([0-9a-f]{12})/review", path)
        if not m:
            self._text(HTTPStatus.NOT_FOUND, "not found")
            return
        sid = m.group(1)

        # Token check first — defends against drive-by cross-origin POST.
        expected = getattr(self.server, "riview_token", "")
        provided = self.headers.get("X-Riview-Token", "")
        if not expected or provided != expected:
            self._text(HTTPStatus.FORBIDDEN, "missing or invalid X-Riview-Token")
            return

        length_header = self.headers.get("Content-Length")
        if not length_header or not length_header.isdigit():
            self._text(HTTPStatus.LENGTH_REQUIRED, "Content-Length required")
            return
        length = int(length_header)
        if length > MAX_REVIEW_BYTES:
            self._text(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "review too large")
            return
        body = self.rfile.read(length)

        try:
            resolve_session(sid)
        except CommandError as e:
            self._text(
                HTTPStatus.NOT_FOUND if e.code == 3 else HTTPStatus.BAD_REQUEST,
                f"session {sid}: {'not found' if e.code == 3 else 'invalid id'}",
            )
            return

        try:
            payload = record_review_for_session(sid, body)
        except ReviewError as e:
            self._text(e.http_status, e.message)
            return
        self._json(HTTPStatus.OK, payload)


def cmd_daemon(args) -> int:
    host = args.host
    port = args.port
    token = ensure_token()
    server = ThreadingHTTPServer((host, port), RIViewHandler)
    server.riview_token = token  # type: ignore[attr-defined]
    sys.stderr.write(
        f"riview daemon listening on http://{host}:{port}/ "
        f"(token at {token_path()})\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nriview daemon shutting down\n")
    finally:
        server.server_close()
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

    s = sub.add_parser(
        "daemon",
        help="Run the HTTP daemon (browser review UI). Defaults to 127.0.0.1:7891.",
    )
    s.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1).")
    s.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"Port (default {DEFAULT_PORT})."
    )
    s.set_defaults(func=cmd_daemon)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CommandError as e:
        return e.code


if __name__ == "__main__":
    sys.exit(main())
