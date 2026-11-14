"""Mobile mode (remote desktop from a phone): Settings switch (the old Phone section is gone), the window fills
the screen and follows size changes, and held upright the patient list hides behind ☰ Patients and a
patient's list sits above the open document. Made-up data only."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H


def script(app, root, V, h):
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    cid = V.save_client({"code": "MOB1"})
    V.save_session({"meta": {"client_id": cid, "session_date": "2026-11-01", "session_number": "1", "kind": "progress",
                             "created": "a"}, "note": "Session note."})
    app.refresh_clients(); h.pump(root, 0.5)

    app.open_setup(); h.pump(root, 1)
    sw = app.setup_win["win"]
    st = h.texts(sw)
    h.ok("Settings: no Phone section any more; a Mobile mode section instead",
         "Phone access" not in st and "Make the phone page" not in st and "Mobile mode on this computer" in st)
    ui = app._mobile_ui
    ui["size"].set("Normal size")
    ui["var"].set(True); h.pump(root, 0.6)
    h.ok("turning it on (normal size) saves it, with nothing to restart",
         h.ss.load_config().get("mobile_mode") is True and not ui["note"].cget("text"))
    sw.withdraw()

    # upright phone
    root.state("normal"); root.geometry("700x1150+0+0"); h.pump(root, 1.2)
    h.ok("upright: the patient list hides behind ☰ Patients",
         app.m_list_btn.winfo_ismapped() and not app.side.winfo_ismapped() and app.main.winfo_ismapped())
    h.ok("…the header drops the 'Offline' caption", not app.offline_lbl.winfo_ismapped())
    app.m_list_btn.invoke(); h.pump(root, 0.3)
    h.ok("☰ Patients shows the list in place of the page", app.side.winfo_ismapped() and not app.main.winfo_ismapped()
         and "Close list" in str(app.m_list_btn.cget("text")))
    app.client_lb.selection_set(0); app.on_client_select(); h.pump(root, 1)
    h.ok("picking a patient goes back to the page", not app.side.winfo_ismapped() and app.main.winfo_ismapped()
         and app.client_id == cid)
    h.ok("…and the patient's list sits above the open document", getattr(app.page, "_stacked", None) is True)

    # sideways / wide
    s = max(1.0, root.winfo_fpixels("1i") / 96.0)          # (this screen's scaling)
    root.geometry(f"{int(960 * s)}x{int(620 * s)}+0+0"); h.pump(root, 1.2)
    h.ok("wide: the usual layout (list on the left, side by side)", app.side.winfo_ismapped()
         and not app.m_list_btn.winfo_ismapped() and getattr(app.page, "_stacked", None) is False
         and app.client_id == cid)

    # the remote desktop changes the screen's size: the window fills it again
    app.screen_size = lambda: (1080, 2340)
    app._m_screen = (1920, 1080)
    app.mobile_watch(); h.pump(root, 0.6)
    h.ok("a new screen size: the window is maximized to fill it", root.state() == "zoomed", root.state())
    root.state("normal")

    # off again
    app.set_mobile_mode(False); root.geometry("700x1000+0+0"); h.pump(root, 1)
    h.ok("off: a narrow window keeps the usual layout", app.side.winfo_ismapped() and not app.m_list_btn.winfo_ismapped())
    h.ok("…and the setting is saved off", h.ss.load_config().get("mobile_mode") is False)
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script)
