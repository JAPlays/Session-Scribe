"""Text messages copied as screen-reader text ("Message from 5 5 5 …, <text>, Friday, …") are tidied to one
line per message before the AI reads them: no repeats, no phone numbers. (Made-up messages.)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H

PASTED = (
    "Hi Sam! Just checking the video link works for you?\n"
    "\n"
    "* \n"
    "Message from 5 5 5 2 0 1 0 9 9 9, Yes it works, see you at 3, thanks, Friday, November 13 2026, 1:08 PM.\n"
    "Yes it works, see you at 3, thanks\n"
    "* ‪(555) 201-0999‬ • 1:15 PM\n"
    "Message from 5 5 5 2 0 1 0 9 9 9, Actually can we move to 4, Friday, November 13 2026, 1:15 PM.\n"
    "Actually can we move to 4\n"
    "* Message from 5 5 5 2 0 1 0 9 9 9, Never mind 3 is fine, Friday, November 13 2026, 1:22 PM.\n"
    "Never mind 3 is fine\n"
    "* ‪(555) 201-0999• 1:23 PM\n")


def script(app, root, V, h):
    ss = h.ss
    t = ss.tidy_message_thread(PASTED)
    lines = t.splitlines()
    h.ok("each message becomes one dated line from the other person",
         "[2026-11-13 1:08 PM] Patient: Yes it works, see you at 3, thanks" in lines
         and "[2026-11-13 1:15 PM] Patient: Actually can we move to 4" in lines
         and "[2026-11-13 1:22 PM] Patient: Never mind 3 is fine" in lines, t)
    h.ok("…without the repeated copy of each message", t.count("Actually can we move to 4") == 1
         and t.count("Never mind 3 is fine") == 1, t)
    h.ok("…without the number/time header lines or the phone number in any form",
         "201-0999" not in t and "5 5 5" not in t and "•" not in t and "‪" not in t, t)
    h.ok("your own message (no 'Message from') is kept as it is", lines[0].startswith("Hi Sam! Just checking"))
    h.ok("ordinary text is left alone", ss.tidy_message_thread("Hello\nSee you Tuesday") == "Hello\nSee you Tuesday")
    h.ok("the format is recognized even if 'Text messages' isn't picked", ss.looks_like_message_export(PASTED)
         and not ss.looks_like_message_export("Dear Dr. X,\nThanks for the update."))
    named = (
        "Hi Robin! This is Sam from the clinic.\n\n"
        "* Message from Robin School Counselor, Sure!, Monday, September 14 2026, 10:01 AM.\n"
        "Sure!\n"
        "* Robin School Counselor • Sep 14, 10:02 AM\n"
        "Message from Robin School Counselor, When are you free?, Monday, September 14 2026, 10:02 AM.\n"
        "R\n"
        "When are you free?\n"
        "* Sep 16, 11:46 AM\n"
        "Message from you, Are you free at 3:30?, Wednesday, September 16 2026, 11:46 AM.\n"
        "Are you free at 3:30?\n"
        "* Robin School Counselor • Sep 16, 11:47 AM\n"
        "Message from Robin School Counselor, Loved “Are you free at 3:30?”, Wednesday, September 16 2026, 11:47 AM.\n"
        "R\n"
        "Loved “Are you free at 3:30?”\n"
        "* Message from you, see you then, Wednesday, September 16 2026, 11:50 AM.\n"
        "see you then Robin School Counselor • Sep 16, 11:52 AM\n"
        "Message from Robin School Counselor, Thanks!, Wednesday, September 16 2026, 11:52 AM.\n"
        "R\n"
        "Thanks!\n")
    t2 = ss.tidy_message_thread(named, other="Contact").splitlines()
    h.ok("named contacts, your own messages ('Message from you'), dated headers and initials",
         t2[0].startswith("Hi Robin!") and "[2026-09-14 10:02 AM] Robin School Counselor: When are you free?" in t2
         and "[2026-09-16 11:46 AM] Counselor: Are you free at 3:30?" in t2
         and not any(l.strip() == "R" or "•" in l or l.startswith("Sep ") for l in t2), t2)
    h.ok("…an iPhone reaction reads as a reaction, not a new message",
         "[2026-09-16 11:47 AM] Robin School Counselor reacted (loved) to: “Are you free at 3:30?”" in t2, t2)
    h.ok("…and a message glued to the next header comes apart cleanly",
         "[2026-09-16 11:50 AM] Counselor: see you then" in t2
         and "[2026-09-16 11:52 AM] Robin School Counselor: Thanks!" in t2 and sum("see you then" in l for l in t2) == 1, t2)
    h.ok("spelled-out phone numbers are scrubbed from anything sent to the AI",
         ss.scrub_contact_details("call 5 5 5 2 0 1 0 9 9 9 today") == "call [PHONE] today"
         and ss.scrub_contact_details("PHQ 1 2 3") == "PHQ 1 2 3")

    # through the window, default: straight into the External communication log, no AI
    cid = V.save_client({"code": "TM1"})
    app.refresh_clients(); app.open_client(cid); h.pump(root, 1)
    lb = app.page._search["list"]
    h.ok("the Contact log is in the list, empty to begin with",
         "📞  Contact log" in [lb.get(i) for i in range(lb.size())])
    h.RESP[:] = [("", "Contact note text.")]
    app.external_comm(V.load_client(cid)); h.pump(root, 0.5)
    w = app.parent_win
    w["mode"].set("messages"); w["method"].set("Text messages"); w["date"].set("2026-11-13")
    w["msgs"].insert("1.0", PASTED)
    h.ok("the button says Save to log", "Save to log" in str(w["go"].cget("text")))
    n0 = len(h.PROMPTS)
    w["go"].invoke(); h.pump(root, 1)
    log = V.load_client(cid).get("communications") or []
    h.ok("the messages are logged as they are, tidied, with nothing sent to the AI",
         len(log) == 1 and "Patient: Actually can we move to 4" in log[0]["text"] and log[0]["method"] == "Text messages"
         and log[0]["date"] == "2026-11-13" and len(h.PROMPTS) == n0, log)
    h.ok("…and no contact note is made", not any(d["meta"].get("source") == "messages" for _, d in V.sessions_for(cid)))
    lb = app.page._search["list"]
    rows = [lb.get(i) for i in range(lb.size())]
    h.ok("the Contact log shows it, open on it", "📞  Contact log  · 1" in rows
         and "Contact log" in h.texts(app.page) and "Never mind 3 is fine" in "\n".join(
             w.get("1.0", "end") for w in h.widgets(app.page, "Text")), rows)
    vw = app.comm_view(V.load_client(cid), log[0]); h.pump(root, 0.3)
    h.ok("View shows the whole thread", "Never mind 3 is fine" in h.texts(vw)); vw.destroy()
    ask = ss.build_ask_prompt(V.load_client(cid), V.sessions_for(cid), "What did they text about moving the time?",
                              [], {}, "")
    h.ok("Ask AI can use the logged messages (phone number still removed)",
         "Actually can we move to 4" in str(ask) and "201-0999" not in str(ask), str(ask)[:300])

    # with "Also draft a contact note": the AI gets the tidy version
    app.external_comm(V.load_client(cid)); h.pump(root, 0.5)
    w = app.parent_win
    w["mode"].set("messages"); w["method"].set("Text messages"); w["date"].set("2026-11-13")
    w["msgs"].insert("1.0", PASTED)
    w["draft"].set(True)
    n0 = len(h.PROMPTS)
    w["go"].invoke()
    h.pump(root, 20, lambda: not app.busy and any(d["meta"].get("source") == "messages" for _, d in V.sessions_for(cid)))
    h.pump(root, 1)
    sent = "\n".join(h.PROMPTS[n0:])
    with_msgs = [p for p in h.PROMPTS[n0:] if "Actually can we move to 4" in p]
    h.ok("the AI gets the tidy version (each message once per prompt)",
         with_msgs and all("Patient: Actually can we move to 4" in p and p.count("Actually can we move to 4") == 1
                           for p in with_msgs), sent[-800:])
    h.ok("…and never the phone number", "201-0999" not in sent and "5 5 5 2" not in sent)
    saved = next((d for _, d in V.sessions_for(cid) if d["meta"].get("source") == "messages"), {})
    h.ok("the saved messages are the tidy version too", "Patient: Never mind 3 is fine" in saved.get("transcript", "")
         and "5 5 5 2" not in saved.get("transcript", ""), saved.get("transcript", "")[:300])


H.start(script)
