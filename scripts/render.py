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
    submit_payload = json.dumps({"url": submit_url, "token": submit_token})
    submit_payload = submit_payload.replace("</", "<\\/")
    return TEMPLATE.replace("__TITLE__", title) \
        .replace("__SPEC_ID__", spec_id) \
        .replace("__VERSION__", str(spec["version"])) \
        .replace("__PAYLOAD__", payload_json) \
        .replace("__SUBMIT__", submit_payload) \
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
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 18px;
    margin-bottom: 16px;
    transition: border-color 120ms;
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
    display: inline-flex;
    align-items: center;
    padding: 2px 8px;
    border-radius: 10px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--muted);
    text-decoration: none;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px;
  }
  .chip:hover, .chip:focus-visible { border-color: var(--accent); color: var(--fg); }
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

<script>
(function() {
  const SPEC = JSON.parse(document.getElementById("spec-payload").textContent);

  const STATUS_OPTIONS = {
    decision: [
      { value: "", label: "— No change —" },
      { value: "confirmed", label: "Confirm" },
      { value: "rejected", label: "Reject" },
      { value: "needs-work", label: "Needs work" }
    ],
    ambiguity: [
      { value: "", label: "— No change —" },
      { value: "resolved", label: "Resolved" },
      { value: "deferred", label: "Defer" }
    ],
    risk: [
      { value: "", label: "— No change —" },
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

  // Per-node review state
  const state = {};
  SPEC.nodes.forEach(n => {
    state[n.id] = {
      new_status: "",
      comment: "",
      body_edit: null,
      resolution: null // { choice_id?, freeform? }
    };
  });

  function isTouchedState(s) {
    return Boolean(s.new_status) || Boolean(s.comment.trim()) || s.resolution !== null;
  }

  function recomputeStaleness() {
    SPEC.nodes.forEach(n => {
      const deps = Array.isArray(n.depends_on) ? n.depends_on : [];
      const upstreamTouched = deps.some(d => state[d] && isTouchedState(state[d]));
      const el = document.getElementById("card-" + n.id);
      if (el) el.classList.toggle("upstream-stale", upstreamTouched);
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
      const chips = ids.map(id =>
        `<a class="chip" href="#card-${escapeHtml(id)}">${escapeHtml(id)}</a>`
      ).join("");
      return `<div class="chips"><span class="chip-label">${label}</span>${chips}</div>`;
    }
    head.innerHTML = `
      <span class="badge kind-${escapeHtml(node.kind)}">${escapeHtml(node.kind)}</span>
      <span class="badge status status-${escapeHtml(node.status)}">${escapeHtml(node.status)}</span>
      ${node.kind === "risk" ? `<span class="badge sev-${escapeHtml(node.severity)}">${escapeHtml(node.severity)} severity</span>` : ""}
      <span class="stale-badge" title="An upstream decision has unsubmitted changes — review may need recalibration.">↑ upstream changed</span>
      <span class="id">${escapeHtml(node.id)}</span>
      <h2>${escapeHtml(node.title)}</h2>
      ${chipsRow("depends on", depList)}
      ${chipsRow("affects", affList)}
    `;
    card.appendChild(head);

    // Body
    if (node._body_html) {
      const body = document.createElement("div");
      body.className = "body";
      body.innerHTML = node._body_html;
      card.appendChild(body);
    }

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

    function updateTouched() {
      const s = state[node.id];
      const touched = isTouchedState(s);
      card.classList.toggle("touched", touched);
      if (cardSubmitBtn) cardSubmitBtn.disabled = !touched;
      // Clear stale "ok" status messages once the user starts editing again.
      if (cardStatusEl && cardStatusEl.classList.contains("ok")) {
        cardStatusEl.textContent = "";
        cardStatusEl.classList.remove("ok");
      }
      recomputeFooter();
      recomputeStaleness();
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
          cardStatusEl.textContent = "Submitted (rev " + (data.revision || "?") + ", " + (data.review_count || 1) + " on server)";
          cardStatusEl.classList.add("ok");
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
      // Only auto-flip status to "resolved" when the user hasn't manually set a status
      // AND we have a resolution that would actually be valid at export time.
      const r = state[node.id].resolution;
      const valid = r && (
        (r.choice_id) ||
        (typeof r.freeform === "string" && r.freeform.trim().length > 0)
      );
      if (valid && !state[node.id].new_status) {
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

    return card;
  }

  function recomputeFooter() {
    const touched = Object.values(state).filter(isTouchedState).length;
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
    const hasStatus = Boolean(s.new_status);
    const hasComment = Boolean(s.comment.trim());
    const hasResolution = s.resolution !== null && (
      (s.resolution.choice_id) ||
      (s.resolution.freeform && s.resolution.freeform.trim())
    );
    if (!hasStatus && !hasComment && !hasResolution) return null;
    if (n.kind === "ambiguity" && s.new_status === "resolved" && !hasResolution) return "invalid";
    const entry = { node_id: n.id };
    if (hasStatus) entry.new_status = s.new_status;
    if (hasResolution) {
      const r = {};
      if (s.resolution.choice_id) r.choice_id = s.resolution.choice_id;
      if (s.resolution.freeform && s.resolution.freeform.trim()) {
        r.freeform = s.resolution.freeform.trim();
      }
      entry.resolution = r;
    }
    if (hasComment) entry.comment = s.comment.trim();
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
    const body = JSON.stringify({
      spec_id: SPEC.spec_id,
      spec_version: SPEC.version,
      reviewed_at: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
      reviewer: null,
      reviews: reviewEntries
    });
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

  let SUBMIT = { url: "", token: "" };

  function init() {
    renderCounts();
    const container = document.getElementById("nodes");
    SPEC.nodes.forEach(n => container.appendChild(renderCard(n)));
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

    SUBMIT = JSON.parse(document.getElementById("submit-config").textContent);
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
          copied.textContent = "Submitted (rev " + (data.revision || "?") + ", status " + (data.status || "?") + ")";
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
          flashBanner("Submitted " + built.delta.reviews.length + " review(s); server has " + (data.review_count || "?") + " for rev " + (data.revision || "?") + (built.dropped.length ? ". Dropped " + built.dropped.length + " incomplete." : "."));
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
