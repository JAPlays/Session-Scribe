"""Feature 5: supervision prep packet."""
import sys, os, zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H
from datetime import date, timedelta

PAT = ("This period:\n- 11/10: explored work stress and shame after [Patient]'s manager yelled.\n\n"
       "Risk and safety:\n- Nothing documented\n\nProgress toward goals:\n- Anxiety lower on weekends.\n\n"
       "Bring to supervision:\n- How do I stay with shame when the patient changes the subject?")


def script(app, root, V, h):
    ss = h.ss
    today = date.today()
    d1, d_old = (today - timedelta(days=2)).isoformat(), (today - timedelta(days=30)).isoformat()
    a = V.save_client({"code": "AB1", "names_to_hide": "Alex Rivera, Patient", "names_format": 2, "goals": "Reduce anxiety"})
    b = V.save_client({"code": "CD2", "names_to_hide": "Casey Dunn, Patient", "names_format": 2, "goals": "Improve sleep"})
    c = V.save_client({"code": "EF3", "names_to_hide": "", "names_format": 2})
    V.save_session({"meta": {"client_id": a, "session_date": d1, "session_number": "3", "kind": "progress", "created": "x"},
                    "note": "Alex Rivera discussed work stress.",
                    "review": {"feedback": "WHAT WENT WELL\n- good\n\nONE THING TO PRACTICE NEXT SESSION\nStay with shame one beat longer.\n"}})
    V.save_session({"meta": {"client_id": b, "session_date": d1, "session_number": "5", "kind": "progress", "created": "x"},
                    "note": "Casey Dunn reported better sleep."})
    V.save_session({"meta": {"client_id": c, "session_date": d_old, "session_number": "1", "kind": "intake", "created": "x"},
                    "note": "Old intake."})
    app.refresh_clients()
    h.ok("Home has a 'Supervision prep' button", (app.show_home(), h.pump(root, 0.5), h.button(root, "Supervision prep"))[2] is not None)
    win = app.supervision_prep(); h.pump(root, 1)
    t = win._test
    codes = {cc["id"]: cc["code"] for cc in app.clients}
    picked = sorted(codes[k] for k, v in t["picks"].items() if v.get())
    h.ok("patients seen in the last week are ticked; others aren't", picked == ["AB1", "CD2"], picked)
    t["q"].insert("1.0", "When should I refer out?\nHow to handle no-shows")
    h.shot("sup_dialog.png")
    h.RESP[:] = [("SUPERVISION about one patient", PAT)]
    n0 = len(h.PROMPTS)
    t["go"].invoke()
    h.pump(root, 30, lambda: not app.busy and V.list_reports("supervision"))
    h.pump(root, 1)
    reps = V.list_reports("supervision")
    sent = "\n".join(h.PROMPTS[n0:])
    h.ok("one AI step per patient with activity", len(h.PROMPTS) - n0 == 2, len(h.PROMPTS) - n0)
    h.ok("names hidden from the AI", "Alex" not in sent and "Casey" not in sent and "[Patient]" in sent)
    h.ok("the session review's feedback goes in as context", "Stay with shame" in sent)
    txt = reps[0]["text"] if reps else ""
    print("   ---\n" + txt[:900] + "\n   ---")
    h.ok("packet saved (encrypted) with the period and patients", reps and reps[0]["start"] <= d1 <= reps[0]["end"]
         and reps[0]["patients"] == ["AB1", "CD2"])
    h.ok("packet has my questions, skills, and each patient", "MY QUESTIONS" in txt and "- When should I refer out?" in txt
         and "SKILLS I'M WORKING ON" in txt and "Stay with shame one beat longer." in txt and "AB1 —" in txt and "CD2 —" in txt
         and "EF3" not in txt)
    rid = reps[0]["id"]
    raw = open(os.path.join(V.reports_dir, rid + ".enc"), "rb").read()
    h.ok("the saved packet file is encrypted", b"refer out" not in raw and b"AB1" not in raw)
    docwins = [w for w in h.widgets(root) if hasattr(w, "_doc")]
    h.ok("the packet opened in a document window", docwins and "SUPERVISION PREP" in docwins[-1]._doc["text"].get("1.0", "end"))
    h.shot("sup_packet.png")
    # edit + save
    tw = docwins[-1]._doc["text"]
    tw.insert("end", "\nAdded by me.")
    h.button(docwins[-1], "Save changes").invoke()
    h.ok("edits are saved", "Added by me." in V.load_report(rid)["text"])
    # export both formats
    import tkinter.filedialog as fd
    out = os.path.join(os.environ["HOME"], "packet.docx")
    fd.asksaveasfilename = lambda **k: out
    ss_mod = h.ss
    app.export_text(root, "Supervision prep", V.load_report(rid)["text"], "x")
    h.ok("exports to Word", os.path.exists(out) and "MY QUESTIONS".title() in zipfile.ZipFile(out).read("word/document.xml").decode())
    out2 = os.path.join(os.environ["HOME"], "packet.pdf")
    fd.asksaveasfilename = lambda **k: out2
    app.export_text(root, "Supervision prep", V.load_report(rid)["text"], "x")
    h.ok("exports to PDF", os.path.exists(out2) and open(out2, "rb").read(4) == b"%PDF")
    # next time starts after the last packet
    win2 = app.supervision_prep(); h.pump(root, 1)
    h.ok("next packet starts the day after the last one ended", win2._test["a"].get() == (date.fromisoformat(reps[0]["end"]) + timedelta(days=1)).isoformat())
    h.ok("earlier packets are listed to reopen", h.button(win2, "–") is not None)


H.start(script)
