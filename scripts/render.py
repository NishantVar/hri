#!/usr/bin/env python3
"""Render a RIView spec to a self-contained interactive HTML review document.

Usage:
    python3 render.py <spec-dir> [--output PATH]

A spec dir contains:
    <basename>.md                human-readable narrative with anchor comments
    <basename>.decisions.json    structured graph of nodes

The default basename is "spec"; pass --basename to point at e.g. mvp.md and
mvp.decisions.json living alongside other docs.

Output: a single HTML file (default: <spec-dir>/<basename>.html).
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ANCHOR_RE = re.compile(
    r"<!--\s*node:([\w-]+)\s*-->\s*\n(.*?)\n\s*<!--\s*/node:\1\s*-->",
    re.DOTALL,
)
ANCHOR_OPEN_RE = re.compile(r"<!--\s*node:([\w-]+)\s*-->")


def parse_anchored_bodies(md_text: str) -> dict[str, str]:
    bodies: dict[str, str] = {}
    for m in ANCHOR_RE.finditer(md_text):
        bodies[m.group(1)] = m.group(2).strip()
    return bodies


def count_anchor_openings(md_text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for m in ANCHOR_OPEN_RE.finditer(md_text):
        counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


def md_to_html(text: str) -> str:
    """Tiny markdown subset: paragraphs, bullets, **bold**, *italic*, `code`."""
    if not text:
        return ""
    escaped = html.escape(text)
    # Inline code first so other inline rules don't eat backtick content.
    escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", escaped)

    blocks = re.split(r"\n\s*\n", escaped.strip())
    out: list[str] = []
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if lines and all(ln.lstrip().startswith(("- ", "* ")) for ln in lines):
            items = "".join(
                f"<li>{ln.lstrip()[2:].strip()}</li>" for ln in lines
            )
            out.append(f"<ul>{items}</ul>")
        else:
            out.append("<p>" + block.replace("\n", "<br>") + "</p>")
    return "".join(out)


ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")  # anchor-safe; matches ANCHOR_RE charset

STATUS_BY_KIND = {
    "decision": {"ai-confident", "confirmed", "rejected", "needs-work"},
    "ambiguity": {"open", "resolved", "deferred"},
    "risk": {"open", "accepted", "mitigated", "dismissed"},
}
SEVERITY_VALUES = {"low", "medium", "high"}


def validate(spec: dict, anchor_counts: dict[str, int]) -> list[str]:
    errors: list[str] = []
    seen_ids: set[str] = set()
    declared_anchors: set[str] = set()
    all_ids = {
        n.get("id") for n in spec["nodes"]
        if isinstance(n, dict) and isinstance(n.get("id"), str)
    }
    for node in spec["nodes"]:
        nid = node["id"]
        deps = node.get("depends_on")
        if deps is not None:
            if not isinstance(deps, list):
                errors.append(f"node {nid}: depends_on must be a list, got {type(deps).__name__}")
            else:
                for dep in deps:
                    if not isinstance(dep, str) or not ID_RE.fullmatch(dep):
                        errors.append(
                            f"node {nid}: depends_on entry {dep!r} must match [A-Za-z0-9_-]+"
                        )
                    elif dep not in all_ids:
                        errors.append(
                            f"node {nid}: depends_on references unknown node id {dep!r}"
                        )
        if not isinstance(nid, str) or not ID_RE.fullmatch(nid):
            errors.append(
                f"node id {nid!r}: must match [A-Za-z0-9_-]+ (id is interpolated into HTML attributes)"
            )
        if nid in seen_ids:
            errors.append(f"duplicate node id: {nid}")
        seen_ids.add(nid)
        anchor = node.get("source_anchor", nid)
        if not isinstance(anchor, str) or not ID_RE.fullmatch(anchor):
            errors.append(
                f"node {nid}: source_anchor {anchor!r} must match [A-Za-z0-9_-]+"
            )
        declared_anchors.add(anchor)
        count = anchor_counts.get(anchor, 0)
        if count == 0:
            errors.append(f"node {nid}: anchor {anchor!r} missing in source markdown")
        elif count > 1:
            errors.append(
                f"node {nid}: anchor {anchor!r} occurs {count} times in source markdown (must be unique)"
            )
        kind = node["kind"]
        if kind not in STATUS_BY_KIND:
            errors.append(f"node {nid}: unknown kind {kind!r}")
            continue  # remaining kind-specific checks need a known kind
        status = node.get("status", "")
        if status not in STATUS_BY_KIND[kind]:
            errors.append(
                f"node {nid}: status {status!r} not valid for kind {kind!r} "
                f"(allowed: {sorted(STATUS_BY_KIND[kind])})"
            )
        if kind == "risk":
            sev = node.get("severity", "")
            if sev not in SEVERITY_VALUES:
                errors.append(
                    f"node {nid}: severity {sev!r} not in {sorted(SEVERITY_VALUES)}"
                )
        if kind == "ambiguity":
            for opt in node.get("options") or []:
                if not isinstance(opt, dict):
                    errors.append(f"node {nid}: option must be an object, got {opt!r}")
                    continue
                oid = opt.get("id", "")
                if not isinstance(oid, str) or not ID_RE.fullmatch(oid):
                    errors.append(
                        f"node {nid}: option id {oid!r} must match [A-Za-z0-9_-]+"
                    )
    # Anchors in the markdown that don't correspond to any declared node — useful warning.
    for anchor in anchor_counts:
        if anchor not in declared_anchors:
            errors.append(
                f"source markdown contains anchor {anchor!r} but no node in the decisions sidecar declares it"
            )
    return errors


def topo_order(nodes: list[dict]) -> list[dict]:
    """Kahn's topological sort. Ties broken by id (ascending). Cycle-safe:
    nodes left over after the main pass (i.e. nodes participating in a cycle)
    are appended in id order at the end. Dependencies pointing at unknown ids
    are ignored for ordering; render.validate() is expected to reject those
    upstream, so the leftover loop is defensive."""
    import heapq
    by_id = {n["id"]: n for n in nodes}
    indeg: dict[str, int] = {nid: 0 for nid in by_id}
    children: dict[str, list[str]] = {nid: [] for nid in by_id}
    for n in nodes:
        for dep in n.get("depends_on") or []:
            if dep in by_id:
                indeg[n["id"]] += 1
                children[dep].append(n["id"])
    ready = [nid for nid, d in indeg.items() if d == 0]
    heapq.heapify(ready)
    out: list[str] = []
    while ready:
        nid = heapq.heappop(ready)
        out.append(nid)
        for child in sorted(children[nid]):
            indeg[child] -= 1
            if indeg[child] == 0:
                heapq.heappush(ready, child)
    seen = set(out)
    for nid in sorted(by_id):
        if nid not in seen:
            out.append(nid)
    return [by_id[nid] for nid in out]


def compute_affects(nodes: list[dict]) -> dict[str, list[str]]:
    """Reverse depends_on: affects[X] = sorted ids of nodes that depend on X."""
    affects: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for n in nodes:
        for dep in n.get("depends_on") or []:
            if dep in affects:
                affects[dep].append(n["id"])
    for k in affects:
        affects[k].sort()
    return affects


def summary_counts(spec: dict) -> dict:
    counts = {
        "decision": {"ai-confident": 0, "confirmed": 0, "rejected": 0, "needs-work": 0},
        "ambiguity": {"open": 0, "resolved": 0, "deferred": 0},
        "risk": {"open": 0, "accepted": 0, "mitigated": 0, "dismissed": 0},
    }
    for node in spec["nodes"]:
        kind = node["kind"]
        status = node.get("status", "")
        if status in counts.get(kind, {}):
            counts[kind][status] += 1
    return counts


def build_html(
    spec: dict,
    bodies: dict[str, str],
    *,
    submit_url: str = "",
    submit_token: str = "",
    session_id: str | None = None,
    base_revision: int | None = None,
    overlay_entries: dict[str, dict] | None = None,
    canonical_by_id: dict[str, dict] | None = None,
) -> str:
    ordered = topo_order(spec["nodes"])
    affects = compute_affects(spec["nodes"])
    enriched_nodes = []
    for node in ordered:
        n = dict(node)
        body_md = bodies.get(node["source_anchor"], "")
        n["_body_html"] = md_to_html(body_md)
        n["_body_md"] = body_md
        n["_affects"] = affects.get(node["id"], [])
        enriched_nodes.append(n)

    counts = summary_counts(spec)
    spec_payload = {
        "spec_id": spec["spec_id"],
        "spec_title": spec["spec_title"],
        "version": spec["version"],
        "nodes": enriched_nodes,
        "counts": counts,
    }
    payload_json = json.dumps(spec_payload, indent=2)
    # Escape `</script>` defensively.
    payload_json = payload_json.replace("</", "<\\/")

    title = html.escape(spec["spec_title"])
    spec_id = html.escape(spec["spec_id"])
    submit_payload = json.dumps({
        "url": submit_url,
        "token": submit_token,
        "session_id": session_id,
        "base_revision": base_revision,
    })
    submit_payload = submit_payload.replace("</", "<\\/")
    overlay_payload = json.dumps(overlay_entries or {})
    overlay_payload = overlay_payload.replace("</", "<\\/")
    # Standalone render (canonical_by_id=None) emits {} — the client falls
    # back to n.status (which IS canonical when no overlay merge has run).
    # ADR-0011 amendment: daemon-served path always passes a populated map.
    canonical_payload = json.dumps(canonical_by_id or {})
    canonical_payload = canonical_payload.replace("</", "<\\/")
    return TEMPLATE.replace("__TITLE__", title) \
        .replace("__SPEC_ID__", spec_id) \
        .replace("__VERSION__", str(spec["version"])) \
        .replace("__PAYLOAD__", payload_json) \
        .replace("__SUBMIT__", submit_payload) \
        .replace("__OVERLAY_ENTRIES__", overlay_payload) \
        .replace("__CANONICAL_BY_ID__", canonical_payload) \
        .replace("__GENERATED_AT__", datetime.now(timezone.utc).isoformat(timespec="seconds"))


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ — RIView</title>
<style>
  :root {
    --bg: #ffffff;
    --fg: #1a1a1a;
    --muted: #5f6b7a;
    --border: #d8dee5;
    --border-strong: #b6bfcc;
    --card-bg: #fbfcfd;
    --accent: #2452a3;
    --decision: #2452a3;
    --ambiguity: #b3631d;
    --risk: #a13030;
    --status-confirmed: #1f7a3a;
    --status-rejected: #a13030;
    --status-needs-work: #b3631d;
    --status-ai: #5f6b7a;
    --status-open: #b3631d;
    --status-resolved: #1f7a3a;
    --status-deferred: #5f6b7a;
    --severity-high: #a13030;
    --severity-medium: #b3631d;
    --severity-low: #5f6b7a;
    --touched-bg: #fff7e6;
    --touched-border: #f2c878;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #14171c;
      --fg: #e6e8eb;
      --muted: #8f99a8;
      --border: #2a313b;
      --border-strong: #3b4452;
      --card-bg: #1a1e25;
      --accent: #6b9cff;
      --decision: #6b9cff;
      --ambiguity: #e6a763;
      --risk: #e07070;
      --status-confirmed: #5dc88a;
      --status-rejected: #e07070;
      --status-needs-work: #e6a763;
      --status-ai: #8f99a8;
      --status-open: #e6a763;
      --status-resolved: #5dc88a;
      --status-deferred: #8f99a8;
      --severity-high: #e07070;
      --severity-medium: #e6a763;
      --severity-low: #8f99a8;
      --touched-bg: #2c2614;
      --touched-border: #6b5320;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    background: var(--bg);
    color: var(--fg);
    line-height: 1.5;
  }
  header.topbar {
    position: sticky;
    top: 0;
    background: var(--bg);
    border-bottom: 1px solid var(--border);
    padding: 14px 24px;
    z-index: 10;
  }
  header.topbar h1 {
    margin: 0 0 4px;
    font-size: 18px;
    font-weight: 600;
  }
  header.topbar .meta {
    color: var(--muted);
    font-size: 12px;
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
  }
  header.topbar .counts {
    margin-top: 8px;
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    font-size: 12px;
  }
  header.topbar .counts span {
    padding: 2px 8px;
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--card-bg);
  }
  main {
    max-width: 880px;
    margin: 24px auto 120px;
    padding: 0 24px;
  }
  .card {
    --card-accent: var(--border-strong);
    --card-tint: var(--card-bg);
    background: var(--card-tint);
    border: 1px solid var(--border);
    border-inline-start: 4px solid var(--card-accent);
    border-radius: 8px;
    padding: 16px 14px 16px 18px;
    margin-bottom: 16px;
    transition: border-color 120ms, background-color 120ms;
  }
  /* Status-driven accent + tint. Grouped by effective color so dark mode
     inherits via the per-status CSS variables defined in :root. */
  .card[data-status="confirmed"],
  .card[data-status="accepted"],
  .card[data-status="mitigated"],
  .card[data-status="resolved"] {
    --card-accent: var(--status-confirmed);
    --card-tint: color-mix(in oklab, var(--status-confirmed) 7%, var(--card-bg));
  }
  .card[data-status="needs-work"],
  .card[data-status="open"] {
    --card-accent: var(--status-needs-work);
    --card-tint: color-mix(in oklab, var(--status-needs-work) 7%, var(--card-bg));
  }
  .card[data-status="rejected"] {
    --card-accent: var(--status-rejected);
    --card-tint: color-mix(in oklab, var(--status-rejected) 7%, var(--card-bg));
  }
  .card[data-status="ai-confident"],
  .card[data-status="deferred"],
  .card[data-status="dismissed"] {
    --card-accent: var(--status-ai);
    --card-tint: color-mix(in oklab, var(--status-ai) 5%, var(--card-bg));
  }
  .card.touched {
    background: var(--touched-bg);
    border-color: var(--touched-border);
  }
  .card .head {
    display: flex;
    align-items: baseline;
    gap: 10px;
    margin-bottom: 8px;
    flex-wrap: wrap;
  }
  .badge {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 10px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-weight: 600;
    border: 1px solid currentColor;
  }
  .badge.kind-decision { color: var(--decision); }
  .badge.kind-ambiguity { color: var(--ambiguity); }
  .badge.kind-risk { color: var(--risk); }
  .badge.status { font-weight: 500; }
  .badge.status-ai-confident { color: var(--status-ai); }
  .badge.status-confirmed { color: var(--status-confirmed); }
  .badge.status-rejected { color: var(--status-rejected); }
  .badge.status-needs-work { color: var(--status-needs-work); }
  .badge.status-open { color: var(--status-open); }
  .badge.status-resolved { color: var(--status-resolved); }
  .badge.status-deferred { color: var(--status-deferred); }
  .badge.status-accepted { color: var(--status-confirmed); }
  .badge.status-mitigated { color: var(--status-confirmed); }
  .badge.status-dismissed { color: var(--status-deferred); }
  .badge.sev-high { color: var(--severity-high); }
  .badge.sev-medium { color: var(--severity-medium); }
  .badge.sev-low { color: var(--severity-low); }
  .card h2 {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    flex-basis: 100%;
  }
  .chips {
    flex-basis: 100%;
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-top: 2px;
    font-size: 11px;
  }
  .chips .chip-label {
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 10px;
    align-self: center;
    margin-right: 2px;
  }
  .chip {
    --chip-accent: var(--border);
    --chip-fg: var(--muted);
    display: inline-flex;
    align-items: center;
    padding: 2px 8px;
    border-radius: 10px;
    border: 1px solid var(--chip-accent);
    background: var(--bg);
    color: var(--chip-fg);
    text-decoration: none;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px;
  }
  /* Dep/affects chips inherit their target node's status color so the chain
     can be traced visually. Grouped to match the card[data-status] rules. */
  .chip[data-status="confirmed"],
  .chip[data-status="accepted"],
  .chip[data-status="mitigated"],
  .chip[data-status="resolved"] {
    --chip-accent: color-mix(in oklab, var(--status-confirmed) 55%, var(--border));
    --chip-fg: var(--status-confirmed);
  }
  .chip[data-status="needs-work"],
  .chip[data-status="open"] {
    --chip-accent: color-mix(in oklab, var(--status-needs-work) 55%, var(--border));
    --chip-fg: var(--status-needs-work);
  }
  .chip[data-status="rejected"] {
    --chip-accent: color-mix(in oklab, var(--status-rejected) 55%, var(--border));
    --chip-fg: var(--status-rejected);
  }
  .chip[data-status="ai-confident"],
  .chip[data-status="deferred"],
  .chip[data-status="dismissed"] {
    --chip-accent: var(--border-strong);
    --chip-fg: var(--muted);
  }
  .chip:hover, .chip:focus-visible { border-color: var(--accent); color: var(--fg); }
  .downstream-chip {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    padding: 2px 8px;
    border-radius: 10px;
    border: 1px solid color-mix(in oklab, var(--status-needs-work) 45%, var(--border));
    background: color-mix(in oklab, var(--status-needs-work) 8%, var(--bg));
    color: var(--status-needs-work);
    font-size: 11px;
    font-weight: 600;
  }
  .stale-badge {
    display: none;
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 10px;
    border: 1px solid var(--touched-border);
    background: var(--touched-bg);
    color: var(--status-needs-work);
    font-weight: 600;
    align-items: center;
    gap: 3px;
  }
  .card.upstream-stale .stale-badge { display: inline-flex; }
  .card:target { outline: 2px solid var(--accent); outline-offset: 4px; }
  html { scroll-padding-top: 96px; }
  .card-actions {
    display: flex;
    gap: 8px;
    align-items: center;
    margin-top: 10px;
  }
  .card-actions .card-status {
    color: var(--muted);
    font-size: 12px;
    margin-left: auto;
  }
  .card-actions .card-status.error { color: var(--status-rejected); }
  .card-actions .card-status.ok { color: var(--status-confirmed); }
  .review textarea[aria-invalid="true"] { border-color: var(--status-rejected); }
  .review textarea:user-invalid { border-color: var(--status-rejected); }
  .reload-banner {
    position: sticky;
    top: 64px;
    margin: 12px auto 0;
    max-width: 880px;
    padding: 10px 14px;
    background: var(--touched-bg);
    border: 1px solid var(--touched-border);
    border-radius: 6px;
    display: none;
    align-items: center;
    gap: 12px;
    font-size: 13px;
    z-index: 9;
    animation: bannerIn 200ms ease-out;
  }
  .reload-banner.visible { display: flex; }
  .reload-banner .banner-msg { color: var(--fg); flex: 1; }
  .reload-banner code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  @keyframes bannerIn {
    from { transform: translateY(-6px); opacity: 0; }
    to { transform: translateY(0); opacity: 1; }
  }
  @media (prefers-reduced-motion: reduce) {
    .reload-banner { animation: none; }
    .card { transition: none; }
  }
  .card .id {
    color: var(--muted);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
  }
  .body { font-size: 14px; }
  .body p { margin: 8px 0; }
  .body code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    background: var(--border);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 12.5px;
  }
  .body-block { display: flex; flex-direction: column; gap: 6px; }
  .body-actions { display: flex; }
  .body-edit-toggle {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--muted);
    border-radius: 3px;
    padding: 2px 8px;
    font-size: 12px;
    cursor: pointer;
  }
  .body-edit-toggle:hover { color: var(--fg); border-color: var(--border-strong); }
  .body-edit-toggle[aria-expanded="true"] {
    color: var(--accent);
    border-color: var(--accent);
  }
  .body-editor textarea {
    width: 100%;
    min-height: 120px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 13px;
    padding: 8px;
    border: 1px solid var(--border);
    border-radius: 4px;
    resize: vertical;
    box-sizing: border-box;
  }
  .body-editor-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 4px;
  }
  .body-editor-hint { font-size: 12px; color: var(--muted); }
  .badge.body-edited {
    color: var(--accent);
    border: 1px solid var(--accent);
    background: transparent;
  }
  .meta-row {
    margin-top: 8px;
    font-size: 13px;
    color: var(--muted);
  }
  .meta-row strong { color: var(--fg); font-weight: 500; }
  .alts { font-size: 13px; color: var(--muted); }
  .alts li { margin: 2px 0; }
  details.detail {
    margin-top: 8px;
    font-size: 13px;
  }
  details.detail summary {
    cursor: pointer;
    color: var(--muted);
  }
  details.prior-review {
    margin-top: 12px;
    padding: 8px 10px;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--card-alt, var(--bg));
    font-size: 13px;
  }
  details.prior-review > summary {
    cursor: pointer;
    color: var(--muted);
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 600;
  }
  details.prior-review[open] > summary { margin-bottom: 6px; }
  .prior-review .pr-row {
    margin: 4px 0;
    color: var(--muted);
  }
  .prior-review .pr-row strong { color: var(--fg); font-weight: 500; }
  .prior-review .pr-transition .arrow { margin: 0 6px; color: var(--muted); }
  .prior-review .pr-comment {
    margin-top: 6px;
    padding: 6px 8px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 3px;
    white-space: pre-wrap;
    color: var(--fg);
  }
  .review {
    border-top: 1px dashed var(--border);
    margin-top: 14px;
    padding-top: 12px;
  }
  .review h3 {
    margin: 0 0 8px;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
    font-weight: 600;
  }
  .review label {
    display: block;
    font-size: 13px;
    margin-bottom: 8px;
  }
  .review label > span.label-text {
    display: block;
    color: var(--muted);
    font-size: 12px;
    margin-bottom: 2px;
  }
  .review select,
  .review textarea,
  .review input[type="text"] {
    width: 100%;
    font-family: inherit;
    font-size: 13px;
    padding: 6px 8px;
    border: 1px solid var(--border-strong);
    border-radius: 4px;
    background: var(--bg);
    color: var(--fg);
  }
  .review textarea { min-height: 50px; resize: vertical; }
  .review .options {
    list-style: none;
    padding: 0;
    margin: 0 0 8px;
  }
  .review .options li {
    margin: 4px 0;
    padding: 6px 8px;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--bg);
  }
  .review .options li.selected { border-color: var(--accent); }
  .review .options input[type="radio"] { margin-right: 6px; }
  .review .options .opt-body { color: var(--muted); font-size: 12px; margin-left: 18px; }
  .footer {
    position: fixed;
    bottom: 0; left: 0; right: 0;
    background: var(--bg);
    border-top: 1px solid var(--border);
    padding: 12px 24px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 13px;
    z-index: 10;
  }
  .footer .counter {
    color: var(--muted);
  }
  .footer .counter strong { color: var(--fg); }
  button.primary {
    background: var(--accent);
    color: white;
    border: none;
    padding: 8px 16px;
    border-radius: 4px;
    font-size: 14px;
    cursor: pointer;
    font-weight: 500;
  }
  button.primary:disabled {
    background: var(--muted);
    cursor: not-allowed;
  }
  button.secondary {
    background: transparent;
    color: var(--accent);
    border: 1px solid var(--accent);
    padding: 6px 12px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 13px;
  }
  dialog.export-modal {
    border: 1px solid var(--border-strong);
    border-radius: 8px;
    background: var(--bg);
    color: var(--fg);
    padding: 0;
    max-width: 720px;
    width: 90vw;
  }
  dialog.export-modal::backdrop { background: rgba(0,0,0,0.4); }
  .modal-head {
    padding: 14px 18px 8px;
    border-bottom: 1px solid var(--border);
  }
  .modal-head h2 { margin: 0 0 4px; font-size: 16px; }
  .modal-head p { margin: 0; color: var(--muted); font-size: 12px; }
  .modal-body { padding: 14px 18px; }
  .modal-body textarea {
    width: 100%;
    min-height: 280px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    border: 1px solid var(--border-strong);
    border-radius: 4px;
    padding: 8px;
    background: var(--card-bg);
    color: var(--fg);
  }
  .modal-actions {
    display: flex;
    gap: 8px;
    padding: 10px 18px 14px;
    justify-content: flex-end;
  }
  .copied { color: var(--status-confirmed); font-size: 12px; margin-right: auto; align-self: center; }
  .toolbar {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .filter-input {
    font-size: 12px;
    padding: 4px 8px;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--bg);
    color: var(--fg);
  }
  @media print {
    .footer, .review, dialog.export-modal { display: none !important; }
  }
</style>
</head>
<body>

<header class="topbar">
  <h1>__TITLE__</h1>
  <div class="meta">
    <span>spec_id: <code>__SPEC_ID__</code></span>
    <span>version: __VERSION__</span>
    <span>generated: __GENERATED_AT__</span>
  </div>
  <div class="counts" id="counts"></div>
</header>

<div class="reload-banner" id="reload-banner" role="status" aria-live="polite">
  <span class="banner-msg">Spec updated to revision <code id="banner-rev">?</code> — your in-progress reviews will be preserved on the server. Reload to see the new content.</span>
  <button type="button" class="primary" id="banner-reload-btn">Reload</button>
</div>

<main>
  <div id="nodes"></div>
</main>

<div class="footer">
  <div class="counter">
    <strong id="touched-count">0</strong> node(s) reviewed.
  </div>
  <div class="toolbar">
    <input type="text" id="filter" class="filter-input" placeholder="Filter by id/title/kind">
    <button type="button" class="secondary" id="reset-btn">Reset</button>
    <button type="button" class="primary" id="submit-all-btn" hidden disabled>Submit all</button>
    <button type="button" class="primary" id="export-btn" disabled>Export Reviews</button>
  </div>
</div>

<dialog class="export-modal" id="export-modal">
  <div class="modal-head">
    <h2>Review Delta</h2>
    <p>Paste this JSON into the agent chat, or save to a file and pass it to <code>apply.py</code>.</p>
  </div>
  <div class="modal-body">
    <textarea id="export-output" readonly></textarea>
  </div>
  <div class="modal-actions">
    <span class="copied" id="copied-msg"></span>
    <button type="button" class="primary" id="submit-server-btn" hidden>Submit to RIView server</button>
    <button type="button" class="secondary" id="download-btn">Download .json</button>
    <button type="button" class="secondary" id="copy-btn">Copy</button>
    <button type="button" class="primary" id="close-btn">Close</button>
  </div>
</dialog>

<script type="application/json" id="spec-payload">
__PAYLOAD__
</script>
<script type="application/json" id="submit-config">
__SUBMIT__
</script>
<script type="application/json" id="overlay-entries">
__OVERLAY_ENTRIES__
</script>
<script type="application/json" id="canonical-by-id">
__CANONICAL_BY_ID__
</script>

<script>
(function() {
  const SPEC = JSON.parse(document.getElementById("spec-payload").textContent);
  // Slice 1: per-node overlay entries from the daemon's submitted review.json.
  // Used to (a) baseline the comment textarea for reload-prefill, and (b)
  // compose full effective entries on partial-field edits so the daemon's
  // by-node-id replace-merge cannot silently drop other already-submitted
  // overlay fields (status / resolution / body_edit). Empty {} on standalone
  // render. The variable is `let` because successful submits advance it in
  // place so a follow-up edit on the same page composes against the new
  // overlay, not the page-load snapshot.
  let OVERLAY_BY_ID = {};
  try {
    OVERLAY_BY_ID = JSON.parse(document.getElementById("overlay-entries").textContent) || {};
  } catch (_) { OVERLAY_BY_ID = {}; }

  // ADR-0011 amendment: canonical (pre-overlay) status + ambiguity resolution
  // exposed by the daemon as a parallel JSON island. Used by buildEntryForNode
  // to detect snap-back-to-canonical and route the diff into cleared_fields so
  // the server's overlay merge evicts the node rather than persisting a no-op.
  // Empty {} in standalone mode (no daemon, no overlay merge) — JS helpers
  // below fall back to n.status, which IS canonical in that mode.
  let CANONICAL_BY_ID = {};
  try {
    CANONICAL_BY_ID = JSON.parse(document.getElementById("canonical-by-id").textContent) || {};
  } catch (_) { CANONICAL_BY_ID = {}; }

  // Status dropdown options. Every status from the node's enum is selectable
  // so the form can pre-fill to the current applied status (slice 2). The
  // initial statuses (ai-confident, open) are included for that purpose;
  // selecting them as a "new_status" is technically a downgrade but the
  // touched-vs-applied diff treats unchanged status as no-op.
  const STATUS_OPTIONS = {
    decision: [
      { value: "ai-confident", label: "AI-confident" },
      { value: "confirmed", label: "Confirm" },
      { value: "rejected", label: "Reject" },
      { value: "needs-work", label: "Needs work" }
    ],
    ambiguity: [
      { value: "open", label: "Open" },
      { value: "resolved", label: "Resolved" },
      { value: "deferred", label: "Defer" }
    ],
    risk: [
      { value: "open", label: "Open" },
      { value: "accepted", label: "Accept" },
      { value: "mitigated", label: "Mitigated" },
      { value: "dismissed", label: "Dismiss" }
    ]
  };

  function escapeHtml(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // Slice 2: applied-state lookup. Touched/diff logic compares form state
  // against APPLIED_BY_ID, not against empty — otherwise pre-filled cards
  // look touched and Submit-all would re-POST the entire spec.
  const APPLIED_BY_ID = {};
  SPEC.nodes.forEach(n => {
    const overlay = OVERLAY_BY_ID[n.id] || {};
    const overlayComment = overlay.comment;
    APPLIED_BY_ID[n.id] = {
      status: n.status,
      comment: typeof overlayComment === "string" ? overlayComment : "",
      // Applied body markdown = the overlay's body_edit if one exists,
      // else canonical. On the daemon path, n._body_md ships the
      // overlay-merged value, so it already IS the applied body — but
      // we prefer the explicit overlay field when present to keep this
      // self-contained and resilient if _body_md is absent (e.g. nodes
      // with no anchored body still get an empty applied baseline).
      body_md: (typeof overlay.body_edit === "string")
        ? overlay.body_edit
        : (typeof n._body_md === "string" ? n._body_md : ""),
      resolution: (n.kind === "ambiguity" && n.resolution && typeof n.resolution === "object")
        ? { choice_id: n.resolution.choice_id || null,
            freeform: n.resolution.freeform || null }
        : null
    };
  });

  // Per-node review state. Pre-filled from APPLIED_BY_ID so the form shows
  // what is currently true and the reviewer edits from there.
  const state = {};
  SPEC.nodes.forEach(n => {
    const applied = APPLIED_BY_ID[n.id];
    state[n.id] = {
      new_status: applied.status,
      comment: applied.comment,
      body_edit: null,
      resolution: applied.resolution
        ? { choice_id: applied.resolution.choice_id,
            freeform: applied.resolution.freeform }
        : null
    };
  });

  // Persisted status by id; used to color depends_on/affects chips and to
  // compute static downstream-pending counts. Does NOT track live form state.
  const STATUS_BY_ID = {};
  SPEC.nodes.forEach(n => { STATUS_BY_ID[n.id] = n.status; });

  // Cards in these persisted statuses get a "↓ N downstream pending" chip
  // when any of their transitive downstream are still in PENDING_STATUSES.
  // Rejection/deferral/dismissal are terminal — they don't trigger the chip.
  const APPROVING_STATUSES = new Set(["confirmed", "accepted", "mitigated", "resolved"]);
  const PENDING_STATUSES = new Set(["ai-confident", "needs-work", "open"]);

  // Forward graph from depends_on: forward[X] = ids that depend on X.
  const FORWARD = {};
  SPEC.nodes.forEach(n => { FORWARD[n.id] = []; });
  SPEC.nodes.forEach(n => {
    (Array.isArray(n.depends_on) ? n.depends_on : []).forEach(dep => {
      if (FORWARD[dep]) FORWARD[dep].push(n.id);
    });
  });

  // BFS the transitive downstream of `id`, returning ids whose persisted
  // status is still pending. Computed once per card at render time.
  function pendingDownstream(id) {
    const pending = [];
    const seen = new Set([id]);
    const queue = [...(FORWARD[id] || [])];
    while (queue.length) {
      const next = queue.shift();
      if (seen.has(next)) continue;
      seen.add(next);
      if (PENDING_STATUSES.has(STATUS_BY_ID[next])) pending.push(next);
      (FORWARD[next] || []).forEach(c => { if (!seen.has(c)) queue.push(c); });
    }
    pending.sort();
    return pending;
  }

  function resolutionsEqual(a, b) {
    if (a === b) return true;
    if (!a && !b) return true;
    const aChoice = (a && a.choice_id) || null;
    const bChoice = (b && b.choice_id) || null;
    if (aChoice !== bChoice) return false;
    const aFree = (a && typeof a.freeform === "string") ? a.freeform.trim() : "";
    const bFree = (b && typeof b.freeform === "string") ? b.freeform.trim() : "";
    return aFree === bFree;
  }

  // Canonical lookups. Daemon-served pages populate CANONICAL_BY_ID for every
  // node; standalone pages emit {} so we fall back to n.status (which is
  // canonical in standalone since no overlay merge mutates it).
  function canonicalStatus(nid) {
    const c = CANONICAL_BY_ID[nid];
    if (c && typeof c.status === "string") return c.status;
    return STATUS_BY_ID[nid];
  }
  function canonicalResolution(nid) {
    const c = CANONICAL_BY_ID[nid];
    if (c && Object.prototype.hasOwnProperty.call(c, "resolution")) return c.resolution;
    return null;
  }
  function canonicalBody(nid) {
    const c = CANONICAL_BY_ID[nid];
    if (c && typeof c.body_md === "string") return c.body_md;
    // Standalone fallback: SPEC.nodes carry _body_md, which IS canonical
    // when no overlay merge has run.
    const n = SPEC.nodes.find(x => x.id === nid);
    return (n && typeof n._body_md === "string") ? n._body_md : "";
  }

  // Slice 1+2: touched = state differs from APPLIED_BY_ID for that node.
  // Empty resolution state matches null applied resolution. Comment baseline
  // comes from the daemon overlay (last submitted comment for this node);
  // touched only if the textarea differs from that baseline.
  function isTouchedNode(nid) {
    const s = state[nid];
    const applied = APPLIED_BY_ID[nid];
    if (!s || !applied) return false;
    if (s.new_status !== applied.status) return true;
    if ((s.comment || "").trim() !== (applied.comment || "").trim()) return true;
    // body_edit: null = user hasn't engaged the editor. Any string (incl. "")
    // is a real edit. Touched if the edited string differs from the current
    // applied body (which IS the overlay body when one exists, canonical
    // otherwise — so retyping canonical with no overlay = not touched, but
    // typing canonical when an overlay exists = touched (snap-back).
    if (s.body_edit !== null && s.body_edit !== (applied.body_md || "")) return true;
    if (!resolutionsEqual(s.resolution, applied.resolution)) return true;
    return false;
  }

  // A "pure approval" is a status FLIP to {confirmed,accepted,mitigated} with
  // no other content (no comment, no resolution change, no body edit). Adds
  // no new semantic information — just endorses what's already there — so it
  // must NOT mark downstream as upstream-stale. With slice 2 prefill the
  // status flip is detected against APPLIED_BY_ID, not against empty.
  // `resolved` deliberately is NOT here: resolving an ambiguity records a
  // choice/freeform answer, which is real new content downstream may want
  // to react to.
  function isPureApprovalNode(nid) {
    const s = state[nid];
    const applied = APPLIED_BY_ID[nid];
    if (!s || !applied) return false;
    if (s.new_status === applied.status) return false; // not a flip
    if (s.new_status !== "confirmed" && s.new_status !== "accepted" && s.new_status !== "mitigated") return false;
    if (s.comment && s.comment.trim().length > 0) return false;
    if (!resolutionsEqual(s.resolution, applied.resolution)) return false;
    // Any engaged body editor (even retyping the applied body) disqualifies
    // the flip from being a "pure approval" — pure approval has no content.
    if (s.body_edit !== null && s.body_edit !== (applied.body_md || "")) return false;
    return true;
  }

  function isMaterialChangeNode(nid) {
    return isTouchedNode(nid) && !isPureApprovalNode(nid);
  }

  function recomputeStaleness() {
    SPEC.nodes.forEach(n => {
      const deps = Array.isArray(n.depends_on) ? n.depends_on : [];
      const upstreamMaterial = deps.some(d => state[d] && isMaterialChangeNode(d));
      const el = document.getElementById("card-" + n.id);
      if (el) el.classList.toggle("upstream-stale", upstreamMaterial);
    });
  }

  function renderCounts() {
    const c = SPEC.counts;
    const parts = [];
    parts.push(`<span>Decisions: ${c.decision["ai-confident"]} ai-confident · ${c.decision.confirmed} confirmed · ${c.decision.rejected} rejected · ${c.decision["needs-work"]} needs-work</span>`);
    parts.push(`<span>Ambiguities: ${c.ambiguity.open} open · ${c.ambiguity.resolved} resolved · ${c.ambiguity.deferred} deferred</span>`);
    parts.push(`<span>Risks: ${c.risk.open} open · ${c.risk.accepted} accepted · ${c.risk.mitigated} mitigated · ${c.risk.dismissed} dismissed</span>`);
    document.getElementById("counts").innerHTML = parts.join("");
  }

  function renderCard(node) {
    const card = document.createElement("section");
    card.className = "card";
    card.id = "card-" + node.id;
    card.dataset.nodeId = node.id;
    card.dataset.kind = node.kind;
    card.dataset.status = node.status;
    const depList = Array.isArray(node.depends_on) ? node.depends_on : [];
    const affList = Array.isArray(node._affects) ? node._affects : [];
    const chipText = depList.concat(affList).join(" ");
    card.dataset.searchText = (node.id + " " + node.title + " " + node.kind + " " + chipText).toLowerCase();

    // Head. Enum fields (kind/status/severity) are validated by the Python
    // renderer, but escape them anyway — daemon pages embed the auth token,
    // so a renderer-side injection would let a hostile sidecar exfil it.
    const head = document.createElement("div");
    head.className = "head";
    function chipsRow(label, ids) {
      if (!ids.length) return "";
      const chips = ids.map(id => {
        const targetStatus = STATUS_BY_ID[id] || "";
        return `<a class="chip" data-status="${escapeHtml(targetStatus)}" href="#card-${escapeHtml(id)}">${escapeHtml(id)}</a>`;
      }).join("");
      return `<div class="chips"><span class="chip-label">${label}</span>${chips}</div>`;
    }
    let downstreamChipHtml = "";
    if (APPROVING_STATUSES.has(node.status)) {
      const pending = pendingDownstream(node.id);
      if (pending.length) {
        const title = "Downstream still pending: " + pending.join(", ");
        downstreamChipHtml = `<span class="downstream-chip" title="${escapeHtml(title)}">↓ ${pending.length} downstream pending</span>`;
      }
    }
    head.innerHTML = `
      <span class="badge kind-${escapeHtml(node.kind)}">${escapeHtml(node.kind)}</span>
      <span class="badge status status-${escapeHtml(node.status)}">${escapeHtml(node.status)}</span>
      <span class="badge body-edited" data-role="body-edited-chip" hidden title="Body markdown differs from canonical in this revision">body edited</span>
      ${node.kind === "risk" ? `<span class="badge sev-${escapeHtml(node.severity)}">${escapeHtml(node.severity)} severity</span>` : ""}
      <span class="stale-badge" title="An upstream decision has unsubmitted changes — review may need recalibration.">↑ upstream changed</span>
      ${downstreamChipHtml}
      <span class="id">${escapeHtml(node.id)}</span>
      <h2>${escapeHtml(node.title)}</h2>
      ${chipsRow("depends on", depList)}
      ${chipsRow("affects", affList)}
    `;
    card.appendChild(head);

    // Body block: always rendered so the body-edit widget has a stable
    // mount even on nodes with empty canonical bodies. The rendered HTML
    // section is hidden when the inline editor is open; the editor lives
    // adjacent so changes feel attached to the prose they edit.
    const bodyBlock = document.createElement("div");
    bodyBlock.className = "body-block";
    bodyBlock.id = "body-block-" + node.id;
    const bodyHtmlId = "body-html-" + node.id;
    const bodyEditorId = "body-editor-" + node.id;
    const bodyHtml = document.createElement("div");
    bodyHtml.className = "body";
    bodyHtml.id = bodyHtmlId;
    bodyHtml.innerHTML = node._body_html || "";
    bodyBlock.appendChild(bodyHtml);

    const bodyActions = document.createElement("div");
    bodyActions.className = "body-actions";
    bodyActions.innerHTML = `
      <button type="button" class="body-edit-toggle" data-role="body-edit-toggle"
              aria-expanded="false" aria-controls="${escapeHtml(bodyEditorId)}">
        Edit body
      </button>
    `;
    bodyBlock.appendChild(bodyActions);

    const bodyEditorWrap = document.createElement("div");
    bodyEditorWrap.className = "body-editor";
    bodyEditorWrap.id = bodyEditorId;
    bodyEditorWrap.hidden = true;
    bodyEditorWrap.innerHTML = `
      <textarea data-role="body-edit"
        aria-label="Body markdown"
        placeholder="Markdown body for this node..."></textarea>
      <div class="body-editor-actions">
        <button type="button" data-role="body-edit-revert" class="secondary">
          Revert to canonical
        </button>
        <span class="body-editor-hint">Save via the card's Submit button.</span>
      </div>
    `;
    bodyBlock.appendChild(bodyEditorWrap);
    card.appendChild(bodyBlock);

    // Per-kind metadata
    if (node.kind === "decision") {
      if (node.rationale) {
        const r = document.createElement("div");
        r.className = "meta-row";
        r.innerHTML = `<strong>Rationale:</strong> ${escapeHtml(node.rationale)}`;
        card.appendChild(r);
      }
      if (node.alternatives && node.alternatives.length) {
        const det = document.createElement("details");
        det.className = "detail";
        det.innerHTML = `<summary>Alternatives considered (${node.alternatives.length})</summary>
          <ul class="alts">${node.alternatives.map(a => `<li>${escapeHtml(a)}</li>`).join("")}</ul>`;
        card.appendChild(det);
      }
    }
    if (node.kind === "risk" && node.mitigation) {
      const m = document.createElement("div");
      m.className = "meta-row";
      m.innerHTML = `<strong>Mitigation:</strong> ${escapeHtml(node.mitigation)}`;
      card.appendChild(m);
    }
    if (node.kind === "ambiguity" && node.prompt) {
      const p = document.createElement("div");
      p.className = "meta-row";
      p.innerHTML = `<strong>Prompt:</strong> ${escapeHtml(node.prompt)}`;
      card.appendChild(p);
    }

    // Prior review (set by apply.py when this node was reviewed in a previous pass)
    if (node.review && typeof node.review === "object") {
      const r = node.review;
      const rows = [];
      const before = r.status_before, after = r.status_after;
      if (before || after) {
        const sameStatus = before === after;
        rows.push(`<div class="pr-row pr-transition"><strong>Status:</strong> ` + (
          sameStatus
            ? `<span class="badge status status-${escapeHtml(after || before)}">${escapeHtml(after || before)}</span> <span class="arrow">(unchanged)</span>`
            : `<span class="badge status status-${escapeHtml(before)}">${escapeHtml(before)}</span><span class="arrow">→</span><span class="badge status status-${escapeHtml(after)}">${escapeHtml(after)}</span>`
        ) + `</div>`);
      }
      if (r.resolution && typeof r.resolution === "object") {
        const choice = r.resolution.choice_id;
        const free = r.resolution.freeform;
        const by = r.resolution.by;
        let resTxt = "";
        if (choice) {
          const opt = Array.isArray(node.options) ? node.options.find(o => o && o.id === choice) : null;
          resTxt = opt && opt.label
            ? `choice: ${escapeHtml(opt.label)} <code>${escapeHtml(choice)}</code>`
            : `choice: <code>${escapeHtml(choice)}</code>`;
        }
        else if (free) resTxt = `freeform: ${escapeHtml(free)}`;
        if (by) resTxt += ` <span class="arrow">(by ${escapeHtml(by)})</span>`;
        if (resTxt) rows.push(`<div class="pr-row"><strong>Resolution:</strong> ${resTxt}</div>`);
      }
      if (r.body_edited) {
        rows.push(`<div class="pr-row"><strong>Body:</strong> edited in this revision</div>`);
      }
      const sourceBits = [];
      if (r.review_source) sourceBits.push(escapeHtml(r.review_source));
      if (r.reviewed_at) sourceBits.push(escapeHtml(r.reviewed_at));
      if (sourceBits.length) {
        rows.push(`<div class="pr-row"><strong>Source:</strong> ${sourceBits.join(" · ")}</div>`);
      }
      const commentBlock = (r.comment && String(r.comment).trim())
        ? `<div class="pr-comment">${escapeHtml(r.comment)}</div>`
        : "";
      const det = document.createElement("details");
      det.className = "prior-review";
      det.open = false;
      const summaryLabel = r.comment && String(r.comment).trim()
        ? "Previous review (with comment)"
        : "Previous review";
      det.innerHTML = `<summary>${summaryLabel}</summary>${rows.join("")}${commentBlock}`;
      card.appendChild(det);
    }

    // Review form
    const review = document.createElement("div");
    review.className = "review";
    let html = `<h3>Your review</h3>`;

    if (node.kind === "ambiguity" && node.options && node.options.length) {
      html += `<label><span class="label-text">Resolution</span>
        <ul class="options" data-role="options">
          ${node.options.map(opt => `
            <li>
              <label>
                <input type="radio" name="opt-${escapeHtml(node.id)}" value="${escapeHtml(opt.id)}">
                <strong>${escapeHtml(opt.label)}</strong>
                ${opt.body ? `<div class="opt-body">${escapeHtml(opt.body)}</div>` : ""}
              </label>
            </li>
          `).join("")}
          <li>
            <label>
              <input type="radio" name="opt-${escapeHtml(node.id)}" value="__freeform__">
              <strong>Freeform answer</strong>
              <textarea data-role="freeform" placeholder="Write your own answer..." style="margin-top:6px;display:none"></textarea>
            </label>
          </li>
        </ul>
      </label>`;
    }

    const opts = STATUS_OPTIONS[node.kind];
    html += `<label><span class="label-text">Status</span>
      <select data-role="status">
        ${opts.map(o => `<option value="${o.value}">${o.label}</option>`).join("")}
      </select>
    </label>`;

    html += `<label><span class="label-text">Comment (optional)</span>
      <textarea data-role="comment" placeholder="Notes for the author agent..."></textarea>
    </label>`;

    html += `<div class="card-actions">
      <button type="button" class="primary card-submit" data-role="card-submit" hidden disabled>Submit decision</button>
      <span class="card-status" data-role="card-status"></span>
    </div>`;

    review.innerHTML = html;
    card.appendChild(review);

    // Wire events
    const statusSel = review.querySelector('[data-role="status"]');
    const commentTa = review.querySelector('[data-role="comment"]');
    const radios = review.querySelectorAll('input[type="radio"]');
    const freeformTa = review.querySelector('[data-role="freeform"]');
    const cardSubmitBtn = review.querySelector('[data-role="card-submit"]');
    const cardStatusEl = review.querySelector('[data-role="card-status"]');
    const bodyToggleBtn = card.querySelector('[data-role="body-edit-toggle"]');
    const bodyEditorEl = card.querySelector(".body-editor");
    const bodyRenderedEl = card.querySelector(".body");
    const bodyEditTa = card.querySelector('[data-role="body-edit"]');
    const bodyRevertBtn = card.querySelector('[data-role="body-edit-revert"]');

    // Slice 2+3: pre-fill is handled by the caller (init) AFTER the card is
    // appended to the document — resyncCardDom uses document.getElementById
    // to find the card, which fails until the card is in the DOM. Cards with
    // an active overlay (state.new_status differs from the first-option
    // default) would otherwise stay stuck on the default select option.

    function updateTouched() {
      const touched = isTouchedNode(node.id);
      card.classList.toggle("touched", touched);
      if (cardSubmitBtn) cardSubmitBtn.disabled = !touched;
      // Clear stale "ok" status messages once the user starts editing again.
      if (cardStatusEl && cardStatusEl.classList.contains("ok")) {
        cardStatusEl.textContent = "";
        cardStatusEl.classList.remove("ok");
      }
      recomputeFooter();
      recomputeStaleness();
      // Slice 3: persist draft on every state mutation.
      saveDraft();
    }

    if (cardSubmitBtn) {
      cardSubmitBtn.addEventListener("click", async () => {
        const entry = buildEntryForNode(node);
        if (entry === "invalid") {
          cardStatusEl.textContent = "Pick or write a resolution before submitting.";
          cardStatusEl.classList.add("error");
          cardStatusEl.classList.remove("ok");
          if (freeformTa) freeformTa.setAttribute("aria-invalid", "true");
          return;
        }
        if (!entry) return;
        cardSubmitBtn.disabled = true;
        cardStatusEl.textContent = "Submitting...";
        cardStatusEl.classList.remove("error", "ok");
        try {
          const data = await postReviews([entry]);
          const conflict = (data.conflicts || []).find(c => c.node_id === entry.node_id);
          if (conflict) {
            const reason = conflict.reason || "node changed since this page loaded";
            cardStatusEl.textContent = "Conflict (rev " + (data.current_revision || "?") + "): " + reason + ". Reload to see the new version.";
            cardStatusEl.classList.add("error");
            cardSubmitBtn.disabled = false;
          } else {
            cardStatusEl.textContent = "Submitted (rev " + (data.current_revision || data.revision || "?") + ", " + (data.review_count || 1) + " on server)";
            cardStatusEl.classList.add("ok");
            // Advance the in-memory baseline so the form reflects what is
            // now on the server. Without this, resetNodeStateToApplied below
            // would snap the form back to the page-load baseline, briefly
            // hiding the accepted edit until the next reload.
            advanceBaselineFromEntry(entry);
            resetNodeStateToApplied(node.id);
            resyncCardDom(node.id);
            saveDraft();
            recomputeFooter();
            recomputeStaleness();
          }
        } catch (err) {
          cardStatusEl.textContent = "Failed: " + (err && err.message ? err.message : err);
          cardStatusEl.classList.add("error");
          cardSubmitBtn.disabled = false;
        }
      });
    }

    statusSel.addEventListener("change", () => {
      state[node.id].new_status = statusSel.value;
      // If the user manually changes an ambiguity's status away from
      // "resolved", drop any selected resolution so we never emit a delta
      // that apply.py rejects ("resolution provided but effective status
      // is not 'resolved'").
      if (node.kind === "ambiguity" && statusSel.value !== "resolved") {
        state[node.id].resolution = null;
        review.querySelectorAll('input[type="radio"]').forEach(r => { r.checked = false; });
        review.querySelectorAll(".options li").forEach(x => x.classList.remove("selected"));
        if (freeformTa) {
          freeformTa.style.display = "none";
          freeformTa.required = false;
          freeformTa.setAttribute("aria-invalid", "false");
        }
      }
      updateTouched();
    });
    commentTa.addEventListener("input", () => {
      state[node.id].comment = commentTa.value;
      updateTouched();
    });
    function maybeAutoResolve() {
      // Auto-flip status to "resolved" when (a) the user just picked a valid
      // resolution AND (b) status select still shows the applied status — i.e.,
      // the reviewer hasn't manually chosen a different status. Slice 2 pre-fills
      // status to the applied value, so the original "is empty" gate would never
      // fire; matching against APPLIED_BY_ID preserves the intent.
      const r = state[node.id].resolution;
      const valid = r && (
        (r.choice_id) ||
        (typeof r.freeform === "string" && r.freeform.trim().length > 0)
      );
      const appliedStatus = (APPLIED_BY_ID[node.id] || {}).status || "";
      if (valid && statusSel.value === appliedStatus && appliedStatus !== "resolved") {
        statusSel.value = "resolved";
        state[node.id].new_status = "resolved";
      }
    }

    radios.forEach(r => {
      r.addEventListener("change", () => {
        const li = r.closest("li");
        review.querySelectorAll(".options li").forEach(x => x.classList.remove("selected"));
        if (r.checked) li.classList.add("selected");
        if (r.value === "__freeform__") {
          if (freeformTa) {
            freeformTa.style.display = "block";
            freeformTa.required = true;
            freeformTa.focus();
            const text = (freeformTa.value || "").trim();
            state[node.id].resolution = text ? { freeform: freeformTa.value } : null;
            freeformTa.setAttribute("aria-invalid", text ? "false" : "true");
          } else {
            state[node.id].resolution = null;
          }
        } else {
          if (freeformTa) {
            freeformTa.style.display = "none";
            freeformTa.required = false;
            freeformTa.setAttribute("aria-invalid", "false");
          }
          state[node.id].resolution = { choice_id: r.value };
        }
        maybeAutoResolve();
        updateTouched();
      });
    });
    if (freeformTa) {
      freeformTa.addEventListener("input", () => {
        const text = freeformTa.value;
        state[node.id].resolution = text.trim() ? { freeform: text } : null;
        freeformTa.setAttribute("aria-invalid", text.trim() ? "false" : "true");
        maybeAutoResolve();
        updateTouched();
      });
    }

    if (bodyToggleBtn && bodyEditorEl && bodyEditTa && bodyRenderedEl) {
      bodyToggleBtn.addEventListener("click", () => {
        const wasOpen = bodyToggleBtn.getAttribute("aria-expanded") === "true";
        const open = !wasOpen;
        bodyToggleBtn.setAttribute("aria-expanded", open ? "true" : "false");
        bodyToggleBtn.textContent = open ? "Hide body editor" : "Edit body";
        bodyEditorEl.hidden = !open;
        bodyRenderedEl.hidden = open;
        if (open) {
          // Prefill from current state when the editor is opened: if the
          // user has typed before, restore that draft; otherwise start
          // from the applied body (overlay body if one exists, canonical
          // otherwise — matches the rendered view).
          const cur = state[node.id];
          const applied = APPLIED_BY_ID[node.id] || {};
          bodyEditTa.value = (cur.body_edit !== null)
            ? cur.body_edit
            : (applied.body_md || "");
          bodyEditTa.focus();
        }
      });
      bodyEditTa.addEventListener("input", () => {
        state[node.id].body_edit = bodyEditTa.value;
        updateTouched();
      });
      if (bodyRevertBtn) {
        bodyRevertBtn.addEventListener("click", () => {
          // Set the textarea + state to canonical so submit composes a
          // snap-back entry (cleared_fields: ["body_edit"]) that evicts
          // any existing overlay body.
          const canonical = canonicalBody(node.id);
          bodyEditTa.value = canonical;
          state[node.id].body_edit = canonical;
          updateTouched();
          bodyEditTa.focus();
        });
      }
    }

    return card;
  }

  function recomputeFooter() {
    const touched = Object.keys(state).filter(isTouchedNode).length;
    document.getElementById("touched-count").textContent = touched;
    document.getElementById("export-btn").disabled = touched === 0;
    const submitAllBtn = document.getElementById("submit-all-btn");
    if (submitAllBtn) submitAllBtn.disabled = touched === 0;
  }

  function flashBanner(msg, isError) {
    const banner = document.getElementById("reload-banner");
    const msgEl = banner.querySelector(".banner-msg");
    msgEl.textContent = msg;
    banner.classList.add("visible");
    banner.classList.toggle("error", Boolean(isError));
    // Auto-dismiss non-revision banners after 6s.
    if (!banner.dataset.persistent) {
      setTimeout(() => {
        if (!banner.dataset.persistent) banner.classList.remove("visible");
      }, 6000);
    }
  }

  function showRevisionBanner(revision) {
    const banner = document.getElementById("reload-banner");
    const revEl = document.getElementById("banner-rev");
    revEl.textContent = String(revision);
    banner.querySelector(".banner-msg").innerHTML =
      'Spec updated to revision <code id="banner-rev">' + revision +
      '</code> — your in-progress reviews will be preserved on the server. Reload to see the new content.';
    banner.dataset.persistent = "1";
    banner.classList.add("visible");
  }

  function subscribeToEvents() {
    if (!window.EventSource || !SUBMIT.url) return;
    const eventsUrl = SUBMIT.url.replace(/\/review$/, "/events");
    let es;
    try {
      es = new EventSource(eventsUrl);
    } catch (_) { return; }
    es.onmessage = (e) => {
      let data;
      try { data = JSON.parse(e.data); } catch (_) { return; }
      if (typeof data.revision === "number" && data.revision > SPEC.version) {
        showRevisionBanner(data.revision);
      }
    };
    // EventSource auto-reconnects on transient errors; nothing to do on onerror.
  }

  function buildEntryForNode(n) {
    const s = state[n.id];
    const applied = APPLIED_BY_ID[n.id] || { status: "", comment: "", resolution: null };
    const statusDiff = s.new_status !== applied.status;
    const trimmedComment = (s.comment || "").trim();
    const appliedComment = (applied.comment || "").trim();
    const commentDiff = trimmedComment !== appliedComment;
    const resolutionDiff = !resolutionsEqual(s.resolution, applied.resolution);
    const appliedBody = applied.body_md || "";
    const bodyDiff = s.body_edit !== null && s.body_edit !== appliedBody;
    // Touched check — no-op if state matches the merged baseline.
    if (!statusDiff && !commentDiff && !resolutionDiff && !bodyDiff) return null;
    // Validity: ambiguity with effective status "resolved" needs a resolution.
    if (n.kind === "ambiguity" && s.new_status === "resolved") {
      const hasValidResolution = s.resolution && (
        (s.resolution.choice_id) ||
        (s.resolution.freeform && s.resolution.freeform.trim())
      );
      if (!hasValidResolution) return "invalid";
    }
    // Compose a FULL effective overlay entry. The daemon's same-revision
    // merge replaces the prior entry by node_id, so a sparse diff would
    // silently drop other already-submitted overlay fields. Start from the
    // existing overlay entry, then layer the user's changes on top. Fields
    // the user did not touch are carried through from OVERLAY_BY_ID.
    const existing = OVERLAY_BY_ID[n.id] || {};
    const entry = { node_id: n.id };
    // new_status: user's value if they changed it, else preserve existing.
    if (statusDiff) {
      entry.new_status = s.new_status;
    } else if (typeof existing.new_status === "string" && existing.new_status) {
      entry.new_status = existing.new_status;
    }
    // resolution: user's value if changed; else preserve existing.
    if (resolutionDiff) {
      if (s.resolution) {
        const r = {};
        if (s.resolution.choice_id) r.choice_id = s.resolution.choice_id;
        if (s.resolution.freeform && s.resolution.freeform.trim()) {
          r.freeform = s.resolution.freeform.trim();
        }
        if (Object.keys(r).length > 0) entry.resolution = r;
      }
    } else if (existing.resolution && typeof existing.resolution === "object") {
      entry.resolution = existing.resolution;
    }
    // body_edit: user's value if they edited (explicit null = untouched);
    // else preserve existing. Empty string is a real edit (user deleted
    // the body intentionally) — only skip when truly untouched.
    if (s.body_edit !== null) {
      entry.body_edit = s.body_edit;
    } else if (typeof existing.body_edit === "string") {
      entry.body_edit = existing.body_edit;
    }
    // comment: state value is canonical here. Empty comment intentionally
    // clears the overlay's prior comment (user explicitly emptied the box).
    if (trimmedComment) entry.comment = trimmedComment;
    // Snap-back-to-canonical (ADR-0011 amendment). If the user dragged a
    // field back to its canonical value, including it as a value is a no-op
    // overlay — drop it and list the field in `cleared_fields` so the
    // server's overlay merge evicts the field (or the whole node if nothing
    // remains). Canonical comes from CANONICAL_BY_ID (daemon path) or
    // STATUS_BY_ID fallback (standalone, where node.status IS canonical).
    const clearedFields = [];
    if (typeof entry.new_status === "string"
        && entry.new_status === canonicalStatus(n.id)) {
      clearedFields.push("new_status");
      delete entry.new_status;
    }
    if (n.kind === "ambiguity"
        && resolutionDiff
        && resolutionsEqual(s.resolution, canonicalResolution(n.id))) {
      // `entry.resolution` may be absent here (user cleared resolution and
      // canonical is null). Always list it so field-merge doesn't preserve
      // a stale prior-overlay resolution.
      clearedFields.push("resolution");
      delete entry.resolution;
    }
    if (bodyDiff && s.body_edit === canonicalBody(n.id)) {
      // User dragged the body back to canonical markdown. Drop body_edit
      // from the entry and list it so the server's overlay merge evicts
      // any prior body_edit (or the whole node if nothing else remains).
      clearedFields.push("body_edit");
      delete entry.body_edit;
    }
    // Field-merge edge: when `cleared_fields` is present, the server's merge
    // PRESERVES any prior-overlay field the entry doesn't mention. So when
    // the user intentionally removed a field (empty comment, dropped
    // resolution) we must also list it — otherwise the prior overlay value
    // survives. The conditions below mirror the "user removed this" cases
    // not already handled by snap-back above.
    if (commentDiff && !trimmedComment && existing.comment !== undefined
        && clearedFields.indexOf("comment") === -1) {
      clearedFields.push("comment");
    }
    if (resolutionDiff && entry.resolution === undefined && existing.resolution !== undefined
        && clearedFields.indexOf("resolution") === -1) {
      clearedFields.push("resolution");
    }
    // If the composed entry has no meaningful fields but there IS an
    // existing overlay entry for this node, the user effectively cleared
    // the overlay (e.g. cleared a comment-only entry). The daemon's
    // empty-entry filter would otherwise drop this submit and the stale
    // overlay comment would survive on reload. Carry an explicit
    // `cleared_fields` marker so the daemon's overlay merge path knows to
    // evict the node (ADR-0011).
    const meaningfulKeys = ["new_status", "resolution", "body_edit", "comment"];
    const isEmpty = !meaningfulKeys.some(k => entry[k] !== undefined);
    if (isEmpty && clearedFields.length === 0) {
      const existingKeys = Object.keys(existing).filter(k => k !== "node_id");
      if (existingKeys.length > 0) {
        existingKeys.forEach(k => clearedFields.push(k));
      } else {
        // No overlay existed and no meaningful diff produced — nothing to submit.
        return null;
      }
    }
    if (clearedFields.length > 0) entry.cleared_fields = clearedFields;
    return entry;
  }

  function buildDelta() {
    const reviews = [];
    const dropped = [];
    SPEC.nodes.forEach(n => {
      const entry = buildEntryForNode(n);
      if (entry === "invalid") { dropped.push(n.id); return; }
      if (entry) reviews.push(entry);
    });
    reviews.sort((a, b) => a.node_id < b.node_id ? -1 : (a.node_id > b.node_id ? 1 : 0));
    return {
      delta: {
        spec_id: SPEC.spec_id,
        spec_version: SPEC.version,
        reviewed_at: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
        reviewer: null,
        reviews: reviews
      },
      dropped: dropped
    };
  }

  async function postReviews(reviewEntries) {
    if (!SUBMIT.url) throw new Error("no submit URL configured");
    const payload = {
      spec_id: SPEC.spec_id,
      spec_version: SPEC.version,
      reviewed_at: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
      reviewer: null,
      reviews: reviewEntries
    };
    if (SUBMIT.base_revision != null) {
      payload.base_revision = SUBMIT.base_revision;
    }
    const body = JSON.stringify(payload);
    const res = await fetch(SUBMIT.url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Riview-Token": SUBMIT.token,
      },
      body: body,
    });
    if (!res.ok) {
      const txt = await res.text().catch(() => "");
      throw new Error("HTTP " + res.status + (txt ? ": " + txt.slice(0, 200) : ""));
    }
    return res.json().catch(() => ({}));
  }

  let SUBMIT = { url: "", token: "", session_id: null, base_revision: null };

  // Slice 3: localStorage draft persistence. Key shape mirrors the design's
  // deci-persist-form-state contract: session-scoped on daemon pages,
  // standalone-scoped when no session (e.g. render.py invoked on a file).
  // Only entries that DIFFER from APPLIED_BY_ID are persisted, so the blob
  // stays small and a "blank" page never writes to storage.
  const DRAFT_RETAIN_REVS = 5;
  function draftKeyPrefix() {
    if (SUBMIT && SUBMIT.session_id) {
      return "riview:draft:" + SUBMIT.session_id + ":";
    }
    return "riview:draft:standalone:" + SPEC.spec_id + ":";
  }
  function draftKeyRev() {
    if (SUBMIT && SUBMIT.session_id && SUBMIT.base_revision != null) {
      return SUBMIT.base_revision;
    }
    return SPEC.version;
  }
  function draftKey() {
    return draftKeyPrefix() + draftKeyRev();
  }
  function loadDraft() {
    try {
      const raw = localStorage.getItem(draftKey());
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      return (parsed && typeof parsed === "object") ? parsed : null;
    } catch (e) { return null; }
  }
  function saveDraft() {
    try {
      const sparse = {};
      Object.keys(state).forEach(nid => {
        if (isTouchedNode(nid)) sparse[nid] = state[nid];
      });
      if (Object.keys(sparse).length === 0) {
        localStorage.removeItem(draftKey());
      } else {
        localStorage.setItem(draftKey(), JSON.stringify(sparse));
      }
    } catch (e) { /* quota / disabled — best-effort persistence */ }
  }
  function pruneOldDrafts() {
    try {
      const prefix = draftKeyPrefix();
      const cur = draftKeyRev();
      if (cur == null) return;
      const stale = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && k.startsWith(prefix)) {
          const tail = k.slice(prefix.length);
          const n = parseInt(tail, 10);
          if (Number.isFinite(n) && n < (cur - DRAFT_RETAIN_REVS + 1)) {
            stale.push(k);
          }
        }
      }
      stale.forEach(k => localStorage.removeItem(k));
    } catch (e) { /* localStorage unavailable */ }
  }
  function rehydrateFromDraft() {
    const draft = loadDraft();
    if (!draft) return;
    Object.keys(draft).forEach(nid => {
      if (!state[nid]) return;
      const persisted = draft[nid];
      if (!persisted || typeof persisted !== "object") return;
      if (typeof persisted.new_status === "string") {
        state[nid].new_status = persisted.new_status;
      }
      if (typeof persisted.comment === "string") {
        state[nid].comment = persisted.comment;
      }
      if (persisted.body_edit !== undefined) {
        state[nid].body_edit = persisted.body_edit;
      }
      if (persisted.resolution === null) {
        state[nid].resolution = null;
      } else if (persisted.resolution && typeof persisted.resolution === "object") {
        state[nid].resolution = {
          choice_id: persisted.resolution.choice_id || null,
          freeform: persisted.resolution.freeform || null,
        };
      }
    });
  }
  function resetNodeStateToApplied(nid) {
    const applied = APPLIED_BY_ID[nid];
    if (!applied || !state[nid]) return;
    state[nid].new_status = applied.status;
    state[nid].comment = applied.comment || "";
    state[nid].body_edit = null;
    state[nid].resolution = applied.resolution
      ? { choice_id: applied.resolution.choice_id,
          freeform: applied.resolution.freeform }
      : null;
  }
  // Slice 1: after the daemon accepts a submitted entry, advance the
  // in-memory overlay + applied baselines so subsequent edits on this page
  // compose against the new server state, not against the page-load
  // snapshot. The submitted `entry` is the full effective overlay entry
  // (see buildEntryForNode); apply the same field-clear semantics here
  // that the daemon uses on disk so the page stays consistent without a
  // reload.
  function advanceBaselineFromEntry(entry) {
    if (!entry || typeof entry !== "object" || !entry.node_id) return;
    const nid = entry.node_id;
    const node = SPEC.nodes.find(x => x.id === nid);
    const cleared = Array.isArray(entry.cleared_fields) ? entry.cleared_fields : [];
    if (cleared.length > 0) {
      const prior = OVERLAY_BY_ID[nid] || {};
      const merged = Object.assign({}, prior);
      cleared.forEach(f => { delete merged[f]; });
      ["new_status", "resolution", "body_edit", "comment"].forEach(f => {
        if (entry[f] !== undefined) merged[f] = entry[f];
      });
      const meaningfulKeys = Object.keys(merged).filter(k => k !== "node_id");
      if (meaningfulKeys.length === 0) {
        delete OVERLAY_BY_ID[nid];
      } else {
        merged.node_id = nid;
        OVERLAY_BY_ID[nid] = merged;
      }
    } else {
      OVERLAY_BY_ID[nid] = entry;
    }
    const applied = APPLIED_BY_ID[nid];
    if (!applied) return;
    const newOverlay = OVERLAY_BY_ID[nid];
    // Status: overlay value wins; otherwise snap APPLIED + STATUS_BY_ID back
    // to canonical so subsequent edits on the same page diff against the
    // real post-submit state (ADR-0011 amendment — canonical now exposed
    // via CANONICAL_BY_ID, so the cleared/evicted path can rebase locally
    // instead of waiting for a reload).
    if (newOverlay && typeof newOverlay.new_status === "string" && newOverlay.new_status) {
      applied.status = newOverlay.new_status;
      STATUS_BY_ID[nid] = newOverlay.new_status;
    } else {
      const canon = canonicalStatus(nid);
      if (typeof canon === "string") {
        applied.status = canon;
        STATUS_BY_ID[nid] = canon;
      }
    }
    if (newOverlay && newOverlay.resolution && typeof newOverlay.resolution === "object") {
      applied.resolution = {
        choice_id: newOverlay.resolution.choice_id || null,
        freeform: newOverlay.resolution.freeform || null,
      };
    } else if (node && node.kind === "ambiguity") {
      // Overlay no longer carries a resolution — restore canonical (may
      // itself be null/absent for unresolved ambiguities; for ambiguities
      // canonically resolved in the spec we restore the real object so the
      // next edit composes correctly without a reload).
      const canonRes = canonicalResolution(nid);
      if (canonRes && typeof canonRes === "object") {
        applied.resolution = {
          choice_id: canonRes.choice_id || null,
          freeform: canonRes.freeform || null,
        };
      } else {
        applied.resolution = null;
      }
    }
    applied.comment = (newOverlay && typeof newOverlay.comment === "string")
      ? newOverlay.comment : "";
    // Body: overlay value wins; otherwise snap back to canonical so the
    // next edit on this page composes against post-submit truth rather
    // than the page-load body. Without this, a snap-back submit would
    // leave applied.body_md pointing at the now-evicted overlay body
    // and the next edit would diff against stale state.
    if (newOverlay && typeof newOverlay.body_edit === "string") {
      applied.body_md = newOverlay.body_edit;
    } else {
      applied.body_md = canonicalBody(nid);
    }
  }
  function resyncCardDom(nid) {
    const card = document.getElementById("card-" + nid);
    if (!card) return;
    const review = card.querySelector(".review");
    if (!review) return;
    const statusSel = review.querySelector('[data-role="status"]');
    const commentTa = review.querySelector('[data-role="comment"]');
    const radios = review.querySelectorAll('input[type="radio"]');
    const freeformTa = review.querySelector('[data-role="freeform"]');
    const cur = state[nid];
    if (!cur) return;
    if (statusSel) {
      const matchOpt = Array.from(statusSel.options).find(o => o.value === cur.new_status);
      if (matchOpt) statusSel.value = cur.new_status;
    }
    if (commentTa) commentTa.value = cur.comment || "";
    radios.forEach(r => { r.checked = false; });
    review.querySelectorAll(".options li").forEach(x => x.classList.remove("selected"));
    if (freeformTa) {
      freeformTa.style.display = "none";
      freeformTa.required = false;
      freeformTa.setAttribute("aria-invalid", "false");
      freeformTa.value = "";
    }
    if (cur.resolution) {
      if (cur.resolution.choice_id) {
        const radio = Array.from(radios).find(r => r.value === cur.resolution.choice_id);
        if (radio) {
          radio.checked = true;
          const li = radio.closest("li");
          if (li) li.classList.add("selected");
        }
      } else if (cur.resolution.freeform && freeformTa) {
        const ff = Array.from(radios).find(r => r.value === "__freeform__");
        if (ff) {
          ff.checked = true;
          const li = ff.closest("li");
          if (li) li.classList.add("selected");
        }
        freeformTa.style.display = "block";
        freeformTa.value = cur.resolution.freeform;
      }
    }
    // Body editor: when open, prefill from current state (overlay edit) or
    // the applied baseline. When closed, nothing to sync — the rendered
    // .body block is server-generated and stays as-is until reload.
    const bodyEditor = card.querySelector('[data-role="body-edit"]');
    if (bodyEditor) {
      const applied = APPLIED_BY_ID[nid] || {};
      bodyEditor.value = (cur.body_edit !== null) ? cur.body_edit : (applied.body_md || "");
    }
    updateEditedChip(card, nid);
    card.classList.toggle("touched", isTouchedNode(nid));
  }

  // "Body edited" chip: shows when applied body (the user's currently saved
  // state, including any overlay) differs from canonical. Mirrors how the
  // status badge surfaces the saved state — the goal is "did this revision
  // change the body from canonical?", not "is the user currently typing?".
  function updateEditedChip(card, nid) {
    const chip = card.querySelector('[data-role="body-edited-chip"]');
    if (!chip) return;
    const applied = APPLIED_BY_ID[nid] || {};
    const edited = (applied.body_md || "") !== canonicalBody(nid);
    chip.hidden = !edited;
  }

  function init() {
    // Read SUBMIT before rendering cards so localStorage rehydration runs
    // against the correct draft key (session_id/base_revision come from here).
    try {
      SUBMIT = JSON.parse(document.getElementById("submit-config").textContent);
    } catch (e) { /* keep default */ }
    pruneOldDrafts();
    rehydrateFromDraft();
    renderCounts();
    const container = document.getElementById("nodes");
    SPEC.nodes.forEach(n => container.appendChild(renderCard(n)));
    // Prefill form fields from state. Must run AFTER append — resyncCardDom
    // looks the card up via document.getElementById.
    SPEC.nodes.forEach(n => resyncCardDom(n.id));
    recomputeFooter();

    const modal = document.getElementById("export-modal");
    const output = document.getElementById("export-output");
    const copied = document.getElementById("copied-msg");

    document.getElementById("export-btn").addEventListener("click", () => {
      const built = buildDelta();
      output.value = JSON.stringify(built.delta, null, 2);
      if (built.dropped.length) {
        copied.textContent = "Dropped " + built.dropped.length +
          " incomplete entry/ies: " + built.dropped.join(", ");
      } else {
        copied.textContent = "";
      }
      modal.showModal();
    });
    document.getElementById("close-btn").addEventListener("click", () => modal.close());
    document.getElementById("copy-btn").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(output.value);
        copied.textContent = "Copied to clipboard";
      } catch (e) {
        output.select();
        document.execCommand("copy");
        copied.textContent = "Copied (fallback)";
      }
    });
    document.getElementById("download-btn").addEventListener("click", () => {
      const blob = new Blob([output.value], { type: "application/json" });
      const a = document.createElement("a");
      const stamp = new Date().toISOString().replace(/[:.]/g, "-").replace(/Z$/, "Z");
      a.href = URL.createObjectURL(blob);
      a.download = `review-${SPEC.spec_id}-${stamp}.json`;
      a.click();
      URL.revokeObjectURL(a.href);
    });

    const submitBtn = document.getElementById("submit-server-btn");
    const submitAllBtn = document.getElementById("submit-all-btn");
    if (SUBMIT.url) {
      // Reveal per-card submit buttons.
      document.querySelectorAll('[data-role="card-submit"]').forEach(b => {
        b.hidden = false;
      });
      submitAllBtn.hidden = false;
      submitBtn.hidden = false;
      submitBtn.addEventListener("click", async () => {
        submitBtn.disabled = true;
        copied.textContent = "Submitting...";
        try {
          const data = await postReviews(JSON.parse(output.value).reviews);
          const conflicts = data.conflicts || [];
          let msg = "Submitted (rev " + (data.current_revision || data.revision || "?") + ", status " + (data.status || "?") + ")";
          if (conflicts.length) {
            msg += "; " + conflicts.length + " conflict(s): " + conflicts.map(c => c.node_id).join(", ");
          }
          copied.textContent = msg;
        } catch (err) {
          copied.textContent = "Submit error: " + (err && err.message ? err.message : err);
        } finally {
          submitBtn.disabled = false;
        }
      });

      submitAllBtn.addEventListener("click", async () => {
        const built = buildDelta();
        if (!built.delta.reviews.length) return;
        submitAllBtn.disabled = true;
        try {
          const data = await postReviews(built.delta.reviews);
          const conflicts = data.conflicts || [];
          const accepted = (data.accepted || []).length;
          let msg;
          if (data.accepted) {
            msg = "Submitted: " + accepted + " accepted, " + conflicts.length + " conflict(s); server has " + (data.review_count || "?") + " for rev " + (data.current_revision || data.revision || "?");
          } else {
            msg = "Submitted " + built.delta.reviews.length + " review(s); server has " + (data.review_count || "?") + " for rev " + (data.current_revision || data.revision || "?");
          }
          if (conflicts.length) {
            msg += " — conflicts: " + conflicts.map(c => c.node_id).join(", ") + ". Reload to see latest.";
          }
          if (built.dropped.length) {
            msg += " Dropped " + built.dropped.length + " incomplete.";
          }
          flashBanner(msg, conflicts.length > 0);
          // Slice 3: reset state + draft for accepted entries; leave conflicts
          // alone so the reviewer can address them after a reload.
          const acceptedIds = new Set();
          if (data.accepted) {
            (data.accepted || []).forEach(a => acceptedIds.add(a.node_id));
          } else {
            // CLI-style response (no per-node accepted list): everything submitted
            // landed unless the call errored.
            built.delta.reviews.forEach(r => acceptedIds.add(r.node_id));
          }
          const entryByNodeId = {};
          built.delta.reviews.forEach(r => { entryByNodeId[r.node_id] = r; });
          acceptedIds.forEach(nid => {
            advanceBaselineFromEntry(entryByNodeId[nid]);
            resetNodeStateToApplied(nid);
            resyncCardDom(nid);
          });
          saveDraft();
          recomputeFooter();
          recomputeStaleness();
          submitAllBtn.disabled = false;
        } catch (err) {
          flashBanner("Submit-all failed: " + (err && err.message ? err.message : err), true);
          submitAllBtn.disabled = false;
        }
      });

      subscribeToEvents();
    }

    document.getElementById("reset-btn").addEventListener("click", () => {
      if (!confirm("Clear all review inputs on this page?")) return;
      location.reload();
    });

    document.getElementById("banner-reload-btn").addEventListener("click", () => {
      const reduced = matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (!reduced && document.startViewTransition) {
        document.startViewTransition(() => location.reload());
      } else {
        location.reload();
      }
    });

    document.getElementById("filter").addEventListener("input", (e) => {
      const q = e.target.value.toLowerCase().trim();
      document.querySelectorAll(".card").forEach(c => {
        const t = c.dataset.searchText;
        c.style.display = (!q || t.includes(q)) ? "" : "none";
      });
    });
  }

  init();
})();
</script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec_dir", type=Path, help="Folder containing <basename>.md and <basename>.decisions.json")
    parser.add_argument("--basename", default="spec",
                        help="Spec file basename (default: spec). Use 'mvp' to target mvp.md + mvp.decisions.json.")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Output HTML path. Default: <spec-dir>/<basename>.html.")
    args = parser.parse_args(argv)

    md_path = args.spec_dir / f"{args.basename}.md"
    json_path = args.spec_dir / f"{args.basename}.decisions.json"
    if not md_path.exists() or not json_path.exists():
        print(f"missing {md_path.name} or {json_path.name} in {args.spec_dir}", file=sys.stderr)
        return 2

    md_text = md_path.read_text()
    spec = json.loads(json_path.read_text())
    bodies = parse_anchored_bodies(md_text)
    anchor_counts = count_anchor_openings(md_text)
    errors = validate(spec, anchor_counts)
    if errors:
        print("validation errors:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 3

    html_out = build_html(spec, bodies)
    out_path = args.output or args.spec_dir / f"{args.basename}.html"
    out_path.write_text(html_out)
    print(f"wrote {out_path} ({len(html_out)} bytes, {len(spec['nodes'])} nodes, v{spec['version']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
