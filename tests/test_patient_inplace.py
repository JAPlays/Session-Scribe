"""Patient page: removing an assessment, a score, a document or a logged message redraws only the list and the
open tab, not the whole page; from elsewhere it opens the patient as usual."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H


def script(app, root, V, h):
    ss = h.ss
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    cid = V.save_client({"code": "PI1",
                         "measures": [ss.measure_entry("PHQ-9", "2026-10-01", total=9)],
                         "assessments": [{"id": "a1", "name": "PCL-5 (PTSD)", "date": "2026-10-02", "score": "40",
                                          "notes": "", "file_id": "", "added": "x"},
                                         {"id": "a2", "name": "ACE questionnaire", "date": "2026-10-03", "score": "3",
                                          "notes": "", "file_id": "", "added": "y"}],
                         "documents": [{"id": "d1", "type": "Psychological assessment", "date": "2025-01-01",
                                        "title": "", "text": "Report one", "file_id": "", "added": "x"},
                                       {"id": "d2", "type": "Medical records", "date": "2025-02-01", "title": "",
                                        "text": "Report two", "file_id": "", "added": "y"}],
                         "communications": [{"id": "m1", "date": "2026-11-01", "with": "patient", "method": "Email",
                                             "text": "Hello", "notes": "", "added": "x"}]})
    V.save_session({"meta": {"client_id": cid, "session_date": "2026-10-05", "session_number": "1", "kind": "progress",
                             "created": "x"}, "note": "n"})
    app.refresh_clients(); app.open_client(cid, select="assess"); h.pump(root, 1)
    page = app.page
    lb = page._search["list"]
    rows = lambda: [lb.get(i) for i in range(lb.size())]

    def same(label):
        h.ok(label + ": the page wasn't rebuilt (same page, same list)", app.page is page and page._search["list"] is lb)

    h.ok("starts with 3 (2 assessments + 1 score)", "🧾  Assessments  · 3" in rows(), rows())
    c = V.load_client(cid)
    app.assessment_remove(c, c["assessments"][0]); h.pump(root, 0.6)
    same("Removing an assessment")
    h.ok("…the list count and the tab update", "🧾  Assessments  · 2" in rows() and "PCL-5 (PTSD)" not in h.texts(page)
         and "ACE questionnaire" in h.texts(page))
    m = V.load_client(cid)["measures"][0]
    app.measure_remove(V.load_client(cid), m["id"], select="assess"); h.pump(root, 0.6)
    same("Removing a score")
    h.ok("…count follows", "🧾  Assessments  · 1" in rows(), rows())

    app.open_client(cid, select="docs"); h.pump(root, 0.8)
    page = app.page; lb = page._search["list"]
    app.doc_remove(V.load_client(cid), V.load_client(cid)["documents"][0]); h.pump(root, 0.6)
    same("Removing a document")
    h.ok("…the Documents count updates and the tab still shows the other one", "📁  Documents  · 1" in rows()
         and "Medical records" in h.texts(page), rows())
    app.doc_remove(V.load_client(cid), V.load_client(cid)["documents"][0]); h.pump(root, 0.6)
    same("Removing the last document")
    h.ok("…the Documents tab leaves the list", not any("Documents" in r for r in rows()), rows())

    app.open_client(cid, select="comms"); h.pump(root, 0.8)
    page = app.page; lb = page._search["list"]
    app.comm_remove(V.load_client(cid), V.load_client(cid)["communications"][0]); h.pump(root, 0.6)
    same("Removing a logged message")
    h.ok("…the Contact log count updates (the tab stays)", "📞  Contact log" in rows(), rows())

    # from somewhere else (Home), it just opens the patient
    app.client_id = None; app.show_home(); h.pump(root, 0.8)
    a = V.load_client(cid)["assessments"][0]
    app.assessment_remove(V.load_client(cid), a); h.pump(root, 0.8)
    h.ok("from another page it opens the patient as usual", app.client_id == cid and app.page is not page)
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script)
