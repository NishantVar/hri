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


def validate(spec: dict, anchor_counts: dict[str, int]) -> list[str]:
    errors: list[str] = []
    seen_ids: set[str] = set()
    declared_anchors: set[str] = set()
    for node in spec["nodes"]:
        nid = node["id"]
        if nid in seen_ids:
            errors.append(f"duplicate node id: {nid}")
        seen_ids.add(nid)
        anchor = node.get("source_anchor", nid)
        declared_anchors.add(anchor)
        count = anchor_counts.get(anchor, 0)
        if count == 0:
            errors.append(f"node {nid}: anchor {anchor!r} missing in source markdown")
        elif count > 1:
            errors.append(
                f"node {nid}: anchor {anchor!r} occurs {count} times in source markdown (must be unique)"
            )
        if node["kind"] not in {"decision", "ambiguity", "risk"}:
            errors.append(f"node {nid}: unknown kind {node['kind']!r}")
    # Anchors in the markdown that don't correspond to any declared node — useful warning.
    for anchor in anchor_counts:
        if anchor not in declared_anchors:
            errors.append(
                f"source markdown contains anchor {anchor!r} but no node in the decisions sidecar declares it"
            )
    return errors


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
    # Attach rendered body markdown to each node for the JS-free fallback view.
    enriched_nodes = []
    for node in spec["nodes"]:
        n = dict(node)
        body_md = bodies.get(node["source_anchor"], "")
        n["_body_html"] = md_to_html(body_md)
        n["_body_md"] = body_md
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
    card.dataset.nodeId = node.id;
    card.dataset.kind = node.kind;
    card.dataset.searchText = (node.id + " " + node.title + " " + node.kind).toLowerCase();

    // Head
    const head = document.createElement("div");
    head.className = "head";
    head.innerHTML = `
      <span class="badge kind-${node.kind}">${node.kind}</span>
      <span class="badge status status-${node.status}">${node.status}</span>
      ${node.kind === "risk" ? `<span class="badge sev-${node.severity}">${node.severity} severity</span>` : ""}
      <span class="id">${escapeHtml(node.id)}</span>
      <h2>${escapeHtml(node.title)}</h2>
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
                <input type="radio" name="opt-${node.id}" value="${escapeHtml(opt.id)}">
                <strong>${escapeHtml(opt.label)}</strong>
                ${opt.body ? `<div class="opt-body">${escapeHtml(opt.body)}</div>` : ""}
              </label>
            </li>
          `).join("")}
          <li>
            <label>
              <input type="radio" name="opt-${node.id}" value="__freeform__">
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

    review.innerHTML = html;
    card.appendChild(review);

    // Wire events
    const statusSel = review.querySelector('[data-role="status"]');
    const commentTa = review.querySelector('[data-role="comment"]');
    const radios = review.querySelectorAll('input[type="radio"]');
    const freeformTa = review.querySelector('[data-role="freeform"]');

    function updateTouched() {
      const s = state[node.id];
      const isTouched = Boolean(s.new_status) || Boolean(s.comment.trim()) || s.resolution !== null;
      card.classList.toggle("touched", isTouched);
      recomputeFooter();
    }

    statusSel.addEventListener("change", () => {
      state[node.id].new_status = statusSel.value;
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
            freeformTa.focus();
            const text = (freeformTa.value || "").trim();
            state[node.id].resolution = text ? { freeform: freeformTa.value } : null;
          } else {
            state[node.id].resolution = null;
          }
        } else {
          if (freeformTa) freeformTa.style.display = "none";
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
        maybeAutoResolve();
        updateTouched();
      });
    }

    return card;
  }

  function recomputeFooter() {
    const touched = Object.values(state).filter(s =>
      Boolean(s.new_status) || Boolean(s.comment.trim()) || s.resolution !== null
    ).length;
    document.getElementById("touched-count").textContent = touched;
    document.getElementById("export-btn").disabled = touched === 0;
  }

  function buildDelta() {
    const reviews = [];
    const dropped = [];
    SPEC.nodes.forEach(n => {
      const s = state[n.id];
      const hasStatus = Boolean(s.new_status);
      const hasComment = Boolean(s.comment.trim());
      const hasResolution = s.resolution !== null && (
        (s.resolution.choice_id) ||
        (s.resolution.freeform && s.resolution.freeform.trim())
      );
      if (!hasStatus && !hasComment && !hasResolution) return;

      // Safety net: never emit an ambiguity entry whose status=resolved but resolution is missing/invalid.
      if (n.kind === "ambiguity" && s.new_status === "resolved" && !hasResolution) {
        dropped.push(n.id);
        return;
      }

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
      reviews.push(entry);
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

    const SUBMIT = JSON.parse(document.getElementById("submit-config").textContent);
    const submitBtn = document.getElementById("submit-server-btn");
    if (SUBMIT.url) {
      submitBtn.hidden = false;
      submitBtn.addEventListener("click", async () => {
        submitBtn.disabled = true;
        copied.textContent = "Submitting...";
        try {
          const res = await fetch(SUBMIT.url, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Riview-Token": SUBMIT.token,
            },
            body: output.value,
          });
          if (!res.ok) {
            const txt = await res.text();
            copied.textContent = "Submit failed (" + res.status + "): " + txt.slice(0, 200);
          } else {
            const data = await res.json().catch(() => ({}));
            copied.textContent = "Submitted (rev " + (data.revision || "?") + ", status " + (data.status || "?") + ")";
          }
        } catch (err) {
          copied.textContent = "Submit error: " + err;
        } finally {
          submitBtn.disabled = false;
        }
      });
    }

    document.getElementById("reset-btn").addEventListener("click", () => {
      if (!confirm("Clear all review inputs on this page?")) return;
      location.reload();
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


def resolve_paths(spec_dir: Path, basename: str, rev: int | None, latest: bool) -> tuple[Path, Path, int]:
    """Return (md_path, json_path, rev_n). rev_n is 0 for base, N for revN."""
    if rev is not None and rev < 0:
        raise ValueError("--rev must be >= 0")
    if rev is not None and latest:
        raise ValueError("--rev and --latest are mutually exclusive")
    base_md = spec_dir / f"{basename}.md"
    base_json = spec_dir / f"{basename}.decisions.json"
    if rev is None and not latest:
        return base_md, base_json, 0
    if rev is not None:
        if rev == 0:
            return base_md, base_json, 0
        md = spec_dir / f"{basename}.rev{rev}.md"
        js = spec_dir / f"{basename}.rev{rev}.decisions.json"
        if not md.exists() or not js.exists():
            raise FileNotFoundError(f"rev {rev} files for basename {basename!r} not found in {spec_dir}")
        return md, js, rev
    # --latest
    rev_re = re.compile(rf"^{re.escape(basename)}\.rev(\d+)\.decisions\.json$")
    latest_n = 0
    for child in spec_dir.iterdir():
        m = rev_re.match(child.name)
        if m:
            latest_n = max(latest_n, int(m.group(1)))
    if latest_n == 0:
        return base_md, base_json, 0
    return (
        spec_dir / f"{basename}.rev{latest_n}.md",
        spec_dir / f"{basename}.rev{latest_n}.decisions.json",
        latest_n,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec_dir", type=Path, help="Folder containing <basename>.md and <basename>.decisions.json")
    parser.add_argument("--basename", default="spec",
                        help="Spec file basename (default: spec). Use 'mvp' to target mvp.md + mvp.decisions.json.")
    parser.add_argument("--rev", type=int, default=None,
                        help="Render a specific revision (<basename>.rev<N>.{md,decisions.json}). 0 means base.")
    parser.add_argument("--latest", action="store_true",
                        help="Render the highest existing revision (or base if no revs exist).")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Output HTML path. Default: <spec-dir>/<basename>.html (base) or <basename>.rev<N>.html.")
    args = parser.parse_args(argv)

    try:
        md_path, json_path, rev_n = resolve_paths(args.spec_dir, args.basename, args.rev, args.latest)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

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
    if args.output:
        out_path = args.output
    else:
        out_path = args.spec_dir / (
            f"{args.basename}.rev{rev_n}.html" if rev_n else f"{args.basename}.html"
        )
    out_path.write_text(html_out)
    rev_label = f"rev{rev_n}" if rev_n else "base"
    print(f"wrote {out_path} ({len(html_out)} bytes, {len(spec['nodes'])} nodes, {rev_label})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
