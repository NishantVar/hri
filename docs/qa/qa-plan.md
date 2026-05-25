# RIView end-to-end QA plan

Human-driven test plan for the website + daemon + responder loop. Each scenario lists steps, the expected outcome, and whether automated coverage exists in `tests/test_daemon.py`.

Out of scope: anything covered exclusively by the test suite that has no UX surface (e.g. token file permissions). Run `python3 -m pytest tests/` before starting.

## Conventions

- `$REPO` = path to this checkout (the RIView daemon repo).
- `$SAMPLE` = `$REPO/sample` (markdown + decisions fixture used below).
- `<sid>` = the session id printed by `register` / `submit`.
- "Reload" = browser hard reload (Cmd+Shift+R). Plain reload also fine — the server is the source of truth.
- All `curl` examples assume the daemon token is read from `<RIVIEW_HOME>/token`.

## One-time setup

```bash
cd $REPO
rm -rf .riview/                                 # start clean
git status                                      # confirm .riview/ not tracked
python3 -m pytest tests/                        # baseline green
python3 scripts/riview.py daemon &              # 127.0.0.1:7891
DAEMON_PID=$!
sleep 1
python3 scripts/riview.py submit $SAMPLE
# capture <sid> from output; open http://127.0.0.1:7891/sessions/<sid>
```

Teardown: `kill $DAEMON_PID && rm -rf .riview/`.

A "reset to clean revision" mid-run:

```bash
rm -rf .riview/sessions/<sid>/reviews/*/review.json
```

(removes overlay only; spec snapshots stay).

---

## A. Website-only state

### A1 — Submit-then-reload persistence (happy path)

**Steps**
1. Open `/sessions/<sid>`.
2. Pick a decision card. Change status from `ai-confident` → `confirmed`. Type a comment "QA A1".
3. Click Submit on that card. Watch for the success indicator.
4. Hard reload.

**Expected**
- Status select shows `confirmed`. Comment textarea shows "QA A1".
- Card is **untouched** (border = status color, Submit button disabled).
- `cat .riview/sessions/<sid>/reviews/1/review.json` shows the entry with `new_status: confirmed` and `comment: "QA A1"`.

**Auto-coverage:** `test_session_page_renders_overlay_status_and_comment`.

### A2 — Partial resubmit preserves siblings

**Steps**
1. Continuing from A1, edit only the comment ("QA A2"). Don't touch status.
2. Submit.
3. Reload. Inspect `reviews/1/review.json`.

**Expected**
- Comment is now "QA A2"; status entry still `confirmed`.
- Card untouched after reload.

**Auto-coverage:** `test_partial_field_resubmit_preserves_other_overlay_fields`.

### A3 — Clear-only-field eviction (`cleared_fields`)

**Steps**
1. Find a decision card with **only a comment** in the overlay (or run A1 with a no-op status change, then resubmit clearing the status separately so only `comment` remains).
2. Verify in `reviews/1/review.json` the entry is `{node_id, comment: "..."}` (single field beyond `node_id`).
3. Blank the comment textarea. Submit.
4. Reload. Inspect `reviews/1/review.json`.

**Expected**
- The node's entry is **gone** from `review.json`.
- Card shows pure canonical state (no overlay border, no comment).
- The on-disk file does **not** contain `cleared_fields` (it was stripped before persist).

**Auto-coverage:** `test_clear_comment_only_overlay_evicts_node_via_cleared_fields`.

### A4 — Partial clear preserves other overlay fields

**Steps**
1. Set up a card with overlay `{new_status: confirmed, comment: "keep status"}`.
2. Blank only the comment; keep status submitted as `confirmed`. Submit.
3. Reload. Inspect `reviews/1/review.json`.

**Expected**
- Entry is now `{node_id, new_status: confirmed}`. No `comment` field.
- Card pre-fills status `confirmed`, comment empty, untouched.

**Auto-coverage:** `test_cleared_fields_with_other_fields_drops_only_listed`.

### A5 — Ambiguity resolution lifecycle

**Steps**
1. Find an ambiguity card. Select a resolution (e.g. `resolved`), pick an option, add a comment "A5-first". Submit.
2. Reload. Confirm pre-fill + untouched.
3. Change resolution to `deferred`. Submit.
4. Reload again.
5. Now blank the comment and change status back to `open` (which should also drop resolution). Submit. Reload.

**Expected after step 2:** overlay entry has `new_status: resolved`, the chosen resolution, comment "A5-first". Card untouched.
**Expected after step 4:** resolution updated; status/comment preserved.
**Expected after step 5:** card returns to canonical; entry gone from `review.json`. The page's resolution selector reverts to canonical (the `advanceBaselineFromEntry` path clears `applied.resolution` when overlay omits it).

**Auto-coverage:** None directly. **TODO**: add `test_ambiguity_resolution_overlay_lifecycle` covering submit → resubmit → clear via `cleared_fields: ["resolution", "comment"]`.

### A6 — Body edit overlay

**Steps**
1. Click into a node's body editor; edit the prose. Submit the card.
2. Reload. Inspect overlay JSON island in page source (`<script id="overlay-entries">`).
3. Edit the body again, then blank only the body (revert to canonical). Submit. Reload.

**Expected after step 2:** body shows edited content; "edited" marker visible; overlay entry includes `body_edit`.
**Expected after step 3:** body returns to canonical; `body_edit` removed from entry. If body_edit was the only overlay field, the node is evicted entirely.

**Auto-coverage:** Indirect (overlay merge tested with body_edit in `_apply_overlay_to_spec`). **TODO**: add `test_body_edit_overlay_clears_via_cleared_fields`.

### A7 — Submit-all with mixed cards

**Steps**
1. Touch three cards of different kinds (one decision, one ambiguity, one risk). Different changes on each.
2. Click the global Submit-all button.
3. Watch the response handler. Reload.

**Expected**
- All three persist (entries in `review.json`).
- If the responder happens to roll forward mid-submit, conflict response surfaces per-card (see C4) — non-conflicting cards still merged.
- After reload, every card pre-fills + untouched.

**Auto-coverage:** None at the HTTP layer for submit-all. **TODO**: add `test_review_post_batch_mixed_entries_accepted` (the daemon endpoint is already batch-capable; we just don't exercise it in a test).

### A8 — Draft persistence (localStorage)

**Steps**
1. Type "draft-A8" into a card's comment. **Do not submit.**
2. Reload.
3. Inspect localStorage in devtools: key `riview:draft:<sid>:1` should exist.
4. Submit the card (with the restored draft).
5. Reload.

**Expected after step 2:** comment textarea contains "draft-A8"; card shows as **touched**; Submit enabled.
**Expected after step 5:** draft key for this node is gone from localStorage; card untouched (overlay took over).

**Auto-coverage:** Wiring tested by `test_session_page_includes_draft_persistence_keys`. Roundtrip is browser-only.

### A9 — Draft retention window

**Steps**
1. Type a draft against revision 1.
2. Cause the session to advance to revision 6+ (six responder rolls, or six fake submits in a script).
3. Reload at the latest revision and inspect localStorage.

**Expected**
- Only keys for the latest five revisions remain. Revision 1's draft key is evicted.

**Auto-coverage:** Client-only. Manual check sufficient.

---

## B. Storage / repo-local invariants

### B1 — Default storage root is `<repo>/.riview/`

**Steps**
1. Fresh terminal, no `RIVIEW_HOME` set.
2. `python3 scripts/riview.py daemon &` then `submit` a spec.
3. `ls $REPO/.riview/sessions/` and `git status`.

**Expected**
- Session dir lives under `$REPO/.riview/sessions/`.
- `git status` lists nothing under `.riview/` (gitignored).

**Auto-coverage:** `RiviewStorageRootTests::test_default_is_repo_local`.

### B2 — `RIVIEW_HOME` override

**Steps**
1. `RIVIEW_HOME=/tmp/riview-qa python3 scripts/riview.py daemon &`
2. `RIVIEW_HOME=/tmp/riview-qa python3 scripts/riview.py submit $SAMPLE`
3. Check `/tmp/riview-qa/sessions/` and `$REPO/.riview/`.

**Expected**
- Session lands in `/tmp/riview-qa/`. Repo's `.riview/` is empty or absent.

**Auto-coverage:** `RiviewStorageRootTests::test_env_override_wins`.

---

## C. Daemon ↔ Responder (the dynamic to-and-fro)

These scenarios require a responder loop. For QA, simulate one with a second terminal that re-submits the spec (advancing revision) or directly writes the next revision via `submit --session <sid>`. Real responder usage is via the `riview-respond` Claude Code skill (see `agents/riview-respond.md`).

### C1 — Fresh revision shows empty overlay

**Steps**
1. From the A1 state (revision 1 has an overlay), the responder writes a new revision:
   ```bash
   # Edit $SAMPLE/spec.md to change something canonical (e.g. tweak one decision's body).
   python3 scripts/riview.py submit $SAMPLE --session <sid>
   ```
2. Reload the browser tab on `/sessions/<sid>`.

**Expected**
- Page now shows revision 2 (check `<script id="submit-config">` for `base_revision: 2`).
- All cards canonical: no overlay borders, comments empty, statuses match the new spec.
- No `reviews/2/review.json` exists.

**Auto-coverage:** Partial — `test_session_page_with_no_overlay_renders_empty_overlay_comments`.

### C2 — Submit while responder watches `/wait`

**Steps**
1. In a second terminal: `python3 scripts/riview.py wait <sid>` (or curl `/sessions/<sid>/wait?cursor=N`).
2. In the browser, edit and submit a card on revision 2.
3. Watch the `wait` call return.

**Expected**
- The `wait` call returns 200 with the new event cursor as soon as the POST completes.
- The responder (or its simulated stand-in) can then `pull` the latest review and apply it.

**Auto-coverage:** `test_wait_wakes_on_review_post`, `test_cli_wait_unblocks_on_review_post`.

### C3 — Stale base, no node change (clean accept)

**Steps**
1. On revision 2, open the page (base_revision=2). Edit a comment, but **do not submit yet**.
2. In another terminal, responder rolls forward to revision 3 **without changing the node you're editing**:
   ```bash
   # Edit a different node's body in $SAMPLE/spec.md.
   python3 scripts/riview.py submit $SAMPLE --session <sid>
   ```
3. Submit your card (still on the stale base_revision=2 page).
4. Reload.

**Expected**
- Submit response: `accepted: [{node_id: <yours>}]`, `conflicts: []`, `current_revision: 3`.
- After reload, the page is now on revision 3 with the overlay merged in.

**Auto-coverage:** `test_stale_submit_accepts_unchanged_node_conflicts_changed_node`, `test_stale_submit_with_base_equal_current_is_clean_accept`.

### C4 — Stale base, conflicting node change

**Steps**
1. On revision 3, open the page, edit a card's comment ("C4 pre-conflict"). Don't submit.
2. Responder rolls forward to revision 4 **changing the same node's body**.
3. Submit on the stale page.

**Expected**
- Submit response: `conflicts: [{node_id, reason: "fingerprint_mismatch", base_revision: 3, current_revision: 4}]`, `accepted: []`.
- UI surfaces the conflict (the card stays touched; user has a path to reload).
- No entry merged into `reviews/4/review.json` for that node.

**Auto-coverage:** `test_stale_submit_accepts_unchanged_node_conflicts_changed_node`, `test_stale_submit_body_edit_detected_as_conflict`.

### C5 — Multi-revision overlay isolation

**Steps**
1. Submit a card on revision 4 (overlay lands in `reviews/4/review.json`).
2. Responder rolls forward to revision 5.
3. Inspect `reviews/4/review.json` and `reviews/5/review.json`.

**Expected**
- `reviews/4/review.json` still holds the revision-4 overlay (history preserved).
- `reviews/5/review.json` does **not** exist — overlay does not bleed forward.
- Page on revision 5 renders canonical (no overlay).

**Auto-coverage:** Partial (revision indexing is well-tested at the storage layer). **TODO**: add `test_overlay_isolated_per_revision` asserting the two files are independent.

### C6 — `apply.py` never sees `cleared_fields`

**Steps**
1. Submit a card with `cleared_fields` (any A3/A4 scenario).
2. Cat the resulting `reviews/N/review.json`.
3. Run `apply.py` manually against that file + spec snapshot. `apply.py` rewrites the spec **in place**, so copy first for QA:
   ```bash
   cp -r $SAMPLE /tmp/qa-apply-out
   python3 scripts/apply.py /tmp/qa-apply-out \
     .riview/sessions/<sid>/reviews/N/review.json \
     --basename spec --dry-run
   ```

**Expected**
- `review.json` contains no `cleared_fields` key on any entry (stripped pre-persist).
- `apply.py` runs to completion. Output spec matches expectations (cleared comments simply absent).

**Auto-coverage:** None directly. **TODO**: add `test_persisted_review_strips_cleared_fields_marker`.

### C7 — Responder pulls applied review, advances cleanly

**Steps**
1. Submit a multi-card review on the current revision.
2. Simulate the responder loop:
   ```bash
   python3 scripts/riview.py pull <sid> > /tmp/qa-pull.json
   diff /tmp/qa-pull.json .riview/sessions/<sid>/reviews/<current>/review.json
   # Apply in place, then submit next revision (apply.py mutates $SAMPLE):
   python3 scripts/apply.py $SAMPLE /tmp/qa-pull.json --basename spec
   python3 scripts/riview.py submit $SAMPLE --session <sid>
   ```
3. Reload browser.

**Expected**
- `pull` output equals `review.json` byte-for-byte.
- `apply.py` writes new spec content reflecting confirmed statuses, etc.
- New revision page renders the applied state as canonical; previous overlay no longer needed.

**Auto-coverage:** Apply path covered by existing apply tests; full responder loop is integration-only.

---

## D. Validation & error paths

### D1 — Malformed `cleared_fields` → 400

**Steps**
```bash
TOKEN=$(cat .riview/token)
curl -i -X POST http://127.0.0.1:7891/sessions/<sid>/review \
  -H "X-Riview-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"spec_id":"<id>","spec_version":1,"base_revision":1,
       "reviews":[{"node_id":"some-id","cleared_fields":"comment"}]}'
```
Repeat with `cleared_fields: ["unknown_field"]`, `cleared_fields: [123]`, `cleared_fields: null`.

**Expected**
- All four return HTTP 400 with a clear error message. No mutation on disk.

**Auto-coverage:** `test_cleared_fields_rejects_malformed_values`.

### D2 — Empty entry silently filtered

**Steps**
```bash
curl -i -X POST http://127.0.0.1:7891/sessions/<sid>/review \
  -H "X-Riview-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"spec_id":"<id>","spec_version":1,"base_revision":1,
       "reviews":[{"node_id":"some-id"}]}'
```

**Expected**
- 200, `accepted: []`, no merge.

**Auto-coverage:** `test_review_post_drops_empty_entries_silently`.

### D3 — Unknown node_id

**Steps** — same as D2 but with `node_id: "does-not-exist"`.

**Expected**
- 400 (or whatever the existing test asserts).

**Auto-coverage:** `test_review_post_rejects_unknown_node_id`.

### D4 — Bad token / missing token

**Steps**
- POST with no `X-Riview-Token` header.
- POST with `X-Riview-Token: wrong`.

**Expected**
- 401 / 403 (per existing tests). No mutation.

**Auto-coverage:** `test_review_post_requires_token`, `test_review_post_bad_token`.

---

## E. Cross-cutting

### E1 — Overlay does not bleed into `node.review.comment`

**Steps**
1. Run any A scenario that submits comments.
2. `cat .riview/sessions/<sid>/revisions/<N>/decisions.json | jq '.nodes[].review'`.

**Expected**
- Every node's `review.comment` is empty / absent. Comments live in `reviews/<N>/review.json` only, never in the per-revision sidecar.

**Auto-coverage:** `test_overlay_does_not_persist_into_node_review_comment`.

### E2 — Color-coding tracks status

**Steps**
1. Cycle one decision card through `ai-confident` → `confirmed` → `rejected` → `needs-work`, submitting + reloading between each.
2. Confirm card border/background color shifts each time.
3. Repeat for an ambiguity (`open` / `resolved` / `deferred`) and a risk (`open` / `accepted` / `mitigated` / `dismissed`).

**Expected**
- Color matches the status enum at every reload.
- The "upstream changed" badge stays quiet on pure approvals (per `dc8e1f0`).

**Auto-coverage:** None (CSS-only). Manual visual check.

### E3 — Refresh during pending submit

**Steps**
1. Throttle network in devtools (e.g. 3G).
2. Edit a card. Click Submit. Before the response arrives, hard-reload.
3. Wait for everything to settle. Inspect `review.json` and the UI.

**Expected**
- Either the submit landed (overlay reflects it on reload) or it didn't (draft restored from localStorage, card touched). **Never** both partially applied.
- No JS errors in console; no orphaned in-flight state.

**Auto-coverage:** None (client-only race). Manual check.

### E4 — Two browser tabs on the same session

**Steps**
1. Open `/sessions/<sid>` in tab A and tab B (same revision).
2. In tab A, submit a card.
3. In tab B (no reload), submit a *different* card.
4. Reload both.

**Expected**
- Both submissions persist (they targeted different nodes; ADR-0005 by-node-id merge).
- Both tabs after reload show both overlay entries, untouched.

**Auto-coverage:** None directly. Single-tab semantics covered.

### E5 — Two tabs editing the **same** card

**Steps**
1. Open `/sessions/<sid>` in tabs A and B.
2. In A, submit a comment "tab-A".
3. In B (still showing pre-submit state for that card), submit "tab-B" without reloading first.

**Expected**
- B's submit succeeds (same `base_revision`, fingerprint matches because the canonical node hasn't changed). The overlay entry ends up with `comment: "tab-B"` (last-write-wins by node_id).
- A reload of A shows "tab-B". This is documented behavior, not a bug.

**Auto-coverage:** None. **TODO** if we want it: `test_same_node_resubmit_last_write_wins`.

---

## Closing checklist

After running through the plan:

- [ ] All A scenarios pass.
- [ ] B1 and B2 pass.
- [ ] All C scenarios pass (use the simulated responder steps above; or run an actual `riview-respond` agent against the session for a higher-fidelity end-to-end).
- [ ] All D scenarios return the right status codes; no mutation on rejections.
- [ ] E1–E5 — note any surprises; the file-watch surprises are usually browser-side.
- [ ] `python3 -m pytest tests/` still green after all runs.
- [ ] `git status` shows only intentional changes (no stray `.riview/` content, no debugging files).

## Tests to add (summary)

Picked up from the "Auto-coverage" notes:

- `test_ambiguity_resolution_overlay_lifecycle` — A5.
- `test_body_edit_overlay_clears_via_cleared_fields` — A6.
- `test_review_post_batch_mixed_entries_accepted` — A7.
- `test_overlay_isolated_per_revision` — C5.
- `test_persisted_review_strips_cleared_fields_marker` — C6.
- `test_same_node_resubmit_last_write_wins` (optional) — E5.
