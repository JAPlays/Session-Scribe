"""Touchscreen scrolling; session maps from a transcript's own timestamps; Themes never counts speaker labels.

Touch: Windows marks finger/pen input; here (no touchscreen) "Every drag scrolls" stands in for it, and
drags are generated as mouse events — the same events Windows gives Tk for a finger."""
import gc
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H  # noqa: E402

TRANSCRIPT = """[00:00:04] Clinician: How have things been since last week?
[00:00:09] Patient: Honestly I've been anxious and worried most days, and work has been really stressful.
I couldn't sleep on Sunday.
[00:01:30] Clinician: That sounds hard. What happened on Sunday?
[00:02:10] Patient: My sister called and we argued again. I felt hopeless and alone after.
[00:04:45] Clinician: What helped, even a little?
[00:05:00] Patient: Walking helped. I felt calmer and a bit proud I went out."""


def drag(h, root, w, dys, x=5, y=5):
    """Press on w, move by each step in dys (pixels, negative = finger moving up), release."""
    rx, ry = w.winfo_rootx() + x, w.winfo_rooty() + y
    w.event_generate("<ButtonPress-1>", x=x, y=y, rootx=rx, rooty=ry)
    cy = 0
    for d in dys:
        cy += d
        w.event_generate("<Motion>", x=x, y=y + cy, rootx=rx, rooty=ry + cy, state=0x100)
        root.update()
    w.event_generate("<ButtonRelease-1>", x=x, y=y + cy, rootx=rx, rooty=ry + cy, state=0x100)
    root.update()


def script(app, root, V, h):
    ss = h.ss
    fn = next(o for o in gc.get_objects() if callable(o) and getattr(o, "__name__", "") == "_from_finger")
    cfg = fn.__closure__[fn.__code__.co_freevars.index("cfg")].cell_contents     # the app's own settings

    # ---- session map and themes from a transcript without saved timings
    d = {"meta": {"kind": "progress", "session_date": "2026-11-01"}, "transcript": TRANSCRIPT,
         "note": "Patient reported anxiety. Patient stated work stress. The patient and clinician reviewed coping."}
    lines = ss.timed_lines(d)
    h.ok("timed lines read from the transcript's timestamps (a wrapped line joins the one before)",
         len(lines) == 6 and lines[1]["start"] == 9.0 and lines[1]["end"] == 90.0
         and "couldn't sleep on Sunday" in lines[1]["text"], lines[:2])
    m = ss.session_map(lines, {}, "Clinician", patient_label="Patient")
    h.ok("…so the session has a session map, with the patient told apart",
         m is not None and m["patient"] == "Patient" and m["approx"] and m["duration"] > 300, m and m["patient"])
    h.ok("…and its heaviest moments found", m and any("hopeless" in hv["quote"] for hv in m["heavy"]), m and m["heavy"])
    h.ok("…while the map's text leaves out pauses it can't know", "Pauses" not in ss.session_map_text(m))
    other = ss.lines_from_transcript("Patient (00:00:05): hi there\nClinician (00:00:10): hello\n")
    h.ok("the 'Speaker (00:00:05): text' style is read too", [l["speaker"] for l in other] == ["Patient", "Clinician"])
    h.ok("no timestamps: no map (as before)", ss.timed_lines({"transcript": "Patient: hi\nClinician: hello"}) == [])
    saved = {"lines": [{"start": 1.0, "end": 3.0, "speaker": "S1", "text": "hi"}], "transcript": TRANSCRIPT}
    h.ok("the transcription's own timings are used when the session has them", ss.timed_lines(saved) is saved["lines"])

    said = ss.patient_speech(d, "Clinician", "Patient")
    h.ok("Themes reads what the patient said in the transcript, not the AI's note",
         "hopeless" in said and "reported" not in said and "How have things" not in said)
    cid = V.save_client({"code": "TP1"})
    note_only = {"meta": {"client_id": cid, "kind": "progress", "session_date": "2026-11-08"},
                 "note": "Patient reported the patient was anxious. Client said therapist helped. Patient patient."}
    sessions = [("s1", dict(d, meta=dict(d["meta"], client_id=cid))), ("s2", note_only), ("s3", note_only)]
    th = ss.session_themes(V.load_client(cid), sessions, "Clinician", patient_label="Patient")
    words = [w for w, *_ in th["words"]]
    h.ok("speaker labels (patient, client, therapist…) never show up as Themes words",
         not {"patient", "client", "therapist", "clinician"} & {w.lower() for w in words}, words)
    th2 = ss.session_themes(V.load_client(cid), sessions, "Counselor Jo", patient_label="Kiddo")
    h.ok("…nor your own labels from Settings", not {"kiddo", "counselor"} & {w.lower() for w, *_ in th2["words"]})

    # ---- touch scrolling on a page
    cfg["touch_scroll"] = "on"
    app.open_setup(); h.pump(root, 2)
    canvas = next((w for w in h.widgets(root) if getattr(w, "_touch_view", None) is w and w.yview()[1] < 1.0), None)
    h.ok("a scrolling page is found", canvas is not None)
    btn = next((b for b in h.widgets(canvas, "TButton") if b.winfo_viewable()), None)
    calls = []
    btn.configure(command=lambda: calls.append(1))
    top = canvas.yview()[0]
    drag(h, root, btn, [-6, -20, -40, -40, -40])
    h.ok("dragging up with a finger scrolls the page down", canvas.yview()[0] > top + 0.01, (top, canvas.yview()))
    h.ok("…and doesn't press the button the drag started on", calls == [] and "pressed" not in btn.state())
    h.pump(root, 0.3)
    h.ok("…and the button's bindings are back to normal afterwards", "TouchPan" not in btn.bindtags())
    pos = canvas.yview()[0]
    drag(h, root, btn, [8, 30, 30])
    h.ok("dragging down scrolls back up", canvas.yview()[0] < pos)
    btn.event_generate("<ButtonPress-1>", x=5, y=5, rootx=btn.winfo_rootx() + 5, rooty=btn.winfo_rooty() + 5)
    btn.event_generate("<ButtonRelease-1>", x=5, y=5, rootx=btn.winfo_rootx() + 5, rooty=btn.winfo_rooty() + 5)
    root.update()
    h.ok("a tap (no drag) still presses the button", calls == [1], calls)

    # ---- touch scrolling the patient list: scrolling doesn't open anyone, a tap does
    import tkinter as tk
    for w in [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]:
        w.destroy()
    for i in range(40):
        V.save_client({"code": f"P{i:02d}"})
    app.refresh_clients(); h.pump(root, 1)
    lb = app.client_lb
    h.ok("lists take finger input first", "TouchPan" in lb.bindtags())
    opened = []
    real_open = app.open_client
    app.open_client = lambda cid, *a, **k: (opened.append(cid), real_open(cid, *a, **k))
    presses = []                                       # presses that reach the list itself (which select)
    lb.bind("<ButtonPress-1>", lambda e: presses.append((e.x, e.y)), add="+")
    sel0, top0 = lb.selection(), lb.yview()[0]
    drag(h, root, lb, [-6, -30, -30, -30], x=20, y=20)
    h.pump(root, 0.5)
    h.ok("dragging the patient list scrolls it", lb.yview()[0] > top0, (top0, lb.yview()))
    h.ok("…without selecting or opening the patient under the finger",
         lb.selection() == sel0 and not opened and presses == [], (lb.selection(), opened, presses))
    lb.event_generate("<ButtonPress-1>", x=20, y=20, rootx=lb.winfo_rootx() + 20, rooty=lb.winfo_rooty() + 20)
    lb.event_generate("<ButtonRelease-1>", x=20, y=20, rootx=lb.winfo_rootx() + 20, rooty=lb.winfo_rooty() + 20)
    h.pump(root, 1)
    h.ok("a tap on a patient reaches the list once, where the finger lifted (that's what selects)",
         presses == [(20, 20)], presses)

    # ---- off: drags are ordinary mouse drags again
    cfg["touch_scroll"] = "off"
    top1 = lb.yview()[0]
    drag(h, root, lb, [-6, -30, -30], x=20, y=40)
    h.ok("with touch scrolling off, a drag doesn't scroll", abs(lb.yview()[0] - top1) < 1e-6)
    h.ok("off on a non-Windows computer by default (automatic = Windows touch only)",
         ss.IS_WIN or ss.DEFAULT_CONFIG["touch_scroll"] == "auto")


H.start(script)
