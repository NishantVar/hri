"""Tests for render.validate() — input hardening on sidecar fields.

The daemon embeds a write token in the rendered page (so any reviewer
JS can POST). That promotes the renderer's previously-trusted sidecar
fields (kind / status / severity / node id) into security boundaries:
a hostile sidecar with HTML in those fields could otherwise inject
script and exfil the token.

Run: python3 -m unittest riview.tests.test_render_validate
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import render  # noqa: E402


def _spec(**node_overrides):
    """Build a single-node spec; caller overrides node fields."""
    node = {
        "id": "n1",
        "kind": "decision",
        "title": "T",
        "status": "ai-confident",
        "confidence": "high",
        "depends_on": [],
        "source_anchor": "n1",
        "rationale": "",
        "alternatives": [],
    }
    node.update(node_overrides)
    return {
        "spec_id": "s",
        "spec_title": "S",
        "source_path": "spec.md",
        "version": 1,
        "nodes": [node],
    }, {node["source_anchor"]: 1}


class ValidateHardeningTests(unittest.TestCase):
    def test_clean_decision_validates(self):
        spec, anchors = _spec()
        self.assertEqual(render.validate(spec, anchors), [])

    def test_rejects_hostile_id_with_angle_brackets(self):
        spec, anchors = _spec(id="<script>alert(1)</script>", source_anchor="n1")
        errs = render.validate(spec, anchors)
        self.assertTrue(any("node id" in e for e in errs), errs)

    def test_rejects_id_with_quote(self):
        spec, anchors = _spec(id='n"x', source_anchor="n1")
        errs = render.validate(spec, anchors)
        self.assertTrue(any("node id" in e for e in errs), errs)

    def test_rejects_unknown_status_for_kind(self):
        spec, anchors = _spec(status="not-a-real-status")
        errs = render.validate(spec, anchors)
        self.assertTrue(any("status" in e for e in errs), errs)

    def test_rejects_status_with_html(self):
        spec, anchors = _spec(status='" onerror="alert(1)')
        errs = render.validate(spec, anchors)
        self.assertTrue(any("status" in e for e in errs), errs)

    def test_rejects_risk_with_bad_severity(self):
        spec, anchors = _spec(
            id="r1", source_anchor="r1", kind="risk", status="open",
            severity='<img src=x onerror=alert(1)>', mitigation="x",
        )
        anchors = {"r1": 1}
        errs = render.validate(spec, anchors)
        self.assertTrue(any("severity" in e for e in errs), errs)

    def test_rejects_ambiguity_with_hostile_option_id(self):
        spec, anchors = _spec(
            id="amb1", source_anchor="amb1", kind="ambiguity", status="open",
            prompt="?", options=[{"id": "ok"}, {"id": '"><script>x</script>'}],
        )
        anchors = {"amb1": 1}
        errs = render.validate(spec, anchors)
        self.assertTrue(any("option id" in e for e in errs), errs)

    def test_rejects_hostile_source_anchor(self):
        spec, _ = _spec(source_anchor="<x>")
        # anchor_counts contributes a separate "anchor missing" error; we just
        # want at least one "must match" error for the hostile pattern.
        errs = render.validate(spec, {"<x>": 1})
        self.assertTrue(any("source_anchor" in e for e in errs), errs)

    def test_rejects_depends_on_unknown_id(self):
        spec, anchors = _spec(depends_on=["does-not-exist"])
        errs = render.validate(spec, anchors)
        self.assertTrue(any("depends_on" in e and "unknown" in e for e in errs), errs)

    def test_rejects_depends_on_hostile_id(self):
        spec, anchors = _spec(depends_on=["<script>"])
        errs = render.validate(spec, anchors)
        self.assertTrue(any("depends_on" in e for e in errs), errs)

    def test_rejects_depends_on_non_list(self):
        spec, anchors = _spec(depends_on="n1")
        errs = render.validate(spec, anchors)
        self.assertTrue(any("depends_on must be a list" in e for e in errs), errs)


if __name__ == "__main__":
    unittest.main()
