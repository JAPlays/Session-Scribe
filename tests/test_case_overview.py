"""Case overview tab: shows the overview (and its buttons) for a patient, and Create writes one."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H
from datetime import date, timedelta

OVERVIEW = "CASE SNAPSHOT\nAnxious adult, improving.\n\nPROGRESS BY GOAL\nGoal 1: better sleep (sessions 1-3)."
TITLED = "CASE OVERVIEW\n\n" + OVERVIEW          # what the model tends to write: a title above CASE SNAPSHOT


def script(app, root, V, h):
    ss = h.ss
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    today = date.today()
    D = lambda n: (today - timedelta(days=n)).isoformat()
    st = ss.strip_case_title
    h.ok("the title line is dropped", st(TITLED) == OVERVIEW, repr(st(TITLED)[:60]))
    h.ok("...also with markdown, a patient code or an underline",
         st("# Case Overview — PT-01\n=====\n" + OVERVIEW) == OVERVIEW and st("**CASE OVERVIEW**\n" + OVERVIEW) == OVERVIEW)
    h.ok("an overview without a title is left alone", st(OVERVIEW) == OVERVIEW)
    h.ok("...and so is text that only begins with the words",
         st("Case overview text AB1") == "Case overview text AB1"
         and st("Case overview: patient is improving.\n" + OVERVIEW).startswith("Case overview: patient")
         and st("CASE OVERVIEW: PT-01 (2026-11-13)\n" + OVERVIEW) == OVERVIEW
         and st("CASE OVERVIEW") == "CASE OVERVIEW")
    h.ok("a sentence that starts with the words isn't a title",
         st("Case overview shows marked progress across all goals since intake, with sleep now stable "
            "and anxiety down.\n" + OVERVIEW).startswith("Case overview shows"))
    a = V.save_client({"code": "CO1", "goals": "Sleep better", "case_overview": {
        "text": TITLED, "session_count": 2, "generated": (today - timedelta(days=1)).isoformat() + "T09:00:00"}})
    b = V.save_client({"code": "CO2"})
    for cid in (a, b):
        for i, n in enumerate((20, 10, 3)):
            V.save_session({"meta": {"client_id": cid, "session_date": D(n), "session_number": str(i + 1),
                                     "kind": "progress", "created": D(n) + "T10:00:00"},
                            "note": f"Progress note {i + 1}.", "overview": f"Overview {i + 1}."})
    app.refresh_clients()

    def case_texts():
        return "\n".join(w.get("1.0", "end") for w in h.widgets(app.page, "Text") if w.winfo_manager())

    try:
        app.open_client(a, select="case"); h.pump(root, 1.5)
    except Exception:
        import traceback; errors.append(traceback.format_exc())
    page = h.texts(app.page)
    h.ok("no errors opening the Case overview tab", not errors, errors[-1:] if errors else "")
    h.ok("the Update button is shown", "Update case overview" in page, page[:600])
    h.ok("the overview text is shown under the header", "Anxious adult, improving." in case_texts(), case_texts()[:300])
    h.ok("a saved overview's empty CASE OVERVIEW heading isn't shown", "CASE OVERVIEW" not in case_texts(),
         case_texts()[:120])

    errors.clear()
    h.RESP.append(("CASE OVERVIEW", TITLED.replace("Anxious adult", "Second patient")))
    try:
        app.open_client(b, select="case"); h.pump(root, 1.5)
        h.ok("a patient without one gets the Create button", h.button(app.page, "Create case overview") is not None)
        h.button(app.page, "Create case overview").invoke()
        h.pump(root, 15, until=lambda: (V.load_client(b).get("case_overview") or {}).get("text"))
        h.pump(root, 1.5)
    except Exception:
        import traceback; errors.append(traceback.format_exc())
    h.ok("no errors creating a case overview", not errors, errors[-1:] if errors else "")
    saved = (V.load_client(b).get("case_overview") or {}).get("text") or ""
    h.ok("Create saves the overview", "Second patient" in saved)
    h.ok("...without the title line", saved.startswith("CASE SNAPSHOT"), repr(saved[:40]))
    h.ok("...and shows it in the tab", "Second patient" in case_texts(), case_texts()[:300])


H.start(script)
