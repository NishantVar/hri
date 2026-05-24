"""Smoke tests for riview/scripts/riview.py session model.

Run from the repo root: python3 -m unittest riview.tests.test_session
Or directly:           python3 riview/tests/test_session.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RIVIEW_CLI = [sys.executable, str(REPO_ROOT / "riview" / "scripts" / "riview.py")]
SAMPLE = REPO_ROOT / "riview" / "sample"


class RiviewSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="riview-test-")
        self.env = {**os.environ, "RIVIEW_HOME": self.tmp}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args, expect=0):
        result = subprocess.run(
            RIVIEW_CLI + list(args),
            env=self.env,
            capture_output=True,
            text=True,
        )
        if result.returncode != expect:
            self.fail(
                f"riview {' '.join(args)} → exit {result.returncode} (expected {expect})"
                f"\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def _last_json_line(self, stdout: str) -> dict:
        return json.loads(stdout.strip().splitlines()[-1])

    def test_submit_creates_session_rev1(self):
        r = self.run_cli("submit", str(SAMPLE))
        out = self._last_json_line(r.stdout)
        self.assertEqual(out["revision"], 1)
        self.assertEqual(out["status"], "awaiting_review")
        sid = out["session_id"]
        root = Path(self.tmp) / "sessions" / sid
        self.assertTrue((root / "meta.json").exists())
        self.assertTrue((root / "revisions" / "1" / "source.md").exists())
        self.assertTrue((root / "revisions" / "1" / "decisions.json").exists())
        self.assertTrue((root / "revisions" / "1" / "submitted_at").exists())

    def test_idempotent_resubmit(self):
        sid = self._last_json_line(self.run_cli("submit", str(SAMPLE)).stdout)["session_id"]
        r2 = self.run_cli("submit", str(SAMPLE), "--session", sid)
        out2 = self._last_json_line(r2.stdout)
        self.assertEqual(out2["revision"], 1)
        self.assertTrue(out2.get("idempotent"))
        # filesystem confirms only one revision
        self.assertEqual(
            sorted(p.name for p in (Path(self.tmp) / "sessions" / sid / "revisions").iterdir()),
            ["1"],
        )

    def test_changed_content_advances_revision(self):
        with tempfile.TemporaryDirectory() as workspace:
            wd = Path(workspace)
            (wd / "spec.md").write_bytes((SAMPLE / "spec.md").read_bytes())
            (wd / "spec.decisions.json").write_bytes(
                (SAMPLE / "spec.decisions.json").read_bytes()
            )
            sid = self._last_json_line(self.run_cli("submit", str(wd)).stdout)["session_id"]
            (wd / "spec.md").write_text(
                (wd / "spec.md").read_text() + "\n\n## extra section\n\nedited.\n"
            )
            r2 = self.run_cli("submit", str(wd), "--session", sid)
            out2 = self._last_json_line(r2.stdout)
            self.assertEqual(out2["revision"], 2)
            self.assertEqual(out2["status"], "awaiting_review")
            revisions = Path(self.tmp) / "sessions" / sid / "revisions"
            self.assertEqual(sorted(p.name for p in revisions.iterdir()), ["1", "2"])

    def test_review_lifecycle(self):
        sid = self._last_json_line(self.run_cli("submit", str(SAMPLE)).stdout)["session_id"]
        review_src = SAMPLE / "review-demo.json"
        r2 = self.run_cli("submit-review", sid, str(review_src))
        out2 = self._last_json_line(r2.stdout)
        self.assertEqual(out2["status"], "review_submitted")
        # pull returns the review verbatim
        pulled = json.loads(self.run_cli("pull", sid).stdout)
        original = json.loads(review_src.read_bytes())
        self.assertEqual(pulled, original)
        # pull is idempotent (doesn't consume)
        pulled_again = json.loads(self.run_cli("pull", sid).stdout)
        self.assertEqual(pulled_again, original)

    def test_pull_no_review(self):
        sid = self._last_json_line(self.run_cli("submit", str(SAMPLE)).stdout)["session_id"]
        self.run_cli("pull", sid, expect=4)

    def test_applied_and_dismiss(self):
        sid = self._last_json_line(self.run_cli("submit", str(SAMPLE)).stdout)["session_id"]
        self.run_cli("submit-review", sid, str(SAMPLE / "review-demo.json"))
        self.run_cli("applied", sid)
        self.assertEqual(
            json.loads(self.run_cli("status", sid).stdout)["status"], "applied"
        )
        self.run_cli("dismiss", sid)
        self.assertEqual(
            json.loads(self.run_cli("status", sid).stdout)["status"], "closed"
        )
        # list excludes closed by default
        self.assertEqual(json.loads(self.run_cli("list").stdout), [])
        # but --all surfaces it
        all_rows = json.loads(self.run_cli("list", "--all").stdout)
        self.assertEqual(len(all_rows), 1)
        self.assertEqual(all_rows[0]["session_id"], sid)

    def test_advance_revision_after_review(self):
        with tempfile.TemporaryDirectory() as workspace:
            wd = Path(workspace)
            (wd / "spec.md").write_bytes((SAMPLE / "spec.md").read_bytes())
            (wd / "spec.decisions.json").write_bytes(
                (SAMPLE / "spec.decisions.json").read_bytes()
            )
            sid = self._last_json_line(self.run_cli("submit", str(wd)).stdout)["session_id"]
            self.run_cli("submit-review", sid, str(SAMPLE / "review-demo.json"))
            (wd / "spec.md").write_text((wd / "spec.md").read_text() + "\n\nedit.\n")
            r2 = self.run_cli("submit", str(wd), "--session", sid)
            out2 = self._last_json_line(r2.stdout)
            self.assertEqual(out2["revision"], 2)
            self.assertEqual(out2["status"], "awaiting_review")
            # review was for rev 1; pull on rev 2 returns 4
            self.run_cli("pull", sid, expect=4)

    def test_session_not_found(self):
        self.run_cli("status", "deadbeefdead", expect=3)
        self.run_cli("pull", "deadbeefdead", expect=3)
        self.run_cli("dismiss", "deadbeefdead", expect=3)
        self.run_cli("applied", "deadbeefdead", expect=3)
        self.run_cli("open", "deadbeefdead", expect=3)
        self.run_cli(
            "submit-review", "deadbeefdead", str(SAMPLE / "review-demo.json"), expect=3
        )

    def test_submit_missing_files(self):
        with tempfile.TemporaryDirectory() as workspace:
            self.run_cli("submit", workspace, expect=2)

    def test_open_url_format(self):
        sid = self._last_json_line(self.run_cli("submit", str(SAMPLE)).stdout)["session_id"]
        url = self.run_cli("open", sid).stdout.strip()
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.assertTrue(url.endswith(f"/sessions/{sid}"))

    def test_malformed_session_id(self):
        # Includes traversal attempts, wrong length, wrong charset, empty,
        # whitespace. All must return exit 2 (bad input) — not 3.
        bad_ids = ["../foo", "deadbeef", "DEADBEEFDEAD", "g" * 12, "abc", ""]
        for bad in bad_ids:
            for sub in ("status", "pull", "dismiss", "applied", "open"):
                self.run_cli(sub, bad, expect=2)
            self.run_cli(
                "submit-review", bad, str(SAMPLE / "review-demo.json"), expect=2
            )
            self.run_cli("submit", str(SAMPLE), "--session", bad, expect=2)

    def test_submit_session_rejects_basename_mismatch(self):
        # Create a session with basename "spec" from the sample, then try to
        # advance it from design/mvp (different basename) — must exit 2.
        sid = self._last_json_line(self.run_cli("submit", str(SAMPLE)).stdout)["session_id"]
        design = REPO_ROOT / "design"
        if not (design / "mvp.md").exists():
            self.skipTest("design/mvp.md fixture not present")
        self.run_cli(
            "submit", str(design), "--basename", "mvp", "--session", sid, expect=2
        )

    def test_submit_session_rejects_spec_id_mismatch(self):
        # Same basename but a different spec_id in the sidecar → exit 2.
        sid = self._last_json_line(self.run_cli("submit", str(SAMPLE)).stdout)["session_id"]
        with tempfile.TemporaryDirectory() as workspace:
            wd = Path(workspace)
            (wd / "spec.md").write_bytes((SAMPLE / "spec.md").read_bytes())
            sidecar = json.loads((SAMPLE / "spec.decisions.json").read_text())
            sidecar["spec_id"] = "different-spec"
            (wd / "spec.decisions.json").write_text(json.dumps(sidecar))
            self.run_cli("submit", str(wd), "--session", sid, expect=2)

    def test_submit_review_rejects_spec_id_mismatch(self):
        sid = self._last_json_line(self.run_cli("submit", str(SAMPLE)).stdout)["session_id"]
        with tempfile.TemporaryDirectory() as workspace:
            bad_review = Path(workspace) / "review.json"
            review = json.loads((SAMPLE / "review-demo.json").read_text())
            review["spec_id"] = "not-the-pomodoro-spec"
            bad_review.write_text(json.dumps(review))
            self.run_cli("submit-review", sid, str(bad_review), expect=2)

    def test_submit_review_rejects_spec_version_mismatch(self):
        sid = self._last_json_line(self.run_cli("submit", str(SAMPLE)).stdout)["session_id"]
        with tempfile.TemporaryDirectory() as workspace:
            bad_review = Path(workspace) / "review.json"
            review = json.loads((SAMPLE / "review-demo.json").read_text())
            review["spec_version"] = review.get("spec_version", 1) + 99
            bad_review.write_text(json.dumps(review))
            self.run_cli("submit-review", sid, str(bad_review), expect=2)

    def test_traversal_does_not_escape_storage_root(self):
        # Even though malformed IDs short-circuit, sanity-check that no file
        # outside RIVIEW_HOME got created.
        before = set(Path(self.tmp).rglob("*"))
        self.run_cli("status", "../escape", expect=2)
        self.run_cli("status", "../../etc/passwd", expect=2)
        after = set(Path(self.tmp).rglob("*"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
