# Session Scribe

A local, privacy-first desktop app for a counseling extern's documentation. It turns therapy-session
recordings into transcripts (WhisperX) and draft notes in the South Shore Counseling format
(LM Studio, running a local model), and keeps everything for each patient in one encrypted vault.
No recording, transcript or note leaves the computer: transcription and note writing both run locally.

It's a single-file Python/tkinter program, `session_scribe.py`. The current version is in `APP_BUILD`
at the top of the file. Every build is a commit in this repository, so the history shows how it changed.

> **Drafts only.** Everything the app writes is a draft marked “needs review.” Speaker labels are a
> best guess. Check each note before anything goes into TheraNest.

## What it does

- **Recordings → notes.** Transcribe a session, tell the app who's the clinician and who's the
  patient, and get a progress note, a session overview and (for contact sessions) a contact note.
  The **Queue** window runs a batch of recordings unattended. It can also send them to your other
  computer through the shared data folder, encrypted.
- **Patients.** Each patient has a case overview, treatment plan, Ask-AI chat about their record, and
  day-by-day sessions. The local AI reads patients' names (they never leave the computer), which keeps notes clear about who said what; Settings or a patient's profile can hide them instead.
- **Progress & risk.** Risk timeline and safety-plan flags, scores, treatment-goal coverage, homework,
  follow-ups and recurring themes. **↻ Refresh &lt;patient code&gt;** brings that one patient's checks and
  case overview up to date in one go.
- **Home dashboard.** What needs attention across the caseload: risk, drafts to review, missing
  notes, plans, scores, follow-ups, homework, goals. **↻ Refresh all patients** updates everyone,
  as one task per patient in the queue.
- **Tasks queue.** Everything you start (case overviews, refreshes, checks) runs one at a time.
  The Queue window's **Tasks** tab shows what's running, paused or waiting. From there you can
  pause, reorder or cancel any task.
- **Phone.** An optional, encrypted companion page for your phone: check and steer the queue, see
  today's sessions, and ask the patient AI.
- **Two computers.** Computers that share the data folder keep each other on the newest build. The
  newer program is published to the data folder encrypted with the vault password.

## Folders

| Where | What | In this repo? |
|---|---|---|
| Program folder, e.g. `C:\Users\<you>\Session Scribe\` | `session_scribe.py`, `engine_check.py`, `Session Scribe.bat` (launcher) | **Yes**: this repository |
| Data folder, chosen in Settings (e.g. on Google Drive so both computers share it) | The encrypted vault: `vault.json`, `settings.enc`, `sessions/`, `clients/`, `files/`, `reports/`, plus `remote/` (hand-offs between computers) and `app-update/` (newest build, encrypted) | **No**: patient data, never committed |
| Local app folder: `%LOCALAPPDATA%\Session Scribe\` (Windows), `~/Library/Application Support/Session Scribe/` (Mac), `~/.local/share/session-scribe/` (Linux) | `config.json` (settings for this computer), `cache/` (encrypted search index), `checkpoints/`, `logs/`, `queue.enc`, `timing.json`, `backups/` (previous builds), app icon | **No**: per-computer state |

`.gitignore` keeps vault files, recordings, exports, logs, config and installers out of git, even
when they end up inside this folder. Before you commit, glance at the changed-files list. It should
only ever show program files, docs and tests.

## Setting up

1. Install **Python 3.12** from python.org (tkinter is included).
2. Install the requirements (the app also offers to do this from **Settings**):
   ```
   python -m pip install -r requirements.txt
   ```
   For GPU transcription, install the CUDA build of PyTorch from pytorch.org first.
3. Install **LM Studio** and download a model. The app is tuned for
   `mistral-small-3.1-24b-instruct-2503`, and it loads that model itself with 16K context,
   1 parallel request and flash attention.
4. Start the app with `Session Scribe.bat`, or `python -m session_scribe` from this folder. On first
   run, choose a data folder and a vault password.

`Session Scribe.bat` points at the author's Python install. Edit the path in it if yours is elsewhere.

### Or: one offline installer

`installer/build.ps1` builds a Windows installer with everything inside: Python, WhisperX, the speaker
model, FFmpeg, and a built-in AI (llama.cpp) with the model, in place of LM Studio. It installs with no
internet and no setup steps, and the app fits itself to the computer's memory and graphics. Each kind
of computer gets its own edition. The first one is Mistral Small 24B. See
[installer/README.md](installer/README.md).

`engine_check.py` is a diagnostic. If transcription hangs while loading PyTorch, run
`python engine_check.py` and it reports which start-up step is to blame. It touches no patient data.

## Tests

`tests/` holds end-to-end feature tests. Each runs the real app against a temporary vault and a
mock LM Studio, so no real data or model is needed.

```
python tests/test_task_queue.py
python tests/test_refresh_one.py
python tests/test_builtin_ai.py     # installer builds: built-in AI, hardware fitting (fake llama-server)
python tests/test_touch_and_map.py  # finger scrolling, session maps from transcript timestamps, Themes labels
python tests/test_names.py          # patient names for the AI: on by default, hidden per patient or for all
```

Each test opens the app window briefly and prints `N/N checks passed`. On Linux, run them under
`xvfb-run -a`. Screenshots are only saved when ImageMagick's `import` is available.

## History

- `docs/handoff-2026.11.12.8.md`: technical notes for build 2026.11.12.8 (dashboard refresh, dismissals,
  rendering and caching changes).
- `git log`: every build since 2026.11.09.2.
