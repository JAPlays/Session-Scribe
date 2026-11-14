"""Fingerprint unlock (Windows Hello), with a stand-in for Windows Hello: set up in Settings, unlock the vault,
the lock screen and the assessment hand-back with it; fall back to DPAPI; a cancelled scan unlocks nothing."""
import sys, os, base64, hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import H


def script(app, root, V, h):
    ss = h.ss
    errors = []
    root.report_callback_exception = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a)))
    if not ss.IS_WIN:
        h.ok("(Windows only — skipped)", True)
        return
    fake = {"det": True, "verify": "Verified", "fail": None, "calls": []}

    def hello(mode, name="", arg=""):
        fake["calls"].append(mode)
        if fake["fail"]:
            raise RuntimeError(ss._HELLO_WORDS[fake["fail"]])
        if mode == "create":
            return "created"
        if mode == "sign":
            extra = b"" if fake["det"] else os.urandom(4)
            return base64.b64encode(hashlib.sha256(name.encode() + arg.encode() + extra).digest()).decode()
        return {"verify": fake["verify"], "delete": "deleted", "available": "Available"}[mode]
    ss._hello_run = hello
    ss._hello_front = lambda: None

    h.ok("off to begin with", ss.hello_state(V) is None)
    app.open_setup(); h.pump(root, 2)
    sw = app.setup_win["win"]
    hw = sw._hello
    hw["on"](); h.pump(root, 0.3)
    dlg = sw._hello_dlg
    dlg["entry"].insert(0, "wrong"); dlg["go"](); h.pump(root, 0.2)
    h.ok("turning it on asks for the vault password", ss.hello_state(V) is None and dlg["win"].winfo_exists())
    dlg["entry"].insert(0, "testpassword1"); dlg["go"](); h.pump(root, 1.5)
    st = ss.hello_state(V)
    h.ok("then Windows Hello creates a key and signs twice; the vault key is stored wrapped by its signature",
         st and st["mode"] == "sign" and fake["calls"][:3] == ["create", "sign", "sign"] and "On for this computer"
         in str(hw["info"].cget("text")), (st, fake["calls"]))
    raw = open(ss.HELLO_FILE, encoding="utf-8").read()
    h.ok("the vault key isn't readable in the file, and the file is in this computer's app folder, not the vault",
         V.raw_key().decode() not in raw and os.path.dirname(ss.HELLO_FILE) == ss.LOCAL_DIR
         and not ss.HELLO_FILE.startswith(V.folder))
    h.ok("the key from Windows Hello unlocks the vault", V.key_ok(ss.hello_key(V)) and V.unlock_key(ss.hello_key(V)))
    sw.withdraw()

    # lock screen: nothing pops up while you're away; it asks as soon as you're back
    fake["calls"].clear()
    app.lock(); h.pump(root, 0.6)
    h.ok("locking doesn't open the prompt while nobody's there", "sign" not in fake["calls"] and app.locked)
    fake["fail"] = "UserCanceled"
    app._lock_wake(); h.pump(root, 1.5)
    h.ok("coming back opens it; Cancel stops after one try and points to the password",
         app.locked and fake["calls"].count("sign") == 1 and "type your password" in h.texts(root), fake["calls"])
    fake["fail"] = "DeviceBusy"
    fake["calls"].clear()
    app._lock_hello(); h.pump(root, 4)
    h.ok("a failing scan is tried 3 times, then it's the password", app.locked and fake["calls"].count("sign") == 3
         and "type your password" in h.texts(root), fake["calls"])
    fake["fail"] = None
    app._lock_hello(); h.pump(root, 0.8)
    h.ok("the fingerprint button starts a fresh try and a good scan unlocks", not app.locked)

    # the assessment hand-back
    cid = V.save_client({"code": "WH1"})
    app.refresh_clients()
    app.run_assessment(V.load_client(cid), "who5", "adult"); h.pump(root, 0.4)
    run = app._assess_run
    run["ask"]("1")
    for i in range(1, 6):
        run["choose"](str(i), 3, 0); h.pump(root, 0.1)
    W = run["win"]
    h.pump(root, 0.3); run["unlock"](); h.pump(root, 1.2)
    h.ok("the counselor screen asks for the fingerprint by itself and shows the results",
         not W.winfo_exists() and app._results_win["win"].winfo_exists())
    app._results_win["win"].destroy()

    # fallback when signatures differ each time
    sw.deiconify(); hw["off"](); h.pump(root, 0.8)
    h.ok("turning it off removes it", ss.hello_state(V) is None and "delete" in fake["calls"])
    fake["det"] = False
    hw["on"](); h.pump(root, 0.3)
    sw._hello_dlg["entry"].insert(0, "testpassword1"); sw._hello_dlg["go"](); h.pump(root, 1.5)
    st = ss.hello_state(V)
    h.ok("if this PC's Hello signs differently each time: Windows account encryption + a Hello check",
         st and st["mode"] == "consent" and V.key_ok(ss.hello_key(V)))
    fake["verify"] = "Canceled"
    try:
        ss.hello_key(V)
        refused = False
    except RuntimeError:
        refused = True
    h.ok("…and without the Hello check it gives nothing", refused)
    st["vault"] = "another-vault"
    open(ss.HELLO_FILE, "w", encoding="utf-8").write(__import__("json").dumps(st))
    h.ok("a setup for a different vault is ignored", ss.hello_state(V) is None)
    sw.withdraw()
    h.ok("no errors", not errors, errors[-1:] if errors else "")


H.start(script)
