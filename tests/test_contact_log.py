"""Contact log: its own tab on every patient, with every contact outside sessions (logged calls, visits, texts,
emails and contact notes) dated and listed like sessions; contact notes leave the session list."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H


def script(app, root, V, h):
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    cid = V.save_client({"code": "CL1"})
    V.save_session({"meta": {"client_id": cid, "session_date": "2026-10-01", "session_number": "1", "kind": "progress",
                             "created": "a"}, "note": "Session note."})
    note_sid = V.save_session({"meta": {"client_id": cid, "session_date": "2026-10-04", "kind": "parent",
                                        "contact_with": "patient", "contact_method": "Phone call", "created": "b"},
                               "note": "Called to reschedule; agreed to Thursday.", "overview": ""})
    app.refresh_clients(); app.open_client(cid); h.pump(root, 1)
    lb = app.page._search["list"]
    rows = lambda: [lb.get(i) for i in range(lb.size())]
    h.ok("the contact note isn't in the session list; the Contact log is a tab", not any("2026-10-04" in r for r in rows())
         and "📞  Contact log  · 1" in rows() and any("2026-10-01" in r for r in rows()), rows())
    h.ok("the header counts sessions only", "1 session" in h.texts(app.page))

    app.open_client(cid, select="comms"); h.pump(root, 1)
    page, lb = app.page, app.page._search["list"]
    view = next(w for w in h.widgets(page) if hasattr(w, "_contacts"))
    c = view._contacts
    h.ok("the log lists it by date and shows the note itself below",
         c["list"].get(0).startswith("2026-10-04") and "Patient contact note" in c["list"].get(0)
         and "agreed to Thursday" in "\n".join(w.get("1.0", "end") for w in h.widgets(page, "Text")), c["list"].get(0))

    # log a phone call
    h.button(page, "Log a contact").invoke(); h.pump(root, 0.4)
    w = app._contact_win
    w["date"].set("2026-11-02"); w["time"].set("10:15"); w["how"].set("Phone call")
    w["text"].insert("1.0", "Patient's mother called about school refusal; returned call next day.")
    w["with"].set("Parent / guardian")
    w["save"](); h.pump(root, 0.8)
    log = V.load_client(cid).get("communications") or []
    h.ok("a logged contact is saved with date, time, who and how", len(log) == 1 and log[0]["time"] == "10:15"
         and log[0]["with"] == "parent" and log[0]["method"] == "Phone call", log)
    h.ok("…the page wasn't rebuilt; the log shows 2, newest first, with the new one open",
         app.page is page and "📞  Contact log  · 2" in rows() and
         next(w for w in h.widgets(page) if hasattr(w, "_contacts"))._contacts["list"].get(0).startswith("2026-11-02")
         and "school refusal" in "\n".join(w.get("1.0", "end") for w in h.widgets(page, "Text")), rows())

    # edit it
    app.contact_log_dialog(V.load_client(cid), log[0]); h.pump(root, 0.3)
    w = app._contact_win
    w["text"].delete("1.0", "end"); w["text"].insert("1.0", "Edited summary."); w["save"](); h.pump(root, 0.6)
    log = V.load_client(cid)["communications"]
    h.ok("editing updates the same entry", len(log) == 1 and log[0]["text"] == "Edited summary.")

    # a link to the contact note (e.g. a review on Home) opens the log on it
    app.client_id = None; app.show_home(); h.pump(root, 0.5)
    app.open_client(cid, select=note_sid); h.pump(root, 1)
    view = next(w for w in h.widgets(app.page) if hasattr(w, "_contacts"))
    sel = view._contacts["list"].curselection()
    h.ok("opening a contact note goes to the Contact log with it selected",
         sel and view._contacts["list"].get(sel[0]).startswith("2026-10-04"))

    app.comm_remove(V.load_client(cid), V.load_client(cid)["communications"][0]); h.pump(root, 0.6)
    h.ok("removing a logged contact", not V.load_client(cid).get("communications"))

    # an email chain printed from Gmail (made-up people and addresses)
    ss = h.ss
    printed = (
        "11/13/26, 9:09 PM Example Clinic Mail - Scheduling\n"
        "Sam Counselor <sam@clinic.example>\n"
        "Scheduling\n"
        "2 messages\n"
        "Pat Parent <pat@mail.example> Wed, Nov 11, 2026 at 3:15 PM\n"
        "To: Sam Counselor <sam@clinic.example>\n"
        "Hi Sam,\n"
        " Can we move Thursday's session to 5pm?\n"
        "Pat Parent\n"
        "555-201-0999\n"
        "Sam Counselor <sam@clinic.example> Wed, Nov 11, 2026 at 3:59 PM\n"
        "To: Pat Parent <pat@mail.example>\n"
        "Yes, 5pm works. See you then.\n"
        "Sam Counselor\n"
        "Psychotherapy Extern\n"
        "Confidentiality Notice\n"
        "The contents of this email message and any attachments are confidential and are intended solely...\n"
        "[Quoted text hidden]\n"
        "https://mail.google.com/mail/u/0/?ik=abc&view=pt\n")
    t = ss.tidy_email_thread(printed)
    h.ok("a printed email chain is recognized", ss.looks_like_email_print(printed))
    h.ok("…each email becomes one dated block, the mailbox owner is the Counselor",
         "[2026-11-11 3:15 PM] Pat Parent → Counselor:\nHi Sam,\nCan we move Thursday's session to 5pm?" in t
         and "[2026-11-11 3:59 PM] Counselor → Pat Parent:\nYes, 5pm works. See you then." in t
         and t.startswith("Subject: Scheduling"), t)
    h.ok("…without signatures, the confidentiality notice, [Quoted text hidden] or the print header/link",
         "555-201-0999" not in t and "Psychotherapy Extern" not in t and "Confidentiality" not in t
         and "Quoted text" not in t and "mail.google" not in t and "9:09 PM" not in t, t)
    # one entry per date; same-date messages merged in time order; lone phone-number lines dropped
    cid3 = V.save_client({"code": "CL3"})
    thread = ("Hi Sam, this is your counselor checking in.\n(555) 201-0999\n\n"
              "[2026-10-19 8:34 AM] Patient: Yea\n"
              "[2026-10-19 10:22 AM] Counselor: Would 5:45 work?\n(555) 201-0999\n\n"
              "[2026-10-21 9:05 AM] Patient: Running late\n")
    app.comm_add(cid3, {"date": "2026-11-13", "with": "patient", "who": "", "method": "Text messages",
                        "text": thread, "notes": ""})
    log = sorted(V.load_client(cid3)["communications"], key=lambda e: e["date"])
    h.ok("texts spanning two dates become two dated entries (not today's date)",
         [e["date"] for e in log] == ["2026-10-19", "2026-10-21"] and log[0]["time"] == "8:34 AM", log)
    h.ok("…the first text before them stays with the first date, and the phone-number lines are gone",
         log[0]["text"].startswith("Hi Sam") and "201-0999" not in log[0]["text"] + log[1]["text"], log[0]["text"])
    more = "[2026-10-19 9:10 AM] Patient: I get a break at 2:45\n[2026-10-19 8:34 AM] Patient: Yea\n"
    app.comm_add(cid3, {"date": "2026-11-13", "with": "patient", "who": "", "method": "Text messages", "text": more,
                        "notes": ""})
    log = sorted(V.load_client(cid3)["communications"], key=lambda e: e["date"])
    lines = [l for l in log[0]["text"].splitlines() if l.startswith("[")]
    h.ok("more messages from the same date are merged into that entry, in time order, without repeats",
         len(log) == 2 and lines == ["[2026-10-19 8:34 AM] Patient: Yea", "[2026-10-19 9:10 AM] Patient: I get a break "
                                     "at 2:45", "[2026-10-19 10:22 AM] Counselor: Would 5:45 work?"], lines)
    mail = ss.tidy_email_thread(printed.replace("Wed, Nov 11, 2026 at 3:59 PM", "Thu, Nov 12, 2026 at 9:00 AM"))
    app.comm_add(cid3, {"date": "2026-11-13", "with": "parent", "who": "", "method": "Email", "text": mail, "notes": ""})
    em = sorted([e for e in V.load_client(cid3)["communications"] if e["method"] == "Email"], key=lambda e: e["date"])
    h.ok("an email chain over two days becomes two dated entries, each keeping the subject",
         [e["date"] for e in em] == ["2026-11-11", "2026-11-12"] and all(e["text"].startswith("Subject: Scheduling")
                                                                         for e in em), em)
    # an entry saved before splitting existed is re-filed when the patient is opened
    c = V.load_client(cid3)
    c["communications"] = [{"id": "old", "date": "2026-11-13", "with": "patient", "method": "Text messages",
                            "text": thread, "notes": "", "added": "x"}]
    V.save_client(c)
    app.open_client(cid3, select="comms"); h.pump(root, 1)
    h.ok("an older multi-date entry is split by date when the patient is opened",
         sorted(e["date"] for e in V.load_client(cid3)["communications"]) == ["2026-10-19", "2026-10-21"])
    # one entry per date for texts and one for emails — whoever they were with
    cid4 = V.save_client({"code": "CL4"})
    app.comm_add(cid4, {"date": "x", "with": "patient", "who": "", "method": "Text messages",
                        "text": "[2026-11-03 9:00 AM] Patient: running late", "notes": ""})
    app.comm_add(cid4, {"date": "x", "with": "parent", "who": "", "method": "Email",
                        "text": "[2026-11-03 8:00 AM] Parent → Counselor:\nForms attached.", "notes": ""})
    app.comm_add(cid4, {"date": "x", "with": "other", "who": "teacher", "method": "Email",
                        "text": "[2026-11-03 7:30 AM] Teacher → Counselor:\nQuick question.", "notes": ""})
    app.comm_add(cid4, {"date": "x", "with": "parent", "who": "", "method": "Text messages",
                        "text": "[2026-11-03 6:45 AM] Parent: on our way", "notes": ""})
    log = V.load_client(cid4)["communications"]
    texts = [e for e in log if e["method"] == "Text messages"]
    mails = [e for e in log if e["method"] == "Email"]
    h.ok("the same day's texts and emails are two entries (one each), whoever they were with",
         len(log) == 2 and len(texts) == 1 and len(mails) == 1 and all(e["date"] == "2026-11-03" for e in log), log)
    h.ok("…each in time order, marked as several people when it mixes them",
         texts[0]["text"].index("6:45 AM") < texts[0]["text"].index("9:00 AM")
         and mails[0]["text"].index("Teacher") < mails[0]["text"].index("Parent")
         and app.comm_party(mails[0]) == "several people" and texts[0]["time"] == "6:45 AM", (texts, mails))

    # dates are read from what's pasted or loaded
    app.external_comm(V.load_client(cid3)); h.pump(root, 0.4)
    w = app.parent_win
    w["date"].set("2026-01-01")
    w["msgs"].insert("1.0", printed); w["detect"](); h.pump(root, 0.2)
    h.ok("pasting an email chain fills in its date and switches to Email",
         w["date"].get() == "2026-11-11" and w["method"].get() == "Email"
         and "Date found" in str(w["found"].cget("text")), (w["date"].get(), w["found"].cget("text")))
    w["msgs"].delete("1.0", "end"); w["msgs"].insert("1.0", thread); w["detect"](); h.pump(root, 0.2)
    h.ok("texts over two days: the first date, and it says one entry per date",
         w["date"].get() == "2026-10-19" and "2 days" in str(w["found"].cget("text")), w["found"].cget("text"))
    w["win"].destroy()
    h.ok(".eml-style 'Date:' headers are read too",
         ss.detect_contact_dates("From: a@b.example\nDate: Wed, 11 Nov 2026 15:15:00 -0500\n\nHi") == ["2026-11-11"])
    app.contact_log_dialog(V.load_client(cid3)); h.pump(root, 0.3)
    cw = app._contact_win
    cw["date"].set("2026-11-05")
    cw["text"].insert("1.0", "Called mom; she'll bring the forms on Nov 20, 2026."); cw["detect"](); h.pump(root, 0.2)
    h.ok("a typed summary keeps the date you chose", cw["date"].get() == "2026-11-05")
    cw["text"].delete("1.0", "end"); cw["text"].insert("1.0", thread); cw["detect"](); h.pump(root, 0.2)
    h.ok("…pasted texts set it (and the How) from the messages", cw["date"].get() == "2026-10-19"
         and "2 days" in str(cw["found"].cget("text")), (cw["date"].get(), cw["how"].get()))
    cw["win"].destroy()
    h.ok("dates with month names read again (find_date / parse_date)",
         ss.find_date("Seen on Nov 11, 2026") == "2026-11-11" and ss.parse_date("Nov 7, 2026") == "2026-11-07")
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script)
