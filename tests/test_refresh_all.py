"""Refresh all patients (Home): patients already up to date are no longer skipped silently — you're asked
whether to refresh them too (named), the queue follows the patient list, and patients without session
notes are named. Made-up data only."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H


def script(app, root, V, h):
    import tkinter.messagebox as mb
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    ids = {code: V.save_client({"code": code}) for code in ("AA1", "BB2", "CC3", "DD4")}
    app.refresh_clients(); h.pump(root, 0.4)
    state = {"AA1": "new", "BB2": "current", "CC3": "current", "DD4": "none"}
    code_of = {v: k for k, v in ids.items()}

    def fake_work(cid, force=False):
        code = code_of[cid]
        st = state[code]
        needs = st == "new" or (force and st == "current")
        return {"id": cid, "code": code, "bundles": [], "plan": [], "n_ins": 1 if needs else 0,
                "case": needs, "docs": st != "none"}
    queued = []
    app._patient_refresh_work = fake_work
    app._queue_patient_refresh = lambda w, select=None: queued.append(w["code"]) or True
    asked = []
    mb.askyesno = lambda *a, **k: asked.append(a[1]) or True
    app.refresh_all_patients(); h.pump(root, 0.3)
    h.ok("you're asked about the patients already up to date, by name",
         asked and "BB2" in asked[-1] and "CC3" in asked[-1] and "1 patient(s) have something new" in asked[-1],
         asked[-1:] if asked else None)
    h.ok("…'Yes' queues every patient with notes, in the patient-list order", queued == ["AA1", "BB2", "CC3"], queued)
    h.ok("…and says which weren't queued (no notes yet)", "DD4" in str(app.status.cget("text")),
         app.status.cget("text"))
    queued.clear()
    mb.askyesno = lambda *a, **k: False
    app.refresh_all_patients(); h.pump(root, 0.3)
    h.ok("'No' queues only the ones with something new", queued == ["AA1"], queued)
    state.update(AA1="current")
    queued.clear()
    app.refresh_all_patients(); h.pump(root, 0.3)
    h.ok("all up to date and 'No': nothing queued, and it says so", queued == []
         and "already up to date" in str(app.status.cget("text")))
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script)
