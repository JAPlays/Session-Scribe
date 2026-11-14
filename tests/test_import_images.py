"""Pictures in imported files are kept: a Word document's, a PDF's and an email's (inline or attached) are
saved encrypted with the document, imported note or contact-log entry, shown in an Images gallery, and
deleted with it. Made-up files only."""
import sys, os, io, random, tempfile, zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H


def png(seed, size=(60, 40)):
    from PIL import Image
    rnd = random.Random(seed)
    im = Image.new("RGB", size)
    im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(size[0] * size[1])])
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def script(app, root, V, h):
    ss = h.ss
    import tkinter.filedialog as fd
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    d = tempfile.mkdtemp()
    # a Word document with a picture
    docx = os.path.join(d, "Evaluation.docx")
    ss.write_docx(docx, [("p", "Psychological evaluation. Testing results are shown in the chart below.")])
    with zipfile.ZipFile(docx, "a") as z:
        z.writestr("word/media/image1.png", png(1))
    # a PDF that is a picture
    from PIL import Image
    pdf = os.path.join(d, "scan.pdf")
    Image.open(io.BytesIO(png(2, (120, 80)))).save(pdf, "PDF")
    # an email with an inline picture and an attached one
    from email.message import EmailMessage
    from email.utils import format_datetime
    from datetime import datetime
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "Pat Parent <pat@mail.example>", "Sam <sam@clinic.example>", "Drawing"
    m["Date"] = format_datetime(datetime(2026, 11, 10, 9, 0).astimezone())
    m.set_content("Here is the drawing he made at school, and his report card.")
    m.add_attachment(png(3), maintype="image", subtype="png", filename="drawing.png")
    m.add_attachment(png(4), maintype="image", subtype="png", filename="report.png")
    eml = os.path.join(d, "drawing.eml")
    open(eml, "wb").write(bytes(m))

    h.ok("pictures are found in a Word document, a PDF and an email",
         len(ss.extract_images(open(docx, "rb").read(), ".docx")) == 1
         and len(ss.extract_images(open(pdf, "rb").read(), ".pdf")) >= 1
         and [i["name"] for i in ss.extract_images(open(eml, "rb").read(), ".eml")] == ["drawing.png", "report.png"])

    cid = V.save_client({"code": "IMG1"})
    V.save_session({"meta": {"client_id": cid, "session_date": "2026-10-01", "session_number": "1", "kind": "progress",
                             "created": "a"}, "note": "n"})
    app.refresh_clients(); app.open_client(cid); h.pump(root, 0.5)

    # Write ▸ Import document: a document and a note, each from its own file
    app.import_dialog(V.load_client(cid)); h.pump(root, 0.5)
    t = app._import_win._test
    t["add_texts"]([("Evaluation.docx", ss.extract_text(docx))], paths=[docx])
    t["add_texts"]([("scan.pdf", "Progress note. Patient discussed school.")], paths=[pdf])
    its = t["items"]
    its[0].update(kind="doc:Psychological evaluation", date="2026-09-15")
    its[1].update(kind="progress", date="2026-10-08", number="2")
    t["date"].set("2026-10-08")                       # (the selected entry is the second: its date field)
    t["do_import"](); h.pump(root, 1)
    docs = V.load_client(cid).get("documents") or []
    h.ok("an imported document keeps its pictures (encrypted)", docs and len(docs[0].get("images") or []) == 1
         and V.load_file(docs[0]["images"][0]["id"]) == png(1), docs[:1])
    sess = [dd for _, dd in V.sessions_for(cid) if dd["meta"].get("session_date") == "2026-10-08"]
    h.ok("…so does an imported note", sess and len(sess[0].get("images") or []) >= 1)

    gw = app.image_gallery("Evaluation", docs[0]["images"]); h.pump(root, 0.5)
    h.ok("the Images gallery shows them", gw._gallery["shown"] == 1); gw.destroy()
    app.open_client(cid, select="docs"); h.pump(root, 0.8)
    h.ok("the document has an Images button", h.button(app.page, "Images (1)") is not None)

    # an email loaded into the contact log
    app.external_comm(V.load_client(cid)); h.pump(root, 0.5)
    w = app.parent_win
    fd.askopenfilenames = lambda **k: [eml]
    w["load"](); h.pump(root, 0.3)
    w["go"].invoke(); h.pump(root, 1)
    log = V.load_client(cid).get("communications") or []
    e = next((x for x in log if x.get("date") == "2026-11-10"), None)
    h.ok("an email's pictures go with its date in the contact log",
         e and [i["name"] for i in e.get("images") or []] == ["drawing.png", "report.png"], log)
    app.open_client(cid, select="comms"); h.pump(root, 0.8)
    h.ok("…with an Images button there", h.button(app.page, "Images (2)") is not None)

    # removing deletes the pictures too
    import tkinter.messagebox as mb
    mb.askyesno = lambda *a, **k: True
    fid = docs[0]["images"][0]["id"]
    app.doc_remove(V.load_client(cid), docs[0]); h.pump(root, 0.4)
    try:
        V.load_file(fid)
        gone = False
    except Exception:
        gone = True
    h.ok("removing a document deletes its pictures from the vault", gone)
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script)
