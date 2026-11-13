Session Scribe (session_scribe.py) — Technical Handoff & Change Log for Claude Code

1. Overview of Architectural & Functional Changes

This document summarizes all modifications, new features, vault schema additions, and rendering/performance optimizations implemented in session_scribe.py (APP_BUILD = "2026.11.12.8").

2. Batch Refreshing (Home Screen vs. Per-Client Views)

A. Global "Refresh All Patients" on the Home Caseload Dashboard

UI Placement: Added ↻  Refresh all patients (style="Accent.TButton") to the top-right action bar of _dashboard(self, f) (Line 15627), alongside 📋  Supervision prep, with a live in-page self.progress_panel(f, owner=("all", "*")).

Method (refresh_all_patients(self, force=False) — Line 15290):

Iterates across all active (non-archived) patients in vault.list_clients().

Collects all pending Progress & risk checks across the entire caseload via _collect_insights_work(self, clients, force=force) (Line 15130):

Syncs contact-note follow-ups immediately (followups_sync).

Gathers unchecked/stale Risk records (risk_todo(sessions)).

Gathers unchecked/stale Treatment goals notes (goals_todo(c, sessions)).

Gathers unscanned Follow-ups sessions (followups_todo(sessions)).

Collects all active patients whose Case overview is missing or stale via case_overview_documented_days(sessions) (Line 9066) and case_overview_stale_days(client, sessions) (Line 9075).

If all active patients are already up to date, prompts via messagebox.askyesno whether to force-regenerate all checks and case overviews anyway.

Runs a single pausable background job (self.run_bg(..., owner=("all", "*"), pausable=True)) that executes all pending Risk, Goals, and Follow-up AI checks (_run_insights_bundles, Line 15172) followed by generate_case_overview for each patient needing a Case overview update, saving each session and client record to the encrypted vault immediately as each step completes.

B. Scoped Single-Client Refresh Buttons Inside Client Directories

Progress & Risk View (_render_insights — Line 14012):

Added Refresh progress & risk (N) in the header bar (head), scoped strictly to the open client (self.refresh_all_insights(only_client=client), Line 15229).

Runs Risk check, Treatment goals check, and Follow-ups scan for that specific patient in one combined background job, with a top-level progress panel (top_prog) visible across all sub-tabs (Risk & scores, Goals & homework, Themes).

Case Overview View (_render_case — Line 12131):

Kept Update case overview / Create case overview scoped strictly to the open client (self.make_case_overview(client), Line 13527).

Wildcard Task Ownership (_owner_matches — Line 10769, page_owns_task — Line 10785, waiting_text — Line 11056):

Extended task-owner matching so batch tasks (("all", "*"), ("insights", "*"), ("case", "*")) and per-client insights tasks (("insights", cid)) automatically bind to the active page's progress_panel.

3. Home Dashboard: Per-Patient Collapsible Dropdown Menus, Temporary Hide, and Permanent Dismissal (✕)

A. Per-Patient Collapsible Dropdowns in Each Dashboard Category (_dashboard — Line 15627)

Within each category in DASH_SECTIONS (risk, review, no_note, plans, scores, followups, homework, goals), items are grouped by patient (by_patient), preserving urgency order.

Each patient has a collapsible card with a toggle button (▼  PT-01 (2 items) when open, ▶  PT-01 (2 items) when minimized):

Clicking a patient's header button toggles (section_key, client_id) in self._dash_collapsed (persisted to self.settings["dash_collapsed"] in the encrypted vault) and packs/unpacks items_box in-place without rebuilding the screen.

Each category header also has a Minimize all patients / Expand all patients button (toggle_all_in_sec) that collapses or expands all patient menus in that category in-place.

B. Stable Item Keys (dash_item_key — Line 8926) & Two-Tier Dismissal (Hide vs. ✕)

Every dashboard row produced by caseload(pairs, today=None, include_dismissed=False) (Line 8942) is assigned a deterministic key r["dkey"] = dash_item_key(category, row) and "category": k (plus "sid": sid for session-backed rows, "fid" for follow-ups, and "hid" for homework).

Temporary Hide (Hide button → dismiss_dashboard_items, Line 15493, & dismiss_dashboard_batch, Line 15515):

Appends dkey to client["dismissed_dashboard"] in the vault.

Temporarily hides the item from the active dashboard while incrementing the hidden counter (· N hidden).

Can be restored at any time via the Unhide dismissed (N) button in the top dashboard bar (restore_dashboard_items, Line 15528).

Permanent Dismissal (✕ button → permanently_dismiss_dashboard_rows, Line 15557):

Available on every individual item row (✕), on each patient's dropdown header (✕), and on each category list header (✕  Dismiss list).

Appends dkey to client["permanently_dismissed_dashboard"] so it is permanently excluded from caseload() and never counted toward Unhide dismissed.

Also clears underlying action flags in the vault where applicable:

review (Drafts to review): Clears meta["needs_review"] and meta["review_reason"] on the session (vault.save_session) and refreshes the sidebar review badge.

followups (Follow-ups to do): Removes the follow-up entry from client["followups"].

homework (Homework to check): Updates the homework item's status from "assigned" to "dismissed" in client["homework"].

4. Performance, Memory Caching & Smooth Scrolling Optimizations

A. Root Causes Identified for Dashboard & Settings Lag

sv_ttk 1-Pixel PNG Sprite Tiling Bottleneck:

sv_ttk styles TLabelframe, TButton, TEntry, TCombobox, TSpinbox, and TCheckbutton using 9-patch PNG images with a 1-pixel center. Tcl/Tk (Ttk_TileImage) tiles 1-pixel centers by calling BitBlt/XCopyArea once per pixel—causing 250,000+ draw calls per ttk.LabelFrame in Settings and thousands per button/card on every scroll repaint.

Lazy Offscreen Widget Mapping in tk.Canvas.create_window (scrollable):

Tk's DisplayWinItem does not call Tk_MapWindow on child widgets inside an embedded canvas window until their vertical coordinates enter the visible canvas viewport. As the user scrolled down, Win32/Aqua mapped native HWNDs for the first time on the fly and fired <Configure> on every newly exposed TLabel (triggering _autowrap → pack_configure + configure(wraplength=...)) and Flow (triggering _reflow widget destruction/re-packing) mid-scroll.

Scrollbar Pack/Unpack Width Jumping & <Enter>/<Leave> Wheel Unbinding:

scrollable's Python on_scroll callback packed/unpacked the scrollbar during scroll, changing canvas width and triggering full-page <Configure> cascades. Binding <MouseWheel> on <Enter>/<Leave> of outer caused constant unbind/rebind thrashing whenever child widgets passed under the cursor during scroll.

Repeated Synchronous Disk/Vault/Environment Scans:

_dashboard_pairs was calling vault.load_session(sid) on every "parent" (contact) session on every dashboard render to check followups_sync.

open_setup (Settings) was calling importlib.invalidate_caches() 3×, walking ~/.cache/huggingface/hub with recursive glob.glob 2×, scanning PATH 3×, and parsing all PDFs/DOCXs in the worksheet folder synchronously on the UI thread just to count worksheets.

B. Fixes Implemented

Pure C Vector Theme Rendering (apply_theme — Line 9601, base_styles — Line 9585, theme_installed — Line 9112):

Replaced sv_ttk 1px PNG sprite tiling and PhotoImage card borders with native C vector styles (fallback_styles + Frame.border for Card.TFrame) using the exact same light/dark palettes (PALETTES). Removed redundant nested Card.TFrame styles on inner layout frames in _dashboard.

Upfront Full-Page Pre-Realization & Scroll Lock (scrollable — Line 9702, _autowrap — Line 9810, Flow — Line 9862):

scrollable initializes inner at its real target width immediately, runs _realize_tree(inner, target_w) to resolve all Flow rows in one synchronous pass, and steps canvas.yview_moveto across all offscreen viewport slices with update_idletasks() (realize_all()) before first paint so Tk calls Tk_MapWindow on 100% of widgets on the page upfront (nothing loads lazily as you scroll).

Added _SCROLLING_UNTIL timestamp guard (Line 9674) and per-widget width memoization (w._aw_last_w) so _autowrap and Flow._on_conf never fire during scrolling.

Replaced Python on_scroll with native Tcl yscrollcommand=sb.set and a single global <MouseWheel> dispatcher (_global_wheel), and removed <Motion> from root.bind_all.

Resident In-Memory Pages for Home Dashboard & Settings (show — Line 11283, open_setup — Line 18323):

Home Dashboard (self._home_page_frame): Kept alive in RAM (pack_forget() when viewing a client, pack() when returning to Home) and memoized caseload data in self._dash_memo keyed by (vault.signature(), today).

Settings Window (self._setup_cached): Pre-built and pre-mapped in RAM shortly after startup (open_setup(_preload=True)), and hidden via win.withdraw() on close so clicking Settings opens via win.deiconify() in < 2 ms.

512 MB Vault Parsed-Dict Cache, Directory TTL Cache & Environment Probe Cache:

Increased Vault.CACHE_BUDGET to 512 MB (Line 3439), cached parsed Python dicts in self._parsed (_read_cached, Line 3493), and added a 3.0-second directory scan cache (self._dir_cache in _scan, Line 3565, auto-invalidated on _changed).

Stored contact note text ("pnote") inside meta["_x"] in Vault._remember (Line 3686) so light_sessions() (Line 3700) provides contact notes to _dashboard_pairs() without decrypting individual session files.

Cached all environment/model checks in _SETUP_PROBES (Line 3926, invalidated only after installer tasks via invalidate_setup_probes(), Line 3930) and added worksheet_count(folder) (Line 8344) using fast os.scandir.