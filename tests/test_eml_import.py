"""Several .eml files loaded at once: each email is its own dated block (date, time, from → to, subject, its own
words without the quoted earlier emails), sorted oldest first, one Contact log entry per date. All names and
addresses are made up."""
import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H

ME = ("Sam Counselor", "sam@clinic.example")
PAT = ("Pat Parent", "pat@mail.example")


def make_eml(folder, name, frm, to, when, subject, plain=None, html=None):
    from email.message import EmailMessage
    from email.utils import format_datetime, formataddr
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = formataddr(frm), formataddr(to), subject
    m["Date"] = format_datetime(when.astimezone())
    if plain is not None:
        m.set_content(plain)
        if html:
            m.add_alternative(html, subtype="html")
    else:
        m.set_content(html, subtype="html")
    p = os.path.join(folder, name)
    with open(p, "wb") as f:
        f.write(bytes(m))
    return p


def script(app, root, V, h):
    import tkinter.filedialog as fd
    from datetime import datetime
    ss = h.ss
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    d = tempfile.mkdtemp()
    a = make_eml(d, "a.eml", PAT, ME, datetime(2026, 11, 10, 9, 5), "Scheduling",
                 "Hi Sam,\nCould we move Thursday's session to 5pm? My shift changed this week and I can't get\n"
                 "there before then.\nThanks,\nPat Parent\n555-201-0999\n")
    b = make_eml(d, "b.eml", ME, PAT, datetime(2026, 11, 10, 14, 30), "Re: Scheduling",
                 "Hi Pat,\n5pm Thursday works. See you then.\nSam Counselor\nPsychotherapy Extern\n\n"
                 "On Tue, Nov 10, 2026 at 9:05 AM Pat Parent <pat@mail.example>\nwrote:\n"
                 "> Hi Sam,\n> Could we move Thursday's session to 5pm?\n")
    c = make_eml(d, "c.eml", PAT, ME, datetime(2026, 11, 12, 8, 0), "Re: Scheduling",
                 html="<html><head><style>p{color:red}</style></head><body><p>Thanks Sam &amp; see you "
                      "Thursday!</p><div>Pat</div></body></html>")
    e = make_eml(d, "d.eml", PAT, ME, datetime(2026, 11, 10, 11, 15), "Forms",
                 "Hi Sam,\nI signed the release forms and left them at the front desk.\n\n"
                 "-----Original Message-----\nFrom: Sam Counselor\nSent: Monday\nPlease sign the forms.\n")
    shuffled = [c, a, e, b]

    cid = V.save_client({"code": "EML1"})
    V.save_session({"meta": {"client_id": cid, "session_date": "2026-11-01", "session_number": "1", "kind": "progress",
                             "created": "x"}, "note": "n"})
    app.refresh_clients(); app.open_client(cid, select="comms"); h.pump(root, 1)

    # load them all at once in the dialog
    app.external_comm(V.load_client(cid)); h.pump(root, 0.5)
    w = app.parent_win
    w["date"].set("2026-01-01")
    fd.askopenfilenames = lambda **k: shuffled
    w["load"](); h.pump(root, 0.3)
    h.ok("loading 4 .eml files finds both dates (the first one fills Date) and switches to Email",
         w["date"].get() == "2026-11-10" and "2 days" in str(w["found"].cget("text")) and w["method"].get() == "Email",
         (w["date"].get(), w["found"].cget("text"), w["method"].get()))
    w["go"].invoke(); h.pump(root, 1)
    log = sorted(V.load_client(cid).get("communications") or [], key=lambda x: x["date"])
    h.ok("…saved as one Email entry per date, not one entry", [(x["date"], x["method"]) for x in log] ==
         [("2026-11-10", "Email"), ("2026-11-12", "Email")], [(x["date"], x["method"]) for x in log])
    t10 = log[0]["text"] if log else ""
    heads = [l for l in t10.split("\n") if l.startswith("[")]
    h.ok("…that day's three emails each have date, time, from → to, oldest first",
         heads == ["[2026-11-10 9:05 AM] Pat Parent → Counselor:", "[2026-11-10 11:15 AM] Pat Parent → Counselor:",
                   "[2026-11-10 2:30 PM] Counselor → Pat Parent:"] and log[0]["time"] == "9:05 AM", heads)
    h.ok("…each email's own words only: no quoted earlier email, signature or 'Original Message'",
         "My shift changed this week and I can't get there before then." in t10 and "5pm Thursday works." in t10
         and "> " not in t10 and "wrote:" not in t10 and "Please sign" not in t10 and "Psychotherapy Extern" not in t10
         and "555-201" not in t10, t10)
    t12 = log[1]["text"] if len(log) > 1 else ""
    h.ok("an HTML-only email reads as plain text", "Thanks Sam & see you Thursday!" in t12 and "<" not in t12
         and "color:red" not in t12, t12)

    # shown in the log
    view = next(x for x in h.widgets(app.page) if hasattr(x, "_contacts"))
    c_ = view._contacts
    i10 = next(i for i, en in enumerate(c_["entries"]) if en[0] == "2026-11-10")
    c_["show"](i10); h.pump(root, 0.3)
    txt = [x for x in h.widgets(app.page, "Text") if "Counselor" in x.get("1.0", "end")][-1]
    shown = txt.get("1.0", "end")
    rng = lambda tag: [txt.get(p, q) for p, q in zip(txt.tag_ranges(tag)[::2], txt.tag_ranges(tag)[1::2])]
    h.ok("the thread view: from → to in bold per email, with date and time, subject when it changes",
         rng("who") == ["Pat Parent → Counselor", "Pat Parent → Counselor", "Counselor → Pat Parent"]
         and [s.strip() for s in rng("esubj")] == ["Scheduling", "Forms", "Re: Scheduling"]
         and "2:30 PM" in shown and "Subject:" not in shown, (rng("who"), rng("esubj")))

    # importing the same files again adds nothing
    app.comm_add(cid, {"date": "x", "with": "parent", "who": "", "method": "Email",
                       "text": "\n\n".join(ss.read_messages_file(p) for p in shuffled), "notes": ""})
    again = V.load_client(cid)["communications"]
    h.ok("loading the same files again doesn't repeat them",
         len(again) == 2 and sum(x["text"].count("[2026-") for x in again) == 4)

    # an entry saved the old way (every file in one block, one date) is split when the patient is opened
    cid2 = V.save_client({"code": "EML2", "communications": [
        {"id": "old1", "date": "2026-11-12", "time": "", "with": "parent", "who": "", "method": "Email",
         "text": "\n\n".join(ss.read_messages_file(p) for p in shuffled), "notes": "", "added": "2026-11-13T09:00:00",
         "updated": "2026-11-13T09:00:00"}]})
    V.save_session({"meta": {"client_id": cid2, "session_date": "2026-11-01", "session_number": "1",
                             "kind": "progress", "created": "x"}, "note": "n"})
    app.refresh_clients(); app.open_client(cid2, select="comms"); h.pump(root, 1)
    fixed = sorted(V.load_client(cid2)["communications"], key=lambda x: x["date"])
    h.ok("an earlier all-in-one .eml entry is re-filed by date", [x["date"] for x in fixed] ==
         ["2026-11-10", "2026-11-12"] and fixed[0]["text"].count("[2026-11-10") == 3, [x["date"] for x in fixed])
    h.ok("a single .eml (no common address) names both people",
         ss.eml_thread(ss.read_messages_file(a)).startswith("[2026-11-10 9:05 AM] Pat Parent → Sam Counselor:"))
    h.ok("…and with your name in Settings, your address is the Counselor even in one file",
         ss.eml_thread(ss.read_messages_file(a), me_name="Sam Counselor").startswith(
             "[2026-11-10 9:05 AM] Pat Parent → Counselor:"))
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script)
