"""Smoke tests for the riview HTTP daemon.

Run from the repo root: python3 -m unittest tests.test_daemon
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

REPO_ROOT = Path(__file__).resolve().parents[1]
RIVIEW_CLI = [sys.executable, str(REPO_ROOT / "scripts" / "riview.py")]
SAMPLE = REPO_ROOT / "sample"


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
        review_dict = json.loads((SAMPLE / "review-demo.json").read_bytes())
        # HTTP path requires base_revision; daemon strips it before persisting,
        # so the round-tripped payload still equals the source file.
        posted = dict(review_dict)
        posted["base_revision"] = 1
        req = urllib.request.Request(
            self.base + f"/sessions/{sid}/review",
            data=json.dumps(posted).encode("utf-8"),
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
        self.assertEqual(json.loads(pulled.stdout), review_dict)

    def test_review_post_rejects_spec_id_mismatch(self):
        sid = self._submit_sample()
        bad = json.loads((SAMPLE / "review-demo.json").read_bytes())
        bad["spec_id"] = "wrong-spec"
        bad["base_revision"] = 1
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
        # HTTP path requires base_revision. Tests that don't care about
        # staleness behavior can omit it; default to the current revision so
        # the post lands cleanly. Tests that do exercise stale-submit pass
        # base_revision (or omit_base_revision=True) explicitly in `review`.
        body = dict(review)
        if "base_revision" not in body and not body.pop("_omit_base_revision", False):
            body["base_revision"] = self._current_revision(sid)
        req = urllib.request.Request(
            self.base + f"/sessions/{sid}/review",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "X-Riview-Token": self._token(),
                "Content-Type": "application/json",
            },
        )
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read().decode("utf-8"))

    def _current_revision(self, sid: str) -> int:
        meta_path = Path(self.tmp) / "sessions" / sid / "meta.json"
        return int(json.loads(meta_path.read_text("utf-8"))["current_revision"])

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
                             "base_revision": 1,
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
            "base_revision": 1,
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
            "base_revision": 1,
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

    def _advance_revision(self, sid: str, mutate) -> int:
        """Submit a new revision of the sample spec to `sid`.

        `mutate(sidecar_dict, md_text) -> (new_sidecar_dict, new_md_text)`
        produces the next revision's content. The mutated spec must bump
        `version` itself — callers usually do that as part of the mutation.
        Returns the resulting `current_revision`.
        """
        tmp_proj = tempfile.mkdtemp(prefix="riview-advance-", dir=self.tmp)
        shutil.copy(SAMPLE / "spec.md", Path(tmp_proj) / "spec.md")
        shutil.copy(SAMPLE / "spec.decisions.json", Path(tmp_proj) / "spec.decisions.json")
        sidecar = json.loads((Path(tmp_proj) / "spec.decisions.json").read_text("utf-8"))
        md = (Path(tmp_proj) / "spec.md").read_text("utf-8")
        new_sidecar, new_md = mutate(sidecar, md)
        (Path(tmp_proj) / "spec.decisions.json").write_text(
            json.dumps(new_sidecar, indent=2) + "\n"
        )
        (Path(tmp_proj) / "spec.md").write_text(new_md)
        r = subprocess.run(
            RIVIEW_CLI + ["submit", tmp_proj, "--session", sid],
            env=self.env, capture_output=True, text=True, check=True,
        )
        return json.loads(r.stdout.strip().splitlines()[-1])["revision"]

    @staticmethod
    def _bump_status(sidecar: dict, md: str, node_id: str, new_status: str):
        s = json.loads(json.dumps(sidecar))  # deep copy
        for n in s["nodes"]:
            if n["id"] == node_id:
                n["status"] = new_status
        s["version"] = s.get("version", 1) + 1
        return s, md

    def test_stale_submit_accepts_unchanged_node_conflicts_changed_node(self):
        # base = rev 1; advance to rev 2 by changing deci-platform's status.
        # Reviewer's POST was composed against rev 1 and touches two nodes:
        #   - deci-platform: changed between rev 1 and rev 2 -> conflict
        #   - amb-sync: unchanged between rev 1 and rev 2 -> accepted
        # The persisted review file holds only the accepted entry, normalized
        # to the current spec_version.
        sid = self._submit_sample()
        new_rev = self._advance_revision(
            sid, lambda s, m: self._bump_status(s, m, "deci-platform", "confirmed")
        )
        self.assertEqual(new_rev, 2)
        payload = self._post_review(sid, {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,         # what reviewer saw
            "base_revision": 1,        # snapshot they reviewed
            "reviews": [
                {"node_id": "deci-platform", "new_status": "rejected", "comment": "no"},
                {"node_id": "amb-sync", "comment": "still open, just thinking"},
            ],
        })
        self.assertEqual(payload["base_revision"], 1)
        self.assertEqual(payload["current_revision"], 2)
        accepted_ids = sorted(e["node_id"] for e in payload["accepted"])
        conflict_ids = sorted(c["node_id"] for c in payload["conflicts"])
        self.assertEqual(accepted_ids, ["amb-sync"])
        self.assertEqual(conflict_ids, ["deci-platform"])
        # review_count is the cumulative count on disk after this POST.
        self.assertEqual(payload["review_count"], 1)
        # Persisted review file: normalized to current spec_version, accepted-only.
        merged = json.loads(subprocess.run(
            RIVIEW_CLI + ["pull", sid], env=self.env,
            capture_output=True, text=True, check=True,
        ).stdout)
        self.assertEqual(merged["spec_version"], 2)
        self.assertEqual([e["node_id"] for e in merged["reviews"]], ["amb-sync"])

    def test_stale_submit_with_base_equal_current_is_clean_accept(self):
        # base == current is a no-staleness submit; every entry accepts.
        sid = self._submit_sample()
        payload = self._post_review(sid, {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "base_revision": 1,
            "reviews": [
                {"node_id": "deci-platform", "new_status": "confirmed"},
                {"node_id": "amb-sync", "comment": "thinking"},
            ],
        })
        self.assertEqual(payload["base_revision"], 1)
        self.assertEqual(payload["current_revision"], 1)
        self.assertEqual(payload["conflicts"], [])
        self.assertEqual(sorted(e["node_id"] for e in payload["accepted"]),
                         ["amb-sync", "deci-platform"])
        self.assertEqual(payload["review_count"], 2)

    def test_stale_submit_missing_base_revision_400(self):
        # HTTP POSTs must include base_revision; CLI submit-review path can omit it.
        sid = self._submit_sample()
        req = urllib.request.Request(
            self.base + f"/sessions/{sid}/review",
            data=json.dumps({
                "spec_id": "pomodoro-mvp",
                "spec_version": 1,
                "reviews": [{"node_id": "deci-platform", "new_status": "confirmed"}],
            }).encode("utf-8"),
            method="POST",
            headers={"X-Riview-Token": self._token(),
                     "Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req).read()
        self.assertEqual(cm.exception.code, 400)

    def test_stale_submit_unknown_base_revision_400(self):
        sid = self._submit_sample()
        req = urllib.request.Request(
            self.base + f"/sessions/{sid}/review",
            data=json.dumps({
                "spec_id": "pomodoro-mvp",
                "spec_version": 1,
                "base_revision": 99,
                "reviews": [{"node_id": "deci-platform", "new_status": "confirmed"}],
            }).encode("utf-8"),
            method="POST",
            headers={"X-Riview-Token": self._token(),
                     "Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req).read()
        self.assertEqual(cm.exception.code, 400)

    def test_stale_submit_malformed_base_revision_400(self):
        sid = self._submit_sample()
        req = urllib.request.Request(
            self.base + f"/sessions/{sid}/review",
            data=json.dumps({
                "spec_id": "pomodoro-mvp",
                "spec_version": 1,
                "base_revision": "first",
                "reviews": [{"node_id": "deci-platform", "new_status": "confirmed"}],
            }).encode("utf-8"),
            method="POST",
            headers={"X-Riview-Token": self._token(),
                     "Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req).read()
        self.assertEqual(cm.exception.code, 400)

    def test_legacy_cli_submit_review_still_works_without_base_revision(self):
        # The CLI path must accept reviews lacking base_revision (today's shape).
        sid = self._submit_sample()
        review_path = Path(self.tmp) / "review.json"
        review_path.write_text(json.dumps({
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviews": [
                {"node_id": "deci-platform", "new_status": "confirmed", "comment": "cli"},
            ],
        }))
        r = subprocess.run(
            RIVIEW_CLI + ["submit-review", sid, str(review_path)],
            env=self.env, capture_output=True, text=True, check=True,
        )
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["status"], "review_submitted")
        self.assertEqual(out["review_count"], 1)
        merged = json.loads(subprocess.run(
            RIVIEW_CLI + ["pull", sid], env=self.env,
            capture_output=True, text=True, check=True,
        ).stdout)
        plat = next(e for e in merged["reviews"] if e["node_id"] == "deci-platform")
        self.assertEqual(plat["new_status"], "confirmed")

    def test_stale_submit_body_edit_detected_as_conflict(self):
        # Modify a node's body in rev 2; reviewer's rev-1 review on that node
        # must be reported as a conflict because the body fingerprint changed.
        def mutate(s, m):
            new_md = m.replace(
                "Ship to iOS using SwiftUI on iOS 17+.",
                "Ship to iOS using SwiftUI on iOS 17+. UPDATED BY RESPONDER.",
                1,
            )
            ns = json.loads(json.dumps(s))
            ns["version"] = ns.get("version", 1) + 1
            return ns, new_md

        sid = self._submit_sample()
        self.assertEqual(self._advance_revision(sid, mutate), 2)
        payload = self._post_review(sid, {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "base_revision": 1,
            "reviews": [
                {"node_id": "deci-platform", "comment": "looks right"},
            ],
        })
        self.assertEqual(payload["accepted"], [])
        self.assertEqual([c["node_id"] for c in payload["conflicts"]], ["deci-platform"])

    def test_session_page_includes_session_id_and_base_revision_in_submit_config(self):
        # Slice 1 plumbs session_id + base_revision into render's submit-config so
        # the JS can include base_revision on POSTs and key localStorage drafts.
        sid = self._submit_sample()
        body = urllib.request.urlopen(self.base + f"/sessions/{sid}").read().decode("utf-8")
        self.assertIn("submit-config", body)
        self.assertIn(f'"session_id": "{sid}"', body)
        self.assertIn('"base_revision": 1', body)

    def test_session_page_includes_draft_persistence_keys(self):
        # Slice 3: rendered JS must use the session-scoped localStorage key
        # `riview:draft:<sid>:<base_revision>` for daemon pages and the
        # standalone-scoped fallback when no session is set. Also the
        # rehydrate / prune helpers must be wired in.
        sid = self._submit_sample()
        body = urllib.request.urlopen(self.base + f"/sessions/{sid}").read().decode("utf-8")
        # Both key shapes are present in the helper definitions.
        self.assertIn('"riview:draft:"', body)
        self.assertIn('"riview:draft:standalone:"', body)
        # Rehydrate + save + prune wired.
        self.assertIn("rehydrateFromDraft", body)
        self.assertIn("saveDraft", body)
        self.assertIn("pruneOldDrafts", body)

    def test_session_page_supports_status_prefill(self):
        # Slice 2: dropdown options must include the initial statuses
        # (ai-confident for decision, open for ambiguity/risk) so the JS
        # can pre-select the currently-applied status when rendering each
        # card. The "— No change —" placeholder option is gone — prefill
        # replaces it.
        sid = self._submit_sample()
        body = urllib.request.urlopen(self.base + f"/sessions/{sid}").read().decode("utf-8")
        # STATUS_OPTIONS literal in the inline JS must list the initial
        # statuses as selectable values.
        self.assertIn('value: "ai-confident"', body)
        self.assertIn('value: "open"', body)
        # Placeholder option gone.
        self.assertNotIn("— No change —", body)
        # APPLIED_BY_ID wires per-node applied state for touched-vs-applied diff.
        self.assertIn("APPLIED_BY_ID", body)

    def test_token_file_perms_0600(self):
        self._submit_sample()
        # Trigger ensure_token via a daemon hit; the daemon already created it
        # at startup, so just check perms.
        token_file = Path(self.tmp) / "token"
        self.assertTrue(token_file.exists())
        mode = token_file.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


    def test_session_page_renders_overlay_status_and_comment(self):
        # Slice 1: after a review POST, reloading the session page must reflect
        # the submitted status + body_edit in the rendered SPEC, and the full
        # overlay entries must surface in the overlay-entries JSON island so
        # the textarea re-prefills and the client can compose full entries on
        # partial-field edits.
        sid = self._submit_sample()
        self._post_review(sid, {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviews": [
                {"node_id": "deci-platform", "new_status": "confirmed",
                 "body_edit": "REVIEWED-BODY-MARK", "comment": "Locked in."},
                {"node_id": "amb-sync", "new_status": "resolved",
                 "resolution": {"choice_id": "local"},
                 "comment": "Local-only for v1."},
            ],
        })
        body = urllib.request.urlopen(self.base + f"/sessions/{sid}").read().decode("utf-8")

        # Overlay-entries JSON island present and carries the full entries
        # (not just comments) so the client can preserve unchanged fields.
        marker = '<script type="application/json" id="overlay-entries">'
        self.assertIn(marker, body)
        start = body.index(marker) + len(marker)
        end = body.index("</script>", start)
        overlay = json.loads(body[start:end])
        self.assertEqual(overlay["deci-platform"]["comment"], "Locked in.")
        self.assertEqual(overlay["deci-platform"]["new_status"], "confirmed")
        self.assertEqual(overlay["deci-platform"]["body_edit"], "REVIEWED-BODY-MARK")
        self.assertEqual(overlay["amb-sync"]["comment"], "Local-only for v1.")
        self.assertEqual(overlay["amb-sync"]["resolution"], {"choice_id": "local"})

        # Spec payload reflects the merged status / resolution / body.
        spec_marker = '<script type="application/json" id="spec-payload">'
        s = body.index(spec_marker) + len(spec_marker)
        e = body.index("</script>", s)
        spec_payload = json.loads(body[s:e])
        nodes_by_id = {n["id"]: n for n in spec_payload["nodes"]}
        self.assertEqual(nodes_by_id["deci-platform"]["status"], "confirmed")
        self.assertIn("REVIEWED-BODY-MARK", nodes_by_id["deci-platform"]["_body_md"])
        self.assertEqual(nodes_by_id["amb-sync"]["status"], "resolved")
        self.assertEqual(
            nodes_by_id["amb-sync"]["resolution"].get("choice_id"), "local",
        )

    def test_session_page_with_no_overlay_renders_empty_overlay_comments(self):
        # Fresh session, no review.json on disk. Render must not error, and
        # the overlay-entries island must be the empty object.
        sid = self._submit_sample()
        body = urllib.request.urlopen(self.base + f"/sessions/{sid}").read().decode("utf-8")
        marker = '<script type="application/json" id="overlay-entries">'
        self.assertIn(marker, body)
        start = body.index(marker) + len(marker)
        end = body.index("</script>", start)
        self.assertEqual(json.loads(body[start:end]), {})
        # No overlay file should have been materialized just from rendering.
        overlay_path = Path(self.tmp) / "sessions" / sid / "reviews" / "1" / "review.json"
        self.assertFalse(overlay_path.exists())

    def test_partial_field_resubmit_preserves_other_overlay_fields(self):
        # Regression: the daemon's by-node-id replace-merge will drop any
        # field that's missing from a re-submitted entry. The renderer must
        # therefore emit FULL effective overlay entries, not sparse diffs —
        # and the daemon's overlay-entries island must carry every field
        # needed to recompose them. Simulate the client behavior here by
        # POSTing the composed full entry; assert that status / resolution /
        # body_edit survive a comment-only edit.
        sid = self._submit_sample()
        # 1) Initial submit: full state for one node.
        self._post_review(sid, {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviews": [
                {"node_id": "deci-platform", "new_status": "confirmed",
                 "body_edit": "BODY-V1", "comment": "Locked in."},
            ],
        })
        # 2) Reload and read the overlay-entries island as the client would.
        body = urllib.request.urlopen(self.base + f"/sessions/{sid}").read().decode("utf-8")
        marker = '<script type="application/json" id="overlay-entries">'
        s = body.index(marker) + len(marker)
        e = body.index("</script>", s)
        overlay = json.loads(body[s:e])
        existing = overlay["deci-platform"]
        # 3) Compose a full entry mirroring buildEntryForNode's behavior:
        # only the comment changed, but the client preserves new_status /
        # body_edit from the existing overlay so the merge does not drop them.
        composed = dict(existing)
        composed["comment"] = "Actually v2"
        self._post_review(sid, {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviews": [composed],
        })
        # 4) On the next render, the merged spec must STILL reflect the
        # original status + body_edit. Without the full-entry contract, the
        # replace-merge would have stored only {comment: ...} and the status
        # would have reverted to canonical here.
        body2 = urllib.request.urlopen(self.base + f"/sessions/{sid}").read().decode("utf-8")
        sm = '<script type="application/json" id="spec-payload">'
        s2 = body2.index(sm) + len(sm)
        e2 = body2.index("</script>", s2)
        spec_payload = json.loads(body2[s2:e2])
        nodes_by_id = {n["id"]: n for n in spec_payload["nodes"]}
        self.assertEqual(nodes_by_id["deci-platform"]["status"], "confirmed")
        self.assertIn("BODY-V1", nodes_by_id["deci-platform"]["_body_md"])
        # Overlay-entries island carries the new comment plus all preserved fields.
        o2_start = body2.index(marker) + len(marker)
        o2_end = body2.index("</script>", o2_start)
        overlay2 = json.loads(body2[o2_start:o2_end])
        self.assertEqual(overlay2["deci-platform"]["comment"], "Actually v2")
        self.assertEqual(overlay2["deci-platform"]["new_status"], "confirmed")
        self.assertEqual(overlay2["deci-platform"]["body_edit"], "BODY-V1")

    def test_clear_comment_only_overlay_evicts_node_via_cleared_fields(self):
        # Regression: with the daemon's empty-entry filter, a comment-only
        # overlay entry could not be cleared by sending {node_id} alone —
        # the filter would drop the submit and the stale comment would
        # survive on reload. The client's compose path emits an explicit
        # `cleared_fields: ["comment"]` marker in that case; the daemon's
        # overlay merge applies it as a field-level diff and evicts the
        # node when only `node_id` remains.
        sid = self._submit_sample()
        # 1) Submit a comment-only overlay entry.
        self._post_review(sid, {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviews": [
                {"node_id": "deci-platform", "comment": "draft note"},
            ],
        })
        body = urllib.request.urlopen(self.base + f"/sessions/{sid}").read().decode("utf-8")
        marker = '<script type="application/json" id="overlay-entries">'
        s = body.index(marker) + len(marker)
        e = body.index("</script>", s)
        overlay = json.loads(body[s:e])
        self.assertEqual(overlay["deci-platform"]["comment"], "draft note")
        # 2) Submit the cleared_fields marker the client would emit.
        self._post_review(sid, {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviews": [
                {"node_id": "deci-platform", "cleared_fields": ["comment"]},
            ],
        })
        # 3) Overlay is gone for this node, and the stored review.json no
        # longer carries an entry for it.
        body2 = urllib.request.urlopen(self.base + f"/sessions/{sid}").read().decode("utf-8")
        s2 = body2.index(marker) + len(marker)
        e2 = body2.index("</script>", s2)
        overlay2 = json.loads(body2[s2:e2])
        self.assertNotIn("deci-platform", overlay2)
        stored = json.loads((Path(self.tmp) / "sessions" / sid / "reviews" / "1" / "review.json").read_text("utf-8"))
        ids = [r["node_id"] for r in stored.get("reviews") or []]
        self.assertNotIn("deci-platform", ids)

    def test_cleared_fields_with_other_fields_drops_only_listed(self):
        # When the overlay had multiple fields and only one is cleared, the
        # remaining fields stay. (This path is exercised by the client when
        # the composed entry still has meaningful content — `cleared_fields`
        # in that case lets the server preserve the rest.)
        sid = self._submit_sample()
        self._post_review(sid, {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviews": [
                {"node_id": "deci-platform", "new_status": "confirmed",
                 "comment": "Locked in."},
            ],
        })
        # Drop the comment field via the marker; keep new_status implicitly.
        self._post_review(sid, {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviews": [
                {"node_id": "deci-platform", "cleared_fields": ["comment"]},
            ],
        })
        stored = json.loads((Path(self.tmp) / "sessions" / sid / "reviews" / "1" / "review.json").read_text("utf-8"))
        plat = next(r for r in stored["reviews"] if r["node_id"] == "deci-platform")
        self.assertEqual(plat.get("new_status"), "confirmed")
        self.assertNotIn("comment", plat)
        self.assertNotIn("cleared_fields", plat)

    def test_cleared_fields_rejects_malformed_values(self):
        # Hardening: cleared_fields is a write semantic, so the POST path
        # rejects shapes other than `list[str]` drawn from the known overlay
        # fields. Keeps accidental API callers from turning a typo into an
        # unexpected overlay deletion.
        sid = self._submit_sample()
        for bad in (
            "comment",                       # not a list
            ["comment", 42],                 # non-string element
            ["unknown_field"],               # not in the allowed set
            True,                            # not a list
        ):
            body = {
                "spec_id": "pomodoro-mvp",
                "spec_version": 1,
                "base_revision": 1,
                "reviews": [
                    {"node_id": "deci-platform", "cleared_fields": bad},
                ],
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

    def test_session_page_emits_canonical_by_id_island(self):
        # ADR-0011 amendment: the renderer exposes canonical (pre-overlay)
        # node status + ambiguity resolution as a JSON island parallel to
        # `overlay-entries`. The client uses it to detect snap-back-to-
        # canonical and route the diff into `cleared_fields` so the
        # daemon's overlay merge evicts the node instead of keeping a
        # no-op entry. STATUS_BY_ID on the page is post-merge so it
        # cannot serve this role.
        sid = self._submit_sample()
        canonical_marker = '<script type="application/json" id="canonical-by-id">'
        overlay_marker = '<script type="application/json" id="overlay-entries">'

        # (a) Fresh session, no overlay: island carries canonical statuses
        # for every node (and resolution=null for the ambiguity nodes).
        body = urllib.request.urlopen(self.base + f"/sessions/{sid}").read().decode("utf-8")
        self.assertIn(canonical_marker, body)
        s = body.index(canonical_marker) + len(canonical_marker)
        e = body.index("</script>", s)
        canon = json.loads(body[s:e])
        self.assertEqual(canon["amb-streaks"]["status"], "open")
        self.assertIsNone(canon["amb-streaks"]["resolution"])
        self.assertEqual(canon["deci-platform"]["status"], "ai-confident")
        self.assertNotIn("resolution", canon["deci-platform"])  # non-ambiguity
        self.assertEqual(canon["risk-bg"]["status"], "open")
        self.assertNotIn("resolution", canon["risk-bg"])

        # (b) POST a review that flips the ambiguity to resolved. After
        # reload, canonical-by-id MUST still carry "open" for that node
        # — this is the bug the fix targets. (overlay-entries correctly
        # reflects the merged value, but canonical should not leak.)
        self._post_review(sid, {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviews": [
                {"node_id": "amb-streaks", "new_status": "resolved",
                 "resolution": {"choice_id": "first-class"},
                 "comment": "A5"},
            ],
        })
        body2 = urllib.request.urlopen(self.base + f"/sessions/{sid}").read().decode("utf-8")
        s = body2.index(canonical_marker) + len(canonical_marker)
        e = body2.index("</script>", s)
        canon2 = json.loads(body2[s:e])
        self.assertEqual(canon2["amb-streaks"]["status"], "open",
                         "canonical-by-id must show canonical, not overlay-merged status")
        self.assertIsNone(canon2["amb-streaks"]["resolution"],
                          "canonical resolution for an unresolved ambiguity is null")
        # Sanity: overlay-entries DOES reflect the overlay (separation of concerns).
        s = body2.index(overlay_marker) + len(overlay_marker)
        e = body2.index("</script>", s)
        overlay = json.loads(body2[s:e])
        self.assertEqual(overlay["amb-streaks"]["new_status"], "resolved")

    def test_standalone_render_emits_empty_canonical_by_id_island(self):
        # Standalone (non-daemon) render via scripts/render.py main(): no
        # overlay merge ever happens, so canonical-by-id is the empty
        # object. The client falls back to `n.status` (which IS canonical
        # in standalone mode) when the island is empty.
        with tempfile.TemporaryDirectory(prefix="riview-standalone-") as td:
            spec_dir = Path(td)
            shutil.copy(SAMPLE / "spec.md", spec_dir / "spec.md")
            shutil.copy(SAMPLE / "spec.decisions.json", spec_dir / "spec.decisions.json")
            subprocess.run(
                [sys.executable, str(REPO_ROOT / "scripts" / "render.py"), str(spec_dir)],
                capture_output=True, text=True, check=True,
            )
            html = (spec_dir / "spec.html").read_text("utf-8")
        marker = '<script type="application/json" id="canonical-by-id">'
        self.assertIn(marker, html)
        s = html.index(marker) + len(marker)
        e = html.index("</script>", s)
        self.assertEqual(json.loads(html[s:e]), {})

    def test_status_snap_back_via_cleared_fields_evicts_node(self):
        # Realistic shape the client will now POST when the reviewer snaps
        # a status back to canonical on a node whose only overlay field
        # was `new_status`. With cleared_fields the server runs the field-
        # merge path; the resulting entry has only node_id left, so the
        # node is evicted from the stored overlay.
        sid = self._submit_sample()
        self._post_review(sid, {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviews": [
                {"node_id": "amb-streaks", "new_status": "resolved",
                 "resolution": {"choice_id": "first-class"}},
            ],
        })
        # Snap-back: status to canonical, drop the now-stale resolution too.
        # Comment was never set, so it isn't in cleared_fields.
        self._post_review(sid, {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviews": [
                {"node_id": "amb-streaks",
                 "cleared_fields": ["new_status", "resolution"]},
            ],
        })
        stored = json.loads((Path(self.tmp) / "sessions" / sid / "reviews" / "1" / "review.json").read_text("utf-8"))
        ids = [r["node_id"] for r in stored.get("reviews") or []]
        self.assertNotIn("amb-streaks", ids,
                         "snap-back-to-canonical must evict the overlay entry")

    def test_cleared_fields_with_field_merge_preserves_unlisted_fields(self):
        # When cleared_fields is present the server switches from full-
        # replace to field-merge (ADR-0011). Any prior overlay field NOT
        # listed in cleared_fields is PRESERVED — the client must account
        # for that and include every field it intentionally drops, not
        # just the snap-back one. This test pins the contract.
        sid = self._submit_sample()
        self._post_review(sid, {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviews": [
                {"node_id": "deci-platform", "new_status": "confirmed",
                 "body_edit": "BODY-KEEP", "comment": "draft"},
            ],
        })
        # Drop new_status only. comment + body_edit must survive.
        self._post_review(sid, {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviews": [
                {"node_id": "deci-platform", "cleared_fields": ["new_status"]},
            ],
        })
        stored = json.loads((Path(self.tmp) / "sessions" / sid / "reviews" / "1" / "review.json").read_text("utf-8"))
        plat = next(r for r in stored["reviews"] if r["node_id"] == "deci-platform")
        self.assertNotIn("new_status", plat)
        self.assertEqual(plat.get("body_edit"), "BODY-KEEP")
        self.assertEqual(plat.get("comment"), "draft")

    def test_overlay_does_not_persist_into_node_review_comment(self):
        # ADR-0011: overlay comments are a separate baseline. They must NOT
        # be written into node.review.comment on the stored revision sidecar,
        # otherwise stale comments would survive into the applied spec via
        # the responder's read path.
        sid = self._submit_sample()
        self._post_review(sid, {
            "spec_id": "pomodoro-mvp",
            "spec_version": 1,
            "reviews": [
                {"node_id": "deci-platform", "comment": "draft note"},
            ],
        })
        urllib.request.urlopen(self.base + f"/sessions/{sid}").read()
        sidecar_path = Path(self.tmp) / "sessions" / sid / "revisions" / "1" / "decisions.json"
        sidecar = json.loads(sidecar_path.read_text("utf-8"))
        plat = next(n for n in sidecar["nodes"] if n["id"] == "deci-platform")
        # Either no review block, or review.comment is not the overlay text.
        review_block = plat.get("review") or {}
        self.assertNotEqual(review_block.get("comment"), "draft note")


class RiviewStorageRootTests(unittest.TestCase):
    """Slice 0: storage root precedence — repo-local default, env override."""

    def test_default_is_repo_local(self):
        # Importing the module fresh with no RIVIEW_HOME set should resolve
        # the storage root to <riview_repo>/.riview/ (next to scripts/).
        env = {k: v for k, v in os.environ.items() if k != "RIVIEW_HOME"}
        r = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, 'scripts'); "
             "import riview; print(riview.riview_home())"],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=True,
        )
        expected = (REPO_ROOT / ".riview").resolve()
        self.assertEqual(Path(r.stdout.strip()).resolve(), expected)

    def test_env_override_wins(self):
        env = {**os.environ, "RIVIEW_HOME": "/tmp/riview-some-override"}
        r = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, 'scripts'); "
             "import riview; print(riview.riview_home())"],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=True,
        )
        self.assertEqual(r.stdout.strip(), "/tmp/riview-some-override")


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
