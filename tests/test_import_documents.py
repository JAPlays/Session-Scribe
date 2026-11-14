"""Write ▸ Import document: your own document types (Settings), imported documents on the Documents tab with the
original file, and the AI using them (case overview, Ask AI)."""
import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H


def script(app, root, V, h):
    ss = h.ss
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    cid = V.save_client({"code": "DOC1"})
    V.save_session({"meta": {"client_id": cid, "session_date": "2026-10-01", "session_number": "1", "kind": "progress",
                             "created": "x"}, "note": "Session one note."})
    app.refresh_clients(); app.open_client(cid); h.pump(root, 1)
    h.ok("the Write menu says Import document…", h.action(app.page, "Import document") is not None
         and h.action(app.page, "Import past notes") is None)
    h.ok("Psychological assessment is a type out of the box", "Psychological assessment" in app.doc_types())

    # Settings: add a type
    app.open_setup(); h.pump(root, 2)
    sw = app.setup_win["win"]
    dt = sw._doc_types
    dt["var"].set("Neuropsychological report"); dt["add"](); h.pump(root, 0.3)
    h.ok("Settings adds a new document type (saved in the vault settings)",
         "Neuropsychological report" in app.doc_types() and "Neuropsychological report" in
         (V.load_settings() or {}).get("doc_types", []))
    dt["var"].set("psychological ASSESSMENT"); dt["add"](); h.pump(root, 0.2)
    h.ok("…but not a duplicate", sum(t.lower() == "psychological assessment" for t in app.doc_types()) == 1)
    _ = sw.withdraw()

    # import a psychological assessment from a file
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "Psych eval 2025.txt")
    open(path, "w").write("PSYCHOLOGICAL ASSESSMENT\nWAIS-IV FSIQ 104. Diagnostic impressions: ADHD, combined "
                          "presentation. Recommendations: extended test time.")
    h.action(app.page, "Import document").invoke(); h.pump(root, 0.5)
    iw = app._import_win
    combo = [w for w in h.widgets(iw, "TCombobox")][0]
    h.ok("the Type list has the built-in note types and your document types",
         {"Progress note", "Psychological assessment", "Neuropsychological report"} <= set(combo.cget("values")))
    iw._test["add_texts"]([("Psych eval 2025.txt", open(path).read())], [path]); h.pump(root, 0.3)
    it = iw._test["items"][0]
    iw._test["date"].set("2025-06-01")
    combo.set("Psychological assessment"); h.pump(root, 0.2)
    h.ok("choosing the type marks the entry as that document type", it["kind"] == "doc:Psychological assessment",
         it["kind"])
    iw._test["do_import"](); h.pump(root, 1)
    c = V.load_client(cid)
    docs = c.get("documents") or []
    h.ok("it's saved as a document, not a session", len(docs) == 1 and docs[0]["type"] == "Psychological assessment"
         and len(V.sessions_for(cid)) == 1, docs)
    fe = next((f for f in c.get("files") or [] if f["id"] == docs[0].get("file_id")), None)
    h.ok("…with the original file kept, encrypted", fe is not None and fe["kind"] == "document"
         and b"FSIQ 104" in V.load_file(fe["id"]))
    lb = app.page._search["list"]
    rows = [lb.get(i) for i in range(lb.size())]
    h.ok("the patient has a Documents tab, open on it", "📁  Documents  · 1" in rows
         and "Psychological assessment" in h.texts(app.page), rows)
    vw = app.doc_view(c, docs[0]); h.pump(root, 0.3)
    h.ok("View shows the document", "extended test time" in "\n".join(
        w.get("1.0", "end") for w in h.widgets(vw, "Text"))); vw.destroy()

    # the AI uses it
    ask = ss.build_ask_prompt(V.load_client(cid), V.sessions_for(cid), "What did the psychological assessment find?",
                              [], {}, "")
    h.ok("Ask AI includes it when asked about the assessment", "FSIQ 104" in str(ask))
    h.RESP[:] = [("", "CASE SNAPSHOT\nOk.")]
    n0 = len(h.PROMPTS)
    ss.generate_case_overview(dict(ss.load_config()), V.load_client(cid), [d for _, d in V.sessions_for(cid)], "",
                              lambda *a, **k: None)
    h.ok("the case overview gets it as background", any("FSIQ 104" in p for p in h.PROMPTS[n0:]))

    # .docx files formatted like real reports (tab stops, tables, styled runs): only the words come through
    import zipfile
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = (
        '<w:p><w:pPr><w:pBdr><w:left w:val="nil"/></w:pBdr><w:tabs><w:tab w:val="left" w:pos="720"/></w:tabs>'
        '<w:spacing w:after="0"/><w:jc w:val="right"/><w:rPr><w:rFonts w:ascii="Spectral"/></w:rPr></w:pPr>'
        '<w:r><w:rPr><w:b w:val="0"/></w:rPr><w:t xml:space="preserve">Reason for referral: </w:t></w:r>'
        '<w:r><w:tab/><w:t>attention concerns.</w:t></w:r></w:p>'
        '<w:tbl><w:tblPr><w:tblW w:w="0"/></w:tblPr><w:tr><w:tc><w:tcPr><w:tcW w:w="100"/></w:tcPr>'
        '<w:p><w:r><w:t>WAIS-IV FSIQ</w:t></w:r></w:p></w:tc><w:tc><w:tcPr/><w:p><w:r><w:t>104</w:t></w:r></w:p>'
        '</w:tc></w:tr></w:tbl>'
        '<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Diagnosis: ADHD &amp; anxiety</w:t></w:r></w:p>')
    dx = os.path.join(tmp, "Report.docx")
    with zipfile.ZipFile(dx, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {W}><w:body>{body}</w:body></w:document>')
    text = ss.extract_text(dx)
    h.ok("a formatted .docx gives only its words", "<w:" not in text and "Reason for referral: attention concerns."
         in text and "WAIS-IV FSIQ" in text and "104" in text and "Diagnosis: ADHD & anxiety" in text, text)

    # a document imported before the fix (XML in its text) is repaired from its kept original
    fid = app.patient_file_add(cid, dx, "document", note="Psychological assessment")["id"]
    c = V.load_client(cid)
    c["documents"].append({"id": "bad1", "type": "Psychological assessment", "date": "2024-01-01", "title": "Report.docx",
                           "text": 'Reason<w:pPr><w:spacing w:after="0"/></w:pPr> junk', "file_id": fid, "added": "x"})
    V.save_client(c)
    V.save_session({"meta": {"client_id": cid, "session_date": "2024-02-01", "session_number": "", "kind": "progress",
                             "source": "imported", "created": "x"},
                    "note": 'Old note<w:rPr><w:b w:val="0"/></w:rPr> text', "transcript": ""})
    app.open_client(cid, select="docs"); h.pump(root, 1)
    c = V.load_client(cid)
    fixed = next(d for d in c["documents"] if d["id"] == "bad1")
    h.ok("opening the patient re-reads the damaged document from its original", "<w:" not in fixed["text"]
         and "Diagnosis: ADHD & anxiety" in fixed["text"], fixed["text"][:200])
    old = next(d for _, d in V.sessions_for(cid) if d["meta"].get("session_date") == "2024-02-01")
    h.ok("…and strips the XML out of an imported note", old["note"] == "Old note text", old["note"])

    # the import window recognizes a report from its title
    h.action(app.page, "Import document").invoke(); h.pump(root, 0.5)
    iw = app._import_win
    iw._test["add_texts"]([("eval.txt", "Confidential\nPSYCHOLOGICAL EVALUATION\nTests administered: WISC-V.\n"
                                        "The client was cooperative.")]); h.pump(root, 0.2)
    h.ok("a psychological evaluation is typed as one automatically (not a progress note)",
         iw._test["items"][-1]["kind"] == "doc:Psychological assessment", iw._test["items"][-1]["kind"])
    iw._test["add_texts"]([("note.txt", "Session focused on sleep. Client was referred for psychological testing "
                                        "later this year by her doctor, and we discussed coping skills at length.")])
    h.ok("…but a note that only mentions testing stays a note", iw._test["items"][-1]["kind"] == "progress")
    iw.destroy()

    # an assessment imported earlier as a session: move it to Documents
    sid = V.save_session({"meta": {"client_id": cid, "session_date": "2023-05-05", "session_number": "",
                                   "kind": "progress", "source": "imported", "created": "x"},
                          "note": "PSYCHOLOGICAL ASSESSMENT\nWISC-V results...", "transcript": ""})
    app.open_client(cid, select="day:2023-05-05"); h.pump(root, 1)
    h.ok("an imported entry has Move to Documents", h.button(app.page, "Move to Documents") is not None)
    h.button(app.page, "Move to Documents").invoke(); h.pump(root, 0.4)
    mw = app._move_doc_win
    h.ok("…which guesses the type", mw["type"].get() == "Psychological assessment", mw["type"].get())
    mw["go"](); h.pump(root, 1)
    c = V.load_client(cid)
    h.ok("it leaves the session list and is on the Documents tab", sid not in [s for s, _ in V.sessions_for(cid)]
         and any(d.get("date") == "2023-05-05" and "WISC-V results" in d["text"] for d in c["documents"]))
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script)
