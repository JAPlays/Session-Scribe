#!/usr/bin/env python3
"""
Session Scribe — local, offline drafting of therapy session notes.

For each client:  recording -> WhisperX transcript -> local LLM (LM Studio)
-> SSC-style narrative progress note + session overview. Each new note is
written with the client's previous notes in view, and a combined Case
Overview can be generated across all sessions. Everything is stored in a
password-encrypted vault.

Privacy safeguards
  * Processing happens on this computer only. A network guard blocks every
    outbound connection except to this machine (LM Studio on 127.0.0.1);
    Hugging Face / pyannote are forced into offline mode.
  * Clients, transcripts, notes and overviews are encrypted at rest.
  * Names listed in a client's profile are replaced with the client code
    before the AI reads anything.
  * The original recording is overwritten and deleted after saving (you
    confirm each time). Auto-lock and clipboard clearing are built in.

Software is only one part of HIPAA compliance: your site must approve
recording and AI-assisted documentation, clients must consent in writing,
the computer should use full-disk encryption (BitLocker), and every draft
must be reviewed by you before it goes in the EHR.

Run:  python session_scribe.py   (pythonw hides the console)
Then use "Settings" in the app — it checks and installs what's missing.
"""

import os
import platform
import sys

APP_NAME = "Session Scribe"
APP_BUILD = "2026.11.09.2"   # raised with every new version; computers sharing a data folder update to the newest
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(APP_DIR, "bin")
DOWNLOAD_MODE = "--download-models" in sys.argv
TRANSCRIBE_MODE = "--transcribe-worker" in sys.argv     # a child process that transcribes one recording

# The environment as it was (proxy settings included) — only for the installers and the model
# download, which carry no patient data and may need a proxy to reach the internet.
ONLINE_ENV = dict(os.environ)
_PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy", "socks_proxy")

# Offline enforcement — must happen before any ML library is imported
if not DOWNLOAD_MODE:
    # No proxies inside the app or the transcription process: a proxy on this computer (127.0.0.1)
    # would otherwise be a way around the network guard below, which only allows local connections.
    for _k in list(os.environ):
        if _k.lower() in _PROXY_VARS:
            del os.environ[_k]
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"
    for _k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
        os.environ[_k] = "1"
for _k, _v in (("HF_HUB_DISABLE_TELEMETRY", "1"), ("DO_NOT_TRACK", "1"),
               ("PYANNOTE_METRICS_ENABLED", "0")):
    os.environ[_k] = _v
os.environ["PATH"] = BIN_DIR + os.pathsep + os.environ.get("PATH", "")   # app-local ffmpeg

import base64
import gc
import hashlib
import importlib
import importlib.util
import ipaddress
import json
import queue
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta

if os.name == "nt":  # keep ffmpeg/pip from flashing console windows
    _BasePopen = subprocess.Popen

    class _QuietPopen(_BasePopen):
        def __init__(self, *a, **k):
            k["creationflags"] = k.get("creationflags", 0) | 0x08000000
            super().__init__(*a, **k)

    subprocess.Popen = _QuietPopen


# --------------------------------------------------------------------------- #
# Network guard
# --------------------------------------------------------------------------- #
class NetworkBlocked(PermissionError):
    pass


def _is_local_host(host):
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode(errors="ignore")
    host = str(host).strip("[]")
    if host in ("", "localhost") or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def install_network_guard():
    """Refuse any connection (or lookup) that isn't to this computer.
    Child processes started on purpose (pip installs, model download) are
    separate programs and aren't affected."""
    orig_connect, orig_connect_ex = socket.socket.connect, socket.socket.connect_ex
    orig_gai = socket.getaddrinfo

    def check(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple):
            if not _is_local_host(address[0]):
                raise NetworkBlocked(f"Blocked an outbound connection to {address[0]}.")

    def connect(self, address):
        check(self, address)
        return orig_connect(self, address)

    def connect_ex(self, address):
        check(self, address)
        return orig_connect_ex(self, address)

    def getaddrinfo(host, *a, **k):
        if not _is_local_host(host):
            raise NetworkBlocked(f"Blocked a network lookup for {host!r}.")
        return orig_gai(host, *a, **k)

    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    socket.getaddrinfo = getaddrinfo


if not DOWNLOAD_MODE:
    install_network_guard()
    # system proxy settings (Windows registry, macOS network settings) are ignored as well
    urllib.request.getproxies = lambda: {}


# --------------------------------------------------------------------------- #
# Configuration and templates
# --------------------------------------------------------------------------- #
# Settings for THIS computer live in the user's local app-data folder (never synced).
# Patient data, templates and note settings live in the shared data folder, which can
# be inside Google Drive so two computers see the same files.
if os.name == "nt":
    LOCAL_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Session Scribe")
elif sys.platform == "darwin":
    LOCAL_DIR = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Session Scribe")
else:
    LOCAL_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "session-scribe")
CONFIG_PATH = os.path.join(LOCAL_DIR, "config.json")
LEGACY_CONFIG_PATH = os.path.join(APP_DIR, "config.json")
SHARED_SETTINGS = "settings.json"            # in the data folder; contains no patient data
SHARED_KEYS = {"language", "min_speakers", "max_speakers", "max_chars", "previous_notes",
               "smooth_flicker", "delete_audio", "autolock_minutes", "role_labels", "date_format"}
TEMPLATE_DIR = os.path.join(APP_DIR, "templates")     # updated by set_data_paths()
DEFAULT_CONFIG = {
    "lmstudio_url": "http://127.0.0.1:1234/v1",
    "model": "",
    "whisper_model": "large-v3-turbo",
    "language": "en",
    "batch_size": 8,
    "min_speakers": 2,
    "max_speakers": 2,
    "max_chars": 24000,
    "previous_notes": 2,
    "smooth_flicker": True,
    "delete_audio": True,
    "autolock_minutes": 10,
    "data_dir": "",             # shared folder (e.g. H:\\My Drive\\Session Scribe); "" = not chosen yet
    "vault_dir": "",            # normally derived from data_dir
    "python_path": "",          # the Python this app runs on (picked automatically)
    "appearance": "system",     # system / light / dark
    "theme_install_tried": "",
    "session_sort": "desc",     # date list order: desc = newest first, asc = oldest first
    "doc_zoom": 0,              # document text size step (A−/A+)
    "export_format": "docx",    # last export format (docx / pdf)
    "setup_assistant_offered": False,   # first Setup visit: offered to install everything for this computer
    "setup_assistant_done": False,
    "shortcut_made": False,
    "role_labels": "",          # transcript speaker labels, e.g. "Therapist, Client, Parent/Guardian, Other"
    "date_format": "iso",       # how dates are shown and typed: iso / us / eu / long (stored as YYYY-MM-DD)
    "remote_accept": True,      # this computer runs recordings sent from the other linked computer
    "remote_notify": True,      # desktop notification when a recording sent elsewhere is done
    "computer_id": "",          # this computer's id for handoffs (made once, kept locally)
    "remote_target": "",        # the queue's last "Run on" choice
}
WHISPER_MODELS = ["large-v3-turbo", "large-v3", "medium.en", "small.en"]
ROLES = ["Clinician", "Patient", "Parent/Guardian", "Other"]
DEFAULT_ROLE_LABELS = ", ".join(ROLES)


def role_labels(cfg=None):
    """Speaker labels for transcripts, from Setup: [you, the person you see, extras…]."""
    raw = (cfg or {}).get("role_labels") or DEFAULT_ROLE_LABELS
    out = []
    for part in re.split(r"[,;\n]", raw):
        part = part.strip()
        if part and part.lower() not in (o.lower() for o in out):
            out.append(part)
    defaults = ROLES
    while len(out) < 2:
        out.append(defaults[len(out)])
    if not any(o.lower() == "other" for o in out[2:]):
        out.append("Other")
    return out


def data_root(cfg):
    return cfg.get("data_dir") or APP_DIR


def set_data_paths(cfg):
    """Point templates (and the vault) at the shared data folder."""
    global TEMPLATE_DIR
    TEMPLATE_DIR = os.path.join(data_root(cfg), "templates")
    if not cfg.get("vault_dir"):
        cfg["vault_dir"] = os.path.join(data_root(cfg), "vault")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    local = _read_json(CONFIG_PATH)
    if not local and os.path.exists(LEGACY_CONFIG_PATH):          # carry over the old config once
        local = _read_json(LEGACY_CONFIG_PATH)
        if local.get("vault_dir") == os.path.join(APP_DIR, "vault"):
            local["vault_dir"] = ""                              # old default: derive it again
    cfg.update({k: v for k, v in local.items() if k in DEFAULT_CONFIG})
    if cfg.get("data_dir"):
        shared = _read_json(os.path.join(cfg["data_dir"], SHARED_SETTINGS))
        cfg.update({k: v for k, v in shared.items() if k in SHARED_KEYS})
    derived = not cfg.get("vault_dir")
    set_date_format(cfg.get("date_format"))
    set_data_paths(cfg)
    cfg["_vault_derived"] = derived
    return cfg


def save_config(cfg):
    os.makedirs(LOCAL_DIR, exist_ok=True)
    local = {k: v for k, v in cfg.items() if k in DEFAULT_CONFIG and not k.startswith("_")}
    if cfg.get("_vault_derived", True) and cfg.get("vault_dir") == os.path.join(data_root(cfg), "vault"):
        local["vault_dir"] = ""
    _atomic_json(CONFIG_PATH, local)
    if cfg.get("data_dir") and os.path.isdir(cfg["data_dir"]):
        path = os.path.join(cfg["data_dir"], SHARED_SETTINGS)
        shared = {k: cfg[k] for k in sorted(SHARED_KEYS) if k in cfg}
        if _read_json(path) != shared:          # only write (and sync) real changes
            _atomic_json(path, shared)


def _atomic_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    _replace_with_retry(tmp, path)


def _replace_with_retry(src, dst, tries=6):
    """os.replace, retrying briefly: cloud-sync apps (Google Drive) can hold a file
    open for a moment while uploading it."""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.4 * (i + 1))


# ---- shared data folder ------------------------------------------------------
CLOUD_MARKERS = ("my drive", "google drive", "googledrive", "onedrive", "dropbox", "icloud",
                 "/library/cloudstorage/", "/library/mobile documents/")


def is_cloud_path(path):
    p = os.path.abspath(path or "").lower().replace("\\", "/")
    return any(m in p for m in CLOUD_MARKERS)


def suggest_data_dirs():
    """Likely Google Drive locations for the shared folder, best first."""
    out = []
    if os.name == "nt":
        import string
        for letter in ["H"] + [c for c in string.ascii_uppercase if c not in "HAB"]:
            drive = f"{letter}:\\My Drive"
            if os.path.isdir(drive):
                out.append(os.path.join(drive, "Session Scribe"))
        g = os.path.join(os.path.expanduser("~"), "Google Drive", "My Drive")
        if os.path.isdir(g):
            out.append(os.path.join(g, "Session Scribe"))
    else:
        import glob
        home = os.path.expanduser("~")
        # macOS: Google Drive for desktop mounts at ~/Library/CloudStorage/GoogleDrive-<account>/My Drive
        for g in sorted(glob.glob(os.path.join(home, "Library", "CloudStorage", "GoogleDrive-*", "My Drive"))) + \
                [os.path.join(home, "Google Drive", "My Drive"), "/Volumes/GoogleDrive/My Drive"]:
            if os.path.isdir(g):
                out.append(os.path.join(g, "Session Scribe"))
    return out


_PINFO = {}


def platform_info():
    """Cached: the computer doesn't change while the app runs."""
    if not _PINFO:
        _PINFO.update(_platform_info())
    return dict(_PINFO)


def _platform_info():
    """What this computer is, for Setup: {"os", "label", "arch", "supported", "note"}."""
    arch = platform.machine().lower()
    if IS_WIN:
        rel = platform.release()
        try:
            if int(platform.version().split(".")[-1]) >= 22000:
                rel = "11"
        except (ValueError, IndexError):
            pass
        return {"os": "windows", "label": f"Windows {rel} ({'ARM' if 'arm' in arch else '64-bit'})", "arch": arch,
                "supported": True, "note": ""}
    if IS_MAC:
        ver = platform.mac_ver()[0] or "?"
        translated = False
        try:
            translated = subprocess.run(["sysctl", "-n", "sysctl.proc_translated"], capture_output=True,
                                        text=True, timeout=5).stdout.strip() == "1"
        except Exception:
            pass
        apple = arch in ("arm64", "aarch64") or translated
        note = ""
        if not apple:
            note = ("This is an Intel Mac. The speech engine (PyTorch) and LM Studio need an Apple Silicon "
                    "Mac (M1 or newer), so transcription and AI notes won't work here. You can still view, "
                    "edit, import and export notes.")
        elif translated:
            note = ("Python is running in Intel mode (Rosetta). Install the macOS Python from python.org "
                    "(Apple Silicon/universal2) for the speech engine to work.")
        return {"os": "mac", "label": f"macOS {ver} ({'Apple Silicon' if apple else 'Intel'})", "arch": arch,
                "supported": apple and not translated, "note": note}
    return {"os": "linux", "label": f"{platform.system()} {platform.release()} ({arch})", "arch": arch,
            "supported": True, "note": ""}


def open_path(path):
    """Open a file or folder with the computer's default app (Explorer / Finder / Linux desktop)."""
    if IS_WIN:
        os.startfile(path)
    elif IS_MAC:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def folder_has_vault(folder):
    return os.path.isfile(os.path.join(folder, "vault", "vault.json"))


def count_records(folder):
    n = 0
    for sub in ("sessions", "clients"):
        try:
            n += sum(1 for f in os.listdir(os.path.join(folder, "vault", sub)) if f.endswith(".enc"))
        except OSError:
            pass
    return n


def move_data(src_root, dst_root, log=lambda t: None):
    """Copy the vault, templates and note settings to the shared folder, check the
    copy, then rename the old vault to a dated backup (nothing is deleted)."""
    os.makedirs(dst_root, exist_ok=True)
    if folder_has_vault(dst_root):
        raise RuntimeError("That folder already has Session Scribe data. Choose \"use the files that are "
                           "already there\" instead, or pick an empty folder.")
    before = count_records(src_root)
    for name in ("vault", "templates"):
        src = os.path.join(src_root, name)
        if os.path.isdir(src):
            log(f"Copying {name}…")
            shutil.copytree(src, os.path.join(dst_root, name), dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("*.tmp"))
    sp = os.path.join(src_root, SHARED_SETTINGS)
    if os.path.isfile(sp) and not os.path.exists(os.path.join(dst_root, SHARED_SETTINGS)):
        shutil.copy2(sp, os.path.join(dst_root, SHARED_SETTINGS))
    if count_records(dst_root) != before:
        raise RuntimeError("The copy didn't check out (file counts differ). Nothing was changed; your "
                           "data is still in the old place.")
    old = os.path.join(src_root, "vault")
    if os.path.isdir(old):
        backup = os.path.join(src_root, f"vault-backup-{date.today().isoformat()}")
        i = 2
        while os.path.exists(backup):
            backup = os.path.join(src_root, f"vault-backup-{date.today().isoformat()}-{i}")
            i += 1
        os.rename(old, backup)
        log(f"Old copy kept as {backup}")
        return backup
    return ""


SYSTEM_PROMPT = """You are a clinical documentation assistant helping a mental health counseling \
extern at an outpatient practice draft documentation from recorded sessions.

Rules you must follow:
- Use ONLY information in the provided transcript, prior notes and clinician-provided context. \
Never invent quotes, history, symptoms, diagnoses, interventions, or plans. (The one exception is a \
"Planned Interventions" list, where you may propose next steps that follow from this session and the \
treatment plan, for the clinician to review.)
- Use the term "patient", never "client". Refer to the clinician as "Counselor". Names have been \
replaced with bracketed placeholders such as [Patient] or [Husband]. Write "patient" for [Patient] \
and keep other placeholders as roles (e.g. "patient's husband"); never guess at real names.
- If something is unclear or missing, write [VERIFY: ...] instead of guessing.
- Never minimize risk. Any mention of suicidal ideation, self-harm, homicidal ideation, abuse or \
neglect, or other safety concerns must be documented. Never state that risk was assessed or denied \
unless the transcript shows it.
- Transcription can contain errors and speaker labels may be imperfect; flag doubtful items \
with [VERIFY].
- Output plain text. No markdown symbols (#, *, **), no tables.
- Everything you write is a DRAFT the clinician will review, edit, and sign."""

DEFAULT_NOTE_TEMPLATE = """Write the progress note for this session in the agency's progress-note \
style: the narrative paragraph, then two short lists below it.

The narrative paragraph (the agency's style):
- ONE narrative paragraph of about 4-7 sentences (roughly 70-150 words). No headings or bullet \
points inside it, no separate Plan or Risk sections.
- Past tense, third person, using "patient" and "Counselor".
- Open with the therapeutic approach and what it was used for, for example "[Approach] was used to \
explore ..." or "[Approach] was utilized to ...", or open with "Session focused on ..." when the \
session centered on one topic.
- Then describe, in flowing sentences: the counselor skills used and why (e.g., open-ended \
questions to facilitate reflection, validation of feelings, reflection of meaning, silence, \
normalizing, empathic challenging, self-disclosure); what the patient explored or shared; and the \
patient's response, insight, or demonstrated abilities. Phrases like "Counselor observed \
patient's tendency to ...", "Patient demonstrated an ability to ...", "Counselor and patient \
explored/identified ..." fit this style.
- Draw approach, skill, patient-focus and coping-skill wording from the agency vocabulary when it \
is provided below.
- Name an approach consistent with the clinician's orientation ({orientation}) only when the \
session supports it; if the approach used is unclear, write [VERIFY: approach].
- If the patient late-cancelled or did not show, write only a brief statement documenting that.
- If ANY safety concern came up (suicidal ideation, self-harm, homicidal ideation, abuse), add \
one factual sentence documenting it and how it was addressed. If none came up, do not add a \
safety statement.
- Where it fits naturally, connect to the previous session or treatment goals in a short clause.

Then, after one blank line, these two sections, exactly in this form (plain text, one item per line, \
each line starting with "- "):

Therapeutic Interventions Used:
- [intervention]
- [intervention]

Planned Interventions (For future sessions):
- [intervention]
- [intervention]

Rules for the two lists:
- Use the agency manual's intervention terms (its keywords for the therapeutic intervention \
section: types of therapy, counselor skills, patient-focused work, coping skills), written the way \
the manual writes them, e.g. "Person-centered therapy", "Validation of feelings", "Reflection of \
meaning", "Exploring negative self-talk", "Breathing techniques". A line may add " — " and a few \
words on what it addressed (at most about 12 words), e.g. "Empathic challenging — patient's \
assumptions about coworkers".
- Therapeutic Interventions Used: 3-8 items, only interventions the transcript shows the counselor \
actually using in THIS session. They must agree with the paragraph above. If you can't tell, write \
"- [VERIFY: interventions used]".
- Planned Interventions (For future sessions): 2-5 items. First, anything the counselor said they \
would do, continue or assign next time (homework included). Then interventions that follow from \
this session's themes and the treatment plan's goals and interventions (when provided), consistent \
with the clinician's orientation ({orientation}). Don't plan anything the session or plan doesn't \
support.
- The manual's example notes show only the paragraph; still add both lists after it.
- Late cancel / no-show: write only the brief statement, with no lists.

Session details
Patient code: {client_code}
Date: {session_date}
Session number: {session_number}
Treatment goals: {goals}
Clinician's observations not captured on audio: {observations}

Below is {source_kind}.

{transcript}"""

DEFAULT_OVERVIEW_TEMPLATE = """Write a clinical overview of this session for the clinician's own \
use and for supervision. Use these plain-text headings, in this order:

CLINICAL SUMMARY
Key content and themes; changes since the previous session (if prior notes are provided); \
progress toward the treatment goals.

RISK AND SAFETY
Each risk-related statement with its timestamp and a brief quote, and whether a safety plan may be \
needed; or state that no risk indicators were discussed in the recording.

PROCESS AND DYNAMICS
Relational patterns in the room, defenses, moments of rupture and repair, and possible \
transference and countertransference. Label every interpretation as a hypothesis for the \
clinician to consider. Note patterns that recur from earlier sessions.

SUPERVISION PREP
What went well; at least two questions worth bringing to supervision; ethical, legal or risk \
items to raise; one or two skills to work on, grounded in specific moments (with timestamps).

PLAN AND NEXT STEPS
Homework or between-session tasks, referrals, coordination of care, and focus for next session.

SESSION TIMELINE
A time-stamped outline of the session, 6-15 entries, one line each, in the form \
[HH:MM:SS] what happened.

Session details
Patient code: {client_code}
Date: {session_date}
Session number: {session_number}
Clinician's theoretical orientation: {orientation}
Treatment goals: {goals}
Clinician's observations not captured on audio: {observations}

Below is {source_kind}.

{transcript}"""

DEFAULT_INTAKE_ASSESSMENT_TEMPLATE = """This was the patient's INTAKE session (Initial Exam). \
Write the agency's INITIAL ASSESSMENT AND DIAGNOSTIC IMPRESSION, using these headings exactly, in \
this order. Leave out header fields like name, birth date and insurer; those are filled in the EHR.

Writing style (match the agency's example): third person, past tense, built from what the patient \
reported, with short direct quotes in quotation marks — e.g. The patient shared that she "...". \
She disclosed that ... The patient reported that ... Vary the verbs (shared, disclosed, reported, \
stated, explained). Refer to the clinician as "this writer". Short paragraphs, plain text.

Diagnostic Impressions:
Provisional DSM-5 diagnoses supported by what was reported, one per line, with DSM/ICD-10-CM codes \
in the form 300.02 (F41.1) Generalized Anxiety Disorder. Add [VERIFY: confirm diagnosis with \
supervisor]. If the information is insufficient for a diagnosis, say so.

Patient's initial explanation of the problem(s), duration and precipitant cause:

Therapist's observations of Patient's Presentation and Family Interactions:
Use only the clinician's observations below and what is evident in the recording (e.g. coherence, \
insight, engagement). Do not invent appearance. If nothing was observed, write [VERIFY: add observations].

Pertinent History (including family, social, psychological, and medical) Any prior therapy:

Family/Psychosocial Assessment:

Evidence of potential or actual risk(s):
First a line listing which apply, from: suicide, violence, physical abuse, sexual abuse, psychotic \
break, running away, substance abuse, self-harm (or "None identified"). Then an explanation \
paragraph with the patient's words and timestamps. If risk was not asked about in the recording, \
write [VERIFY: risk assessment not captured in recording].

Patient made Contract/Safety Plan to cover risk(s): Yes/No
Explanation — e.g. why a safety plan was or wasn't made, and that risk will be assessed further. \
If any current risk was reported, flag [VERIFY: Safety Plan].

Patient/Family strengths (including support system(s)):

Tentative goals and plans:

Who will be involved in treatment?:

Expected length of treatment: (TBD unless discussed)

Is patient appropriate for agency services?: Yes/No — with a one-line explanation (e.g. weekly \
psychotherapy services with this writer).

Special needs of patient (e.g. need for interpreter, interpreter for the deaf, religious \
consultant, etc.): Yes/No — then a one-line explanation.

Cultural variables?: Yes/No — then explain, or "No cultural variables were assessed at this time."

Educational or vocational problems or needs: Yes/No — then explain.

Session details
Patient code: {client_code}
Intake date: {session_date}
Clinician's theoretical orientation: {orientation}
Background on file: {background}
Clinician's observations not captured on audio: {observations}

Below is {source_kind}.

{transcript}"""

DEFAULT_INTAKE_QUESTIONNAIRE_TEMPLATE = """This was the patient's INTAKE session. Fill in the \
agency's 13-section diagnostic intake questionnaire from what was said in the session. Use these \
numbered section headings exactly, in this order. Under each, write concise answers in clinical \
shorthand ("Px" for patient is acceptable), covering the points listed. For anything the session \
did not cover, write [assess with px]. Never fill a gap with a guess.

1. Presenting Problem: why help is sought now; what feels different recently; current stressors; \
hoped-for change; onset, frequency, duration, severity and impact on daily life; coping tried so \
far; similar past episodes, how they were treated and what helped.
2. Risk & Safety Assessment: thoughts of self-harm or not wanting to be alive; past attempts or \
plans; thoughts of harming others; safety in living situation and relationships; anything \
currently making the patient feel unsafe.
3. Mental Status (supporting questions): sleep; appetite; energy; mood most days.
4. Psychiatric & Medical History: prior diagnoses; prior counseling or psychiatric care; family \
mental health or substance use history and treatment; medical conditions; surgeries or \
hospitalizations; medications (name, dose, frequency, prescriber); alcohol, nicotine, drug or \
prescription misuse (substance, onset, frequency, duration).
5. Developmental History: childhood home life; caregivers' emotional availability; developmental \
milestones; early friendships; school experience; early stressors, losses or disruptions; typical \
response to conflict and stress; recurring relationship patterns.
6. Family & Social Relationships: who is in the family; quality of those relationships; close \
friendships and supports; social activities; leisure time.
7. Cultural, Spiritual, Systemic & Identity Factors: important identities; religious or spiritual \
community; cultural beliefs about mental health; discrimination or marginalization; barriers to \
care; systemic stressors (financial, housing, community).
8. Education & Occupational History: highest education; current employment and satisfaction; past \
jobs; volunteer, military or major extracurricular activity.
9. Legal or Safety History: arrests or charges; civil court matters (custody, orders of \
protection); current legal stressors.
10. Maintaining Factors: what makes symptoms worse; what helps short-term but causes problems \
later; what the patient believes keeps the problems going.
11. Strengths, Values & Protective Factors: strengths; coping skills that work; important values \
and goals.
12. Treatment Expectations: what the patient hopes therapy will help with; therapist style they \
respond to; approaches that helped before.
13. Final Wrap-Up: anything else the patient wanted the clinician to know.

Patient code: {client_code}
Date: {session_date}
Background on file: {background}
Clinician's observations not captured on audio: {observations}

Below is {source_kind}.

{transcript}"""

DEFAULT_CASE_TEMPLATE = """Write a current CASE OVERVIEW for this patient, drawing on all of the \
documentation below (intake, progress notes and session overviews), for treatment planning and \
supervision. Use these plain-text headings, in this order:

CASE SNAPSHOT
Presenting concerns, relevant background, provisional diagnosis if documented, and where things \
stand now.

PROGRESS BY GOAL
For each treatment goal: where the patient started, what has changed across sessions (cite \
session numbers), and current status. If goals aren't listed, describe progress on the main \
concerns and write [VERIFY: add treatment plan goals].

RECURRING THEMES AND PATTERNS
Themes, conflicts and patterns that recur across sessions, with session numbers.

INTERVENTIONS AND RESPONSE
Approaches and counselor skills used, how the patient has responded, and what seems most and \
least helpful.

RISK HISTORY
Every risk-related item across sessions (session number and date), or a statement that none \
were documented.

THERAPEUTIC RELATIONSHIP
How the alliance has developed; possible transference and countertransference themes, labeled \
as hypotheses.

CASE CONCEPTUALIZATION
A brief conceptualization through the clinician's orientation, covering presenting problem, \
alliance, symptoms, psychosocial history, preliminary diagnosis, treatment goals and techniques.

CURRENT FOCUS AND NEXT STEPS
Open threads, outstanding homework or referrals, treatment-plan review needs, and suggested focus \
for upcoming sessions.

QUESTIONS FOR SUPERVISION
Two to five questions worth raising.

Patient code: {client_code}
Clinician's theoretical orientation: {orientation}
Background: {background}
Treatment goals: {goals}
Sessions covered: {session_count} ({date_range})

Below is {source_kind}.

{sessions}"""

DIGEST_PROMPT = """This is part {part} of {total} of a therapy session transcript. Condense it \
into a detailed clinical digest so the full session can be documented later. Keep the \
[HH:MM:SS] timestamps. Preserve: topics and emotional content; each clinician intervention and \
the patient's response; any risk-related statement quoted word-for-word with its timestamp; \
history and background details; homework, plans and follow-ups; a few short, clinically \
meaningful quotes. Do not interpret beyond what was said. About 400-700 words.

{chunk}"""

NOTES_DIGEST_PROMPT = """These are documents from a block of therapy sessions with one patient \
(part {part} of {total}). Condense them into a session-by-session digest that keeps session \
numbers and dates, and preserves: main themes, progress on goals, interventions and response, \
every risk-related item, homework and plans, and process observations. No new interpretation. \
About 500-900 words.

{chunk}"""

DEFAULT_TREATMENT_PLAN_TEMPLATE = """Write a TREATMENT PLAN for this patient in the agency's \
treatment-plan format, using ONLY the documentation below. It will be typed into the EHR's \
treatment-plan form, so use these plain-text field labels, in this order:

Diagnosis: the diagnosis with DSM-5 / ICD-10-CM code(s), matching the most recent assessment and \
progress notes. If none is documented, write [VERIFY: diagnosis].
Review in: {review_days} days
Presenting Problem: 1-3 sentences on why the patient is in treatment, current symptoms, and the \
impact on daily functioning.
Behavioral Definitions: 2-5 sentences, one per line, each describing an observable symptom or \
behavior that justifies treatment, with frequency, intensity or duration where the documentation \
supports it. Never vague phrases like "has anxiety" or "gets upset".

Then 2-4 goals. For each goal use exactly this layout:
Goal 1: a clear, clinically relevant, patient-centered outcome, measurable when possible (never \
"feel better" or "fix anxiety").
Objective 1a: a behavioral, observable, measurable step toward the goal, with a frequency or \
criterion when possible (e.g. "in at least 3 situations per week").
Objective 1b: (a second objective if useful)
Intervention: what the therapist will do, written as "Therapist will utilize [approach/technique] to \
[purpose]". Match the clinician's orientation ({orientation}), individualize it to this patient, \
and avoid generic wording like "Therapist will provide support".
Additional Intervention: (optional) a second, complementary approach.

Patient Agreement: one or two sentences, in your own fresh wording (not a stock phrase), stating \
that the patient participated in developing this plan and agreed with it. If the patient is a minor, \
refer to the parent/guardian's participation and agreement. Add [VERIFY: confirm agreement].

Ground every goal in the documentation: presenting concerns, the intake, themes that recur across \
sessions, and the patient's own stated goals. Keep goals aligned with the diagnosis. If a previous \
treatment plan is provided, carry forward goals that are still active, update objectives to reflect \
the progress shown in the notes since then, and retire or revise goals that were met.

After the plan, write a line containing only --- and then "NOTES FOR CLINICIAN (not part of the \
plan):" followed by 2-5 short lines on what changed from the previous plan and why, what evidence \
each goal rests on, and anything to verify.
{instructions}
Patient code: {client_code}
Clinician's theoretical orientation: {orientation}
Background on file: {background}
Patient-stated goals on file: {goals}

Below is {source_kind}.

{material}"""

REVISE_PROMPT = """Revise the {doc_label} below according to the clinician's instructions. Change \
only what the instructions ask for, plus whatever is needed to keep the document consistent; keep \
everything else exactly as written, in the same format, structure and style. Do not add facts that \
are not in the draft or the session material. If an instruction asks for something the material \
doesn't support, write [VERIFY: ...] instead of inventing it. Output only the revised document.

CLINICIAN'S INSTRUCTIONS:
{instructions}

CURRENT DRAFT:
{draft}

SESSION MATERIAL (for checking facts only):
{source}"""

CONTEXT_HEADER = """PATIENT CONTEXT — for continuity only. Document only what happened in THIS \
session as session content. Use this to follow up on the previous plan and homework, note what \
has changed since last time, and connect to ongoing themes. Do not copy prior notes.

Background: {background}
{intake}{plan}{case}{previous}"""

STYLE_HEADER = """AGENCY DOCUMENTATION REFERENCE (from the agency's own manual, loaded by the \
clinician). Match the style, length and wording of these examples closely, but write about THIS \
session only — never copy an example's content.
{examples}{vocabulary}{intake_points}"""

TEMPLATE_FILES = {
    "session_note.txt": DEFAULT_NOTE_TEMPLATE,
    "overview.txt": DEFAULT_OVERVIEW_TEMPLATE,
    "intake_assessment.txt": DEFAULT_INTAKE_ASSESSMENT_TEMPLATE,
    "intake_questionnaire.txt": DEFAULT_INTAKE_QUESTIONNAIRE_TEMPLATE,
    "case_overview.txt": DEFAULT_CASE_TEMPLATE,
    "treatment_plan.txt": DEFAULT_TREATMENT_PLAN_TEMPLATE,
}
# Hashes of earlier built-in templates: unedited copies are upgraded automatically.
OLD_TEMPLATE_HASHES = {
    "d7c68cfafca089bc7186faed7020a2f4a51e2d00b862c363ec6073fd95d2b950",   # v1 note
    "47aba04c2ae8360c3f8f4ee5de38ce01bead4c506e931b0ec3ceae5b7c699c3d",   # v1 overview
    "38d79b41718f3f4144cbb2d59edfd8b4528f5d9b66d20d8fd30fe7b27c9d4822",   # v2 note
    "1b5b48d2628a4cc14491b1191c04bffb1b65eb7c6eb2621ef4bfa45cfb9bcc3b",   # v2 overview
    "298ba9a3e976f5131273e0a49f63233342b76fca6c5d1f92a5e07a8362f07aa5",   # v2 case overview
    "f08f9585b19702cdfad094aabc9701b37ea0826cab44183b26c135105f92b825",   # v3 note (before the interventions sections)
}
TEMPLATE_HELP = """These files are the instructions given to the local AI. Edit them to fine-tune \
the format. Keep the {placeholders}; they're filled in automatically.

session_note.txt, overview.txt: {client_code} {session_date} {session_number} {orientation} \
{goals} {observations} {source_kind} {transcript}
intake_assessment.txt, intake_questionnaire.txt: {client_code} {session_date} {orientation} \
{background} {observations} {source_kind} {transcript}
case_overview.txt: {client_code} {orientation} {background} {goals} {session_count} \
{date_range} {source_kind} {sessions}
treatment_plan.txt: {client_code} {orientation} {background} {goals} {review_days} \
{instructions} {source_kind} {material}

Added automatically before these instructions: the patient's previous notes and intake, and (if \
loaded in Settings) the example notes and vocabulary from your agency's manual.
Delete a file to restore its default.
"""


# Built-in excerpts from South Shore Counseling's internal Notes Manual (example progress notes,
# intervention vocabulary, intake discussion points), included for an SSC extern's own documentation.
# SSC's manual is internal material: don't share this program outside SSC.
BUILTIN_REFERENCE = {
    "source": "SSC Notes Manual (built in)",
    "examples": [
        "Relational therapy was used to explore family dynamics. Patient shared her frustrations about her father's impulsiveness and her sympathetic feelings towards her mom's loneliness. Counselor utilized silence to encourage further reflection, and emphasized a safe space while patient was crying. Counselor praised the patient's ability to reflect and be vulnerable in session.",
        "Person centered therapy was used to help patient explore his feelings and gain understanding of how he internalizes his daily experiences. Counselor observed patient's tendency to have rigid thinking, and normalized identification of feelings. Patient demonstrated an ability to recognize he struggles with change; he reported currently struggling to accept friendship dynamics changing.",
        "Counselor utilized psychotherapy to explore the patient's family upbringing, with an emphasis on the patient's relationship with his parents. Counselor and patient explored his parents' negative attitudes towards emotional expression, identifying that their views contributed to him suppressing his emotions. Counselor and patient also explored the patient's perceived lack of support from his parents. Identified themes of invalidation, dismissal, and feeling misunderstood.",
        "CBT was utilized to explore patient's behavioral reactions to his anger. Counselor observed patient's tendency to be self-critical. Reflection of feeling and meaning were used to clarify underlying values and reasoning. Counselor provided validation of feelings and empathic challenging as needed.",
        "Session focused on processing how patient felt when working on a recent dating goal. Patient demonstrated an ability to regulate her discomfort/anxiety with breathing techniques and take time afterward to reflect/practice self-compassion. Counselor and patient identified patient's tendency to seek instant gratification, and worked to identify other ways she can feel accomplished.",
        "Person centered therapy was utilized to explore patient's internal dialogue in response to her feelings about her existing relationship with food. Counselor observed patient struggles with self-doubt about her decisions and has developed maladaptive thought patterns. Counselor utilized open-ended questions to facilitate further reflection about patient's fears and how her childhood experiences are impacting her in the present. Counselor also utilized silence to provide emotional comfort and validate a safe space.",
        'Session focused on patient\'s disclosure he is working on "letting go of things." CBT was utilized to explore thought processes and examine his tendency to make assumptions about others. Counselor encouraged further reflection through open-ended questions and encouragement. Counselor and patient also discussed patient\'s fears regarding his purpose and how he can challenge them.',
        'Person centered therapy was used to explore the correlation between patient being uncertain of who he is, and his tendency to put on a "persona" that has people pleasing tendencies. As a result, patient feels emotionally exhausted, then finds himself engaging in negative self-talk. Counselor used open-ended questions for reflection, validated patient\'s feelings, and used self-disclosure to support patient\'s subjective experience.',
        "CBT was continued to be utilized to explore patient's ability to maintain a healthy mindset despite recent setbacks in his professional goals. Patient shared details of the setbacks, his emotional reaction, and his behavioral reaction to his feelings. Counselor used open-ended questions to promote further self-reflection and used self-disclosure to help validate patient's disappointment/frustration.",
    ],
    "vocabulary": 'Different types of therapy: Cognitive behavioral play therapy; CBT; DBT informed techniques; Solution focused therapy; Psychodynamic therapy; Person-centered therapy; Family Systems; ACT techniques; Attachment Therapy; Schema Therapy; Socratic questioning; Motivational Interviewing; Role playing; Supportive Therapy; Self Compassion Focused Therapy\nCounselor skills: Establishing the therapeutic alliance; Building rapport; Strengthening rapport; Active listening; Empathic listening; Empathic understanding; Empathic challenging; Insight formation; Insight development; Self-disclosure; Use of humor; Positive and supportive reinforcement; Normalizing; Validation of feelings; Free association; Reflection of content; Reflection of meaning; Summarization of content\nPatient focused: Identified likes and interests; Explored interpersonal relationships; Explored intrapersonal relationships; Explored relationship patterns; Explored behavioral patterns; Exploring triggers; Identifying feelings; Recognizing feelings; Increasing self-awareness; Addressing core beliefs; Exploring negative self-talk; Exploring relationship dynamics; Identified goals\nCoping skills: Develop/Improve Self-compassion; Exposure and Response prevention; Meaning making; Shame resilience; Conflict resolution skills; Assertiveness Training; Improving communication skills; Mindfulness training; Psychoeducation; Exploring patient strengths; Anxiety management; Stress management; Journaling; Breathing techniques; Distress tolerance skills',
    "intake_points": "1. Diagnostic Impressions\n2. Patient's initial explanation of the problem(s), duration and precipitant cause\n3. Therapist's observations of Patient's Presentation and Family Interactions\n4. Pertinent History (including family, social, psychological, and medical, and any prior therapy)\n5. Family/Psychosocial Assessment\n6. Evidence of potential or actual risk(s); if so, create Safety Plan\n7. Patient's main strengths (including support system(s))\n8. Tentative goals and plans\n9. Cultural variables\n10. Educational or vocational problems or needs",
}


def _hash(text):
    return hashlib.sha256(text.strip().encode()).hexdigest()


def ensure_templates():
    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    for name, text in TEMPLATE_FILES.items():
        p = os.path.join(TEMPLATE_DIR, name)
        try:
            with open(p, encoding="utf-8") as f:
                upgrade = _hash(f.read()) in OLD_TEMPLATE_HASHES
        except OSError:
            upgrade = True
        if upgrade:
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
    readme = os.path.join(TEMPLATE_DIR, "README.txt")
    try:
        with open(readme, encoding="utf-8") as f:
            same = f.read() == TEMPLATE_HELP
    except OSError:
        same = False
    if not same:                       # don't touch synced files that haven't changed
        with open(readme, "w", encoding="utf-8") as f:
            f.write(TEMPLATE_HELP)


def read_template(name):
    try:
        with open(os.path.join(TEMPLATE_DIR, name), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return TEMPLATE_FILES[name]


def fill(template, values):
    for k, v in values.items():
        template = template.replace("{" + k + "}", str(v))
    return template


# --------------------------------------------------------------------------- #
# Encrypted vault
# --------------------------------------------------------------------------- #
def wipe_file(path):
    """Overwrite with zeros, then delete. (On SSDs this isn't a guarantee —
    BitLocker is what really protects deleted data.)"""
    try:
        size = os.path.getsize(path)
        with open(path, "r+b") as f:
            block, done = b"\0" * (1 << 20), 0
            while done < size:
                n = min(len(block), size - done)
                f.write(block[:n])
                done += n
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    try:
        os.remove(path)
    except FileNotFoundError:
        pass                                   # already gone (e.g. removed by sync)


# ---- sync check (two computers sharing the data folder through Google Drive) -------------
SYNC_DIR = "sync"
_HASH_CACHE = {}


def this_computer():
    return re.sub(r"[^A-Za-z0-9_.-]", "_", platform.node() or "this-computer")[:60]


def file_manifest(root):
    """{relative path: [content hash, modified time]} for everything that syncs: the encrypted
    vault, templates and shared settings. Hashes are cached, so repeat checks are quick."""
    out = {}
    for sub in ("vault", "templates"):
        base = os.path.join(root, sub)
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if not d.startswith(".")]
            for fn in fns:
                if fn.endswith((".tmp", ".part")) or fn.startswith(("~$", ".")) or fn == "deleted.json":
                    continue
                full = os.path.join(dp, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                key = (full, st.st_size, st.st_mtime_ns)
                h = _HASH_CACHE.get(key)
                if h is None:
                    try:
                        with open(full, "rb") as f:
                            h = hashlib.sha256(f.read()).hexdigest()[:20]
                    except OSError:
                        continue
                    _HASH_CACHE[key] = h
                out[os.path.relpath(full, root).replace(os.sep, "/")] = [h, int(st.st_mtime)]
    sp = os.path.join(root, SHARED_SETTINGS)
    if os.path.isfile(sp):
        with open(sp, "rb") as f:
            out[SHARED_SETTINGS] = [hashlib.sha256(f.read()).hexdigest()[:20], int(os.path.getmtime(sp))]
    return out


def write_sync_marker(root, manifest=None):
    """Record what this computer has, so the other computer can tell what hasn't arrived yet.
    Contains only random file names and hashes — no patient information."""
    folder = os.path.join(root, SYNC_DIR)
    os.makedirs(folder, exist_ok=True)
    data = {"computer": platform.node() or "this computer", "time": int(time.time()),
            "files": manifest if manifest is not None else file_manifest(root)}
    path = os.path.join(folder, this_computer() + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    _replace_with_retry(tmp, path)


def drive_app_running():
    if IS_MAC:
        try:
            return subprocess.run(["pgrep", "-f", "Google Drive"], capture_output=True, timeout=5).returncode == 0
        except Exception:
            return None
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq GoogleDriveFS.exe", "/NH"], capture_output=True,
                             text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "googledrivefs.exe" in out.stdout.lower()
    except Exception:
        return None


def sync_report(root):
    """Compare this computer's copy with what the other computer(s) last reported.
    Returns {"ok": bool, "summary": str, "details": [str], "others": int}."""
    details, problems = [], []
    cloud = is_cloud_path(root)
    if not os.path.isdir(os.path.join(root, "vault")):
        return {"ok": False, "summary": "Can't reach the data folder", "others": 0,
                "details": [f"{root} isn't available. If it's in Google Drive, make sure Drive is running."]}
    mine = file_manifest(root)
    try:
        write_sync_marker(root, mine)
    except OSError as e:
        problems.append(f"Couldn't write to the shared folder ({e}).")
    try:
        with open(os.path.join(root, "vault", "deleted.json"), encoding="utf-8") as f:
            gone = set(json.load(f))
    except (OSError, ValueError):
        gone = set()
    if cloud:
        running = drive_app_running()
        if running is False:
            problems.append("Google Drive for desktop isn't running, so changes aren't being sent or received. "
                            "Start Google Drive.")
    conflicts = [rel for rel in mine if re.search(r"\(\d+\)\.\w+$|conflict", rel, re.I)]
    if conflicts:
        problems.append(f"Google Drive made {len(conflicts)} conflict cop{'y' if len(conflicts) == 1 else 'ies'} "
                        f"(both computers changed the same file): " + ", ".join(conflicts[:3]))
    others = 0
    folder = os.path.join(root, SYNC_DIR)
    me = this_computer() + ".json"
    for fn in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if not fn.endswith(".json") or fn == me:
            continue
        try:
            with open(os.path.join(folder, fn), encoding="utf-8") as f:
                other = json.load(f)
        except (OSError, ValueError):
            continue
        others += 1
        name = other.get("computer") or fn[:-5]
        when = datetime.fromtimestamp(other.get("time", 0)).strftime("%b %d, %I:%M %p").replace(" 0", " ")
        theirs = other.get("files", {})
        incoming = [rel for rel, (h, t) in theirs.items()
                    if rel not in gone and (rel not in mine or (mine[rel][0] != h and t > mine[rel][1]))]
        outgoing = [rel for rel, (h, t) in mine.items()
                    if rel not in theirs or (theirs[rel][0] != h and t > theirs[rel][1])]
        if incoming:
            problems.append(f"{len(incoming)} change(s) from {name} haven't arrived on this computer yet. "
                            "Google Drive is probably still downloading them — check again in a minute.")
        else:
            details.append(f"Has everything {name} had when it last checked in ({when}).")
        if outgoing:
            details.append(f"{len(outgoing)} newer change(s) from this computer will show on {name} next time "
                           "it opens Session Scribe (or presses Sync).")
    if not others:
        details.append("No other computer has checked in yet. Press Sync on the other computer once and both "
                       "will confirm each other from then on." if cloud else
                       "This data folder is on this computer only — nothing to sync.")
    ok = not problems
    summary = ("Everything is synced" if ok and others else "Up to date on this computer" if ok
               else "Not fully synced")
    return {"ok": ok, "summary": summary, "details": problems + details, "others": others}


def _num(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def day_numbers(sessions):
    """{date: session number} — one number per date (documents on the same date are one session)."""
    out = {}
    for _, d in sessions:
        m = d.get("meta", {})
        day, n = m.get("session_date") or "", _num(m.get("session_number"))
        if day and n is not None and day not in out:
            out[day] = n
    return out


def duplicate_numbers(sessions):
    """{number: [dates]} for session numbers used on more than one date."""
    by_num = {}
    for day, n in day_numbers(sessions).items():
        by_num.setdefault(n, []).append(day)
    return {n: sorted(days) for n, days in sorted(by_num.items()) if len(days) > 1}


# ---- TheraNest appointment history ------------------------------------------------------
APPT_EXCLUDE_CODES = {"99997"}
SERVICE_CODE = re.compile(r"\b(9\d{4}|H\d{4}|G\d{4}|T\d{4})\b")
APPT_STATUSES = [  # (pattern, label, counted?)
    (re.compile(r"\bre-?scheduled\b", re.I), "Rescheduled", False),
    (re.compile(r"\b(late\s+)?cancell?ed\b|\bcancel(l)?ation\b|\blate\s+cancel\b", re.I), "Canceled", False),
    (re.compile(r"\bno[\s-]?show\b", re.I), "No show", True),
    (re.compile(r"\b(show(ed)?|arrived|attended|completed|checked[\s-]?(in|out)|kept|seen)\b", re.I), "Show", True),
    (re.compile(r"\b(scheduled|confirmed|pending|booked)\b", re.I), "Scheduled", True),
]
DATE_COLUMNS = ("appointment date", "appt date", "service date", "date of service", "start date", "start",
                "date")


def read_table_file(path):
    """Rows of cells from .csv or .xlsx (read locally, no extra packages)."""
    import csv
    import zipfile
    import html as _html
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
            return [row for row in csv.reader(f)]
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            sx = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
            for si in re.findall(r"<si>(.*?)</si>", sx, flags=re.S):
                shared.append(_html.unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", si, flags=re.S))))
        sheet = next((n for n in sorted(z.namelist()) if n.startswith("xl/worksheets/sheet")), None)
        if not sheet:
            return []
        xml = z.read(sheet).decode("utf-8", "replace")
    rows = []
    for rx in re.findall(r"<row[^>]*>(.*?)</row>", xml, flags=re.S):
        cells = {}
        for ref, attrs, body in re.findall(r'<c r="([A-Z]+)\d+"([^>]*)>(.*?)</c>', rx, flags=re.S):
            v = re.search(r"<v>(.*?)</v>", body, flags=re.S)
            t = re.search(r't="(\w+)"', attrs)
            if t and t.group(1) == "s" and v:
                val = shared[int(v.group(1))]
            elif t and t.group(1) == "inlineStr":
                val = _html.unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", body, flags=re.S)))
            else:
                val = _html.unescape(v.group(1)) if v else ""
            col = 0
            for ch in ref:
                col = col * 26 + (ord(ch) - 64)
            cells[col - 1] = val
        if cells:
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
    return rows


def _excel_date(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if 30000 < n < 80000:
        return (date(1899, 12, 30) + timedelta(days=int(n))).isoformat()
    return None


def _appt(day, line, code=None, status_text=None):
    code = code or (SERVICE_CODE.search(line).group(1) if SERVICE_CODE.search(line) else "")
    label, counted = "", True
    hay = status_text if status_text is not None else line
    # the status column is usually last; a service description can contain status words
    # ("99997 – No Show/Late Cancel"), so the match nearest the end of the line wins
    best = None
    for prio, (rx, lab, cnt) in enumerate(APPT_STATUSES):
        for m in rx.finditer(hay):
            key = (m.end(), -prio)
            if best is None or key > best[0]:
                best = (key, lab, cnt)
    if best:
        label, counted = best[1], best[2]
    why = ""
    if not counted:
        why = label
    elif code in APPT_EXCLUDE_CODES:
        counted, why = False, f"service {code}"
    elif day > date.today().isoformat():
        counted, why = False, "upcoming"
    return {"date": day, "code": code, "status": label or "—", "counted": counted, "why": why}


def parse_appointment_rows(rows):
    """Table rows (CSV/Excel) → appointments, using the header when there is one."""
    head_i = next((i for i, r in enumerate(rows[:15])
                   if any("date" in str(c).lower() for c in r)
                   and any(k in " ".join(map(str, r)).lower() for k in ("status", "service", "type"))), None)
    if head_i is None:
        return parse_appointment_text("\n".join(" | ".join(map(str, r)) for r in rows))
    head = [str(c).strip().lower() for c in rows[head_i]]
    def col(*names):
        for n in names:
            for i, h in enumerate(head):
                if h == n:
                    return i
        for n in names:
            for i, h in enumerate(head):
                if n in h:
                    return i
        return None
    dc = col(*DATE_COLUMNS)
    sc = col("status", "appointment status", "attendance")
    vc = col("service type", "service code", "service", "cpt", "code", "type")
    out = []
    for r in rows[head_i + 1:]:
        cells = [str(c) for c in r] + [""] * len(head)
        raw = cells[dc] if dc is not None else ""
        day = _excel_date(raw) or find_date(raw)
        if not day:
            continue
        line = " | ".join(cells)
        svc = cells[vc] if vc is not None else line
        m = SERVICE_CODE.search(svc)
        out.append(_appt(day, line, code=m.group(1) if m else "",
                         status_text=cells[sc] if sc is not None else None))
    return out


def parse_appointment_text(text):
    """Report text (PDF/Word/pasted) → appointments. One appointment per dated line; a status
    or service that wrapped onto the next line(s) is picked up too."""
    out, cur = [], None
    for line in text.replace("\f", "\n").split("\n"):
        t = line.strip()
        if not t:
            continue
        day = find_date(t)
        if day and not re.search(r"\b(birth|dob|printed|generated|report date|run date)\b", t, re.I):
            if cur:
                out.append(cur)
            cur = [day, t]
        elif cur and len(cur[1]) < 400 and not re.match(r"^(page\s+\d+|total)", t, re.I):
            cur[1] += " " + t
    if cur:
        out.append(cur)
    return [_appt(d, line) for d, line in out]


def read_appointment_report(path=None, text=None):
    if text is not None:
        return parse_appointment_text(_normalize(text))
    ext = os.path.splitext(path)[1].lower()
    if ext in (".csv", ".xlsx", ".xlsm"):
        return parse_appointment_rows(read_table_file(path))
    return parse_appointment_text(extract_text(path))


def compare_appointments(appts, sessions, first_number=1):
    """Line up TheraNest's attended appointments with the patient's library.
    Returns rows: {date, expected, have, kind, note, fix_num, fix_date}."""
    counted = sorted(a["date"] for a in appts if a["counted"])
    expected = {}
    for i, d_ in enumerate(counted):
        expected.setdefault(d_, i + first_number)
    excluded = {}
    for a in appts:
        if not a["counted"] and a["date"] not in expected:
            excluded.setdefault(a["date"], a["why"] or a["status"])
    have = day_numbers(sessions)
    lib_days = sorted({d.get("meta", {}).get("session_date") for _, d in sessions
                       if d.get("meta", {}).get("session_date")})
    unmatched_theranest = [d_ for d_ in expected if d_ not in lib_days]
    # a misdated note: the only TheraNest date within a week that has no note — used only when
    # the pairing is one-to-one, so two notes are never pointed at the same date
    extra_days = [d_ for d_ in lib_days if d_ not in expected and d_ not in excluded]
    near = {d_: [t for t in unmatched_theranest
                 if abs((date.fromisoformat(t) - date.fromisoformat(d_)).days) <= 7] for d_ in extra_days}
    claims = {}
    for d_, ts in near.items():
        if len(ts) == 1:
            claims.setdefault(ts[0], []).append(d_)
    suggest = {ds[0]: t for t, ds in claims.items() if len(ds) == 1}
    rows = []
    for d_ in sorted(set(expected) | set(lib_days)):
        e, h = expected.get(d_), have.get(d_)
        r = {"date": d_, "expected": e, "have": h, "kind": "", "note": "", "fix_num": None, "fix_date": None,
             "clear_num": False}
        if e is not None and d_ in lib_days:
            if h == e:
                r["kind"], r["note"] = "ok", "✓ Matches"
            else:
                r["kind"] = "number"
                r["note"] = f"Session # should be {e}" + (f" (has #{h})" if h is not None else " (has none)")
                r["fix_num"] = e
        elif e is not None:
            r["kind"], r["note"] = "missing", "Not in Session Scribe (no note for this date)"
        else:
            why = excluded.get(d_)
            r["kind"] = "extra"
            r["note"] = (f"Not a session in TheraNest ({why})" if why
                         else "Not in TheraNest's history — wrong date?")
            if why and h is not None:
                r["clear_num"] = True
                r["note"] += " — remove its session #"
            if d_ in suggest:
                r["fix_date"] = suggest[d_]
                r["note"] += f" Probably {suggest[d_]}."
        rows.append(r)
    # sessions moved to a suggested date take that date's number
    for r in rows:
        if r["fix_date"]:
            r["fix_num"] = expected.get(r["fix_date"])
    return rows


# ---- export to Word / PDF (built here, no extra packages) --------------------------------
EXPORT_KINDS = [("notes", "Progress notes"), ("intake", "Intake assessments"), ("overviews", "Session overviews"),
                ("transcripts", "Transcripts"), ("plan", "Current treatment plan"),
                ("plans_old", "Earlier treatment plan versions"), ("case", "Case overview"),
                ("chats", "Ask-AI conversations")]


def collect_export(client, sessions, include, start="", end=""):
    """Blocks for an export: [("title"|"sub"|"h1"|"h2"|"p"|"break", text)]."""
    code = client.get("code", "Patient")
    inr = lambda d_: bool(d_) and (not start or d_ >= start) and (not end or d_ <= end)
    rng = (f"{fmt_date(start) or 'first session'} to {fmt_date(end) or 'latest'}") if (start or end) else "all dates"
    blocks = [("title", f"{code} — clinical documentation"),
              ("sub", f"Dates: {rng}  ·  Exported {fmt_date(date.today().isoformat())} from Session Scribe")]
    first = True

    def section(h, text):
        nonlocal first
        if not first:
            blocks.append(("break", ""))
        first = False
        blocks.append(("h1", h))
        for para in re.split(r"\n\s*\n", (text or "").strip()):
            blocks.append(("p", para.strip()))
    if "case" in include and (client.get("case_overview") or {}).get("text"):
        section("Case overview", client["case_overview"]["text"])
    plans = client.get("treatment_plans") or []
    cur = current_plan(client)
    if "plan" in include and cur:
        section(f"Treatment plan (version {cur.get('version', '')}, {fmt_date(str(cur.get('created', ''))[:10])})",
                plan_for_ehr(cur["text"]))
    if "plans_old" in include:
        for pl in sorted(plans, key=lambda x: str(x.get("created", ""))):
            if pl is cur or not inr(str(pl.get("created", ""))[:10]):
                continue
            section(f"Treatment plan (version {pl.get('version', '')}, {fmt_date(str(pl.get('created', ''))[:10])})",
                    plan_for_ehr(pl["text"]))
    for day, records in group_by_date(sessions):
        if not inr(day):
            continue
        parts = []
        for label, key, _sid, d in day_docs(records):
            kind = d.get("meta", {}).get("kind")
            want = {"note": "intake" if kind == "intake" else "notes", "overview": "overviews",
                    "transcript": "transcripts"}[key]
            if want in include and (d.get(key) or "").strip():
                parts.append((label, d[key]))
        if not parts:
            continue
        if not first:
            blocks.append(("break", ""))
        first = False
        blocks.append(("h1", f"{day_title(records)} — {fmt_date(day)}"))
        for label, text in parts:
            blocks.append(("h2", label))
            for para in re.split(r"\n\s*\n", text.strip()):
                blocks.append(("p", para.strip()))
    if "chats" in include:
        for ch in client.get("chats") or []:
            if not inr(str(ch.get("created", ""))[:10]):
                continue
            if not first:
                blocks.append(("break", ""))
            first = False
            blocks.append(("h1", f"Ask AI — {ch.get('title', 'conversation')} ({fmt_date(str(ch.get('created', ''))[:10])})"))
            for msg in ch.get("messages", []):
                who = "Question" if msg["role"] == "user" else "AI (draft — verify)"
                blocks.append(("h2", who))
                for para in re.split(r"\n\s*\n", msg["text"].strip()):
                    blocks.append(("p", para.strip()))
    return blocks


def _xml(t):
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def write_docx(path, blocks, footer=""):
    """A real Word document (styles: Title, Subtitle, Heading 1/2, Normal) — opens in Word,
    Google Docs and LibreOffice."""
    import zipfile
    body = []
    for kind, text in blocks:
        if kind == "break":
            body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
            continue
        style = {"title": "Title", "sub": "Subtitle", "h1": "Heading1", "h2": "Heading2"}.get(kind)
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        runs = []
        for i, line in enumerate(text.split("\n")):
            if i:
                runs.append("<w:r><w:br/></w:r>")
            runs.append(f'<w:r><w:t xml:space="preserve">{_xml(line)}</w:t></w:r>')
        body.append(f"<w:p>{ppr}{''.join(runs)}</w:p>")
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" ' \
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    foot_ref = '<w:footerReference w:type="default" r:id="rIdFooter"/>' if footer else ""
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {W}><w:body>'
                + "".join(body) +
                f'<w:sectPr>{foot_ref}<w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" '
                'w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
                '</w:body></w:document>')
    font = '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
    styles = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles {W}>'
              f'<w:docDefaults><w:rPrDefault><w:rPr>{font}<w:sz w:val="22"/></w:rPr></w:rPrDefault>'
              '<w:pPrDefault><w:pPr><w:spacing w:after="160" w:line="276" w:lineRule="auto"/></w:pPr>'
              '</w:pPrDefault></w:docDefaults>'
              '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
              '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/>'
              '<w:pPr><w:spacing w:after="80"/></w:pPr><w:rPr><w:sz w:val="48"/><w:color w:val="1F3864"/></w:rPr></w:style>'
              '<w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/><w:basedOn w:val="Normal"/>'
              '<w:pPr><w:spacing w:after="360"/></w:pPr><w:rPr><w:color w:val="595959"/><w:sz w:val="20"/></w:rPr></w:style>'
              '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
              '<w:next w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="240" w:after="120"/><w:outlineLvl w:val="0"/>'
              '</w:pPr><w:rPr><w:b/><w:color w:val="1F3864"/><w:sz w:val="30"/></w:rPr></w:style>'
              '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>'
              '<w:next w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="200" w:after="80"/><w:outlineLvl w:val="1"/>'
              '</w:pPr><w:rPr><w:b/><w:color w:val="2E74B5"/><w:sz w:val="24"/></w:rPr></w:style>'
              '<w:style w:type="paragraph" w:styleId="Footer"><w:name w:val="footer"/><w:basedOn w:val="Normal"/>'
              '<w:rPr><w:color w:val="7F7F7F"/><w:sz w:val="18"/></w:rPr></w:style>'
              '</w:styles>')
    footer_xml = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:ftr {W}><w:p><w:pPr>'
                  f'<w:pStyle w:val="Footer"/><w:jc w:val="center"/></w:pPr><w:r><w:t xml:space="preserve">'
                  f'{_xml(footer)}  ·  Page </w:t></w:r><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r>'
                  '<w:instrText xml:space="preserve"> PAGE </w:instrText></w:r><w:r><w:fldChar w:fldCharType="end"/>'
                  '</w:r></w:p></w:ftr>')
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
          'wordprocessingml.document.main+xml"/>'
          '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.'
          'wordprocessingml.styles+xml"/>'
          + ('<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.'
             'wordprocessingml.footer+xml"/>' if footer else "") + '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>')
    doc_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/styles" Target="styles.xml"/>'
                + ('<Relationship Id="rIdFooter" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                   'relationships/footer" Target="footer1.xml"/>' if footer else "") + '</Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", styles)
        if footer:
            z.writestr("word/footer1.xml", footer_xml)


_HELV = dict(zip(range(32, 127), [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556,
    556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278,
    500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469,
    556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500,
    278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]))


def write_pdf(path, blocks, footer=""):
    """A simple, clean PDF (US Letter, Helvetica) with headings, page breaks and page numbers."""
    W_, H_, M = 612, 792, 72
    styles = {"title": ("F2", 22, 10, (0.12, 0.22, 0.39)), "sub": ("F1", 10, 18, (0.35, 0.35, 0.35)),
              "h1": ("F2", 15, 8, (0.12, 0.22, 0.39)), "h2": ("F2", 12, 5, (0.18, 0.45, 0.71)),
              "p": ("F1", 11, 7, (0.1, 0.1, 0.1))}

    def enc(t):
        return t.encode("cp1252", "replace").decode("latin-1")

    def width(t, size, bold):
        k = 1.06 if bold else 1.0
        return sum(_HELV.get(ord(c), 556) for c in t) * size / 1000 * k

    def wrap(text, size, bold):
        out = []
        for raw in text.split("\n"):
            words, line = raw.split(" "), ""
            for w in words:
                cand = (line + " " + w) if line else w
                if width(enc(cand), size, bold) <= W_ - 2 * M or not line:
                    line = cand
                else:
                    out.append(line)
                    line = w
            out.append(line)
        return out
    pages, ops, y = [], [], H_ - M

    def new_page():
        nonlocal ops, y
        if ops:
            pages.append(ops)
        ops, y = [], H_ - M
    for kind, text in blocks:
        if kind == "break":
            new_page()
            continue
        font, size, after, rgb = styles.get(kind, styles["p"])
        lead = size * 1.35
        lines = wrap(text, size, font == "F2")
        if kind in ("h1", "h2") and y - lead * (len(lines) + 2) < M:
            new_page()                                    # keep headings with what follows
        if kind in ("h1", "h2") and y < H_ - M:
            y -= size * 0.6
        for ln in lines:
            if y - lead < M:
                new_page()
            y -= lead
            t = enc(ln).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"BT /{font} {size} Tf {rgb[0]} {rgb[1]} {rgb[2]} rg {M} {y:.1f} Td ({t}) Tj ET")
        y -= after
        if kind == "h1":
            ops.append(f"0.75 0.8 0.88 RG 0.8 w {M} {y + after - 3:.1f} m {W_ - M} {y + after - 3:.1f} l S")
    if ops or not pages:
        pages.append(ops)
    n = len(pages)
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None,
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"]
    kids = []
    for i, page_ops in enumerate(pages, 1):
        foot = enc(f"{footer}  ·  Page {i} of {n}" if footer else f"Page {i} of {n}")
        foot = foot.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        fw = width(foot, 9, False)
        stream = "\n".join(page_ops + [f"BT /F1 9 Tf 0.5 0.5 0.5 rg {(W_ - fw) / 2:.1f} 40 Td ({foot}) Tj ET"])
        data = stream.encode("latin-1")
        objs.append(f"<< /Length {len(data)} >>\nstream\n" + stream + "\nendstream")
        content_id = len(objs)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W_} {H_}] /Contents {content_id} 0 R "
                    "/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {n} >>"
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o.encode("latin-1") + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    with open(path, "wb") as f:
        f.write(bytes(out))


# ---- Ask AI about a patient ----------------------------------------------------------------
ASK_SYSTEM = """You help a mental health counseling extern at South Shore Counseling (SSC) understand \
one patient's record. Answer ONLY from the PATIENT RECORD and SSC MANUAL GUIDANCE provided. \
Rules:
- Cite where each point comes from, e.g. "(Session 4, 2026-04-23)" or "(treatment plan)".
- If the record doesn't contain the answer, say so plainly and say what's missing. Never invent \
history, quotes, diagnoses, dates or scores.
- Use SSC's terms ("patient", "Counselor") and the SSC manual's vocabulary when describing interventions.
- Frame clinical ideas as things to consider or bring to supervision, consistent with the \
clinician's orientation ({orientation}); you are not making decisions for them.
- If anything suggests risk (self-harm, harm to others, abuse, neglect), point it out first and \
recommend discussing it with the supervisor promptly.
- Names are replaced by placeholders like [Patient]; keep them as they are.
- Be concise: short paragraphs or bullet points, plain text (no markdown tables)."""

ASK_STOP = set("""a an the and or but if of to in on at for from with about into over after before is are was
were be been being do does did have has had i me my we our you your he him his she her they them their it its
this that these those what which who whom how when where why can could should would will shall may might
there here than then so not no yes any all each more most other some such only own same too very just also
tell give show please patient client session sessions note notes""".split())


def _ask_words(q):
    return {w for w in re.findall(r"[a-z][a-z'-]{2,}", q.lower()) if w not in ASK_STOP}


def build_ask_prompt(client, sessions, question, history, reference, orientation, budget=24000):
    """The question plus the most relevant parts of the patient's library, within `budget`
    characters. Returns (prompt, sources) — names already hidden."""
    hide = client_hider(client)
    words = _ask_words(question)
    want_day = ""
    for m in re.finditer(r"\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b|"
                         r"\b[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b", question):
        want_day = parse_date(m.group(0))         # follows the date format chosen in Settings
        if want_day:
            break
    m = re.search(r"\bsession\s*#?\s*(\d{1,3})\b", question, re.I)
    want_num = int(m.group(1)) if m else None
    parts, sources, used = [], [], 0

    def add(header, text, limit=None):
        nonlocal used
        text = hide((text or "").strip())
        if not text:
            return False
        if limit:
            text = text[:limit] + (" …[shortened]" if len(text) > limit else "")
        block = f"=== {header} ===\n{text}\n"
        if used + len(block) > budget:
            return False
        parts.append(block)
        used += len(block)
        return True

    # always: who the patient is, the plan and the big picture
    prof = [f"Patient code: {client.get('code', '')}"]
    if client.get("goals"):
        prof.append("Goals: " + client["goals"])
    if client.get("background"):
        prof.append("Background: " + client["background"])
    add("PATIENT PROFILE", "\n".join(prof))
    plan = current_plan(client)
    if plan:
        add(f"CURRENT TREATMENT PLAN (v{plan.get('version', '')}, {str(plan.get('created', ''))[:10]})",
            plan_for_ehr(plan["text"]), limit=6000)
        sources.append("treatment plan")
        older = [p_ for p_ in client.get("treatment_plans") or [] if p_ is not plan]
        if older:
            add("EARLIER TREATMENT PLANS", "\n".join(f"v{p_.get('version', '')} ({str(p_.get('created', ''))[:10]})"
                                                     for p_ in older))
    case = (client.get("case_overview") or {}).get("text")
    if case and add("CASE OVERVIEW", case, limit=6000):
        sources.append("case overview")
    groups = group_by_date(sessions)
    index = []
    for day, records in groups:
        index.append(f"{fmt_date(day)}: {day_title(records)} ({', '.join(lbl for lbl, *_ in day_docs(records))})")
    if index:
        add("SESSION INDEX (all dates on file)", "\n".join(index))
    appts = (client.get("appointments") or {}).get("items")
    if appts:
        add("THERANEST APPOINTMENT HISTORY", "\n".join(f"{a['date']}: {a['status']}"
                                                        + (f" ({a['code']})" if a.get("code") else "")
                                                        + ("" if a["counted"] else " — not counted")
                                                        for a in appts))

    # documents, ranked: notes first (newest first), then overviews and transcript excerpts that
    # match the question; a date or session number in the question pulls that session in
    cands = []
    for day, records in groups:
        num = day_numbers(records).get(day)
        for label, key, _sid, d in day_docs(records):
            text = d.get(key) or ""
            if not text.strip():
                continue
            chunks = [text] if key != "transcript" else [text[i:i + 3000] for i in range(0, len(text), 3000)]
            for ci, chunk in enumerate(chunks):
                low = chunk.lower()
                score = sum(low.count(w) for w in words)
                focus = (want_day and day == want_day) or (want_num is not None and num == want_num)
                base = {"note": 3, "overview": 1, "transcript": 0}[key]
                cands.append({"day": day, "num": num, "label": label + (f" (part {ci + 1})" if len(chunks) > 1 else ""),
                              "key": key, "text": chunk, "rank": (1000 if focus else 0) + score * 5 + base,
                              "recent": day})
    picked = []
    room = budget - used - 4000                       # leave space for the manual + conversation
    notes = sorted([c for c in cands if c["key"] == "note"], key=lambda c: c["recent"], reverse=True)
    focused = [c for c in cands if c["rank"] >= 1000]
    others = sorted([c for c in cands if c["key"] != "note" and c["rank"] < 1000],
                    key=lambda c: (c["rank"], c["recent"]), reverse=True)
    for c in focused + notes + [c for c in others if c["rank"] > 1]:
        if c in picked:
            continue
        size = len(c["text"]) + 80
        if size > room:
            continue
        picked.append(c)
        room -= size
    for c in sorted(picked, key=lambda c: c["day"]):
        head = f"{fmt_date(c['day'])}" + (f" · Session {c['num']}" if c["num"] is not None else "") + f" · {c['label']}"
        add(head, c["text"])
    n_notes = sum(1 for c in picked if c["key"] == "note")
    n_ov = sum(1 for c in picked if c["key"] == "overview")
    n_tr = sum(1 for c in picked if c["key"] == "transcript")
    total_notes = sum(1 for c in cands if c["key"] == "note")
    if n_notes:
        sources.append(f"{n_notes} of {total_notes} notes")
    if n_ov:
        sources.append(f"{n_ov} overview{'s' if n_ov != 1 else ''}")
    if n_tr:
        sources.append(f"{n_tr} transcript excerpt{'s' if n_tr != 1 else ''}")

    ref = reference or {}
    manual = []
    if ref.get("vocabulary"):
        manual.append("Intervention vocabulary:\n" + ref["vocabulary"])
    if ref.get("intake_points"):
        manual.append("Intake discussion points:\n" + ref["intake_points"])
    ex = (ref.get("examples") or [])[:2]
    if ex:
        manual.append("Example SSC progress notes:\n" + "\n\n".join(ex))
    manual_txt = "\n\n".join(manual)[:3500]
    convo = []
    for msg in (history or [])[-6:]:
        who = "Clinician" if msg["role"] == "user" else "Assistant"
        convo.append(f"{who}: {hide(msg['text'])[:1500]}")
    prompt = ("SSC MANUAL GUIDANCE\n" + manual_txt
              + (f"\n\nCLINICIAN'S THEORETICAL ORIENTATION: {orientation.strip()}" if (orientation or "").strip() else "")
              + "\n\nPATIENT RECORD\n" + "\n".join(parts)
              + ("\nCONVERSATION SO FAR\n" + "\n\n".join(convo) + "\n" if convo else "")
              + "\nQUESTION\n" + hide(question.strip()))
    return prompt, sources


# ---- process on the other computer (handoff through the shared, synced data folder) -------
# Everything that names a patient or carries audio is encrypted with the vault key, so Google
# Drive only ever sees ciphertext. Plain files hold nothing but computer names and times.
#   remote/hosts/<computer>.json   heartbeat: which computer is open and ready (no patient data)
#   remote/jobs/<id>.job           the request (encrypted): patient id, date, session #, type…
#   remote/jobs/<id>.audio         the recording (encrypted in chunks)
#   remote/status/<id>.st          progress, written only by the computer doing the work (encrypted)
#   remote/cancel/<id>             written by the sender to call a job off
REMOTE_DIR = "remote"
UPDATE_DIR = "app-update"   # the newest session_scribe.py, encrypted, for the other computer(s) to pick up


def build_tuple(b):
    try:
        return tuple(int(x) for x in str(b).split("."))
    except (TypeError, ValueError):
        return (0,)


def app_update_check(vault, data_dir, skip=None):
    """Keep every computer that shares this data folder on the newest Session Scribe.
    - This copy is newer than the shared one: publish it (encrypted with the vault key, so only
      someone with your password can hand an update to your other computer).
    - The shared one is newer: decrypt it, check it's complete and valid Python, return it.
    Returns {"published": build} / {"update": (build, source_text, from_computer)} / {}."""
    folder = os.path.join(data_dir, UPDATE_DIR)
    info = _read_json(os.path.join(folder, "build.json"))
    shared = info.get("build") or "0"
    if build_tuple(APP_BUILD) > build_tuple(shared):
        with open(os.path.abspath(__file__), "rb") as f:
            src = f.read()
        if f'APP_BUILD = "{APP_BUILD}"'.encode() not in src:
            return {}
        blob = vault.fernet.encrypt(src)
        os.makedirs(folder, exist_ok=True)
        tmp = os.path.join(folder, "session_scribe.enc.tmp")
        with open(tmp, "wb") as f:
            f.write(blob)
        _replace_with_retry(tmp, os.path.join(folder, "session_scribe.enc"))
        meta = {"build": APP_BUILD, "sha256": hashlib.sha256(blob).hexdigest(),
                "computer": platform.node() or "the other computer", "time": int(time.time())}
        tmp = os.path.join(folder, "build.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        _replace_with_retry(tmp, os.path.join(folder, "build.json"))
        return {"published": APP_BUILD}
    if build_tuple(shared) > build_tuple(APP_BUILD) and shared != skip:
        try:
            with open(os.path.join(folder, "session_scribe.enc"), "rb") as f:
                blob = f.read()
        except OSError:
            return {}
        if hashlib.sha256(blob).hexdigest() != info.get("sha256"):
            return {}                                   # still syncing in
        try:
            text = vault.fernet.decrypt(blob).decode("utf-8")
        except Exception:
            return {}                                   # not made with this vault's password: ignore it
        if f'APP_BUILD = "{shared}"' not in text:
            return {}
        try:
            compile(text, "session_scribe.py", "exec")
        except SyntaxError:
            return {}
        return {"update": (shared, text, info.get("computer") or "your other computer")}
    return {}


def install_app_update(text, build):
    """Replace this program file with the new version (the old one is kept in backups)."""
    target = os.path.abspath(__file__)
    bdir = os.path.join(LOCAL_DIR, "backups")
    os.makedirs(bdir, exist_ok=True)
    shutil.copy2(target, os.path.join(bdir, f"session_scribe-{APP_BUILD}.py"))
    olds = sorted((fn for fn in os.listdir(bdir) if fn.startswith("session_scribe-")),
                  key=lambda fn: build_tuple(fn[15:-3]))
    for fn in olds[:-3]:                                # keep the last three
        _quiet(os.remove, os.path.join(bdir, fn))
    tmp = target + ".new"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    _replace_with_retry(tmp, target)
REMOTE_ONLINE_SECS = 4 * 60          # a heartbeat newer than this = the app is open there
REMOTE_CHUNK = 4 << 20
REMOTE_POLL_MS = 15_000              # how often each side checks the handoff folder
REMOTE_FAST_MS = 6_000               # ...and how often while a handoff is actually in flight
REMOTE_BEAT_SECS = 60                # how often "I'm open" is refreshed
REMOTE_FINAL = ("done", "transcribed", "failed", "cancelled")


def computer_id(cfg):
    """This computer's id for handoffs: its name plus a random tag that's kept in the
    local config, so two computers with the same name can't be mixed up."""
    if not cfg.get("computer_id"):
        cfg["computer_id"] = f"{this_computer()[:40]}-{secrets.token_hex(3)}"
        try:
            save_config(cfg)
        except Exception:
            pass
    return cfg["computer_id"]


def remote_root(cfg):
    return os.path.join(data_root(cfg), REMOTE_DIR)


def _rpath(root, kind, name):
    folder = os.path.join(root, kind)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, name)


def remote_heartbeat(root, cid, info):
    """Say "Session Scribe is open here" (plus readiness) for the other computer to see."""
    data = dict(info, id=cid, computer=platform.node() or "this computer", time=int(time.time()))
    _atomic_json(_rpath(root, "hosts", cid + ".json"), data)


def remote_hosts(root, me):
    """The other computers that share this data folder, newest heartbeat first."""
    out, now = [], time.time()
    try:
        names = os.listdir(os.path.join(root, "hosts"))
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".json") or fn[:-5] == me:
            continue
        h = _read_json(os.path.join(root, "hosts", fn))
        if not h.get("id"):
            continue
        h["age"] = max(0, now - float(h.get("time", 0) or 0))
        if h["age"] > 30 * 86400:
            continue                           # a computer that hasn't opened the app in a month
        h["online"] = h["age"] < REMOTE_ONLINE_SECS
        out.append(h)
    return sorted(out, key=lambda h: h["age"])


def _encrypt_file(fernet, src, dst):
    """Encrypt a (possibly large) file in chunks. Returns (bytes written, sha256 of the original)."""
    sha, tmp = hashlib.sha256(), dst + ".tmp"
    with open(src, "rb") as fi, open(tmp, "wb") as fo:
        while True:
            block = fi.read(REMOTE_CHUNK)
            if not block:
                break
            sha.update(block)
            tok = fernet.encrypt(block)
            fo.write(len(tok).to_bytes(8, "big") + tok)
        fo.flush()
        os.fsync(fo.fileno())
    _replace_with_retry(tmp, dst)
    return os.path.getsize(dst), sha.hexdigest()


def _decrypt_file(fernet, src, dst, want_sha):
    sha = hashlib.sha256()
    try:
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            while True:
                head = fi.read(8)
                if not head:
                    break
                n = int.from_bytes(head, "big")
                tok = fi.read(n)
                if len(head) < 8 or len(tok) != n:
                    raise ValueError("The recording hasn't finished syncing yet.")
                block = fernet.decrypt(tok)
                sha.update(block)
                fo.write(block)
        if sha.hexdigest() != want_sha:
            raise ValueError("The recording arrived damaged or incomplete.")
    except Exception:
        _quiet(os.remove, dst)
        raise


def remote_send(vault, root, job, audio_path):
    """Hand a recording to the other computer: the encrypted audio first, then the job
    (so the job never shows up before its audio is complete)."""
    jid = job["id"]
    size, sha = _encrypt_file(vault.fernet, audio_path, _rpath(root, "jobs", jid + ".audio"))
    job = dict(job, audio_bytes=size, audio_sha=sha, audio_ext=os.path.splitext(audio_path)[1].lower() or ".wav",
               audio_name=os.path.basename(audio_path))
    vault._atomic_write(_rpath(root, "jobs", jid + ".job"),
                        vault.fernet.encrypt(json.dumps(job).encode("utf-8")))
    return job


def remote_jobs(vault, root):
    """Every job in the handoff folder that can be read with this vault's key."""
    out = []
    folder = os.path.join(root, "jobs")
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for fn in names:
        if fn.endswith(".job"):
            try:
                with open(os.path.join(folder, fn), "rb") as f:
                    out.append(json.loads(vault.fernet.decrypt(f.read()).decode("utf-8")))
            except Exception:
                continue                       # half-synced, or another vault's: ignore
    return sorted(out, key=lambda j: j.get("created", ""))


def remote_audio_ready(root, job):
    try:
        return os.path.getsize(os.path.join(root, "jobs", job["id"] + ".audio")) == job.get("audio_bytes")
    except OSError:
        return False


def remote_fetch_audio(vault, root, job, dst_dir):
    """Decrypt the job's recording into a local (not synced) folder; returns the path."""
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, job["id"] + re.sub(r"[^.a-z0-9]", "", job.get("audio_ext", ".wav"))[:8])
    _decrypt_file(vault.fernet, os.path.join(root, "jobs", job["id"] + ".audio"), dst, job.get("audio_sha", ""))
    return dst


_STATUS_LOCK = threading.Lock()      # one status write at a time (they share a .tmp file name)


def remote_set_status(vault, root, jid, status, **extra):
    st = dict(extra, id=jid, status=status, time=int(time.time()), build=APP_BUILD)
    data = vault.fernet.encrypt(json.dumps(st).encode("utf-8"))
    with _STATUS_LOCK:
        vault._atomic_write(_rpath(root, "status", jid + ".st"), data)


class RemoteProgressPublisher:
    """Worker side: sends a handed-over recording's live progress to the computer that sent it.

    Writes happen on this object's own thread. The transcription reads its progress from a pipe
    on the calling thread, so if that thread waited on a slow cloud-drive write, the pipe would
    fill and the transcription itself would stall. update() only stores the latest value.

    Latest value wins. A step that's still running is re-sent every `beat` seconds with when it
    started, so the sender can tell "slow" (e.g. a big model loading on a CPU) from "stuck"."""

    def __init__(self, vault, root, jid, stage, every=3.0, beat=20.0, quiet_limit=20 * 60,
                 alive=None, on_beat=None):
        self.vault, self.root, self.jid, self.stage = vault, root, jid, stage
        self.every, self.beat, self.quiet_limit = every, beat, quiet_limit
        self.alive, self.on_beat = alive or (lambda: True), on_beat
        self._lk, self._ev = threading.Lock(), threading.Event()
        self._latest, self._dirty, self._stop = None, False, False
        self._step, self._since, self._touched = None, time.time(), time.time()
        self.writes = 0
        threading.Thread(target=self._run, daemon=True).start()

    def update(self, text, pct=None):
        """Cheap and never blocks: safe to call from the progress callback."""
        with self._lk:
            now = time.time()
            if text != self._step:
                self._step, self._since = text, now
            self._latest, self._dirty, self._touched = (text, pct), True, now
        self._ev.set()

    def close(self):
        """No more writes after this returns (a write already under way finishes first)."""
        self._stop = True
        self._ev.set()

    def _run(self):
        last = 0.0
        while not self._stop:
            self._ev.wait(timeout=1.0)
            self._ev.clear()
            if self._stop:
                return
            now = time.time()
            if not self.alive() or now - self._touched > self.quiet_limit:
                return                           # task gone (cancelled / finished elsewhere): stop beating
            with self._lk:
                latest, dirty, since = self._latest, self._dirty, self._since
            if latest is None or not ((dirty and now - last >= self.every) or now - last >= self.beat):
                continue
            text, pct = latest
            with _STATUS_LOCK:                   # checked under the lock, so a final status written
                if self._stop:                   # after close() can never be overwritten by us
                    return
                try:
                    st = dict(id=self.jid, status=self.stage, time=int(now), build=APP_BUILD,
                              host=platform.node(), pct=pct, text=(text or "")[:120], step_since=int(since))
                    self.vault._atomic_write(_rpath(self.root, "status", self.jid + ".st"),
                                             self.vault.fernet.encrypt(json.dumps(st).encode("utf-8")))
                    self.writes += 1
                except Exception:
                    pass
            last = now
            with self._lk:
                if self._latest == latest:
                    self._dirty = False
            if self.on_beat:
                _quiet(self.on_beat, text, pct, now - since)


def remote_status(vault, root, jid):
    try:
        with open(os.path.join(root, "status", jid + ".st"), "rb") as f:
            return json.loads(vault.fernet.decrypt(f.read()).decode("utf-8"))
    except Exception:
        return None


def remote_cancel(root, jid):
    try:
        with open(_rpath(root, "cancel", jid), "w") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


def remote_cancelled(root, jid):
    return os.path.exists(os.path.join(root, "cancel", jid))


def remote_drop_audio(root, jid):
    p = os.path.join(root, "jobs", jid + ".audio")
    if os.path.exists(p):
        _quiet(os.remove, p)          # encrypted already; plain delete is enough


def remote_cleanup(root, jid):
    """Remove every trace of a finished job from the handoff folder."""
    for kind, name in (("jobs", jid + ".job"), ("jobs", jid + ".audio"), ("status", jid + ".st"),
                       ("cancel", jid)):
        p = os.path.join(root, kind, name)
        if os.path.exists(p):
            _quiet(os.remove, p)


def desktop_notify(title, message):
    """A system notification (Windows toast / macOS Notification Center). Never raises;
    runs in the background so a slow notifier can't freeze the app."""
    def run():
        try:
            if IS_WIN:
                from xml.sax.saxutils import escape
                xml = (f"<toast><visual><binding template=\"ToastGeneric\"><text>{escape(title)}</text>"
                       f"<text>{escape(message)}</text></binding></visual><audio src=\"ms-winsoundevent:"
                       f"Notification.Default\"/></toast>")
                ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                      "ContentType = WindowsRuntime] > $null\n"
                      "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
                      "ContentType = WindowsRuntime] > $null\n"
                      "$x = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
                      "$x.LoadXml('" + xml.replace("'", "''") + "')\n"
                      "$t = New-Object Windows.UI.Notifications.ToastNotification $x\n"
                      "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
                      "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
                      ".Show($t)\n")
                enc = base64.b64encode(ps.encode("utf-16-le")).decode()
                subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                               capture_output=True, timeout=20,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            elif IS_MAC:
                subprocess.run(["osascript", "-e", "on run argv", "-e",
                                "display notification (item 2 of argv) with title (item 1 of argv) "
                                "sound name \"Glass\"", "-e", "end run", title, message],
                               capture_output=True, timeout=20)
            elif shutil.which("notify-send"):
                subprocess.run(["notify-send", title, message], capture_output=True, timeout=20)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


def set_private_clipboard(text):
    """Put text on the clipboard so that Windows never keeps it in Clipboard History or syncs it to
    the cloud ("Sync across your devices"), and macOS keeps it on this Mac only (no Universal
    Clipboard). Returns True if it was handled here; False means "use the normal clipboard"."""
    try:
        if IS_WIN:
            return _win_private_clip(text)
        if IS_MAC:
            env = dict(os.environ, SS_CLIP=text)            # not in the command line (other users can see those)
            js = ("ObjC.import('AppKit');"
                  "var t = $.NSProcessInfo.processInfo.environment.objectForKey('SS_CLIP').js;"
                  "var pb = $.NSPasteboard.generalPasteboard;"
                  "pb.prepareForNewContentsWithOptions(1);"      # 1 = current host only: no Universal Clipboard
                  "pb.setStringForType($(t), 'public.utf8-plain-text');"
                  "pb.setStringForType($(''), 'org.nspasteboard.ConcealedType');"   # clipboard managers: don't keep
                  "pb.setStringForType($(''), 'org.nspasteboard.TransientType');")
            r = subprocess.run(["osascript", "-l", "JavaScript", "-e", js], env=env, capture_output=True, timeout=10)
            return r.returncode == 0
    except Exception:
        return False
    return False


def _win_private_clip(text):
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    GMEM_MOVEABLE, CF_UNICODETEXT = 0x0002, 13

    def put(fmt, data):
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        p = kernel32.GlobalLock(h)
        if not p:
            kernel32.GlobalFree(h)
            return False
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(fmt, h):
            kernel32.GlobalFree(h)                      # still ours if Windows didn't take it
            return False
        return True
    for _ in range(10):                                 # another program may hold the clipboard for a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        user32.EmptyClipboard()
        if not put(CF_UNICODETEXT, (text + "\0").encode("utf-16-le")):
            return False
        zero = (0).to_bytes(4, "little")
        for name, data in (("ExcludeClipboardContentFromMonitorProcessing", zero),
                           ("CanIncludeInClipboardHistory", zero), ("CanUploadToCloudClipboard", zero)):
            fmt = user32.RegisterClipboardFormatW(name)
            if fmt:
                put(fmt, data)
        return True
    finally:
        user32.CloseClipboard()


def _quiet(fn, *a):
    try:
        fn(*a)
    except Exception:
        pass


def session_sort_key(data):
    m = data.get("meta", {})
    try:
        num = int(str(m.get("session_number", "")).strip() or 0)
    except ValueError:
        num = 0
    return (m.get("session_date", ""), num, m.get("created", ""))


class Vault:
    CHECK = b"session-scribe-vault-ok"

    def __init__(self, folder):
        self.folder = folder
        self.sessions_dir = os.path.join(folder, "sessions")
        self.clients_dir = os.path.join(folder, "clients")
        self.meta_path = os.path.join(folder, "vault.json")
        self.settings_path = os.path.join(folder, "settings.enc")
        self.fernet = None
        # sid -> ((mtime_ns, size), meta): lets the app find a patient's sessions, review flags,
        # dates etc. without decrypting every session in the vault each time (only files that
        # changed — e.g. synced from the other computer — are read again)
        self._idx = {}
        self._ilock = threading.Lock()

    @property
    def exists(self):
        return os.path.exists(self.meta_path)

    @staticmethod
    def _derive(password, salt, n):
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        return base64.urlsafe_b64encode(
            Scrypt(salt=salt, length=32, n=n, r=8, p=1).derive(password.encode("utf-8")))

    def _dirs(self):
        os.makedirs(self.sessions_dir, exist_ok=True)
        os.makedirs(self.clients_dir, exist_ok=True)

    def create(self, password):
        from cryptography.fernet import Fernet
        self._dirs()
        salt, n = secrets.token_bytes(16), 2 ** 15
        key = self._derive(password, salt, n)
        meta = {"version": 2, "kdf": "scrypt", "n": n,
                "salt": base64.b64encode(salt).decode(),
                "check": Fernet(key).encrypt(self.CHECK).decode()}
        self._atomic_write(self.meta_path, json.dumps(meta).encode())
        self.fernet = Fernet(key)

    def _key_for(self, password):
        from cryptography.fernet import Fernet, InvalidToken
        with open(self.meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        key = self._derive(password, base64.b64decode(meta["salt"]), meta["n"])
        try:
            return key if Fernet(key).decrypt(meta["check"].encode()) == self.CHECK else None
        except InvalidToken:
            return None

    def unlock(self, password):
        from cryptography.fernet import Fernet
        key = self._key_for(password)
        if key:
            self.fernet = Fernet(key)
            self._dirs()
            self.migrate()
        return key is not None

    def verify(self, password):
        return self._key_for(password) is not None

    @staticmethod
    def _atomic_write(path, data):
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp, path)

    def signature(self):
        """Changes whenever a file in the vault changes (e.g. synced from the other computer)."""
        sig = []
        for d in (self.sessions_dir, self.clients_dir):
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if e.name.endswith(".enc"):
                            st = e.stat()
                            sig.append((e.name, st.st_mtime_ns, st.st_size))
            except OSError:
                pass
        try:
            st = os.stat(self.settings_path)
            sig.append(("settings", st.st_mtime_ns, st.st_size))
        except OSError:
            pass
        return tuple(sorted(sig))

    def _write(self, path, obj):
        self._atomic_write(path, self.fernet.encrypt(json.dumps(obj).encode("utf-8")))

    def _read(self, path):
        with open(path, "rb") as f:
            return json.loads(self.fernet.decrypt(f.read()).decode("utf-8"))

    # --- sessions
    def save_session(self, data, sid=None):
        sid = sid or uuid.uuid4().hex
        path = os.path.join(self.sessions_dir, sid + ".enc")
        self._write(path, data)
        self._remember(sid, path, data)
        return sid

    def _remember(self, sid, path, data):
        try:
            st = os.stat(path)
        except OSError:
            return
        with self._ilock:
            self._idx[sid] = ((st.st_mtime_ns, st.st_size), dict(data.get("meta", {})))

    def load_session(self, sid):
        return self._read(os.path.join(self.sessions_dir, sid + ".enc"))

    def session_index(self):
        """{sid: meta} for every readable session, kept current cheaply (see _idx)."""
        try:
            entries = list(os.scandir(self.sessions_dir))
        except OSError:
            return {}
        seen, stale = set(), []
        with self._ilock:
            for e in entries:
                if not e.name.endswith(".enc"):
                    continue
                sid = e.name[:-4]
                try:
                    st = e.stat()
                except OSError:
                    continue
                seen.add(sid)
                cur = self._idx.get(sid)
                if cur is None or cur[0] != (st.st_mtime_ns, st.st_size):
                    stale.append((sid, e.path))
        for sid, path in stale:                      # decrypt outside the lock
            try:
                self._remember(sid, path, self.load_session(sid))
            except Exception:
                with self._ilock:
                    self._idx.pop(sid, None)
                seen.discard(sid)
        with self._ilock:
            for sid in [k for k in self._idx if k not in seen]:
                del self._idx[sid]
            return {sid: dict(m) for sid, (_, m) in self._idx.items()}

    def all_sessions(self):
        out = []
        for fn in os.listdir(self.sessions_dir):
            if fn.endswith(".enc"):
                try:
                    d = self.load_session(fn[:-4])
                except Exception:
                    continue
                out.append((fn[:-4], d))
                self._remember(fn[:-4], os.path.join(self.sessions_dir, fn), d)
        return out

    def sessions_for(self, client_id):
        """Oldest first. Only this patient's files are decrypted."""
        s = []
        for sid, meta in self.session_index().items():
            if meta.get("client_id") == client_id:
                try:
                    s.append((sid, self.load_session(sid)))
                except Exception:
                    continue
        return sorted(s, key=lambda x: session_sort_key(x[1]))

    def delete_session(self, sid):
        wipe_file(os.path.join(self.sessions_dir, sid + ".enc"))
        with self._ilock:
            self._idx.pop(sid, None)
        self._tombstone("sessions/" + sid + ".enc")

    def _tombstone(self, rel):
        """Remember deletions so the sync check doesn't report them as 'not arrived yet'."""
        path = os.path.join(self.folder, "deleted.json")
        try:
            with open(path, encoding="utf-8") as f:
                gone = json.load(f)
        except (OSError, ValueError):
            gone = []
        if rel not in gone:
            gone.append(rel)
            try:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(gone[-5000:], f)
                _replace_with_retry(tmp, path)
            except OSError:
                pass

    # --- clients
    def save_client(self, client):
        client.setdefault("id", uuid.uuid4().hex)
        self._write(os.path.join(self.clients_dir, client["id"] + ".enc"), client)
        return client["id"]

    def load_client(self, cid):
        return self._read(os.path.join(self.clients_dir, cid + ".enc"))

    def list_clients(self):
        out = []
        for fn in os.listdir(self.clients_dir):
            if fn.endswith(".enc"):
                try:
                    out.append(self.load_client(fn[:-4]))
                except Exception:
                    continue
        return sorted(out, key=lambda c: c.get("code", "").lower())

    def delete_client(self, cid):
        for sid, _ in self.sessions_for(cid):
            self.delete_session(sid)
        wipe_file(os.path.join(self.clients_dir, cid + ".enc"))
        self._tombstone("clients/" + cid + ".enc")

    def migrate(self):
        """Attach sessions saved by v1 (no client profiles) to client records."""
        clients = {c.get("code", "").lower(): c for c in self.list_clients()}
        for sid, m in self.session_index().items():
            if m.get("client_id"):
                continue
            try:
                d = self.load_session(sid)
            except Exception:
                continue
            meta = d.setdefault("meta", {})
            code = (meta.get("client_code") or "Unassigned").strip()
            c = clients.get(code.lower())
            if not c:
                c = {"code": code, "names_to_hide": d.get("names_to_hide", ""),
                     "goals": d.get("goals", ""), "background": "",
                     "created": datetime.now().isoformat(timespec="seconds")}
                self.save_client(c)
                clients[code.lower()] = c
            meta["client_id"] = c["id"]
            self.save_session(d, sid)

    # --- settings
    def load_settings(self):
        try:
            return self._read(self.settings_path)
        except Exception:
            return {}

    def save_settings(self, settings):
        self._write(self.settings_path, settings)


# --------------------------------------------------------------------------- #
# Setup checks and installers
# --------------------------------------------------------------------------- #
def engine_installed():
    importlib.invalidate_caches()
    return importlib.util.find_spec("whisperx") is not None


def ffmpeg_installed():
    return shutil.which("ffmpeg") is not None


def hf_cache_names():
    home = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
    hub = os.environ.get("HF_HUB_CACHE") or os.path.join(home, "hub")
    try:
        return [d.lower() for d in os.listdir(hub)]
    except OSError:
        return []


def _hf_hub_dir():
    home = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
    return os.environ.get("HF_HUB_CACHE") or os.path.join(home, "hub")


def cached_whisper_models():
    """Whisper model sizes fully present in the Hugging Face cache (have model.bin)."""
    import glob
    hub, sizes = _hf_hub_dir(), set()
    for d in glob.glob(os.path.join(hub, "models--*")):
        name = os.path.basename(d).split("--")[-1].lower()
        if "whisper" not in name or not glob.glob(os.path.join(d, "snapshots", "*", "model.bin")):
            continue
        size = re.sub(r"^.*?whisper-", "", name)
        size = re.sub(r"-ct2$", "", size)
        sizes.add(size)
    return sorted(sizes)


def models_status(cfg, hf_token):
    """Per-component check: True / False / None (can't tell or not needed)."""
    model = (cfg.get("whisper_model") or "").lower()
    cached = cached_whisper_models()
    lang = (cfg.get("language") or "en").lower()
    align = None
    if lang == "en":
        torch_home = os.environ.get("TORCH_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "torch")
        ckpt = os.path.join(torch_home, "hub", "checkpoints")
        try:
            align = any(f.startswith("wav2vec2_fairseq_base_ls960_asr_ls960") for f in os.listdir(ckpt))
        except OSError:
            align = False
    speaker = speaker_model_cached() if hf_token else None
    return {"whisper": model in cached, "cached": cached, "align": align, "speaker": speaker}


def speaker_model_cached():
    """True if a complete pyannote speaker model is on disk. Newer WhisperX uses
    speaker-diarization-community-1 (one download containing everything); older
    versions use speaker-diarization-3.x plus a separate segmentation-3.0 model."""
    import glob
    hub = _hf_hub_dir()

    def has(repo_part, *patterns):
        for d in glob.glob(os.path.join(hub, "models--pyannote--*")):
            if repo_part in os.path.basename(d).lower():
                snap = os.path.join(d, "snapshots", "*")
                if all(glob.glob(os.path.join(snap, p), recursive=True) for p in patterns):
                    return True
        return False
    weights = "**/*.bin"
    community = has("speaker-diarization-community", "config.yaml") and (
        has("speaker-diarization-community", weights) or has("speaker-diarization-community", "**/*.safetensors"))
    legacy = has("speaker-diarization-3", "config.yaml") and has("segmentation-3", weights)
    return community or legacy


def models_cached():
    names = hf_cache_names()
    return (bool(cached_whisper_models()), any("pyannote" in n for n in names))


def run_streamed(cmd, log, env=None):
    """Installers and the model download (no patient data): they run with the computer's normal
    internet settings, proxy included."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace", env=env or dict(ONLINE_ENV, PATH=os.environ["PATH"]))
    for line in p.stdout:
        if line.strip():
            log(line.rstrip())
    p.wait()
    return p.returncode


WHISPERX_PY_MIN, WHISPERX_PY_MAX = (3, 10), (3, 13)


def python_supported(ver=None):
    ver = tuple(ver or sys.version_info[:2])
    return WHISPERX_PY_MIN <= ver <= WHISPERX_PY_MAX


def find_compatible_pythons():
    """Other Python installs on this computer that WhisperX supports.
    Returns [(path, (major, minor), has_whisperx)], best first."""
    paths = []
    if os.name == "nt":
        try:
            out = subprocess.run(["py", "-0p"], capture_output=True, text=True, timeout=10).stdout
            paths += re.findall(r"([A-Za-z]:\\[^\r\n*]*?python\.exe)", out, flags=re.I)
        except Exception:
            pass
        import glob
        la = os.environ.get("LOCALAPPDATA", "")
        for pat in (os.path.join(la, "Programs", "Python", "Python3*", "python.exe"),
                    os.path.join(la, "Python", "pythoncore-3.*", "python.exe"),
                    r"C:\Python3*\python.exe", r"C:\Program Files\Python3*\python.exe"):
            paths += glob.glob(pat)
    else:
        for v in ("3.13", "3.12", "3.11", "3.10"):
            p = shutil.which("python" + v)
            if p:
                paths.append(p)
            for cand in (f"/Library/Frameworks/Python.framework/Versions/{v}/bin/python{v}",
                         f"/opt/homebrew/bin/python{v}", f"/usr/local/bin/python{v}"):
                if os.path.isfile(cand):
                    paths.append(cand)
    found, seen = [], {os.path.normcase(os.path.abspath(sys.executable))}
    for p in paths:
        key = os.path.normcase(os.path.abspath(p.strip()))
        if key in seen or not os.path.isfile(key):
            continue
        seen.add(key)
        info = probe_python(p.strip())
        if info and python_supported(info[0]):
            found.append((p.strip(), info[0], info[1]))
    found.sort(key=lambda x: (not x[2], (-x[1][0], -x[1][1])))
    return found


def probe_python(py):
    """(version, has_whisperx, has_cryptography) for another Python, or None."""
    code = ("import sys,importlib.util as u;print(sys.version_info[0],sys.version_info[1],"
            "int(u.find_spec('whisperx') is not None),int(u.find_spec('cryptography') is not None))")
    try:
        out = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=30).stdout.split()
        return (int(out[0]), int(out[1])), out[2] == "1", out[3] == "1"
    except Exception:
        return None


def _gui_python(py):
    """pythonw.exe next to python.exe on Windows (no console window)."""
    if os.name == "nt":
        pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
        if os.path.isfile(pyw):
            return pyw
    return py


RELAUNCH_FLAG = "SESSION_SCRIBE_RELAUNCHED"


def relaunch_if_needed():
    """If this Python can't run WhisperX (e.g. Python 3.14) but another Python on
    the computer can, reopen the app with that one — automatically, every time.
    The choice is remembered in config.json so later starts are quick."""
    if os.environ.get(RELAUNCH_FLAG) or DOWNLOAD_MODE:
        return
    if python_supported() and engine_installed():
        return
    cfg = load_config()
    target = None
    saved = cfg.get("python_path") or ""
    if saved and os.path.isfile(saved):
        info = probe_python(saved)
        if info and python_supported(info[0]) and info[1]:
            target = (saved, info)
    if target is None:
        for py, ver, has in find_compatible_pythons():
            if has:
                target = (py, probe_python(py))
                break
    if not target or not target[1]:
        return                       # nothing better available: Setup explains what to do
    py, (ver, has_wx, has_crypto) = target
    if not has_crypto:               # the one small package the app itself needs
        subprocess.run([py, "-m", "pip", "install", "-q", "cryptography"], capture_output=True,
                       env=dict(ONLINE_ENV, PATH=os.environ["PATH"]))
    if cfg.get("python_path") != py:
        cfg["python_path"] = py
        try:
            save_config(cfg)
        except OSError:
            pass
    env = dict(ONLINE_ENV, PATH=os.environ["PATH"], **{RELAUNCH_FLAG: "1"})
    subprocess.Popen([_gui_python(py), os.path.abspath(__file__)] + sys.argv[1:], env=env)
    sys.exit(0)


def write_mac_app(script_path, runner):
    """~/Applications/Session Scribe.app — a double-clickable app that starts the program
    with the right Python (no Terminal window)."""
    app = os.path.join(os.path.expanduser("~"), "Applications", "Session Scribe.app")
    macos = os.path.join(app, "Contents", "MacOS")
    os.makedirs(macos, exist_ok=True)
    with open(os.path.join(app, "Contents", "Info.plist"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>'
                '<key>CFBundleName</key><string>Session Scribe</string>'
                '<key>CFBundleDisplayName</key><string>Session Scribe</string>'
                '<key>CFBundleIdentifier</key><string>local.session-scribe</string>'
                '<key>CFBundleExecutable</key><string>Session Scribe</string>'
                '<key>CFBundlePackageType</key><string>APPL</string>'
                '<key>CFBundleShortVersionString</key><string>1.0</string>'
                '<key>LSMinimumSystemVersion</key><string>11.0</string>'
                '<key>NSHighResolutionCapable</key><true/></dict></plist>\n')
    exe = os.path.join(macos, "Session Scribe")
    with open(exe, "w", encoding="utf-8") as f:
        f.write(f'#!/bin/bash\nexec "{runner}" "{script_path}"\n')
    os.chmod(exe, 0o755)
    return app


def write_launcher(folder, runner):
    """Write the double-click launcher next to the program in `folder`."""
    script = os.path.basename(os.path.abspath(__file__))
    if IS_MAC:
        path = os.path.join(folder, "Session Scribe.command")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f'#!/bin/bash\ncd "$(dirname "$0")"\nexec "{runner}" "{script}"\n')
        os.chmod(path, 0o755)
        try:
            write_mac_app(os.path.join(folder, script), runner)
        except OSError:
            pass
        return path
    if os.name == "nt":
        path = os.path.join(folder, "Session Scribe.bat")
        with open(path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(f'@echo off\nstart "" "{runner}" "%~dp0{script}"\n')
    else:
        path = os.path.join(folder, "session_scribe.sh")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\nexec "{runner}" "$(dirname "$0")/{script}"\n')
        os.chmod(path, 0o755)
    return path


def default_program_dir():
    return os.path.join(os.path.expanduser("~"), "Session Scribe")


def in_downloads(path):
    p = os.path.normcase(os.path.abspath(path))
    dl = os.path.normcase(os.path.join(os.path.expanduser("~"), "Downloads"))
    return p == dl or p.startswith(dl + os.sep)


def _inside(path, folder):
    p, f = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(folder))
    return p == f or p.startswith(f + os.sep)


def shortcut_exists():
    """Is there already a Session Scribe shortcut/app on this computer?"""
    home = os.path.expanduser("~")
    if IS_MAC:
        return os.path.isdir(os.path.join(home, "Applications", "Session Scribe.app"))
    if IS_WIN:
        for desk in (os.path.join(home, "Desktop"), os.path.join(home, "OneDrive", "Desktop"),
                     os.path.join(os.environ.get("PUBLIC", ""), "Desktop")):
            if os.path.isfile(os.path.join(desk, "Session Scribe.lnk")):
                return True
        return False
    return os.path.isfile(os.path.join(APP_DIR, "session_scribe.sh"))


def desktop_shortcut(launcher, log=lambda t: None):
    """Put a 'Session Scribe' shortcut on the desktop (Windows: .lnk; Mac: link to the app)."""
    if IS_MAC:
        app = os.path.join(os.path.expanduser("~"), "Applications", "Session Scribe.app")
        link = os.path.join(os.path.expanduser("~"), "Desktop", "Session Scribe.app")
        try:
            if not os.path.isdir(app):
                write_mac_app(os.path.join(os.path.dirname(launcher), os.path.basename(os.path.abspath(__file__))),
                              _gui_python(load_config().get("python_path") or sys.executable))
            if os.path.islink(link) or os.path.exists(link):
                os.remove(link)
            os.symlink(app, link)
            return link
        except OSError as e:
            log(f"Couldn't make the desktop shortcut ({e}).")
            return ""
    if os.name != "nt":
        return ""
    ps = ("$d=[Environment]::GetFolderPath('Desktop');"
          "$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Session Scribe.lnk'));"
          f"$s.TargetPath='{launcher}';$s.WorkingDirectory='{os.path.dirname(launcher)}';"
          "$s.WindowStyle=7;$s.Save();Write-Output (Join-Path $d 'Session Scribe.lnk')")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                             capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.stdout.strip()
    except Exception as e:
        log(f"Couldn't make the desktop shortcut ({e}).")
        return ""


def move_program(cfg, dst, shortcut=True, log=lambda t: None, data_target=None):
    """Copy the program (script, FFmpeg in bin/, launcher) to `dst`. If the patient data also
    lives in the old program folder it is moved too (checked, old copy kept as a dated backup).
    Old program files are removed only after the new copy is verified. Returns a summary dict."""
    src = APP_DIR
    dst = os.path.normpath(os.path.abspath(dst))
    if os.path.normcase(dst) == os.path.normcase(src):
        raise RuntimeError("That's already where the program is.")
    if _inside(dst, src):
        raise RuntimeError("Choose a folder outside the current program folder.")
    if is_cloud_path(dst):
        raise RuntimeError("Keep the program itself on this computer (not in Google Drive) — only the "
                           "shared data folder should be in Drive.")
    os.makedirs(dst, exist_ok=True)
    script = os.path.basename(os.path.abspath(__file__))
    new_script = os.path.join(dst, script)
    log("Copying the program…")
    shutil.copy2(os.path.abspath(__file__), new_script)
    if os.path.isdir(BIN_DIR):
        log("Copying FFmpeg…")
        shutil.copytree(BIN_DIR, os.path.join(dst, "bin"), dirs_exist_ok=True)
    with open(os.path.abspath(__file__), "rb") as a, open(new_script, "rb") as b:
        if a.read() != b.read():
            raise RuntimeError("The copied program didn't match the original. Nothing was removed.")
    result = {"dir": dst, "script": new_script, "data_moved": "", "backup": "", "shortcut": "", "left": []}

    # Patient data that lives inside the old program folder moves with it
    droot = data_root(cfg)
    if _inside(droot, src) and folder_has_vault(droot):
        rel = os.path.relpath(droot, src)
        new_root = os.path.normpath(data_target) if data_target else os.path.join(dst, "data" if rel == "." else rel)
        if folder_has_vault(new_root):
            # The Google Drive folder already has the shared files (e.g. from the other computer):
            # use those, and set the local copy aside as a backup instead of mixing the two.
            log("Google Drive already has Session Scribe files — using those.")
            backup = os.path.join(droot, f"vault-backup-{date.today().isoformat()}")
            i = 2
            while os.path.exists(backup):
                backup = os.path.join(droot, f"vault-backup-{date.today().isoformat()}-{i}")
                i += 1
            os.rename(os.path.join(droot, "vault"), backup)
            result["backup"] = backup
            result["joined_existing"] = True
        else:
            log("Moving patient data (encrypted)…")
            result["backup"] = move_data(droot, new_root, log)
        result["data_moved"] = new_root
        for name in ("templates", SHARED_SETTINGS):          # copied and checked; not patient data
            p = os.path.join(droot, name)
            try:
                shutil.rmtree(p) if os.path.isdir(p) else (os.path.isfile(p) and os.remove(p))
            except OSError:
                pass
        cfg["data_dir"] = new_root
        cfg["vault_dir"] = ""
        cfg["_vault_derived"] = True
        set_data_paths(cfg)
        save_config(cfg)
    elif _inside(droot, src):          # empty/new data folder inside the program folder: just repoint
        cfg["data_dir"] = os.path.join(dst, "data")
        cfg["vault_dir"] = ""
        cfg["_vault_derived"] = True
        set_data_paths(cfg)
        save_config(cfg)

    # The data folder link (e.g. H:\My Drive\Session Scribe) lives in this computer's own settings
    # (%LOCALAPPDATA%), not in the program folder, so it carries over untouched. Confirm it.
    after = load_config()
    if os.path.normcase(after.get("data_dir") or "") != os.path.normcase(cfg.get("data_dir") or ""):
        raise RuntimeError("The data folder setting didn't carry over — the old program was left in place.")
    result["data_dir"] = cfg.get("data_dir", "")
    result["data_synced"] = is_cloud_path(result["data_dir"])
    result["data_reachable"] = folder_has_vault(result["data_dir"]) if result["data_dir"] else False

    runner = _gui_python(cfg.get("python_path") or sys.executable)
    launcher = write_launcher(dst, runner)
    result["launcher"] = launcher
    if shortcut:
        result["shortcut"] = desktop_shortcut(launcher, log)

    # Remove the old program files (never patient data — that's kept as a backup above)
    for name in (script, "Session Scribe.bat", "Session Scribe.command", "session_scribe.sh", "bin", "__pycache__"):
        p = os.path.join(src, name)
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)
            elif os.path.isfile(p):
                os.remove(p)
        except OSError:
            result["left"].append(p)
    if os.path.isfile(LEGACY_CONFIG_PATH):
        try:
            os.remove(LEGACY_CONFIG_PATH)       # already copied to this computer's settings
        except OSError:
            pass
    for name in os.listdir(src) if os.path.isdir(src) else []:
        if name.startswith("vault-backup-") or name in ("vault", "templates", "data"):
            result["left"].append(os.path.join(src, name))
    log("Done.")
    return result


def create_launcher(py, ver, has_whisperx, log):
    """Prepare a compatible Python and write a double-click launcher that uses it."""
    v = f"{ver[0]}.{ver[1]}"
    log(f"Preparing Python {v}: {py}")
    pkgs = ["cryptography"] + ([] if has_whisperx else ["whisperx"])
    if not has_whisperx:
        log("WhisperX isn't in that Python yet — installing it too (large download, can take 10+ minutes)…")
    if run_streamed([py, "-m", "pip", "install", "--upgrade"] + pkgs, log):
        raise RuntimeError(f"pip couldn't install {', '.join(pkgs)} into Python {v} — see the log above.")
    runner = _gui_python(py)
    cfg = load_config()
    cfg["python_path"] = py
    save_config(cfg)
    path = write_launcher(APP_DIR, runner)
    return {"launcher": path, "runner": runner, "version": v}


def install_engine(log):
    if not python_supported():
        raise RuntimeError(f"WhisperX doesn't support Python {sys.version_info[0]}.{sys.version_info[1]} "
                           "(it needs 3.10–3.13). Use the \"Use Python 3.x\" button instead.")
    log(f"Installing WhisperX into: {sys.executable}")
    log("This downloads ~2-3 GB (PyTorch and friends) and can take 10+ minutes…")
    code = run_streamed([sys.executable, "-m", "pip", "install", "--upgrade", "whisperx"], log)
    if code != 0:
        raise RuntimeError("pip couldn't install WhisperX — see the log above.")
    if not engine_installed():
        raise RuntimeError("Install finished but WhisperX still isn't visible. Restart the app.")
    return "WhisperX installed."


def install_ffmpeg(log):
    log("Installing a self-contained FFmpeg…")
    if run_streamed([sys.executable, "-m", "pip", "install", "--upgrade", "imageio-ffmpeg"], log):
        raise RuntimeError("pip couldn't install imageio-ffmpeg.")
    out = subprocess.run([sys.executable, "-c",
                          "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"],
                         capture_output=True, text=True)
    src = out.stdout.strip()
    if not src or not os.path.isfile(src):
        raise RuntimeError("Couldn't locate the FFmpeg program after installing.")
    os.makedirs(BIN_DIR, exist_ok=True)
    shutil.copy2(src, os.path.join(BIN_DIR, "ffmpeg.exe" if os.name == "nt" else "ffmpeg"))
    return "FFmpeg installed in the app's bin folder."


def download_models(cfg, hf_token, log):
    env = {k: v for k, v in dict(ONLINE_ENV, PATH=os.environ["PATH"]).items()
           if k not in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE")}
    if hf_token:
        env["HF_TOKEN"] = hf_token   # passed via environment, not the command line
    cmd = [sys.executable, os.path.abspath(__file__), "--download-models",
           "--whisper-model", cfg["whisper_model"], "--language", cfg["language"] or "en"]
    if run_streamed(cmd, log, env=env):
        raise RuntimeError("Model download failed — see the log. For speaker labels, accept the "
                           "pyannote model terms on huggingface.co with the same account as your token.")
    return "Models downloaded. Transcription now works fully offline."


def download_models_main():
    args = sys.argv
    model = args[args.index("--whisper-model") + 1] if "--whisper-model" in args else "large-v3-turbo"
    lang = args[args.index("--language") + 1] if "--language" in args else "en"
    print("Downloading models. No patient data is used in this step.", flush=True)
    try:
        import torch
        import whisperx
        device = "cuda" if torch.cuda.is_available() else "cpu"
        compute = "float16" if device == "cuda" else "int8"
        print(f"• Speech model ({model})…", flush=True)
        del_m = whisperx.load_model(model, device, compute_type=compute, language=lang or None)
        del del_m
        print(f"• Word-alignment model ({lang or 'en'})…", flush=True)
        whisperx.load_align_model(language_code=lang or "en", device=device)
        token = os.environ.get("HF_TOKEN")
        if token:
            print("• Speaker-identification model…", flush=True)
            DP = getattr(whisperx, "DiarizationPipeline", None)
            if DP is None:
                from whisperx.diarize import DiarizationPipeline as DP
            try:
                DP(use_auth_token=token, device=device)
            except TypeError:
                DP(token=token, device=device)
        else:
            print("• No Hugging Face token saved — skipping the speaker model.", flush=True)
        print("DONE", flush=True)
        return 0
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}", flush=True)
        return 1


# --------------------------------------------------------------------------- #
# Documents: text extraction, agency manual reference, past-note import
# --------------------------------------------------------------------------- #
IMPORT_EXTS = (".txt", ".md", ".docx", ".pdf")


def pdf_support():
    importlib.invalidate_caches()
    return importlib.util.find_spec("pypdf") is not None


def install_pdf_support(log):
    if run_streamed([sys.executable, "-m", "pip", "install", "--upgrade", "pypdf"], log):
        raise RuntimeError("pip couldn't install pypdf.")
    return "PDF support installed."


def extract_text(path):
    """Plain text from .txt/.md/.docx/.pdf — all read locally."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        import html
        import zipfile
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
        paras = []
        for p in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S):
            p = re.sub(r"<w:tab/>", "\t", p)
            # page breaks become form feeds, so multi-page documents can be reassembled
            p = re.sub(r"<w:br[^>]*w:type=\"page\"[^>]*/>|<w:lastRenderedPageBreak/>", "<w:t>\f</w:t>", p)
            p = re.sub(r"<w:br[^>]*/>", "\n", p)
            paras.append(html.unescape("".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, flags=re.S))))
        return _normalize("\n".join(paras))
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise RuntimeError("Reading PDFs needs a small add-on: click Settings → Install next to "
                               "\"PDF support\". (Or use a .docx / .txt copy.)") from None
        pages = []
        for pg in PdfReader(path).pages:
            try:   # layout mode keeps lines intact; plain mode can put one word per line
                txt = pg.extract_text(extraction_mode="layout") or ""
            except TypeError:
                txt = pg.extract_text() or ""
            pages.append(txt)
        return _normalize("\f".join(pages))
    with open(path, encoding="utf-8", errors="replace") as f:
        return _normalize(f.read())


def _normalize(text):
    text = re.sub(r"[​‌‍﻿ ]", lambda m: " " if m.group() == " " else "", text)
    text = text.replace("’", "'").replace("“", '"').replace("”", '"')
    pages = text.split("\f")                       # keep page boundaries
    return "\f".join("\n".join(re.sub(r"[ \t]{2,}", " ", line).rstrip() for line in pg.split("\n"))
                     for pg in pages)


def _clean_lines(s):
    out = []
    for line in s.splitlines():
        line = line.strip().replace("\\.", ".")
        if not line or re.fullmatch(r"\d{1,3}", line) or "SOUTH SHORE COUNSELING" in line.upper() \
                or line.lower().startswith("feel free to print"):
            continue
        out.append(line)
    return out


def _section(text, start_pat, end_pats, max_len=9000):
    """Text after the first heading match that isn't a table-of-contents entry."""
    for m in re.finditer(start_pat, text, flags=re.I):
        line_rest = text[m.end():text.find("\n", m.end()) if "\n" in text[m.end():] else len(text)]
        if "(page" in line_rest.lower():
            continue
        rest = text[m.end():]
        end = len(rest)
        for ep in end_pats:
            e = re.search(ep, rest, flags=re.I)
            if e and e.start() < end:
                end = e.start()
        chunk = rest[:end][:max_len]
        if len(chunk.strip()) > 40:
            return chunk
    return ""


def parse_reference(text):
    """Pull the example progress notes, intervention vocabulary and intake discussion
    points out of the agency manual's text. Nothing leaves the computer."""
    ex = _section(text, r"progress note examples", [r"examples? of keywords", r"common (intervention )?"
                                                    r"documentation terms", r"theranest guidelines"])
    lines = _clean_lines(ex)
    joined = "\n".join(lines)
    items = [re.sub(r"\s+", " ", x).strip() for x in re.split(r"(?:^|\n)\s*\d{1,2}\.\s+", joined)]
    items = [x for x in items if len(x) > 80]
    if len(items) < 2:                                   # unnumbered: one example per paragraph
        items, cur = [], ""
        for line in lines:
            cur = (cur + " " + line).strip()
            if re.search(r"[.!?\"”]$", line) and len(cur) > 150:
                items.append(cur)
                cur = ""
        if len(cur) > 80:
            items.append(cur)
    vocab = _section(text, r"examples? of keywords[^\n]*", [r"theranest guidelines", r"how to write a treatment plan"],
                     max_len=5000)
    points = _section(text, r"intake discussion points", [r"administrative reminders", r"phone call script",
                                                          r"\bsafety plan\b\s*\n\s*\n", r"registration form"],
                      max_len=2500)
    return {"examples": items[:12], "vocabulary": "\n".join(_clean_lines(vocab)),
            "intake_points": "\n".join(_clean_lines(points))}


def style_block(ref, kind):
    if not ref:
        return ""
    exs = voc = pts = ""
    if kind == "progress" and ref.get("examples"):
        exs = "\nEXAMPLE PROGRESS NOTES:\n" + "\n\n".join(
            f"Example {i}: {e}" for i, e in enumerate(ref["examples"][:9], 1)) + "\n"
    if kind in ("progress", "intake") and ref.get("vocabulary"):
        voc = f"\nAPPROVED INTERVENTION VOCABULARY:\n{ref['vocabulary'][:4000]}\n"
    if kind == "intake" and ref.get("intake_points"):
        pts = f"\nINTAKE DISCUSSION POINTS:\n{ref['intake_points'][:2000]}\n"
    if not (exs or voc or pts):
        return ""
    return fill(STYLE_HEADER, {"examples": exs, "vocabulary": voc, "intake_points": pts})


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                       "sep", "oct", "nov", "dec"], 1)}


# ---------------------------------------------------------------- date format
# Dates are always stored as YYYY-MM-DD; this only changes how they're shown and typed.
DATE_FORMATS = {"iso": "YYYY-MM-DD", "us": "MM/DD/YYYY", "eu": "DD/MM/YYYY", "long": "Nov 7, 2026"}
_DATE_PREF = {"fmt": "iso"}
_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MON_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def set_date_format(fmt):
    _DATE_PREF["fmt"] = fmt if fmt in DATE_FORMATS else "iso"


def date_hint():
    """What to type, for messages: e.g. 'MM/DD/YYYY'."""
    return {"iso": "YYYY-MM-DD", "us": "MM/DD/YYYY", "eu": "DD/MM/YYYY", "long": "Nov 7, 2026"}[_DATE_PREF["fmt"]]


def _fmt_ymd(y, m, d):
    f = _DATE_PREF["fmt"]
    if f == "us":
        return f"{m:02d}/{d:02d}/{y:04d}"
    if f == "eu":
        return f"{d:02d}/{m:02d}/{y:04d}"
    if f == "long" and 1 <= m <= 12:
        return f"{_MON_ABBR[m - 1]} {d}, {y}"
    return f"{y:04d}-{m:02d}-{d:02d}"


def fmt_date(s):
    """'2026-11-07' (or '2026-11-07T15:19…') in the chosen format; anything else unchanged."""
    if not s:
        return s or ""
    s = str(s)
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        return s
    rest = s[10:]
    if rest[:1] == "T":                       # timestamp: keep just the date
        rest = ""
    return _fmt_ymd(int(m[1]), int(m[2]), int(m[3])) + rest


def fmt_dates_in(text):
    """Every YYYY-MM-DD inside a piece of text, in the chosen format."""
    if _DATE_PREF["fmt"] == "iso" or not text:
        return text
    return _ISO_RE.sub(lambda m: _fmt_ymd(int(m[1]), int(m[2]), int(m[3])), text)


def parse_date(s):
    """A date typed in any common format -> 'YYYY-MM-DD', or '' if it isn't a real date.
    Ambiguous 03/04/2026 follows the chosen format (day first only for DD/MM/YYYY)."""
    s = (s or "").strip()
    if not s:
        return ""
    y = mo = d = None
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    else:
        m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})", s)
        if m:
            a, b, y = int(m[1]), int(m[2]), int(m[3]) + (2000 if len(m[3]) == 2 else 0)
            mo, d = (b, a) if _DATE_PREF["fmt"] == "eu" else (a, b)
            if mo > 12 >= d:                   # 25/12/2026 can only mean one thing
                mo, d = d, mo
        else:
            m = re.fullmatch(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", s)
            if m and m[1].lower() in _MONTHS:
                y, mo, d = int(m[3]), _MONTHS[m[1].lower()], int(m[2])
            else:
                m = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})", s)
                if m and m[2].lower() in _MONTHS:
                    y, mo, d = int(m[3]), _MONTHS[m[2].lower()], int(m[1])
    if y is None:
        return ""
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return ""


def find_date(s):
    """First date in the text as YYYY-MM-DD, or ''."""
    best = None
    for m in re.finditer(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", s):
        cand = (m.start(), (int(m[1]), int(m[2]), int(m[3])))
        best = cand if best is None or cand[0] < best[0] else best
    for m in re.finditer(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})\b", s):
        y = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
        cand = (m.start(), (y, int(m[1]), int(m[2])))
        best = cand if best is None or cand[0] < best[0] else best
    for m in re.finditer(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b",
                         s, flags=re.I):
        cand = (m.start(), (int(m[3]), _MONTHS[m[1].lower()[:3]], int(m[2])))
        best = cand if best is None or cand[0] < best[0] else best
    if best and best[1]:
        y, mo, d = best[1]
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return ""


def guess_kind(text):
    head = text[:400].lower()
    return "intake" if any(k in head for k in ("intake", "initial exam", "diagnostic impression",
                                               "presenting problem")) else "progress"


def guess_session_number(text):
    m = re.search(r"session\s*(?:#|no\.?|number)?\s*(\d{1,3})\b", text[:300], flags=re.I)
    return m[1] if m else ""


# TheraNest exports: each document starts with one of these headings, and a document
# that runs over several pages repeats its heading (and header block) on every page.
EHR_HEADERS = [
    ("intake", re.compile(r"^\s*INITIAL\s+ASSESSMENT\s+AND\s+DIAGNOSTIC\s+IMPRESSIONS?\b", re.I)),
    ("plan", re.compile(r"^\s*MUTUAL\s+TREATMENT\s+PLAN\b", re.I)),
    ("progress", re.compile(r"^\s*PROGRESS\s+NOTES?\b", re.I)),
]
# which "…Date:" label dates each kind of document (first match wins); DOB etc. never count
DATE_LABELS = {
    "progress": ["service", "session", "appointment", ""],
    "plan": ["", "plan", "start", "created", "creation"],
    "intake": ["intake", "assessment", "service", "evaluation", "session", ""],
}
NOT_A_VISIT_DATE = ("birth", "dob", "review", "target", "end", "sign", "due", "discharge", "expir", "next")
PAGE_ARTIFACT = re.compile(r"^\s*(page\s+\d+(\s*(of|/)\s*\d+)?|\d+\s*(of|/)\s*\d+)\s*$", re.I)


def header_kind(line):
    if len(line.strip()) > 90:                 # a heading, not a sentence that mentions it
        return None
    for kind, rx in EHR_HEADERS:
        if rx.search(line):
            return kind
    return None


def labeled_date(text, kind):
    """The document's own date, e.g. 'Service Date: 04/01/2026' for a progress note."""
    found = []                                  # (label prefix, iso date)
    for line in text.split("\n")[:60]:
        for m in re.finditer(r"([A-Za-z][A-Za-z /]{0,25}?)?\bdate\s*(?:of\s+(\w+)\s*)?[:\-]\s*([^\n]{4,40})",
                             line, flags=re.I):
            prefix = ((m.group(1) or "") + " " + (m.group(2) or "")).strip().lower()
            if any(bad in prefix for bad in NOT_A_VISIT_DATE):
                continue
            iso = find_date(m.group(3))
            if iso:
                found.append((prefix, iso))
    for want in DATE_LABELS.get(kind, [""]):
        for prefix, iso in found:
            if (want == "" and prefix in ("", "the")) or (want and want in prefix):
                return iso
    if found:
        return found[0][1]
    for line in text.split("\n"):              # last resort: first date not on a birth-date line
        if not any(bad in line.lower() for bad in ("birth", "dob")):
            iso = find_date(line)
            if iso:
                return iso
    return ""


def parse_ehr_export(text):
    """Split a TheraNest bulk export into documents. Returns None if the text doesn't
    use the TheraNest headings (so the generic splitter is used instead)."""
    text = text.replace("\r\n", "\n")
    if not any(header_kind(l) for l in text.split("\n")):
        return None
    # 1) pages (form feeds) — or, for pasted text, start a new piece at every heading
    from collections import Counter
    counts = Counter(l.strip() for l in text.split("\n") if l.strip())
    # letterhead-style lines repeated on every page (they belong with the heading below them)
    repeated = {l for l, n in counts.items() if n >= 3 and len(l) < 90 and not header_kind(l)}
    pieces = []
    for page in text.split("\f"):
        cur = []
        for line in page.split("\n"):
            if header_kind(line) and any(header_kind(x) for x in cur):   # another heading on this page
                carry = []
                while cur and (not cur[-1].strip() or cur[-1].strip() in repeated):
                    carry.insert(0, cur.pop())
                pieces.append("\n".join(cur))
                cur = carry
            cur.append(line)
        pieces.append("\n".join(cur))
    # 2) classify each piece
    segs = []
    for pc in pieces:
        lines = [l for l in pc.split("\n") if not PAGE_ARTIFACT.match(l)]
        if not any(l.strip() for l in lines):
            continue
        kind = next((header_kind(l) for l in lines[:25] if header_kind(l)), None)
        body = "\n".join(lines).strip()
        segs.append({"kind": kind, "date": labeled_date(body, kind) if kind else "", "text": body})
    # 3) glue pages of the same document back together
    docs = []
    for sg in segs:
        prev = docs[-1] if docs else None
        same = prev is not None and (sg["kind"] is None or (
            sg["kind"] == prev["kind"] and (not sg["date"] or sg["date"] == prev["date"])))
        if same:
            top = {l.strip() for l in prev["text"].split("\n")[:20] if l.strip()}
            lines = sg["text"].split("\n")
            i = 0                                   # drop the repeated page header block
            while i < len(lines) and i < 20 and (not lines[i].strip() or lines[i].strip() in top
                                                 or header_kind(lines[i])):
                i += 1
            rest = "\n".join(lines[i:]).strip()
            if rest:
                prev["text"] += "\n" + rest
        elif sg["kind"] is None and not docs:
            if len(sg["text"]) > 200:               # substantial text before any heading
                docs.append({"kind": guess_kind(sg["text"]), "date": find_date(sg["text"][:300]),
                             "text": sg["text"]})
        else:
            docs.append(dict(sg))
    return [{"date": d["date"], "number": guess_session_number(d["text"]) if d["kind"] == "progress" else "",
             "kind": d["kind"], "text": tidy_ehr_note(d["text"], d["kind"]), "ehr": True}
            for d in docs if len(d["text"]) >= 20]


# ---- tidying imported TheraNest notes ------------------------------------------------
MSE_FIELDS = ["Thought Process", "Thought Content", "Appearance", "Orientation", "Behaviour", "Behavior",
              "Speech", "Affect", "Mood", "Perception", "Judgement", "Judgment", "Insight", "Appetite", "Sleep"]
MSE_LABEL = re.compile(r"\b(" + "|".join(f.replace(" ", r"\s+") for f in MSE_FIELDS) + r")\s*:", re.I)
MSE_START = re.compile(r"^\s*(" + "|".join(f.replace(" ", r"\s+") for f in MSE_FIELDS) + r")\s*:", re.I)
FIELD_LINE = re.compile(r"^\s*[A-Z][A-Za-z0-9 /()&'#.,-]{1,45}:(\s|$)")
LETTERHEAD = re.compile(r"south\s+shore\s+counseling", re.I)
INSURER = re.compile(r"Insurer\(?s?\)?\s*:.*?(?=\b(?:Place\s+of\s+Service|CPT\s+Code|Case\s*#|Provider\(s\)|"
                     r"Service\s+Date|Session\s+Start)\b|$)", re.I)
STAFF_SIG = re.compile(r"\bStaff\s+Signature\b", re.I)
SIG_DETAIL = re.compile(r"(\bsign(ed|ature)?\b|electronically|\bdate\b|\btime\b|\d{1,2}/\d{1,2}/\d{2,4}|"
                        r"\d{1,2}:\d{2}|\b(MHC|LMHC|LMHC-D|LCSW|LMSW|MA|EdM|PhD|PsyD|NPI|Extern|Intern)\b)", re.I)
FOCUS_LINE = re.compile(r"^\s*([A-Za-z /()&'-]{0,40}\bfocus\b[A-Za-z /()&'-]{0,40}):\s*(.*)$", re.I)
WNL_ONLY = re.compile(r"^\s*(wnl|w\.n\.l\.?|within\s+normal\s+limits?)\s*[.,;]?\s*$", re.I)


def _mse_summary(region_text):
    """'Appearance: WNL Orientation: WNL Mood: Anxious, tired' -> ['Mood: Anxious, tired']"""
    hits = list(MSE_LABEL.finditer(region_text))
    keep = []
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(region_text)
        value = re.sub(r"\s+", " ", region_text[m.end():end]).strip(" ,;")
        if value and not WNL_ONLY.match(value):
            label = re.sub(r"\s+", " ", m.group(1)).title()
            keep.append(f"{label}: {value}")
    return keep


def tidy_ehr_note(text, kind="progress"):
    """Clean a note imported from TheraNest:
    - drop the letterhead ("SOUTH SHORE COUNSELING …") and the document heading
    - drop the Insurer(s) entry and the Staff Signature block
    - progress notes: keep only mental-status items that say more than plain "WNL"
    - progress notes: put the primary focus of the session at the top"""
    lines = text.replace("\r\n", "\n").replace("\f", "\n").split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        t = line.strip()
        if LETTERHEAD.search(t) and len(t) < 90 or header_kind(t) or PAGE_ARTIFACT.match(t):
            i += 1
            continue
        if re.search(r"Insurer\(?s?\)?\s*:", line, re.I):
            line = INSURER.sub("", line).rstrip()
            if not line.strip():
                i += 1
                continue
        sig = STAFF_SIG.search(line)
        if sig:
            before = line[:sig.start()].rstrip()
            if before.strip():
                out.append(before)
            i += 1                                   # signature details that follow it
            while i < len(lines) and (not lines[i].strip() or (
                    SIG_DETAIL.search(lines[i]) and len(lines[i].split()) <= 14
                    and not re.search(r"\b(supervisor|clinical)\s+signature\b", lines[i], re.I)
                    and not MSE_START.match(lines[i]) and not FOCUS_LINE.match(lines[i]))):
                i += 1
            continue
        if kind == "progress" and MSE_START.match(line):
            region = [line]
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if not nxt or MSE_START.match(nxt):
                    region.append(lines[j])
                    j += 1
                    continue
                # a short wrapped value, only if more mental-status fields follow it
                k = j + 1
                while k < len(lines) and not lines[k].strip():
                    k += 1
                if len(nxt.split()) <= 8 and not FIELD_LINE.match(nxt) and k < len(lines) \
                        and MSE_START.match(lines[k]):
                    region.append(lines[j])
                    j += 1
                    continue
                # the last field's value wrapped onto the next line ("Sleep: Decreased, waking at / night")
                if lines[j - 1].strip() and len(nxt.split()) <= 6 and not FIELD_LINE.match(nxt) \
                        and (nxt[:1].islower() or lines[j - 1].rstrip().endswith((",", "and", "or", "at", "to"))):
                    region.append(lines[j])
                    j += 1
                break
            kept = _mse_summary(" ".join(region))
            out.extend(kept)
            if kept:
                out.append("")
            i = j
            continue
        out.append(line)
        i += 1

    if kind == "progress":
        out = _focus_first(out)
    text = "\n".join(l.rstrip() for l in out)
    text = re.sub(r"(?<=[^.:;!?\n])\n(?=[a-z])", " ", text)      # rejoin sentences the PDF wrapped
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _focus_first(lines):
    """Move the 'Primary Focus of Session' entry to the top. Without one, the first narrative
    paragraph (the clinical summary) goes first instead, ahead of the admin header lines."""
    for i, l in enumerate(lines):
        m = FOCUS_LINE.match(l)
        if not m:
            continue
        block, j = [l.strip()], i + 1
        if not m.group(2).strip():                       # value on the following line(s)
            while j < len(lines) and not lines[j].strip():
                j += 1
        while j < len(lines) and lines[j].strip() and not FIELD_LINE.match(lines[j]):
            block.append(lines[j].strip())
            j += 1
        if len(block) == 1 and not m.group(2).strip():
            return lines                                 # empty field: leave as is
        label = m.group(1).strip()
        value = " ".join([m.group(2).strip()] + block[1:]).strip()
        return [f"{label}: {value}", ""] + lines[:i] + lines[j:]
    # no labelled focus: first real paragraph goes first
    start = next((i for i, l in enumerate(lines) if len(l.split()) >= 8 and not FIELD_LINE.match(l)
                  and re.search(r"[a-z]", l)), None)
    if start is None or start == 0:
        return lines
    j = start
    while j < len(lines) and lines[j].strip() and not FIELD_LINE.match(lines[j]):
        j += 1
    return lines[start:j] + [""] + lines[:start] + lines[j:]


TIDY_VERSION = 1      # bump if the import clean-up rules change, so older imports get re-tidied once


def tidy_all_imported(vault):
    """One-time clean-up of every note/plan imported before the clean-up existed (all patients).
    Each record is marked with TIDY_VERSION afterwards, so it's never re-tidied — later edits you
    make to an imported note stay exactly as you leave them. Returns (notes changed, plans changed)."""
    now = datetime.now().isoformat(timespec="seconds")
    n_notes = n_plans = 0
    for sid, m in vault.session_index().items():
        if m.get("source") != "imported" or m.get("tidy_version", 0) >= TIDY_VERSION:
            continue
        try:
            d = vault.load_session(sid)
        except Exception:
            continue
        meta = d.setdefault("meta", {})
        new = tidy_ehr_note(d.get("note", ""), meta.get("kind", "progress"))
        if new and new != d.get("note", "").strip():
            d["note"] = new
            meta["updated"] = now
            n_notes += 1
        meta["tidy_version"] = TIDY_VERSION
        vault.save_session(d, sid)
    for c in vault.list_clients():
        changed = False
        for pl in c.get("treatment_plans", []):
            if pl.get("imported") and pl.get("tidy_version", 0) < TIDY_VERSION:
                new = tidy_ehr_note(pl.get("text", ""), "plan")
                if new and new != pl.get("text", "").strip():
                    pl["text"] = new
                    n_plans += 1
                pl["tidy_version"] = TIDY_VERSION
                changed = True
        if changed:
            vault.save_client(c)
    return n_notes, n_plans


def split_notes(text, split=True):
    """One file may hold several notes (e.g. copied from the EHR). TheraNest exports are
    split by their headings; anything else at short lines that contain a date.
    Returns [{date, number, kind, text}] (kind: intake / progress / plan)."""
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    if split:
        ehr = parse_ehr_export(text)
        if ehr is not None:
            return ehr
    text = text.replace("\f", "\n")

    chunks = [text]
    if split:
        lines, starts = text.split("\n"), []
        for i, line in enumerate(lines):
            if len(line.strip()) <= 70 and find_date(line):
                starts.append(i)
        if len(starts) >= 2:
            if starts[0] != 0 and "\n".join(lines[:starts[0]]).strip():
                starts.insert(0, 0)
            chunks = ["\n".join(lines[a:b]).strip() for a, b in zip(starts, starts[1:] + [len(lines)])]
    out = []
    for c in chunks:
        if len(c) < 20:
            continue
        out.append({"date": find_date(c[:200]), "number": guess_session_number(c),
                    "kind": guess_kind(c), "text": c})
    return out


# --------------------------------------------------------------------------- #
# Transcription
# --------------------------------------------------------------------------- #
def fmt_ts(seconds):
    s = int(seconds or 0)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


# --------------------------------------------------------------------------- #
# Stopping a task that's running in the background
# --------------------------------------------------------------------------- #
class Cancelled(Exception):
    """The task was called off (e.g. its session was discarded)."""


class WorkerError(Exception):
    """A failure reported by the transcription process; the message is already user-friendly."""


class CancelToken:
    """Handed to one background task. cancel() stops it for real: the transcription process is
    killed and the connection to LM Studio is cut (which makes LM Studio stop writing)."""

    def __init__(self):
        self._ev = threading.Event()
        self._lock = threading.Lock()
        self._stoppers = []

    def is_set(self):
        return self._ev.is_set()

    def check(self):
        if self._ev.is_set():
            raise Cancelled()

    def on_cancel(self, fn):
        """Run fn() when the task is cancelled (now, if it already was). Returns an unregister function."""
        with self._lock:
            if not self._ev.is_set():
                self._stoppers.append(fn)
                return lambda: self._forget(fn)
        _quiet(fn)
        return lambda: None

    def _forget(self, fn):
        with self._lock:
            if fn in self._stoppers:
                self._stoppers.remove(fn)

    def cancel(self):
        with self._lock:
            if self._ev.is_set():
                return
            self._ev.set()
            fns, self._stoppers = self._stoppers, []
        for fn in fns:
            _quiet(fn)


_TASK = threading.local()          # the CancelToken of the background task running on this thread


def current_token():
    return getattr(_TASK, "token", None)


def check_cancelled():
    t = current_token()
    if t is not None:
        t.check()


def on_cancel(fn):
    t = current_token()
    return t.on_cancel(fn) if t is not None else (lambda: None)


class StageError(Exception):
    """Wraps a failure with the transcription step it happened in."""

    def __init__(self, stage, original):
        super().__init__(f"{stage}: {original}")
        self.stage, self.original = stage, original


def friendly_error(e):
    if isinstance(e, (WorkerError, Cancelled)):
        return str(e) or "Stopped."
    if isinstance(e, StageError):
        base = friendly_error(e.original)
        detail = f"{type(e.original).__name__}: {e.original}".strip()
        if len(detail) > 400:
            detail = detail[:400] + "…"
        return f"{base}\n\nStep that failed: {e.stage}\nDetails: {detail}"
    msg = str(e)
    if isinstance(e, ModuleNotFoundError) and "whisperx" in msg:
        return ("The speech engine (WhisperX) isn't installed for the Python running this app.\n\n"
                "Click Settings (top right) → Install next to \"Speech engine\".")
    if isinstance(e, FileNotFoundError) and "ffmpeg" in msg.lower():
        return "FFmpeg is missing. Click Settings → Install next to \"FFmpeg\"."
    if any(t in msg for t in ("LocalEntryNotFound", "Cannot find an appropriate cached",
                              "offline mode", "HF_HUB_OFFLINE", "local_files_only")):
        return ("A model this step needs isn't downloaded yet. Open Settings: the Speech models row "
                "shows which piece is missing. Click Download there (one-time; no patient data).")
    if isinstance(e, NetworkBlocked):
        return (f"A component tried to go online and was blocked ({msg}). If you haven't yet, "
                "run Settings → Download models; afterwards everything works offline.")
    return f"{type(e).__name__}: {msg}"


def _stage(name, fn, *a, **k):
    try:
        return fn(*a, **k)
    except (ModuleNotFoundError, StageError):
        raise
    except Exception as e:
        raise StageError(name, e) from e


class _ProgressStream:
    """Stands in for stdout/stderr while WhisperX runs and turns its progress
    printouts ("Progress: 42.1%..." or "[12.3 --> 15.6]") into a fraction 0..1.
    Also keeps the app working under pythonw, where stdout/stderr don't exist."""

    def __init__(self, on_fraction, duration):
        self.on_fraction, self.duration, self.buf, self.best = on_fraction, duration, "", 0.0

    def write(self, s):
        self.buf += s
        *lines, self.buf = re.split(r"[\r\n]", self.buf)
        for line in lines:
            frac = None
            m = re.search(r"Progress:\s*([\d.]+)\s*%", line)
            if m:
                frac = float(m[1]) / 100
            else:
                m = re.search(r"\[\s*[\d.]+\s*-->\s*([\d.]+)\s*\]", line)
                if m and self.duration:
                    frac = float(m[1]) / self.duration
            if frac is not None:
                frac = max(0.0, min(frac, 1.0))
                if frac >= self.best:
                    self.best = frac
                    self.on_fraction(frac)
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


def _with_progress(on_fraction, duration, fn, *a, **k):
    import contextlib
    stream = _ProgressStream(on_fraction, duration)
    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
        return fn(*a, **k)


def _call_with_optional(fn, optional, *a, **k):
    """Call fn with extra keyword args if this WhisperX version accepts them."""
    try:
        return fn(*a, **dict(k, **optional))
    except TypeError as e:
        if not any(name in str(e) for name in optional):
            raise
        return fn(*a, **k)


class _HookedPipeline:
    """Wraps pyannote's pipeline so WhisperX's diarization reports progress."""

    def __init__(self, inner, hook):
        self._inner, self._hook = inner, hook

    def __call__(self, *a, **k):
        try:
            return self._inner(*a, hook=self._hook, **k)
        except TypeError as e:
            if "hook" not in str(e):
                raise
            return self._inner(*a, **k)

    def __getattr__(self, name):
        return getattr(self._inner, name)


_WORKER_TAG = "@@SSW "


def _kill_tree(p):
    """Stop the transcription process and anything it started (FFmpeg), immediately."""
    if p.poll() is not None:
        return
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, timeout=15)
        else:
            import signal
            os.killpg(p.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        p.kill()
    except Exception:
        pass


def _worker_python():
    """The Python running this app — the console build on Windows (no window appears: see
    _QuietPopen) so the child has proper pipes."""
    py = sys.executable
    if IS_WIN and os.path.basename(py).lower() == "pythonw.exe":
        alt = os.path.join(os.path.dirname(py), "python.exe")
        if os.path.isfile(alt):
            py = alt
    return py


def transcribe(audio_path, cfg, hf_token, progress):
    """progress(text, percent). Runs WhisperX in its own process on this computer, so it can be
    stopped instantly (the session was discarded, the queue job was cancelled) and its memory is
    fully released afterwards. The audio never leaves the computer; results come back over a pipe."""
    if os.environ.get("SESSION_SCRIBE_INPROC_TRANSCRIBE"):
        return transcribe_here(audio_path, cfg, hf_token, progress)
    check_cancelled()
    env = dict(os.environ)
    extra = [p for p in sys.path if p and os.path.isdir(p)
             and not any(os.path.abspath(p).startswith(os.path.abspath(b))
                         for b in {sys.prefix, sys.base_prefix, sys.exec_prefix})]
    if extra:                     # paths added at run time (rare) must reach the child too
        env["PYTHONPATH"] = os.pathsep.join(extra + [env["PYTHONPATH"]] if env.get("PYTHONPATH") else extra)
    env["PYTHONIOENCODING"] = "utf-8"
    kw = {} if IS_WIN else {"start_new_session": True}
    p = subprocess.Popen([_worker_python(), os.path.abspath(__file__), "--transcribe-worker"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         cwd=APP_DIR, env=env, **kw)
    forget = on_cancel(lambda: _kill_tree(p))
    result, error, ok = None, None, False
    try:
        job = {"audio": audio_path, "hf_token": hf_token or "",
               "cfg": {k: cfg.get(k) for k in ("whisper_model", "language", "batch_size",
                                               "min_speakers", "max_speakers") if cfg.get(k) is not None}}
        p.stdin.write((json.dumps(job) + "\n").encode("utf-8"))
        p.stdin.flush()                     # stdin stays open: if this app closes, the child exits too
        for raw in p.stdout:
            line = raw.decode("utf-8", errors="replace")
            if not line.startswith(_WORKER_TAG):
                continue
            msg = json.loads(line[len(_WORKER_TAG):])
            if msg["t"] == "p":
                progress(msg["text"], msg.get("pct"))
            elif msg["t"] == "r":
                result = msg
            elif msg["t"] == "e":
                error = msg
        ok = True
    except (BrokenPipeError, OSError, ValueError):
        ok = True
    finally:
        forget()
        if not ok:                          # stopped by an error here (or cancelled): don't leave it running
            _kill_tree(p)
        _quiet(p.stdin.close)
        try:
            p.wait(timeout=30)
        except Exception:
            _kill_tree(p)
    check_cancelled()
    if error is not None:
        if error.get("missing_module"):
            raise ModuleNotFoundError(error["msg"], name=error["missing_module"])
        raise WorkerError(error["msg"])
    if result is None:
        raise WorkerError("The transcription process stopped unexpectedly (exit code "
                          f"{p.returncode}). If this keeps happening, the computer may be running out of memory "
                          "— try a smaller speech model in Settings.")
    return result["segments"], result["duration"], result["diarized"], result["aligned"]


def transcribe_worker_main():
    """Child process: read one job from stdin, transcribe on this computer, write the result to stdout."""
    out = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)              # anything WhisperX/C libraries print goes nowhere, not into our pipe
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    lock = threading.Lock()

    def plain(o):                       # numpy numbers/arrays inside WhisperX results
        for attr in ("item", "tolist"):
            if hasattr(o, attr):
                return getattr(o, attr)()
        return str(o)

    def send(**m):
        with lock:
            out.write(_WORKER_TAG + json.dumps(m, default=plain) + "\n")
            out.flush()
    job = json.loads(sys.stdin.readline() or "{}")

    def watch_parent():                 # the app closed (or was killed): stop right away
        try:
            while sys.stdin.read(1):
                pass
        finally:
            os._exit(3)
    threading.Thread(target=watch_parent, daemon=True).start()
    try:
        segs, dur, diar, aligned = transcribe_here(job["audio"], job.get("cfg") or {}, job.get("hf_token"),
                                                   lambda text, pct=None: send(t="p", text=text, pct=pct))
        send(t="r", segments=segs, duration=dur, diarized=diar, aligned=aligned)
    except ModuleNotFoundError as e:
        send(t="e", msg=friendly_error(e), missing_module=getattr(e, "name", None) or "whisperx")
    except Exception as e:
        send(t="e", msg=friendly_error(e))
    out.flush()
    os._exit(0)                         # skip slow library teardown


def transcribe_here(audio_path, cfg, hf_token, progress):
    """progress(text, percent) — percent is overall 0-100 across every step."""
    diarize = bool(hf_token)
    # overall % ranges for each step
    R = ({"load": (0, 4), "read": (4, 6), "tx": (6, 62), "align": (62, 72), "spk_load": (72, 75),
          "spk": (75, 99)} if diarize else
         {"load": (0, 4), "read": (4, 6), "tx": (6, 82), "align": (82, 99)})

    def at(step, frac=0.0):
        lo, hi = R[step]
        return lo + (hi - lo) * frac

    progress("Loading the speech model…", at("load"))
    import torch
    import whisperx

    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute = "float16" if device == "cuda" else "int8"
    lang = cfg.get("language") or None
    model = _stage(f"loading the speech model ({cfg.get('whisper_model') or 'large-v3-turbo'})", whisperx.load_model,
                   cfg.get("whisper_model") or "large-v3-turbo", device, compute_type=compute, language=lang)
    progress("Reading the recording…", at("read"))
    audio = _stage("reading the audio file (FFmpeg)", whisperx.load_audio, audio_path)
    duration = len(audio) / 16000
    label = f"Transcribing {fmt_ts(duration)} of audio"
    progress(label + "…", at("tx"))
    result = _stage("transcribing", _with_progress,
                    lambda f: progress(f"{label}…", at("tx", f)), duration,
                    _call_with_optional, model.transcribe, {"print_progress": True, "verbose": True},
                    audio, batch_size=int(cfg.get("batch_size", 8)), language=lang)
    lang = result.get("language") or lang or "en"
    del model
    gc.collect()
    # Alignment gives every word its own timestamps. Word-level speaker assignment
    # (and therefore clean speaker turns) is impossible without it.
    progress(f"Lining words up with timestamps ({lang})…", at("align"))
    aligned = False
    try:
        model_a, md = whisperx.load_align_model(language_code=lang, device=device)
        result = _with_progress(lambda f: progress("Lining words up with timestamps…", at("align", f)),
                                duration, _call_with_optional, whisperx.align, {"print_progress": True},
                                result["segments"], model_a, md, audio, device, return_char_alignments=False)
        aligned = any(w.get("start") is not None for s in result["segments"] for w in s.get("words", []))
        del model_a
        gc.collect()
    except Exception as e:
        progress(f"Word alignment failed ({type(e).__name__}) — speaker labels will be less precise.",
                 at("align", 1))
    diarized = False
    if diarize:
        progress("Loading the speaker model…", at("spk_load"))
        DP = getattr(whisperx, "DiarizationPipeline", None)
        if DP is None:
            from whisperx.diarize import DiarizationPipeline as DP

        def make_dp():
            try:
                return DP(use_auth_token=hf_token, device=device)
            except TypeError:
                return DP(token=hf_token, device=device)
        dm = _stage("loading the speaker model (pyannote)", make_dp)
        # pyannote's steps and how much of the speaker step each one takes
        spans = {"segmentation": (0.0, 0.30), "speaker_counting": (0.30, 0.35),
                 "embeddings": (0.35, 0.95), "discrete_diarization": (0.95, 1.0)}
        names = {"segmentation": "Finding where each person speaks",
                 "speaker_counting": "Counting speakers",
                 "embeddings": "Recognizing each speaker's voice",
                 "discrete_diarization": "Assigning speakers"}

        def hook(step, artifact=None, file=None, total=None, completed=None, **_):
            lo, hi = spans.get(step, (0.0, 1.0))
            frac = lo + (hi - lo) * (completed / total if total and completed is not None else 0.0)
            progress(f"Identifying speakers: {names.get(step, step)}…", at("spk", frac))
        if hasattr(dm, "model"):
            dm.model = _HookedPipeline(dm.model, hook)
        progress("Identifying speakers…", at("spk"))
        segs = _stage("identifying speakers", _with_progress, lambda f: None, duration, dm, audio,
                      min_speakers=int(cfg.get("min_speakers", 2)), max_speakers=int(cfg.get("max_speakers", 2)))
        result = whisperx.assign_word_speakers(segs, result)   # labels each word, not just segments
        diarized = True
        del dm
        gc.collect()
    progress("Finishing up…", 100)
    return result["segments"], duration, diarized, aligned


def words_to_turns(segments, pause=2.0, smooth=True, min_dur=0.5, embed_gap=0.3):
    """Build speaker turns from WORD-level speaker labels.

    WhisperX's segment label is a majority vote, so a segment spanning both
    speakers gets one label for everyone's words. After alignment +
    assign_word_speakers each word carries its own `speaker`, so turns are
    rebuilt word by word instead of trusting the segment label.

    * Words with no timing/speaker (often numbers or symbols the aligner can't
      place) inherit the neighbouring speaker and timestamp.
    * Segments with no word data (alignment failed) fall back to the segment label.
    * smooth: a one-word turn shorter than `min_dur` seconds, sitting between two
      turns of the same other speaker with almost no pause on either side
      (< embed_gap s), is treated as diarization flicker and folded back in.
      Real short answers ("No." after a question) have pauses around them, so
      they're left alone.
    """
    words = []
    for s in segments:
        ws = [w for w in (s.get("words") or []) if (w.get("word") or "").strip()]
        if not ws:
            text = (s.get("text") or "").strip()
            if text:
                words.append({"text": text, "start": s.get("start"), "end": s.get("end"),
                              "speaker": s.get("speaker")})
            continue
        for w in ws:
            words.append({"text": w["word"].strip(), "start": w.get("start"), "end": w.get("end"),
                          "speaker": w.get("speaker") or None})
    if not words:
        return []

    # inherit speaker from the nearest labelled neighbour (previous first, then next)
    last = None
    for w in words:
        last = w["speaker"] = w["speaker"] or last
    nxt = None
    for w in reversed(words):
        nxt = w["speaker"] = w["speaker"] or nxt
    # fill missing timestamps from neighbours
    prev_end = 0.0
    for w in words:
        w["speaker"] = w["speaker"] or "SPEAKER"
        w["start"] = float(w["start"]) if w["start"] is not None else prev_end
        w["end"] = float(w["end"]) if w["end"] is not None else w["start"]
        prev_end = max(prev_end, w["end"])

    turns = []
    for w in words:
        t = turns[-1] if turns else None
        if t and t["speaker"] == w["speaker"] and w["start"] - t["end"] < pause:
            t["words"].append(w)
            t["end"] = max(t["end"], w["end"])
        else:
            turns.append({"speaker": w["speaker"], "start": w["start"], "end": w["end"], "words": [w]})

    if smooth:
        i = 1
        while i < len(turns) - 1:
            prev, t, nxt = turns[i - 1], turns[i], turns[i + 1]
            flicker = (len(t["words"]) == 1 and t["end"] - t["start"] < min_dur
                       and prev["speaker"] == nxt["speaker"] != t["speaker"]
                       and t["start"] - prev["end"] < embed_gap and nxt["start"] - t["end"] < embed_gap)
            if flicker:
                prev["words"] += t["words"] + nxt["words"]
                prev["end"] = nxt["end"]
                del turns[i:i + 2]
                i = max(1, i - 1)
            else:
                i += 1

    return [{"start": t["start"], "end": t["end"], "speaker": t["speaker"],
             "text": " ".join(w["text"] for w in t["words"])} for t in turns]


def guess_roles(lines, labels=None):
    labels = labels or ROLES
    you, them, other = labels[0], labels[1], labels[-1]
    speakers = list(dict.fromkeys(l["speaker"] for l in lines))
    if len(speakers) == 1:
        return {speakers[0]: "Speaker"}
    q = {s: 0 for s in speakers}
    for l in lines:
        q[l["speaker"]] += l["text"].count("?")
    clinician = max(speakers, key=lambda s: q[s])
    roles, client_done = {}, False
    for s in speakers:
        if s == clinician:
            roles[s] = you
        elif not client_done:
            roles[s], client_done = them, True
        else:
            roles[s] = other
    return roles


def render_transcript(lines, roles):
    return "\n".join(f"[{fmt_ts(l['start'])}] {roles.get(l['speaker'], l['speaker'])}: {l['text']}"
                     for l in lines)


def parse_name_map(s, legacy=False):
    """'Danielle, Patient; John, Husband' (or one pair per line) -> [(name, '[Placeholder]')].
    A name with no placeholder becomes [Name]. legacy=True reads the old plain
    comma-separated name list."""
    s = s or ""
    if legacy:
        return [(n.strip(), "[Name]") for n in s.split(",") if n.strip()]
    pairs = []
    for entry in re.split(r"[;\n]+", s):
        entry = entry.strip()
        if not entry:
            continue
        name, _, ph = entry.partition(",")
        name, ph = name.strip(), ph.strip().strip("[]").strip() or "Name"
        if name:
            pairs.append((name, f"[{ph}]"))
    return pairs


def apply_name_map(text, pairs):
    """Replace each name (case-insensitive, whole word, possessives included) with its
    placeholder. For a full name like 'Danielle Smith', the first and last names are
    also replaced on their own."""
    expanded = []
    for name, ph in pairs:
        expanded.append((name, ph))
        parts = name.split()
        if len(parts) > 1:
            expanded += [(p, ph) for p in parts if len(p) > 2]
    for name, ph in sorted(expanded, key=lambda x: len(x[0]), reverse=True):
        text = re.sub(r"(?<![\w\[])" + re.escape(name) + r"\b", ph, text, flags=re.IGNORECASE)
    return text


def client_name_map(client):
    return parse_name_map(client.get("names_to_hide", ""), legacy=client.get("names_format") != 2)


def client_hider(client):
    pairs = client_name_map(client)
    return lambda s: apply_name_map(s or "", pairs)


# --------------------------------------------------------------------------- #
# Local LLM (LM Studio)
# --------------------------------------------------------------------------- #
def assert_local_url(url):
    if not _is_local_host(urllib.parse.urlparse(url).hostname):
        raise NetworkBlocked("The AI server address must be on this computer "
                             "(localhost or 127.0.0.1).")


_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(url, body=None, timeout=30):
    assert_local_url(url)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with _NO_PROXY.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"LM Studio returned an error ({e.code}): {detail}") from None
    except (urllib.error.URLError, ConnectionError, socket.timeout) as e:
        raise RuntimeError("Couldn't reach LM Studio. Open LM Studio, load a model, and start the "
                           f"local server (Developer tab). Details: {e}") from None


def list_models(cfg, timeout=30):
    data = _http_json(cfg["lmstudio_url"].rstrip("/") + "/models", timeout=timeout)
    return [m.get("id") for m in data.get("data", []) if m.get("id")]


def llm_chat(cfg, prompt, on_progress=None, expected=1500, system=None, on_text=None, temperature=0.2):
    """Ask the local model. Streams the reply so on_progress(fraction 0..1) can
    drive a progress bar; `expected` is the rough length (characters) of a
    typical answer, used to estimate the fraction while it's being written.
    If the task is cancelled, the connection is cut at once and LM Studio stops writing."""
    import http.client
    check_cancelled()
    model = cfg.get("model") or (list_models(cfg) or [None])[0]
    if not model:
        raise RuntimeError("LM Studio is running but no model is loaded.")
    url = cfg["lmstudio_url"].rstrip("/") + "/chat/completions"
    assert_local_url(url)
    body = {"model": model, "temperature": temperature, "stream": True,
            "messages": [{"role": "system", "content": system or SYSTEM_PROMPT},
                         {"role": "user", "content": prompt}]}
    text, thinking, finish = "", 0, None
    report = on_progress or (lambda f: None)
    u = urllib.parse.urlparse(url)
    conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(u.hostname, u.port, timeout=1800)
    held = {}

    def cut():
        # shut the socket itself: the response keeps reading from it even after http.client
        # lets go of it, and shutdown() wakes a read that's waiting for LM Studio
        sock = held.get("sock") or getattr(conn, "sock", None)
        if sock is not None:
            _quiet(sock.shutdown, socket.SHUT_RDWR)
        _quiet(conn.close)
    forget = on_cancel(cut)
    try:
        conn.connect()
        held["sock"] = conn.sock
        check_cancelled()
        conn.request("POST", (u.path or "/") + (f"?{u.query}" if u.query else ""),
                     body=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        if r.status >= 400:
            detail = r.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"LM Studio returned an error ({r.status}): {detail}")
        if "event-stream" not in (r.getheader("Content-Type") or ""):
            data = json.loads(r.read().decode("utf-8"))      # server ignored streaming
            choice = data["choices"][0]
            text, finish = choice["message"]["content"] or "", choice.get("finish_reason")
        else:
            while True:
                raw = r.readline()
                check_cancelled()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)["choices"][0]
                except (ValueError, KeyError, IndexError):
                    continue
                delta = chunk.get("delta") or {}
                text += delta.get("content") or ""
                thinking += len(delta.get("reasoning_content") or delta.get("reasoning") or "")
                finish = chunk.get("finish_reason") or finish
                shown = re.sub(r"<think>.*?(</think>|$)", "", text, flags=re.S)
                visible = len(shown)
                if on_text is not None and delta.get("content"):
                    on_text(shown)
                # thinking counts a little, so the bar still moves for reasoning models
                report(min(0.97, (visible + 0.15 * (thinking + len(text) - visible)) / max(expected, 1)))
    except (Cancelled, RuntimeError, NetworkBlocked):
        raise
    except Exception as e:
        check_cancelled()                  # a read cut short by cancelling is a cancel, not an error
        if isinstance(e, (KeyError, IndexError, ValueError)):
            raise RuntimeError("LM Studio sent back an unexpected response.") from None
        raise RuntimeError("Couldn't reach LM Studio. Open LM Studio, load a model, and start the "
                           f"local server (Developer tab). Details: {e}") from None
    finally:
        forget()
        _quiet(conn.close)
    report(1.0)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"^\s*```[a-z]*\n|\n```\s*$", "", text.strip())
    if finish == "length":
        text += ("\n\n[VERIFY: the AI stopped early — output may be cut off. Increase the context "
                 "length in LM Studio.]")
    return text.strip()


class _Steps:
    """Overall progress across several AI calls, weighted by expected length."""

    def __init__(self, progress, plan):
        self.progress, self.plan = progress, plan          # plan: [(label, expected_chars)]
        self.total = sum(w for _, w in plan) or 1
        self.done = 0

    def run(self, i, cfg, prompt):
        label, weight = self.plan[i]
        base = self.done
        self.progress(f"{label}…", 100 * base / self.total)
        out = llm_chat(cfg, prompt, lambda f: self.progress(f"{label}…", 100 * (base + f * weight) / self.total),
                       expected=weight)
        self.done += weight
        return out


def split_chunks(text, max_chars, sep="\n"):
    chunks, cur, size = [], [], 0
    for part in text.split(sep):
        if cur and size + len(part) + len(sep) > max_chars:
            chunks.append(sep.join(cur))
            cur, size = [], 0
        cur.append(part)
        size += len(part) + len(sep)
    if cur:
        chunks.append(sep.join(cur))
    return chunks


def session_label(meta):
    kind = "Intake" if meta.get("kind") == "intake" else \
        (f"Session {meta['session_number']}" if meta.get("session_number") else "Session")
    return f"{kind} — {fmt_date(meta.get('session_date', '?'))}" + (" (imported)" if meta.get("source") == "imported" else "")


def group_by_date(sessions):
    """[(sid, data)] -> [(date, [(sid, data), ...])], oldest date first. Everything
    saved for the same date (generated session, imported notes…) shows as one entry.
    Within a date: generated before imported, intake before progress."""
    groups = {}
    for sid, d in sessions:
        groups.setdefault(d.get("meta", {}).get("session_date") or "undated", []).append((sid, d))

    def rec_key(x):
        m = x[1].get("meta", {})
        return (m.get("source") == "imported", m.get("kind") != "intake", m.get("created", ""))
    return [(day, sorted(recs, key=rec_key)) for day, recs in sorted(groups.items())]


def day_title(records):
    metas = [d.get("meta", {}) for _, d in records]
    if any(m.get("kind") == "intake" for m in metas):
        return "Intake"
    num = next((m.get("session_number") for m in metas if m.get("session_number")), "")
    return f"Session {num}" if num else "Session"


def record_docs(d):
    """[(tab label, key)] for one saved record."""
    m = d.get("meta", {})
    intake = m.get("kind") == "intake"
    if m.get("source") == "imported":
        return [("Imported intake" if intake else "Imported note", "note")]
    labels = KIND_LABELS["intake" if intake else "progress"]
    docs = [(labels[0], "note")]
    if d.get("overview", "").strip():
        docs.append((labels[1], "overview"))
    if d.get("transcript", "").strip():
        docs.append(("Transcript", "transcript"))
    return docs


def day_docs(records):
    """Every document for one date: [(label, key, sid, data)], labels made unique."""
    out, seen = [], {}
    for sid, d in records:
        for label, key in record_docs(d):
            seen[label] = seen.get(label, 0) + 1
            out.append((label if seen[label] == 1 else f"{label} ({seen[label]})", key, sid, d))
    return out


def search_patient(client, sessions, query, limit=300):
    """Search every document for one patient (case overview, notes, overviews,
    transcripts, intakes, imported notes). Case-insensitive; all words must appear.
    Returns [{"day", "label", "key", "sid", "count", "snippet"}], newest first."""
    words = [w for w in query.lower().split() if w]
    if not words:
        return []

    def check(text):
        low = (text or "").lower()
        if not all(w in low for w in words):
            return 0, ""
        count = sum(low.count(w) for w in words)
        i = low.find(words[0])
        a, b = max(0, i - 50), min(len(text), i + len(words[0]) + 70)
        snippet = ("…" if a else "") + re.sub(r"\s+", " ", text[a:b]).strip() + ("…" if b < len(text) else "")
        return count, snippet

    results = []
    case = (client.get("case_overview") or {}).get("text", "")
    n, snip = check(case)
    if n:
        results.append({"day": None, "label": "Case overview", "key": "case", "sid": None, "count": n,
                        "snippet": snip})
    plan = current_plan(client)
    if plan:
        n, snip = check(plan["text"])
        if n:
            results.append({"day": None, "label": "Treatment plan", "key": "plan", "sid": None, "count": n,
                            "snippet": snip})
    for day, records in reversed(group_by_date(sessions)):
        for label, key, sid, d in day_docs(records):
            n, snip = check(d.get(key, ""))
            if not n and words and all(w in day for w in words):          # searching a date finds that day
                n, snip = 1, (d.get(key, "") or "")[:110]
            if n:
                results.append({"day": day, "label": label, "key": key, "sid": sid, "count": n, "snippet": snip})
            if len(results) >= limit:
                return results
    return results


# --------------------------------------------------------------------------- #
# Treatment plans
# --------------------------------------------------------------------------- #
PLAN_SPLIT = re.compile(r"\n\s*-{3,}\s*\n(?=\s*NOTES FOR CLINICIAN)", re.I)


def current_plan(client):
    plans = client.get("treatment_plans") or []
    return plans[-1] if plans else None


NOTE_USED_RE = re.compile(r"(?im)^\s*therapeutic interventions used\s*:?\s*$")
NOTE_PLANNED_RE = re.compile(r"(?im)^\s*planned interventions\b[^\n]*$")


def note_sections(text):
    """A progress note's parts: (paragraph, interventions used, planned interventions).
    The lists are '' when the note doesn't have them (older or imported notes)."""
    text = text or ""
    u, pl = NOTE_USED_RE.search(text), NOTE_PLANNED_RE.search(text)
    cuts = sorted(m.start() for m in (u, pl) if m)
    body = text[:cuts[0]] if cuts else text

    def part(m):
        if not m:
            return ""
        nxt = [c for c in cuts if c > m.start()]
        return text[m.end():nxt[0] if nxt else len(text)].strip()
    return body.strip(), part(u), part(pl)


def plan_for_ehr(text):
    """The plan without the clinician-only notes at the end (what gets copied to the EHR)."""
    return PLAN_SPLIT.split(text or "", maxsplit=1)[0].strip()


def plan_goals(text):
    """'Goal 1: ...' lines from a plan, for the patient profile's treatment goals."""
    return [m.group(1).strip() for m in re.finditer(r"(?im)^\s*goal\s*\d+\s*[:.\-–]\s*(.+)$", plan_for_ehr(text))]


def plan_review_due(plan):
    try:
        start = datetime.fromisoformat(plan.get("created", "")[:19]).date()
        from datetime import timedelta
        return start + timedelta(days=int(plan.get("review_days") or 90))
    except (ValueError, TypeError):
        return None


def group_blocks(blocks, max_chars):
    """Split documentation blocks into groups that each fit the model (empty list if
    everything already fits)."""
    if len("\n\n".join(blocks)) <= max_chars:
        return []
    groups, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) > max_chars:
            groups.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + b
    groups.append(cur)
    return groups


def plan_material(client, sessions, hide):
    """Everything a treatment plan should rest on, oldest first."""
    blocks = []
    prev = current_plan(client)
    if prev:
        blocks.append(f"=== PREVIOUS TREATMENT PLAN (version {prev['version']}, {prev.get('created', '')[:10]}) ===\n"
                      f"{hide(plan_for_ehr(prev['text']))}")
    case = (client.get("case_overview") or {}).get("text", "")
    if case:
        blocks.append(f"=== CASE OVERVIEW ===\n{hide(case)}")
    for day, records in group_by_date([(None, d) for d in sessions]):
        parts = []
        for label, key, _, d in day_docs(records):
            if key == "transcript" or not d.get(key, "").strip():
                continue
            if key == "overview" and d.get("meta", {}).get("kind") != "intake":
                continue                                   # progress notes carry the content
            parts.append(f"{label.upper()}:\n{hide(d[key]).strip()}")
        if parts:
            blocks.append(f"=== {day_title(records)} — {fmt_date(day)} ===\n" + "\n\n".join(parts))
    return blocks


def generate_treatment_plan(cfg, client, sessions, orientation, review_days, instructions, progress):
    hide = client_hider(client)
    blocks = plan_material(client, sessions, hide)
    groups = group_blocks(blocks, int(cfg.get("max_chars", 24000)))
    steps = _Steps(progress, [(f"Condensing the record, part {i} of {len(groups)}", 3500)
                              for i in range(1, len(groups) + 1)] + [("Writing the treatment plan", 5000)])
    if groups:
        material = "\n\n".join(steps.run(i, cfg, fill(NOTES_DIGEST_PROMPT, {"part": i + 1, "total": len(groups),
                                                                          "chunk": g}))
                                for i, g in enumerate(groups))
        kind = "condensed digests of this patient's documentation, oldest first"
    else:
        material = "\n\n".join(blocks)
        kind = "this patient's documentation, oldest first"
    out = steps.run(len(groups), cfg, fill(read_template("treatment_plan.txt"), {
        "client_code": client.get("code", ""), "orientation": orientation or "not specified",
        "background": hide(client.get("background", "")).strip() or "(not provided)",
        "goals": hide(client.get("goals", "")).strip() or "(not provided)",
        "review_days": review_days,
        "instructions": (f"\nCLINICIAN'S INSTRUCTIONS FOR THIS PLAN: {hide(instructions)}\n"
                         if instructions.strip() else ""),
        "source_kind": kind, "material": material or "(no documentation yet)"}))
    progress("Done.", 100)
    return out


def build_context(client, prior_sessions, n_prev, hide, same_day=None):
    """prior_sessions: oldest-first session dicts from EARLIER dates. same_day: other
    documentation already saved for this session's date (e.g. an imported note)."""
    case = client.get("case_overview") or {}
    case_txt = ""
    if case.get("text"):
        case_txt = (f"\nCASE OVERVIEW TO DATE (generated {case.get('generated', '')[:10]}):\n"
                    f"{hide(case['text'])}\n")
    intakes = [d for d in prior_sessions if d.get("meta", {}).get("kind") == "intake" and d.get("note", "").strip()]
    intake_txt = ""
    if intakes:
        d = intakes[-1]
        intake_txt = f"\nINTAKE ASSESSMENT ({session_label(d['meta'])}):\n{hide(d['note'])[:6000]}\n"
    notes = [d for d in prior_sessions if d.get("note", "").strip() and d.get("meta", {}).get("kind") != "intake"]
    recent = notes[-max(0, n_prev):] if n_prev else []
    if recent:
        prev = "\nPREVIOUS SESSION NOTES (oldest first):\n" + "\n\n".join(
            f"{session_label(d['meta'])}:\n{hide(d['note'])}" for d in recent)
    elif not prior_sessions:
        prev = "\nThis is the first documented session for this patient.\n"
    else:
        prev = ""
    used = ([session_label(intakes[-1]["meta"])] if intakes else []) + [session_label(d["meta"]) for d in recent]
    same = [d for d in (same_day or []) if d.get("note", "").strip()]
    if same:
        prev += ("\nOTHER DOCUMENTATION ALREADY ON FILE FOR THIS SAME DATE (likely the clinician's own record of "
                 "this session). Keep the new draft consistent with it; if they conflict, flag it with [VERIFY]:\n"
                 + "\n\n".join(f"{session_label(d['meta'])}:\n{hide(d['note'])[:4000]}" for d in same) + "\n")
        used += [session_label(d["meta"]) + " (same date)" for d in same]
    if case_txt:
        used.append("case overview")
    plan = current_plan(client)
    plan_txt = ""
    if plan:
        plan_txt = (f"\nCURRENT TREATMENT PLAN (version {plan['version']}, {plan.get('created', '')[:10]}) — relate "
                    f"progress to these goals:\n{hide(plan_for_ehr(plan['text']))[:5000]}\n")
        used.append(f"treatment plan v{plan['version']}")
    return fill(CONTEXT_HEADER, {"background": hide(client.get("background", "").strip()) or "(not provided)",
                                 "plan": plan_txt,
                                 "intake": intake_txt, "case": case_txt, "previous": prev}), used


def generate_documents(cfg, values, transcript, context, progress, kind="progress", reference=None,
                       instructions="", which=("first", "second")):
    """Returns (first, second): progress -> (note, overview); intake -> (assessment, questionnaire).
    `which` limits what's written (the other comes back as None); `instructions` are the
    clinician's directions for this draft."""
    max_chars = int(cfg.get("max_chars", 24000))
    chunks = split_chunks(transcript, max_chars) if len(transcript) > max_chars else []
    if kind == "intake":
        tail = [("Writing the intake assessment", 4000), ("Filling in the 13-section intake questionnaire", 5000)]
    else:
        tail = [("Writing the progress note", 1000), ("Writing the session overview", 3500)]
    tail = [t for t, name in zip(tail, ("first", "second")) if name in which]
    steps = _Steps(progress, [(f"Long session — condensing part {i} of {len(chunks)}", 3000)
                              for i in range(1, len(chunks) + 1)] + tail)
    if chunks:
        digests = []
        for i, chunk in enumerate(chunks, 1):
            digests.append(steps.run(i - 1, cfg, fill(DIGEST_PROMPT, {"part": i, "total": len(chunks),
                                                                       "chunk": chunk})))
        source = "\n\n".join(f"--- Part {i} of {len(digests)} ---\n{d}"
                             for i, d in enumerate(digests, 1))
        kind_txt = ("a detailed, time-stamped digest of the session (the full transcript was too long "
                    "to include in one pass)")
    else:
        source, kind_txt = transcript, "the full session transcript"
    v = dict(values, transcript=source, source_kind=kind_txt)
    style = style_block(reference, kind)
    lead = context + ("\n\n" + style if style else "") + "\n\n"
    extra = (f"\n\nCLINICIAN'S INSTRUCTIONS FOR THIS DRAFT (follow them, as long as the transcript supports "
             f"it):\n{instructions.strip()}" if instructions.strip() else "")
    n = len(chunks)
    t1, t2 = ("intake_assessment.txt", "intake_questionnaire.txt") if kind == "intake" else \
        ("session_note.txt", "overview.txt")
    first = second = None
    if "first" in which:
        first = steps.run(n, cfg, lead + fill(read_template(t1), v) + extra)
        n += 1
    if "second" in which:
        second = steps.run(n, cfg, context + "\n\n" + fill(read_template(t2), v) + extra)
    progress("Done.", 100)
    return first, second


def generate_case_overview(cfg, client, sessions, orientation, progress):
    """sessions: oldest-first list of session dicts."""
    hide = client_hider(client)
    blocks = []
    days = group_by_date([(None, d) for d in sessions])
    for day, records in days:
        parts = []
        for label, key, _, d in day_docs(records):
            if key == "transcript" or not d.get(key, "").strip():
                continue
            parts.append(f"{label.upper()}:\n{hide(d[key]).strip()}")
        if parts:
            blocks.append(f"=== {day_title(records)} — {fmt_date(day)} ===\n" + "\n\n".join(parts))
    text = "\n\n".join(blocks)
    max_chars = int(cfg.get("max_chars", 24000))
    if len(text) > max_chars:
        groups, cur = [], ""
        for b in blocks:
            if cur and len(cur) + len(b) > max_chars:
                groups.append(cur)
                cur = ""
            cur += ("\n\n" if cur else "") + b
        groups.append(cur)
    else:
        groups = []
    steps = _Steps(progress, [(f"Condensing sessions, part {i} of {len(groups)}", 3500)
                              for i in range(1, len(groups) + 1)] + [("Writing the case overview", 6000)])
    if groups:
        digests = []
        for i, g in enumerate(groups, 1):
            digests.append(steps.run(i - 1, cfg, fill(NOTES_DIGEST_PROMPT, {"part": i, "total": len(groups),
                                                                             "chunk": g})))
        source = "\n\n".join(digests)
        kind = "condensed digests of all session documentation, in order"
    else:
        source, kind = text, "all session documentation, in order"
    dates = [d.get("meta", {}).get("session_date", "") for d in sessions if d.get("meta", {}).get("session_date")]
    return steps.run(len(groups), cfg, fill(read_template("case_overview.txt"), {
        "client_code": client.get("code", ""), "orientation": orientation or "not specified",
        "background": hide(client.get("background", "")).strip() or "(not provided)",
        "goals": hide(client.get("goals", "")).strip() or "(not provided)",
        "session_count": len(days),
        "date_range": f"{min(dates)} to {max(dates)}" if dates else "",
        "source_kind": kind, "sessions": source}))


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
def _dpi_aware():
    """Crisp text on high-DPI Windows screens (otherwise Windows blurs the window)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


THEME_PACKAGE = "sv-ttk"        # Microsoft's Windows 11 "Sun Valley" look for Tk


def theme_installed():
    importlib.invalidate_caches()
    return importlib.util.find_spec("sv_ttk") is not None


def ensure_theme_package():
    """Install the small Windows 11 theme once if it's missing (falls back to a
    built-in flat look if offline). Tried at most once a day."""
    if theme_installed():
        return
    cfg = load_config()
    today = date.today().isoformat()
    if cfg.get("theme_install_tried") == today:
        return
    cfg["theme_install_tried"] = today
    try:
        save_config(cfg)
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                        "--retries", "0", "--timeout", "8", THEME_PACKAGE], capture_output=True, timeout=120,
                       env=dict(ONLINE_ENV, PATH=os.environ["PATH"]))
    except Exception:
        pass
    importlib.invalidate_caches()


def install_theme(log):
    if run_streamed([sys.executable, "-m", "pip", "install", "--upgrade", THEME_PACKAGE], log):
        raise RuntimeError("pip couldn't install the theme.")
    return "Modern theme installed — restart the app to use it."
STEPS = ["Recording", "Transcribe", "Check transcript", "Review notes"]
KIND_LABELS = {"progress": ("Progress note", "Overview"), "intake": ("Intake assessment", "Intake questionnaire")}


def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    try:
        import cryptography  # noqa: F401
    except ImportError:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(APP_NAME, "One package is missing. Open a terminal and run:\n\n"
                                       f'"{sys.executable}" -m pip install cryptography')
        return

    cfg = load_config()
    vault = Vault(cfg["vault_dir"])

    _dpi_aware()
    root = tk.Tk()
    root.title(APP_NAME)
    _last_err = {"t": 0.0}

    def on_tk_error(exc, val, tb):
        """A button or timer crashed: log it (on this computer only) and say so, instead of
        silently doing nothing — the app keeps running."""
        import traceback
        text = "".join(traceback.format_exception(exc, val, tb))
        try:
            os.makedirs(LOCAL_DIR, exist_ok=True)
            with open(os.path.join(LOCAL_DIR, "error-log.txt"), "a", encoding="utf-8") as f_:
                f_.write(f"\n--- {datetime.now().isoformat(timespec='seconds')} ---\n{text}")
        except OSError:
            pass
        sys.stderr.write(text)
        if time.time() - _last_err["t"] > 10:
            _last_err["t"] = time.time()
            try:
                messagebox.showerror(APP_NAME, "Something went wrong: " + friendly_error(val)[:300]
                                     + "\n\nNothing else was affected. Details were saved to error-log.txt in "
                                       f"{LOCAL_DIR} (technical details only).")
            except Exception:
                pass
    root.report_callback_exception = on_tk_error
    SCALE = max(1.0, root.winfo_fpixels("1i") / 96.0)      # 1.25 / 1.5 on high-DPI screens

    def px(n):
        return int(round(n * SCALE))

    def scaled(geom):
        w, h = (int(x) for x in geom.split("x"))
        return f"{px(w)}x{px(h)}"

    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    ww, wh = min(px(1240), int(sw * 0.94)), min(px(840), int(sh * 0.88))
    root.geometry(f"{ww}x{wh}+{max(0, (sw - ww) // 2)}+{max(0, (sh - wh) // 3)}")
    root.minsize(min(px(800), ww), min(px(600), wh))
    style = ttk.Style()

    # ---------------------------------------------------------------- fonts
    from tkinter import font as tkfont
    families = set(tkfont.families())

    def pick(*names):
        return next((n for n in names if n in families), names[-1])

    BODY = pick("Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "Helvetica Neue", "Inter", "Cantarell",
                "Noto Sans", "DejaVu Sans", "Helvetica")
    DOCF = pick("Aptos", "Calibri", "Segoe UI", "Helvetica Neue", "Carlito", "Noto Sans", "DejaVu Sans", "Helvetica")
    DISPLAY = pick("Segoe UI Variable Display", "Segoe UI", BODY)
    SEMI = "Segoe UI Variable Text Semibold" in families
    F = {
        "body": tkfont.Font(family=BODY, size=10),
        "body_lg": tkfont.Font(family=BODY, size=11),
        "strong": tkfont.Font(family="Segoe UI Variable Text Semibold" if SEMI else BODY, size=10,
                              weight="normal" if SEMI else "bold"),
        "caption": tkfont.Font(family=BODY, size=9),
        "caption_strong": tkfont.Font(family=BODY, size=9, weight="bold"),
        "sub": tkfont.Font(family="Segoe UI Variable Display Semibold" if SEMI else DISPLAY, size=13,
                           weight="normal" if SEMI else "bold"),
        "title": tkfont.Font(family="Segoe UI Variable Display Semibold" if SEMI else DISPLAY, size=19,
                             weight="normal" if SEMI else "bold"),
        "hero": tkfont.Font(family="Segoe UI Variable Display Semibold" if SEMI else DISPLAY, size=24,
                            weight="normal" if SEMI else "bold"),
        "strong_lg": tkfont.Font(family="Segoe UI Variable Text Semibold" if SEMI else BODY, size=11,
                                 weight="normal" if SEMI else "bold"),
        "icon": tkfont.Font(family=BODY, size=12, weight="bold"),
        "mono": tkfont.Font(family=pick("Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Courier"), size=9),
        # documents (notes, transcripts, plans): word-processor typography
        "doc": tkfont.Font(family=DOCF, size=11),
        "doc_b": tkfont.Font(family=DOCF, size=11, weight="bold"),
        "doc_h": tkfont.Font(family=DOCF, size=13, weight="bold"),
        "doc_s": tkfont.Font(family=DOCF, size=9),
    }
    # macOS draws 1 pt = 1 px (Windows: 1.33 px), so the same point sizes look small there
    FK = 96 / 72 if IS_MAC else 1.0

    def fs(n):
        return int(round(n * FK))
    if IS_MAC:
        for _f in F.values():
            _f.configure(size=fs(_f.cget("size")))
    _z = max(-3, min(6, int(cfg.get("doc_zoom", 0) or 0)))
    for _k, _base in (("doc", 11), ("doc_b", 11), ("doc_h", 13), ("doc_s", 9)):
        F[_k].configure(size=fs(_base + _z))

    # --------------------------------------------------------------- colors
    PALETTES = {
        "light": dict(bg="#fafafa", surface="#ffffff", surface2="#f3f3f3", sidebar="#f0f0f0",
                      border="#e0e0e0", text="#1c1c1c", muted="#616161", accent="#005fb8",
                      accent_soft="#dcebf7", on_accent="#ffffff", good="#0f7b0f", good_bg="#dff6dd",
                      bad="#c42b1c", bad_bg="#fde7e9", warn="#8a5300", warn_bg="#fff4ce",
                      select="#cce4f7", lock="#1c1c1c", lock_fg="#fafafa"),
        "dark": dict(bg="#1c1c1c", surface="#2b2b2b", surface2="#232323", sidebar="#202020",
                     border="#3d3d3d", text="#fafafa", muted="#a8a8a8", accent="#57c8ff",
                     accent_soft="#1d3b4f", on_accent="#000000", good="#6ccb5f", good_bg="#1f3a1f",
                     bad="#ff99a4", bad_bg="#442726", warn="#fce100", warn_bg="#433519",
                     select="#264f78", lock="#101010", lock_fg="#fafafa"),
    }
    THEME = {"mode": "light", "P": PALETTES["light"], "sv": False}
    registered = []                                  # raw tk widgets that need recoloring

    MOD = "Command" if IS_MAC else "Control"          # ⌘ on Mac, Ctrl elsewhere

    def P(key):
        return THEME["P"][key]

    def system_mode():
        if os.name == "nt":
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                    return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
            except OSError:
                pass
        if IS_MAC:
            try:
                out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True,
                                     text=True, timeout=5).stdout
                return "dark" if "dark" in out.lower() else "light"
            except Exception:
                pass
        return "light"

    def titlebar(win):
        """Dark/light Windows title bar to match the app."""
        if os.name != "nt":
            return
        try:
            import ctypes
            win.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            val = ctypes.c_int(1 if THEME["mode"] == "dark" else 0)
            for attr in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4) == 0:
                    break
        except Exception:
            pass

    def color_widget(w, role):
        p = THEME["P"]
        try:
            if role in ("text", "text_ro", "log"):
                w.configure(background=p["surface2"] if role == "log" else p["surface"], foreground=p["text"],
                            insertbackground=p["text"], selectbackground=p["select"], selectforeground=p["text"],
                            inactiveselectbackground=p["select"], highlightbackground=p["border"],
                            highlightcolor=p["accent"])
                w.tag_configure("find", background=p["warn_bg"], foreground=p["text"])
                w.tag_configure("find_cur", background=p["accent"], foreground=p["on_accent"])
                w.tag_raise("find_cur")
            elif role == "doc":
                w.configure(background=p["surface"], foreground=p["text"], insertbackground=p["text"],
                            selectbackground=p["select"], selectforeground=p["text"],
                            inactiveselectbackground=p["select"], highlightbackground=p["border"],
                            highlightcolor=p["border"])
                w.tag_configure("find", background=p["warn_bg"], foreground=p["text"])
                w.tag_configure("find_cur", background=p["accent"], foreground=p["on_accent"])
                w.tag_configure("ts", foreground=p["muted"], font=F["doc_s"])
                w.tag_configure("spk", foreground=p["accent"], font=F["doc_b"])
                w.tag_raise("find")
                w.tag_raise("find_cur")
            elif role == "backdrop":
                w.configure(background=p["surface2"] if THEME["mode"] == "light" else p["bg"])
            elif role == "menu":
                w.configure(background=p["surface"], foreground=p["text"], activebackground=p["accent_soft"],
                            activeforeground=p["text"], disabledforeground=p["muted"], relief="flat",
                            borderwidth=1, font=F["body"])
            elif role == "window":
                w.configure(background=p["bg"])
                titlebar(w)
            elif role == "canvas":
                w.configure(background=p["bg"])
        except tk.TclError:
            pass

    def register(w, role):
        registered.append((w, role))
        color_widget(w, role)
        return w

    def fallback_styles(p):
        """A clean flat look for when the Sun Valley theme isn't installed."""
        style.theme_use("clam")
        style.configure(".", background=p["bg"], foreground=p["text"], font=F["body"], bordercolor=p["border"],
                        lightcolor=p["bg"], darkcolor=p["bg"], troughcolor=p["surface2"],
                        focuscolor=p["accent"], selectbackground=p["select"], selectforeground=p["text"],
                        fieldbackground=p["surface"], insertcolor=p["text"])
        style.configure("TButton", padding=(px(12), px(5)), relief="flat", background=p["surface"],
                        bordercolor=p["border"], lightcolor=p["surface"], darkcolor=p["surface"])
        style.map("TButton", background=[("pressed", p["surface2"]), ("active", p["accent_soft"])],
                  bordercolor=[("focus", p["accent"])])
        style.configure("Accent.TButton", background=p["accent"], foreground=p["on_accent"],
                        lightcolor=p["accent"], darkcolor=p["accent"], bordercolor=p["accent"])
        style.map("Accent.TButton", background=[("pressed", p["accent"]), ("active", p["accent"])],
                  foreground=[("disabled", p["muted"])])
        style.configure("TEntry", padding=px(5), fieldbackground=p["surface"], bordercolor=p["border"],
                        lightcolor=p["surface"], darkcolor=p["surface"])
        style.map("TEntry", bordercolor=[("focus", p["accent"])])
        style.configure("TCombobox", padding=px(4), fieldbackground=p["surface"], arrowcolor=p["text"])
        style.configure("TSpinbox", padding=px(4), fieldbackground=p["surface"], arrowcolor=p["text"])
        style.configure("TNotebook", background=p["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", padding=(px(14), px(6)), background=p["bg"], bordercolor=p["border"],
                        lightcolor=p["bg"], darkcolor=p["bg"])
        style.map("TNotebook.Tab", background=[("selected", p["surface"])],
                  lightcolor=[("selected", p["accent"])])
        style.configure("Treeview", background=p["surface"], fieldbackground=p["surface"], borderwidth=0)
        style.configure("TProgressbar", background=p["accent"], troughcolor=p["surface2"], bordercolor=p["surface2"],
                        lightcolor=p["accent"], darkcolor=p["accent"], thickness=px(6))
        style.configure("Card.TFrame", background=p["bg"], relief="solid", borderwidth=1, bordercolor=p["border"])
        style.configure("TLabelframe", background=p["bg"], bordercolor=p["border"])
        style.configure("TCheckbutton", background=p["bg"])
        style.configure("TRadiobutton", background=p["bg"])
        style.configure("Toolbutton", padding=(px(10), px(4)), background=p["bg"])

    def custom_styles(p):
        bg = p["bg"]
        style.configure("Title.TLabel", font=F["title"], foreground=p["text"])
        style.configure("Hero.TLabel", font=F["hero"], foreground=p["text"])
        style.configure("H2.TLabel", font=F["sub"], foreground=p["text"])
        style.configure("Strong.TLabel", font=F["strong"], foreground=p["text"])
        style.configure("Muted.TLabel", foreground=p["muted"])
        style.configure("Caption.TLabel", font=F["caption"], foreground=p["muted"])
        style.configure("Warn.TLabel", foreground=p["warn"])
        style.configure("Bad.TLabel", foreground=p["bad"])
        style.configure("BadStrong.TLabel", foreground=p["bad"], font=F["strong"])
        style.configure("Good.TLabel", foreground=p["good"])
        style.configure("Link.TLabel", foreground=p["accent"])
        style.configure("Badge.TLabel", font=F["hero"], foreground=p["accent"])
        for name, fg, cbg in (("Good", p["good"], p["good_bg"]), ("Bad", p["bad"], p["bad_bg"]),
                              ("Warn", p["warn"], p["warn_bg"]), ("Muted", p["muted"], p["surface2"]),
                              ("Accent", p["accent"], p["accent_soft"])):
            style.configure(f"Chip{name}.TLabel", foreground=fg, background=cbg, font=F["caption_strong"],
                            padding=(px(10), px(3)))
        style.configure("StepNow.TLabel", foreground=p["on_accent"], background=p["accent"], font=F["strong"],
                        padding=(px(14), px(6)))
        style.configure("StepDone.TLabel", foreground=p["accent"], background=p["accent_soft"], font=F["body"],
                        padding=(px(14), px(6)))
        style.configure("StepTodo.TLabel", foreground=p["muted"], background=p["surface2"], font=F["body"],
                        padding=(px(14), px(6)))
        style.configure("Side.TFrame", background=p["sidebar"])
        style.configure("SideCaption.TLabel", background=p["sidebar"], foreground=p["muted"],
                        font=F["caption_strong"])
        style.configure("Bar.TFrame", background=p["surface2"])
        style.configure("Bar.TLabel", background=p["surface2"], foreground=p["text"])
        style.configure("BarMuted.TLabel", background=p["surface2"], foreground=p["muted"])
        style.configure("Accent.TButton", font=F["strong"])
        style.configure("RowTitle.TLabel", font=F["strong_lg"], foreground=p["text"])
        # Table rows sized from the actual font height (the theme's fixed pixel height makes rows
        # overlap once Windows display scaling enlarges the text)
        line = F["body"].metrics("linespace")
        style.configure("Treeview", font=F["body"], rowheight=line + px(10))
        style.configure("Treeview.Heading", font=F["strong"], padding=(px(8), px(4)))
        for name in ("TButton", "TCheckbutton", "TRadiobutton", "TMenubutton", "Toolbutton"):
            style.configure(name, font=F["body"])
        style.configure("Side.TCheckbutton", background=p["sidebar"])
        style.map("Side.TCheckbutton", background=[("active", p["sidebar"]), ("!active", p["sidebar"])])
        # list rows match the list's own field color, so the list reads as one clean panel
        for name, rowbg, rh, fnt in (("Side.Treeview", bg, 36, F["body_lg"]),
                                     ("List.Treeview", bg, 34, F["body"])):
            style.configure(name, background=rowbg, fieldbackground=rowbg, foreground=p["text"],
                            rowheight=max(px(rh), fnt.metrics("linespace") + px(12)), font=fnt, borderwidth=0, indent=0)
            style.map(name, background=[("selected", p["accent_soft"])],
                      foreground=[("selected", p["text"])])
            # plain rows: no tree arrow / indent column
            style.layout(f"{name}.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
                ("Treeitem.text", {"side": "left", "sticky": ""})]})])
            style.configure(f"{name}.Item", padding=(px(12), 0))
        if not THEME["sv"]:
            style.layout("Side.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        row_dividers(p)

    # Every list and table row gets a thin divider line along its bottom edge, so patients,
    # sessions and table rows read as separate items rather than stacked text. The line is a tiny
    # stretchable image under Tk's normal row background (which keeps the selection highlight);
    # it's repainted when the theme changes.
    ROW_IMG = {}

    def row_dividers(p):
        t = max(1, px(1))
        img = ROW_IMG.get("line")
        if img is None:
            img = ROW_IMG["line"] = tk.PhotoImage(master=root, width=3, height=t)
            try:
                style.element_create("Divider.line", "image", img, border=(1, 0, 1, 0), sticky="ew")
            except tk.TclError:
                pass
        img.put(p["border"], to=(0, 0, 3, t))
        for sty in ("List.Treeview", "Side.Treeview", "Treeview"):
            try:
                style.layout(f"{sty}.Row", [("Divider.line", {"side": "bottom", "sticky": "ew"}),
                                            ("Treeitem.row", {"sticky": "nswe"})])
            except tk.TclError:
                pass

    # The Windows 11 theme sizes its fonts in pixels, which don't grow with Windows display scaling
    # (125–200%) while everything else does — tiny body text next to big headings. Re-size them in
    # points so the whole app scales together.
    SV_FONT_POINTS = {"SunValleyCaptionFont": 9, "SunValleyBodyFont": 10, "SunValleyBodyStrongFont": 10,
                      "SunValleyBodyLargeFont": 12, "SunValleySubtitleFont": 15, "SunValleyTitleFont": 20,
                      "SunValleyTitleLargeFont": 28, "SunValleyDisplayFont": 40}

    def fix_fonts():
        names = set(tkfont.names(root))
        for name, pt in SV_FONT_POINTS.items():
            if name in names:
                try:
                    tkfont.nametofont(name, root).configure(size=fs(pt))
                except tk.TclError:
                    pass
        for name, pt in (("TkDefaultFont", 10), ("TkTextFont", 10), ("TkMenuFont", 10),
                         ("TkHeadingFont", 10), ("TkCaptionFont", 10), ("TkTooltipFont", 9)):
            if name in names:
                try:
                    tkfont.nametofont(name, root).configure(family=BODY, size=fs(pt))
                except tk.TclError:
                    pass

    def base_styles(p):
        """Set the base colors and font right away. (The theme normally does this a moment later in
        response to an event; on some PCs that never arrives and every label falls back to Tk's
        grey #d9d9d9 — the 'boxes' behind text.)"""
        bg = p["bg"]
        style.configure(".", background=bg, foreground=p["text"], font=F["body"], troughcolor=bg,
                        selectbackground=p["select"], selectforeground=p["text"], insertcolor=p["text"])
        style.map(".", foreground=[("disabled", p["muted"])])
        for name in ("TFrame", "TLabel", "TCheckbutton", "TRadiobutton", "TLabelframe", "TLabelframe.Label"):
            style.configure(name, background=bg)
        style.configure("TLabelframe.Label", foreground=p["muted"], font=F["caption_strong"])
        # Cards: a flat bordered panel in the page color, so text inside never sits in a
        # different-colored box (the theme's card picture is a slightly different shade).
        style.layout("Card.TFrame", [("Frame.border", {"sticky": "nswe"})])
        style.configure("Card.TFrame", background=bg, relief="solid", borderwidth=1, bordercolor=p["border"],
                        lightcolor=p["border"], darkcolor=p["border"])

    def apply_theme(mode):
        resolved = mode if mode in ("light", "dark") else system_mode()
        THEME.update(mode=resolved, P=PALETTES[resolved])
        try:
            import sv_ttk
            sv_ttk.set_theme(resolved, root)
            THEME["sv"] = True
            try:
                root.tk.call("configure_colors")       # what the theme would do later — do it now
            except tk.TclError:
                pass
        except Exception:
            THEME["sv"] = False
            fallback_styles(THEME["P"])
        fix_fonts()
        base_styles(THEME["P"])
        custom_styles(THEME["P"])
        root.configure(background=P("bg"))
        titlebar(root)
        for w, role in list(registered):
            try:
                if w.winfo_exists():
                    color_widget(w, role)
                    continue
            except tk.TclError:
                pass
            registered.remove((w, role))

    def new_window(parent, title="", size=None):
        win = tk.Toplevel(parent)
        if title:
            win.title(title)
        win.transient(parent)
        register(win, "window")
        if size:
            w, h = (int(x) for x in scaled(size).split("x"))
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            w, h = min(w, int(sw * 0.94)), min(h, int(sh * 0.88))
            win.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
            win.minsize(min(w, px(520)), min(h, px(380)))

            def fit_height():
                # don't let the window be dragged shorter than its own content needs (buttons
                # would be cut off); never more than the size it opened at
                try:
                    if win.winfo_exists():
                        need = min(win.winfo_reqheight(), h, int(sh * 0.85))
                        cur_w, cur_h = win.minsize()
                        win.minsize(cur_w, max(cur_h, need))
                except tk.TclError:
                    pass
            win.after(400, fit_height)
        return win

    def tree_box(parent, **kw):
        """A Treeview with its own vertical scrollbar, so every row stays reachable in a short window.
        Returns (box, tree); pack/grid the box."""
        box = ttk.Frame(parent)
        tv = ttk.Treeview(box, **kw)
        sb = ttk.Scrollbar(box, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tv.pack(side="left", fill="both", expand=True)
        return box, tv

    def auto_scroll(parent, make):
        """Put a list in a frame with a scrollbar that only shows up when the rows don't all fit.
        Returns (box, widget); pack the box."""
        box = ttk.Frame(parent)
        w = make(box)
        sb = ttk.Scrollbar(box, orient="vertical", command=w.yview)

        def setter(first, last):
            sb.set(first, last)
            need = not (float(first) <= 0.0 and float(last) >= 1.0)
            if need and not sb.winfo_ismapped():
                sb.pack(side="right", fill="y", before=w)
            elif not need and sb.winfo_ismapped():
                sb.pack_forget()
        w.configure(yscrollcommand=setter)
        w.pack(side="left", fill="both", expand=True)
        return box, w

    def scrollable(parent, padding=0):
        """A vertically scrolling area (mouse wheel works); returns (outer, inner)."""
        outer = ttk.Frame(parent)
        canvas = register(tk.Canvas(outer, highlightthickness=0, borderwidth=0), "canvas")
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, padding=padding)
        item = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(item, width=e.width))
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        def wheel(e):
            if canvas.winfo_exists() and canvas.yview() != (0.0, 1.0):
                canvas.yview_scroll(-1 if (e.num == 4 or e.delta > 0) else 1, "units")

        def enter(_):
            canvas.bind_all("<MouseWheel>", wheel)
            canvas.bind_all("<Button-4>", wheel)
            canvas.bind_all("<Button-5>", wheel)

        def leave(_):
            for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                canvas.unbind_all(ev)
        outer.bind("<Enter>", enter)
        outer.bind("<Leave>", leave)
        return outer, inner

    def _autowrap(e):
        """Any label that wraps (wraplength set) re-wraps to the width it really has, so long text
        never forces the window wider than the screen or pushes buttons off the edge."""
        w = e.widget
        if not hasattr(w, "cget"):          # Tk-internal widget (e.g. inside a dropdown): not ours
            return
        try:
            wl = int(str(w.cget("wraplength")) or 0)
        except (tk.TclError, ValueError):
            return
        if wl <= 0:
            return
        try:
            mgr = w.winfo_manager()
            if mgr == "pack":
                info = w.pack_info()
                if not getattr(w, "_aw", False):
                    w._aw = True
                    if info.get("side", "top") in ("top", "bottom") and info.get("fill") in ("none", "y"):
                        w.pack_configure(fill="x")
                        info["fill"] = "x"
                if info.get("fill") not in ("x", "both"):
                    return                 # not stretched to its row: its width just follows the text
            elif mgr == "grid":
                sticky = str(w.grid_info().get("sticky", ""))
                if not ("e" in sticky and "w" in sticky):
                    return
            else:
                return
            new = max(px(140), e.width - px(4))
            if abs(new - wl) > px(3) and e.width > px(60):
                w.configure(wraplength=new)
        except tk.TclError:
            pass

    root.bind_class("TLabel", "<Configure>", _autowrap, add="+")

    def pin_bottom(w, **opts):
        """Pack a button bar at the bottom edge BEFORE everything else in its container, so a
        short window shrinks the content above it — the buttons always stay visible."""
        slaves = [x for x in w.master.pack_slaves() if x is not w]
        w.pack(side="bottom", fill=opts.pop("fill", "x"), before=slaves[0] if slaves else None, **opts)
        return w

    class Flow(ttk.Frame):
        """A row of buttons/controls that wraps onto more lines when the window is too narrow,
        so nothing is ever cut off. Children are created with this as parent, then .add()ed in
        left-to-right order. align="right" right-justifies each line."""

        def __init__(self, parent, align="left", gap=None, vgap=None, **kw):
            super().__init__(parent, **kw)
            self.items, self.align = [], align
            self.gap = px(6) if gap is None else gap
            self.vgap = px(6) if vgap is None else vgap
            self._lastw, self._rows, self._pending = None, [], False
            self.hidden = set()
            self.bind("<Configure>", self._on_conf)

        def show(self, w, visible=True):
            """Show or hide an added widget (keeps its place in the order)."""
            (self.hidden.discard if visible else self.hidden.add)(str(w))
            if not visible:
                w.pack_forget()
            self._layout = None
            self._schedule()

        def add(self, w, gap=None):
            self.items.append((w, self.gap if gap is None else gap))
            self._schedule()
            return w

        def _schedule(self):
            if not self._pending:
                self._pending = True
                self.after_idle(self._reflow)

        def _on_conf(self, e):
            if e.width != self._lastw:
                self._lastw = e.width
                self._schedule()

        def _reflow(self):
            self._pending = False
            if not self.winfo_exists():
                return
            width = self.winfo_width()
            if width <= 1:
                width = 10 ** 6
            rows, cur = [[]], 0
            for w, g in self.items:
                if not w.winfo_exists() or str(w) in self.hidden:
                    continue
                need = w.winfo_reqwidth() + (g if rows[-1] else 0)
                if rows[-1] and cur + need > width:
                    rows.append([])
                    cur, need = 0, w.winfo_reqwidth()
                rows[-1].append((w, g))
                cur += need
            layout = [[w for w, _ in r] for r in rows]
            if layout == getattr(self, "_layout", None):
                return
            self._layout = layout
            for r in self._rows:
                for ch in r.pack_slaves():
                    ch.pack_forget()
                r.destroy()
            self._rows = []
            style_ = self.cget("style")
            for i, row in enumerate(rows):
                fr = ttk.Frame(self, style=style_) if style_ else ttk.Frame(self)
                fr.pack(side="top", anchor="e" if self.align == "right" else "w",
                        pady=(0 if i == 0 else self.vgap, 0))
                self._rows.append(fr)
                for j, (w, g) in enumerate(row):
                    w.pack(in_=fr, side="left", padx=(0 if j == 0 else g, 0))
                    w.lift(fr)

    class NiceList(ttk.Treeview):
        """A Windows-11-style list (Treeview) with the small Listbox API the app uses."""

        def __init__(self, parent, style_name="List.Treeview"):
            super().__init__(parent, show="tree", selectmode="browse", style=style_name)
            self.column("#0", stretch=True, width=px(250))
            self._ids, self._prog = [], ()       # _prog: the selection last set from code

        def insert(self, index, text):                      # noqa: A003  (Listbox-like)
            self._ids.append(super().insert("", "end", text=text))

        def delete(self, *args):
            if self._ids:
                super().delete(*self._ids)
            self._ids, self._prog = [], ()

        def curselection(self):
            return tuple(self._ids.index(i) for i in self.selection() if i in self._ids)

        def selection_set(self, index):
            self._prog = (self._ids[index],)
            super().selection_set(self._ids[index])

        def selection_clear(self, *args):
            self._prog = ()
            cur = self.selection()
            if cur:
                super().selection_remove(*cur)

        def see(self, index):
            super().see(self._ids[index])

        def get(self, index):
            return self.item(self._ids[index], "text")

        def size(self):
            return len(self._ids)

        def bind(self, sequence=None, func=None, add=None):
            if sequence == "<<ListboxSelect>>":
                def handler(e, func=func):
                    # Treeview reports selection changes made from code too; only react
                    # when the user picked something different from what the code set.
                    sel = tuple(self.selection())
                    if sel == self._prog:
                        return
                    self._prog = sel
                    return func(e)
                return super().bind("<<TreeviewSelect>>", handler, add)
            return super().bind(sequence, func, add)

    apply_theme(cfg.get("appearance", "system"))

    class FindBar:
        """Find-in-document bar (Ctrl+F): highlights every match, "3 of 12", Enter /
        Shift+Enter or the arrows to step through, Esc to close."""

        def __init__(self, outer, text, inner):
            self.t, self.inner, self.matches, self.idx = text, inner, [], -1
            self.bar = ttk.Frame(outer, padding=(0, 0, 0, px(6)))
            self.var = tk.StringVar()
            ttk.Label(self.bar, text="🔍", style="Muted.TLabel").pack(side="left", padx=(0, px(6)))
            self.entry = ttk.Entry(self.bar, textvariable=self.var, width=30)
            self.entry.pack(side="left")
            self.count = ttk.Label(self.bar, text="", style="Muted.TLabel", width=14)
            self.count.pack(side="left", padx=px(8))
            ttk.Button(self.bar, text="↑", width=3, command=lambda: self.step(-1)).pack(side="left")
            ttk.Button(self.bar, text="↓", width=3, command=lambda: self.step(1)).pack(side="left", padx=px(4))
            ttk.Button(self.bar, text="✕", width=3, command=self.hide).pack(side="left")
            self.var.trace_add("write", lambda *_: self.run())
            self.entry.bind("<Return>", lambda e: self.step(1))
            self.entry.bind("<Shift-Return>", lambda e: self.step(-1))
            self.entry.bind("<Escape>", lambda e: self.hide())
            self.entry.bind("<F3>", lambda e: self.step(1))

        def show(self, query=None, focus=True):
            if not self.bar.winfo_ismapped():
                self.bar.pack(fill="x", before=self.inner)
            if query is not None:
                self.var.set(query)
            else:
                sel = ""
                try:
                    sel = self.t.get("sel.first", "sel.last")
                except tk.TclError:
                    pass
                if sel and "\n" not in sel and len(sel) < 80:
                    self.var.set(sel)
                self.run()
            if focus:
                self.entry.focus_set()
                self.entry.select_range(0, "end")

        def hide(self):
            self.bar.pack_forget()
            self.clear()
            self.t.focus_set()

        def clear(self):
            self.t.tag_remove("find", "1.0", "end")
            self.t.tag_remove("find_cur", "1.0", "end")
            self.matches, self.idx = [], -1

        def run(self):
            self.clear()
            q = self.var.get()
            if not q.strip():
                self.count.config(text="")
                return
            start, n = "1.0", tk.IntVar()
            while len(self.matches) < 5000:
                pos = self.t.search(q, start, stopindex="end", nocase=True, count=n)
                if not pos or not n.get():
                    break
                end = f"{pos}+{n.get()}c"
                self.t.tag_add("find", pos, end)
                self.matches.append((pos, end))
                start = end
            if not self.matches:
                self.count.config(text="No matches", style="Bad.TLabel")
                return
            self.count.config(style="Muted.TLabel")
            self.step(1, first=True)

        def step(self, d, first=False):
            if not self.matches:
                return
            self.idx = 0 if first else (self.idx + d) % len(self.matches)
            pos, end = self.matches[self.idx]
            self.t.tag_remove("find_cur", "1.0", "end")
            self.t.tag_add("find_cur", pos, end)
            self.t.see(pos)
            self.count.config(text=f"{self.idx + 1} of {len(self.matches)}")

    def set_doc_zoom(step):
        """A− / A+ in any document view: resizes every document's text together (remembered)."""
        z = max(-3, min(6, int(cfg.get("doc_zoom", 0)) + step))
        cfg["doc_zoom"] = z
        F["doc"].configure(size=fs(11 + z))
        F["doc_b"].configure(size=fs(11 + z))
        F["doc_h"].configure(size=fs(13 + z))
        F["doc_s"].configure(size=fs(9 + z))
        try:
            save_config(cfg)
        except Exception:
            pass

    DOC_HEAD = re.compile(r"^(#{1,3}\s+.+|[A-Z][A-Za-z0-9 /&()'’,-]{2,70}:|[A-Z0-9][A-Z0-9 /&()'’,.-]{3,70})$")
    DOC_LABEL = re.compile(r"^([A-Z][A-Za-z0-9/()&'’ -]{1,40}):\s+\S")
    DOC_TURN = re.compile(r"^(\[\d{1,2}:\d{2}(?::\d{2})?\])\s+([^:\[\]]{1,32}:)")

    def restyle_doc(t):
        """Headings, bold field labels and transcript speakers — recomputed from the plain text,
        so what's saved and copied stays plain text."""
        for tag in ("h", "lbl", "ts", "spk"):
            t.tag_remove(tag, "1.0", "end")
        n = int(t.index("end-1c").split(".")[0])
        words = 0
        for i in range(1, n + 1):
            line = t.get(f"{i}.0", f"{i}.end")
            st = line.strip()
            if not st:
                continue
            words += len(st.split())
            m = DOC_TURN.match(line)
            if m:
                t.tag_add("ts", f"{i}.0", f"{i}.{m.end(1)}")
                t.tag_add("spk", f"{i}.{m.start(2)}", f"{i}.{m.end(2)}")
                continue
            if len(st) <= 72 and DOC_HEAD.match(st) and re.search(r"[A-Za-z]{2}", st) and not st.endswith("."):
                t.tag_add("h", f"{i}.0", f"{i}.end")
                continue
            m = DOC_LABEL.match(line)
            if m and len(m.group(1).split()) <= 5:
                t.tag_add("lbl", f"{i}.0", f"{i}.{m.end(1) + 1}")
        cnt = getattr(t, "count_label", None)
        if cnt is not None and cnt.winfo_exists():
            cnt.config(text=f"{words:,} word{'s' if words != 1 else ''}")

    def scrolled_text(parent, height=10, readonly=False, font_size=10, doc=False):
        if doc:
            return doc_text(parent, height, readonly)
        fr = ttk.Frame(parent)
        inner = ttk.Frame(fr)
        inner.pack(fill="both", expand=True)
        t = tk.Text(inner, wrap="word", undo=True, height=height, relief="flat", borderwidth=0,
                    highlightthickness=1, font=F["body_lg"] if font_size > 10 else F["body"],
                    padx=px(14), pady=px(10), spacing1=px(1), spacing3=px(3))
        register(t, "text_ro" if readonly else "text")
        sb = ttk.Scrollbar(inner, orient="vertical", command=t.yview)
        t.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(px(2), 0))
        t.pack(side="left", fill="both", expand=True)
        t.finder = FindBar(fr, t, inner)

        def open_find(_=None):
            t.finder.show()
            return "break"
        t.bind(f"<{MOD}-f>", open_find)
        t.bind(f"<{MOD}-F>", open_find)
        t.bind("<F3>", lambda e: (t.finder.step(1), "break")[1])
        if readonly:
            t.config(state="disabled")
            t.bind("<1>", lambda e: t.focus_set())          # so Ctrl+F works after clicking
        return fr, t

    def doc_text(parent, height=10, readonly=False):
        """A document view that feels like a word processor: a centered page on a soft backdrop,
        comfortable type and spacing, auto-styled headings, zoom, and a live word count."""
        fr = ttk.Frame(parent)
        tb = ttk.Frame(fr)
        tb.pack(fill="x", pady=(0, px(4)))
        inner = ttk.Frame(fr)
        inner.pack(fill="both", expand=True)
        back = register(tk.Frame(inner, borderwidth=0, highlightthickness=0), "backdrop")
        sb = ttk.Scrollbar(inner, orient="vertical")
        sb.pack(side="right", fill="y", padx=(px(2), 0))
        back.pack(side="left", fill="both", expand=True)
        t = tk.Text(back, wrap="word", undo=True, height=height, relief="flat", borderwidth=0,
                    highlightthickness=1, font=F["doc"], padx=px(52), pady=px(36),
                    spacing1=px(2), spacing2=px(4), spacing3=px(8), cursor="xterm")
        register(t, "doc")
        t.tag_configure("h", font=F["doc_h"], spacing1=px(12), spacing3=px(6))
        t.tag_configure("lbl", font=F["doc_b"])
        t.pack(fill="both", expand=True, padx=px(16), pady=(px(14), 0))
        sb.config(command=t.yview)
        t.config(yscrollcommand=sb.set)

        def fit(e):
            # keep the page a readable width, centered, like a document on screen
            m = max(px(14), (e.width - px(880)) // 2)
            t.pack_configure(padx=m)
        back.bind("<Configure>", fit)
        t.finder = FindBar(fr, t, inner)

        def open_find(_=None):
            t.finder.show()
            return "break"
        t.bind(f"<{MOD}-f>", open_find)
        t.bind(f"<{MOD}-F>", open_find)
        t.bind("<F3>", lambda e: (t.finder.step(1), "break")[1])
        # toolbar: undo/redo (editable), zoom, word count
        if not readonly:
            ttk.Button(tb, text="↶", width=3, style="Toolbutton",
                       command=lambda: t.event_generate("<<Undo>>")).pack(side="left")
            ttk.Button(tb, text="↷", width=3, style="Toolbutton",
                       command=lambda: t.event_generate("<<Redo>>")).pack(side="left")
            ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=px(6), pady=px(3))
        ttk.Button(tb, text="A−", width=3, style="Toolbutton", command=lambda: set_doc_zoom(-1)).pack(side="left")
        ttk.Button(tb, text="A+", width=3, style="Toolbutton", command=lambda: set_doc_zoom(1)).pack(side="left")
        t.count_label = ttk.Label(tb, text="", style="Caption.TLabel")
        t.count_label.pack(side="right", padx=(0, px(4)))
        pending = {"id": None}

        def later(_=None):
            if pending["id"]:
                t.after_cancel(pending["id"])
            pending["id"] = t.after(350, lambda: restyle_doc(t))
        t.bind("<KeyRelease>", later, add="+")
        t.bind("<<Paste>>", later, add="+")
        t.restyle = lambda: restyle_doc(t)
        if readonly:
            t.config(state="disabled")
            t.bind("<1>", lambda e: t.focus_set())
        return fr, t

    def open_calendar(anchor, var, on_pick=None):
        """A small month calendar under `anchor`. Pick a month/year, click a day → YYYY-MM-DD."""
        import calendar as _cal
        try:
            cur = date.fromisoformat(var.get().strip())
        except ValueError:
            cur = None
        shown = {"y": (cur or date.today()).year, "m": (cur or date.today()).month}
        top = tk.Toplevel(anchor)
        top.withdraw()
        top.overrideredirect(True)
        top.transient(anchor.winfo_toplevel())
        p = THEME["P"]
        outer = tk.Frame(top, background=p["border"], padx=1, pady=1)
        outer.pack()
        box = tk.Frame(outer, background=p["surface"], padx=px(10), pady=px(8))
        box.pack()
        head = tk.Frame(box, background=p["surface"])
        head.pack(fill="x")
        months = list(_cal.month_name)[1:]
        m_var = tk.StringVar(value=months[shown["m"] - 1])
        y_var = tk.StringVar(value=str(shown["y"]))
        grid = tk.Frame(box, background=p["surface"])
        grid.pack(pady=(px(6), 0))

        def close(_=None):
            try:
                top.grab_release()
            except tk.TclError:
                pass
            top.destroy()

        def pick(d_):
            var.set(d_.isoformat())
            close()
            if on_pick:
                on_pick(d_.isoformat())

        def cell(parent, text, r, c, fg, bg, font, cmd=None, hover=True):
            w = tk.Label(parent, text=text, width=3, background=bg, foreground=fg, font=font,
                         padx=px(4), pady=px(3), cursor="hand2" if cmd else "")
            w.grid(row=r, column=c, padx=1, pady=1)
            if cmd:
                w.bind("<Button-1>", lambda e: cmd())
                if hover:
                    w.bind("<Enter>", lambda e: w.config(background=p["accent_soft"]))
                    w.bind("<Leave>", lambda e: w.config(background=bg))
            return w

        def draw():
            for ch in grid.winfo_children():
                ch.destroy()
            for i, wd in enumerate(["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"]):
                cell(grid, wd, 0, i, p["muted"], p["surface"], F["caption_strong"])
            weeks = _cal.Calendar(firstweekday=6).monthdatescalendar(shown["y"], shown["m"])
            today = date.today()
            for r, week in enumerate(weeks, 1):
                for c, d_ in enumerate(week):
                    inside = d_.month == shown["m"]
                    sel = cur is not None and d_ == cur
                    fg = p["on_accent"] if sel else (p["text"] if inside else p["border"])
                    bg = p["accent"] if sel else p["surface"]
                    font = F["strong"] if (d_ == today or sel) else F["body"]
                    w = cell(grid, str(d_.day), r, c, fg, bg, font, cmd=lambda d_=d_: pick(d_), hover=not sel)
                    if d_ == today and not sel:
                        w.config(foreground=p["accent"])
            m_var.set(months[shown["m"] - 1])
            y_var.set(str(shown["y"]))

        def move(k):
            m = shown["m"] - 1 + k
            shown["y"], shown["m"] = shown["y"] + m // 12, m % 12 + 1
            draw()

        def jump(_=None):
            try:
                shown["m"] = months.index(m_var.get()) + 1
                shown["y"] = max(1900, min(2100, int(y_var.get())))
            except ValueError:
                pass
            draw()
        ttk.Button(head, text="‹", width=2, style="Toolbutton", command=lambda: move(-1)).pack(side="left")
        mcb = ttk.Combobox(head, textvariable=m_var, values=months, state="readonly", width=10)
        mcb.pack(side="left", padx=px(4))
        mcb.bind("<<ComboboxSelected>>", jump)
        ysp = ttk.Spinbox(head, from_=1990, to=2100, textvariable=y_var, width=6, command=jump)
        ysp.pack(side="left")
        ysp.bind("<Return>", jump)
        ysp.bind("<FocusOut>", jump)
        ttk.Button(head, text="›", width=2, style="Toolbutton", command=lambda: move(1)).pack(side="left",
                                                                                              padx=(px(4), 0))
        foot = tk.Frame(box, background=p["surface"])
        foot.pack(fill="x", pady=(px(6), 0))
        ttk.Button(foot, text="Today", command=lambda: pick(date.today())).pack(side="left")
        ttk.Button(foot, text="Close", command=close).pack(side="right")
        draw()
        top.update_idletasks()
        x, y = anchor.winfo_rootx(), anchor.winfo_rooty() + anchor.winfo_height() + px(2)
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        x = min(x, sw - top.winfo_reqwidth() - 8)
        if y + top.winfo_reqheight() > sh - 40:
            y = anchor.winfo_rooty() - top.winfo_reqheight() - px(2)
        top.geometry(f"+{x}+{y}")
        top.deiconify()
        top.lift()
        top.bind("<Escape>", close)
        try:
            top.grab_set()
        except tk.TclError:
            pass
        # clicking anywhere outside the calendar closes it
        def outside(e):
            wx, wy = top.winfo_rootx(), top.winfo_rooty()
            if not (wx <= e.x_root <= wx + top.winfo_width() and wy <= e.y_root <= wy + top.winfo_height()):
                close()
        top.bind("<Button-1>", outside, add="+")
        top.focus_force()
        anchor.winfo_toplevel()._calendar = top          # for tests
        return top

    def date_field(parent, var, width=12, on_pick=None):
        """Date entry with a 📅 button that opens the mini calendar. Typing still works.
        `var` always holds YYYY-MM-DD (or the raw text while it isn't a valid date yet);
        the box shows and accepts the date format chosen in Settings."""
        fr = ttk.Frame(parent)
        disp = tk.StringVar(value=fmt_date(var.get()))
        busy = {"on": False}

        def from_var(*_):
            if not busy["on"]:
                busy["on"] = True
                try:
                    disp.set(fmt_date(var.get()))
                finally:
                    busy["on"] = False

        def from_box(*_):
            if not busy["on"]:
                busy["on"] = True
                try:
                    raw = disp.get().strip()
                    var.set(parse_date(raw) or raw)
                finally:
                    busy["on"] = False
        var.trace_add("write", from_var)
        disp.trace_add("write", from_box)
        fr._date_vars = (var, disp)          # keep the traces alive with the widget
        e = ttk.Entry(fr, textvariable=disp, width=max(width, 13))
        e.bind("<FocusOut>", lambda _e: from_var(), add="+")     # tidy what was typed
        e.pack(side="left")
        b = ttk.Button(fr, text="📅", width=3, command=lambda: open_calendar(e, var, on_pick))
        b.pack(side="left", padx=(px(2), 0))
        fr.entry, fr.button = e, b
        return fr

    def find_button(parent, t, text="🔍 Find"):
        return ttk.Button(parent, text=text, command=lambda: t.finder.show())

    def get_text(t):
        return t.get("1.0", "end-1c")

    def set_text(t, s):
        ro = str(t.cget("state")) == "disabled"
        t.config(state="normal")
        t.delete("1.0", "end")
        t.insert("1.0", s or "")
        t.edit_reset()
        t.edit_modified(False)
        if getattr(t, "restyle", None):
            t.restyle()
        if ro:
            t.config(state="disabled")
        finder = getattr(t, "finder", None)
        if finder is not None and finder.bar.winfo_ismapped():
            finder.run()

    # ------------------------------------------------------------ password
    def password_screen(on_ok):
        frame = ttk.Frame(root, style="Card.TFrame", padding=(px(40), px(32)))
        frame.place(relx=0.5, rely=0.45, anchor="center")
        creating = not vault.exists
        ttk.Label(frame, text="🔐", font=F["hero"]).pack(anchor="w")
        ttk.Label(frame, text=APP_NAME, style="Title.TLabel").pack(anchor="w", pady=(px(4), px(2)))
        ttk.Label(frame, text=("Create a vault password. Everything you save is encrypted with it."
                               if creating else "Enter your vault password to continue."),
                  style="Muted.TLabel").pack(anchor="w", pady=(0, px(18)))
        ttk.Label(frame, text="Password", style="Caption.TLabel").pack(anchor="w")
        pw1 = ttk.Entry(frame, show="•", width=38)
        pw1.pack(fill="x", pady=(px(2), px(10)))
        pw2 = ttk.Entry(frame, show="•", width=38)
        if creating:
            ttk.Label(frame, text="Confirm password", style="Caption.TLabel").pack(anchor="w")
            pw2.pack(fill="x", pady=(px(2), px(10)))
            ttk.Label(frame, text="There's no password recovery. If it's lost, saved sessions can't be "
                                  "opened. Use 10+ characters.", style="Warn.TLabel", wraplength=px(330),
                      justify="left").pack(anchor="w", pady=(0, px(6)))
        msg = ttk.Label(frame, text="", style="Bad.TLabel")
        msg.pack(anchor="w", pady=(0, px(6)))

        def submit(_=None):
            p = pw1.get()
            if creating:
                if len(p) < 10:
                    return msg.config(text="Use at least 10 characters.")
                if p != pw2.get():
                    return msg.config(text="Passwords don't match.")
                vault.create(p)
            elif not vault.unlock(p):
                pw1.delete(0, "end")
                return msg.config(text="Incorrect password.")
            frame.destroy()
            on_ok()

        ttk.Button(frame, text="Create vault" if creating else "Unlock", style="Accent.TButton",
                   command=submit).pack(fill="x", ipady=px(3))
        pw1.bind("<Return>", submit)
        pw2.bind("<Return>", submit)
        pw1.focus_set()

    # ================================================================= App
    class App:
        def __init__(self):
            self.settings = vault.load_settings()
            self.settings.setdefault("hf_token", os.environ.get("HF_TOKEN", ""))
            self.settings.setdefault("orientation", "Psychodynamic and person-centered")
            self.msgs = queue.Queue()
            self.busy = False
            self.waitlist = []          # tasks you started while another was running; they run in order
            self.locked = False
            self.clip = None
            # Ctrl+C / Ctrl+X in notes and transcripts go through the private clipboard too
            root.bind_class("Text", "<<Copy>>", lambda e: self._text_copy(e.widget, cut=False))
            root.bind_class("Text", "<<Cut>>", lambda e: self._text_copy(e.widget, cut=True))
            self.last_activity = time.time()
            self.page = None
            self.client_id = None
            self.wizard = None             # the session wizard currently on screen
            self._page_editing = False
            self.parked = None             # an unfinished session you navigated away from
            self.task_label = ""           # what the background task is doing
            self.lm_status = None          # None = checking, list = models, str = error
            self.progress_label = None
            self.setup_win = None
            self._compat = None
            self.clients = []
            computer_id(cfg)                       # make this computer's handoff id once, up front
            # recordings handed over from the other computer are decrypted here only while they're
            # being transcribed; clear any left behind if the app was closed mid-way
            for fn in (os.listdir(os.path.join(LOCAL_DIR, "remote-audio"))
                       if os.path.isdir(os.path.join(LOCAL_DIR, "remote-audio")) else []):
                _quiet(wipe_file, os.path.join(LOCAL_DIR, "remote-audio", fn))
            self._build_shell()
            tidied = (0, 0)
            try:
                tidied = tidy_all_imported(vault)     # once per imported record, all patients
            except Exception:
                pass                                   # never block opening the app over this
            self.refresh_clients()
            self.show_home()
            if any(tidied):
                self.set_status(f"Tidied {tidied[0]} previously imported note(s)"
                                + (f" and {tidied[1]} treatment plan(s)" if tidied[1] else "")
                                + ": letterhead, Insurer(s), Staff Signature and plain-WNL items removed; "
                                  "primary focus moved to the top.")
            self.check_lmstudio()
            for ev in ("<Key>", "<Motion>", "<Button>"):
                root.bind_all(ev, lambda e: setattr(self, "last_activity", time.time()), add="+")
            self._search_entry = None
            root.bind_all(f"<{MOD}-f>", self.global_find)
            root.bind_all(f"<{MOD}-F>", self.global_find)
            root.protocol("WM_DELETE_WINDOW", self.on_close)
            self.poll()
            self.idle_check()
            self._sig = vault.signature()
            self.view_select = None
            root.after(10_000, self.sync_check)
            root.after(4_000, self.remote_tick)

        # ------------------------------------------------- multi-computer sync
        def sync_check(self):
            """Every 10 s: if files changed underneath us (synced from the other computer
            by Google Drive), refresh what's on screen. Never interrupts a session in
            progress or an open dialog."""
            try:
                sig = vault.signature()
                if sig != self._sig and not self.locked:
                    # only hold off while a modal dialog has the focus; the queue and Settings
                    # windows can stay open (the queue is often open while waiting for results)
                    dialog_open = bool(str(root.tk.call("grab", "current")))   # (a message box counts too)
                    if not dialog_open and not self._user_typing():
                        self._sig = sig
                        self.settings = vault.load_settings() or self.settings
                        self.refresh_clients()
                        if self._page_editing:
                            pass                       # keep the edit in progress; it refreshes after Save
                        elif self.wizard is None and self.client_id:
                            if any(c["id"] == self.client_id for c in self.all_clients):
                                self.open_client(self.client_id, select=self.view_select)
                            else:
                                self.show_home()
                        elif self.wizard is None and self.client_id is None:
                            self.show_home()
                        if not self.busy:
                            self.set_status("Updated with changes synced from your other computer.")
            finally:
                self.maybe_mark_sync()
                root.after(10_000, self.sync_check)

        def _user_typing(self):
            """True while the cursor is in an editable box on the page (a question being typed,
            a search…) — re-drawing the page then would throw that text away."""
            try:
                w = root.focus_get()
            except (KeyError, tk.TclError):
                return False
            if w is None or self.page is None or not str(w).startswith(str(self.page)):
                return False
            cls = w.winfo_class()
            if cls == "Text":
                return str(w.cget("state")) == "normal" and bool(w.get("1.0", "end-1c").strip())
            if cls in ("TEntry", "Entry"):
                return str(w.cget("state")) not in ("disabled", "readonly") and bool(w.get().strip())
            return False

        def maybe_mark_sync(self, force=False):
            """Keep this computer's sync marker current (in the background, at most once a minute)."""
            sig = getattr(self, "_sig", None)
            if not force and (sig == getattr(self, "_marked_sig", object())
                              or time.time() - getattr(self, "_marked_at", 0) < 60):
                return
            self._marked_sig, self._marked_at = sig, time.time()
            r = data_root(cfg)
            threading.Thread(target=lambda: _quiet(write_sync_marker, r), daemon=True).start()

        def sync_now(self, on_result=None):
            """Setup → Sync now: pick up everything that's arrived from the other computer, refresh
            the screen, record what this computer has, and report whether both sides match."""
            if self.wizard is None:
                self._sig = vault.signature()
                self.settings = vault.load_settings() or self.settings
                self.refresh_clients()
                if self.client_id and any(c["id"] == self.client_id for c in self.all_clients):
                    self.open_client(self.client_id, select=self.view_select)
                elif not self.client_id:
                    self.show_home()
            r = data_root(cfg)

            def work(_progress=None):
                return sync_report(r)

            def done(rep_):
                self._marked_sig, self._marked_at = self._sig, time.time()
                self.last_sync = dict(rep_, at=datetime.now())
                self.set_status(("✓ " if rep_["ok"] else "⚠ ") + rep_["summary"] + ".")
                if on_result:
                    on_result(self.last_sync)

            def bg():
                try:
                    res = work()
                except Exception as e:
                    res = {"ok": False, "summary": "Sync check failed", "details": [str(e)], "others": 0}
                self.msgs.put(("call", lambda: done(res)))
            threading.Thread(target=bg, daemon=True).start()

        # ----------------------------------------------------------- shell
        def _build_shell(self):
            header = ttk.Frame(root, padding=(px(22), px(14), px(22), px(6)))
            header.pack(fill="x")
            ttk.Label(header, text=APP_NAME, style="H2.TLabel").pack(side="left")
            ttk.Label(header, text="🔒  Offline · stays on this computer", style="Caption.TLabel").pack(
                side="left", padx=px(14))
            hdr_btns = Flow(header, align="right", gap=px(8))
            hdr_btns.pack(side="right", fill="x", expand=True)
            self.queue_btn = hdr_btns.add(ttk.Button(hdr_btns, text="🎙  Queue recordings",
                                                     command=lambda: self.open_queue(self.client_id)))
            hdr_btns.add(ttk.Button(hdr_btns, text="⚙  Settings", command=self.open_setup))
            hdr_btns.add(ttk.Button(hdr_btns, text="🔒  Lock", command=self.lock))

            self.strip = ttk.Frame(root, padding=(px(22), px(2), px(22), px(10)))   # only shown for problems
            self.strip_sep = ttk.Separator(root)
            self.strip_sep.pack(fill="x")

            bottom = ttk.Frame(root, style="Bar.TFrame", padding=(px(22), px(7), px(22), px(7)))
            bottom.pack(side="bottom", fill="x")
            self.status = ttk.Label(bottom, text="Ready.", style="Bar.TLabel")
            self.status.pack(side="left")
            self.return_btn = ttk.Button(bottom, command=self.resume_parked)  # shown when a session is parked
            self.busy_bar = ttk.Progressbar(bottom, mode="indeterminate", length=px(220), maximum=100)
            self.pct_label = ttk.Label(bottom, text="", width=22, anchor="e", style="BarMuted.TLabel")
            self.task_chip = ttk.Label(bottom, text="", style="ChipAccent.TLabel")   # whose task is running
            self.wait_btn = ttk.Menubutton(bottom, text="", style="Toolbutton")       # tasks waiting in line
            self.wait_menu = tk.Menu(self.wait_btn, tearoff=False)
            self.wait_btn.config(menu=self.wait_menu)
            self.pct_label.pack(side="right", padx=(0, px(10)))
            self.page_bar = None
            self.page_pct = None
            self._task_start = None
            self._last_pct = None
            ttk.Separator(root).pack(side="bottom", fill="x")

            body = ttk.Frame(root)
            body.pack(fill="both", expand=True)
            side = ttk.Frame(body, style="Side.TFrame", padding=(px(12), px(16), px(12), px(14)), width=px(250))
            side.pack(side="left", fill="y")
            side.pack_propagate(False)
            body.bind("<Configure>", lambda e: side.config(width=px(250) if e.width >= px(1060) else px(200))
                      if e.widget is body else None, add="+")
            ttk.Separator(body, orient="vertical").pack(side="left", fill="y")
            ttk.Button(side, text="⌂  Home", command=lambda: self.leave(self.show_home)).pack(fill="x")
            ttk.Label(side, text="PATIENTS", style="SideCaption.TLabel").pack(anchor="w", pady=(px(18), px(6)),
                                                                          padx=px(4))
            # bottom controls are placed first, so a short window shrinks the list, not the buttons
            ttk.Button(side, text="＋  New patient", style="Accent.TButton",
                       command=self.new_client).pack(side="bottom", fill="x", pady=(px(8), 0), ipady=px(2))
            self.show_archived = tk.BooleanVar(value=False)
            self.archived_cb = ttk.Checkbutton(side, text="Show archived", variable=self.show_archived,
                                               command=lambda: self.refresh_clients(), style="Side.TCheckbutton")
            self.archived_cb.pack(side="bottom", anchor="w", pady=(px(8), 0), padx=px(2))
            cl_box, self.client_lb = auto_scroll(side, lambda b: NiceList(b, "Side.Treeview"))
            cl_box.pack(fill="both", expand=True)
            self.client_lb.bind("<<ListboxSelect>>", self.on_client_select)
            self.main = ttk.Frame(body)
            self.main.pack(side="left", fill="both", expand=True)
            self.refresh_strip()

        def refresh_strip(self):
            for w in self.strip.winfo_children():
                w.destroy()
            ms = models_status(cfg, self.settings.get("hf_token", ""))
            items = [("Speech engine", engine_installed()),
                     ("FFmpeg", ffmpeg_installed()),
                     ("Speech models", ms["whisper"] and ms["align"] is not False and ms["speaker"] is not False),
                     ("Speaker labels", bool(self.settings.get("hf_token")) or None),
                     ("LM Studio", isinstance(self.lm_status, list) and bool(self.lm_status)
                      if self.lm_status is not None else None)]
            # Only problems are shown. Working parts, optional extras (speaker labels) and checks still
            # running stay hidden, so the top of the window is quiet when everything is fine.
            problems = [name for name, ok in items if ok is False]
            for name in problems:
                ttk.Label(self.strip, text=f"✕  {name}", style="ChipBad.TLabel").pack(side="left", padx=(0, px(6)))
            if problems:
                ttk.Button(self.strip, text="Fix in Settings…", command=self.open_setup).pack(side="left", padx=px(8))
                if not self.strip.winfo_ismapped():
                    self.strip.pack(fill="x", before=self.strip_sep)
            else:
                self.strip.pack_forget()
            was = getattr(self, "setup_ok", None)
            self.setup_ok = not problems
            if was is not None and was != self.setup_ok and self.client_id is None and self.wizard is None \
                    and getattr(self, "page", None) is not None and not getattr(self, "locked", False):
                self.show_home()                       # keep the Home steps in step with setup

        def check_lmstudio(self):
            self.lm_status = None
            self.refresh_strip()
            cfg_copy = dict(cfg)

            def work():
                try:
                    res = list_models(cfg_copy, timeout=3)
                except Exception as e:
                    res = str(e)
                self.msgs.put(("lm", res))
            threading.Thread(target=work, daemon=True).start()

        # ---------------------------------------------------- task runner
        def _alive(self, w):
            try:
                return w is not None and w.winfo_exists()
            except tk.TclError:
                return False

        def page_owns_task(self):
            return self.busy and getattr(self, "_page_owner", None) is not None and \
                self._page_owner == getattr(self, "task_owner", None)

        def set_status(self, t, pct=None, from_task=False):
            code = getattr(self, "task_patient", "") if self.busy else ""
            if from_task and code and not t.startswith(code):
                self.status.config(text=f"{code}  ·  {t}")       # bottom bar: say whose task this is
            else:
                self.status.config(text=t)
            if self._alive(self.progress_label) and self.page_owns_task():
                self.progress_label.config(text=t)
            if pct is not None and self.busy:
                self.set_percent(pct)

        def set_percent(self, pct):
            """Switch every visible bar to a determinate percentage, with time left."""
            pct = max(0.0, min(float(pct), 100.0))
            if self._last_pct is not None and pct < self._last_pct:
                pct = self._last_pct                       # never move backwards
            self._last_pct = pct
            text = f"{pct:.0f}%"
            if self._task_start and 3 <= pct < 99:
                elapsed = time.time() - self._task_start
                left = elapsed * (100 - pct) / pct
                if elapsed > 8:
                    text += (f" · ~{left / 60:.0f} min left" if left >= 90 else f" · ~{max(5, round(left / 5) * 5):.0f} s left")
            for bar in (self.busy_bar, self.page_bar if self.page_owns_task() else None):
                if self._alive(bar):
                    if str(bar.cget("mode")) != "determinate":
                        bar.stop()
                        bar.config(mode="determinate")
                    bar["value"] = pct
            self.pct_label.config(text=text)
            if self._alive(self.page_pct) and self.page_owns_task():
                self.page_pct.config(text=text)

        def progress_panel(self, parent, text="", length=460, always=False, owner=None):
            """A label + percentage bar on the current page. It only tracks the running
            task if that task belongs to this page (owner) — otherwise the bottom bar does."""
            self._page_owner = owner
            box = ttk.Frame(parent)
            self.progress_label = ttk.Label(box, text=text, wraplength=px(760), justify="left")
            self.progress_label.pack(anchor="w")
            row = ttk.Frame(box)
            row.pack(anchor="w", pady=(4, 0))
            self.page_bar = ttk.Progressbar(row, mode="indeterminate", length=px(length), maximum=100)
            self.page_bar.pack(side="left")
            self.page_pct = ttk.Label(row, text="", style="H2.TLabel")
            self.page_pct.pack(side="left", padx=10)
            self._page_row = row
            wt = None if self.page_owns_task() else self.waiting_text(owner)
            if wt:
                self.progress_label.config(text=wt)
            if self.page_owns_task():
                if self._last_pct is None:
                    self.page_bar.start(12)
                else:
                    self.set_percent(self._last_pct)
            elif always and not wt:
                self.page_bar.start(12)
            else:
                row.pack_forget()
            return box

        def set_busy(self, on):
            self.busy = on
            if not on:
                self.task_label = ""
                self.task_patient = ""
            if on:
                self._task_start, self._last_pct = time.time(), None
                row = getattr(self, "_page_row", None)
                if self._alive(row) and not row.winfo_ismapped() and self.page_owns_task():
                    row.pack(anchor="w", pady=(4, 0))
                    self.page_bar.start(12)
                self.busy_bar.config(mode="indeterminate")
                self.busy_bar.pack(side="right", before=self.pct_label)
                code = getattr(self, "task_patient", "")
                if code:
                    self.task_chip.config(text=f"{code}  ·  {self.task_label or 'working'}")
                    self.task_chip.pack(side="right", padx=(px(10), px(4)), after=self.pct_label)
                self.busy_bar.start(12)
                self.pct_label.config(text="")
            else:
                self.busy_bar.stop()
                self.busy_bar.pack_forget()
                self.task_chip.pack_forget()
                self.busy_bar.config(mode="determinate")
                self.busy_bar["value"] = 0
                self.pct_label.config(text="")
                self._task_start = None

        def run_bg(self, fn, on_done, on_error=None, label="", quiet=False, owner=None, patient="", on_start=None):
            """Run fn in the background. If something is already running, a task you started waits in
            line and starts by itself when the one before it finishes (returns "queued"). Automatic
            work (quiet=True: the recording queue, Settings steps) never waits in line — it returns
            False and tries again shortly, letting tasks you asked for go first."""
            if self.busy or (quiet and self.waitlist):
                if quiet:
                    return False
                if owner is not None and any(j["owner"] == owner for j in self.waitlist):
                    self.set_status(f"That's already waiting in line ({label}).")
                    return "queued"
                self.waitlist.append({"fn": fn, "on_done": on_done, "on_error": on_error, "label": label,
                                      "owner": owner, "patient": patient, "on_start": on_start})
                n = len(self.waitlist)
                self.set_status(f"Waiting in line: {label} — starts by itself after "
                                f"{self.task_label or 'the current task'}" + (f" (and {n - 1} before it)" if n > 1 else "")
                                + ".")
                self.refresh_waiting()
                if owner is not None and getattr(self, "_page_owner", None) == owner:
                    if self._alive(self.progress_label):         # the page already showing this task: say it waits
                        self.progress_label.config(text=self.waiting_text(owner))
                    if self._alive(self.page_bar):
                        self.page_bar.stop()
                        _quiet(self._page_row.pack_forget)
                return "queued"
            return self._start_bg(fn, on_done, on_error, label, owner, patient, on_start)

        def _start_bg(self, fn, on_done, on_error, label, owner, patient, on_start=None):
            self.task_owner = owner
            self.task_patient = patient
            # the chip says "DS-01 · transcribing"; task_label keeps the full wording for messages
            self.task_label = re.sub(r"\s+((for|about)\s+)?" + re.escape(patient) + r"$", "", label) if patient else label
            self.set_busy(True)
            self.task_label = label
            token = self.task_token = CancelToken()

            def progress(t, pct=None):
                token.check()                      # a cancelled task stops at its next step
                self.msgs.put(("status", t, pct, token))

            def target():
                _TASK.token = token
                try:
                    res = fn(progress)
                except Exception as e:
                    if not token.is_set():
                        self.msgs.put(("error", friendly_error(e), on_error, token))
                else:
                    if not token.is_set():
                        self.msgs.put(("done", on_done, res, token))
                finally:
                    _TASK.token = None
            threading.Thread(target=target, daemon=True).start()
            if self._alive(self.progress_label) and self.page_owns_task():
                self.progress_label.config(text=label[:1].upper() + label[1:] + "…")
            if on_start is not None:
                _quiet(on_start)
            self.refresh_waiting()
            return True

        # ---------------------------------------------- tasks waiting in line
        def waiting_text(self, owner):
            """'Waiting in line…' for a page whose task hasn't started yet (None if it isn't waiting)."""
            for i, j in enumerate(self.waitlist):
                if owner is not None and j["owner"] == owner:
                    ahead = f"{self.task_label or 'the current task'}" + (f" and {i} more" if i else "")
                    return f"Waiting in line — this starts by itself after {ahead} finishes."
            return None

        def pump_waitlist(self):
            """Start the next task in line once nothing is running."""
            if self.busy or not self.waitlist:
                self.refresh_waiting()
                return
            j = self.waitlist.pop(0)
            self._start_bg(j["fn"], j["on_done"], j["on_error"], j["label"], j["owner"], j["patient"], j["on_start"])

        def drop_waiting(self, owner=None, index=None, tell=False):
            """Take tasks out of the line (they never started, so nothing needs stopping)."""
            keep, gone = [], []
            for i, j in enumerate(self.waitlist):
                (gone if (i == index or (owner is not None and j["owner"] == owner)) else keep).append(j)
            self.waitlist = keep
            for j in gone:
                if tell and j["on_error"]:
                    _quiet(j["on_error"], "Cancelled before it started.")
            self.refresh_waiting()
            return len(gone)

        def refresh_waiting(self):
            """The '2 waiting' button in the bottom bar, with a menu to see or cancel each one."""
            btn = getattr(self, "wait_btn", None)
            if not self._alive(btn):
                return
            n = len(self.waitlist)
            if not n:
                btn.pack_forget()
                return
            btn.config(text=f"⏳  {n} waiting")
            m = self.wait_menu
            m.delete(0, "end")
            for i, j in enumerate(self.waitlist):
                who = f"{j['patient']}  ·  " if j["patient"] else ""
                verb = re.sub(r"\s+(for|about)\s+" + re.escape(j["patient"]) + r"$", "", j["label"]) if j["patient"] \
                    else j["label"]
                m.add_command(label=f"{i + 1}.  {who}{verb}   —   cancel", command=lambda i=i: self.drop_waiting(
                    index=i, tell=True) and self.set_status("Removed from the line."))
            if not btn.winfo_ismapped():
                btn.pack(side="right", padx=(px(6), 0), after=self.pct_label)

        def cancel_task(self, owner=None, why="Stopped."):
            """Stop the background task for real (if it belongs to `owner`): the transcription
            process is killed / LM Studio is disconnected, nothing it was doing gets saved, and
            the app is free for the next task straight away."""
            dropped = self.drop_waiting(owner) if owner is not None else 0
            token = getattr(self, "task_token", None)
            if not self.busy or token is None or (owner is not None and self.task_owner != owner):
                if dropped:
                    self.set_status(why)
                return bool(dropped)
            token.cancel()
            self.task_token = None
            self.set_busy(False)
            self.set_status(why)
            root.after(300, self.pump_waitlist)
            return True

        def poll(self):
            try:
                for _ in range(200):                      # bounded, so a flood can't freeze the window
                    try:
                        m = self.msgs.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        self._handle_msg(m)
                    except Exception as e:
                        # one failing callback must never stop the loop that runs all the others
                        import traceback
                        traceback.print_exc()
                        self._recover(m, e)
            finally:
                root.after(150, self.poll)

        def _recover(self, m, e):
            """A finishing step crashed: say so, and keep the recording queue moving."""
            if m[0] not in ("done", "error"):
                return
            self.set_status("Something went wrong finishing that task: " + friendly_error(e)[:160])
            if getattr(self, "task_owner", None) == ("queue",):
                Q = self._q()
                for it in Q["items"]:
                    if it["status"] in ("transcribing", "writing"):
                        it["status"], it["error"] = "failed", "Couldn't finish: " + friendly_error(e)
                        self.remote_mark(it, "failed", error=it["error"])
                self.queue_refresh()
                if Q["running"]:
                    root.after(500, self.queue_next)

        def _handle_msg(self, m):
            kind = m[0]
            if kind in ("status", "done", "error") and len(m) > 3 and m[-1] is not getattr(self, "task_token", None):
                return                             # from a task that was cancelled: ignore
            if kind == "status":
                self.set_status(m[1], m[2] if len(m) > 2 else None, from_task=True)
            elif kind == "done":
                self.set_busy(False)
                try:
                    m[1](m[2])
                finally:
                    root.after(150, self.pump_waitlist)
            elif kind == "error":
                code = getattr(self, "task_patient", "")
                self.set_busy(False)
                self.set_status((f"{code}: " if code else "") + "Something went wrong — nothing was saved.")
                root.after(150, self.pump_waitlist)        # the next task in line doesn't wait for this message
                if m[2]:
                    m[2](m[1])
                else:
                    messagebox.showerror(APP_NAME, m[1])
            elif kind == "lm":
                self.lm_status = m[1]
                self.refresh_strip()
                if self.setup_win is not None:
                    self.setup_win["refresh"]()
            elif kind == "call":                  # result from a helper thread
                m[1]()
            elif kind == "setuplog":
                if self.setup_win is not None:
                    self.setup_win["log"](m[1])

        # ------------------------------------------------------ clipboard
        def copy(self, text, what):
            if not (text or "").strip():
                return
            if not set_private_clipboard(text):
                root.clipboard_clear()
                root.clipboard_append(text)
            self.clip = text
            self.set_status(f"{what} copied — paste it into TheraNest. The clipboard clears in 2 minutes.")
            self._clip_timer()

        def _text_copy(self, w, cut=False):
            try:
                txt = w.get("sel.first", "sel.last")
            except tk.TclError:
                return "break"                          # nothing selected
            if not txt:
                return "break"
            if set_private_clipboard(txt):
                self.clip = txt
                self._clip_timer()
            else:
                root.clipboard_clear()
                root.clipboard_append(txt)
                self.clip = txt
                self._clip_timer()
            if cut and str(w.cget("state")) == "normal":
                w.delete("sel.first", "sel.last")
                w.edit_separator()
            return "break"

        def _clip_timer(self):
            """Clear the clipboard 2 minutes after the LAST copy (an earlier timer never cuts a newer copy short)."""
            if getattr(self, "_clip_after", None):
                _quiet(root.after_cancel, self._clip_after)
            self._clip_after = root.after(120_000, self.clear_clipboard)

        def note_copy_menu(self, parent, get):
            """'Copy part ▾': the paragraph, or either interventions list, for TheraNest's separate boxes."""
            mb = ttk.Menubutton(parent, text="Copy part  ▾")
            m = tk.Menu(mb, tearoff=False)
            mb.config(menu=m)

            def pick(i, label):
                part = note_sections(get())[i]
                if not part.strip():
                    return messagebox.showinfo(APP_NAME, f"This note has no “{label}” section. (Notes written "
                                                         "before this was added, and imported notes, don't.)")
                self.copy(part, label)
            m.add_command(label="Note paragraph only", command=lambda: pick(0, "Note paragraph"))
            m.add_command(label="Therapeutic Interventions Used", command=lambda: pick(1, "Therapeutic Interventions Used"))
            m.add_command(label="Planned Interventions (For future sessions)",
                          command=lambda: pick(2, "Planned Interventions"))
            return mb

        def clear_clipboard(self):
            try:
                if self.clip is not None and root.clipboard_get() == self.clip:
                    root.clipboard_clear()
                    root.clipboard_append("")
            except tk.TclError:
                pass
            self.clip = None

        # ----------------------------------------------------- navigation
        def show(self, builder):
            if self.page is not None:
                self.page.destroy()
            self.progress_label = self.page_bar = self.page_pct = None
            self._page_owner = None
            self._page_editing = False             # set while text is being edited in place
            self.page = ttk.Frame(self.main, padding=(px(28), px(22), px(28), px(18)))
            self.page.pack(fill="both", expand=True)
            self._page_builder = builder
            builder(self.page)

        def leave(self, then):
            """Navigate away. An unfinished session (still transcribing/writing, or
            unsaved) is parked — a button at the bottom brings you back to it."""
            self.stash_wizard()
            then()
            return True

        def stash_wizard(self):
            w = self.wizard
            if w is not None:
                if w.get("collect"):
                    w["collect"]()
                    w["collect"] = None
                if not w.get("discarded") and (w.get("task") or w.get("dirty") or w.get("ready")):
                    self.parked = w
            self.wizard = None
            self.update_return_button()

        def update_return_button(self):
            w = self.parked
            if w is None or w is self.wizard or w.get("discarded"):
                self.return_btn.pack_forget()
                return
            code = w["client"].get("code", "")
            if w.get("error"):
                text, style = f"⚠ {code} session needs attention — open ▶", "Accent.TButton"
            elif w.get("ready") == "transcript":
                text, style = f"✓ {code} transcript ready — Check transcript ▶", "Accent.TButton"
            elif w.get("ready") == "drafts":
                text, style = f"✓ {code} drafts ready — Review notes ▶", "Accent.TButton"
            elif w.get("task") == "transcribe":
                text, style = f"↩ Back to {code} session (transcribing)", "TButton"
            elif w.get("task") == "generate":
                text, style = f"↩ Back to {code} session (writing notes)", "TButton"
            else:
                text, style = f"↩ Resume unsaved {code} session", "TButton"
            self.return_btn.config(text=text, style=style)
            self.return_btn.pack(side="left", padx=(0, px(14)), before=self.status)

        def resume_parked(self):
            w = self.parked
            if w is None:
                return
            self.stash_wizard() if self.wizard is not w else None
            self.parked = None
            self.wizard = w
            self.client_id = w["client"]["id"]
            self.highlight_client(self.client_id)
            w["ready"] = None
            self.update_return_button()
            self.goto(w["step"])
            if w.get("error") and w["step"] != 1:          # transcription errors show on their own page
                msg, w["error"] = w["error"], None
                messagebox.showerror(APP_NAME, msg)

        def discard_wizard(self):
            w = self.wizard
            if w is None:
                return
            if w.get("task"):
                step = "transcription" if w["task"] == "transcribe" else "note writing"
                if not messagebox.askyesno(APP_NAME, f"Discard this session? The {step} that's running will be "
                                                     "stopped right away and nothing from it is kept."):
                    return
            elif w.get("dirty") and not messagebox.askyesno(APP_NAME, "Discard this unsaved session?"):
                return
            self.abandon_wizard(w)
            self.wizard = None
            if self.parked is w:
                self.parked = None
            self.update_return_button()
            self.open_client(w["client"]["id"])

        def abandon_wizard(self, w, why=None):
            """Throw a session away — and stop whatever it has running in the background."""
            w["discarded"] = True
            if w.get("task"):
                code = w["client"].get("code", "")
                what = "transcription" if w["task"] == "transcribe" else "note writing"
                if self.waiting_text(("wizard", id(w))):
                    why = why or f"Discarded {code}'s session — its {what} was taken out of the line."
                self.cancel_task(("wizard", id(w)), why or f"Discarded {code}'s session — the {what} was stopped.")
                w["task"] = None

        def restart_app(self, script=None):
            """Reopen the app (same Python) — used after switching data folders or moving the program."""
            self.clear_clipboard()
            env = dict(ONLINE_ENV, PATH=os.environ["PATH"], **{RELAUNCH_FLAG: "1"})
            try:
                subprocess.Popen([_gui_python(sys.executable), script or os.path.abspath(__file__)], env=env,
                                 cwd=os.path.dirname(script) if script else None)
            except Exception as e:
                messagebox.showinfo(APP_NAME, f"Please reopen Session Scribe yourself ({e}).")
            root.destroy()

        def global_find(self, _=None):
            """Ctrl+F outside a text box: patient page → search the whole patient;
            elsewhere → find in the document that's showing."""
            if self.locked:
                return "break"
            focus = root.focus_get()
            if isinstance(focus, tk.Text) and getattr(focus, "finder", None):
                focus.finder.show()
                return "break"
            if self._alive(self._search_entry) and self._search_entry.winfo_viewable():
                self._search_entry.focus_set()
                self._search_entry.select_range(0, "end")
                return "break"

            def visible_texts(w, acc):
                for c in w.winfo_children():
                    if isinstance(c, tk.Text) and getattr(c, "finder", None) and c.winfo_viewable():
                        acc.append(c)
                    visible_texts(c, acc)
                return acc
            texts = visible_texts(self.page, []) if self._alive(self.page) else []
            if texts:
                texts[0].finder.show()
            return "break"

        def redraw(self):
            """Re-render the current screen (after switching light/dark)."""
            self.refresh_strip()
            if self.wizard is not None:
                self.goto(self.wizard["step"])
            elif self.client_id:
                self.open_client(self.client_id)
            else:
                self.show_home()

        def wizard_on_screen(self, w):
            return self.wizard is w and self._alive(self.page)

        def refresh_clients(self, select_id=None):
            self.all_clients = vault.list_clients()
            n_arch = sum(1 for c in self.all_clients if c.get("archived"))
            show = self.show_archived.get()
            # active patients first, archived (when shown) after them
            self.clients = [c for c in self.all_clients if not c.get("archived")] + \
                           ([c for c in self.all_clients if c.get("archived")] if show else [])
            self._sig = vault.signature()
            # patients with drafts waiting for review (e.g. from the recording queue)
            self.review_clients = {}
            for m in vault.session_index().values():
                if m.get("needs_review") and m.get("client_id"):
                    self.review_clients[m["client_id"]] = self.review_clients.get(m["client_id"], 0) + 1
            self.client_lb.delete(0, "end")
            for c in self.clients:
                n = self.review_clients.get(c["id"], 0)
                self.client_lb.insert("end", ("🗄  " if c.get("archived") else "  ") + c.get("code", "?")
                                      + (f"   ⚠ {n} to review" if n else ""))
            self.archived_cb.config(text=f"Show archived ({n_arch})" if n_arch else "Show archived")
            self.highlight_client(select_id or self.client_id)

        def highlight_client(self, cid):
            self.client_lb.selection_clear(0, "end")
            for i, c in enumerate(self.clients):
                if c["id"] == cid:
                    self.client_lb.selection_set(i)
                    self.client_lb.see(i)

        def on_client_select(self, _=None):
            sel = self.client_lb.curselection()
            if not sel:
                return
            cid = self.clients[sel[0]]["id"]
            if cid == self.client_id and self.wizard is None:
                return
            self.leave(lambda: self.open_client(cid))

        # ============================================================ HOME
        def show_home(self):
            self.client_id = None
            self.client_lb.selection_clear(0, "end")

            def build(f):
                hour = datetime.now().hour
                greet = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
                ttk.Label(f, text=greet, style="Hero.TLabel").pack(anchor="w")
                n_pat = sum(1 for c in self.clients if not c.get("archived"))
                steps = [] if self.setup_ok else [
                    ("Finish setup", "Some pieces are missing. The Settings window checks everything and installs "
                                     "what's needed with one click each.", ("Open Settings", self.open_setup))]
                steps += [
                    ("Add a patient",
                     "Create a profile with a patient code (not a name), names to hide from the AI, "
                     "and treatment goals.", ("＋ New patient", self.new_client)),
                    ("Add a session",
                     "Pick the patient on the left, then click ＋ New session. You'll be walked "
                     "through it one step at a time, and each note builds on the previous ones. Have several "
                     "recordings? Queue them and let them run while you're away.",
                     ("🎙  Queue recordings…", lambda: self.open_queue())),
                ]
                words = {2: "Two", 3: "Three"}
                ttk.Label(f, text=(f"{n_pat} patient{'s' if n_pat != 1 else ''} in your vault. " if n_pat else "")
                          + f"{words[len(steps)]} steps to a note:", style="Muted.TLabel").pack(
                    anchor="w", pady=(px(2), px(18)))
                for i, (title, desc, action) in enumerate(steps, 1):
                    num, done = str(i), False
                    card = ttk.Frame(f, style="Card.TFrame", padding=(px(20), px(16)))
                    card.pack(fill="x", pady=px(5))
                    ttk.Label(card, text="✓" if done else num, style="Badge.TLabel", width=2).pack(
                        side="left", anchor="n")
                    col = ttk.Frame(card)
                    col.pack(side="left", fill="x", expand=True, padx=(px(8), 0))
                    ttk.Label(col, text=title, style="H2.TLabel").pack(anchor="w")
                    ttk.Label(col, text=desc, wraplength=px(620), justify="left", style="Muted.TLabel").pack(
                        anchor="w", pady=(px(2), 0))
                    if action:
                        ttk.Button(col, text=action[0], command=action[1], style="Accent.TButton").pack(
                            anchor="w", pady=(px(10), 0))
                ttk.Label(f, text="ⓘ  Before using this with real patients: confirm your site allows recording and "
                                  "AI-drafted notes, get written patient consent, and turn on "
                                  + ("FileVault" if IS_MAC else "BitLocker") + " (disk encryption). Always "
                                  "review drafts before they go in the EHR.",
                          style="Caption.TLabel", wraplength=px(700), justify="left").pack(anchor="w", pady=(px(18), 0))
            self.show(build)

        # ===================================================== client dialog
        def client_dialog(self, client=None):
            win = new_window(root, "Edit patient" if client else "New patient", "680x700")
            win.grab_set()
            f = ttk.Frame(win, padding=(px(24), px(20)))
            f.pack(fill="both", expand=True)
            f.columnconfigure(0, weight=1)
            c = dict(client or {})
            code_var = tk.StringVar(value=c.get("code", ""))
            existing = "\n".join(f"{n}, {p.strip('[]')}" for n, p in client_name_map(c))
            ttk.Label(f, text="Patient code", style="H2.TLabel").grid(row=0, column=0, sticky="w")
            ttk.Label(f, text="Something like initials + number (e.g. JD-07). Avoid full names.",
                      style="Muted.TLabel").grid(row=1, column=0, sticky="w")
            ttk.Entry(f, textvariable=code_var).grid(row=2, column=0, sticky="ew", pady=(2, 10))
            ttk.Label(f, text="Names to hide from the AI", style="H2.TLabel").grid(row=3, column=0, sticky="w")
            ttk.Label(f, text="One per line (or separated by ;) as  Name, Placeholder  — e.g.  Danielle, Patient  "
                              "and  John, Husband.  Danielle becomes [Patient] and John becomes [Husband] before "
                              "the AI sees anything. Include schools, workplaces and towns too.",
                      style="Muted.TLabel", wraplength=px(580), justify="left").grid(row=4, column=0, sticky="w")
            nfr, names_txt = scrolled_text(f, height=4)
            nfr.grid(row=5, column=0, sticky="ew", pady=(2, 0))
            set_text(names_txt, existing)
            preview = ttk.Label(f, text="", style="Muted.TLabel", wraplength=px(580), justify="left")
            preview.grid(row=6, column=0, sticky="w", pady=(2, 10))

            def update_preview(_=None):
                pairs = parse_name_map(get_text(names_txt))
                preview.config(text=("Will replace:  " + "   ·   ".join(f"{n} → {p}" for n, p in pairs))
                               if pairs else "Nothing to replace yet.")
                names_txt.edit_modified(False)
            names_txt.bind("<<Modified>>", update_preview)
            update_preview()
            ttk.Label(f, text="Treatment goals", style="H2.TLabel").grid(row=7, column=0, sticky="w")
            gfr, goals = scrolled_text(f, height=3)
            gfr.grid(row=8, column=0, sticky="ew", pady=(2, 10))
            set_text(goals, c.get("goals", ""))
            ttk.Label(f, text="Background (optional)", style="H2.TLabel").grid(row=9, column=0, sticky="w")
            ttk.Label(f, text="Presenting concerns, relevant history, diagnosis — whatever helps the "
                              "notes stay accurate.", style="Muted.TLabel").grid(row=10, column=0, sticky="w")
            bfr, bg = scrolled_text(f, height=3)
            bfr.grid(row=11, column=0, sticky="nsew", pady=(2, 10))
            f.rowconfigure(11, weight=1)
            for r_ in (5, 8):                          # names/goals boxes give way before the buttons
                f.rowconfigure(r_, weight=1)
            set_text(bg, c.get("background", ""))
            result = {}

            def save():
                code = code_var.get().strip()
                if not code:
                    return messagebox.showinfo(APP_NAME, "Enter a patient code.", parent=win)
                if any(x.get("code", "").lower() == code.lower() and x["id"] != c.get("id")
                       for x in vault.list_clients()):
                    return messagebox.showinfo(APP_NAME, "Another patient already uses that code.", parent=win)
                pairs = parse_name_map(get_text(names_txt))
                c.update(code=code, names_format=2,
                         names_to_hide="\n".join(f"{n}, {p.strip('[]')}" for n, p in pairs),
                         goals=get_text(goals).strip(), background=get_text(bg).strip())
                c.setdefault("created", datetime.now().isoformat(timespec="seconds"))
                vault.save_client(c)
                result["client"] = c
                win.destroy()

            btns = ttk.Frame(f)
            btns.grid(row=12, column=0, sticky="e")
            ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
            ttk.Button(btns, text="Save patient", style="Accent.TButton", command=save).pack(side="right", padx=6)
            win.wait_window()
            return result.get("client")

        def new_client(self):
            c = self.client_dialog()
            if c:
                self.stash_wizard()
                self.refresh_clients(c["id"])
                self.open_client(c["id"])

        # ===================================================== client page
        def open_client(self, cid, select=None):
            self.stash_wizard()
            self.client_id = cid
            self.highlight_client(cid)
            try:
                client = vault.load_client(cid)
            except Exception:
                self.refresh_clients()
                return self.show_home()
            sessions = vault.sessions_for(cid)          # oldest first
            self._sig = vault.signature()              # what's on screen is current
            self.show(lambda f: self._client_page(f, client, sessions, select))

        def _client_page(self, f, client, sessions, select):
            top = ttk.Frame(f)
            top.pack(fill="x")
            titles = ttk.Frame(top)
            titles.pack(side="left")
            name_row = ttk.Frame(titles)
            name_row.pack(anchor="w")
            ttk.Label(name_row, text=client.get("code", ""), style="Title.TLabel").pack(side="left")
            if client.get("archived"):
                ttk.Label(name_row, text="🗄  Archived", style="ChipWarn.TLabel").pack(side="left", padx=px(10))
            days = group_by_date(sessions)
            last = days[-1][0] if days else ""
            ttk.Label(titles, text=f"{len(days)} session{'s' if len(days) != 1 else ''}"
                                   + (f"  ·  last seen {fmt_date(last)}" if last else ""),
                      style="Muted.TLabel").pack(anchor="w")
            pbtns = Flow(top, align="right")
            pbtns.pack(side="right", fill="x", expand=True, anchor="n", padx=(px(16), 0))
            pbtns.add(ttk.Button(pbtns, text="＋  New session", style="Accent.TButton",
                                 command=lambda: self.start_wizard(client)))
            pbtns.add(ttk.Button(pbtns, text="Import past notes…", command=lambda: self.import_dialog(client)))
            pbtns.add(ttk.Button(pbtns, text="⤓  Export…", command=lambda: self.export_all(client)))
            pbtns.add(ttk.Button(pbtns, text="Edit patient", command=lambda: self.edit_client(client)))
            more = ttk.Menubutton(pbtns, text="More")
            menu = register(tk.Menu(more, tearoff=False), "menu")
            menu.add_command(label="Tidy imported notes…", command=lambda: self.tidy_imported(client))
            menu.add_command(label="Queue recordings for this patient…", command=lambda: self.open_queue(client["id"]))
            menu.add_command(label="Check dates & session #s against TheraNest…",
                             command=lambda: self.appointment_check(client))
            menu.add_separator()
            if client.get("archived"):
                menu.add_command(label="↩  Restore patient", command=lambda: self.set_archived(client, False))
            else:
                menu.add_command(label="🗄  Archive patient", command=lambda: self.set_archived(client, True))
            menu.add_command(label="🗑  Delete patient…", command=lambda: self.delete_client(client))
            more["menu"] = menu
            pbtns.add(more)

            if client.get("goals"):
                ttk.Label(f, text="Goals  ·  " + "  ·  ".join(l.strip() for l in client["goals"].splitlines() if l.strip()),
                          style="Caption.TLabel", wraplength=px(860), justify="left").pack(anchor="w", pady=(px(8), 0))

            dups = duplicate_numbers(sessions)
            if dups:
                ban = ttk.Frame(f, style="Card.TFrame", padding=(px(12), px(8)))
                ban.pack(fill="x", pady=(px(10), 0))
                txt = "; ".join(f"#{n} is used on {' and '.join(fmt_date(x) for x in days)}" for n, days in dups.items())
                ttk.Label(ban, text=f"✕  Duplicate session numbers: {txt}. Open one of those dates and use "
                                    "“Change date / session #…” to fix it.", style="Bad.TLabel",
                          wraplength=px(860), justify="left").pack(anchor="w")
            dup_days = {d_ for days in dups.values() for d_ in days}

            ttk.Separator(f).pack(fill="x", pady=(px(12), 0))
            body = ttk.Panedwindow(f, orient="horizontal")
            body.pack(fill="both", expand=True, pady=(px(12), 0))
            left_wrap = ttk.Frame(body, width=px(300))
            ttk.Separator(left_wrap, orient="vertical").pack(side="right", fill="y", padx=(px(10), 0))
            left = ttk.Frame(left_wrap)
            left.pack(side="left", fill="both", expand=True)
            right = ttk.Frame(body, padding=(px(6), 0, 0, 0))
            body.add(left_wrap, weight=0)
            body.add(right, weight=1)
            split = {"auto": True, "w": None}

            def fit_split(e):
                # narrow window: give the open document more room; a sash the user dragged stays put
                if not split["auto"] or e.width == split["w"] or e.width < px(200):
                    return
                split["w"] = e.width
                try:
                    body.sashpos(0, px(300) if e.width >= px(820) else max(px(190), int(e.width * 0.36)))
                except tk.TclError:
                    pass
            body.bind("<Configure>", fit_split, add="+")
            body.bind("<ButtonRelease-1>", lambda e: split.update(auto=False), add="+")

            # ---- search this patient's whole folder
            sbox = ttk.Frame(left)
            sbox.pack(fill="x", pady=(0, px(8)))
            q_var = tk.StringVar()
            q_entry = ttk.Entry(sbox, textvariable=q_var)
            q_entry.pack(side="left", fill="x", expand=True)
            clear_btn = ttk.Button(sbox, text="✕", width=3, command=lambda: (q_var.set(""), q_entry.focus_set()))
            hint = ttk.Label(sbox, text="🔍  Search this patient…", style="Muted.TLabel", cursor="xterm")
            hint.place(in_=q_entry, x=px(8), rely=0.5, anchor="w")
            hint.bind("<1>", lambda e: q_entry.focus_set())
            result_info = ttk.Label(left, text="", style="Caption.TLabel", wraplength=px(250), justify="left")
            self._search_entry = q_entry

            sort_row = ttk.Frame(left)
            sort_row.pack(fill="x", pady=(0, px(4)))
            sort_btn = ttk.Button(sort_row)
            sort_btn.pack(side="right")
            ttk.Label(sort_row, text="SESSIONS", style="Caption.TLabel").pack(side="left", padx=(px(2), 0))

            lb_box, lb = auto_scroll(left, lambda b: NiceList(b, "List.Treeview"))
            lb_box.pack(fill="both", expand=True)
            items = []
            state = {"query": "", "results": []}

            def newest_first():
                return cfg.get("session_sort", "desc") != "asc"

            def label_sort():
                sort_btn.config(text="↓  Newest first" if newest_first() else "↑  Oldest first")

            def toggle_sort():
                cfg["session_sort"] = "asc" if newest_first() else "desc"
                save_config(cfg)
                label_sort()
                keep = items[lb.curselection()[0]] if lb.curselection() else None
                fill_list()
                idx = next((i for i, it in enumerate(items)
                            if keep and it[0] == keep[0] and (it[1] == keep[1] or
                                                               (it[0] == "result" and it[1]["sid"] == keep[1]["sid"]
                                                                and it[1]["key"] == keep[1]["key"]))), None)
                if idx is not None:
                    lb.selection_set(idx)
                    lb.see(idx)
                self.set_status("Showing " + ("newest" if newest_first() else "oldest") + " sessions first.")
            sort_btn.config(command=toggle_sort)
            label_sort()

            def fill_list():
                lb.delete(0, "end")
                items.clear()
                if state["query"]:
                    res = state["results"]
                    if not newest_first():           # keep the case overview on top, flip the dates
                        res = [r for r in res if r["key"] in ("case", "plan")] + \
                              list(reversed([r for r in res if r["key"] not in ("case", "plan")]))
                    for r in res:
                        items.append(("result", r))
                        where = r["label"] if r["key"] in ("case", "plan") else f"{r['day']}   {r['label']}"
                        lb.insert("end", f"{where}   ×{r['count']}")
                    return
                items.append(("case", None))
                lb.insert("end", "★  Case overview")
                plan = current_plan(client)
                due = plan_review_due(plan) if plan else None
                items.append(("plan", None))
                lb.insert("end", "📋  Treatment plan" + (f"  · v{plan['version']}" if plan else "  · none yet")
                                 + ("  · review due" if due and due <= date.today() else ""))
                items.append(("ask", None))
                n_chats = len(client.get("chats") or [])
                lb.insert("end", "💬  Ask AI about this patient" + (f"  · {n_chats}" if n_chats else ""))
                for day, records in (reversed(days) if newest_first() else days):
                    items.append(("day", day))
                    n_docs = len(day_docs(records))
                    imported_only = all(d["meta"].get("source") == "imported" for _, d in records)
                    review = any(d["meta"].get("needs_review") for _, d in records)
                    lb.insert("end", f"{fmt_date(day)}   {day_title(records)}"
                                     + ("  · ✕ dup #" if day in dup_days else "")
                                     + ("  · ⚠ review" if review else "")
                                     + ("  · imported" if imported_only else "")
                                     + (f"  · {n_docs} docs" if len(records) > 1 else ""))

            viewer = ttk.Frame(right, padding=(px(18), 0, 0, 0))
            viewer.pack(fill="both", expand=True)

            def render(i):
                for w in viewer.winfo_children():
                    w.destroy()
                kind, val = items[i]
                if kind == "result":
                    q = state["query"]
                    if val["key"] == "plan":
                        self._render_plan(viewer, client, sessions, query=q)
                    elif val["key"] == "case":
                        self._render_case(viewer, client, sessions, query=q)
                    else:
                        self._render_day(viewer, client, val["day"], dict(days)[val["day"]],
                                         focus=(val["sid"], val["key"]), query=q)
                    return
                self.view_select = kind if kind in ("case", "plan", "ask") else f"day:{val}"
                if kind == "ask":
                    self._render_ask(viewer, client, sessions)
                elif kind == "plan":
                    self._render_plan(viewer, client, sessions)
                elif kind == "case":
                    self._render_case(viewer, client, sessions)
                else:
                    self._render_day(viewer, client, val, dict(days)[val])

            lb.bind("<<ListboxSelect>>", lambda e: lb.curselection() and render(lb.curselection()[0]))

            pending = {"job": None}

            def do_search():
                pending["job"] = None
                q = q_var.get().strip()
                hint.place_forget() if q_var.get() else hint.place(in_=q_entry, x=px(8), rely=0.5, anchor="w")
                if q:
                    clear_btn.pack(side="left", padx=(px(6), 0))
                else:
                    clear_btn.pack_forget()
                if q == state["query"]:
                    return
                state["query"] = q
                state["results"] = search_patient(client, sessions, q) if q else []
                fill_list()
                if q:
                    total = sum(r["count"] for r in state["results"])
                    docs = len(state["results"])
                    result_info.config(text=(f"{total} match{'es' if total != 1 else ''} in {docs} "
                                             f"document{'s' if docs != 1 else ''} — click one to see them highlighted."
                                             if docs else f"No matches for “{q}”."),
                                       style="Caption.TLabel" if docs else "Bad.TLabel")
                    result_info.pack(anchor="w", pady=(0, px(6)), before=lb_box)
                    if items:
                        lb.selection_set(0)
                        render(0)
                    else:
                        for w in viewer.winfo_children():
                            w.destroy()
                else:
                    result_info.pack_forget()
                    if items:
                        latest_day = days[-1][0] if days else None
                        back = next((i for i, it in enumerate(items) if it == ("day", latest_day)), 0)
                        lb.selection_set(back)
                        lb.see(back)
                        render(back)

            def on_type(*_):
                hint.place_forget() if q_var.get() else hint.place(in_=q_entry, x=px(8), rely=0.5, anchor="w")
                if pending["job"]:
                    root.after_cancel(pending["job"])
                pending["job"] = root.after(250, do_search)
            q_var.trace_add("write", on_type)
            q_entry.bind("<Escape>", lambda e: q_var.set(""))
            q_entry.bind("<Return>", lambda e: (do_search(), lb.focus_set()))
            f._search = {"var": q_var, "run": do_search, "list": lb, "items": items, "state": state}
            fill_list()

            if not sessions:
                for w in viewer.winfo_children():
                    w.destroy()
                ttk.Label(viewer, text="No sessions yet", style="H2.TLabel").pack(anchor="w", pady=(20, 4))
                ttk.Label(viewer, text="Click ＋ New session to add the first recording, or Import past notes… "
                                       "to bring in notes you've already written.", wraplength=px(560), justify="left",
                          style="Muted.TLabel").pack(anchor="w")
                return
            latest = days[-1][0] if days else None          # open on the most recent session
            idx = next((i for i, it in enumerate(items) if it == ("day", latest)), min(1, len(items) - 1))
            if select == "plan":
                idx = 1
            elif select == "ask" or (isinstance(select, str) and select.startswith("ask:")):
                idx = 2
            elif select == "case" or (select is None and client.get("case_overview")):
                idx = 0
            elif select:
                want = select[4:] if str(select).startswith("day:") else \
                    next((d["meta"].get("session_date") for sid, d in sessions if sid == select), None)
                idx = next((i for i, it in enumerate(items) if it[1] == want), idx)
            lb.selection_set(idx)
            lb.see(idx)
            render(idx)

        # ======================================================== ASK AI
        ASK_STARTERS = ["Summarize progress toward each treatment goal.",
                        "What themes come up most often across sessions?",
                        "What should I bring to supervision about this patient?",
                        "Are any risk factors or safety concerns documented?",
                        "Which interventions have been used, and how did the patient respond?"]

        def _render_ask(self, v, client, sessions, chat_id=None):
            """Ask the local AI about this patient. Answers use the SSC manual guidance and this
            patient's own library; every conversation is saved (encrypted) with the patient."""
            client = vault.load_client(client["id"])
            chats = client.get("chats") or []
            if chat_id is None:
                chat_id = getattr(self, "_ask_chat", {}).get(client["id"])
            chat = next((c for c in chats if c["id"] == chat_id), None)
            head = Flow(v, gap=px(8))
            head.pack(fill="x")
            head.add(ttk.Label(head, text="Ask AI", style="H2.TLabel"))
            if chats:
                titles = [f"{c['title'][:48]}  ·  {fmt_date(str(c.get('updated', c.get('created', '')))[:10])}"
                          for c in reversed(chats)]
                ids = [c["id"] for c in reversed(chats)]
                pick = tk.StringVar(value=titles[ids.index(chat["id"])] if chat else "Past conversations…")
                cb = ttk.Combobox(head, textvariable=pick, values=titles, state="readonly", width=26)
                head.add(cb, gap=px(16))
                cb.bind("<<ComboboxSelected>>", lambda e: self._ask_open(client, ids[titles.index(pick.get())]))
            if chat:
                head.add(ttk.Button(head, text="Delete…", command=lambda: self._ask_delete(client, chat)))
            head.add(ttk.Button(head, text="＋ New conversation", command=lambda: self._ask_open(client, None)))
            cap = ttk.Label(v, text="Answers come from the local AI using this patient's notes, overviews, transcripts, "
                                    "treatment plans and case overview, plus the SSC manual. They're drafts — check the "
                                    "cited sessions before relying on them. Nothing leaves this computer.",
                            style="Caption.TLabel", wraplength=px(400), justify="left")
            cap.pack(anchor="w", fill="x", pady=(px(2), px(8)))
            cap.bind("<Configure>", lambda e: cap.config(wraplength=max(px(200), e.width - px(8))))

            # conversation, shown like a document
            fr, t = scrolled_text(v, readonly=True, doc=True)
            P_ = THEME["P"]
            t.tag_configure("who_q", font=F["caption_strong"], foreground=P_["accent"], spacing1=px(14),
                            justify="right", rmargin=px(4))
            t.tag_configure("q", lmargin1=px(140), lmargin2=px(140), rmargin=px(4), justify="right",
                            font=F["doc_b"])
            t.tag_configure("who_a", font=F["caption_strong"], foreground=P_["muted"], spacing1=px(14))
            t.tag_configure("a", lmargin1=0, lmargin2=0, rmargin=px(60))
            t.tag_configure("src", font=F["doc_s"], foreground=P_["muted"], spacing3=px(6))
            t.tag_configure("live", foreground=P_["muted"])
            t.tag_configure("starter", foreground=P_["accent"], spacing1=px(6), lmargin1=px(4), lmargin2=px(22))
            t.tag_bind("starter", "<Enter>", lambda e: t.config(cursor="hand2"))
            t.tag_bind("starter", "<Leave>", lambda e: t.config(cursor="xterm"))

            def show_messages(live=None):
                t.config(state="normal")
                t.delete("1.0", "end")
                msgs = (chat or {}).get("messages", [])
                if not msgs and live is None:
                    t.insert("end", "Ask anything about this patient — their goals, progress, themes, what was said "
                                    "in a session, what to bring to supervision. Try one of these:\n", "who_a")
                    # suggestions live inside the scrolling page, so a short window can still reach them all
                    for i, q in enumerate(self.ASK_STARTERS):
                        tag = f"st{i}"
                        t.insert("end", "›  " + q + "\n", ("starter", tag))
                        t.tag_bind(tag, "<Button-1>", lambda e, q=q: (box.delete("1.0", "end"),
                                                                      box.insert("1.0", q), send()))
                for m_ in msgs:
                    if m_["role"] == "user":
                        t.insert("end", "You · " + str(m_.get("time", ""))[11:16] + "\n", "who_q")
                        t.insert("end", m_["text"].strip() + "\n", "q")
                    else:
                        t.insert("end", "AI\n", "who_a")
                        t.insert("end", m_["text"].strip() + "\n", "a")
                        if m_.get("sources"):
                            t.insert("end", "Based on: " + ", ".join(m_["sources"]) + "\n", "src")
                if live is not None:
                    t.insert("end", "You\n", "who_q")
                    t.insert("end", live[0].strip() + "\n", "q")
                    t.insert("end", "AI\n", "who_a")
                    t.insert("end", (live[1] or (live[2] if len(live) > 2 and live[2] else "Reading the record…"))
                             + "\n", "a" if live[1] else ("a", "live"))
                t.config(state="disabled")
                t.see("end")
            self._ask_view = {"client": client["id"], "show": show_messages, "text": t}

            # the question box is reserved at the bottom first, so the conversation can't squeeze it out
            note = ttk.Label(v, text=("⌘↵" if IS_MAC else "Ctrl+Enter") + " to send", style="Caption.TLabel")
            note.pack(side="bottom", anchor="e")
            ask = ttk.Frame(v)
            ask.pack(side="bottom", fill="x", pady=(px(8), 0))
            send_btn = ttk.Button(ask, text="Ask  ➤", style="Accent.TButton")
            send_btn.pack(side="right", padx=(px(8), 0), fill="y")
            box = register(tk.Text(ask, height=3, width=10, wrap="word", relief="flat", borderwidth=0,
                                   highlightthickness=1, font=F["doc"], padx=px(10), pady=px(8)), "text")
            box.pack(side="left", fill="x", expand=True)
            fr.pack(fill="both", expand=True)

            def send(_=None):
                q = get_text(box).strip()
                if not q:
                    return "break"
                if any(j["owner"] == ("ask", client["id"]) for j in self.waitlist) or \
                        (self.busy and self.task_owner == ("ask", client["id"])):
                    messagebox.showinfo(APP_NAME, "One question at a time for this patient — this one can be asked "
                                                  "once the answer in progress is done.")
                    return "break"
                box.delete("1.0", "end")
                self._ask_send(client, chat["id"] if chat else None, q)
                return "break"
            send_btn.config(command=send)
            box.bind(f"<{MOD}-Return>", send)
            show_messages()
            box.focus_set()
            v._ask = {"box": box, "send": send, "show": show_messages}       # for tests

        def _ask_open(self, client, chat_id):
            self.__dict__.setdefault("_ask_chat", {})[client["id"]] = chat_id
            self.open_client(client["id"], select="ask")

        def _ask_delete(self, client, chat):
            if not messagebox.askyesno(APP_NAME, f"Delete the conversation “{chat['title'][:60]}”?"):
                return
            c = vault.load_client(client["id"])
            c["chats"] = [x for x in c.get("chats") or [] if x["id"] != chat["id"]]
            vault.save_client(c)
            self._sig = vault.signature()
            self._ask_open(client, None)

        def _ask_send(self, client, chat_id, question):
            code = client.get("code", "")
            c = vault.load_client(client["id"])
            sessions = vault.sessions_for(client["id"])
            chat = next((x for x in c.get("chats") or [] if x["id"] == chat_id), None)
            history = (chat or {}).get("messages", [])
            prompt, sources = build_ask_prompt(c, sessions, question, history,
                                               self.settings.get("reference") or BUILTIN_REFERENCE,
                                               self.settings.get("orientation") or "not specified",
                                               budget=max(12000, int(cfg.get("max_chars", 24000))))
            system = ASK_SYSTEM.format(orientation=self.settings.get("orientation") or "not specified")
            job_cfg = dict(cfg)
            live = {"text": "", "at": 0}

            def view_ok():
                av = getattr(self, "_ask_view", None)
                return av and av["client"] == client["id"] and av["text"].winfo_exists() and \
                    self.__dict__.get("_ask_chat", {}).get(client["id"]) == chat_id

            def on_text(txt):
                now = time.time()
                if now - live["at"] < 0.15:
                    return
                live["at"] = now
                self.msgs.put(("call", lambda txt=txt: view_ok() and self._ask_view["show"]((question, txt))))

            def job(progress):
                return llm_chat(job_cfg, prompt, lambda f: progress("Answering…", 100 * f), expected=1400,
                                system=system, on_text=on_text, temperature=0.3)

            def done(answer):
                cc = vault.load_client(client["id"])
                cc.setdefault("chats", [])
                now = datetime.now().isoformat(timespec="seconds")
                ch = next((x for x in cc["chats"] if x["id"] == chat_id), None)
                if ch is None:
                    ch = {"id": uuid.uuid4().hex, "title": re.sub(r"\s+", " ", question)[:70], "created": now,
                          "messages": []}
                    cc["chats"].append(ch)
                ch["messages"] += [{"role": "user", "text": question, "time": now},
                                   {"role": "ai", "text": answer, "time": now, "sources": sources}]
                ch["updated"] = now
                vault.save_client(cc)
                self._sig = vault.signature()
                was_here = view_ok()
                self.__dict__.setdefault("_ask_chat", {})[client["id"]] = ch["id"]
                if was_here and self.client_id == client["id"] and not self.wizard:
                    self.open_client(client["id"], select="ask")
                self.set_status(f"{code}: answer saved to “{ch['title'][:40]}”.")

            def failed(msg):
                if view_ok():
                    self._ask_view["show"]((question, "⚠ " + msg))
            started = self.run_bg(job, done, failed, label=f"answering a question about {code}",
                                  owner=("ask", client["id"]), patient=code,
                                  on_start=lambda: view_ok() and self._ask_view["show"]((question, "")))
            if started == "queued" and view_ok():
                self._ask_view["show"]((question, "", self.waiting_text(("ask", client["id"]))))

        def _render_case(self, v, client, sessions, query=None):
            case = client.get("case_overview") or {}
            head = Flow(v, gap=px(8))
            head.pack(fill="x")
            head.add(ttk.Label(head, text="Case overview", style="H2.TLabel"))
            btn = head.add(ttk.Button(head, text="Update case overview" if case.get("text") else "Create case overview",
                                      style="Accent.TButton", command=lambda: self.make_case_overview(client)),
                           gap=px(16))
            if not sessions:
                btn.config(state="disabled")
            head.add(ttk.Button(head, text="Copy", command=lambda: self.copy(case.get("text", ""), "Case overview")))
            if case.get("text"):
                covered = case.get("session_count", 0)
                gen = case.get("generated", "")
                info = f"Generated {gen[:16].replace('T', ' ')} from {covered} session(s)."
                stale = sum(1 for _, recs in group_by_date(sessions)
                            if max((d["meta"].get("updated") or d["meta"].get("created") or "") for _, d in recs) > gen)
                ttk.Label(v, text=info, style="Muted.TLabel").pack(anchor="w", pady=(2, 0))
                if stale > 0:
                    ttk.Label(v, text=f"{stale} date(s) have new or changed documentation since then — click Update.",
                              style="Warn.TLabel").pack(anchor="w")
            else:
                ttk.Label(v, text="Combines every session note and overview into one current picture: "
                                  "progress by goal, recurring themes, risk history, and questions for "
                                  "supervision. Once created, new session notes use it for continuity.",
                          style="Muted.TLabel", wraplength=px(640), justify="left").pack(anchor="w", pady=(2, 0))
            self.progress_panel(v, owner=("case", client["id"])).pack(anchor="w", fill="x", pady=(px(4), 0))
            fr, t = scrolled_text(v, readonly=True, doc=True)
            fr.pack(fill="both", expand=True, pady=(6, 0))
            set_text(t, case.get("text", ""))
            head.add(find_button(head, t))
            if query:
                t.finder.show(query, focus=False)

        def _render_plan(self, v, client, sessions, query=None, version=None):
            """The patient's treatment plan: current version (or an older one), with
            create/update, edit, copy for the EHR, and goals → patient profile."""
            plans = client.get("treatment_plans") or []
            head = Flow(v, gap=px(16))
            head.pack(fill="x")
            head.add(ttk.Label(head, text="Treatment plan", style="H2.TLabel"))
            head.add(ttk.Button(head, text="Update treatment plan…" if plans else "Create treatment plan…",
                                style="Accent.TButton", command=lambda: self.plan_dialog(client)))
            if not plans:
                ttk.Label(v, text="Drafts a plan in SSC's format (diagnosis, presenting problem, behavioral "
                                  "definitions, goals with measurable objectives and interventions, and the patient "
                                  "agreement statement) from the intake, session notes, case overview and the "
                                  "patient's stated goals. Updating it later builds on the previous plan.",
                          style="Muted.TLabel", wraplength=px(640), justify="left").pack(anchor="w", pady=(px(4), 0))
                self.progress_panel(v, owner=("plan", client["id"])).pack(anchor="w", fill="x", pady=(px(4), 0))
                return
            i = len(plans) - 1 if version is None else version
            plan = plans[i]
            info = Flow(v, gap=px(8))
            info.pack(fill="x", pady=(px(4), 0))
            due = plan_review_due(plan)
            info.add(ttk.Label(info, text=f"Version {plan['version']}  ·  {fmt_date(plan.get('created', '')[:10])}"
                                          + (f"  ·  edited {fmt_date(plan['edited'][:10])}" if plan.get("edited") else "")
                                          + (f"  ·  review by {fmt_date(due.isoformat())}" if due else ""),
                               style="Muted.TLabel"))
            if i == len(plans) - 1 and due and due <= date.today():
                info.add(ttk.Label(info, text="Review due", style="ChipWarn.TLabel"))
            if i != len(plans) - 1:
                info.add(ttk.Label(info, text="Older version", style="ChipMuted.TLabel"))
            if len(plans) > 1:
                names = [f"Version {p['version']} — {fmt_date(p.get('created', '')[:10])}" + (" (current)" if j == len(plans) - 1 else "")
                         for j, p in enumerate(plans)]
                vv = tk.StringVar(value=names[i])
                cb = ttk.Combobox(info, textvariable=vv, values=names, state="readonly", width=30)
                info.add(cb, gap=px(16))
                cb.bind("<<ComboboxSelected>>", lambda e: self._rerender(
                    v, lambda: self._render_plan(v, client, sessions, version=names.index(vv.get()))))
            if plan.get("based_on"):
                ttk.Label(v, text="Based on: " + ", ".join(plan["based_on"]), style="Caption.TLabel",
                          wraplength=px(760), justify="left").pack(anchor="w", pady=(px(2), 0))
            self.progress_panel(v, owner=("plan", client["id"])).pack(anchor="w", fill="x", pady=(px(4), 0))
            bar = Flow(v)
            bar.pack(fill="x", pady=(px(6), 0))
            fr, t = scrolled_text(v, readonly=True, doc=True)
            fr.pack(fill="both", expand=True, pady=(px(6), 0))
            set_text(t, plan["text"])
            bar.add(ttk.Button(bar, text="Copy plan for TheraNest", style="Accent.TButton",
                               command=lambda: self.copy(plan_for_ehr(plan["text"]), "Treatment plan")))
            edit_btn = bar.add(ttk.Button(bar, text="Edit"))
            goals = plan_goals(plan["text"])
            if goals:
                bar.add(ttk.Button(bar, text="Goals → patient profile",
                                   command=lambda: self.goals_to_profile(client, goals)))
            bar.add(find_button(bar, t))
            if i == len(plans) - 1:
                bar.add(ttk.Button(bar, text="Delete…", command=lambda: self.delete_plan(client, i)))

            def toggle_edit():
                if str(t.cget("state")) == "disabled":
                    t.config(state="normal")
                    color_widget(t, "text")
                    t.focus_set()
                    edit_btn.config(text="Save changes", style="Accent.TButton")
                    self._page_editing = True          # don't refresh this page under the edit
                else:
                    c = vault.load_client(client["id"])
                    c["treatment_plans"][i]["text"] = get_text(t).strip()
                    c["treatment_plans"][i]["edited"] = datetime.now().isoformat(timespec="seconds")
                    vault.save_client(c)
                    self.open_client(client["id"], select="plan")
                    self.set_status("Treatment plan saved.")
            edit_btn.config(command=toggle_edit)
            if query:
                t.finder.show(query, focus=False)

        def _rerender(self, v, fn):
            for w in v.winfo_children():
                w.destroy()
            fn()

        def goals_to_profile(self, client, goals):
            c = vault.load_client(client["id"])
            new = "\n".join(goals)
            if c.get("goals", "").strip() and not messagebox.askyesno(
                    APP_NAME, "Replace the treatment goals in the patient profile with the plan's goals?\n\n"
                              + new):
                return
            c["goals"] = new
            vault.save_client(c)
            self.open_client(client["id"], select="plan")
            self.set_status("Patient profile goals updated from the treatment plan. New notes will use them.")

        def delete_plan(self, client, index):
            if not messagebox.askyesno(APP_NAME, "Delete this version of the treatment plan? Older versions stay."):
                return
            c = vault.load_client(client["id"])
            c["treatment_plans"].pop(index)
            vault.save_client(c)
            self.open_client(client["id"], select="plan")

        def plan_dialog(self, client):
            """Options before drafting a treatment plan."""
            client = vault.load_client(client["id"])
            sessions = [d for _, d in vault.sessions_for(client["id"])]
            prev = current_plan(client)
            win = new_window(root, "Treatment plan", "640x520")
            win.grab_set()
            f = ttk.Frame(win, padding=(px(24), px(20)))
            f.pack(fill="both", expand=True)
            ttk.Label(f, text="Update treatment plan" if prev else "Create treatment plan", style="H2.TLabel").pack(anchor="w")
            n_notes = sum(1 for d in sessions if d.get("note", "").strip() and d.get("meta", {}).get("kind") != "intake")
            has_intake = any(d.get("meta", {}).get("kind") == "intake" for d in sessions)
            src = ([f"previous plan (v{prev['version']})"] if prev else []) + \
                  (["intake"] if has_intake else []) + [f"{n_notes} session note{'s' if n_notes != 1 else ''}"] + \
                  (["case overview"] if (client.get("case_overview") or {}).get("text") else []) + \
                  (["patient's stated goals"] if client.get("goals") else [])
            ttk.Label(f, text="Will be based on: " + ", ".join(src), style="Muted.TLabel", wraplength=px(560),
                      justify="left").pack(anchor="w", pady=(px(2), px(12)))
            if not has_intake:
                ttk.Label(f, text="No intake on file yet. The plan will rest on the session notes alone; importing "
                                  "the intake first gives a better plan.", style="Warn.TLabel", wraplength=px(560),
                          justify="left").pack(anchor="w", pady=(0, px(8)))
            row = ttk.Frame(f)
            row.pack(anchor="w")
            ttk.Label(row, text="Review in").pack(side="left")
            days_var = tk.StringVar(value=str((prev or {}).get("review_days") or 90))
            ttk.Spinbox(row, from_=30, to=180, increment=15, textvariable=days_var, width=5).pack(side="left", padx=px(6))
            ttk.Label(row, text="days  (SSC: 60–90 unless your supervisor says otherwise)", style="Muted.TLabel").pack(side="left")
            ttk.Label(f, text="Anything to focus on or change? (optional)", style="Strong.TLabel").pack(
                anchor="w", pady=(px(14), px(2)))
            ttk.Label(f, text="e.g. “Add a goal for sleep”, “Use DBT-informed interventions for goal 2”, "
                              "“Patient is 15 — reference parent agreement”, “Retire the school goal; it was met”.",
                      style="Caption.TLabel", wraplength=px(560), justify="left").pack(anchor="w")
            ifr, instr = scrolled_text(f, height=5)
            ifr.pack(fill="both", expand=True, pady=(px(4), 0))
            btns = pin_bottom(ttk.Frame(f), pady=(px(14), 0))

            def go():
                try:
                    review = max(1, int(days_var.get()))
                except ValueError:
                    review = 90
                text = get_text(instr).strip()
                win.destroy()
                self.make_treatment_plan(client, review, text, src)
            ttk.Button(btns, text="Write plan", style="Accent.TButton", command=go).pack(side="right")
            ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right", padx=px(8))
            win._go, win._instr, win._days = go, instr, days_var
            self._plan_win = win

        def make_treatment_plan(self, client, review_days, instructions, based_on):
            sessions = [d for _, d in vault.sessions_for(client["id"])]
            job_cfg, orient = dict(cfg), self.settings.get("orientation", "")
            code = client.get("code", "")

            def done(text):
                c = vault.load_client(client["id"])
                plans = c.setdefault("treatment_plans", [])
                plans.append({"version": (plans[-1]["version"] + 1) if plans else 1, "text": text,
                              "created": datetime.now().isoformat(timespec="seconds"),
                              "review_days": review_days, "based_on": based_on,
                              "instructions": instructions})
                vault.save_client(c)
                if self.client_id == client["id"] and self.wizard is None:
                    self.open_client(client["id"], select="plan")
                self.set_status(f"{code}: treatment plan drafted. Review it, check every [VERIFY], and confirm with "
                                "your supervisor before entering it in TheraNest.")
            if self.run_bg(lambda p: generate_treatment_plan(job_cfg, client, sessions, orient, review_days,
                                                             instructions, p), done,
                           label=f"writing the treatment plan for {code}", owner=("plan", client["id"]),
                           patient=code):
                self.open_client(client["id"], select="plan")

        def _render_day(self, v, client, day, records, focus=None, query=None):
            """Everything saved for one date, one tab per document. focus=(sid, key) opens
            that tab; query highlights every match in it."""
            head = Flow(v, gap=px(16))
            head.pack(fill="x")
            head.add(ttk.Label(head, text=f"{day_title(records)} — {fmt_date(day)}", style="H2.TLabel"))
            head.add(ttk.Button(head, text="Change date / session #…",
                                command=lambda: self.redate(client, records, day)))
            for rsid, rd in records:
                if rd["meta"].get("needs_review"):
                    ban = ttk.Frame(v, style="Card.TFrame", padding=(px(12), px(8)))
                    ban.pack(fill="x", pady=(px(6), 0))
                    msg = ttk.Label(ban, text="⚠  Drafted unattended from the recording queue — "
                                              + rd["meta"].get("review_reason", "please review") + ". "
                                              "To fix speakers: Edit → ◀ Back → Who's who, then Write notes.",
                                    style="Warn.TLabel", justify="left", wraplength=px(640))
                    msg.pack(fill="x")
                    head.add(ttk.Button(head, text="✓  Mark as reviewed", style="Accent.TButton",
                                        command=lambda rsid=rsid, rd=rd: self.mark_reviewed(client, rsid, rd, day)),
                             gap=px(6))
            if len(records) > 1:
                srcs = ["generated session" if d["meta"].get("source") != "imported" else "imported note"
                        for _, d in records]
                ttk.Label(v, text="Combined for this date: " + ", ".join(srcs),
                          style="Muted.TLabel").pack(anchor="w", pady=(2, 0))
            nb = ttk.Notebook(v)
            nb.pack(fill="both", expand=True, pady=(6, 0))
            v._tabs = {}
            for label, key, sid, d in day_docs(records):
                tab = ttk.Frame(nb, padding=(0, 6, 0, 0))
                nb.add(tab, text=f"  {label}  ")
                bar = Flow(tab)
                bar.pack(fill="x")
                bar.add(ttk.Button(bar, text=f"Copy {label.split(' (')[0].lower()}", style="Accent.TButton",
                                   command=lambda d=d, key=key, label=label: self.copy(d.get(key, ""), label)))
                bar.add(ttk.Button(bar, text="Edit", command=lambda sid=sid, d=d: self.start_wizard(client, sid, d)))
                if key == "note":
                    bar.add(self.note_copy_menu(bar, lambda d=d: d.get("note", "")))
                if len(records) > 1:
                    bar.add(ttk.Button(bar, text="Move to another date…",
                                       command=lambda sid=sid, d=d: self.redate(client, [(sid, d)], day)))
                if key == "note" and d.get("context_used"):
                    ttk.Label(tab, text="Built on: " + ", ".join(d["context_used"]),
                              style="Muted.TLabel", wraplength=px(760), justify="left").pack(anchor="w", pady=(4, 0))
                fr, t = scrolled_text(tab, readonly=True, doc=True)
                fr.pack(fill="both", expand=True, pady=(6, 0))
                set_text(t, d.get(key, ""))
                bar.add(find_button(bar, t))
                bar.add(ttk.Button(bar, text="Delete…", command=lambda sid=sid, d=d: self.delete_session(client, sid, d)))
                v._tabs[(sid, key)] = (tab, t)
            if focus in v._tabs:
                tab, t = v._tabs[focus]
                nb.select(tab)
                if query:
                    t.finder.show(query, focus=False)

        def appointment_check(self, client):
            """Compare the library with TheraNest's appointment history report: which dates were real
            sessions (not canceled, rescheduled or service 99997) and what number each should be."""
            win = new_window(root, f"Check against TheraNest — {client.get('code', '')}", "1040x720")
            f = ttk.Frame(win, padding=(px(24), px(18)))
            f.pack(fill="both", expand=True)
            ttk.Label(f, text="Check dates & session numbers", style="Title.TLabel").pack(anchor="w")
            ttk.Label(f, text="Load this patient's Appointment History report from TheraNest (PDF, Excel, CSV or "
                              "Word — or paste it). Appointments marked Canceled or Rescheduled, and service type "
                              "99997, aren't counted as sessions; upcoming ones aren't either. The rest are numbered "
                              "in date order and compared with this patient's notes. The report is read on this "
                              "computer only; just the dates, service codes and statuses are kept (encrypted).",
                      style="Muted.TLabel", wraplength=px(980), justify="left").pack(anchor="w", pady=(px(2), px(10)))
            bar = ttk.Frame(f)
            bar.pack(fill="x")
            first_var = tk.StringVar(value=str(client.get("appointments", {}).get("first_number", 1)))
            src_lbl = ttk.Label(f, text="", style="Caption.TLabel", wraplength=px(980), justify="left")
            src_lbl.pack(anchor="w", pady=(px(8), 0))
            cols = ("date", "theranest", "library", "result")
            tv_box, tv = tree_box(f, columns=cols, show="headings", height=4, selectmode="browse")
            for c, title, wdt in (("date", "Date", 120), ("theranest", "TheraNest session #", 160),
                                  ("library", "Session Scribe #", 150), ("result", "Result", 560)):
                tv.heading(c, text=title, anchor="w")
                tv.column(c, width=px(wdt), stretch=(c == "result"), anchor="w")
            P = THEME["P"]
            tv.tag_configure("ok", foreground=P["good"])
            tv.tag_configure("fix", foreground=P["warn"])
            tv.tag_configure("bad", foreground=P["bad"])
            tv_box.pack(fill="both", expand=True, pady=(px(8), px(8)))
            summary = ttk.Label(f, text="", style="Muted.TLabel", wraplength=px(980), justify="left")
            pin_bottom(summary)
            bottom = ttk.Frame(f)
            pin_bottom(bottom, pady=(px(10), 0))          # pinned last = nearest the bottom edge
            ttk.Button(bottom, text="Close", command=win.destroy).pack(side="right")
            fix_btn = ttk.Button(bottom, text="Apply fixes", style="Accent.TButton")
            fix_btn.pack(side="right", padx=px(8))
            state = {"rows": []}

            def show():
                c = vault.load_client(client["id"])
                ap = c.get("appointments") or {}
                items = ap.get("items") or []
                tv.delete(*tv.get_children())
                if not items:
                    src_lbl.config(text="No appointment history loaded yet.")
                    summary.config(text="")
                    fix_btn.config(state="disabled")
                    return
                counted = [a for a in items if a["counted"]]
                skipped = {}
                for a in items:
                    if not a["counted"]:
                        skipped[a["why"] or a["status"]] = skipped.get(a["why"] or a["status"], 0) + 1
                src_lbl.config(text=f"Using {ap.get('source', 'the report')} (loaded {fmt_date(ap.get('loaded', '')[:10])}): "
                                    f"{len(items)} appointments, {len(counted)} counted as sessions"
                                    + (" · not counted: " + ", ".join(f"{n} {k}" for k, n in skipped.items())
                                       if skipped else ""))
                try:
                    first = int(first_var.get())
                except ValueError:
                    first = 1
                rows = compare_appointments(items, vault.sessions_for(client["id"]), first)
                state["rows"] = rows
                for r in rows:
                    tag = "ok" if r["kind"] == "ok" else "fix" if (r["fix_num"] or r["fix_date"] or r.get("clear_num")) else "bad"
                    tv.insert("", "end", values=(fmt_date(r["date"]), "" if r["expected"] is None else f"#{r['expected']}",
                                                 "" if r["have"] is None else f"#{r['have']}", fmt_dates_in(r["note"])),
                              tags=(tag,))
                n_fix = sum(1 for r in rows if r["fix_num"] or r["fix_date"] or r.get("clear_num"))
                n_ok = sum(1 for r in rows if r["kind"] == "ok")
                n_missing = sum(1 for r in rows if r["kind"] == "missing")
                n_other = sum(1 for r in rows if r["kind"] == "extra" and not r["fix_date"])
                summary.config(text=f"✓ {n_ok} match · {n_fix} can be fixed automatically"
                                    + (f" · {n_missing} TheraNest session(s) with no note here" if n_missing else "")
                                    + (f" · {n_other} date(s) to check by hand" if n_other else ""))
                fix_btn.config(state="normal" if n_fix else "disabled",
                               text=f"Apply {n_fix} fix{'es' if n_fix != 1 else ''}" if n_fix else "Nothing to fix")

            def load(items, source):
                if not items:
                    return messagebox.showinfo(APP_NAME, "Couldn't find any dated appointments in that report. "
                                                         "Try the PDF or Excel export of the Appointment History "
                                                         "report.", parent=win)
                c = vault.load_client(client["id"])
                c["appointments"] = {"items": items, "source": source, "first_number": first_var.get(),
                                     "loaded": datetime.now().isoformat(timespec="seconds")}
                vault.save_client(c)
                self._sig = vault.signature()
                show()

            def from_file():
                path = filedialog.askopenfilename(parent=win, title="Choose the Appointment History report",
                                                  filetypes=[("Reports", "*.pdf *.xlsx *.csv *.docx *.txt"),
                                                             ("All files", "*.*")])
                if not path:
                    return
                try:
                    items = read_appointment_report(path)
                except Exception as e:
                    return messagebox.showerror(APP_NAME, f"Couldn't read that file: {friendly_error(e)}", parent=win)
                load(items, os.path.basename(path))

            def from_paste():
                dlg = new_window(win, "Paste appointment history", "760x520")
                dlg.grab_set()
                g = ttk.Frame(dlg, padding=px(16))
                g.pack(fill="both", expand=True)
                ttk.Label(g, text="Paste the report (select all in TheraNest, copy, paste here):").pack(anchor="w")
                fr, txt = scrolled_text(g, height=14)
                fr.pack(fill="both", expand=True, pady=px(8))
                def ok():
                    items = read_appointment_report(text=get_text(txt))
                    dlg.destroy()
                    load(items, "pasted report")
                ttk.Button(g, text="Use this", style="Accent.TButton", command=ok).pack(side="right")
                root._paste_appts = (txt, ok)

            def apply_fixes():
                rows = [r for r in state["rows"] if r["fix_num"] or r["fix_date"] or r.get("clear_num")]
                if not rows:
                    return
                lines = []
                for r in rows:
                    if r["fix_date"]:
                        lines.append(f"• {r['date']} → move to {r['fix_date']}, session #{r['fix_num']}")
                    elif r.get("clear_num"):
                        lines.append(f"• {r['date']}: remove session #{r['have']} (not a session in TheraNest)")
                    else:
                        lines.append(f"• {r['date']}: session #{r['have'] if r['have'] is not None else '–'} "
                                     f"→ #{r['fix_num']}")
                if not messagebox.askyesno(APP_NAME, "Apply these changes?\n\n" + "\n".join(lines[:25])
                                           + ("\n…" if len(lines) > 25 else ""), parent=win):
                    return
                now = datetime.now().isoformat(timespec="seconds")
                by_day = {r["date"]: r for r in rows}
                for sid, d in vault.sessions_for(client["id"]):
                    r = by_day.get(d["meta"].get("session_date"))
                    if not r:
                        continue
                    if r["fix_date"]:
                        d["meta"]["session_date"] = r["fix_date"]
                    if r.get("clear_num"):
                        d["meta"]["session_number"] = ""
                    elif r["fix_num"] is not None:
                        d["meta"]["session_number"] = str(r["fix_num"])
                    d["meta"]["updated"] = now
                    vault.save_session(d, sid)
                self._sig = vault.signature()
                show()
                if self.client_id == client["id"] and not self.wizard:
                    self.open_client(client["id"])
                self.set_status(f"{client.get('code', '')}: applied {len(rows)} fix(es) from TheraNest's "
                                "appointment history.")

            ttk.Button(bar, text="Load report…", style="Accent.TButton", command=from_file).pack(side="left")
            ttk.Button(bar, text="Paste…", command=from_paste).pack(side="left", padx=px(8))
            ttk.Label(bar, text="First counted session is #").pack(side="left", padx=(px(20), px(4)))
            sp = ttk.Spinbox(bar, from_=0, to=5, textvariable=first_var, width=4, command=show)
            sp.pack(side="left")
            sp.bind("<FocusOut>", lambda e: show())
            fix_btn.config(command=apply_fixes)
            win._test = {"load": load, "apply": apply_fixes, "tv": tv, "summary": summary, "first": first_var,
                         "show": show}
            root._appt_win = win
            show()

        def mark_reviewed(self, client, sid, d, day):
            d["meta"]["needs_review"] = False
            d["meta"].pop("review_reason", None)
            d["meta"]["updated"] = datetime.now().isoformat(timespec="seconds")
            vault.save_session(d, sid)
            self.refresh_clients(client["id"])
            self.open_client(client["id"], select=f"day:{day}")
            self.set_status(f"{client.get('code', '')}: {fmt_date(day)} marked as reviewed.")

        def redate(self, client, records, old_day):
            """Move one record (or everything on a date) to a different date."""
            win = new_window(root, "Change date or session #")
            win.grab_set()
            f = ttk.Frame(win, padding=(px(24), px(20)))
            f.pack(fill="both", expand=True)
            docs = [label for label, *_ in day_docs(records)]
            what = "everything on this date" if len(records) > 1 else ", ".join(docs)
            ttk.Label(f, text="Change date or session #", style="H2.TLabel").grid(row=0, column=0, columnspan=2,
                                                                                 sticky="w")
            ttk.Label(f, text=f"Applies to {what} on {fmt_date(old_day)}. Documents saved together "
                              f"({', '.join(docs)}) move together.", style="Muted.TLabel",
                      wraplength=px(440), justify="left").grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 10))
            d_var = tk.StringVar(value=old_day if old_day != "undated" else date.today().isoformat())
            cur_num = next((d["meta"].get("session_number") for _, d in records if d["meta"].get("session_number")), "")
            n_var = tk.StringVar(value=cur_num)
            ttk.Label(f, text="Date").grid(row=2, column=0, sticky="w")
            dfield = date_field(f, d_var, width=14)
            dfield.grid(row=2, column=1, sticky="w", padx=8, pady=3)
            e = dfield.entry
            ttk.Label(f, text="Session #").grid(row=3, column=0, sticky="w")
            ttk.Entry(f, textvariable=n_var, width=6).grid(row=3, column=1, sticky="w", padx=8, pady=3)
            shift_var = tk.BooleanVar(value=True)
            shift_cb = ttk.Checkbutton(f, text="Also renumber the sessions after this one", variable=shift_var)
            shift_cb.grid(row=4, column=0, columnspan=2, sticky="w", pady=(px(4), 0))
            preview = ttk.Label(f, text="", style="Muted.TLabel", wraplength=px(440), justify="left")
            preview.grid(row=5, column=0, columnspan=2, sticky="w")
            msg = ttk.Label(f, text="", style="Bad.TLabel", wraplength=px(440), justify="left")
            msg.grid(row=6, column=0, columnspan=2, sticky="w", pady=(4, 0))
            moving_ids = {sid for sid, _ in records}

            def plan(new_day, num):
                """Which later dates get renumbered, and to what (same shift as this one)."""
                old_n, new_n = _num(cur_num), _num(num)
                if not shift_var.get() or old_n is None or new_n is None or old_n == new_n:
                    return {}
                others = [(sid, d) for sid, d in vault.sessions_for(client["id"]) if sid not in moving_ids]
                return {day_: n + (new_n - old_n) for day_, n in day_numbers(others).items()
                        if day_ > (new_day or old_day)}

            def update_preview(*_):
                new_day, num = d_var.get().strip(), n_var.get().strip()
                changes = plan(new_day, num)
                if changes:
                    items = sorted(changes.items())
                    others = day_numbers([(sid, d) for sid, d in vault.sessions_for(client["id"])
                                          if sid not in moving_ids])
                    txt = ", ".join(f"{fmt_date(d_)}: #{others[d_]} → #{n}" for d_, n in items[:4])
                    preview.config(text=f"Will also renumber {len(items)} later session(s): {txt}"
                                        + (" …" if len(items) > 4 else ""))
                else:
                    preview.config(text="")
            for v_ in (n_var, d_var, shift_var):
                v_.trace_add("write", update_preview)

            def apply(_=None):
                new = d_var.get().strip()
                try:
                    datetime.strptime(new, "%Y-%m-%d")
                except ValueError:
                    return msg.config(text=f"Use the format {date_hint()}, e.g. {fmt_date('2026-11-06')} "
                                           "(or pick it from the calendar).")
                num = n_var.get().strip()
                if new == old_day and num == cur_num:
                    return win.destroy()
                if num and _num(num) is None:
                    return msg.config(text="Session # must be a number.")
                moving = moving_ids
                changes = plan(new, num)
                # duplicates are an error: the same session # on two different dates
                if _num(num) is not None:
                    others = day_numbers([(sid, d) for sid, d in vault.sessions_for(client["id"])
                                          if sid not in moving])
                    others.update(changes)
                    clash = sorted(d_ for d_, n in others.items() if n == _num(num) and d_ != new)
                    if clash:
                        return msg.config(text=f"Session #{num} is already used on {', '.join(clash)}. "
                                               "Choose another number"
                                               + (", or tick “Also renumber…” to shift the later sessions."
                                                  if not shift_var.get() else "."))
                already = [d for sid, d in vault.sessions_for(client["id"])
                           if sid not in moving and d["meta"].get("session_date") == new]
                if already and new != old_day and not messagebox.askyesno(
                        APP_NAME, f"There's already documentation on {fmt_date(new)} ({day_title([(None, d) for d in already])}). "
                                  "Combine them under that date?", parent=win):
                    return
                now = datetime.now().isoformat(timespec="seconds")
                for sid, _ in records:
                    d = vault.load_session(sid)                  # fresh copy
                    d["meta"]["session_date"] = new
                    if num != cur_num:
                        d["meta"]["session_number"] = num
                    d["meta"]["updated"] = now
                    vault.save_session(d, sid)
                if changes:
                    for sid, d in vault.sessions_for(client["id"]):
                        day_ = d["meta"].get("session_date")
                        if sid not in moving and day_ in changes and _num(d["meta"].get("session_number")) is not None:
                            d["meta"]["session_number"] = str(changes[day_])
                            d["meta"]["updated"] = now
                            vault.save_session(d, sid)
                win.destroy()
                self.open_client(client["id"], select=f"day:{new}")
                bits = []
                if new != old_day:
                    bits.append(f"Moved to {fmt_date(new)}." + (" Combined with what was already there." if already else ""))
                if num != cur_num:
                    bits.append(f"Now session #{num}" + (f"; renumbered {len(changes)} later session(s)."
                                                         if changes else "."))
                self.set_status(" ".join(bits))

            btns = ttk.Frame(f)
            btns.grid(row=7, column=0, columnspan=2, sticky="e", pady=(12, 0))
            ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
            ttk.Button(btns, text="Save", style="Accent.TButton", command=apply).pack(side="right", padx=6)
            win._test = {"date": d_var, "num": n_var, "shift": shift_var, "apply": apply, "msg": msg,
                         "preview": preview}
            root._redate = win
            e.bind("<Return>", apply)
            e.focus_set()
            e.select_range(0, "end")
            win.update_idletasks()
            win.geometry(f"+{root.winfo_rootx() + (root.winfo_width() - win.winfo_reqwidth()) // 2}"
                         f"+{root.winfo_rooty() + (root.winfo_height() - win.winfo_reqheight()) // 3}")
            self._redate_win = win
            win._apply, win._d, win._n = apply, d_var, n_var

        def edit_client(self, client):
            if self.client_dialog(client):
                self.refresh_clients(client["id"])
                self.open_client(client["id"])

        def set_archived(self, client, archived):
            """Archive hides a patient from the list (e.g. after termination) without
            deleting anything; Restore brings them back."""
            c = vault.load_client(client["id"])
            if archived:
                c["archived"] = True
                c["archived_on"] = date.today().isoformat()
            else:
                c.pop("archived", None)
                c.pop("archived_on", None)
            vault.save_client(c)
            if archived:
                self.refresh_clients()
                self.client_id = None
                self.show_home()
                self.set_status(f"{c['code']} archived. Nothing was deleted. Tick “Show archived” in the patient "
                                "list to see or restore them.")
            else:
                self.refresh_clients(c["id"])
                self.open_client(c["id"])
                self.set_status(f"{c['code']} restored to your active patients.")

        def delete_client(self, client):
            code = simpledialog.askstring(
                APP_NAME, f"This permanently deletes {client['code']} and ALL of their sessions.\n\n"
                          "Type the patient code to confirm:", parent=root)
            if code is not None and code.strip() == client["code"]:
                for w in (self.parked, self.wizard):
                    if w is not None and w["client"]["id"] == client["id"]:
                        self.abandon_wizard(w)
                for kind in ("plan", "case", "ask"):
                    self.cancel_task((kind, client["id"]), f"{client['code']} deleted — their task was stopped.")
                if self.parked is not None and self.parked.get("discarded"):
                    self.parked = None
                self.update_return_button()
                vault.delete_client(client["id"])
                self.client_id = None
                self.refresh_clients()
                self.show_home()
                self.set_status("Patient deleted.")
            elif code is not None:
                messagebox.showinfo(APP_NAME, "The code didn't match — nothing was deleted.")

        def delete_session(self, client, sid, d=None):
            docs = ", ".join(label for label, _ in record_docs(d)) if d else "this session"
            day = (d or {}).get("meta", {}).get("session_date", "")
            if messagebox.askyesno(APP_NAME, f"Permanently delete {docs}" + (f" from {fmt_date(day)}" if day else "")
                                             + "?\n\nThis can't be undone."):
                vault.delete_session(sid)
                self.open_client(client["id"], select=f"day:{day}" if day else None)

        def export_all(self, client, sessions=None):
            """Export chosen documents for one patient, optionally between two dates, as Word or PDF."""
            client = vault.load_client(client["id"])
            sessions = vault.sessions_for(client["id"])
            days = sorted({d["meta"].get("session_date") for _, d in sessions if d["meta"].get("session_date")})
            win = new_window(root, f"Export — {client.get('code', '')}", "720x640")
            win.grab_set()
            f = ttk.Frame(win, padding=(px(26), px(20)))
            f.pack(fill="both", expand=True)
            ttk.Label(f, text="Export", style="Title.TLabel").pack(anchor="w")
            ttk.Label(f, text=f"Choose what to include for {client.get('code', '')}. Everything goes into one "
                              "document, oldest first.", style="Muted.TLabel", wraplength=px(640),
                      justify="left").pack(anchor="w", pady=(px(2), px(12)))
            what = ttk.LabelFrame(f, text="Include", padding=px(10))
            what.pack(fill="x")
            defaults = {"notes", "intake", "plan", "case"}
            inc = {k: tk.BooleanVar(value=k in defaults) for k, _ in EXPORT_KINDS}
            for i_, (k, label) in enumerate(EXPORT_KINDS):
                ttk.Checkbutton(what, text=label, variable=inc[k]).grid(row=i_ // 2, column=i_ % 2, sticky="w",
                                                                        padx=(0, px(30)), pady=px(2))
            rng = ttk.LabelFrame(f, text="Dates", padding=px(10))
            rng.pack(fill="x", pady=(px(12), 0))
            all_var = tk.BooleanVar(value=True)
            a_var = tk.StringVar(value=days[0] if days else date.today().isoformat())
            b_var = tk.StringVar(value=days[-1] if days else date.today().isoformat())
            ttk.Radiobutton(rng, text="All dates", value=True, variable=all_var).grid(row=0, column=0, sticky="w")
            between = Flow(rng, gap=px(8))
            between.grid(row=1, column=0, sticky="ew", pady=(px(6), 0))
            rng.columnconfigure(0, weight=1)
            between.add(ttk.Radiobutton(between, text="Between", value=False, variable=all_var))
            a_fld = between.add(date_field(between, a_var, on_pick=lambda _d: all_var.set(False)))
            between.add(ttk.Label(between, text="and"))
            b_fld = between.add(date_field(between, b_var, on_pick=lambda _d: all_var.set(False)))
            for e_ in (a_fld.entry, b_fld.entry):
                e_.bind("<Key>", lambda e: all_var.set(False), add="+")
            fmt = ttk.LabelFrame(f, text="Format", padding=px(10))
            fmt.pack(fill="x", pady=(px(12), 0))
            fmt_var = tk.StringVar(value=cfg.get("export_format", "docx"))
            ttk.Radiobutton(fmt, text="Word (.docx) — editable", value="docx", variable=fmt_var).pack(side="left")
            ttk.Radiobutton(fmt, text="PDF", value="pdf", variable=fmt_var).pack(side="left", padx=px(24))
            ttk.Label(f, text="⚠  Exported files are NOT encrypted. Save them somewhere secure (not a synced cloud "
                              "folder) and delete them when you're done.", style="Warn.TLabel", wraplength=px(640),
                      justify="left").pack(anchor="w", pady=(px(14), 0))
            info = ttk.Label(f, text="", style="Bad.TLabel")
            info.pack(anchor="w", pady=(px(6), 0))

            def go():
                include = {k for k, v in inc.items() if v.get()}
                if not include:
                    return info.config(text="Tick at least one thing to include.")
                start = end = ""
                if not all_var.get():
                    start, end = a_var.get().strip(), b_var.get().strip()
                    try:
                        date.fromisoformat(start)
                        date.fromisoformat(end)
                    except ValueError:
                        return info.config(text=f"Use dates like {fmt_date('2026-09-01')} (or pick them from "
                                                "the calendar).")
                    if start > end:
                        start, end = end, start
                blocks = collect_export(client, sessions, include, start, end)
                if len(blocks) <= 2:
                    return info.config(text="Nothing matches those choices and dates.")
                ext = fmt_var.get()
                rng_txt = f"_{start}_to_{end}" if start else ""
                path = filedialog.asksaveasfilename(
                    parent=win, defaultextension="." + ext, initialfile=f"{client.get('code', 'patient')}{rng_txt}.{ext}",
                    filetypes=[("Word document", "*.docx")] if ext == "docx" else [("PDF", "*.pdf")])
                if not path:
                    return
                if is_cloud_path(path) and not messagebox.askyesno(
                        APP_NAME, "That location is a cloud-synced folder, so this UNENCRYPTED export would be "
                                  "uploaded as readable text.\n\nSave it there anyway?", parent=win):
                    return
                try:
                    footer = f"{client.get('code', '')} · Confidential"
                    (write_docx if ext == "docx" else write_pdf)(path, blocks, footer=footer)
                except Exception as e:
                    return messagebox.showerror(APP_NAME, f"Couldn't export: {friendly_error(e)}", parent=win)
                cfg["export_format"] = ext
                save_config(cfg)
                win.destroy()
                self.set_status(f"{client.get('code', '')}: exported to {os.path.basename(path)}.")
                if messagebox.askyesno(APP_NAME, f"Exported to:\n{path}\n\nOpen it now?"):
                    try:
                        open_path(path)
                    except Exception:
                        pass
            btns = pin_bottom(ttk.Frame(f), pady=(px(16), 0))
            ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
            ttk.Button(btns, text="Export…", style="Accent.TButton", command=go).pack(side="right", padx=px(8))
            win._test = {"inc": inc, "all": all_var, "a": a_var, "b": b_var, "fmt": fmt_var, "go": go, "info": info,
                         "a_fld": a_fld}
            root._export_win = win

        def tidy_imported(self, client):
            """Apply the import clean-up to notes that were imported before it existed."""
            changes = []
            for sid, d in vault.sessions_for(client["id"]):
                meta = d.get("meta", {})
                if meta.get("source") != "imported":
                    continue
                new = tidy_ehr_note(d.get("note", ""), meta.get("kind", "progress"))
                if new and new != d.get("note", "").strip():
                    changes.append((sid, d, new))
            c = vault.load_client(client["id"])
            plan_changes = [(pl, tidy_ehr_note(pl["text"], "plan")) for pl in c.get("treatment_plans", [])
                            if pl.get("imported")]
            plan_changes = [(pl, t) for pl, t in plan_changes if t and t != pl["text"].strip()]
            if not changes and not plan_changes:
                return messagebox.showinfo(APP_NAME, "All imported notes for this patient are already tidy.")
            if not messagebox.askyesno(
                    APP_NAME, f"Tidy {len(changes)} imported note(s)"
                              + (f" and {len(plan_changes)} imported treatment plan(s)" if plan_changes else "")
                              + "?\n\n• Removes the SOUTH SHORE COUNSELING letterhead and heading\n"
                                "• Removes the Insurer(s) line and the Staff Signature\n"
                                "• Keeps only mental-status items that aren't just \"WNL\"\n"
                                "• Puts the primary focus of the session at the top\n\n"
                                "Notes you wrote or generated in Session Scribe aren't touched."):
                return
            for sid, d, new in changes:
                d["note"] = new
                d["meta"]["updated"] = datetime.now().isoformat(timespec="seconds")
                d["meta"]["tidy_version"] = TIDY_VERSION
                vault.save_session(d, sid)
            if plan_changes:
                for pl, t in plan_changes:
                    pl["text"] = t
                    pl["tidy_version"] = TIDY_VERSION
                vault.save_client(c)
            self.open_client(client["id"])
            self.set_status(f"Tidied {len(changes)} imported note(s)"
                            + (f" and {len(plan_changes)} plan(s)" if plan_changes else "") + ".")

        def import_dialog(self, client):
            """Bring in notes written before using the app, so new notes and the case
            overview build on the full history."""
            win = new_window(root, f"Import past notes — {client['code']}", "1000x700")
            win.grab_set()
            f = ttk.Frame(win, padding=(px(24), px(20)))
            f.pack(fill="both", expand=True)
            ttk.Label(f, text="Import past notes", style="Title.TLabel").pack(anchor="w")
            ttk.Label(f, text="Add files (.txt, .docx, .pdf) or paste notes. TheraNest exports are split "
                              "automatically: “INITIAL ASSESSMENT AND DIAGNOSTIC IMPRESSION” becomes the intake, "
                              "“MUTUAL TREATMENT PLAN” a treatment plan (its “Date:”), and each “PROGRESS NOTES” a "
                              "note (its “Service Date:”); pages of the same document are joined back together. "
                              "Check each entry's date and type, then import.",
                      style="Muted.TLabel", wraplength=px(920), justify="left").pack(anchor="w", pady=(2, 8))
            items = []
            split_var = tk.BooleanVar(value=True)
            bar = Flow(f, gap=px(8))
            bar.pack(fill="x")

            cols = ("date", "num", "kind", "source", "preview")
            tv = ttk.Treeview(f, columns=cols, show="headings", height=9, selectmode="browse")
            for c, title, wdt in (("date", "Date", 110), ("num", "Session #", 90), ("kind", "Type", 150),
                                  ("source", "From", 190), ("preview", "Beginning of note", 460)):
                tv.heading(c, text=title, anchor="w")
                tv.column(c, width=px(wdt), minwidth=px(min(wdt, 90)), stretch=(c == "preview"), anchor="w")
            tv.pack(fill="x", pady=(8, 6))

            ed = ttk.LabelFrame(f, text="Selected note", padding=8)
            ed.pack(fill="both", expand=True)
            row = ttk.Frame(ed)
            row.pack(fill="x")
            d_var, n_var, k_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
            ttk.Label(row, text="Date").pack(side="left")
            imp_date = date_field(row, d_var, on_pick=lambda _d: store())
            imp_date.pack(side="left", padx=(6, 16))
            ttk.Label(row, text="Session #").pack(side="left")
            ttk.Entry(row, textvariable=n_var, width=6).pack(side="left", padx=(6, 16))
            ttk.Label(row, text="Type").pack(side="left")
            ttk.Combobox(row, textvariable=k_var, values=["Progress note", "Intake assessment", "Treatment plan"],
                         state="readonly", width=18).pack(side="left", padx=6)
            rm = ttk.Button(row, text="Remove entry")
            rm.pack(side="right")
            tfr, txt = scrolled_text(ed, height=8, doc=True)
            tfr.pack(fill="both", expand=True, pady=(6, 0))
            cur = {"i": None}
            kind_name = {"progress": "Progress note", "intake": "Intake assessment", "plan": "Treatment plan"}
            kind_key = {v: k for k, v in kind_name.items()}

            META = re.compile(r"\b(dob|birth date|date|client|patient name|session (start|end) time|duration|"
                              r"provider\(s\)|provider|insurer\(s\)|insurer|place of service|cpt code|case #|"
                              r"npi|appearance|orientation|behavior|speech|mood|affect|thought|perception|"
                              r"judgment|insight|counseling\s*\d*$)\b\s*[:#]?", re.I)

            def preview(text):
                """First real sentence of the note — skip letterhead, headings, and EHR header/checklist
                lines (session times, provider, insurer, CPT, 'Appearance: WNL', …)."""
                fallback = ""
                for line in text.split("\n"):
                    t = line.strip()
                    if not t or header_kind(t) or (t.isupper() and len(t) > 12) or "SOUTH SHORE" in t.upper():
                        continue
                    if META.match(t) or re.search(r"\b(WNL|N/A)\b", t) and len(t.split()) <= 8:
                        continue
                    if len(t.split()) >= 5 and re.search(r"[a-z]", t):
                        return t[:110]
                    fallback = fallback or t
                return (fallback or re.sub(r"\s+", " ", text))[:110]

            def row_values(it):
                return (fmt_date(it["date"]) or "?", it["number"], kind_name[it["kind"]], it["source"], preview(it["text"]))

            def store():
                i = cur["i"]
                if i is None or i >= len(items):
                    return
                it = items[i]
                it.update(date=d_var.get().strip(), number=n_var.get().strip(),
                          kind=kind_key.get(k_var.get(), "progress"), text=get_text(txt).strip())
                tv.item(str(i), values=row_values(it))

            def refresh(select=None):
                tv.delete(*tv.get_children())
                for i, it in enumerate(items):
                    tv.insert("", "end", iid=str(i), values=row_values(it))
                if items:
                    sel = min(select if select is not None else 0, len(items) - 1)
                    tv.selection_set(str(sel))
                    show(sel)
                else:
                    cur["i"] = None
                    d_var.set("")
                    n_var.set("")
                    k_var.set("")
                    set_text(txt, "")
                count.config(text=f"{len(items)} note(s) ready to import")

            def show(i):
                cur["i"] = i
                it = items[i]
                d_var.set(it["date"])
                n_var.set(it["number"])
                k_var.set(kind_name[it["kind"]])
                set_text(txt, it["text"])

            def on_select(_=None):
                sel = tv.selection()
                if sel and int(sel[0]) != cur["i"]:
                    store()
                    show(int(sel[0]))
            tv.bind("<<TreeviewSelect>>", on_select)

            def add_texts(pairs):
                store()
                for source, text in pairs:
                    for n in split_notes(text, split_var.get()):
                        n["source"] = source
                        items.append(n)
                items.sort(key=lambda it: it["date"] or "9999")
                refresh(len(items) - 1 if items else None)

            def add_files():
                paths = filedialog.askopenfilenames(parent=win, title="Choose past notes", filetypes=[
                    ("Notes", "*.txt *.md *.docx *.pdf"), ("All files", "*.*")])
                pairs, errors = [], []
                for p in paths:
                    try:
                        pairs.append((os.path.basename(p), extract_text(p)))
                    except Exception as e:
                        errors.append(f"{os.path.basename(p)}: {e}")
                if errors:
                    messagebox.showerror(APP_NAME, "Some files couldn't be read:\n\n" + "\n".join(errors), parent=win)
                add_texts(pairs)

            def paste():
                pw = new_window(win, "Paste notes", "720x500")
                pw.grab_set()
                pf = ttk.Frame(pw, padding=(px(20), px(16)))
                pf.pack(fill="both", expand=True)
                ttk.Label(pf, text="Paste one or more notes. Put each note's date on its own line above it "
                                   "if you're pasting several.", style="Muted.TLabel").pack(anchor="w")
                fr, t = scrolled_text(pf, height=16)
                fr.pack(fill="both", expand=True, pady=6)

                def ok():
                    text = get_text(t).strip()
                    pw.destroy()
                    if text:
                        add_texts([("pasted", text)])
                ttk.Button(pf, text="Add", style="Accent.TButton", command=ok).pack(side="right")
                t.focus_set()

            def remove():
                i = cur["i"]
                if i is not None and i < len(items):
                    items.pop(i)
                    cur["i"] = None
                    refresh(i)
            rm.config(command=remove)

            bar.add(ttk.Button(bar, text="Add files…", style="Accent.TButton", command=add_files))
            bar.add(ttk.Button(bar, text="Paste…", command=paste))
            bar.add(ttk.Checkbutton(bar, text="Split files that contain several dated notes",
                                    variable=split_var), gap=px(14))
            bottom = ttk.Frame(f)
            bottom.pack(fill="x", pady=(8, 0), side="bottom", before=ed)   # never squeezed off-screen
            count = ttk.Label(bottom, text="0 note(s) ready to import", style="Muted.TLabel")
            count.pack(side="left")

            def do_import():
                store()
                if not items:
                    return messagebox.showinfo(APP_NAME, "Add some notes first.", parent=win)
                missing = [i for i, it in enumerate(items) if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", it["date"] or "")]
                if missing:
                    tv.selection_set(str(missing[0]))
                    show(missing[0])
                    return messagebox.showinfo(APP_NAME, f"Every note needs a date ({date_hint()}) so the history "
                                                         "stays in order. The first one missing is selected.",
                                               parent=win)
                now = datetime.now().isoformat(timespec="seconds")
                existing_days = {d["meta"].get("session_date") for _, d in vault.sessions_for(client["id"])}
                plans_in = [it for it in items if it["kind"] == "plan"]
                notes_in = [it for it in items if it["kind"] != "plan"]
                joined = sum(1 for it in notes_in if it["date"] in existing_days)
                if plans_in:          # treatment plans join the patient's plan history, in date order
                    c = vault.load_client(client["id"])
                    plans = c.setdefault("treatment_plans", [])
                    for it in plans_in:
                        plans.append({"version": 0, "text": it["text"], "created": it["date"] + "T00:00:00",
                                      "review_days": 90, "based_on": ["imported from TheraNest"],
                                      "imported": True, "tidy_version": TIDY_VERSION})
                    plans.sort(key=lambda x: x.get("created", ""))
                    for i, pl in enumerate(plans, 1):
                        pl["version"] = i
                    vault.save_client(c)
                for it in notes_in:
                    vault.save_session({
                        "meta": {"client_id": client["id"], "client_code": client.get("code", ""),
                                 "session_date": it["date"], "session_number": it["number"],
                                 "kind": it["kind"], "source": "imported", "created": now, "updated": now,
                                 "tidy_version": TIDY_VERSION if it.get("ehr") else 0},
                        "observations": "", "transcript": "", "note": it["text"], "overview": "",
                        "context_used": []})
                n = len(notes_in)
                win.destroy()
                self.open_client(client["id"])
                self.set_status(f"Imported {n} note(s)"
                                + (f" and {len(plans_in)} treatment plan(s)" if plans_in else "")
                                + (f"; {joined} joined a date that already had a session "
                                                              "(shown as an extra tab)" if joined else "")
                                + ". Update the case overview to include them.")

            ttk.Button(bottom, text="Cancel", command=win.destroy).pack(side="right")
            ttk.Button(bottom, text="Import", style="Accent.TButton", command=do_import).pack(side="right", padx=6)
            win._test = {"add_texts": add_texts, "items": items, "do_import": do_import}
            self._import_win = win

        def make_case_overview(self, client):
            sessions = [d for _, d in vault.sessions_for(client["id"])]
            if not sessions:
                return
            job_cfg, orient = dict(cfg), self.settings.get("orientation", "")

            def done(text):
                c = vault.load_client(client["id"])
                c["case_overview"] = {"text": text,
                                      "session_count": len({d["meta"].get("session_date") for d in sessions}),
                                      "generated": datetime.now().isoformat(timespec="seconds")}
                vault.save_client(c)
                self.open_client(client["id"], select="case")
                self.set_status(f"{client.get('code', '')}: case overview updated. Review it before relying on it.")
            if self.run_bg(lambda p: generate_case_overview(job_cfg, client, sessions, orient, p), done,
                           label=f"writing the case overview for {client.get('code', '')}",
                           owner=("case", client["id"]), patient=client.get("code", "")) == "queued" \
                    and self.client_id == client["id"] and not self.wizard:
                self.open_client(client["id"], select="case")

        # ========================================================== WIZARD
        def start_wizard(self, client, sid=None, data=None):
            p = self.parked
            if p is not None and not p.get("discarded"):
                state = ("still transcribing" if p.get("task") == "transcribe" else
                         "still writing notes" if p.get("task") == "generate" else "not saved yet")
                ans = messagebox.askyesnocancel(
                    APP_NAME, f"There's an unfinished session for {p['client'].get('code', '')} ({state}).\n\n"
                              "Yes — go back to it\nNo — discard it and start this one\nCancel — do nothing")
                if ans is None:
                    return
                if ans:
                    return self.resume_parked()
                self.abandon_wizard(p)
                self.parked = None
                self.update_return_button()
            other_ready = any(h.get("can_transcribe") and h.get("accepting") for h in self._rem()["hosts"])
            if not self.setup_ok and sid is None and not engine_installed() and not other_ready:
                if messagebox.askyesno(APP_NAME, "The speech engine isn't installed yet, so recordings "
                                                 "can't be transcribed.\n\nOpen Settings now? (You can also "
                                                 "continue and paste a transcript instead.)"):
                    return self.open_setup()
            sessions = vault.sessions_for(client["id"])
            nums = []
            for _, d in sessions:
                try:
                    nums.append(int(d["meta"].get("session_number") or 0))
                except ValueError:
                    pass
            d = data or {}
            m = d.get("meta", {})
            self.wizard = {
                "client": client, "sid": sid, "step": 3 if d.get("note") else 0,
                "audio": "", "pasted": False,
                "date": m.get("session_date") or date.today().isoformat(),
                "number": m.get("session_number") or str((max(nums) if nums else 0) + 1),
                "observations": d.get("observations", ""), "duration": m.get("duration", 0.0),
                "lines": d.get("lines", []), "roles": dict(d.get("roles", {})),
                "diarized": bool(d.get("diarized")), "aligned": d.get("aligned", True),
                "transcript": d.get("transcript", ""), "note": d.get("note", ""),
                "overview": d.get("overview", ""), "context_used": d.get("context_used", []),
                "created": m.get("created"), "dirty": False, "collect": None,
                "audio_name": m.get("audio_name", ""),
                "kind": m.get("kind") or ("progress" if sessions else "intake"),
                "source": m.get("source", ""),
                "task": None, "ready": None, "error": None, "discarded": False,
            }
            if sid and d.get("transcript") and not d.get("lines"):
                self.wizard["pasted"] = True
            self.goto(self.wizard["step"])

        def goto(self, step):
            w = self.wizard
            if w.get("collect"):
                w["collect"]()
            w["collect"] = None
            w["step"] = step
            self.show(self._wizard_page)

        def _wizard_page(self, f):
            w = self.wizard
            top = ttk.Frame(f)
            top.pack(fill="x")
            title = "Edit session" if w["sid"] else "New session"
            ttk.Label(top, text=f"{title} — {w['client']['code']}", style="Title.TLabel").pack(side="left")
            tb = Flow(top, align="right")
            tb.pack(side="right", fill="x", expand=True, padx=(px(16), 0))
            tb.add(ttk.Button(tb, text="Back to patient", command=lambda: self.leave(
                lambda: self.open_client(w["client"]["id"]))))
            tb.add(ttk.Button(tb, text="Discard session", command=self.discard_wizard))
            bar = Flow(f, gap=px(6), padding=(0, px(14), 0, px(12)))
            bar.pack(fill="x")
            for i, name in enumerate(STEPS):
                st = "StepNow.TLabel" if i == w["step"] else "StepDone.TLabel" if i < w["step"] else "StepTodo.TLabel"
                bar.add(ttk.Label(bar, text=f"{'✓' if i < w['step'] else i + 1}   {name}", style=st))
                if i < len(STEPS) - 1:
                    bar.add(ttk.Label(bar, text="›", style="Muted.TLabel"))
            ttk.Separator(f).pack(fill="x", pady=(0, px(14)))
            content = ttk.Frame(f)
            content.pack(fill="both", expand=True)
            [self._step_recording, self._step_transcribe, self._step_check, self._step_review][w["step"]](content)

        # ---- step 1
        def _step_recording(self, outer):
            w = self.wizard
            nav = ttk.Frame(outer)                     # pinned first, so a short window never hides it
            nav.pack(side="bottom", fill="x", pady=(px(10), 0))
            f = ttk.Frame(outer)
            f.pack(fill="both", expand=True)
            f.columnconfigure(1, weight=1)
            ttk.Label(f, text="Which recording is this session?", style="H2.TLabel").grid(
                row=0, column=0, columnspan=3, sticky="w")
            audio_var = tk.StringVar(value=w["audio"])
            rec_box = ttk.Frame(f)
            rec_box.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 2))
            rec = ttk.Frame(rec_box)
            rec.pack(fill="x")
            ttk.Entry(rec, textvariable=audio_var, state="readonly").pack(side="left", fill="x", expand=True)

            def choose():
                p = filedialog.askopenfilename(title="Choose the session recording", filetypes=[
                    ("Audio", "*.wav *.mp3 *.m4a *.flac *.ogg *.opus *.aac *.wma *.mp4 *.webm"),
                    ("All files", "*.*")])
                if p:
                    audio_var.set(p)
                    w["dirty"] = True
                    show_cloud_warning()
            ttk.Button(rec, text="Choose file…", command=choose).pack(side="left", padx=(6, 0))
            # where to do the work: here, or the other computer (only for a new session)
            on_ids = self.run_on_choices() if not w.get("sid") else {}
            on_var = tk.StringVar()
            unseen = self.not_seen_text() if not w.get("sid") else ""
            if len(on_ids) <= 1 and unseen:
                ttk.Label(rec_box, text="Process on: this computer.  " + unseen, style="Muted.TLabel",
                          wraplength=px(760), justify="left").pack(anchor="w", fill="x", pady=(px(8), 0))
            if len(on_ids) > 1:
                on_row = Flow(rec_box, gap=px(8))
                on_row.pack(fill="x", pady=(px(8), 0))
                on_row.add(ttk.Label(on_row, text="Process on", style="Strong.TLabel"))
                on_cb = on_row.add(ttk.Combobox(on_row, textvariable=on_var, values=list(on_ids),
                                                state="readonly", width=34))
                on_info = on_row.add(ttk.Label(on_row, text="", style="Muted.TLabel", wraplength=px(520),
                                               justify="left"))
                want = w.get("run_on", cfg.get("remote_target") or "")
                on_var.set(next((k for k, v in on_ids.items() if v == want), next(iter(on_ids))))

                def show_on(_=None):
                    tgt = on_ids.get(on_var.get(), "")
                    w["run_on"] = tgt
                    if not tgt:
                        on_info.config(text="transcribed and written here", style="Muted.TLabel")
                        return
                    h = next((h for h in self._rem()["hosts"] if h["id"] == tgt), None)
                    ok = bool(h and h.get("online") and h.get("accepting") and h.get("can_transcribe"))
                    on_info.config(text=(self.remote_ready_text(h) if h else "not seen yet")
                                   + " · sent encrypted through your synced data folder; you'll get a "
                                     "notification when it's ready to review",
                                   style="Good.TLabel" if ok else "Warn.TLabel")
                on_cb.bind("<<ComboboxSelected>>", show_on)
                show_on()
            cloud_warn = ttk.Label(rec_box, text="⚠  This recording is in a cloud-synced folder, so it's being uploaded "
                                           "unencrypted. Keep recordings in a folder on this computer.",
                                   style="Warn.TLabel", wraplength=px(760), justify="left")

            def show_cloud_warning():
                if audio_var.get() and is_cloud_path(audio_var.get()):
                    cloud_warn.pack(anchor="w", pady=(px(4), 0))
                else:
                    cloud_warn.pack_forget()
            show_cloud_warning()
            ttk.Label(f, text="Tip: speaker labels are best with a clean recording and the mic between you "
                              "both. Overlapping speech can't be fully separated.",
                      style="Muted.TLabel", wraplength=px(700), justify="left").grid(
                row=8, column=0, columnspan=3, sticky="ew", pady=(10, 0))

            paste_fr = ttk.Frame(f)
            pfr, paste_txt = scrolled_text(paste_fr, height=7)
            ttk.Label(paste_fr, text="Paste the transcript (timestamps and speaker names help but "
                                     "aren't required):", style="Muted.TLabel").pack(anchor="w")
            pfr.pack(fill="both", expand=True)
            set_text(paste_txt, w["transcript"] if w["pasted"] else "")
            toggle = ttk.Button(f)

            def show_paste(on):
                w["pasted"] = on
                if on:
                    paste_fr.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=(4, 8))
                    toggle.config(text="Use a recording instead")
                else:
                    paste_fr.grid_remove()
                    toggle.config(text="I have a transcript already — paste it instead")
            toggle.config(command=lambda: show_paste(not w["pasted"]))
            toggle.grid(row=2, column=0, columnspan=3, sticky="w", pady=(2, 8))
            show_paste(w["pasted"])

            details = Flow(f, gap=px(10))
            details.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(4, 8))
            date_var, num_var = tk.StringVar(value=w["date"]), tk.StringVar(value=w["number"])
            details.add(ttk.Label(details, text="Session date"))
            details.add(date_field(details, date_var), gap=px(4))
            details.add(ttk.Label(details, text="Session #"), gap=px(18))
            details.add(ttk.Entry(details, textvariable=num_var, width=6), gap=px(4))
            kind_var = tk.StringVar(value=w["kind"])
            details.add(ttk.Label(details, text="Type:"), gap=px(18))
            details.add(ttk.Radiobutton(details, text="Progress note", value="progress", variable=kind_var))
            details.add(ttk.Radiobutton(details, text="Intake (Initial Exam)", value="intake", variable=kind_var))

            ttk.Label(f, text="Anything the recording won't capture? (optional)", style="H2.TLabel").grid(
                row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))
            ttk.Label(f, text="Appearance, affect, body language, MSE observations, things said before "
                              "recording started…", style="Muted.TLabel").grid(row=6, column=0, columnspan=3, sticky="w")
            ofr, obs = scrolled_text(f, height=5)
            ofr.grid(row=7, column=0, columnspan=3, sticky="nsew", pady=(4, 0))
            f.rowconfigure(7, weight=1)
            f.rowconfigure(3, weight=1)                     # the paste box shrinks before the buttons do
            set_text(obs, w["observations"])
            obs.bind("<<Modified>>", lambda e: obs.edit_modified() and w.update(dirty=True))

            def collect():
                w["audio"] = audio_var.get()
                w["date"], w["number"] = date_var.get().strip(), num_var.get().strip()
                w["kind"] = kind_var.get()
                w["observations"] = get_text(obs).strip()
                if w["pasted"]:
                    w["transcript"] = get_text(paste_txt).strip()
            w["collect"] = collect

            def next_step():
                collect()
                if not w["date"]:
                    return messagebox.showinfo(APP_NAME, "Enter the session date.")
                if w["pasted"]:
                    if not w["transcript"]:
                        return messagebox.showinfo(APP_NAME, "Paste the transcript first.")
                    w["dirty"] = True
                    return self.goto(2)
                if not w["audio"] or not os.path.isfile(w["audio"]):
                    return messagebox.showinfo(APP_NAME, "Choose the session recording first.")
                target = on_ids.get(on_var.get(), "") if on_ids else ""
                if target:
                    return self.wizard_send_remote(w, target)
                if not engine_installed():
                    if messagebox.askyesno(APP_NAME, "The speech engine (WhisperX) isn't installed for "
                                                     "this copy of Python yet.\n\nOpen Settings to install it?"):
                        self.open_setup()
                    return
                w["transcript"], w["note"], w["overview"] = "", "", ""
                self.goto(1)
                self.start_transcription()

            ttk.Button(nav, text="Next ▶", style="Accent.TButton", command=next_step).pack(side="right")

        def wizard_send_remote(self, w, target):
            """Hand this session's recording to the other computer (transcription + notes there).
            It comes back as a draft to review, with a notification — like the recording queue."""
            Q = self._q()
            Q["seq"] += 1
            it = {"seq": Q["seq"], "path": w["audio"], "audio_name": os.path.basename(w["audio"]),
                  "client_id": w["client"]["id"], "date": w["date"], "number": w["number"],
                  "kind": w["kind"], "observations": w.get("observations", ""), "status": "waiting", "error": "",
                  "number_manual": bool(str(w["number"]).strip()), "kind_manual": True}
            Q["items"].append(it)
            if not it["number_manual"]:
                self.queue_renumber()
            host = next((h for h in self._rem()["hosts"] if h["id"] == target), None)
            if not self.remote_start([it], target, host, root, write=True):
                Q["items"].remove(it)
                return
            cfg["remote_target"] = target
            save_config(cfg)
            name = it.get("remote_name") or self._host_name(target)
            code, cid = w["client"].get("code", ""), w["client"]["id"]
            w["dirty"] = False
            self.abandon_wizard(w)
            if self.parked is w:
                self.parked = None
            self.wizard = None
            self.update_return_button()
            self.queue_refresh()
            self.open_client(cid)
            self.set_status(f"{code}: recording sent to {name}. You'll get a notification when the draft is ready "
                            "to review — Queue recordings shows its progress.")

        # ---- step 2
        def _step_transcribe(self, f):
            w = self.wizard
            ttk.Label(f, text="Transcribing on this computer…", style="H2.TLabel").pack(anchor="w")
            ttk.Label(f, text=os.path.basename(w["audio"]), style="Muted.TLabel").pack(anchor="w", pady=(2, 14))
            self.progress_panel(f, "Starting…", length=520, always=not w.get("error"),
                                owner=("wizard", id(w))).pack(anchor="w", fill="x", pady=(0, px(10)))
            ttk.Label(f, text="You can switch to other patients and notes while this runs. When it's done, "
                              "a button at the bottom of the window brings you back here.\nNothing is sent anywhere — "
                              "the audio is processed by WhisperX on this computer.", style="Muted.TLabel",
                      justify="left").pack(anchor="w", pady=(10, 0))
            self.err_box = ttk.Frame(f)
            self.err_box.pack(fill="x", pady=16)
            if w.get("error"):
                self.show_transcribe_error(w, w["error"])

        def show_transcribe_error(self, w, msg):
            for c in self.err_box.winfo_children():
                c.destroy()
            ttk.Label(self.err_box, text="Transcription didn't finish", style="BadStrong.TLabel").pack(anchor="w")
            ttk.Label(self.err_box, text=msg, wraplength=px(700), justify="left").pack(anchor="w", pady=(4, 8))
            row = ttk.Frame(self.err_box)
            row.pack(anchor="w")
            ttk.Button(row, text="◀ Back", command=lambda: (w.update(error=None), self.goto(0))).pack(side="left")
            ttk.Button(row, text="Open Settings", command=self.open_setup).pack(side="left", padx=6)
            if self._alive(self.progress_label):
                self.progress_label.config(text="")
            if self._alive(self.page_bar):
                self.page_bar.stop()
                self.page_bar.config(mode="determinate")
                self.page_bar["value"] = 0

        def start_transcription(self):
            w = self.wizard
            job_cfg, token, path = dict(cfg), self.settings.get("hf_token", ""), w["audio"]

            def job(progress):
                segs, dur, diar, aligned = transcribe(path, job_cfg, token, progress)
                turns = words_to_turns(segs, smooth=bool(job_cfg.get("smooth_flicker", True)))
                return turns, dur, diar, aligned

            code = w["client"].get("code", "")

            def done(res):
                w["task"] = None
                if w.get("discarded"):
                    return self.set_status("Finished transcribing a discarded session — result thrown away.")
                lines, dur, diar, aligned = res
                if not lines:
                    return failed("No speech was found in that recording.")
                w.update(lines=lines, duration=dur, diarized=diar, aligned=aligned,
                         roles=guess_roles(lines, role_labels(cfg)), dirty=True, error=None)
                w["transcript"] = render_transcript(lines, w["roles"])
                w["step"] = 2
                if self.wizard_on_screen(w):
                    self.goto(2)
                    self.set_status(f"Transcribed {fmt_ts(dur)}. Check it over, then write the notes.")
                else:
                    w["ready"] = "transcript"
                    self.set_status(f"Transcription finished ({fmt_ts(dur)}).")
                    root.bell()
                self.update_return_button()

            def failed(msg):
                w["task"] = None
                if w.get("discarded"):
                    return
                w["error"] = msg
                if self.wizard_on_screen(w) and w["step"] == 1:
                    self.show_transcribe_error(w, msg)
                else:
                    self.set_status(f"{code}: transcription didn't finish — open the session to see why.")
                self.update_return_button()
            if self.run_bg(job, done, failed, label=f"transcribing {code}", owner=("wizard", id(w)), patient=code):
                w["task"], w["error"] = "transcribe", None
            else:
                w["step"] = 0
                self.goto(0)

        # ---- step 3
        def _step_check(self, f):
            w = self.wizard
            head = ttk.Frame(f)
            head.pack(fill="x")
            ttk.Label(head, text="Check the transcript", style="H2.TLabel").pack(side="left")
            ttk.Label(f, text="Fix misheard words if needed. Names from the patient profile are hidden "
                              "from the AI automatically.", style="Muted.TLabel", wraplength=px(700),
                      justify="left").pack(anchor="w", pady=(2, 8))
            fr, txt = scrolled_text(f, height=16, doc=True)
            find_button(head, txt, "🔍 Find (⌘F)" if IS_MAC else "🔍 Find (Ctrl+F)").pack(side="right")
            nav = ttk.Frame(f)                      # reserved first: always visible, whatever the size
            nav.pack(side="bottom", fill="x", pady=(10, 0))
            if w["lines"] and w["diarized"]:
                spk = Flow(f, gap=px(14))
                spk.pack(fill="x", pady=(0, 8))
                spk.add(ttk.Label(spk, text="Who's who?", style="H2.TLabel"))
                for s in dict.fromkeys(l["speaker"] for l in w["lines"]):
                    sample = next((l["text"] for l in w["lines"] if l["speaker"] == s and len(l["text"]) > 25), "")[:50]
                    box = spk.add(ttk.Frame(spk))
                    labels = role_labels(cfg)
                    var = tk.StringVar(value=w["roles"].get(s, labels[-1]))
                    shown = labels + [r for r in dict.fromkeys(w["roles"].values()) if r not in labels]
                    cb = ttk.Combobox(box, textvariable=var, values=shown, width=15, state="readonly")
                    cb.pack(anchor="w")
                    ttk.Label(box, text=f"“{sample}…”", style="Muted.TLabel").pack(anchor="w")

                    def changed(_e, s=s, var=var):
                        if get_text(txt).strip() != render_transcript(w["lines"], w["roles"]).strip() and \
                                not messagebox.askyesno(APP_NAME, "Changing labels rebuilds the transcript and "
                                                                  "undoes your edits to it. Continue?"):
                            return var.set(w["roles"][s])
                        w["roles"][s] = var.get()
                        set_text(txt, render_transcript(w["lines"], w["roles"]))
                    cb.bind("<<ComboboxSelected>>", changed)
                if not w.get("aligned", True):
                    ttk.Label(f, text="Word alignment didn't run, so speaker labels come from whole "
                                      "segments and some lines may mix both speakers. Check carefully.",
                              style="Warn.TLabel", wraplength=px(800)).pack(anchor="w", pady=(0, 8))
            elif w["lines"]:
                ttk.Label(f, text="Speaker labels are off — add a Hugging Face token in Settings to have "
                                  "the clinician and patient labeled automatically.",
                          style="Warn.TLabel", wraplength=px(700), justify="left").pack(anchor="w", pady=(0, 8))
            fr.pack(fill="both", expand=True)
            set_text(txt, w["transcript"])
            txt.bind("<<Modified>>", lambda e: txt.edit_modified() and w.update(dirty=True))

            def collect():
                w["transcript"] = get_text(txt).strip()
            w["collect"] = collect

            def next_step():
                collect()
                if not w["transcript"]:
                    return messagebox.showinfo(APP_NAME, "The transcript is empty.")
                generate = not w["note"] or messagebox.askyesno(
                    APP_NAME, "Rewrite the note and overview from this transcript?\n\n"
                              "(No keeps the current drafts.)")
                self.goto(3)
                if generate:
                    self.start_generation()

            ttk.Button(nav, text="◀ Back", command=lambda: self.goto(0)).pack(side="left")
            ttk.Button(nav, text="Write notes ▶", style="Accent.TButton", command=next_step).pack(side="right")

        # ---- step 4
        def _step_review(self, f):
            w = self.wizard
            head = ttk.Frame(f)
            head.pack(fill="x")
            ttk.Label(head, text="Review the drafts", style="H2.TLabel").pack(side="left")
            self.progress_panel(f, owner=("wizard", id(w))).pack(anchor="w", fill="x", pady=(px(2), px(4)))
            ctx = ("Built on: " + ", ".join(w["context_used"])) if w["context_used"] else \
                ("First session for this patient — no earlier notes to build on." if w["note"] else "")
            ttk.Label(f, text=ctx, style="Muted.TLabel", wraplength=px(700), justify="left").pack(anchor="w")
            ttk.Label(f, text="Edit freely. Search for [VERIFY] and check everything against your own "
                              "memory of the session before saving.", style="Warn.TLabel", wraplength=px(700),
                      justify="left").pack(anchor="w", pady=(2, 6))
            nb = ttk.Notebook(f)
            nb.pack(fill="both", expand=True)
            nfr, note = scrolled_text(nb, doc=True)
            vfr, ov = scrolled_text(nb, doc=True)
            labels = KIND_LABELS[w["kind"]]
            nb.add(nfr, text=f"  {labels[0]}  ")
            nb.add(vfr, text=f"  {labels[1]}  ")
            set_text(note, w["note"])
            set_text(ov, w["overview"])

            def current_text():
                return note if nb.index(nb.select()) == 0 else ov
            ttk.Button(head, text="Find [VERIFY]", command=lambda: current_text().finder.show("[VERIFY")).pack(side="right")
            ttk.Button(head, text="🔍 Find (⌘F)" if IS_MAC else "🔍 Find (Ctrl+F)", command=lambda: current_text().finder.show()).pack(
                side="right", padx=px(6))
            for t in (note, ov):
                t.bind("<<Modified>>", lambda e: e.widget.edit_modified() and w.update(dirty=True))

            def collect():
                w["note"], w["overview"] = get_text(note).strip(), get_text(ov).strip()
            w["collect"] = collect

            nav = Flow(f)                          # reserved at the bottom: never pushed off-screen
            nav.pack(side="bottom", fill="x", pady=(10, 0), before=nb)
            nav.add(ttk.Button(nav, text="◀ Back", command=lambda: self.goto(2)))
            rw = nav.add(ttk.Button(nav, text="✎  Rewrite with instructions…",
                                    command=lambda: (collect(), self.rewrite_dialog())))
            if not (w["transcript"].strip() or w["note"].strip() or w["overview"].strip()):
                rw.config(state="disabled")
            if w.get("undo"):
                def undo():
                    collect()
                    w["note"], w["overview"] = w["undo"]
                    w["undo"] = None
                    w["collect"] = None
                    self.goto(3)
                    self.set_status("Rewrite undone — the previous drafts are back.")
                nav.add(ttk.Button(nav, text="↶  Undo rewrite", command=undo))
            nav.add(ttk.Button(nav, text=f"Copy {labels[0].lower()}",
                               command=lambda: self.copy(get_text(note), labels[0])), gap=px(24))
            if w["kind"] != "intake":
                nav.add(self.note_copy_menu(nav, lambda: get_text(note)))
            nav.add(ttk.Button(nav, text=f"Copy {labels[1].lower()}",
                               command=lambda: self.copy(get_text(ov), labels[1])))
            nav.add(ttk.Button(nav, text="Save session ✓", style="Accent.TButton", command=self.save_wizard),
                    gap=px(24))
            if self.page_owns_task():          # drafts are being (re)written right now
                note.config(state="disabled")
                ov.config(state="disabled")

        def _gen_inputs(self, client, exclude_sid, day, number, observations):
            """Everything the AI needs to write one session's documents (names already hidden):
            earlier notes, same-date documents, goals, plan, orientation. Shared by the
            session wizard and the recording queue."""
            code = client.get("code") or "the patient"
            hide = client_hider(client)
            others = [(sid, d) for sid, d in vault.sessions_for(client["id"]) if sid != exclude_sid]
            prior = [d for _, d in others if (d["meta"].get("session_date") or "") < (day or "")]
            same_day = [d for _, d in others if d["meta"].get("session_date") == day]
            context, used = build_context(client, prior, int(cfg.get("previous_notes", 2)), hide, same_day)
            values = {"client_code": code, "session_date": fmt_date(day) or "[VERIFY: date]",
                      "session_number": number or "[VERIFY]",
                      "orientation": self.settings.get("orientation") or "not specified",
                      "goals": hide(client.get("goals", "")).strip()
                      or hide("\n".join(plan_goals((current_plan(client) or {}).get("text", "")))).strip()
                      or "(not provided)",
                      "observations": hide(observations) or "(not provided)",
                      "background": hide(client.get("background", "")).strip() or "(not provided)"}
            return code, hide, context, used, values

        def start_generation(self, instructions="", which=("first", "second")):
            w = self.wizard
            client = vault.load_client(w["client"]["id"])
            w["client"] = client
            code, hide, context, used, values = self._gen_inputs(client, w["sid"], w["date"], w["number"],
                                                                 w["observations"])
            transcript, job_cfg = hide(w["transcript"]), dict(cfg)
            instructions = hide(instructions)
            had_drafts = bool(w["note"] or w["overview"])
            kind, reference = w["kind"], self.settings.get("reference") or BUILTIN_REFERENCE

            def done(res):
                w["task"] = None
                if w.get("discarded"):
                    return self.set_status("Finished writing for a discarded session — result thrown away.")
                if had_drafts:
                    w["undo"] = (w["note"], w["overview"])
                if res[0] is not None:
                    w["note"] = res[0]
                if res[1] is not None:
                    w["overview"] = res[1]
                w["context_used"] = used
                w["dirty"] = True
                w["step"] = 3
                if self.wizard_on_screen(w):
                    w["collect"] = None
                    self.goto(3)
                    self.set_status("Drafts ready — review them, then save.")
                else:
                    w["ready"] = "drafts"
                    self.set_status("Drafts finished.")
                    root.bell()
                self.update_return_button()

            def failed(msg):
                w["task"] = None
                if w.get("discarded"):
                    return
                if self.wizard_on_screen(w):
                    self.goto(3)
                    messagebox.showerror(APP_NAME, msg)
                else:
                    w["error"] = msg
                    self.set_status(f"{code}: writing the notes didn't finish — open the session to see why.")
                self.update_return_button()
            if self.run_bg(lambda p: generate_documents(job_cfg, values, transcript, context, p,
                                                        kind=kind, reference=reference,
                                                        instructions=instructions, which=which), done, failed,
                           label=f"writing notes for {code}", owner=("wizard", id(w)), patient=code):
                w["task"], w["error"] = "generate", None
                w["collect"] = None
                self.show(self._wizard_page)
                if not self.waiting_text(("wizard", id(w))):          # (if it's waiting in line, the bar says so)
                    self.set_status("Writing drafts with the local AI… You can switch to other patients meanwhile.")

        def rewrite_dialog(self):
            """Rewrite the progress note / overview (or intake documents) following the
            clinician's instructions: either a targeted edit or a full rewrite."""
            w = self.wizard
            labels = KIND_LABELS[w["kind"]]
            win = new_window(root, "Rewrite drafts", "680x600")
            win.grab_set()
            f = ttk.Frame(win, padding=(px(24), px(20)))
            f.pack(fill="both", expand=True)
            ttk.Label(f, text="Rewrite with instructions", style="H2.TLabel").pack(anchor="w")
            ttk.Label(f, text="Which draft?", style="Strong.TLabel").pack(anchor="w", pady=(px(12), px(2)))
            which_var = tk.StringVar(value="first")
            row = ttk.Frame(f)
            row.pack(anchor="w")
            for val, txt in (("first", labels[0]), ("second", labels[1]), ("both", "Both")):
                ttk.Radiobutton(row, text=txt, value=val, variable=which_var).pack(side="left", padx=(0, px(14)))
            ttk.Label(f, text="What should change?", style="Strong.TLabel").pack(anchor="w", pady=(px(14), px(2)))
            ttk.Label(f, text="e.g. “Make it shorter, about 120 words”, “Describe interventions in psychodynamic terms”, "
                              "“Add that the patient arrived 10 minutes late”, “Remove the direct quote”, "
                              "“Mention the homework from last session”.",
                      style="Caption.TLabel", wraplength=px(600), justify="left").pack(anchor="w")
            ifr, instr = scrolled_text(f, height=6)
            ifr.pack(fill="both", expand=True, pady=(px(4), 0))
            ttk.Label(f, text="How?", style="Strong.TLabel").pack(anchor="w", pady=(px(12), px(2)))
            mode_var = tk.StringVar(value="edit")
            ttk.Radiobutton(f, text="Edit only what I asked; keep the rest of the draft (including my own edits)",
                            value="edit", variable=mode_var).pack(anchor="w")
            full = ttk.Radiobutton(f, text="Rewrite from the transcript, following my instructions",
                                   value="full", variable=mode_var)
            full.pack(anchor="w")
            if not w["transcript"].strip():
                full.config(state="disabled")
            err = ttk.Label(f, text="", style="Bad.TLabel")
            err.pack(anchor="w")
            btns = pin_bottom(ttk.Frame(f), pady=(px(12), 0))

            def go():
                text = get_text(instr).strip()
                which = ("first", "second") if which_var.get() == "both" else (which_var.get(),)
                if not text:
                    return err.config(text="Describe what should change.")
                if mode_var.get() == "edit":
                    drafts = {"first": w["note"], "second": w["overview"]}
                    if not any(drafts[k].strip() for k in which):
                        return err.config(text="That draft is empty — use “Rewrite from the transcript”.")
                    win.destroy()
                    self.revise_drafts(text, which)
                else:
                    win.destroy()
                    self.start_generation(instructions=text, which=which)
            ttk.Button(btns, text="Rewrite", style="Accent.TButton", command=go).pack(side="right")
            ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right", padx=px(8))
            instr.focus_set()
            win._go, win._instr, win._which, win._mode = go, instr, which_var, mode_var
            self._rewrite_win = win

        def revise_drafts(self, instructions, which):
            """Targeted edit of the current draft(s) — keeps everything not asked about."""
            w = self.wizard
            client = vault.load_client(w["client"]["id"])
            hide = client_hider(client)
            labels = KIND_LABELS[w["kind"]]
            jobs = [(k, labels[0] if k == "first" else labels[1],
                     hide(w["note"] if k == "first" else w["overview"])) for k in which
                    if (w["note"] if k == "first" else w["overview"]).strip()]
            source, job_cfg, instr = hide(w["transcript"]), dict(cfg), hide(instructions)
            code = client.get("code", "")

            def job(progress):
                steps = _Steps(progress, [(f"Revising the {lbl.lower()}", max(800, len(d))) for _, lbl, d in jobs])
                out = {}
                for i, (k, lbl, draft) in enumerate(jobs):
                    out[k] = steps.run(i, job_cfg, fill(REVISE_PROMPT, {
                        "doc_label": lbl.lower(), "instructions": instr, "draft": draft,
                        "source": (source or "(no transcript — rely on the draft)")[:int(job_cfg.get("max_chars", 24000))]}))
                progress("Done.", 100)
                return out

            def done(out):
                w["task"] = None
                if w.get("discarded"):
                    return
                w["undo"] = (w["note"], w["overview"])
                w["note"] = out.get("first", w["note"])
                w["overview"] = out.get("second", w["overview"])
                w["dirty"] = True
                if self.wizard_on_screen(w):
                    w["collect"] = None
                    self.goto(3)
                    self.set_status("Revised. Check the changes — “Undo rewrite” brings back the previous version.")
                else:
                    w["ready"] = "drafts"
                    self.set_status("Revision finished.")
                    root.bell()
                self.update_return_button()

            def failed(msg):
                w["task"] = None
                if self.wizard_on_screen(w):
                    self.goto(3)
                    messagebox.showerror(APP_NAME, msg)
                else:
                    w["error"] = msg
                self.update_return_button()
            if self.run_bg(job, done, failed, label=f"revising notes for {code}", owner=("wizard", id(w)), patient=code):
                w["task"], w["error"] = "generate", None
                w["collect"] = None
                self.show(self._wizard_page)
                if not self.waiting_text(("wizard", id(w))):
                    self.set_status("Revising the draft with the local AI…")

        def save_wizard(self):
            w = self.wizard
            if w.get("collect"):
                w["collect"]()
            if not (w["note"] or w["transcript"]):
                return messagebox.showinfo(APP_NAME, "There's nothing to save yet.")
            client = w["client"]
            shift = {}
            n_new = _num(w["number"])
            if n_new is not None:
                others = [(sid, d) for sid, d in vault.sessions_for(client["id"]) if sid != w["sid"]]
                nums = day_numbers(others)
                clash = sorted(d_ for d_, n in nums.items() if n == n_new and d_ != w["date"])
                if clash:
                    ans = messagebox.askyesnocancel(
                        APP_NAME, f"Session #{n_new} is already used on {', '.join(clash)}.\n\n"
                                  f"Yes — renumber that session and every later one up by 1 (e.g. #{n_new} → "
                                  f"#{n_new + 1}).\nNo — save with a duplicate number anyway (shown as an error "
                                  "until fixed).\nCancel — go back and change the number.")
                    if ans is None:
                        return
                    if ans:
                        shift = {d_: n + 1 for d_, n in nums.items() if n >= n_new and d_ != w["date"]}
            data = {"meta": {"client_id": client["id"], "client_code": client.get("code", ""),
                             "session_date": w["date"], "session_number": w["number"],
                             "duration": w["duration"],
                             "audio_name": os.path.basename(w["audio"]) if w["audio"] else w.get("audio_name", ""),
                             "created": w["created"] or datetime.now().isoformat(timespec="seconds"),
                             "updated": datetime.now().isoformat(timespec="seconds"),
                             "kind": w["kind"], "source": w.get("source", "")},
                    "observations": w["observations"], "transcript": w["transcript"],
                    "note": w["note"], "overview": w["overview"], "context_used": w["context_used"]}
            if w.get("lines"):
                data.update(lines=w["lines"], roles=w["roles"], diarized=w["diarized"], aligned=w["aligned"])
            try:
                sid = vault.save_session(data, w["sid"])
                for osid, od in (vault.sessions_for(client["id"]) if shift else []):
                    d_ = od["meta"].get("session_date")
                    if osid != sid and d_ in shift and _num(od["meta"].get("session_number")) is not None:
                        od["meta"]["session_number"] = str(shift[d_])
                        vault.save_session(od, osid)
            except Exception as e:
                return messagebox.showerror(APP_NAME, f"Couldn't save: {e}")
            w["dirty"] = False
            msg = "Session saved to the encrypted vault."
            audio = w["audio"]
            if cfg.get("delete_audio") and audio and os.path.isfile(audio):
                if messagebox.askyesno(APP_NAME, f"Saved. Permanently delete the original recording?\n\n"
                                                 f"{audio}\n\nIt will be overwritten and removed. This "
                                                 "can't be undone."):
                    try:
                        wipe_file(audio)
                        msg += " Recording deleted."
                    except OSError as e:
                        messagebox.showerror(APP_NAME, f"Couldn't delete the recording: {e}")
                else:
                    msg += " Recording kept."
            self.wizard = None
            if self.parked is w:
                self.parked = None
            self.update_return_button()
            self.refresh_clients(client["id"])       # clears "⚠ to review" once a draft is saved
            self.open_client(client["id"], select=sid)
            self.set_status(msg)

        # =========================================================== QUEUE
        # Several recordings, labelled up front, processed one after another while you're away:
        # transcribe → label speakers (best guess) → write the notes → save, marked "needs review".
        QUEUE_KIND = {"Progress note": "progress", "Intake": "intake"}

        def _q(self):
            if not hasattr(self, "queue"):
                self.queue = {"items": [], "running": False, "stop": False, "write": True, "seq": 0}
            return self.queue

        def queue_counts(self):
            items = self._q()["items"]
            done = sum(1 for it in items if it["status"] in ("done", "transcribed", "failed"))
            return done, len(items)

        def _client_by_id(self, cid):
            return next((c for c in self.all_clients if c["id"] == cid), None) if cid else None

        def queue_guess(self, path, default_cid=None):
            """Prefill one recording: patient from a code in the file name (else the patient
            that's open), date from the file name (else when the file was recorded/saved)."""
            name = os.path.basename(path)
            flat = re.sub(r"[^a-z0-9]", "", name.lower())
            cid = default_cid
            for c in self.all_clients:
                code = re.sub(r"[^a-z0-9]", "", c.get("code", "").lower())
                if len(code) >= 3 and code in flat:
                    cid = c["id"]
                    break
            day = find_date(re.sub(r"[_]", " ", os.path.splitext(name)[0]))
            if not day:
                try:
                    day = datetime.fromtimestamp(os.path.getmtime(path)).date().isoformat()
                except OSError:
                    day = date.today().isoformat()
            return cid, day

        def queue_renumber(self):
            """Session numbers continue each patient's count, in date order, unless typed by hand.
            A patient's first-ever session defaults to the intake."""
            items = self._q()["items"]
            by_patient = {}
            for it in items:
                if it["client_id"] and it["status"] in ("waiting", "failed"):
                    by_patient.setdefault(it["client_id"], []).append(it)
            for cid, its in by_patient.items():
                existing = vault.sessions_for(cid)
                nums = []
                for _, d in existing:
                    try:
                        nums.append(int(d["meta"].get("session_number") or 0))
                    except ValueError:
                        pass
                n = max(nums) if nums else 0
                for i, it in enumerate(sorted(its, key=lambda x: (x["date"], x["seq"]))):
                    if not it.get("kind_manual"):
                        it["kind"] = "intake" if (not existing and i == 0) else "progress"
                    if it.get("number_manual"):
                        try:
                            n = max(n, int(it["number"]))
                        except ValueError:
                            pass
                    else:
                        n += 1
                        it["number"] = str(n)

        def open_queue(self, default_cid=None):
            if self.locked:
                return
            if getattr(self, "queue_win", None) is not None and self._alive(self.queue_win["win"]):
                win = self.queue_win["win"]
                win.deiconify()
                win.lift()
                return
            Q = self._q()
            win = new_window(root, "Recording queue", "1080x700")
            win.minsize(min(px(720), root.winfo_screenwidth()), min(px(600), int(root.winfo_screenheight() * 0.88)))
            f = ttk.Frame(win, padding=(px(24), px(18)))
            f.pack(fill="both", expand=True)
            ttk.Label(f, text="Recording queue", style="Title.TLabel").pack(anchor="w")
            ttk.Label(f, text="Add several recordings, check the patient, date, type and session # for each, then "
                              "press Start and leave the computer. Each one is transcribed and its notes are written, "
                              "oldest first so every note builds on the ones before it. Results are saved as drafts "
                              "marked “needs review” — speaker labels are a best guess, so check them before anything "
                              "goes in TheraNest.", style="Muted.TLabel", wraplength=px(1000),
                      justify="left").pack(anchor="w", pady=(px(2), px(10)))
            bar = ttk.Frame(f)
            bar.pack(fill="x")
            cols = ("file", "patient", "date", "num", "kind", "status")
            tv_box, tv = tree_box(f, columns=cols, show="headings", height=4, selectmode="extended")
            for c, title, wdt in (("file", "Recording", 300), ("patient", "Patient", 110), ("date", "Date", 120),
                                  ("num", "Session #", 90), ("kind", "Type", 130), ("status", "Status", 260)):
                tv.heading(c, text=title, anchor="w")
                tv.column(c, width=px(wdt), minwidth=px(70), stretch=(c in ("file", "status")), anchor="w")
            tv_box.pack(fill="both", expand=True, pady=(px(8), px(8)))

            ed = ttk.LabelFrame(f, text="Selected recording(s)", padding=px(10))
            pin_bottom(ed)
            codes = {c.get("code", "?"): c["id"] for c in self.all_clients if not c.get("archived")}
            p_var, d_var, n_var, k_var = tk.StringVar(), tk.StringVar(), tk.StringVar(), tk.StringVar()
            fields = Flow(ed, gap=px(18))
            fields.grid(row=0, column=0, columnspan=8, sticky="ew")

            def field(label, make):
                pair = ttk.Frame(fields)
                ttk.Label(pair, text=label).pack(side="left", padx=(0, px(6)))
                w = make(pair)
                w.pack(side="left")
                fields.add(pair)
                return w
            p_cb = field("Patient", lambda pr: ttk.Combobox(pr, textvariable=p_var, values=sorted(codes),
                                                            state="readonly", width=14))
            d_fld = field("Date", lambda pr: date_field(pr, d_var, on_pick=lambda _d: apply()))
            d_en = d_fld.entry
            n_en = field("Session #", lambda pr: ttk.Entry(pr, textvariable=n_var, width=6))
            k_cb = field("Type", lambda pr: ttk.Combobox(pr, textvariable=k_var, values=list(self.QUEUE_KIND),
                                                         state="readonly", width=14))
            ttk.Label(ed, text="Notes the recording won't capture (optional)").grid(
                row=1, column=0, columnspan=8, sticky="w", pady=(px(8), 0))
            o_var = tk.StringVar()
            o_en = ttk.Entry(ed, textvariable=o_var)
            o_en.grid(row=2, column=0, columnspan=8, sticky="ew", pady=(px(2), 0))
            ed.columnconfigure(7, weight=1)
            hint = ttk.Label(ed, text="", style="Muted.TLabel", wraplength=px(980), justify="left")
            hint.grid(row=3, column=0, columnspan=8, sticky="w", pady=(px(4), 0))

            pin_bottom(ttk.Separator(f), pady=(px(10), 0))
            bottom = ttk.Frame(f)
            pin_bottom(bottom, pady=(px(10), 0))
            opts_row = ttk.Frame(bottom)                  # line 1: option + summary
            opts_row.pack(fill="x")
            acts = ttk.Frame(bottom)                      # line 2: the buttons (always their own line)
            acts.pack(fill="x", pady=(px(8), 0))
            write_var = tk.BooleanVar(value=Q["write"])
            run_row = ttk.Frame(bottom)                   # where to run: this computer or the linked one
            run_row.pack(fill="x", pady=(0, px(8)), before=opts_row)
            ttk.Label(run_row, text="Run on").pack(side="left")
            run_var = tk.StringVar()
            run_cb = ttk.Combobox(run_row, textvariable=run_var, state="readonly", width=34)
            run_cb.pack(side="left", padx=(px(8), px(10)))
            run_info = ttk.Label(run_row, text="", style="Muted.TLabel", wraplength=px(520), justify="left")
            run_info.pack(side="left", fill="x", expand=True)
            run_ids = {}

            def fill_hosts():
                R = self._rem()
                run_ids.clear()
                run_ids.update(self.run_on_choices())
                tgt = cfg.get("remote_target") or ""
                run_cb.config(values=list(run_ids))
                run_var.set(next((k for k, v in run_ids.items() if v == tgt), next(iter(run_ids))))
                h = next((h for h in R["hosts"] if h["id"] == tgt), None)
                if not tgt:
                    run_info.config(text=("Your other computer can do the work instead — choose it here."
                                          if R["hosts"] else self.not_seen_text()), style="Muted.TLabel")
                else:
                    ok = bool(h and h.get("online") and h.get("accepting") and h.get("can_transcribe"))
                    run_info.config(text=(self.remote_ready_text(h) if h else "not seen yet")
                                    + " · sent encrypted through your synced data folder; you'll be notified here "
                                      "as each one is done.", style="Good.TLabel" if ok else "Warn.TLabel")
                start_btn.config(text=("▶  Send to " + self._host_name(tgt)) if tgt else "▶  Start queue")

            def pick_host(_=None):
                cfg["remote_target"] = run_ids.get(run_var.get(), "")
                save_config(cfg)
                fill_hosts()
            run_cb.bind("<<ComboboxSelected>>", pick_host)
            ttk.Checkbutton(opts_row, text="Also write the notes (not just transcripts)", variable=write_var,
                            command=lambda: Q.update(write=write_var.get())).pack(side="left")
            summary = ttk.Label(opts_row, text="", style="Muted.TLabel", wraplength=px(400), justify="left")
            summary.pack(side="left", fill="x", expand=True, padx=px(16))
            ttk.Button(acts, text="Close", command=win.withdraw).pack(side="right")
            stop_btn = ttk.Button(acts, text="Stop after this one", command=lambda: self.queue_stop())
            halt_btn = ttk.Button(acts, text="■  Stop now", command=lambda: self.queue_halt())
            start_btn = ttk.Button(acts, text="▶  Start queue", style="Accent.TButton",
                                   command=lambda: self.queue_start())
            start_btn.pack(side="right", padx=px(8))

            loading = {"on": False}
            STATUS = {"waiting": "Waiting", "transcribing": "Transcribing…", "writing": "Writing notes…",
                      "done": "✓ Saved — needs review", "failed": "✕ ", "transcribed": "✓ Saved (transcript only)",
                      "sending": "Sending to {h}…", "sent": "Waiting for {h}", "rqueued": "Received on {h} — queued…",
                      "rtranscribing": "Transcribing on {h}…",
                      "rwriting": "Writing notes on {h}…", "syncing": "Done on {h} — syncing here…"}

            def row(it):
                c = self._client_by_id(it["client_id"])
                st = STATUS.get(it["status"], it["status"]).replace("{h}", it.get("remote_name") or "other computer")
                if it["status"] in ("rtranscribing", "rwriting"):
                    if isinstance(it.get("rpct"), (int, float)):
                        st = st.rstrip("…") + f" {it['rpct']:.0f}%"
                        if it.get("rtext"):
                            st += f" — {it['rtext'].rstrip('…')}"
                    st += self._remote_health(it)
                elif it["status"] == "rqueued":
                    st += {"syncing": " recording still arriving", "profile": " waiting for patient profile"}.get(
                        it.get("rreason"), "")
                if it.get("remote_to") and it["status"] in ("sent", "rqueued", "rtranscribing", "rwriting"):
                    h = next((h for h in self._rem()["hosts"] if h["id"] == it["remote_to"]), None)
                    if h is not None and not h.get("online"):
                        st += " (it's offline — continues when Session Scribe opens there)"
                if it["status"] == "failed":
                    first = next((l for l in (it.get("error") or "failed").split("\n") if l.strip()), "failed")
                    st = "✕ Failed: " + re.sub(r"^\w+Error:\s*", "", first)[:90]
                elif it["status"] == "done" and it.get("notes_error"):
                    st = "✓ Saved transcript — notes failed"
                name = it.get("audio_name") or os.path.basename(it["path"])
                if it.get("remote_from"):
                    name = f"From {it['remote_from']}: {name}"
                return (name, c.get("code", "?") if c else "— choose —", fmt_date(it["date"]),
                        it["number"], "Intake" if it["kind"] == "intake" else "Progress note", st)

            def selected():
                ids = set(tv.selection())
                return [it for it in Q["items"] if str(it["seq"]) in ids]

            def refresh(keep=None):
                codes.clear()                              # patients added since the window opened
                codes.update({c.get("code", "?"): c["id"] for c in self.all_clients if not c.get("archived")})
                p_cb.config(values=sorted(codes))
                if commit():                              # never lose an edit in progress
                    self.queue_renumber()
                # keep what the user has selected NOW (e.g. the row they just clicked) — not the
                # rows the editor showed before, which would undo their click
                keep = list(keep if keep is not None else tv.selection())
                loading["on"] = True                      # rebuilding the list isn't a user selection
                tv.delete(*tv.get_children())
                for it in sorted(Q["items"], key=lambda x: (x["date"], x["seq"])):
                    tv.insert("", "end", iid=str(it["seq"]), values=row(it))
                keep = [k for k in keep if tv.exists(k)]
                if keep:
                    tv.selection_set(keep)
                    tv.see(keep[0])
                tv.update_idletasks()
                loading["on"] = False
                editing["ids"] = list(tv.selection())
                done, total = self.queue_counts()
                waiting = sum(1 for it in Q["items"] if it["status"] in ("waiting", "failed")
                              and not it.get("remote_from"))
                summary.config(text=(f"{total} recording{'s' if total != 1 else ''} · {done} finished"
                                     + (" · running" if Q["running"] else "")) if total else
                               "No recordings yet — click “Add recordings…”.")
                if Q["running"]:
                    start_btn.pack_forget()
                    halt_btn.pack(side="right", padx=px(8))
                    stop_btn.pack(side="right")
                    stop_btn.config(text="Stopping after this one…" if Q["stop"] else "Stop after this one",
                                    state="disabled" if Q["stop"] else "normal")
                else:
                    stop_btn.pack_forget()
                    halt_btn.pack_forget()
                    start_btn.pack(side="right", padx=px(8))
                    start_btn.config(state="normal" if waiting else "disabled")
                load_editor()

            # The editor remembers WHICH recordings it's showing. Edits always go to those, and are
            # saved before another row is loaded — so clicking a different row (or the list being
            # re-sorted) can never send an edit to the wrong recording or lose it.
            editing = {"ids": []}
            FINAL = ("transcribing", "writing", "done", "transcribed", "sending", "sent", "rqueued",
                     "rtranscribing", "rwriting", "syncing")

            def commit():
                """Write what's in the editor into the recordings it was loaded from."""
                if loading["on"]:
                    return False
                ids = set(editing["ids"])
                its = [it for it in Q["items"] if str(it["seq"]) in ids and it["status"] not in FINAL
                       and not it.get("remote_from")]
                if not its:
                    return False
                changed = False
                for it in its:
                    if p_var.get() in codes and codes[p_var.get()] != it["client_id"]:
                        it["client_id"], it["number_manual"], changed = codes[p_var.get()], False, True
                    if k_var.get() in self.QUEUE_KIND and self.QUEUE_KIND[k_var.get()] != it["kind"]:
                        it["kind"], it["kind_manual"], changed = self.QUEUE_KIND[k_var.get()], True, True
                if len(editing["ids"]) == 1 and len(its) == 1:
                    it = its[0]
                    dv = d_var.get().strip()
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", dv) and dv != it["date"]:
                        it["date"], changed = dv, True
                    if n_var.get().strip() != it["number"]:
                        it["number"], it["number_manual"], changed = n_var.get().strip(), bool(n_var.get().strip()), True
                    if o_var.get() != it.get("observations", ""):
                        it["observations"], changed = o_var.get(), True
                    if changed and it["status"] == "failed":
                        it["status"], it["error"] = "waiting", ""
                return changed

            def load_editor(_=None):
                if loading["on"]:
                    return
                new_ids = list(tv.selection())
                if sorted(new_ids) != sorted(editing["ids"]) and commit():
                    # the previous recording(s) had unsaved edits: save them, refresh their rows
                    self.queue_renumber()
                    for it in Q["items"]:
                        if tv.exists(str(it["seq"])):
                            tv.item(str(it["seq"]), values=row(it))
                editing["ids"] = new_ids
                its = selected()
                loading["on"] = True
                editable = bool(its) and not any(it["status"] in FINAL or it.get("remote_from") for it in its)
                one = len(its) == 1
                for wdg in (p_cb, k_cb):
                    wdg.config(state="readonly" if editable else "disabled")
                for wdg in (d_en, n_en, o_en):
                    wdg.config(state="normal" if editable and one else "disabled")
                if its:
                    c = self._client_by_id(its[0]["client_id"])
                    same_p = len({it["client_id"] for it in its}) == 1
                    same_k = len({it["kind"] for it in its}) == 1
                    p_var.set(c.get("code", "") if (c and same_p) else "")
                    k_var.set(("Intake" if its[0]["kind"] == "intake" else "Progress note") if same_k else "")
                    d_var.set(its[0]["date"] if one else "")
                    n_var.set(its[0]["number"] if one else "")
                    o_var.set(its[0].get("observations", "") if one else "")
                else:
                    for v_ in (p_var, k_var, d_var, n_var, o_var):
                        v_.set("")
                failed_one = one and its[0]["status"] == "failed"
                hint.config(text=("Why it failed: " + re.sub(r"\s+", " ", its[0].get("error", ""))[:400]
                                  + "  — fix it (or the labels above) and press Start to try again." if failed_one else
                                  "Patient and type apply to all selected recordings. Select one to change "
                                  "its date or session #." if len(its) > 1 else
                                  ("Tip: select several rows (⌘/Shift-click) to set" if IS_MAC else "Tip: select several rows (Ctrl/Shift-click) to set")
                                  + " the patient for all of them "
                                  "at once."))
                loading["on"] = False
            tv.bind("<<TreeviewSelect>>", load_editor)

            def apply(*_):
                if loading["on"] or not editing["ids"]:
                    return
                refresh()
            p_cb.bind("<<ComboboxSelected>>", apply)
            k_cb.bind("<<ComboboxSelected>>", apply)
            for e_ in (d_en, n_en, o_en):
                e_.bind("<FocusOut>", apply)
                e_.bind("<Return>", apply)

            def add():
                paths = filedialog.askopenfilenames(parent=win, title="Choose the session recordings", filetypes=[
                    ("Audio", "*.wav *.mp3 *.m4a *.flac *.ogg *.opus *.aac *.wma *.mp4 *.webm"), ("All files", "*.*")])
                if not paths:
                    return
                have = {os.path.normcase(it["path"]) for it in Q["items"]}
                new = []
                for path in paths:
                    if os.path.normcase(path) in have:
                        continue
                    cid, day = self.queue_guess(path, default_cid)
                    Q["seq"] += 1
                    it = {"seq": Q["seq"], "path": path, "client_id": cid, "date": day, "number": "",
                          "kind": "progress", "observations": "", "status": "waiting", "error": ""}
                    Q["items"].append(it)
                    new.append(str(it["seq"]))
                self.queue_renumber()
                refresh(new)
                if any(is_cloud_path(p_) for p_ in paths):
                    messagebox.showwarning(APP_NAME, "Some of these recordings are in a cloud-synced folder, so "
                                                     "they're being uploaded unencrypted. Keep recordings in a "
                                                     "folder on this computer.", parent=win)

            def remove():
                its = [it for it in selected() if it["status"] not in ("transcribing", "writing")]
                pending = [it for it in its if it.get("remote_to") and it["status"] not in REMOTE_FINAL]
                if pending and not messagebox.askyesno(
                        APP_NAME, f"Call off {len(pending)} recording(s) sent to "
                                  f"{pending[0].get('remote_name') or 'your other computer'}? (Anything it has "
                                  "already finished is kept.)", parent=win):
                    return
                self.remote_cancel_items(pending)
                ids = {str(it["seq"]) for it in its}
                Q["items"] = [it for it in Q["items"] if str(it["seq"]) not in ids]
                self.queue_renumber()
                refresh(())

            def clear_done():
                Q["items"] = [it for it in Q["items"] if it["status"] not in ("done", "transcribed")]
                refresh(())
            ttk.Button(bar, text="＋  Add recordings…", style="Accent.TButton", command=add).pack(side="left")
            ttk.Button(bar, text="Remove", command=remove).pack(side="left", padx=px(8))
            ttk.Button(bar, text="Clear finished", command=clear_done).pack(side="left")
            self.queue_win = {"win": win, "refresh": refresh, "tv": tv, "add": add, "apply": apply,
                              "vars": (p_var, d_var, n_var, k_var, o_var), "write": write_var,
                              "hosts": fill_hosts, "run": (run_var, run_ids, pick_host),
                              "rows": lambda: [tv.item(str(it["seq"]), values=row(it)) for it in Q["items"]
                                               if tv.exists(str(it["seq"]))]}
            win.protocol("WM_DELETE_WINDOW", win.withdraw)
            refresh()
            fill_hosts()

        def queue_refresh(self):
            if getattr(self, "queue_win", None) is not None and self._alive(self.queue_win["win"]):
                self.queue_win["refresh"]()
            self.update_queue_button()

        def update_queue_button(self):
            if not hasattr(self, "queue_btn"):
                return
            done, total = self.queue_counts()
            Q = self._q()
            away = any(it.get("remote_to") and it["status"] not in REMOTE_FINAL for it in Q["items"])
            self.queue_btn.config(text=f"🎙  Queue  {done}/{total}" if (Q["running"] or away) else
                                  ("🎙  Queue  ·  " + str(total)) if total else "🎙  Queue recordings")

        def stay_awake(self, on):
            """Stop the computer from sleeping while the queue runs (you're away from it).
            Windows: power request; Mac: `caffeinate` (a MacBook still sleeps if the lid is closed)."""
            if IS_MAC:
                proc = getattr(self, "_caffeinate", None)
                if on and proc is None:
                    try:
                        self._caffeinate = subprocess.Popen(["caffeinate", "-i", "-m", "-w", str(os.getpid())])
                    except Exception:
                        self._caffeinate = None
                elif not on and proc is not None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    self._caffeinate = None
                return
            if os.name != "nt":
                return
            try:
                import ctypes
                ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
                ctypes.windll.kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED if on else ES_CONTINUOUS)
            except Exception:
                pass

        def queue_start(self):
            Q = self._q()
            # (recordings handed over from the other computer run by themselves; a failed one is
            # re-sent from there, since its audio is deleted here)
            todo = [it for it in Q["items"] if it["status"] in ("waiting", "failed") and not it.get("remote_from")]
            if not todo:
                return
            win = self.queue_win["win"] if getattr(self, "queue_win", None) else root
            missing = [it for it in todo if not self._client_by_id(it["client_id"])]
            if missing:
                return messagebox.showinfo(APP_NAME, f"Choose the patient for {len(missing)} recording(s) first "
                                                     f"(e.g. {os.path.basename(missing[0]['path'])}).", parent=win)
            gone = [it for it in todo if not os.path.isfile(it["path"])]
            if gone:
                return messagebox.showinfo(APP_NAME, f"Can't find {os.path.basename(gone[0]['path'])} anymore. "
                                                     "Remove it from the queue or put it back.", parent=win)
            target = cfg.get("remote_target") or ""
            if target:
                host = next((h for h in self._rem()["hosts"] if h["id"] == target), None)
                return self.remote_start(todo, target, host, win)
            if not engine_installed():
                return messagebox.showinfo(APP_NAME, "The speech engine isn't installed yet — open Settings first.",
                                           parent=win)
            if Q["write"] and not (isinstance(self.lm_status, list) and self.lm_status):
                if not messagebox.askyesno(APP_NAME, "LM Studio doesn't seem to be running, so the notes can't be "
                                                     "written. Start the queue anyway? (Transcripts are still "
                                                     "saved; you can write the notes later.)", parent=win):
                    return
            if self.busy:
                return messagebox.showinfo(APP_NAME, f"Another task is running ({self.task_label}). The queue can "
                                                     "start when it finishes.", parent=win)
            for it in todo:
                it["status"], it["error"] = "waiting", ""
                it["write"] = Q["write"]
            Q["running"], Q["stop"], Q["auto"] = True, False, False
            self.stay_awake(True)
            self.queue_refresh()
            self.queue_next()

        def queue_stop(self):
            self._q()["stop"] = True
            self.queue_refresh()

        def queue_halt(self):
            """Stop the queue now: the recording being worked on is stopped mid-way (nothing from it is
            saved; it goes back to waiting) and nothing else starts."""
            Q = self._q()
            cur = [it for it in Q["items"] if it["status"] in ("transcribing", "writing")]
            self.cancel_task(("queue",), "Queue stopped.")
            for it in cur:
                for k in ("lines", "transcript", "note", "overview"):
                    it.pop(k, None)
                it["status"], it["error"] = "waiting", ""
                if it.get("remote_from"):
                    self.remote_mark(it, "queued")
            Q["stop"] = True
            self.queue_finish("Queue stopped right away — the recording in progress was stopped and nothing "
                              "from it was saved. Press Start to run it again.")

        def queue_finish(self, why=""):
            Q = self._q()
            Q["running"], Q["stop"], Q["auto"] = False, False, False
            self.stay_awake(False)
            ok = sum(1 for it in Q["items"] if it["status"] in ("done", "transcribed"))
            bad = sum(1 for it in Q["items"] if it["status"] == "failed")
            self.set_status(why or (f"Queue finished: {ok} session(s) saved as drafts to review"
                                    + (f", {bad} failed (see the queue)" if bad else "") + "."))
            self.queue_refresh()
            if self.client_id and not self.wizard and not self.locked and not self._page_editing:
                self.open_client(self.client_id)
            root.bell()

        def queue_next(self):
            Q = self._q()
            if not Q["running"]:
                return
            if Q["stop"]:
                return self.queue_finish("Queue stopped. Press Start in the queue to carry on.")
            waiting = sorted((it for it in Q["items"] if it["status"] == "waiting"
                              and not it.get("remote_to")                      # sent elsewhere: not ours to run
                              and (not Q.get("auto") or it.get("remote_from"))),  # auto-run: only handed-over jobs
                             key=lambda x: (x["date"], x["seq"]))
            if not waiting:
                return self.queue_finish()
            if self.busy:                                  # something else grabbed the worker: try again shortly
                return root.after(5000, self.queue_next)
            it = waiting[0]
            if it.get("remote_from") and remote_cancelled(remote_root(cfg), it["remote_job"]):
                self.remote_drop_incoming(it, "cancelled")
                return root.after(100, self.queue_next)
            self.queue_transcribe(it)

        def _queue_label(self, verb):
            done, total = self.queue_counts()
            return f"queue {done + 1}/{total}: {verb}"

        def queue_transcribe(self, it):
            client = self._client_by_id(it["client_id"])
            code = client.get("code", "")
            job_cfg, token, path = dict(cfg), self.settings.get("hf_token", ""), it["path"]
            live = {}                                     # the live-progress sender, for a handed-over recording

            def job(progress):
                live["tok"] = current_token()

                def prog(t, pct=None):
                    progress(t, pct)                      # this computer's own bar (and stops if cancelled)
                    if live.get("pub"):
                        live["pub"].update(t, pct)        # never blocks: written on its own thread
                segs, dur, diar, aligned = transcribe(path, job_cfg, token, prog)
                turns = words_to_turns(segs, smooth=bool(job_cfg.get("smooth_flicker", True)))
                return turns, dur, diar, aligned

            def done(res):
                lines, dur, diar, aligned = res
                pub = it.pop("_pub", None)
                if pub is not None:
                    pub.close()                            # transcription is over: no more "transcribing" beats
                self.remote_audio_done(it)                 # a handed-over recording isn't needed any more
                if not lines:
                    return failed("No speech was found in that recording.")
                roles = guess_roles(lines, role_labels(cfg))
                it.update(lines=lines, duration=dur, diarized=diar, aligned=aligned, roles=roles,
                          transcript=render_transcript(lines, roles))
                if it.get("remote_from") and remote_cancelled(remote_root(cfg), it["remote_job"]):
                    for k in ("lines", "transcript"):
                        it.pop(k, None)
                    self.remote_drop_incoming(it, "cancelled")
                    return root.after(200, self.queue_next)
                if it.get("write", self._q()["write"]):
                    self.queue_write(it)
                else:
                    self.queue_save(it)

            def failed(msg):
                self.remote_audio_done(it)
                it["status"], it["error"] = "failed", msg
                self.remote_mark(it, "failed", error=msg)
                self.queue_refresh()
                root.after(200, self.queue_next)
            if self.run_bg(job, done, failed, label=self._queue_label(f"transcribing {code}"), quiet=True,
                           owner=("queue",), patient=code):
                it["status"] = "transcribing"
                self.remote_mark(it, "transcribing")
                self.remote_live(it, "transcribing", live)
                self.queue_refresh()
            else:
                root.after(5000, self.queue_next)

        def queue_write(self, it):
            try:
                client = vault.load_client(it["client_id"])
                code, hide, context, used, values = self._gen_inputs(client, None, it["date"], it["number"],
                                                                     it.get("observations", ""))
            except Exception as e:
                it["notes_error"] = friendly_error(e)
                return self.queue_save(it)
            transcript, job_cfg = hide(it["transcript"]), dict(cfg)
            kind, reference = it["kind"], self.settings.get("reference") or BUILTIN_REFERENCE

            def done(res):
                it["note"], it["overview"], it["context_used"] = res[0] or "", res[1] or "", used
                self.queue_save(it)

            def failed(msg):
                it["notes_error"] = msg                   # keep the transcript; notes can be written later
                self.queue_save(it)
            live = {}

            def gen(p):
                live["tok"] = current_token()

                def prog(t, pct=None):
                    p(t, pct)
                    if live.get("pub"):
                        live["pub"].update(t, pct)
                return generate_documents(job_cfg, values, transcript, context, prog, kind=kind,
                                          reference=reference)
            if self.run_bg(gen, done, failed,
                           label=self._queue_label(f"writing notes for {code}"), quiet=True,
                           owner=("queue",), patient=code):
                it["status"] = "writing"
                self.remote_mark(it, "writing")
                self.remote_live(it, "writing", live)
                self.queue_refresh()
            else:                                         # worker taken: retry this step shortly
                root.after(5000, lambda: self.queue_write(it))

        def queue_save(self, it):
            client = self._client_by_id(it["client_id"])
            now = datetime.now().isoformat(timespec="seconds")
            data = {"meta": {"client_id": it["client_id"], "client_code": client.get("code", ""),
                             "session_date": it["date"], "session_number": it["number"],
                             "duration": it.get("duration", 0.0),
                             "audio_name": it.get("audio_name") or os.path.basename(it["path"]),
                             "created": now, "updated": now, "kind": it["kind"], "source": "queue",
                             "needs_review": True,
                             "review_reason": ("speaker labels were guessed automatically"
                                               + ("; the notes couldn't be written: " + it["notes_error"][:200]
                                                  if it.get("notes_error") else ""))},
                    "observations": it.get("observations", ""), "transcript": it["transcript"],
                    "note": it.get("note", ""), "overview": it.get("overview", ""),
                    "context_used": it.get("context_used", []),
                    "lines": it["lines"], "roles": it["roles"], "diarized": it["diarized"], "aligned": it["aligned"]}
            if it.get("remote_from"):
                data["meta"]["processed_on"] = platform.node() or "the other computer"
            try:
                it["sid"] = vault.save_session(data)
                self.refresh_clients(self.client_id)    # sidebar shows "⚠ to review"; also resets _sig
                it["status"] = "done" if it.get("write", self._q()["write"]) else "transcribed"
                self.remote_mark(it, it["status"], sid=it["sid"], notes_error=it.get("notes_error", ""))
            except Exception as e:
                it["status"], it["error"] = "failed", f"Couldn't save: {e}"
                self.remote_mark(it, "failed", error=it["error"])
            for k in ("lines", "transcript", "note", "overview"):     # don't keep patient text in memory
                it.pop(k, None)
            self.queue_refresh()
            if self.client_id == it["client_id"] and not self.wizard and not self.locked and not self._page_editing:
                self.open_client(self.client_id)
            root.after(200, self.queue_next)

        # ================================================= OTHER COMPUTER (remote processing)
        # Sender: the queue's "Run on" sends each recording (encrypted) through the shared data
        # folder, then watches the worker's progress, waits for the finished session to sync
        # here, and notifies. Worker: picks up recordings addressed to it and runs them through
        # its own queue. Both sides only ever exchange encrypted files via Google Drive.
        def _rem(self):
            if not hasattr(self, "_remote"):
                self._remote = {"hosts": [], "out": {}, "taken": set(), "busy": False, "beat": 0,
                                "rebuilt": False, "notified": set()}
            return self._remote

        def _host_name(self, hid):
            h = next((h for h in self._rem()["hosts"] if h["id"] == hid), None)
            return (h or {}).get("computer") or (hid or "").rsplit("-", 1)[0] or "the other computer"

        def run_on_choices(self):
            """{label: computer id} for 'Process on' menus — this computer ('' ) first, then the others."""
            R = self._rem()
            me = platform.node() or "This computer"
            ch = {f"{me} (this computer)": ""}
            for h in R["hosts"]:
                ch[f"{h.get('computer', h['id'])} (your other computer)"] = h["id"]
            tgt = cfg.get("remote_target") or ""
            if tgt and tgt not in ch.values():
                ch[f"{self._host_name(tgt)} (your other computer)"] = tgt
            return ch

        def computers_not_seen(self):
            """Other computers that sync this data folder but haven't checked in for handoffs —
            usually because they're still on an older version of Session Scribe."""
            seen = {re.sub(r"[^A-Za-z0-9_.-]", "_", h.get("computer") or "") for h in self._rem()["hosts"]}
            out = []
            try:
                folder = os.path.join(data_root(cfg), SYNC_DIR)
                for fn in sorted(os.listdir(folder)):
                    if not fn.endswith(".json") or fn[:-5] == this_computer() or fn[:-5] in seen:
                        continue
                    name = _read_json(os.path.join(folder, fn)).get("computer") or fn[:-5]
                    out.append(name)
            except OSError:
                pass
            return out

        def not_seen_text(self):
            names = self.computers_not_seen()
            if not names:
                return ""
            who = " and ".join(names)
            return (f"{who} {'uses' if len(names) == 1 else 'use'} this data folder but can't take recordings yet — "
                    "install this version of Session Scribe there and open it; it shows up here within a minute.")

        def apply_app_update(self, build, text, src):
            """A newer Session Scribe arrived from the other computer: install it, then restart
            when nothing is in progress."""
            R = self._rem()
            try:
                install_app_update(text, build)
            except Exception as e:
                R["installed"] = build                     # don't retry every 30 s; it's in the error log
                self.set_status(f"Couldn't install the update from {src}: {friendly_error(e)}")
                return
            R["installed"], R["restart_for"] = build, (build, src)
            self.offer_restart()

        def offer_restart(self):
            pend = self._rem().get("restart_for")
            if not pend:
                return
            build, src = pend
            Q = self._q()
            idle = not (self.busy or self.waitlist or Q["running"] or self.locked
                        or (self.wizard and (self.wizard.get("dirty") or self.wizard.get("task")))
                        or (self.parked and (self.parked.get("dirty") or self.parked.get("task")))
                        or str(root.tk.call("grab", "current")) or self._user_typing())
            if not idle:
                self.set_status(f"Session Scribe was updated (version {build}, from {src}). It'll offer to "
                                "restart when nothing is in progress.")
                root.after(60_000, self.offer_restart)
                return
            self._rem()["restart_for"] = None
            if messagebox.askyesno(APP_NAME, f"Session Scribe was updated to version {build} — the copy on "
                                             f"{src} is newer, so it was installed here too.\n\nRestart now to "
                                             "use it? (Everything is saved.)"):
                self.restart_app()
            else:
                self.set_status(f"Updated to version {build} — it takes effect the next time Session Scribe opens.")

        def remote_ready_text(self, h):
            if not h.get("online"):
                mins = int(h["age"] // 60)
                ago = (f"{mins // 60} h" if mins >= 120 else f"{mins} min") if mins < 60 * 48 else "days"
                return f"offline (last seen {ago} ago)"
            if not h.get("accepting"):
                return "open, but not accepting recordings (its Settings)"
            if not h.get("can_transcribe"):
                return "open, but its speech engine isn't set up"
            bits = ["ready"]
            if not h.get("build") or build_tuple(h["build"]) < build_tuple(APP_BUILD):
                bits.append("on an older version — restart Session Scribe there to finish updating")
            if not h.get("can_write"):
                bits.append("LM Studio not running — transcripts only")
            if h.get("busy"):
                bits.append("busy")
            return ", ".join(bits)

        def remote_tick(self):
            """Every 15 s, in the background: say we're open (and ready), pick up recordings
            sent here, and check on recordings sent elsewhere."""
            R = self._rem()
            try:
                if not R["busy"] and vault.fernet is not None:
                    R["busy"] = True
                    Q = self._q()
                    snap = {"out": list(R["out"]), "taken": set(R["taken"]), "ackd": dict(R.get("ackd", {})),
                            "beat_due": time.time() - R["beat"] > REMOTE_BEAT_SECS,
                            # LM Studio is often started after the app: notice it without a click
                            "lm_cfg": dict(cfg) if time.time() - R.get("lm_at", 0) > 60 else None,
                            "accept": bool(cfg.get("remote_accept", True)), "rebuild": not R["rebuilt"],
                            "update_due": time.time() - R.get("upd_at", 0) > 30, "installed": R.get("installed"),
                            # a recording from the other computer that's being worked on right now
                            "running": next((it["remote_job"] for it in Q["items"] if it.get("remote_from")
                                             and it["status"] in ("transcribing", "writing")), None),
                            "info": {"accepting": bool(cfg.get("remote_accept", True)),
                                     "can_transcribe": bool(platform_info()["supported"]) and (
                                         engine_installed() if time.time() - R["beat"] > REMOTE_BEAT_SECS
                                         else R.get("can_tr", False)),
                                     "can_write": isinstance(self.lm_status, list) and bool(self.lm_status),
                                     "busy": bool(Q["running"]), "os": platform_info()["label"],
                                     "build": APP_BUILD}}
                    threading.Thread(target=self._remote_scan, args=(snap,), daemon=True).start()
            finally:
                # poll faster while a handoff is actually moving, so both progress bars keep step
                Q = self._q()
                busy = any(it.get("remote_to") and it["status"] in
                           ("sending", "sent", "rqueued", "rtranscribing", "rwriting", "syncing")
                           for it in Q["items"]) or \
                       any(it.get("remote_from") and it["status"] in ("waiting", "transcribing", "writing")
                           for it in Q["items"])
                root.after(REMOTE_FAST_MS if busy else REMOTE_POLL_MS, self.remote_tick)

        def _remote_scan(self, snap):
            """Background thread: file work only; results go back to the UI thread."""
            res = {"hosts": [], "incoming": [], "status": {}, "mine": [], "errors": []}
            if snap.get("lm_cfg"):
                try:
                    res["lm"] = list_models(snap["lm_cfg"], timeout=3)
                except Exception as e:
                    res["lm"] = str(e)
            try:
                rr, me = remote_root(cfg), computer_id(cfg)
                if snap["beat_due"]:
                    try:
                        remote_heartbeat(rr, me, snap["info"])
                        res["beat"] = True
                    except OSError as e:
                        res["errors"].append(str(e))
                res["hosts"] = remote_hosts(rr, me)
                jobs = remote_jobs(vault, rr) if os.path.isdir(os.path.join(rr, "jobs")) else []
                for j in jobs:
                    if j.get("to") == me and snap["accept"] and j["id"] not in snap["taken"]:
                        st = remote_status(vault, rr, j["id"])
                        running = st and st.get("status") in ("transcribing", "writing")
                        if (st and st.get("status") in REMOTE_FINAL) or remote_cancelled(rr, j["id"]):
                            continue
                        # Tell the sender we've SEEN the recording and why it hasn't started yet, so its
                        # row stops saying "Waiting" and shows real state. Published once per reason.
                        reason = None
                        if not remote_audio_ready(rr, j):
                            reason = "syncing"             # the recording itself is still arriving here
                        elif not os.path.exists(os.path.join(vault.clients_dir, j.get("client_id", "") + ".enc")):
                            reason = "profile"             # the patient profile hasn't synced in yet
                        if reason:
                            if not running and snap["ackd"].get(j["id"]) != reason:
                                try:
                                    remote_set_status(vault, rr, j["id"], "queued",
                                                      host=platform.node(), reason=reason)
                                    res.setdefault("ackd", {})[j["id"]] = reason
                                except OSError as e:
                                    res["errors"].append(str(e))
                            continue
                        try:
                            path = remote_fetch_audio(vault, rr, j, os.path.join(LOCAL_DIR, "remote-audio"))
                        except Exception as e:
                            res["errors"].append(f"{j['id']}: {e}")
                            continue
                        snap["taken"].add(j["id"])
                        res.setdefault("ackd", {})[j["id"]] = "started"
                        res["incoming"].append((j, path))
                    elif j.get("from") == me and (snap["rebuild"] or j["id"] in snap["out"]):
                        if snap["rebuild"] and j["id"] not in snap["out"]:
                            res["mine"].append(j)           # sent before the app was last closed
                if snap.get("running") and remote_cancelled(rr, snap["running"]):
                    res["kill"] = snap["running"]
                if snap.get("update_due"):
                    try:
                        res["app"] = app_update_check(vault, data_root(cfg), skip=snap.get("installed"))
                    except Exception as e:
                        res["errors"].append(f"update: {e}")
                for jid in set(snap["out"]) | {j["id"] for j in res["mine"]}:
                    st = remote_status(vault, rr, jid)
                    arrived = bool(st and st.get("sid") and
                                   os.path.exists(os.path.join(vault.sessions_dir, st["sid"] + ".enc")))
                    res["status"][jid] = (st, arrived)
            except Exception as e:
                res["errors"].append(str(e))
            self.msgs.put(("call", lambda: self._remote_apply(snap, res)))

        def _remote_apply(self, snap, res):
            R = self._rem()
            R["busy"] = False
            if res.get("ackd"):
                R.setdefault("ackd", {}).update(res["ackd"])
            if snap.get("update_due"):
                R["upd_at"] = time.time()
            if res.get("app", {}).get("update"):
                self.apply_app_update(*res["app"]["update"])
            if "lm" in res:
                R["lm_at"] = time.time()
                if res["lm"] != self.lm_status and not (isinstance(res["lm"], str) and isinstance(self.lm_status, str)):
                    self.lm_status = res["lm"]
                    self.refresh_strip()
                    if self.setup_win is not None:
                        self.setup_win["refresh"]()
            if res.get("beat"):
                R["beat"] = time.time()
                R["can_tr"] = snap["info"]["can_transcribe"]
            sig = lambda hs: sorted((h["id"], bool(h.get("online")), bool(h.get("accepting")),
                                     bool(h.get("can_transcribe"))) for h in hs)
            changed = sig(R["hosts"]) != sig(res["hosts"])
            R["hosts"] = res["hosts"]
            w = self.wizard
            if changed and w and w.get("step") == 0 and not w.get("task") and \
                    getattr(self, "_page_builder", None) == self._wizard_page and not self._user_typing():
                self.goto(0)                       # a computer came online / went away: refresh "Process on"
            if snap["rebuild"]:
                R["rebuilt"] = True
                for j in res["mine"]:
                    self._remote_track(j)
            if res.get("kill"):
                # the other computer called this one off: stop it now rather than finishing it
                it = next((x for x in self._q()["items"] if x.get("remote_job") == res["kill"]
                           and x["status"] in ("transcribing", "writing")), None)
                if it is not None and self.cancel_task(("queue",), "A recording was cancelled from the other "
                                                                   "computer — stopped."):
                    for k in ("lines", "transcript"):
                        it.pop(k, None)
                    self.remote_drop_incoming(it, "cancelled")
                    if self._q()["running"]:
                        root.after(300, self.queue_next)
            for j, path in res["incoming"]:
                self.remote_take(j, path)
            changed = 0
            for jid, (st, arrived) in res["status"].items():
                changed = max(changed, self._remote_progress(jid, st, arrived) or 0)
            # a recording being worked on elsewhere: its elapsed time / "gone quiet" note changes even
            # when the worker has nothing new to say, so keep its row and the status line current
            act = next((it for it in self._q()["items"] if it.get("remote_to")
                        and it["status"] in ("rqueued", "rtranscribing", "rwriting", "syncing")), None)
            if changed == 2 or res["incoming"] or res["mine"]:
                self.queue_refresh()
            elif changed or act is not None:
                qw = getattr(self, "queue_win", None)        # just repaint the rows: never disturbs editing
                if qw and self._alive(qw["win"]) and qw.get("rows"):
                    qw["rows"]()
            if act is not None:
                if not self.busy:
                    line = self._remote_line(act)
                    if line and line != R.get("lastline"):
                        R["lastline"] = line
                        self.set_status(line)
            qw = getattr(self, "queue_win", None)
            if qw and self._alive(qw["win"]) and qw.get("hosts"):
                qw["hosts"]()

        def _remote_line(self, it):
            """One line describing a handed-over recording's live state, for the status bar."""
            where = it.get("remote_name") or "your other computer"
            s, pct, txt = it["status"], it.get("rpct"), (it.get("rtext") or "")
            if s == "rqueued":
                why = {"syncing": "the recording is still syncing over there",
                       "profile": "waiting for the patient profile to sync"}.get(it.get("rreason"),
                                                                                 "queued, about to start")
                return f"Received on {where} — {why}…"
            if s == "syncing":
                return f"Done on {where} — syncing the result back here…"
            verb = "Transcribing" if s == "rtranscribing" else "Writing notes"
            tail = f" · {pct:.0f}%" if isinstance(pct, (int, float)) else ""
            detail = f" — {txt.rstrip('…')}" if (txt and tail) else ""
            return f"{verb} on {where}{tail}{detail}{self._remote_health(it)}"

        def _remote_health(self, it):
            """How long the current step has run there, and a plain warning if it's gone quiet or
            the other computer is still on an older version (which can't send live progress)."""
            if it["status"] not in ("rtranscribing", "rwriting"):
                return ""
            where = it.get("remote_name") or "the other computer"
            if not it.get("rbuild") or build_tuple(it["rbuild"]) < build_tuple(APP_BUILD):
                return (f" (live progress needs the newer version on {where} — restart Session Scribe "
                        "there; it was updated automatically)")
            out = ""
            if it.get("rtime") and it.get("rsince"):
                secs = max(0, int(it["rtime"]) - int(it["rsince"]))
                if secs >= 20:
                    out = f" ({secs // 60}:{secs % 60:02d} on this step)"
            quiet = time.time() - it.get("rseen", time.time())
            if quiet > 150:
                out += (f" — no update from {where} for {int(quiet // 60)} min; check that Session Scribe is "
                        "still open there and Google Drive is syncing")
            return out

        # ---- sender side
        def remote_start(self, todo, target, host, win, write=None):
            """Send recordings to the other computer. Returns True if they're on their way."""
            name = (host or {}).get("computer") or self._host_name(target)
            if host is None or not host.get("online"):
                if not messagebox.askyesno(APP_NAME, f"{name} doesn't seem to have Session Scribe open right now. "
                                                     "Send anyway? It'll start as soon as Session Scribe is "
                                                     "opened there.", parent=win):
                    return False
            elif not host.get("can_transcribe"):
                messagebox.showinfo(APP_NAME, f"{name} can't transcribe yet — its speech engine isn't set "
                                              "up. Open Settings on that computer first.", parent=win)
                return False
            elif not host.get("accepting"):
                messagebox.showinfo(APP_NAME, f"{name} isn't accepting recordings. On that computer, "
                                              "turn on “Run recordings sent from my other computer” in "
                                              "Settings → Folders & sync.", parent=win)
                return False
            Q, me = self._q(), computer_id(cfg)
            for it in todo:
                it.update(status="sending", error="", remote_to=target, remote_name=name,
                          write=Q["write"] if write is None else write)
            self.queue_refresh()
            rr = remote_root(cfg)

            def work():
                for it in todo:
                    job = {"id": uuid.uuid4().hex, "from": me, "from_name": platform.node() or "the other computer",
                           "to": target, "created": datetime.now().isoformat(timespec="seconds"),
                           "client_id": it["client_id"], "date": it["date"], "number": it["number"],
                           "kind": it["kind"], "observations": it.get("observations", ""), "write": it["write"]}
                    try:
                        job = remote_send(vault, rr, job, it["path"])
                        self.msgs.put(("call", lambda it=it, job=job: self._remote_sent(it, job)))
                    except Exception as e:
                        msg = friendly_error(e)
                        self.msgs.put(("call", lambda it=it, msg=msg: self._remote_send_failed(it, msg)))
            threading.Thread(target=work, daemon=True).start()
            self.set_status(f"Sending {len(todo)} recording(s) to {name} — encrypted, through your synced "
                            "data folder. You'll get a notification as each one is done.")
            return True

        def _remote_sent(self, it, job):
            it.update(status="sent", remote_job=job["id"], audio_name=job.get("audio_name"))
            self._rem()["out"][job["id"]] = it
            self.queue_refresh()

        def _remote_send_failed(self, it, msg):
            it.update(status="failed", error="Couldn't send it: " + msg, remote_to=None)
            self.queue_refresh()

        def _remote_track(self, job):
            """Show a job sent before the app was last closed, so its result is picked up."""
            Q = self._q()
            if any(it.get("remote_job") == job["id"] for it in Q["items"]):
                return
            Q["seq"] += 1
            it = {"seq": Q["seq"], "path": job.get("audio_name") or "recording", "audio_name": job.get("audio_name"),
                  "client_id": job.get("client_id"), "date": job.get("date", ""), "number": job.get("number", ""),
                  "kind": job.get("kind", "progress"), "observations": job.get("observations", ""),
                  "status": "sent", "error": "", "remote_to": job.get("to"), "remote_job": job["id"],
                  "remote_name": self._host_name(job.get("to")), "write": job.get("write", True),
                  "number_manual": True, "kind_manual": True}
            Q["items"].append(it)
            self._rem()["out"][job["id"]] = it

        def _remote_progress(self, jid, st, arrived):
            """Apply the worker's latest status to the queue row. Returns True if it changed."""
            R = self._rem()
            it = R["out"].get(jid)
            if it is None:
                return False
            before = (it["status"], it.get("error"), it.get("rpct"), it.get("rtext"), it.get("rreason"))
            stage = (st or {}).get("status")
            # live detail the row and status line show, kept in step with the worker
            it["rpct"] = (st or {}).get("pct")
            it["rtext"] = (st or {}).get("text") or ""
            it["rreason"] = (st or {}).get("reason") or ""
            prev_time = it.get("rtime")
            it["rtime"] = (st or {}).get("time")             # when the worker last wrote (its clock)
            if it["rtime"] != prev_time or "rseen" not in it:
                it["rseen"] = time.time()                    # when WE last saw something new (our clock)
            it["rsince"] = (st or {}).get("step_since")      # when its current step began
            it["rbuild"] = (st or {}).get("build")           # its Session Scribe version (old ones send none)
            if stage == "queued":
                it["status"] = "rqueued"               # received there, not yet started (see rreason)
            elif stage in ("transcribing", "writing"):
                it["status"] = {"transcribing": "rtranscribing", "writing": "rwriting"}[stage]
            elif stage in ("done", "transcribed"):
                if arrived:
                    self._remote_finish(it, st)
                else:
                    it["status"] = "syncing"
            elif stage == "failed":
                why = re.sub(r"^\w+(Error|Exception):\s*", "", (st.get("error") or "failed").strip())
                it.update(status="failed", error=f"On {it.get('remote_name')}: {why}", remote_to=None)
                R["out"].pop(jid, None)
                remote_cleanup(remote_root(cfg), jid)
                self._remote_notify(it, failed=True)
            elif stage == "cancelled":
                R["out"].pop(jid, None)
                remote_cleanup(remote_root(cfg), jid)
                it.update(status="failed", error="Cancelled.", remote_to=None)
            after = (it["status"], it.get("error"), it.get("rpct"), it.get("rtext"), it.get("rreason"))
            # 2 = the row's state changed (rebuild the list), 1 = only its live detail moved (repaint)
            return 2 if before[:2] != after[:2] else (1 if before != after else 0)

        def _remote_finish(self, it, st):
            jid = it["remote_job"]
            self._rem()["out"].pop(jid, None)
            it.update(status=st["status"], sid=st.get("sid"), notes_error=st.get("notes_error", ""))
            remote_cleanup(remote_root(cfg), jid)
            # the finished session is here now: show it, flag it for review, record that we're in sync
            self.refresh_clients(self.client_id)
            if self.client_id == it["client_id"] and not self.wizard and not self.locked and not self._page_editing \
                    and not self._user_typing():
                self.open_client(self.client_id, select=self.view_select)
            self.maybe_mark_sync(force=True)
            self._remote_notify(it)

        def _remote_notify(self, it, failed=False):
            R = self._rem()
            key = (it.get("remote_job") or it.get("seq"), failed)
            if key in R["notified"]:
                return
            R["notified"].add(key)
            c = self._client_by_id(it["client_id"])
            code = (c or {}).get("code", "a patient")
            where = it.get("remote_name") or "your other computer"
            if failed:
                title, msg = "Recording couldn't be processed", f"{code} · {fmt_date(it['date'])} — on {where}. " \
                                                                 "Open the recording queue to see why."
            else:
                title = "Recording done — needs review"
                msg = f"{code} · {fmt_date(it['date'])} was processed on {where} and is ready to review here."
                if it.get("notes_error"):
                    msg = f"{code} · {fmt_date(it['date'])}: transcript saved on {where}, but the notes " \
                          "couldn't be written."
            left = sum(1 for x in self._q()["items"] if x.get("remote_to") and x["status"] not in REMOTE_FINAL)
            if not failed and not left:
                msg += " That was the last one."
            if self.locked:                 # nothing identifying while the app is locked
                msg = f"A recording sent to {where} " + ("couldn't be processed" if failed else "is done") + \
                      " — unlock Session Scribe to see it."
            else:
                self.set_status(msg)
                self.toast(title, msg, (lambda: self.open_client(it["client_id"], select=f"day:{it['date']}"))
                           if not failed else (lambda: self.open_queue()))
            if cfg.get("remote_notify", True):
                # the system notification never names a patient: Windows/macOS keep notifications in
                # their history and can show them on the lock screen. Details are in the app.
                generic = (f"A recording sent to {where} " + ("couldn't be processed" if failed else
                                                             "is done and needs review")
                           + (". That was the last one." if (not failed and not left) else
                              f". {left} still in progress." if left else ".")
                           + " Open Session Scribe to see it.")
                desktop_notify(title, generic)
            root.bell()

        def toast(self, title, msg, on_open=None):
            """A small card in the bottom-right of the window for a few seconds."""
            old = getattr(self, "_toast", None)
            if self._alive(old):
                old.destroy()
            card = ttk.Frame(root, style="Card.TFrame", padding=(px(14), px(10)))
            self._toast = card
            ttk.Label(card, text=title, style="Strong.TLabel").pack(anchor="w")
            ttk.Label(card, text=msg, style="Muted.TLabel", wraplength=px(320), justify="left").pack(
                anchor="w", pady=(px(2), px(8)))
            row_ = ttk.Frame(card)
            row_.pack(anchor="e")
            if on_open:
                ttk.Button(row_, text="Open", style="Accent.TButton",
                           command=lambda: (card.destroy(), self.leave(on_open))).pack(side="right")
            ttk.Button(row_, text="Dismiss", command=card.destroy).pack(side="right", padx=(0, px(6)))
            card.place(relx=1.0, rely=1.0, x=-px(18), y=-px(52), anchor="se")
            card.lift()
            root.after(15_000, lambda: self._alive(card) and card.destroy())

        def remote_cancel_items(self, its):
            rr = remote_root(cfg)
            for it in its:
                jid = it.get("remote_job")
                if not jid or it["status"] in REMOTE_FINAL:
                    continue
                if remote_status(vault, rr, jid) is None:
                    remote_cleanup(rr, jid)                # not picked up yet: just take it back
                    self._rem()["out"].pop(jid, None)
                else:
                    remote_cancel(rr, jid)                 # the other computer stops it and confirms

        # ---- worker side
        def remote_take(self, job, path):
            """A recording sent here from the other computer: run it through this computer's queue."""
            R, Q = self._rem(), self._q()
            if job["id"] in R["taken"] or any(it.get("remote_job") == job["id"] for it in Q["items"]):
                return
            R["taken"].add(job["id"])
            if not self._client_by_id(job.get("client_id")):
                self.refresh_clients(self.client_id)     # the profile just synced in
            Q["seq"] += 1
            it = {"seq": Q["seq"], "path": path, "audio_name": job.get("audio_name") or "recording",
                  "client_id": job.get("client_id"), "date": job.get("date", ""), "number": job.get("number", ""),
                  "kind": job.get("kind", "progress"), "observations": job.get("observations", ""),
                  "status": "waiting", "error": "", "write": bool(job.get("write", True)),
                  "number_manual": True, "kind_manual": True,    # the sender already numbered it
                  "remote_job": job["id"], "remote_from": job.get("from_name") or "your other computer"}
            Q["items"].append(it)
            self.remote_mark(it, "queued")
            self.set_status(f"Recording received from {it['remote_from']} — it'll be processed here.")
            if not Q["running"]:
                Q["running"], Q["stop"], Q["auto"] = True, False, True
                self.stay_awake(True)
                self.queue_refresh()
                root.after(300, self.queue_next)
            else:
                self.queue_refresh()

        def remote_mark(self, it, status, **extra):
            """Worker: tell the sender how its recording is doing (encrypted status file)."""
            pub = it.pop("_pub", None)
            if pub is not None:
                pub.close()                                # an explicit new state replaces live progress
            jid = it.get("remote_job")
            if not jid or not it.get("remote_from"):       # only for recordings handed over to us
                return
            try:
                remote_set_status(vault, remote_root(cfg), jid, status, host=platform.node(), **extra)
            except Exception:
                pass

        def remote_live(self, it, stage, holder):
            """Worker: start sending this handed-over recording's live progress to the sender (on its
            own thread — see RemoteProgressPublisher). holder["pub"] is what the progress callback
            feeds; holder["tok"] is the task's cancel token, set from inside the task."""
            if not (it.get("remote_job") and it.get("remote_from")):
                return

            def alive():
                tok = holder.get("tok")
                return tok is None or not tok.is_set()

            def beat(text, pct, secs):
                # a long step with nothing new to report (e.g. the model loading): show it's alive here too
                if secs >= 15 and self.busy and self.task_owner == ("queue",):
                    m, s = divmod(int(secs), 60)
                    self.msgs.put(("status", f"{text.rstrip('…')}… ({m}:{s:02d})", pct, holder.get("tok")))
            holder["pub"] = it["_pub"] = RemoteProgressPublisher(vault, remote_root(cfg), it["remote_job"],
                                                                 stage, alive=alive, on_beat=beat)

        def remote_audio_done(self, it):
            """Worker: the recording has been transcribed (or failed) — delete both copies of it."""
            # only ever the temporary copy decrypted from a handed-over job — never your own recording
            if it.get("remote_job") and it.get("remote_from"):
                if it.get("path") and os.path.exists(it["path"]) and \
                        os.path.dirname(os.path.abspath(it["path"])) == os.path.abspath(
                            os.path.join(LOCAL_DIR, "remote-audio")):
                    _quiet(wipe_file, it["path"])
                remote_drop_audio(remote_root(cfg), it["remote_job"])

        def remote_drop_incoming(self, it, why):
            self.remote_audio_done(it)
            self.remote_mark(it, why)
            Q = self._q()
            Q["items"] = [x for x in Q["items"] if x is not it]
            self.queue_refresh()

        # =========================================================== SETUP
        def open_setup(self):
            if self.setup_win is not None:
                self.setup_win["win"].lift()
                return
            win = new_window(root, "Settings", "920x820")
            scroller, outer = scrollable(win, padding=(px(24), px(20), px(18), px(20)))
            scroller.pack(fill="both", expand=True)
            ttk.Label(outer, text="Settings", style="Title.TLabel").pack(anchor="w")
            ttk.Label(outer, text=f"Running with: {sys.executable}", style="Caption.TLabel").pack(anchor="w", pady=(0, 8))
            checks = ttk.Frame(outer)          # only things that need attention
            checks.pack(fill="x")
            checks.columnconfigure(1, weight=1)

            # ---- this computer: detect the OS and set up everything it needs, in one go
            pinfo = platform_info()
            pc = ttk.Frame(outer, style="Card.TFrame", padding=(px(14), px(10)))
            pc.pack(fill="x", pady=(0, px(10)), before=checks)
            pc_top = Flow(pc, gap=px(16))
            pc_top.pack(fill="x")
            pc_top.add(ttk.Label(pc_top, text=f"Session Scribe {APP_BUILD}  ·  {pinfo['label']}  ·  Python "
                                              f"{sys.version_info[0]}.{sys.version_info[1]}",
                                 style="Strong.TLabel"))
            all_btn = pc_top.add(ttk.Button(pc_top, text="Set up everything for this computer",
                                            style="Accent.TButton"))
            pc_note = ttk.Label(pc, text="", style="Muted.TLabel", wraplength=px(760), justify="left")
            pc_note.pack(anchor="w", pady=(px(4), 0))

            def make_shortcut(lg):
                runner = _gui_python(cfg.get("python_path") or sys.executable)
                launcher = write_launcher(APP_DIR, runner)
                where = desktop_shortcut(launcher, lg)
                cfg["shortcut_made"] = True
                save_config(cfg)
                return ("Desktop shortcut created." if where else
                        f"Launcher created: {os.path.basename(launcher)} in the program folder.")

            def plan_steps():
                """Everything this computer still needs, in a sensible order."""
                steps = []
                if not theme_installed():
                    steps.append(("Modern look", install_theme))
                if not pdf_support():
                    steps.append(("PDF import support", install_pdf_support))
                if not ffmpeg_installed():
                    steps.append(("FFmpeg (reads audio files)", install_ffmpeg))
                speech_ok = pinfo["supported"] and python_supported()
                if speech_ok and not engine_installed():
                    steps.append(("Speech engine — WhisperX (~2–3 GB)", install_engine))
                if speech_ok:
                    ms = models_status(cfg, token_var.get().strip())
                    if not ms["whisper"] or ms["align"] is False or ms["speaker"] is False:
                        steps.append((f"Speech models — {cfg['whisper_model']} (~1.5 GB)",
                                      lambda lg: download_models(cfg, self.settings.get("hf_token", ""), lg)))
                if not cfg.get("shortcut_made") and not shortcut_exists():
                    steps.append(("Desktop shortcut" if not IS_MAC else "App in Applications + Desktop", make_shortcut))
                return steps

            def describe():
                steps = plan_steps()
                bits = []
                if pinfo["note"]:
                    bits.append("⚠ " + pinfo["note"])
                if pinfo["supported"] and not python_supported():
                    bits.append(f"⚠ Python {sys.version_info[0]}.{sys.version_info[1]} is too new for the speech "
                                "engine — install Python 3.12 (the installer in the setup folder does this), "
                                "then reopen Session Scribe.")
                if steps:
                    bits.append("Still needed here: " + ", ".join(n for n, _ in steps) + ".")
                    pc_top.show(all_btn, True)
                else:
                    bits.append("✓ Everything this computer needs is installed.")
                    pc_top.show(all_btn, False)
                if pinfo["os"] == "mac" and pinfo["supported"]:
                    bits.append("On a Mac, transcription runs on the processor (no NVIDIA graphics), so it's slower "
                                "than on a gaming-class PC — fine for the recording queue. LM Studio runs well on "
                                "Apple Silicon.")
                pc_note.config(text="\n".join(bits))
                return steps

            def run_all(confirm=True):
                steps = plan_steps()
                if not steps:
                    return describe()
                if confirm and not messagebox.askyesno(
                        APP_NAME, f"Session Scribe detected {pinfo['label']}.\n\nSet up everything this computer "
                                  "needs?\n\n" + "\n".join("•  " + n for n, _ in steps)
                                  + "\n\nThis can take 15–30 minutes and downloads a few GB the first time. "
                                    "No patient data is involved.", parent=win):
                    return
                all_btn.config(state="disabled", text="Setting up…")
                state = {"i": 0, "failed": []}

                def next_step():
                    if state["i"] >= len(steps):
                        cfg["setup_assistant_done"] = True
                        save_config(cfg)
                        if all_btn.winfo_exists():
                            all_btn.config(state="normal", text="Set up everything for this computer")
                        log("✓ Finished setting up." + (" Problems: " + ", ".join(state["failed"]) +
                                                   " — see above." if state["failed"] else
                                                   " Everything is installed."))
                        refresh()
                        self.refresh_strip()
                        return
                    name, fn = steps[state["i"]]
                    state["i"] += 1
                    if "Speech models" in name and not engine_installed():
                        state["failed"].append(name)
                        log("✗ Skipped the speech models — the speech engine isn't installed.")
                        return next_step()
                    log(f"→ {name}…")

                    def work(progress):
                        return fn(lambda t: self.msgs.put(("setuplog", t)))

                    def done(msg):
                        log("✓ " + (msg or name))
                        next_step()

                    def fail(msg):
                        state["failed"].append(name)
                        log(f"✗ {name}: {msg}")
                        next_step()
                    if not self.run_bg(work, done, fail, label=f"Settings: {name}", quiet=True):
                        state["i"] -= 1
                        log("Another task is running — setup will continue when it finishes.")
                        root.after(5000, next_step)
                next_step()
            all_btn.config(command=run_all)

            # ---- folders & sync
            fs = ttk.LabelFrame(outer, text="Folders & sync", padding=px(10))
            fs.pack(fill="x", pady=(px(10), 0))
            fs.columnconfigure(1, weight=1)
            folder_lbl = ttk.Label(fs, text="", wraplength=px(560), justify="left")
            prog_lbl = ttk.Label(fs, text="", wraplength=px(560), justify="left")
            ttk.Label(fs, text="Data folder", style="Strong.TLabel").grid(row=0, column=0, sticky="nw", padx=(0, px(12)))
            folder_lbl.grid(row=0, column=1, sticky="ew")
            ttk.Button(fs, text="Change…", command=lambda: change_folder()).grid(row=0, column=2, sticky="e")
            ttk.Separator(fs).grid(row=1, column=0, columnspan=3, sticky="ew", pady=px(8))
            ttk.Label(fs, text="Program", style="Strong.TLabel").grid(row=2, column=0, sticky="nw",
                                                                     padx=(0, px(12)))
            prog_lbl.grid(row=2, column=1, sticky="ew")
            pbf = ttk.Frame(fs)
            pbf.grid(row=2, column=2, sticky="ne")
            ttk.Button(pbf, text="Move…", command=lambda: move_program_dialog()).pack(side="left")
            ttk.Button(pbf, text="Open", command=lambda: open_program_folder()).pack(side="left", padx=(px(4), 0))
            ttk.Separator(fs).grid(row=3, column=0, columnspan=3, sticky="ew", pady=px(8))
            syncrow = ttk.Frame(fs)
            syncrow.grid(row=4, column=0, columnspan=3, sticky="ew")
            sync_btn = ttk.Button(syncrow, text="⟳  Sync now", style="Accent.TButton")
            sync_btn.pack(side="left")
            sync_lbl = ttk.Label(syncrow, text="Checking…", style="Muted.TLabel", wraplength=px(560), justify="left")
            sync_lbl.pack(side="left", fill="x", expand=True, padx=(px(12), 0))
            sync_more = ttk.Label(fs, text="", style="Muted.TLabel", wraplength=px(760), justify="left")
            sync_more.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(px(4), 0))
            # ---- the other computer can process recordings for this one (and vice versa)
            ttk.Separator(fs).grid(row=6, column=0, columnspan=3, sticky="ew", pady=px(8))
            ttk.Label(fs, text="Other\ncomputers", style="Strong.TLabel", justify="left").grid(
                row=7, column=0, sticky="nw", padx=(0, px(12)))
            oc = ttk.Frame(fs)
            oc.grid(row=7, column=1, columnspan=2, sticky="ew")
            remote_var = tk.BooleanVar(value=bool(cfg.get("remote_accept", True)))
            notify_var = tk.BooleanVar(value=bool(cfg.get("remote_notify", True)))

            def save_remote():
                cfg["remote_accept"], cfg["remote_notify"] = remote_var.get(), notify_var.get()
                save_config(cfg)
                self._rem()["beat"] = 0                 # tell the other computer right away
            ttk.Checkbutton(oc, text="Run recordings sent from my other computer", variable=remote_var,
                            command=save_remote).pack(anchor="w")
            ttk.Checkbutton(oc, text="Notify me when they're done", variable=notify_var,
                            command=save_remote).pack(anchor="w", pady=(px(2), 0))
            hosts_lbl = ttk.Label(oc, text="", style="Muted.TLabel", wraplength=px(600), justify="left")
            hosts_lbl.pack(fill="x", pady=(px(4), 0))

            def show_hosts():
                if not hosts_lbl.winfo_exists():
                    return
                hs = self._rem()["hosts"]
                hosts_lbl.config(text=("\n".join(f"• {h.get('computer', h['id'])}: {self.remote_ready_text(h)}"
                                                 for h in hs) if hs else
                                       "No other computer has opened this data folder yet. Once both computers "
                                       "use the same synced folder and have Session Scribe open, the recording "
                                       "queue can send recordings from one to the other (encrypted).")
                                 + "\nThe computer doing the work must have Session Scribe open (locked is "
                                   "fine) and the speech engine set up.")
                win.after(15_000, show_hosts)
            show_hosts()

            def show_sync(res):
                if not sync_lbl.winfo_exists():
                    return
                at = res["at"].strftime("%I:%M %p").lstrip("0")
                sync_lbl.config(text=("✓  " if res["ok"] else "⚠  ") + f"{res['summary']}  ·  checked {at}",
                                style="Good.TLabel" if res["ok"] else "Warn.TLabel")
                sync_more.config(text="\n".join("• " + d for d in res["details"]))
                sync_btn.config(state="normal", text="⟳  Sync now")

            def do_sync():
                sync_btn.config(state="disabled", text="Syncing…")
                sync_lbl.config(text="Checking both computers' files…", style="Muted.TLabel")
                self.sync_now(show_sync)
            sync_btn.config(command=do_sync)

            prefs = ttk.LabelFrame(outer, text="Preferences", padding=10)
            sv = {k: tk.StringVar(value=str(cfg[k])) for k in
                  ("lmstudio_url", "model", "whisper_model", "language", "max_chars", "previous_notes",
                   "autolock_minutes", "min_speakers", "max_speakers")}
            orient_var = tk.StringVar(value=self.settings.get("orientation", ""))
            token_var = tk.StringVar(value=self.settings.get("hf_token", ""))
            delete_var = tk.BooleanVar(value=bool(cfg.get("delete_audio", True)))
            smooth_var = tk.BooleanVar(value=bool(cfg.get("smooth_flicker", True)))
            sys_name = "Match macOS" if IS_MAC else "Match Windows" if IS_WIN else "Match system"
            APPEARANCE = {"system": sys_name, "light": "Light", "dark": "Dark"}
            appear_var = tk.StringVar(value=APPEARANCE.get(cfg.get("appearance", "system"), sys_name))
            logbox = register(tk.Text(outer, height=7, wrap="word", state="disabled", relief="flat",
                                      borderwidth=0, highlightthickness=1, font=F["mono"], padx=px(10),
                                      pady=px(8)), "log")

            def log(t):
                if logbox.winfo_exists():
                    logbox.config(state="normal")
                    logbox.insert("end", t + "\n")
                    logbox.see("end")
                    logbox.config(state="disabled")

            def task(fn):
                def work(progress):
                    return fn(lambda t: self.msgs.put(("setuplog", t)))

                def done(msg):
                    log("✓ " + msg)
                    refresh()
                    self.refresh_strip()

                def fail(msg):
                    log("✗ " + msg)
                    refresh()
                if not self.run_bg(work, done, fail, label="Settings", quiet=True):
                    log("Another task is running — try again when it finishes.")

            def switch_python(py, ver, has):
                v = f"{ver[0]}.{ver[1]}"
                note = "" if has else "\n\nWhisperX will also be installed into it (~2-3 GB, can take 10+ minutes)."
                if not messagebox.askyesno(APP_NAME, f"Set Session Scribe up to run on Python {v}?\n\n{py}{note}"
                                                     "\n\nNo patient data is involved.", parent=win):
                    return

                def work(progress):
                    return create_launcher(py, ver, has, lambda t: self.msgs.put(("setuplog", t)))

                def done(res):
                    name = os.path.basename(res["launcher"])
                    log(f"✓ Done. From now on the app switches to Python {res['version']} by itself whenever you "
                        f"open it, however you open it. (\"{name}\" in the app folder opens it directly, too.)")
                    if messagebox.askyesno(APP_NAME, f"Ready. Restart Session Scribe on Python {res['version']} now?"
                                                     "\n\nYou'll be asked for your vault password again.", parent=win):
                        if self.wizard and self.wizard.get("dirty") and not messagebox.askyesno(
                                APP_NAME, "You have an unsaved session. Restart anyway?", parent=win):
                            return
                        subprocess.Popen([res["runner"], os.path.abspath(__file__)])
                        self.clear_clipboard()
                        root.destroy()

                def fail(msg):
                    log("✗ " + msg)
                if not self.run_bg(work, done, fail, label="Settings", quiet=True):
                    log("Another task is running — try again when it finishes.")

            def change_folder():
                if self.busy or self.waitlist or (self.wizard and self.wizard.get("dirty")) or \
                        (self.parked and self.parked.get("dirty")):
                    return messagebox.showinfo(APP_NAME, "Finish or save the current session (and let any running "
                                                         "task finish) before changing the data folder.", parent=win)

                def chosen(note):
                    msg = (note + "\n\n" if note else "") + "Session Scribe will now restart to open that folder."
                    messagebox.showinfo(APP_NAME, msg)
                    self.restart_app()
                choose_data_folder(win, chosen, first_run=False)

            def open_program_folder():
                try:
                    open_path(APP_DIR)
                except Exception as e:
                    messagebox.showinfo(APP_NAME, f"The program is in:\n{APP_DIR}\n\n({e})", parent=win)

            def move_program_dialog():
                if self.busy or self.waitlist or (self.wizard and self.wizard.get("dirty")) or \
                        (self.parked and self.parked.get("dirty")):
                    return messagebox.showinfo(APP_NAME, "Finish or save the current session (and let any running "
                                                         "task finish) before moving the program.", parent=win)
                dlg = new_window(win, "Move Session Scribe", "720x430")
                dlg.grab_set()
                f = ttk.Frame(dlg, padding=(px(28), px(22)))
                f.pack(fill="both", expand=True)
                ttk.Label(f, text="Move the program to a permanent folder", style="H2.TLabel").pack(anchor="w")
                droot = data_root(cfg)
                data_too = _inside(droot, APP_DIR) and folder_has_vault(droot)
                ttk.Label(f, text=f"Now in: {APP_DIR}", style="Muted.TLabel", wraplength=px(620),
                          justify="left").pack(anchor="w", pady=(px(4), px(10)))
                ttk.Label(f, text="New folder", style="Caption.TLabel").pack(anchor="w")
                row = ttk.Frame(f)
                row.pack(fill="x", pady=(px(2), px(10)))
                dest = tk.StringVar(value=default_program_dir())
                ttk.Entry(row, textvariable=dest, width=56).pack(side="left", fill="x", expand=True)

                def browse():
                    d = filedialog.askdirectory(parent=dlg, title="Choose where to keep Session Scribe",
                                                mustexist=False)
                    if d:
                        d = os.path.normpath(d)
                        if os.path.basename(d).lower() != "session scribe":
                            d = os.path.join(d, "Session Scribe")
                        dest.set(d)
                ttk.Button(row, text="Browse…", command=browse).pack(side="left", padx=(px(8), 0))
                what = ["• The program, its FFmpeg copy and the launcher move to the new folder; the old "
                        "program files are removed afterwards."]
                data_choice = tk.StringVar(value="")
                if data_too:
                    what.append("• Your patient data is in the program folder right now, so it moves too "
                                "(checked after copying). The old encrypted copy is kept as a dated "
                                "\"vault-backup\" folder until you delete it.")
                elif is_cloud_path(droot):
                    what.append(f"• Your shared Google Drive folder stays linked exactly as it is:\n   {droot}\n"
                                "   Nothing in Drive is moved or changed, so your other computer keeps syncing "
                                "the same patients.")
                else:
                    what.append(f"• Patient data stays where it is ({droot}).")
                what.append("• This computer's settings, Python and speech models aren't affected.")
                ttk.Label(f, text="\n".join(what), style="Muted.TLabel", wraplength=px(620),
                          justify="left").pack(anchor="w", pady=(0, px(8)))
                if data_too:
                    drives = suggest_data_dirs()
                    ttk.Label(f, text="Where should the patient data go?", style="Caption.TLabel").pack(anchor="w")
                    for d in drives:
                        label = (f"Google Drive — use the files already there (from your other computer): {d}"
                                 if folder_has_vault(d) else f"Google Drive, shared with your other computer: {d}")
                        ttk.Radiobutton(f, text=label, value=d, variable=data_choice).pack(anchor="w")
                    ttk.Radiobutton(f, text="This computer only (inside the new program folder)", value="",
                                    variable=data_choice).pack(anchor="w", pady=(0, px(6)))
                    if drives:
                        data_choice.set(drives[0])
                        dlg.geometry(f"{px(720)}x{px(520)}")
                sc = tk.BooleanVar(value=os.name == "nt")
                if os.name == "nt":
                    ttk.Checkbutton(f, text="Put a Session Scribe shortcut on the desktop",
                                    variable=sc).pack(anchor="w")
                err = ttk.Label(f, text="", style="Bad.TLabel", wraplength=px(620), justify="left")
                err.pack(anchor="w", pady=(px(6), 0))

                def go():
                    d = dest.get().strip()
                    if not d:
                        return err.config(text="Choose a folder.")
                    err.config(text="Moving…", style="Muted.TLabel")
                    dlg.update_idletasks()
                    try:
                        target = data_choice.get() or None
                        if target and folder_has_vault(target) and not messagebox.askyesno(
                                APP_NAME, f"Google Drive already has Session Scribe files in\n{target}\n\n"
                                          "Session Scribe will use those, and the copy in your current folder "
                                          "will be set aside as a backup (not merged). Continue?", parent=dlg):
                            return err.config(text="")
                        res = move_program(cfg, d, shortcut=sc.get(), data_target=target)
                    except Exception as e:
                        return err.config(text=f"Couldn't move it: {e}", style="Bad.TLabel")
                    msg = [f"Session Scribe now lives in:\n{res['dir']}"]
                    if res["shortcut"]:
                        msg.append("Open it from the new \"Session Scribe\" shortcut on your desktop.")
                    else:
                        msg.append(f"Open it with \"{os.path.basename(res['launcher'])}\" in that folder.")
                    msg.append("Any old shortcut or pinned taskbar icon still points at Downloads — "
                               "remove it and pin the new one.")
                    if res.get("joined_existing"):
                        msg.append(f"Now using the shared files in {res['data_moved']}. Your previous local "
                                   f"copy was set aside as {res['backup']}.")
                    elif res["data_moved"]:
                        msg.append(f"Patient data moved to {res['data_moved']}.")
                    if res["data_synced"]:
                        msg.append(f"Still linked to your Google Drive folder:\n{res['data_dir']}\n"
                                   "Your other computer keeps seeing the same patients.")
                    if res["left"]:
                        msg.append("Left behind in the old folder (delete once you've checked everything "
                                   "opens):\n" + "\n".join(res["left"]))
                    msg.append("Session Scribe will now restart from the new folder.")
                    dlg.destroy()
                    messagebox.showinfo(APP_NAME, "\n\n".join(msg))
                    self.restart_app(res["script"])

                btns = pin_bottom(ttk.Frame(f), pady=(px(14), 0))
                ttk.Button(btns, text="Move", style="Accent.TButton", command=go).pack(side="right", ipadx=px(12))
                ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right", padx=px(8))
                dlg._test = {"dest": dest, "go": go, "err": err, "shortcut": sc, "data": data_choice}
                root._move_dialog = dlg

            def load_manual():
                p = filedialog.askopenfilename(parent=win, title="Choose your SSC Notes Manual or Externship Manual",
                                               filetypes=[("Manual", "*.docx *.pdf *.txt"), ("All files", "*.*")])
                if not p:
                    return

                def work(progress):
                    return parse_reference(extract_text(p))

                def done(found):
                    if not (found["examples"] or found["vocabulary"] or found["intake_points"]):
                        log("✗ Couldn't find the example progress notes, vocabulary or intake discussion points in "
                            "that file. Try the .docx version of the Notes Manual.")
                        return
                    old = self.settings.get("reference") or BUILTIN_REFERENCE
                    merged = {k: found[k] or old.get(k) for k in ("examples", "vocabulary", "intake_points")}
                    srcs = [x for x in (self.settings.get("reference") or {}).get("source", "").split(" + ") if x]
                    name = os.path.basename(p)
                    merged["source"] = " + ".join(dict.fromkeys(srcs + [name]))
                    merged["loaded"] = datetime.now().isoformat(timespec="seconds")
                    self.settings["reference"] = merged
                    vault.save_settings(self.settings)
                    log(f"✓ Loaded {name}: {len(found['examples'])} example notes, vocabulary "
                        f"{'found' if found['vocabulary'] else 'not found'}, intake discussion points "
                        f"{'found' if found['intake_points'] else 'not found'}.")
                    refresh()

                def fail(msg):
                    log("✗ " + msg)
                if not self.run_bg(work, done, fail, label="Settings", quiet=True):
                    log("Another task is running — try again when it finishes.")

            def remove_manual():
                if messagebox.askyesno(APP_NAME, "Go back to the built-in SSC manual excerpts?", parent=win):
                    self.settings.pop("reference", None)
                    vault.save_settings(self.settings)
                    log("✓ Removed.")
                    refresh()

            def save_prefs(quiet=False):
                url = sv["lmstudio_url"].get().strip()
                try:
                    assert_local_url(url)
                    new = {"lmstudio_url": url, "model": sv["model"].get().strip(),
                           "whisper_model": sv["whisper_model"].get(), "language": sv["language"].get().strip(),
                           "max_chars": max(4000, int(sv["max_chars"].get())),
                           "previous_notes": max(0, int(sv["previous_notes"].get())),
                           "smooth_flicker": smooth_var.get(),
                           "autolock_minutes": max(0, int(sv["autolock_minutes"].get())),
                           "min_speakers": max(1, int(sv["min_speakers"].get())),
                           "max_speakers": max(1, int(sv["max_speakers"].get())),
                           "delete_audio": delete_var.get(),
                           "role_labels": ", ".join([you_var.get().strip() or "Clinician",
                                                     them_var.get().strip() or "Patient"]
                                                    + [x.strip() for x in extra_var.get().split(",") if x.strip()]),
                           "appearance": next((k for k, v in APPEARANCE.items() if v == appear_var.get()), "system"),
                           "date_format": next((k for k, v in DATE_FORMATS.items() if v == datefmt_var.get()), "iso"),
                           "remote_accept": remote_var.get()}
                except NetworkBlocked as e:
                    return messagebox.showerror(APP_NAME, str(e), parent=win)
                except ValueError:
                    return messagebox.showerror(APP_NAME, "Numbers only in the number fields.", parent=win)
                new["max_speakers"] = max(new["max_speakers"], new["min_speakers"])
                theme_changed = new["appearance"] != cfg.get("appearance", "system")
                dates_changed = new["date_format"] != cfg.get("date_format", "iso")
                cfg.update(new)
                save_config(cfg)
                set_date_format(cfg["date_format"])
                if theme_changed:
                    apply_theme(cfg["appearance"])
                if theme_changed or dates_changed:
                    self.redraw()
                    qw = getattr(self, "queue_win", None)
                    if qw and self._alive(qw["win"]):
                        qw["refresh"]()
                self.settings["orientation"] = orient_var.get().strip()
                self.settings["hf_token"] = token_var.get().strip()
                vault.save_settings(self.settings)
                if not quiet:
                    log("✓ Saved.")
                self.check_lmstudio()
                refresh()

            def refresh():
                for c in checks.winfo_children():
                    c.destroy()
                ms = models_status(cfg, token_var.get().strip())
                lm = self.lm_status
                cur = f"{sys.version_info[0]}.{sys.version_info[1]}"
                yn = {True: "✓ downloaded", False: "✗ missing", None: "• n/a"}
                model_detail = (f"Speech model ({cfg['whisper_model']}): {yn[ms['whisper']]}\n"
                                f"Word alignment: {yn[ms['align']] if ms['align'] is not None else '• checked at first use'}\n"
                                f"Speaker model: {yn[ms['speaker']] if ms['speaker'] is not None else '• off (no token)'}")
                others = [m for m in ms["cached"] if m != cfg["whisper_model"]]
                if others:
                    model_detail += "\nAlso on this computer: " + ", ".join(others)
                if not engine_installed():
                    model_detail += "\nInstall the speech engine first — downloads need it."
                models_ok = ms["whisper"] and ms["align"] is not False and ms["speaker"] is not False

                def use_model(m):
                    sv["whisper_model"].set(m)
                    save_prefs(quiet=True)
                    log(f"✓ Switched the speech model to {m} (already downloaded).")

                def start_download():
                    if not engine_installed():
                        return log("✗ Install the speech engine (WhisperX) first, then download models.")
                    save_prefs(quiet=True)
                    task(lambda lg: download_models(cfg, self.settings.get("hf_token", ""), lg))
                model_actions = [("Download", start_download)]
                if not ms["whisper"] and others:
                    model_actions.insert(0, (f"Use {others[0]}", lambda m=others[0]: use_model(m)))
                custom = self.settings.get("reference") or {}
                ref = custom or BUILTIN_REFERENCE
                if custom:
                    ref_detail = (f"Using {custom.get('source', 'manual')} (loaded {fmt_date(custom.get('loaded', '')[:10])}): "
                                  f"{len(ref.get('examples', []))} example notes, vocabulary "
                                  f"{'✓' if ref.get('vocabulary') else '✗'}, intake points "
                                  f"{'✓' if ref.get('intake_points') else '✗'}.")
                else:
                    ref_detail = (f"Built in: {len(ref['examples'])} example progress notes, the intervention vocabulary, "
                                  "and the 10 intake discussion points. Nothing to load. If SSC updates its "
                                  "manual, use Load manual… to replace it.")
                if engine_installed():
                    engine_row = ("Speech engine (WhisperX)", True, "Installed.", None)
                elif python_supported():
                    engine_row = ("Speech engine (WhisperX)", False,
                                  "Not installed for the Python running this app.",
                                  ("Install", lambda: messagebox.askyesno(
                                      APP_NAME, "Install WhisperX into this Python? It's a large download "
                                                "(~2-3 GB) and can take a while. No patient data is involved.",
                                      parent=win) and task(install_engine)))
                else:
                    if self._compat is None:
                        self._compat = find_compatible_pythons()
                    if self._compat:
                        py, ver, has = self._compat[0]
                        v = f"{ver[0]}.{ver[1]}"
                        engine_row = ("Speech engine (WhisperX)", False,
                                      f"This app is running on Python {cur}, which WhisperX doesn't support yet "
                                      f"(it needs 3.10–3.13). Found Python {v}"
                                      + (" with WhisperX already installed" if has else "")
                                      + f" at {py}. One click sets the app up to use it.",
                                      (f"Use Python {v}", lambda: switch_python(py, ver, has)))
                    else:
                        engine_row = ("Speech engine (WhisperX)", False,
                                      f"This app is running on Python {cur}, which WhisperX doesn't support yet. "
                                      "Install Python 3.12 from python.org, then reopen Settings.", None)
                droot = data_root(cfg)
                folder_detail = (f"{droot}\n" + ("Synced by Google Drive, so your other computer sees the same patients, "
                                                 "notes and templates (encrypted)." if is_cloud_path(droot) else
                                                 "On this computer only.")
                                 + f"\nThis computer's own settings (Python, LM Studio, speech model, look) are in "
                                   f"{LOCAL_DIR}.")
                dl = in_downloads(APP_DIR)
                prog_detail = (f"{APP_DIR}\n" + ("This is your Downloads folder — easy to clear out by accident. "
                                                  "Move it to a permanent folder." if dl else
                                                  "The program itself (not your patient data)."))
                folder_lbl.config(text=folder_detail)
                prog_lbl.config(text=prog_detail, style="Warn.TLabel" if dl else "TLabel")
                rows = [
                    engine_row,
                    ("FFmpeg (reads audio files)", ffmpeg_installed(),
                     "Found." if ffmpeg_installed() else "Not found.",
                     None if ffmpeg_installed() else ("Install", lambda: task(install_ffmpeg))),
                    ("Speaker labels", bool(token_var.get().strip()) or None,
                     "Token saved." if token_var.get().strip() else
                     "Optional. Paste a free Hugging Face token below, accept the pyannote model terms "
                     "on huggingface.co, then click Download.", None),
                    ("Speech models", models_ok, model_detail, model_actions),
                    ("LM Studio (writes the notes)",
                     None if lm is None else (isinstance(lm, list) and bool(lm)),
                     "Checking…" if lm is None else (
                         ("Connected. Loaded: " + ", ".join(lm)) if isinstance(lm, list) and lm else
                         ("Running, but no model is loaded." if isinstance(lm, list) else
                          "Not reachable. Open LM Studio, load a model (set context length to 12k+), "
                          "and start the server in the Developer tab.")),
                     ("Check again", self.check_lmstudio)),
                    ("PDF support (for importing PDFs)", pdf_support() or None,
                     "Installed." if pdf_support() else "Optional — only needed to import .pdf files.",
                     None if pdf_support() else ("Install", lambda: task(install_pdf_support))),
                ]
                # Show only what needs attention: missing, broken, or not set up yet.
                # (LM Studio "checking…" isn't a problem, so it stays hidden too.)
                rows = [row for row in rows if row[1] is not True
                        and not (row[0].startswith("LM Studio") and lm is None)]
                manual_lbl.config(text=ref_detail)
                describe()
                pb.show(manual_builtin, bool(custom))
                if not rows:
                    ttk.Label(checks, text="✓  Everything is set up and working.", style="Good.TLabel").grid(
                        row=0, column=0, columnspan=3, sticky="w", pady=(0, px(4)))
                for r, (name, ok, detail, action) in enumerate(rows):
                    icon, st = (("✓", "Good.TLabel") if ok is True else ("•", "Muted.TLabel") if ok is None
                                else ("✗", "Bad.TLabel"))
                    if r:
                        ttk.Separator(checks).grid(row=r * 3 - 1, column=0, columnspan=3, sticky="ew",
                                                   pady=(0, px(8)))
                    ttk.Label(checks, text=icon, style=st, font=F["icon"]).grid(row=r * 3, column=0, sticky="nw",
                                                                                padx=(0, px(10)))
                    ttk.Label(checks, text=name, style="RowTitle.TLabel").grid(row=r * 3, column=1, sticky="w")
                    ttk.Label(checks, text=detail, style="Muted.TLabel", wraplength=px(520),
                              justify="left").grid(row=r * 3 + 1, column=1, sticky="ew", pady=(0, 8))
                    acts = action if isinstance(action, list) else ([action] if action else [])
                    if acts:
                        bf = Flow(checks, align="right", gap=px(4))
                        bf.grid(row=r * 3, column=2, rowspan=2, sticky="ne", padx=(px(8), 0))
                        for label, cmd in acts:
                            bf.add(ttk.Button(bf, text=label, command=cmd))

            prefs.pack(fill="x", pady=(10, 8))
            prefs.columnconfigure(1, weight=1)
            prefs.columnconfigure(3, weight=1)

            # settings grid: two label/field pairs per line, or one per line when the window is narrow
            pg = ttk.Frame(prefs)
            pg.grid(row=0, column=0, columnspan=4, sticky="ew")
            pref_items, pref_seps, pref_cols = [], [], {"n": None}

            def pr(r, c, label, widget):
                lab = ttk.Label(pg, text=label, wraplength=px(190), justify="left") if label else None
                pref_items.append((lab, widget))

            def lay_prefs(_e=None):
                n = 2 if pg.winfo_width() >= px(720) or pg.winfo_width() <= 1 else 1
                if n == pref_cols["n"]:
                    return
                pref_cols["n"] = n
                for sp in pref_seps:
                    sp.destroy()
                pref_seps.clear()
                for c in range(4):
                    pg.columnconfigure(c, weight=0)
                pg.columnconfigure(1, weight=1)
                if n == 2:
                    pg.columnconfigure(3, weight=1)
                for i, (lab, w) in enumerate(pref_items):
                    line, col = divmod(i, n)
                    if line and col == 0:
                        sp = ttk.Separator(pg)
                        sp.grid(row=line * 2 - 1, column=0, columnspan=4, sticky="ew", pady=px(2))
                        pref_seps.append(sp)
                    if lab is None:
                        w.grid(row=line * 2, column=col * 2, columnspan=2, sticky="ew", pady=px(4), padx=(0, 16))
                        continue
                    lab.grid(row=line * 2, column=col * 2, sticky="w", pady=px(4), padx=(0, 6))
                    w.grid(row=line * 2, column=col * 2 + 1, sticky="ew", pady=px(4), padx=(0, 16))
            pg.bind("<Configure>", lay_prefs, add="+")
            pr(0, 0, "Theoretical orientation", ttk.Entry(pg, textvariable=orient_var))
            pr(0, 2, "Hugging Face token", ttk.Entry(pg, textvariable=token_var, show="•"))
            pr(1, 0, "Speech model", ttk.Combobox(pg, textvariable=sv["whisper_model"],
                                                  values=WHISPER_MODELS, state="readonly"))
            pr(1, 2, "Language (blank = auto)", ttk.Entry(pg, textvariable=sv["language"], width=6))
            spk = ttk.Frame(pg)
            ttk.Spinbox(spk, from_=1, to=6, textvariable=sv["min_speakers"], width=4).pack(side="left")
            ttk.Label(spk, text=" to ").pack(side="left")
            ttk.Spinbox(spk, from_=1, to=6, textvariable=sv["max_speakers"], width=4).pack(side="left")
            pr(2, 0, "People in session", spk)
            pr(2, 2, "Previous notes to build on", ttk.Spinbox(pg, from_=0, to=6,
                                                               textvariable=sv["previous_notes"], width=4))
            pr(3, 0, "Auto-lock (minutes, 0 = off)", ttk.Entry(pg, textvariable=sv["autolock_minutes"], width=6))
            pr(3, 2, "Condense transcripts over (chars)", ttk.Entry(pg, textvariable=sv["max_chars"], width=8))
            pr(4, 0, "LM Studio address", ttk.Entry(pg, textvariable=sv["lmstudio_url"]))
            pr(4, 2, "AI model (blank = loaded one)", ttk.Entry(pg, textvariable=sv["model"]))
            pr(5, 0, "Appearance", ttk.Combobox(pg, textvariable=appear_var, values=list(APPEARANCE.values()),
                                                state="readonly", width=16))
            datefmt_var = tk.StringVar(value=DATE_FORMATS.get(cfg.get("date_format", "iso"), DATE_FORMATS["iso"]))
            pr(5, 2, "Date format", ttk.Combobox(pg, textvariable=datefmt_var, values=list(DATE_FORMATS.values()),
                                                 state="readonly", width=16))
            labels_now = role_labels(cfg)
            you_var = tk.StringVar(value=labels_now[0])
            them_var = tk.StringVar(value=labels_now[1])
            extra_var = tk.StringVar(value=", ".join(labels_now[2:]))
            pr(6, 0, "Your label in transcripts", ttk.Combobox(pg, textvariable=you_var, width=16,
                                                             values=["Clinician", "Therapist", "Counselor"]))
            pr(6, 2, "Label for the person you see", ttk.Combobox(pg, textvariable=them_var, width=16,
                                                                values=["Patient", "Client"]))
            pr(7, 0, "Other speaker labels", ttk.Entry(pg, textvariable=extra_var))
            pr(7, 2, None, ttk.Label(pg, text="Comma-separated, e.g. Parent/Guardian, Partner, Other. Used in Who's who and "
                                  "transcripts; earlier transcripts keep their labels.",
                      style="Caption.TLabel", wraplength=px(360), justify="left"))
            lay_prefs()
            ttk.Separator(prefs).grid(row=15, column=0, columnspan=4, sticky="ew", pady=px(2))
            ttk.Checkbutton(prefs, text="Offer to delete the recording after saving a session",
                            variable=delete_var).grid(row=16, column=0, columnspan=4, sticky="w", pady=(px(6), 0))
            ttk.Checkbutton(prefs, text="Smooth one-word speaker flicker (diarization blips)",
                            variable=smooth_var).grid(row=17, column=0, columnspan=4, sticky="w", pady=(px(4), 0))
            ttk.Separator(prefs).grid(row=18, column=0, columnspan=4, sticky="ew", pady=(px(8), 0))
            pb = Flow(prefs, gap=px(6))
            pb.grid(row=19, column=0, columnspan=4, sticky="ew", pady=(px(10), 0))
            pb.add(ttk.Button(pb, text="Save preferences", style="Accent.TButton", command=save_prefs))

            def open_templates():
                try:
                    open_path(TEMPLATE_DIR)
                except Exception:
                    pass
            pb.add(ttk.Button(pb, text="Edit note templates…", command=open_templates))
            pb.add(ttk.Button(pb, text="Load newer SSC manual…", command=load_manual))
            manual_builtin = pb.add(ttk.Button(pb, text="Use built-in manual", command=remove_manual))
            pb.show(manual_builtin, False)
            manual_lbl = ttk.Label(prefs, text="", style="Caption.TLabel", wraplength=px(760), justify="left")
            manual_lbl.grid(row=20, column=0, columnspan=4, sticky="ew", pady=(px(6), 0))

            logbox.pack(fill="both", expand=True)

            def close():
                self.setup_win = None
                win.destroy()
                self.refresh_strip()
                if self.page is not None and self.wizard is None and self.client_id is None:
                    self.show_home()
            win.protocol("WM_DELETE_WINDOW", close)
            self.setup_win = {"win": win, "log": log, "refresh": refresh, "sync": do_sync, "run_all": run_all,
                              "plan": plan_steps, "describe": describe}
            refresh()
            do_sync()
            # first time Setup opens on this computer: detect it and offer to install everything at once
            if not cfg.get("setup_assistant_offered"):
                cfg["setup_assistant_offered"] = True
                save_config(cfg)
                if plan_steps():
                    win.after(700, lambda: win.winfo_exists() and run_all())

        # ========================================================== LOCKING
        def lock(self):
            if self.locked:
                return
            self.locked = True
            self.clear_clipboard()
            if self._alive(getattr(self, "_toast", None)):
                self._toast.destroy()                  # it can name a patient
            if self.setup_win is not None:
                self.setup_win["win"].withdraw()
            qwin = getattr(self, "queue_win", None)
            q_was_open = bool(qwin and self._alive(qwin["win"]) and qwin["win"].state() != "withdrawn")
            if q_was_open:
                qwin["win"].withdraw()
            f = ttk.Frame(root)
            f.place(relx=0, rely=0, relwidth=1, relheight=1)
            f.lift()
            box = ttk.Frame(f, style="Card.TFrame", padding=(px(40), px(30)))
            box.place(relx=0.5, rely=0.45, anchor="center")
            ttk.Label(box, text="🔒", font=F["hero"]).pack(anchor="w")
            ttk.Label(box, text=f"{APP_NAME} is locked", style="Title.TLabel").pack(anchor="w", pady=(px(4), px(2)))
            ttk.Label(box, text="Patient information is hidden. Enter your vault password.",
                      style="Muted.TLabel").pack(anchor="w", pady=(0, px(16)))
            pw = ttk.Entry(box, show="•", width=36)
            pw.pack(fill="x")
            err = ttk.Label(box, text="", style="Bad.TLabel")
            err.pack(anchor="w", pady=px(6))
            q_label = ttk.Label(box, text="", style="Muted.TLabel")      # no patient codes on this screen
            q_label.pack(anchor="w", pady=(0, px(6)))

            def q_tick():
                if not f.winfo_exists():
                    return
                Q = self._q()
                done, total = self.queue_counts()
                q_label.config(text=(f"Recording queue: {done} of {total} finished — still working while locked."
                                     if Q["running"] else f"Recording queue finished: {done} of {total}."
                                     if total else ""))
                f.after(3000, q_tick)
            q_tick()

            def attempt(_=None):
                if vault.verify(pw.get()):
                    f.destroy()
                    self.locked = False
                    self.last_activity = time.time()
                    if self.setup_win is not None:
                        self.setup_win["win"].deiconify()
                    if q_was_open and self._alive(qwin["win"]):
                        qwin["win"].deiconify()
                else:
                    err.config(text="Incorrect password.")
                    pw.delete(0, "end")
            ttk.Button(box, text="Unlock", style="Accent.TButton", command=attempt).pack(fill="x", ipady=px(3))
            pw.bind("<Return>", attempt)
            pw.focus_set()

        def idle_check(self):
            mins = int(cfg.get("autolock_minutes", 0) or 0)
            if mins and not self.locked and time.time() - self.last_activity > mins * 60:
                self.lock()
            root.after(15_000, self.idle_check)

        def on_close(self):
            Q = self._q()
            left = sum(1 for it in Q["items"] if it["status"] in ("waiting", "transcribing", "writing"))
            if Q["running"] and left and not messagebox.askyesno(
                    APP_NAME, f"The recording queue still has {left} recording(s) to do. Quit anyway? "
                              "(Finished ones are already saved.)"):
                return
            if (self.busy or self.waitlist) and not Q["running"] and not messagebox.askyesno(
                    APP_NAME, "A task is still running" + (f" and {len(self.waitlist)} more are waiting in line"
                                                          if self.waitlist else "") + ". Quit anyway?"):
                return
            unsaved = [w for w in (self.wizard, self.parked) if w and w.get("dirty") and not w.get("discarded")]
            if unsaved and not messagebox.askyesno(
                    APP_NAME, f"The {unsaved[0]['client'].get('code', '')} session isn't saved. Quit anyway?"):
                return
            self.cancel_task(None, "Closing.")        # stop the transcription process / LM Studio request too
            self.clear_clipboard()
            root.destroy()

    def start():
        root._app = App()

    # ------------------------------------------------------- data folder
    def choose_data_folder(parent, on_chosen, first_run=True, warning=""):
        """Pick where patient data, templates and note settings are kept.
        Used once per computer, and from Setup → Change…"""
        holder = ttk.Frame(parent) if first_run else new_window(parent, "Where to keep your files", "760x620")
        if first_run:
            holder.place(relx=0.5, rely=0.45, anchor="center")
        else:
            holder.grab_set()
        f = ttk.Frame(holder, style="Card.TFrame" if first_run else "TFrame", padding=(px(36), px(28)))
        f.pack(fill="both", expand=True)
        current = data_root(cfg)
        old_has_data = folder_has_vault(current)
        suggestions = suggest_data_dirs()
        ttk.Label(f, text="☁", font=F["hero"]).pack(anchor="w")
        ttk.Label(f, text="Where should Session Scribe keep its files?", style="Title.TLabel").pack(
            anchor="w", pady=(px(4), px(2)))
        ttk.Label(f, text="Pick your Google Drive folder to use the same patients, notes and templates on both "
                          "computers. Everything is encrypted before it's saved, so Drive only ever stores "
                          "scrambled files. Transcription and note writing always run on the computer you're using.",
                  style="Muted.TLabel", wraplength=px(560), justify="left").pack(anchor="w", pady=(0, px(16)))
        if warning:
            ttk.Label(f, text="⚠ " + warning, style="Bad.TLabel", wraplength=px(560),
                      justify="left").pack(anchor="w", pady=(0, px(12)))
        ttk.Label(f, text="Shared folder", style="Caption.TLabel").pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(px(2), px(8)))
        default = cfg.get("data_dir") or (suggestions[0] if suggestions else os.path.join(APP_DIR, "data"))
        path_var = tk.StringVar(value=default)
        ttk.Entry(row, textvariable=path_var, width=58).pack(side="left", fill="x", expand=True)

        def browse():
            d = filedialog.askdirectory(parent=holder.winfo_toplevel(), title="Choose the shared folder",
                                        initialdir=os.path.dirname(path_var.get()) or None, mustexist=False)
            if d:
                d = os.path.normpath(d)
                if not folder_has_vault(d) and os.path.basename(d).lower() != "session scribe" \
                        and not os.listdir(d) == []:
                    d = os.path.join(d, "Session Scribe")
                path_var.set(d)
        ttk.Button(row, text="Browse…", command=browse).pack(side="left", padx=(px(8), 0))
        info = ttk.Label(f, text="", style="Muted.TLabel", wraplength=px(560), justify="left")
        info.pack(anchor="w", pady=(0, px(6)))
        move_var = tk.BooleanVar(value=True)
        move_cb = ttk.Checkbutton(f, text="Move my existing patients and sessions there (a backup copy is kept)",
                                  variable=move_var)
        err = ttk.Label(f, text="", style="Bad.TLabel", wraplength=px(560), justify="left")

        def describe(*_):
            d = path_var.get().strip()
            move_cb.pack_forget()
            if not d:
                return info.config(text="")
            if folder_has_vault(d):
                info.config(text="✓ Session Scribe files are already in this folder (for example, from your other "
                                 "computer). You'll unlock them with the same vault password.", style="Good.TLabel")
            else:
                cloud = "This is a cloud-synced folder. " if is_cloud_path(d) else \
                    "This folder isn't in Google Drive, so files will stay on this computer only. "
                info.config(text=cloud + ("A new, empty Session Scribe folder will be set up here." if not old_has_data
                                          else ""), style="Muted.TLabel")
                if old_has_data and os.path.normcase(os.path.abspath(d)) != os.path.normcase(os.path.abspath(current)):
                    move_cb.pack(anchor="w", pady=(0, px(6)), after=info)
        path_var.trace_add("write", describe)
        describe()
        err.pack(anchor="w")
        if suggestions:
            ttk.Label(f, text="Found Google Drive: " + "  ·  ".join(suggestions), style="Caption.TLabel",
                      wraplength=px(560), justify="left").pack(anchor="w", pady=(px(4), 0))

        def use_local():
            path_var.set(current if old_has_data else os.path.join(APP_DIR, "data"))

        def confirm():
            d = os.path.normpath(path_var.get().strip())
            if not d or d == ".":
                return err.config(text="Choose a folder.")
            same = os.path.normcase(os.path.abspath(d)) == os.path.normcase(os.path.abspath(current))
            if warning and not folder_has_vault(d) and not messagebox.askyesno(
                    APP_NAME, f"There are no Session Scribe files in\n{d}\n\nStart a new, empty set of "
                              "patients there? (Choose No if your files are somewhere else, or if Google "
                              "Drive is still starting up.)", parent=holder.winfo_toplevel()):
                return
            try:
                os.makedirs(d, exist_ok=True)
                if not folder_has_vault(d) and old_has_data and not same and move_var.get():
                    backup = move_data(current, d)
                    note = f"Moved. The old copy was kept as {backup} — delete it once you're happy." if backup else ""
                elif folder_has_vault(d) and old_has_data and not same:
                    note = (f"Using the files already in {d}. Your previous files at {current} were left "
                            "untouched.")
                else:
                    note = ""
            except Exception as e:
                return err.config(text=f"Couldn't use that folder: {e}")
            cfg["data_dir"] = d
            cfg["vault_dir"] = ""
            cfg["_vault_derived"] = True
            set_data_paths(cfg)
            save_config(cfg)
            on_chosen(note)

        btns = pin_bottom(ttk.Frame(f), pady=(px(16), 0))
        ttk.Button(btns, text="Continue", style="Accent.TButton", command=confirm).pack(side="right", ipadx=px(12))
        ttk.Button(btns, text="Keep files on this computer only", command=use_local).pack(side="left")
        if not first_run:
            ttk.Button(btns, text="Cancel", command=holder.destroy).pack(side="right", padx=px(8))
        holder._test = {"path": path_var, "move": move_var, "confirm": confirm, "info": info, "err": err}
        root._data_chooser = holder
        return holder

    def begin(note=""):
        nonlocal vault
        vault = Vault(cfg["vault_dir"])
        ensure_templates()
        password_screen(start)
        if note:
            messagebox.showinfo(APP_NAME, note)

    missing = cfg.get("data_dir") and not folder_has_vault(cfg["data_dir"]) and (
        not os.path.isdir(cfg["data_dir"]) or count_records(cfg["data_dir"]) == 0 and any(
            folder_has_vault(p) for p in (APP_DIR, os.path.join(APP_DIR, "data"), default_program_dir(),
                                          os.path.join(default_program_dir(), "data"))
            if os.path.normcase(os.path.abspath(p)) != os.path.normcase(os.path.abspath(cfg["data_dir"]))))
    if not cfg.get("data_dir") or missing:      # first start, or the data folder can't be found
        holder = {}

        def chosen(note):
            holder["w"].destroy()
            begin(note)
        warn = ""
        if missing:
            warn = (f"Can't find your Session Scribe files in {cfg['data_dir']}. If that's your Google Drive, "
                    "make sure Google Drive is running and signed in, then click Continue. If you moved the "
                    "folder, choose its new location.")
        holder["w"] = choose_data_folder(root, chosen, warning=warn)
    else:
        begin()
    root.mainloop()


if __name__ == "__main__":
    # pythonw has no console: give WhisperX/tqdm somewhere harmless to print
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    if TRANSCRIBE_MODE:
        transcribe_worker_main()
    if DOWNLOAD_MODE:
        sys.exit(download_models_main())
    relaunch_if_needed()
    ensure_theme_package()
    run_gui()
