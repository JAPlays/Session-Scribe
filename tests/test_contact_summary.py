"""Contact log: pick two dates and the AI writes one detailed summary note of every text, email, call and
contact note between them (made-up data only)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H

SUMMARY = ("OVERVIEW\nBetween the dates the Counselor had 3 contacts with the parent.\n\nCONTACTS BY DATE\n"
           "- Nov 10, 2026: email about scheduling.\n\nAGREEMENTS AND FOLLOW-UP\n- None discussed")


def script(app, root, V, h):
    ss = h.ss
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    cid = V.save_client({"code": "SUM1", "names_to_hide": "Alex Rivera, Patient", "names_format": 2})
    V.save_session({"meta": {"client_id": cid, "session_date": "2026-10-01", "session_number": "1", "kind": "progress",
                             "created": "a"}, "note": "Session note."})
    V.save_session({"meta": {"client_id": cid, "session_date": "2026-11-04", "kind": "parent", "contact_with": "parent",
                             "contact_method": "Phone call", "created": "b"}, "note": "Mother called about Alex's forms."})
    app.comm_add(cid, {"date": "x", "with": "parent", "who": "", "method": "Email", "notes": "",
                       "text": "[2026-11-10 9:05 AM] Pat Parent → Counselor:\nCan Alex come at 5pm? Reach me at "
                               "pat@mail.example or 555-201-0999."})
    app.comm_add(cid, {"date": "x", "with": "patient", "who": "", "method": "Text messages", "notes": "",
                       "text": "[2026-11-11 8:34 AM] Patient: Running late"})
    app.comm_add(cid, {"date": "2026-11-12", "time": "10:15", "with": "parent", "who": "", "method": "Phone call",
                       "text": "Returned call about the release form.", "notes": ""})
    app.comm_add(cid, {"date": "2026-10-20", "time": "", "with": "parent", "who": "", "method": "Phone call",
                       "text": "Older call, outside the period.", "notes": ""})
    app.refresh_clients(); app.open_client(cid, select="comms"); h.pump(root, 1)
    btn = h.button(app.page, "Summarize a period")
    h.ok("the Contact log has 'Summarize a period…'", btn is not None)
    btn.invoke(); h.pump(root, 0.4)
    w = app._summary_win
    w["a"].set("2026-11-01"); w["b"].set("2026-11-12"); h.pump(root, 0.3)
    cnt = str(w["count"].cget("text"))
    h.ok("picking two dates counts the contacts between them", cnt.startswith("4 contacts") and "email" in cnt
         and "phone call" in cnt and "contact note" in cnt, cnt)
    w["b"].set("2026-10-01"); w["a"].set("2026-09-01"); h.pump(root, 0.3)
    w["go"].invoke(); h.pump(root, 0.3)
    h.ok("…a period with none says so", "no contacts" in str(w["status"].cget("text")).lower())
    w["a"].set("2026-11-01"); w["b"].set("2026-11-12")
    w["obs"].insert("1.0", "Focus on scheduling.")
    n0 = len(h.PROMPTS)
    w["go"].invoke()
    h.pump(root, 20, until=lambda: (V.load_client(cid).get("contact_summaries") or []) and not app.busy)
    h.pump(root, 0.6)
    prompt = h.PROMPTS[-1] if len(h.PROMPTS) > n0 else ""
    h.ok("the AI is asked for one detailed note over the period, every contact oldest first",
         "CONTACT SUMMARY NOTE" in prompt and "Nov 1, 2026" in prompt or "2026-11-01" in prompt, prompt[:300])
    order = [prompt.find(s) for s in ("called about", "Can ", "Running late", "release form")]
    h.ok("…the contact note, email, texts and call are all there, in date order, nothing outside the period",
         all(i > 0 for i in order) and order == sorted(order) and "Older call" not in prompt
         and "Focus on scheduling." in prompt, order)
    h.ok("…names hidden, email addresses and phone numbers removed",
         "Alex" not in prompt and "pat@mail.example" not in prompt and "555-201" not in prompt
         and "[EMAIL]" in prompt and "[PHONE]" in prompt)
    sums = V.load_client(cid).get("contact_summaries") or []
    h.ok("the summary is saved with its period, as a draft to review",
         len(sums) == 1 and sums[0]["start"] == "2026-11-01" and sums[0]["end"] == "2026-11-12"
         and sums[0]["text"].startswith("OVERVIEW") and sums[0]["needs_review"], sums)
    view = next(x for x in h.widgets(app.page) if hasattr(x, "_contacts"))
    c = view._contacts
    sel = c["list"].curselection()
    h.ok("…listed in the Contact log and opened", sel and "Contact summary" in c["list"].get(sel[0])
         and "⚠ review" in c["list"].get(sel[0]), [c["list"].get(i) for i in range(c["list"].size())])
    det = next(x for x in h.widgets(app.page) if hasattr(x, "_summary"))
    tb = det._summary["text"]
    tb.insert("end", "\n- Counselor to send the form.")
    det._summary["save"](); h.pump(root, 0.6)
    s = V.load_client(cid)["contact_summaries"][0]
    h.ok("editing and saving keeps the change and clears 'review'",
         s["text"].endswith("send the form.") and not s["needs_review"])
    blocks = ss.collect_export(V.load_client(cid), V.sessions_for(cid), {"contacts"})
    h2 = [t for k, t in blocks if k == "h2"]
    h3 = [t for k, t in blocks if k == "h3"]
    h.ok("the export's Contact log includes the summary, its headings as headings",
         any(x.startswith("Contact summary") for x in h2) and "OVERVIEW" in h3 and "CONTACTS BY DATE" in h3, h2)
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script, responses=[("CONTACT SUMMARY NOTE", SUMMARY)])
