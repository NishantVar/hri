"""Smoke tests for the riview HTTP daemon.

Run from the repo root: python3 -m unittest riview.tests.test_daemon
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RIVIEW_CLI = [sys.executable, str(REPO_ROOT / "riview" / "scripts" / "riview.py")]
SAMPLE = REPO_ROOT / "riview" / "sample"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(url: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.5).read()
            return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.1)
    raise RuntimeError(f"daemon did not come up at {url}")


class RiviewDaemonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="riview-daemon-")
        self.env = {**os.environ, "RIVIEW_HOME": self.tmp}
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc = subprocess.Popen(
            RIVIEW_CLI + ["daemon", "--port", str(self.port)],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_for(self.base + "/")
        except Exception:
            self.proc.terminate()
            self.proc.wait(timeout=2)
            raise

    def tearDown(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _submit_sample(self) -> str:
        r = subprocess.run(
            RIVIEW_CLI + ["submit", str(SAMPLE)],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(r.stdout.strip().splitlines()[-1])["session_id"]

    def _token(self) -> str:
        return (Path(self.tmp) / "token").read_text("utf-8").strip()

    def test_index_lists_session(self):
        sid = self._submit_sample()
        body = urllib.request.urlopen(self.base + "/").read().decode("utf-8")
        self.assertIn(sid, body)
        self.assertIn("Pomodoro", body)

    def test_session_page_renders(self):
        sid = self._submit_sample()
        body = urllib.request.urlopen(self.base + f"/sessions/{sid}").read().decode("utf-8")
        self.assertIn("submit-config", body)
        self.assertIn(f"/sessions/{sid}/review", body)
        # spec content should be present
        self.assertIn("pomodoro-mvp", body)

    def test_session_page_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + "/sessions/deadbeefdead").read()
        self.assertEqual(cm.exception.code, 404)

    def test_session_page_bad_id_404(self):
        # Bad id format is not matched by the route → 404, not a 500.
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + "/sessions/not-a-real-id").read()
        self.assertEqual(cm.exception.code, 404)

    def test_review_post_requires_token(self):
        sid = self._submit_sample()
        review = (SAMPLE / "review-demo.json").read_bytes()
        req = urllib.request.Request(
            self.base + f"/sessions/{sid}/review",
            data=review,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req).read()
        self.assertEqual(cm.exception.code, 403)

    def test_review_post_bad_token(self):
        sid = self._submit_sample()
        review = (SAMPLE / "review-demo.json").read_bytes()
        req = urllib.request.Request(
            self.base + f"/sessions/{sid}/review",
            data=review,
            method="POST",
            headers={"X-Riview-Token": "nope", "Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req).read()
        self.assertEqual(cm.exception.code, 403)

    def test_review_post_roundtrip(self):
        sid = self._submit_sample()
        review = (SAMPLE / "review-demo.json").read_bytes()
        req = urllib.request.Request(
            self.base + f"/sessions/{sid}/review",
            data=review,
            method="POST",
            headers={
                "X-Riview-Token": self._token(),
                "Content-Type": "application/json",
            },
        )
        resp = urllib.request.urlopen(req)
        payload = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(payload["session_id"], sid)
        self.assertEqual(payload["status"], "review_submitted")
        self.assertEqual(payload["revision"], 1)
        # CLI pull confirms persistence + matches original bytes
        pulled = subprocess.run(
            RIVIEW_CLI + ["pull", sid],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(pulled.stdout), json.loads(review))

    def test_review_post_rejects_spec_id_mismatch(self):
        sid = self._submit_sample()
        bad = json.loads((SAMPLE / "review-demo.json").read_bytes())
        bad["spec_id"] = "wrong-spec"
        req = urllib.request.Request(
            self.base + f"/sessions/{sid}/review",
            data=json.dumps(bad).encode("utf-8"),
            method="POST",
            headers={
                "X-Riview-Token": self._token(),
                "Content-Type": "application/json",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req).read()
        self.assertEqual(cm.exception.code, 409)

    def test_review_post_too_large(self):
        sid = self._submit_sample()
        huge = b"x" * (2 * 1024 * 1024)
        req = urllib.request.Request(
            self.base + f"/sessions/{sid}/review",
            data=huge,
            method="POST",
            headers={
                "X-Riview-Token": self._token(),
                "Content-Type": "application/json",
            },
        )
        # Server rejects on Content-Length before reading the body, so the
        # client may see either a clean 413 or a connection reset (depending
        # on socket buffering). Both prove enforcement; either is acceptable.
        try:
            urllib.request.urlopen(req).read()
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 413)
        except urllib.error.URLError:
            pass
        else:
            self.fail("oversized POST was accepted")
        # Sanity-check the daemon is still alive afterwards.
        urllib.request.urlopen(self.base + "/").read()

    def test_review_post_invalid_json(self):
        sid = self._submit_sample()
        req = urllib.request.Request(
            self.base + f"/sessions/{sid}/review",
            data=b"{not json",
            method="POST",
            headers={
                "X-Riview-Token": self._token(),
                "Content-Type": "application/json",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req).read()
        self.assertEqual(cm.exception.code, 400)

    def test_malformed_shape_stored_session_returns_409_without_crash(self):
        # Shape-level corruption (nodes is a string, not a list) used to
        # traceback in render.validate(). The wrapper now guards it, so the
        # daemon should return 409 just like the enum-violation case.
        sid = self._submit_sample()
        rev_dir = Path(self.tmp) / "sessions" / sid / "revisions" / "1"
        sidecar = json.loads((rev_dir / "decisions.json").read_text())
        sidecar["nodes"] = "bad"
        (rev_dir / "decisions.json").write_text(json.dumps(sidecar))

        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + f"/sessions/{sid}").read()
        self.assertEqual(cm.exception.code, 409)
        body = cm.exception.read().decode("utf-8")
        self.assertNotIn(self._token(), body)
        self.assertNotIn("submit-config", body)
        # Daemon should still be alive on the next request.
        urllib.request.urlopen(self.base + "/").read()

    def test_invalid_stored_session_returns_409_without_token(self):
        # Simulate a session whose stored sidecar somehow has a hostile field
        # (e.g. legacy data from before validation was wired in, or a corrupted
        # write). The daemon must refuse to embed the auth token in a page
        # derived from this content.
        sid = self._submit_sample()
        rev_dir = Path(self.tmp) / "sessions" / sid / "revisions" / "1"
        sidecar = json.loads((rev_dir / "decisions.json").read_text())
        sidecar["nodes"][0]["status"] = '"><script>alert(1)</script>'
        (rev_dir / "decisions.json").write_text(json.dumps(sidecar))

        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + f"/sessions/{sid}").read()
        self.assertEqual(cm.exception.code, 409)
        body = cm.exception.read().decode("utf-8")
        # The error page MUST NOT carry the auth token or the submit-config.
        self.assertNotIn(self._token(), body)
        self.assertNotIn("submit-config", body)
        # Should explain what failed.
        self.assertIn("failed validation", body)

    def test_token_file_perms_0600(self):
        self._submit_sample()
        # Trigger ensure_token via a daemon hit; the daemon already created it
        # at startup, so just check perms.
        token_file = Path(self.tmp) / "token"
        self.assertTrue(token_file.exists())
        mode = token_file.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


class RiviewDaemonHostGateTests(unittest.TestCase):
    """Daemon-startup tests that don't share the long-lived fixture above."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="riview-daemon-host-")
        self.env = {**os.environ, "RIVIEW_HOME": self.tmp}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_non_loopback_host_requires_unsafe_flag(self):
        port = _free_port()
        r = subprocess.run(
            RIVIEW_CLI + ["daemon", "--host", "0.0.0.0", "--port", str(port)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("refusing to bind", r.stderr)
        self.assertIn("--unsafe-host", r.stderr)

    def test_token_perms_repaired_on_existing_loose_file(self):
        # Pre-create a token file with world-readable perms; ensure daemon
        # startup chmod's it back to 0600.
        token_file = Path(self.tmp) / "token"
        token_file.write_text("deadbeef\n")
        os.chmod(token_file, 0o644)
        port = _free_port()
        proc = subprocess.Popen(
            RIVIEW_CLI + ["daemon", "--port", str(port)],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_for(f"http://127.0.0.1:{port}/")
            mode = token_file.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)
            # And the existing token value was preserved, not regenerated.
            self.assertEqual(token_file.read_text().strip(), "deadbeef")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    unittest.main()
