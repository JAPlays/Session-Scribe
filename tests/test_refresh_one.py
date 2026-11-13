"""One 'Refresh all' button on Progress & risk and Case overview, scoped to the open patient."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H


def script(app, root, V, h):
    ss = h.ss
    cid = V.save_client({"code": "AB1", "names_to_hide": "Alex Rivera", "names_format": 2})
    other = V.save_client({"code": "ZZ9", "names_to_hide": "Zed", "names_format": 2})
    V.save_session({"meta": {"client_id": other, "session_date": "2026-11-10", "session_number": "1", "kind": "progress",
                             "created": "x"}, "note": "Zed talked.", "transcript": "[01:00] Patient: hi"})
    V.save_session({"meta": {"client_id": cid, "session_date": "2026-11-10", "session_number": "3", "kind": "progress",
                             "created": "x"}, "note": "Alex Rivera discussed sleep.",
                    "transcript": "[10:00] Clinician: I'll send you the handout.\n[10:05] Patient: Okay."})
    h.RESP[:] = [("CASE OVERVIEW", "Case overview text AB1"), ("", '{"items": []}')]
    for page in ("insights", "case"):
        app.refresh_clients(); app.open_client(cid, select=page); h.pump(root, 1)
        b = h.button(root, "Refresh all")
        h.ok(f"{page}: Refresh all button is there", b is not None and str(b.cget("state")) != "disabled")
    app.open_client(cid, select="insights"); h.pump(root, 1)
    n0 = len(h.PROMPTS)
    h.button(root, "Refresh all").invoke()
    h.pump(root, 30, lambda: not app.busy and V.load_client(cid).get("case_overview", {}).get("text")); h.pump(root, 1)
    c = V.load_client(cid)
    h.ok("case overview written", bool(c.get("case_overview", {}).get("text")))
    sess = [d for _, d in V.sessions_for(cid)]
    h.ok("follow-ups scanned", any(d.get("followups_scanned") for d in sess))
    sent = "\n".join(h.PROMPTS[n0:])
    h.ok("other patient untouched", "Zed" not in sent and not V.load_client(other).get("case_overview"))
    h.ok("stays on Progress & risk page", h.button(root, "Refresh all") is not None and "Progress & risk" in h.texts(root))
    app.open_client(cid, select="case"); h.pump(root, 1)
    h.ok("case page shows the text", "Case overview text AB1" in h.texts(root))


H.start(script)
