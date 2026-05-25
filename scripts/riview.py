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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
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
import apply as apply_mod  # noqa: E402

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
    # Storage root precedence: $RIVIEW_HOME, else repo-local `<riview_repo>/.riview/`.
    # The default lives next to the daemon source so one RIView checkout backs
    # all consuming-project agents that submit to it. Existing `~/.riview/`
    # deployments keep working by exporting `RIVIEW_HOME=~/.riview`.
    override = os.environ.get("RIVIEW_HOME")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / ".riview"


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
        # fsync the parent dir so the rename itself survives a crash, not just
        # the file contents. Best-effort; skip silently on platforms (e.g.
        # Windows) where directory fsync is unsupported.
        try:
            dfd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
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
    """Read ~/.riview/token, creating it with 0o600 perms on first call.

    Repairs loose perms on an existing token file so an earlier (pre-1b) or
    user-edited token isn't world-readable while we depend on it for auth.
    """
    p = token_path()
    if p.exists():
        try:
            mode = p.stat().st_mode & 0o777
            if mode != 0o600:
                os.chmod(p, 0o600)
        except OSError:
            pass  # best-effort; non-POSIX or unusual fs
        return p.read_text("utf-8").strip()
    p.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(24)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Per-session Condition objects, lazily created. Writers call _notify_session
# after persisting changes; long-poll / SSE readers wait on the matching
# Condition. The dict itself is guarded by _session_events_lock.
_session_events_lock = threading.Lock()
_session_events: dict[str, threading.Condition] = {}


def _session_event(sid: str) -> threading.Condition:
    with _session_events_lock:
        cond = _session_events.get(sid)
        if cond is None:
            cond = threading.Condition()
            _session_events[sid] = cond
        return cond


def _notify_session(sid: str) -> None:
    cond = _session_event(sid)
    with cond:
        cond.notify_all()


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


def validate_spec_pair(md_bytes: bytes, sidecar: dict) -> list[str]:
    """Apply render.validate() to a spec pair.

    Used by both submit (pre-persistence) and the daemon (pre-render). Keeps
    one definition of "well-formed enough to safely render": fail fast on
    bad input rather than letting it land on disk or in a token-bearing page.

    Also performs the shape checks that render.validate() assumes (nodes is
    a list of dicts with an id), since render.validate() would otherwise
    traceback on malformed-but-syntactically-valid sidecars.
    """
    try:
        md_text = md_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        return [f"source markdown is not valid utf-8: {e}"]
    if not isinstance(sidecar, dict):
        return ["decisions sidecar must be a JSON object"]
    if "nodes" not in sidecar:
        return ["decisions sidecar must have a 'nodes' array"]
    nodes = sidecar["nodes"]
    if not isinstance(nodes, list):
        return [f"'nodes' must be an array, got {type(nodes).__name__}"]
    shape_errors: list[str] = []
    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            shape_errors.append(
                f"nodes[{i}] must be an object, got {type(node).__name__}"
            )
            continue
        for required in ("id", "kind"):
            if required not in node:
                shape_errors.append(f"nodes[{i}] missing required field {required!r}")
    if shape_errors:
        return shape_errors
    anchor_counts = render.count_anchor_openings(md_text)
    return render.validate(sidecar, anchor_counts)


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
    errors = validate_spec_pair(md_bytes, sidecar)
    if errors:
        sys.stderr.write(
            f"error: spec failed validation ({len(errors)} issue"
            f"{'s' if len(errors) != 1 else ''}):\n"
        )
        for e in errors:
            sys.stderr.write(f"  - {e}\n")
        return 2
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
                    "event_seq": int(meta.get("event_seq", 0)),
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

    final_meta = load_meta(sid)
    _emit({
        "session_id": sid,
        "revision": new_rev,
        "status": "awaiting_review",
        "event_seq": int(final_meta.get("event_seq", 0)),
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
    meta["event_seq"] = int(meta.get("event_seq", 0)) + 1
    save_meta(sid, meta)
    _notify_session(sid)


class ReviewError(Exception):
    """Raised by record_review_for_session for any validation/write failure."""

    def __init__(self, message: str, http_status: int = 400):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


def _merge_reviews_by_node_id(
    existing: list[dict], incoming: list[dict]
) -> list[dict]:
    """Upsert incoming entries into existing by node_id; sort by node_id.

    Later entries (incoming) win on conflict. Order within the result is
    deterministic (sorted by node_id) so concurrent writers converge.
    """
    by_id: dict[str, dict] = {}
    for entry in existing:
        if isinstance(entry, dict) and isinstance(entry.get("node_id"), str):
            by_id[entry["node_id"]] = entry
    for entry in incoming:
        by_id[entry["node_id"]] = entry
    return sorted(by_id.values(), key=lambda e: e["node_id"])


# Fields included in a node's stale-check fingerprint, by kind. The reviewer
# saw these (title/status/body/etc.) when forming their opinion; if any change
# between base and current, the per-entry submit is reported as a conflict so
# the reviewer can decide whether to re-review.
_FINGERPRINT_BASE_FIELDS = ("kind", "title", "status", "source_anchor", "depends_on")
_FINGERPRINT_KIND_FIELDS = {
    "decision": ("rationale", "alternatives"),
    "ambiguity": ("prompt", "options", "resolution"),
    "risk": ("severity", "mitigation"),
}


def _node_fingerprint(node: dict, body_md: str) -> str:
    """Deterministic fingerprint over the user-visible parts of a node.

    Excludes `review` (prior-pass metadata) and `confidence` (AI-internal).
    Excluding them keeps a pure-status confirm cycle from invalidating later
    reviews on adjacent nodes.
    """
    payload: dict = {"body_md": body_md}
    for f in _FINGERPRINT_BASE_FIELDS:
        payload[f] = node.get(f)
    for f in _FINGERPRINT_KIND_FIELDS.get(node.get("kind", ""), ()):
        payload[f] = node.get(f)
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _load_rev_spec(sid: str, rev: int) -> tuple[dict, str]:
    """Return (sidecar, md_text) for a session revision. Raises FileNotFoundError."""
    rev_dir = session_dir(sid) / "revisions" / str(rev)
    sidecar = json.loads((rev_dir / "decisions.json").read_text("utf-8"))
    md_text = (rev_dir / "source.md").read_text("utf-8")
    return sidecar, md_text


def _fingerprint_index(sidecar: dict, md_text: str) -> dict[str, str]:
    """Map node_id -> fingerprint for every node in a revision."""
    bodies = render.parse_anchored_bodies(md_text)
    out: dict[str, str] = {}
    for n in sidecar.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        nid = n.get("id")
        if not isinstance(nid, str):
            continue
        out[nid] = _node_fingerprint(n, bodies.get(n.get("source_anchor", nid), ""))
    return out


def record_review_for_session(
    sid: str,
    review_bytes: bytes,
    *,
    require_base_revision: bool = False,
) -> dict:
    """Validate + persist a review for a session's current revision.

    Shared by `submit-review` CLI and the HTTP POST endpoint. Holds the
    per-session write lock around the meta update.

    Two staleness modes:
    - If the body contains `base_revision` (browser POSTs after slice 1):
      per-node optimistic concurrency. For each entry, the daemon fingerprints
      the node at base_revision and at current_revision; equal => accept, differ
      (or new-since-base) => conflict. Only accepted entries are persisted, and
      they are rewritten to the current `spec_version` so the on-disk review
      file is always coherent with the current revision.
    - If the body has no `base_revision` (legacy CLI path): the `spec_version`
      must match the current revision's version exactly, and all entries are
      accepted. `require_base_revision=True` (set by the HTTP handler) rejects
      this path with 400 — only the CLI may submit without `base_revision`.

    On repeated POSTs against the same revision, `reviews[]` is merged by
    `node_id` (upsert: later entries win) so per-card and batch submits can
    coexist without one stomping the other. Other top-level fields take the
    latest POST's values.

    Raises ReviewError on bad input. Returns the emit-ready payload on success.
    """
    try:
        review = json.loads(review_bytes)
    except json.JSONDecodeError as e:
        raise ReviewError(f"invalid JSON: {e}", http_status=400)
    if not isinstance(review, dict):
        raise ReviewError("review must be a JSON object", http_status=400)
    incoming_reviews = review.get("reviews")
    if not isinstance(incoming_reviews, list):
        raise ReviewError("review.reviews must be a list", http_status=400)
    for i, entry in enumerate(incoming_reviews):
        if not isinstance(entry, dict):
            raise ReviewError(
                f"review.reviews[{i}] must be an object", http_status=400
            )
        nid = entry.get("node_id")
        if not isinstance(nid, str) or not nid:
            raise ReviewError(
                f"review.reviews[{i}].node_id must be a non-empty string",
                http_status=400,
            )

    base_revision_raw = review.get("base_revision")
    has_base = "base_revision" in review and base_revision_raw is not None
    if not has_base and require_base_revision:
        raise ReviewError(
            "POST body must include integer 'base_revision' "
            "(the session revision the page rendered against)",
            http_status=400,
        )
    base_revision: int | None = None
    if has_base:
        if isinstance(base_revision_raw, bool) or not isinstance(base_revision_raw, int):
            raise ReviewError(
                f"base_revision must be an integer, got {type(base_revision_raw).__name__}",
                http_status=400,
            )
        base_revision = base_revision_raw

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

        cur_sidecar, cur_md = _load_rev_spec(sid, cur)

        if has_base:
            # base_revision must refer to a known revision in this session.
            if str(base_revision) not in (meta.get("revisions") or {}):
                raise ReviewError(
                    f"unknown base_revision {base_revision!r} for session {sid} "
                    f"(known: {sorted(int(r) for r in (meta.get('revisions') or {}))})",
                    http_status=400,
                )
        else:
            # Legacy CLI path: require spec_version == current.
            if review.get("spec_version") != cur_sidecar.get("version"):
                raise ReviewError(
                    f"review spec_version {review.get('spec_version')!r} does not "
                    f"match session current revision (rev {cur}) version "
                    f"{cur_sidecar.get('version')!r}",
                    http_status=409,
                )

        # Reject unknown node_ids up-front (would fail apply.py later anyway).
        known_node_ids = {
            n["id"] for n in cur_sidecar.get("nodes") or [] if isinstance(n, dict) and isinstance(n.get("id"), str)
        }
        unknown = [
            entry["node_id"] for entry in incoming_reviews
            if entry["node_id"] not in known_node_ids
        ]
        if unknown:
            raise ReviewError(
                f"unknown node_id(s) for rev {cur}: {sorted(set(unknown))}",
                http_status=409,
            )

        # Drop entries whose only field is node_id — they would silently clobber
        # a prior real review for the same node on merge. Schema rule: "Entries
        # with all fields null/empty are dropped silently."
        filtered_incoming = [
            entry for entry in incoming_reviews if not apply_mod.is_empty_entry(entry)
        ]

        # Per-node staleness split. CLI path (no base_revision) accepts everything.
        accepted_entries: list[dict] = []
        conflict_records: list[dict] = []
        if has_base and base_revision is not None and base_revision != cur:
            base_sidecar, base_md = _load_rev_spec(sid, base_revision)
            base_fp = _fingerprint_index(base_sidecar, base_md)
            cur_fp = _fingerprint_index(cur_sidecar, cur_md)
            for entry in filtered_incoming:
                nid = entry["node_id"]
                if nid not in base_fp:
                    conflict_records.append({
                        "node_id": nid,
                        "reason": "node did not exist at base_revision",
                        "base_revision": base_revision,
                        "current_revision": cur,
                    })
                elif base_fp[nid] != cur_fp.get(nid):
                    conflict_records.append({
                        "node_id": nid,
                        "reason": "node changed between base_revision and current_revision",
                        "base_revision": base_revision,
                        "current_revision": cur,
                    })
                else:
                    accepted_entries.append(entry)
        else:
            accepted_entries = list(filtered_incoming)

        rev_dir = session_dir(sid) / "reviews" / str(cur)
        rev_dir.mkdir(parents=True, exist_ok=True)
        review_path = rev_dir / "review.json"

        existing_reviews: list[dict] = []
        if review_path.exists():
            try:
                existing = json.loads(review_path.read_text("utf-8"))
                if isinstance(existing, dict) and isinstance(
                    existing.get("reviews"), list
                ):
                    existing_reviews = existing["reviews"]
            except json.JSONDecodeError:
                # Corrupted prior file shouldn't block a clean overwrite.
                existing_reviews = []

        # Persisted review file is always coherent with the current revision:
        # spec_version is normalized to current, and base_revision is stripped
        # (it described the in-flight POST, not the stored review).
        persisted_top = dict(review)
        persisted_top.pop("base_revision", None)
        persisted_top["spec_version"] = cur_sidecar.get("version")
        persisted_top["reviews"] = _merge_reviews_by_node_id(
            existing_reviews, accepted_entries
        )
        persisted_bytes = (
            json.dumps(persisted_top, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        atomic_write_bytes(review_path, persisted_bytes)
        atomic_write_text(rev_dir / "submitted_at", now_iso() + "\n")
        meta["status"] = "review_submitted"
        meta["event_seq"] = int(meta.get("event_seq", 0)) + 1
        save_meta(sid, meta)
        _notify_session(sid)
        payload: dict = {
            "session_id": sid,
            "revision": cur,
            "current_revision": cur,
            "status": "review_submitted",
            "event_seq": meta["event_seq"],
            "review_count": len(persisted_top["reviews"]),
        }
        if has_base:
            payload["base_revision"] = base_revision
            payload["accepted"] = [{"node_id": e["node_id"]} for e in accepted_entries]
            payload["conflicts"] = conflict_records
        return payload


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


def _render_session_html(
    sid: str, meta: dict, submit_token: str
) -> tuple[int, str]:
    """Return (http_status, html_body) for the per-session page.

    Re-runs render.validate() against the stored spec. If it fails, returns
    a plain error page WITHOUT the submit-config token — refusing to hand a
    write capability to an XSS vector hiding in the stored sidecar.
    """
    cur = meta.get("current_revision", 0)
    if cur < 1:
        body = (
            "<!doctype html><html><body><p>session "
            f"{html_lib.escape(sid)} has no revisions yet.</p></body></html>"
        )
        return HTTPStatus.OK, body
    rev_dir = session_dir(sid) / "revisions" / str(cur)
    md_bytes = (rev_dir / "source.md").read_bytes()
    json_bytes = (rev_dir / "decisions.json").read_bytes()
    try:
        spec = json.loads(json_bytes)
    except json.JSONDecodeError:
        spec = None
    if spec is None:
        return HTTPStatus.CONFLICT, _render_invalid_session_html(
            sid, ["decisions.json is not valid JSON"]
        )
    errors = validate_spec_pair(md_bytes, spec)
    if errors:
        return HTTPStatus.CONFLICT, _render_invalid_session_html(sid, errors)
    md_text = md_bytes.decode("utf-8")
    bodies = render.parse_anchored_bodies(md_text)
    overlay_entries = _apply_overlay_to_spec(sid, cur, spec, bodies)
    return HTTPStatus.OK, render.build_html(
        spec,
        bodies,
        submit_url=f"/sessions/{sid}/review",
        submit_token=submit_token,
        session_id=sid,
        base_revision=cur,
        overlay_entries=overlay_entries,
    )


def _apply_overlay_to_spec(
    sid: str, cur: int, spec: dict, bodies: dict[str, str]
) -> dict[str, dict]:
    """Merge the current revision's submitted review (the website's overlay)
    into the in-memory spec + bodies before render.

    Status, resolution, and body_edit fields land on the node / bodies dicts
    directly so the renderer's existing prefill picks them up. The full per-
    node overlay entries are also returned to the renderer so the client can
    emit complete entries on partial-field edits — without that, the daemon's
    by-node-id replace-merge would silently drop other already-submitted
    overlay fields (ADR-0011). Comments are tracked as part of the overlay
    entry; they prefill the textarea baseline without leaking into
    `node.review.comment` on the stored sidecar.
    """
    overlay_path = session_dir(sid) / "reviews" / str(cur) / "review.json"
    if not overlay_path.exists():
        return {}
    try:
        doc = json.loads(overlay_path.read_text("utf-8"))
    except json.JSONDecodeError:
        return {}
    entries = doc.get("reviews") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        return {}
    nodes = spec.get("nodes") if isinstance(spec, dict) else None
    if not isinstance(nodes, list):
        return {}
    node_by_id = {n["id"]: n for n in nodes if isinstance(n, dict) and "id" in n}

    overlay_entries: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        nid = entry.get("node_id")
        if not isinstance(nid, str):
            continue
        node = node_by_id.get(nid)
        if node is None:
            continue
        overlay_entries[nid] = entry
        new_status = entry.get("new_status")
        if isinstance(new_status, str) and new_status:
            node["status"] = new_status
        resolution = entry.get("resolution")
        if node.get("kind") == "ambiguity" and isinstance(resolution, dict):
            node["resolution"] = resolution
        body_edit = entry.get("body_edit")
        if isinstance(body_edit, str):
            anchor = node.get("source_anchor", nid)
            if isinstance(anchor, str):
                bodies[anchor] = body_edit
    return overlay_entries


def _render_invalid_session_html(sid: str, errors: list[str]) -> str:
    items = "".join(f"<li>{html_lib.escape(e)}</li>" for e in errors)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>session {html_lib.escape(sid)} invalid</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:2rem;color:#1a1a1a;}"
        "h1{font-size:1.1rem;}li{margin:.25rem 0;}</style></head><body>"
        f"<h1>session {html_lib.escape(sid)} failed validation</h1>"
        "<p>The stored spec for this session does not pass renderer validation. "
        "The review UI is suppressed because it would otherwise embed an auth "
        "token on a page derived from untrusted content.</p>"
        f"<ul>{items}</ul>"
        "<p>Re-submit a corrected spec with "
        "<code>riview submit &lt;dir&gt; --session "
        f"{html_lib.escape(sid)}</code>.</p>"
        "</body></html>"
    )


def _session_event_snapshot(sid: str) -> dict:
    """Cheap read-only view of session state for /wait + /events emitters."""
    meta = load_meta(sid)
    cur = int(meta.get("current_revision", 0))
    has_review = False
    if cur > 0:
        has_review = (
            session_dir(sid) / "reviews" / str(cur) / "review.json"
        ).exists()
    return {
        "session_id": sid,
        "event_seq": int(meta.get("event_seq", 0)),
        "revision": cur,
        "status": meta.get("status"),
        "has_review": has_review,
    }


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
        parts = urlsplit(self.path)
        path = parts.path
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
            status, html_body = _render_session_html(sid, meta, token)
            self._html(status, html_body)
            return
        m = re.fullmatch(r"/sessions/([0-9a-f]{12})/wait", path)
        if m:
            self._handle_wait(m.group(1), parts.query)
            return
        m = re.fullmatch(r"/sessions/([0-9a-f]{12})/events", path)
        if m:
            self._handle_events(m.group(1))
            return
        self._text(HTTPStatus.NOT_FOUND, "not found")

    def _handle_wait(self, sid: str, query: str) -> None:
        try:
            resolve_session(sid)
        except CommandError as e:
            self._text(
                HTTPStatus.NOT_FOUND if e.code == 3 else HTTPStatus.BAD_REQUEST,
                f"session {sid}: {'not found' if e.code == 3 else 'invalid id'}",
            )
            return
        qs = urllib.parse.parse_qs(query)
        try:
            since = int(qs.get("since", ["0"])[0])
        except ValueError:
            self._text(HTTPStatus.BAD_REQUEST, "since must be an integer")
            return
        try:
            timeout = float(qs.get("timeout", ["25"])[0])
        except ValueError:
            self._text(HTTPStatus.BAD_REQUEST, "timeout must be a number")
            return
        timeout = max(0.1, min(timeout, 60.0))
        cond = _session_event(sid)
        deadline = time.monotonic() + timeout
        with cond:
            snapshot = _session_event_snapshot(sid)
            while snapshot["event_seq"] <= since:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                cond.wait(timeout=remaining)
                snapshot = _session_event_snapshot(sid)
        if snapshot["event_seq"] > since:
            self._json(HTTPStatus.OK, snapshot)
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _handle_events(self, sid: str) -> None:
        try:
            resolve_session(sid)
        except CommandError as e:
            self._text(
                HTTPStatus.NOT_FOUND if e.code == 3 else HTTPStatus.BAD_REQUEST,
                f"session {sid}: {'not found' if e.code == 3 else 'invalid id'}",
            )
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # Defeat reverse-proxy buffering (nginx) when someone fronts the daemon.
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        cond = _session_event(sid)
        last_seq = -1
        try:
            while True:
                with cond:
                    snapshot = _session_event_snapshot(sid)
                    if snapshot["event_seq"] <= last_seq:
                        cond.wait(timeout=15.0)
                        snapshot = _session_event_snapshot(sid)
                if snapshot["event_seq"] > last_seq:
                    payload = json.dumps(snapshot)
                    # Default (unnamed) event so EventSource.onmessage fires.
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    last_seq = snapshot["event_seq"]
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

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
            payload = record_review_for_session(
                sid, body, require_base_revision=True
            )
        except ReviewError as e:
            self._text(e.http_status, e.message)
            return
        self._json(HTTPStatus.OK, payload)


def cmd_daemon(args) -> int:
    host = args.host
    port = args.port
    if host not in LOOPBACK_HOSTS and not args.unsafe_host:
        sys.stderr.write(
            f"error: refusing to bind {host!r} (non-loopback). The auth token "
            f"is embedded in unauthenticated GET pages, so anyone with network "
            f"access could read it and submit reviews. Re-run with "
            f"--unsafe-host to override.\n"
        )
        return 2
    if host not in LOOPBACK_HOSTS:
        sys.stderr.write(
            f"WARNING: binding {host!r} exposes session contents and the auth "
            f"token to anyone with network access to this machine.\n"
        )
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


def cmd_wait(args) -> int:
    """Block until the daemon reports a session event past `--since`.

    Designed for agents driving background work: launch this in a backgrounded
    Bash, sleep on its output (the harness wakes you when it exits), then call
    `pull` to read the new review.

    Default behavior is "wait for the next event from now" — the CLI reads
    the current event_seq from meta and uses it as the baseline. Pass an
    explicit `--since N` to wait for events past a specific sequence number.
    """
    meta = resolve_session(args.session)  # 404/2 on bad input
    base_url = args.url.rstrip("/")
    overall_deadline = (
        time.monotonic() + args.timeout if args.timeout > 0 else None
    )
    since = (
        args.since if args.since is not None else int(meta.get("event_seq", 0))
    )
    while True:
        per_request_timeout = 25.0
        if overall_deadline is not None:
            r = overall_deadline - time.monotonic()
            if r <= 0:
                sys.stderr.write(
                    f"timed out waiting for session {args.session}\n"
                )
                return 5
            per_request_timeout = min(per_request_timeout, r)
        q = urllib.parse.urlencode(
            {"since": since, "timeout": per_request_timeout}
        )
        url = f"{base_url}/sessions/{args.session}/wait?{q}"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(
                req, timeout=per_request_timeout + 10
            ) as resp:
                if resp.status == 204:
                    continue  # daemon timed out; loop and re-poll
                body = resp.read().decode("utf-8")
                payload = json.loads(body)
                _emit(payload)
                return 0
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace").strip()
            sys.stderr.write(
                f"error: daemon returned {e.code}: {detail}\n"
            )
            return 5
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            sys.stderr.write(
                f"error: cannot reach daemon at {base_url}: {e}\n"
            )
            return 5


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
        "wait",
        help="Block until a new review/revision lands on a session (long-poll against the daemon).",
    )
    s.add_argument("session", help="Session ID.")
    s.add_argument(
        "--since",
        type=int,
        default=None,
        help="Event sequence to wait past. Default: current event_seq from meta "
             "(i.e. wait for the next change from now).",
    )
    s.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        help="Overall timeout in seconds (0 = no limit; reconnect to the daemon as needed).",
    )
    s.add_argument(
        "--url",
        default=f"http://127.0.0.1:{DEFAULT_PORT}",
        help=f"Daemon base URL (default http://127.0.0.1:{DEFAULT_PORT}).",
    )
    s.set_defaults(func=cmd_wait)

    s = sub.add_parser(
        "daemon",
        help="Run the HTTP daemon (browser review UI). Defaults to 127.0.0.1:7891.",
    )
    s.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1).")
    s.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"Port (default {DEFAULT_PORT})."
    )
    s.add_argument(
        "--unsafe-host",
        action="store_true",
        help="Required to bind anything other than loopback. The token is "
             "embedded in unauthenticated GET pages, so non-loopback bind "
             "exposes it to the network.",
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
