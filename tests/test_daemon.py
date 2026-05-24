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
import threading
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

    def _post_review(self, sid: str, review: dict) -> dict:
        req = urllib.request.Request(
            self.base + f"/sessions/{sid}/review",
            data=json.dumps(review).encode("utf-8"),
            method="POST",
            headers={
                "X-Riview-Token": self._token(),
                "Content-Type": "application/json",
            },
        )
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read().decode("utf-8"))

    def test_review_post_merges_by_node_id(self):
        # Two POSTs land on the same revision. Later entries upsert older ones
        # by node_id; sort-by-node_id keeps the on-disk blob deterministic.
        sid = self._submit_sample()
        first = {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviews": [
                {"node_id": "deci-platform", "new_status": "ai-confident"},
                {"node_id": "risk-bg", "comment": "first pass"},
            ],
        }
        second = {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviewer": "humans",
            "reviews": [
                {"node_id": "deci-platform", "new_status": "confirmed",
                 "comment": "settled"},  # upsert
                {"node_id": "deci-notify", "comment": "added later"},  # new
            ],
        }
        p1 = self._post_review(sid, first)
        p2 = self._post_review(sid, second)
        self.assertEqual(p2["review_count"], 3)
        self.assertGreater(p2["event_seq"], p1["event_seq"])
        merged = json.loads(
            subprocess.run(
                RIVIEW_CLI + ["pull", sid],
                env=self.env,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
        ids = [e["node_id"] for e in merged["reviews"]]
        self.assertEqual(ids, ["deci-notify", "deci-platform", "risk-bg"])
        plat = next(e for e in merged["reviews"] if e["node_id"] == "deci-platform")
        self.assertEqual(plat["new_status"], "confirmed")
        self.assertEqual(plat["comment"], "settled")
        # Latest top-level fields win.
        self.assertEqual(merged.get("reviewer"), "humans")

    def test_review_post_rejects_non_list_reviews(self):
        sid = self._submit_sample()
        req = urllib.request.Request(
            self.base + f"/sessions/{sid}/review",
            data=json.dumps({"spec_id": "pomodoro-mvp", "spec_version": 1,
                             "reviews": "nope"}).encode("utf-8"),
            method="POST",
            headers={"X-Riview-Token": self._token(),
                     "Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req).read()
        self.assertEqual(cm.exception.code, 400)

    def test_review_post_rejects_entry_without_node_id(self):
        sid = self._submit_sample()
        body = {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviews": [{"comment": "no id"}],
        }
        req = urllib.request.Request(
            self.base + f"/sessions/{sid}/review",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"X-Riview-Token": self._token(),
                     "Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req).read()
        self.assertEqual(cm.exception.code, 400)

    def test_review_post_drops_empty_entries_silently(self):
        # An entry with only node_id (no status/comment/resolution/body_edit)
        # must NOT clobber a prior real review for the same node on merge.
        sid = self._submit_sample()
        first = self._post_review(sid, {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviews": [
                {"node_id": "deci-platform", "new_status": "confirmed", "comment": "ship it"},
            ],
        })
        self.assertEqual(first["review_count"], 1)
        # Empty entry for the same node — should be filtered, prior entry preserved.
        second = self._post_review(sid, {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviews": [{"node_id": "deci-platform"}],
        })
        self.assertEqual(second["review_count"], 1)
        merged = json.loads(
            subprocess.run(
                RIVIEW_CLI + ["pull", sid],
                env=self.env, capture_output=True, text=True, check=True,
            ).stdout
        )
        plat = next(e for e in merged["reviews"] if e["node_id"] == "deci-platform")
        self.assertEqual(plat.get("new_status"), "confirmed")
        self.assertEqual(plat.get("comment"), "ship it")

    def test_review_post_rejects_unknown_node_id(self):
        # apply.py would reject the whole delta later; reject at POST time so
        # the on-disk review.json stays clean.
        sid = self._submit_sample()
        body = {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviews": [{"node_id": "not-a-real-node", "comment": "x"}],
        }
        req = urllib.request.Request(
            self.base + f"/sessions/{sid}/review",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"X-Riview-Token": self._token(),
                     "Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req).read()
        self.assertEqual(cm.exception.code, 409)

    def test_wait_returns_immediately_when_behind(self):
        sid = self._submit_sample()  # submit bumps event_seq to 1
        body = urllib.request.urlopen(
            self.base + f"/sessions/{sid}/wait?since=0&timeout=2"
        ).read().decode("utf-8")
        payload = json.loads(body)
        self.assertEqual(payload["session_id"], sid)
        self.assertGreaterEqual(payload["event_seq"], 1)
        self.assertEqual(payload["revision"], 1)
        self.assertEqual(payload["status"], "awaiting_review")

    def test_wait_times_out_with_204(self):
        sid = self._submit_sample()
        snap = json.loads(
            urllib.request.urlopen(
                self.base + f"/sessions/{sid}/wait?since=0&timeout=1"
            ).read().decode("utf-8")
        )
        # Now wait past the latest event_seq with a short timeout — no new
        # events arrive, so the server should return 204.
        resp = urllib.request.urlopen(
            self.base + f"/sessions/{sid}/wait?since={snap['event_seq']}&timeout=1"
        )
        self.assertEqual(resp.status, 204)
        self.assertEqual(resp.read(), b"")

    def test_wait_wakes_on_review_post(self):
        sid = self._submit_sample()
        snap0 = json.loads(
            urllib.request.urlopen(
                self.base + f"/sessions/{sid}/wait?since=0&timeout=1"
            ).read().decode("utf-8")
        )
        result: dict = {}

        def long_poll():
            try:
                body = urllib.request.urlopen(
                    self.base + f"/sessions/{sid}/wait?since={snap0['event_seq']}&timeout=5"
                ).read().decode("utf-8")
                result["payload"] = json.loads(body)
            except Exception as e:  # noqa: BLE001
                result["err"] = repr(e)

        t = threading.Thread(target=long_poll)
        t.start()
        # Give the daemon a beat to enter cond.wait.
        time.sleep(0.2)
        self._post_review(sid, {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviews": [{"node_id": "deci-platform", "comment": "ping"}],
        })
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "long-poll never returned")
        self.assertNotIn("err", result, result.get("err", ""))
        self.assertGreater(result["payload"]["event_seq"], snap0["event_seq"])
        self.assertEqual(result["payload"]["status"], "review_submitted")

    def test_events_emits_default_message(self):
        # EventSource.onmessage only fires for unnamed/default events. If the
        # server emits `event: <name>` lines, the browser banner stops working.
        # Lock the frame format down so a future refactor can't regress.
        sid = self._submit_sample()
        body_frame: dict = {}

        def reader():
            try:
                with urllib.request.urlopen(self.base + f"/sessions/{sid}/events") as r:
                    buf = b""
                    deadline = time.monotonic() + 5.0
                    while b"\n\n" not in buf and time.monotonic() < deadline:
                        # read1 returns whatever the underlying socket has,
                        # so we don't block forever waiting for a full buffer
                        # of bytes (subsequent SSE frames are 15s apart).
                        chunk = r.read1(1024)
                        if not chunk:
                            break
                        buf += chunk
                    body_frame["raw"] = buf.decode("utf-8")
            except Exception as e:  # noqa: BLE001
                body_frame["err"] = repr(e)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(timeout=5)
        self.assertNotIn("err", body_frame, body_frame.get("err", ""))
        raw = body_frame.get("raw", "")
        # First frame should be a default-event `data: ...` block, NOT a named
        # event. The handler emits the snapshot frame as soon as it opens.
        self.assertIn("data:", raw)
        self.assertNotIn("event:", raw)

    def test_wait_404_on_missing_session(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(
                self.base + "/sessions/deadbeefdead/wait?since=0&timeout=1"
            ).read()
        self.assertEqual(cm.exception.code, 404)

    def test_cli_wait_unblocks_on_review_post(self):
        sid = self._submit_sample()
        # Launch `riview wait` in the background; it'll long-poll until our POST.
        proc = subprocess.Popen(
            RIVIEW_CLI + ["wait", sid, "--timeout", "10", "--url", self.base],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.3)
        self._post_review(sid, {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviews": [{"node_id": "deci-platform", "comment": "cli ping"}],
        })
        stdout, stderr = proc.communicate(timeout=8)
        self.assertEqual(proc.returncode, 0, stderr.decode("utf-8", "replace"))
        payload = json.loads(stdout.decode("utf-8").splitlines()[-1])
        self.assertEqual(payload["session_id"], sid)
        self.assertEqual(payload["status"], "review_submitted")

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
