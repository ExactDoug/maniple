# Maniple MCP Server — Code Safety & Robustness Review

**Date:** 2026-05-28
**Reviewer:** Claude Code `/investigate` (6 parallel read-only sub-agents, synthesized)
**Scope:** `src/maniple_mcp/` — correctness & safety (NOT security; see `codex-security-review-2026-05-28.md` for the security pass)
**Method:** Six read-only investigation agents, each on a distinct angle: error handling, concurrency/races, resource leaks, data-loss, input validation/parsing, and tmux↔iTerm backend parity.

> Note: the package lives at `src/maniple_mcp/`. The repo `CLAUDE.md` references `src/claude_team_mcp/`, which is stale — worth correcting separately.

---

## Executive Summary

The codebase is generally well-structured, with good defensive patterns in several places (context-managed file I/O, bounded subprocess cache, branch preservation on worktree removal, frozen dataclasses for immutable types, graceful worktree fallback on spawn). However, the review surfaced a coherent cluster of **correctness and safety risks concentrated in three areas**:

1. **No timeouts anywhere on subprocess / iTerm2 / tmux calls** — the single most pervasive issue. A hung `git`, `tmux`, or iTerm2 API call blocks the entire spawn/close/list pipeline indefinitely with no recovery. Flagged independently by the error-handling, resource-leak, and concurrency agents.
2. **No cleanup/rollback on partial `spawn_workers` failure** — panes, worktrees, and registry entries created mid-spawn are orphaned when a later step fails. Resources and disk accumulate; the registry fills with dead entries.
3. **Silent data loss on worktree removal** — `remove_worktree()` defaults to `force=True`, and `list_worktrees(remove_orphans=True)` uses `shutil.rmtree()`, both discarding uncommitted/unpushed worker changes with no check or warning.

Secondary themes: unsynchronized shared registry state under concurrent async tool calls, fragile JSONL boundary parsing (truncated reads, missing `encoding="utf-8"`), marker-extraction edge cases, and backend parity gaps (window/profile features iTerm-only; iTerm backend under-tested).

### Severity Roll-up

| Severity | Count | Headline items |
|----------|-------|----------------|
| Critical | 5 | No subprocess timeouts; no spawn rollback (panes + worktrees); force worktree removal discards uncommitted work; truncated-JSON idle-detection miss |
| High | 8 | Unsync registry mutation; TOCTOU file checks; orphan `rmtree` w/o content check; no uncommitted-check before close; registry fills with dead sessions; recovered sessions never auto-pruned; name/slug resolution ambiguity; iTerm window/profile parity gaps |
| Medium | ~12 | Generic exception masking; marker parse collisions; unbounded JSONL scans; path-resolution discrepancy on removal; encoding assumptions; layout config fallthrough; iTerm test coverage |
| Low/Info | several | Logging gaps (bare `pass`); name-pool dup risk; positive patterns noted |

---

## Critical Findings

### C1. No timeouts on any subprocess / iTerm2 / tmux call
**Area:** Error handling, resource leaks, concurrency
**Files:**
- `worktree.py:216, 232, 360, 396, 442, 451` — every `subprocess.run()` lacks `timeout=`
- `terminal_backends/tmux.py:558` — `_run_tmux()` via `asyncio.to_thread()` with no timeout
- `iterm_utils.py` (throughout) — `send_text/send_prompt/send_key/read_screen` wrap iTerm2 async APIs with no `asyncio.wait_for`

**Impact:** A hung `git worktree add` (network/SSH/corrupt repo), wedged tmux pane, or unresponsive iTerm2 blocks the entire `spawn_workers`/`close_workers`/`list_*` call indefinitely. No caller can recover without killing the process; in-flight resources are left in limbo (compounds C2/C3).
**Remediation:** Add `timeout=30` (configurable) to all `subprocess.run()`; wrap tmux `to_thread` in `asyncio.wait_for(..., timeout=30)`; wrap iTerm2 async ops in `asyncio.wait_for(..., timeout≈5)`. Convert `TimeoutExpired`/`asyncio.TimeoutError` into actionable errors.

### C2. `spawn_workers` does not roll back panes/worktrees on partial failure
**Area:** Resource leaks
**Files:** `tools/spawn_workers.py:258–873` (pane creation `426–655`, worktree creation `343–372`, outer catch `868`)
**Impact:** If any step after pane/worktree creation fails (marker-poll timeout `741–774`, agent startup `697`, prompt send `734–816`), the created iTerm tabs / tmux windows and the `.worktrees/<worker>` dirs + branches are never cleaned up. Repeated failed spawns accumulate orphaned windows, disk, and dangling branches.
**Remediation:** Track created panes/worktrees and clean them up in a `try/finally` (or context-manager) on the failure path; only commit to registry after success, or roll back the registry entry too (see H5).

### C3. Worktree removal force-discards uncommitted work
**Area:** Data loss
**Files:** `worktree.py:404–457` (`remove_worktree(force=True)` default, `git worktree remove --force` at `438`); called from `tools/close_workers.py:98–101`
**Impact:** Closing a worker (even an idle/READY one) silently erases any uncommitted/unstaged changes in its worktree. No stash, no warning, no chance to salvage. Branch is retained but working-tree changes are gone.
**Remediation:** Default `force=False`; before removal run `git status --porcelain` on the worktree and refuse (or warn + skip) when dirty; only `--force` after explicit confirmation. Consider a "trash" move instead of hard delete (see M-data).

### C4. Orphan worktree removal uses `shutil.rmtree()` with no content check
**Area:** Data loss
**Files:** `tools/list_worktrees.py:88–98`
**Impact:** `list_worktrees(remove_orphans=True)` deletes any unregistered `.worktrees` directory via `shutil.rmtree()` without verifying it's actually a git worktree or checking for uncommitted/unpushed work. A worker that crashed (leaving a live worktree git no longer tracks) loses everything.
**Remediation:** Verify `<dir>/.git` exists and `git status --porcelain` is clean before removal; add a dry-run/confirm path; log path + last commit before deleting; prefer trash-move over `rmtree`.

### C5. Truncated JSON at read boundary breaks Codex idle detection
**Area:** Input validation / fragile parsing
**Files:** `idle_detection.py:254–260` (tail read + `split(b"\n")`); related first-buffer read `152–200`
**Impact:** The tail-read seeks to `size - read_size` and parses lines; the first (partial) line is malformed JSON and is silently dropped. If the completion/`ThreadStarted` marker lands in that boundary slice, idle detection returns "still working" forever → worker appears hung; downstream waits hit timeout.
**Remediation:** After seek, discard the first partial line (`readline()`) before parsing the remainder; or read a larger buffer / whole file when small.

---

## High Findings

### H1. Shared registry state mutated without synchronization
**Area:** Concurrency
**Files:** `registry.py:222–290` (`ManagedSession` is **not** frozen, unlike its siblings), `288–290` `update_activity()`, direct field writes `312, 338`; callers `check_idle_workers.py:85`, `message_workers.py:223,250,345,350`, `wait_idle_workers.py:144`
**Impact:** Concurrent async tool invocations mutate the same `ManagedSession`/registry with no lock — inconsistent status, stale cached `claude_session_id`/`codex_jsonl_path`, wrong idle decisions.
**Remediation:** Freeze `ManagedSession` and transition via `dataclasses.replace()`, or guard mutations with an `asyncio.Lock` (per-session or registry-wide).

### H2. Global registry singleton init is not atomic + dict-iteration races
**Area:** Concurrency
**Files:** `server.py:36–48` (check-then-create singleton); `registry.py:558, 579` (`list_all()`/`list_by_status()` iterate `_recovered_sessions`) vs `708–735` (`recover_from_events()` mutates it)
**Impact:** Two concurrent first-callers can build two registries (lost state). Iterating `_recovered_sessions` while recovery appends → `RuntimeError: dictionary changed size during iteration`.
**Remediation:** Lazily init the registry in the lifespan/`AppContext` (already partly present) or guard with a lock; snapshot dicts (`list(d.items())`) before iterating/mutating.

### H3. TOCTOU on JSONL existence checks
**Area:** Concurrency / parsing
**Files:** `idle_detection.py:221–223, 244–254`; `session_state.py:425–437`
**Impact:** `exists()` → `open()`/`stat()` windows let log rotation/archival/truncation slip in between, raising `FileNotFoundError` or yielding stale reads during concurrent cleanup.
**Remediation:** Drop the pre-check; wrap reads in try/except (some sites already do at `438`); treat missing-file distinctly from "still working" so idle polling doesn't false-timeout (see also the polling/rotation race).

### H4. Registry fills with dead sessions on failed spawn
**Area:** Resource leaks
**Files:** `tools/spawn_workers.py:699–716`; `registry.py:456–485`
**Impact:** Sessions are `registry.add()`-ed before agent startup/marker correlation succeed. On later failure they remain pointing at dead/partial panes; `list_workers()` reports phantoms; registry grows unbounded under repeated failures.
**Remediation:** Add to registry only after successful startup, or roll back on failure (ties to C2).

### H5. Recovered sessions never auto-pruned
**Area:** Resource leaks
**Files:** `registry.py:618–742`; `tools/prune_recovered_workers.py`
**Impact:** `recover_from_events()` accumulates `_recovered_sessions` that are only cleared when the user manually calls `prune_recovered_workers`. Stale/closed recovered entries persist across restarts and show as active.
**Remediation:** Call `prune_stale_recovered_sessions()` at server startup and/or add TTL-based auto-pruning.

### H6. No uncommitted-change check before `close_workers`
**Area:** Data loss
**Files:** `tools/close_workers.py:45–126` (busy-check `69–75`, removal `98`)
**Impact:** The `force` flag only bypasses the BUSY check, not a (nonexistent) dirty-worktree check. A READY worker with uncommitted work is wiped with no signal.
**Remediation:** Always run a `git status` check before worktree removal, independent of `force`; return the dirty file list so the coordinator can act.

### H7. Session name/ID resolution ambiguity & case sensitivity
**Area:** Input validation
**Files:** `registry.py:499–543` (`get_by_name` `510`, `resolve` `514–543`)
**Impact:** Duplicate worker names → `get_by_name()` returns only the first; the rest are unreachable. Name match is case-sensitive; terminal-ID compare (`539`) is case-sensitive though some backends vary casing → mis-routed messages.
**Remediation:** Enforce unique names at spawn; case-insensitive name fallback; normalize terminal IDs.

### H8. Backend parity gaps: window/app + profile features iTerm-only; iTerm under-tested
**Area:** Backend parity (project policy mandates tmux+iTerm parity)
**Files:** iTerm-only methods `terminal_backends/iterm.py:240–253` (`activate_app`, `activate_window_for_handle`, `get_window_for_handle`, `find_handle_by_native_id`, `list_handles`) used in `tools/spawn_workers.py:465–480, 825–829`; tmux rejects profiles `terminal_backends/tmux.py:145–146, 264–265, 308–309`; tests `tests/test_tmux_backend.py` (8 tests) vs `tests/test_iterm_utils.py` (~5, no layout/split/send_prompt/list_sessions coverage)
**Impact:** "Coordinator window reuse" and pane appearance/customization are iTerm-only; tmux users silently get different behavior. iTerm backend's core methods (multi-pane layout, prompt delivery, split) are untested, so regressions there go uncaught.
**Remediation:** Either implement tmux equivalents (tmux window discovery from a pane ID) or document the asymmetry per the parity policy with a follow-up issue; add iTerm backend tests to match tmux coverage.
**Good parity observed:** Enter-key semantics (`C-m` vs `\x0d`), identical paste-delay formula, `CODEX_PRE_ENTER_DELAY=0.5`, consistent 10s/15s timeouts, clean session-ID abstraction (`backend_id`/`native_id`), robust backend-selection logic.

---

## Medium Findings

| ID | Title | File:line | Issue | Fix |
|----|-------|-----------|-------|-----|
| M1 | Generic `except` masks specific spawn failures | `tools/spawn_workers.py:868–873` | All errors collapse to one "iTerm connection" hint; a hung-git timeout reads as a connection error | Catch `WorktreeError`/`RuntimeError`/etc. separately with targeted hints |
| M2 | Swallowed exceptions w/o logging | `iterm_utils.py:430`; `terminal_backends/tmux.py:169,356,406`; `terminal_backends/iterm.py:106–109`; `idle_detection.py:136,147`; `poll_worker_changes.py:93–103` | Bare `pass`/empty catch hides root cause (iTerm wedge, tmux stderr, bad duration field) | Log at DEBUG/WARNING with the failing command/exception |
| M3 | Marker discovery TOCTOU caches stale session id | `registry.py:292–313`; `session_state.py:390–441` | Caches a stem whose file may already be archived | Return + validate full path before caching |
| M4 | Idle polling vs file rotation → false timeout | `idle_detection.py:350–366,405–425,454–501` | Missing JSONL read as "still working" across polls | Distinguish missing-file from working; return `(idle, exists)` |
| M5 | Unbounded JSONL/codex directory scans | `session_state.py:642–739,742–838,475–515` | Globs all projects/sessions on every marker poll | Cap projects/files scanned; index by terminal/session id |
| M6 | Path-resolution discrepancy on removal | `worktree_detection.py:38–101`; `close_workers.py:96–101` | Stale/symlink-shifted absolute paths could target wrong dir | Confirm worktree is git-registered and under `<repo>/.worktrees` before removal |
| M7 | Missing JSONL field validation | `session_state.py:992–1054` | `get("uuid","")`/`get("role","")` accept malformed entries | Skip entries missing required fields, log warning |
| M8 | Empty/rotated file not guarded in `parse_codex_session` | `session_state.py:1088` (vs guarded `idle_detection.py:249`) | Returns empty state silently during rotation | Size/exist check before parse |
| M9 | Marker suffix/prefix collisions | `session_state.py:1297–1316, 294–319` | `]` in value or multi-marker text extracts wrong id | Escape/validate values; extract per-prefix then validate format |
| M10 | No `encoding="utf-8"` on JSONL opens | `session_state.py:992,1088,1338,1429`; `idle_detection.py:174` | Platform-default encoding drops emoji/non-ASCII (esp. Windows cp1252) | Pass `encoding="utf-8"` everywhere |
| M11 | Layout config fallthrough | `tools/spawn_workers.py:238–240,438,626` | Invalid configured layout silently treated as "new" | Validate against allowed set; raise on invalid |
| M12 | Tool input not validated (issue_id/badge/prompt) | `tools/spawn_workers.py:305–312` | Free-form values flow into init/tracker with no format/length checks | Validate `issue_id` format, bound/sanitize badge & prompt |

---

## Low / Info

- **L1** `idle_detection.py:136,147`, others — bare `pass` obscures intent; replace with explanatory debug logs.
- **L2** `names.py:331–385` — combining name sets for >5 workers can yield duplicate names (no dedup) → ambiguous name resolution; dedup/resample.
- **L3** `registry.py:583–597` — `remove()` silently no-ops if the id is a recovered (not live) session; log when not found.
- **L4** `discover_workers.py:92–94,98–100` — assumes `terminal_session.native_id` set; empty-list-on-error hides discovery failures.

### Good patterns observed (keep / extend)
- Context-managed file I/O throughout `session_state.py` / `worktree.py`.
- Bounded TTL cache in `subprocess_cache.py` (5-min eviction, ≤2 keys).
- Branches **preserved** on worktree removal (recovery via cherry-pick) — `close_workers.py:144–152`.
- Frozen dataclasses for immutable types (`TerminalId`, `RecoveryReport`, `PruneReport`, `RecoveredSession`).
- Graceful worktree fallback on spawn (`spawn_workers.py:357–371`); startup prune wrapped safely (`server.py:311–312`); screen-frame defaults (`iterm_utils.py:234–269`).
- Clean cross-backend abstractions (Enter semantics, paste-delay formula, backend selection).

---

## Recommended Priority Order

1. **Add timeouts everywhere** (C1) — highest leverage; prevents the indefinite-hang class outright.
2. **Spawn rollback / cleanup on failure** (C2, H4) — `try/finally` over panes+worktrees; register only on success.
3. **Guard worktree deletion** (C3, C4, H6) — dirty-check before any removal; default `force=False`; trash-move for orphans.
4. **Fix Codex idle boundary parse** (C5) and **idle/rotation false-timeout** (M4) — restore reliable completion detection.
5. **Serialize registry access** (H1, H2) — freeze `ManagedSession`/locks; snapshot before iterate; lazy singleton.
6. **Auto-prune recovered sessions** (H5); **enforce unique/normalized names** (H7).
7. **Backend parity** (H8) — tests for iTerm backend + document or close the window/profile asymmetry per policy.
8. Sweep Medium items: `encoding="utf-8"`, input validation, marker-extraction hardening, scoped exception handling/logging.

---

*All investigation was strictly read-only; no code was modified. Companion security review: `docs/reviews/codex-security-review-2026-05-28.md`.*
