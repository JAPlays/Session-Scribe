"""Importing a registration form organizes every answer by topic (Registration tab), flags what matters
(a firearm in the home…), leaves out the policy wording, and can add the form's names to the names-to-hide
list. The form here is made up: a small Jotform-style PDF built by this test."""
import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H

# (kind, x, y, size, text): head/label = bold, value = regular. US Letter, y from the bottom.
ROWS = [
    ("value", 400, 760, 11, "November 10, 2026"),
    ("head", 200, 730, 18, "Child and Adolescent Registration"),
    ("value", 200, 712, 12, "There are several pages of this form. Thank you for trusting us!"),
    ("head", 40, 680, 18, "Patient's Information"),
    ("label", 40, 655, 12, "Patient's Full Name"), ("value", 250, 655, 11, "Robin Example"),
    ("label", 40, 632, 12, "Patient's Date of Birth"), ("value", 250, 632, 11, "March 3, 2012"),
    ("label", 40, 609, 12, "Parent/Guardian 1 Name"), ("value", 250, 609, 11, "Pat Example"),
    ("label", 40, 586, 12, "Parent/Guardian 1 Cell Number"), ("value", 250, 586, 11, "(555) 010-2000"),
    ("label", 40, 563, 12, "Parent/Guardian 1 Relationship"), ("value", 250, 563, 11, "Mother"),
    ("label", 40, 538, 12, "Can we text you on the phone number"), ("value", 255, 534, 11, "Yes"),
    ("label", 40, 524, 12, "above?"),
    ("label", 40, 498, 12, "Emergency Contact Name"), ("value", 250, 498, 11, "Lee"), ("value", 290, 498, 11, "Example"),
    ("label", 40, 475, 12, "Emergency Contact Relationship"), ("value", 250, 475, 11, "Aunt"),
    ("label", 40, 448, 12, "Reason(s) for beginning psychotherapy at this time"),
    ("value", 40, 432, 11, "Worry about school and trouble sleeping"),
    ("head", 40, 400, 18, "Home Life"),
    ("label", 40, 375, 12, "Please check any of the following difficulties your child has had:"),
    ("value", 255, 358, 11, "Shy and inhibited"), ("value", 390, 358, 11, "Difficulty sleeping"),
    ("label", 40, 330, 12, "Is there a firearm in the home?"), ("value", 255, 330, 11, "Yes"),
    ("label", 40, 307, 12, "If yes, does the child/adolescent have"), ("value", 255, 303, 11, "No"),
    ("label", 40, 293, 12, "access to the firearm in the home?"),
    ("label", 40, 268, 12, "Who is responsible for the bill?"), ("value", 250, 268, 11, "mom and dad"),
    ("value", 40, 248, 11, "By signing below, I confirm the insurance information above is right and complete."),
    ("value", 40, 234, 11, "I will tell the practice about any change to my child's health insurance."),
    ("head", 40, 200, 18, "Your Confidentiality and Private Practice Policies"),
    ("value", 40, 180, 11, "I certify this information is true. All information I share is held in confidence,"),
    ("value", 40, 166, 11, "with the limits the law sets for abuse, neglect and danger to self or others."),
    ("label", 40, 140, 12, "Signature"),
    ("label", 40, 118, 12, "Date"), ("value", 250, 118, 11, "November 10, 2026"),
    ("head", 40, 90, 18, "Release of Information Form"),
    ("label", 40, 68, 12, "I authorize the practice to use and disclose my child's health information to:"),
    ("value", 40, 52, 11, "Dr Example"),
]


def make_pdf(path):
    ops = []
    for kind, x, y, size, text in ROWS:
        t = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"BT /{'F2' if kind != 'value' else 'F1'} {size} Tf {x} {y} Td ({t}) Tj ET")
    stream = "\n".join(ops).encode("latin-1")
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            "/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>",
            None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"]
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        if o is None:
            out += f"{i} 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream\nendobj\n"
        else:
            out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for o in offs:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    open(path, "wb").write(bytes(out))


def script(app, root, V, h):
    import tkinter.filedialog as fd
    import tkinter.messagebox as mb
    ss = h.ss
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    pdf = os.path.join(tempfile.mkdtemp(), "registration.pdf")
    make_pdf(pdf)
    info = ss.read_registration(open(pdf, "rb").read(), ".pdf")
    f = {x["label"]: x for x in info["fields"]}
    val = lambda k: f.get(k, {}).get("value")
    h.ok("the form's title and date are read", info["title"] == "Child and Adolescent Registration"
         and info["form_date"] == "2026-11-10", (info["title"], info["form_date"]))
    h.ok("answers beside their questions are paired with them",
         val("Name") == "Robin Example" and val("Date of birth") == "March 3, 2012"
         and f["Name"]["topic"] == "Patient", {k: v["value"] for k, v in f.items()})
    h.ok("a question over two lines still gets its answer", val("OK to text") == "Yes"
         and f["OK to text"]["topic"] == "Contact preferences")
    h.ok("a guardian's details are gathered into one line; the emergency contact too",
         val("Parent / guardian 1") == "Pat Example — Mother\n(555) 010-2000"
         and val("Emergency contact") == "Lee Example — Aunt", (val("Parent / guardian 1"), val("Emergency contact")))
    h.ok("an answer under its question; ticked boxes listed",
         val("Reason for starting therapy") == "Worry about school and trouble sleeping"
         and val("Early difficulties") == "Shy and inhibited; Difficulty sleeping",
         (val("Reason for starting therapy"), val("Early difficulties")))
    h.ok("the policy wording is left out (not taken as answers)",
         val("Responsible for the bill") == "mom and dad" and "certify" not in ss.registration_text(info)
         and "By signing" not in ss.registration_text(info), ss.registration_text(info)[-500:])
    h.ok("consents: who information may go to, and when it was signed",
         val("Release of information to") == "Dr Example" and val("Signed") == "November 10, 2026")
    order = [x["topic"] for x in info["fields"]]
    h.ok("organized by topic in a set order", order.index("Patient") < order.index("Parents / guardians")
         < order.index("Treatment") < order.index("Safety") < order.index("Consents"), order)
    h.ok("a firearm in the home is flagged", any("Firearm in the home (child has no access)" in x
                                                  for x in ss.registration_flags(info)), ss.registration_flags(info))

    # importing it on a patient
    cid = V.save_client({"code": "REG1"})
    V.save_session({"meta": {"client_id": cid, "session_date": "2026-11-01", "session_number": "1", "kind": "progress",
                             "created": "a"}, "note": "n"})
    app.refresh_clients(); app.open_client(cid); h.pump(root, 0.6)
    fd.askopenfilename = lambda **k: pdf
    mb.askyesno = lambda *a, **k: False                 # (not the PHQ-9 / GAD-7 reading this time)
    app.patient_files(V.load_client(cid)); h.pump(root, 0.3)
    app.files_win["add"]("registration"); h.pump(root, 1)
    app.files_win["win"].destroy()
    c = V.load_client(cid)
    h.ok("importing the form organizes it on the patient", (c.get("registration_info") or {}).get("fields")
         and c["registration_info"]["file_name"] == "registration.pdf")
    rows = [app.page._search["list"].get(i) for i in range(app.page._search["list"].size())]
    reg = next((x for x in h.widgets(app.page) if hasattr(x, "_registration")), None)
    shown = h.texts(app.page)
    h.ok("…and opens its Registration tab: topics, answers, the firearm warning",
         any("Registration" in r for r in rows) and reg is not None and "Robin Example" in shown
         and "Safety" in shown and "Firearm in the home" in shown and "Pat Example — Mother" in shown, rows)
    mb.askyesno = lambda *a, **k: True
    btn = h.button(app.page, "Hide names from AI")
    h.ok("the form's names can be added to the names-to-hide list", btn is not None)
    btn.invoke(); h.pump(root, 0.6)
    names = dict((n, ph) for n, ph in ss.client_name_map(V.load_client(cid)))
    h.ok("…as the patient, the mother and the aunt", names.get("Robin Example") == "[Patient]"
         and names.get("Pat Example") == "[Mother]" and names.get("Lee Example") == "[Aunt]", names)
    h.ok("…and then the button goes away", h.button(app.page, "Hide names from AI") is None)
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script)
