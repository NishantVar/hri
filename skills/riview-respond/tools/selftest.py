#!/usr/bin/env python3
"""Skill-local selftest for skills/riview-respond/.

Runs without a live daemon or session. Verifies:

1. preflight.py and snapshot.py py_compile.
2. Both helpers accept --help and exit 0.
3. Both helpers exit 2 on a missing session.
4. preflight.py exits 0 against a matching synthetic $RIVIEW_HOME fixture.
5. preflight.py exits 3 when the project files drift from the recorded hash.
6. snapshot.py exits 0 against a synthetic fixture and emits a review_hash when
   a review.json is present.

Exit codes:
    0  - all checks passed
    1  - one or more checks failed (per-check report on stderr)

Usage:
    selftest.py
"""
from __future__ import annotations

import hashlib
import json
import py_compile
import subprocess
import sys
import tempfile
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
PREFLIGHT = TOOLS_DIR / "preflight.py"
SNAPSHOT = TOOLS_DIR / "snapshot.py"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run(script: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
    )


def _make_fixture(root: Path, *, drift: bool, with_review: bool) -> str:
    """Build a minimal $RIVIEW_HOME + project dir. Returns session_id."""
    session_id = "deadbeefcafe"
    project = root / "project"
    project.mkdir()
    md_bytes = b"# spec\n\n<!-- node:n1 -->\nbody\n<!-- /node:n1 -->\n"
    json_bytes = b'{"version": 1, "nodes": []}\n'
    (project / "spec.md").write_bytes(md_bytes)
    (project / "spec.decisions.json").write_bytes(json_bytes)

    md_hash = _sha256(md_bytes)
    json_hash = _sha256(json_bytes)

    session_dir = root / "sessions" / session_id
    session_dir.mkdir(parents=True)
    meta = {
        "current_revision": 1,
        "event_seq": 7,
        "status": "awaiting_review",
        "project_path": str(project),
        "basename": "spec",
        "revisions": {
            "1": {"md_hash": md_hash, "json_hash": json_hash},
        },
    }
    (session_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    if with_review:
        reviews = session_dir / "reviews" / "1"
        reviews.mkdir(parents=True)
        (reviews / "review.json").write_text('{"entries": []}\n', encoding="utf-8")

    if drift:
        (project / "spec.md").write_bytes(md_bytes + b"drifted\n")

    return session_id


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        marker = "PASS" if ok else "FAIL"
        print(f"  [{marker}] {name}" + (f" - {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(f"{name}: {detail}")

    print("riview-respond selftest")
    print("-----------------------")

    # 1. py_compile
    for script in (PREFLIGHT, SNAPSHOT):
        try:
            py_compile.compile(str(script), doraise=True)
            check(f"py_compile {script.name}", True)
        except py_compile.PyCompileError as e:
            check(f"py_compile {script.name}", False, str(e))

    # 2. --help on both
    for script in (PREFLIGHT, SNAPSHOT):
        r = _run(script, ["--help"])
        check(
            f"{script.name} --help exits 0",
            r.returncode == 0 and "usage:" in r.stdout.lower(),
            f"rc={r.returncode} stdout={r.stdout!r}",
        )

    # 3. Missing session -> exit 2 for both
    with tempfile.TemporaryDirectory() as td:
        empty_home = str(Path(td))
        for script in (PREFLIGHT, SNAPSHOT):
            r = _run(script, ["nonesuch", "--riview-home", empty_home])
            check(
                f"{script.name} missing session -> exit 2",
                r.returncode == 2,
                f"rc={r.returncode} stderr={r.stderr!r}",
            )

    # 4. preflight exit 0 against matching fixture
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        sid = _make_fixture(root, drift=False, with_review=False)
        r = _run(PREFLIGHT, [sid, "--riview-home", str(root)])
        ok = r.returncode == 0
        if ok:
            try:
                out = json.loads(r.stdout)
                ok = out.get("current_revision") == 1 and out.get("basename") == "spec"
            except json.JSONDecodeError:
                ok = False
        check(
            "preflight matching fixture -> exit 0 + JSON",
            ok,
            f"rc={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}",
        )

    # 5. preflight exit 3 on drift
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        sid = _make_fixture(root, drift=True, with_review=False)
        r = _run(PREFLIGHT, [sid, "--riview-home", str(root)])
        check(
            "preflight drifted fixture -> exit 3",
            r.returncode == 3 and "drifted" in r.stderr,
            f"rc={r.returncode} stderr={r.stderr!r}",
        )

    # 6. snapshot exit 0 + review_hash present when review.json exists
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        sid = _make_fixture(root, drift=False, with_review=True)
        r = _run(SNAPSHOT, [sid, "--riview-home", str(root)])
        ok = r.returncode == 0
        if ok:
            try:
                out = json.loads(r.stdout)
                ok = (
                    out.get("current_revision") == 1
                    and out.get("event_seq") == 7
                    and out.get("status") == "awaiting_review"
                    and isinstance(out.get("review_hash"), str)
                    and len(out["review_hash"]) == 64
                )
            except json.JSONDecodeError:
                ok = False
        check(
            "snapshot fixture (with review) -> exit 0 + review_hash",
            ok,
            f"rc={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}",
        )

    print("-----------------------")
    if failures:
        print(f"{len(failures)} check(s) failed", file=sys.stderr)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
