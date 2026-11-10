"""Session Scribe engine check: finds which part of the app's start-up stops PyTorch from loading.
Runs a few short tests, each in its own process, and prints which ones load PyTorch and which freeze.
Touches no patient data and changes nothing. Run:  python engine_check.py"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "session_scribe.py")
LIMIT = 75          # seconds before a test counts as frozen (PyTorch alone takes ~7 s here)

LOAD_APP = ("import importlib.util; _s = importlib.util.spec_from_file_location('ssmod', APP); "
            "_m = importlib.util.module_from_spec(_s); _s.loader.exec_module(_m)\n")
FH_ON = "import faulthandler; faulthandler.enable(file=sys.stderr)\n"
FH_TIMER = "import faulthandler; faulthandler.dump_traceback_later(180, repeat=True, file=sys.stderr)\n"
QUIET_STDOUT = ("_out = os.fdopen(os.dup(1), 'w'); _dn = os.open(os.devnull, os.O_WRONLY); os.dup2(_dn, 1); "
                "sys.stdout = open(os.devnull, 'w')\n")
TORCH = ("t = time.time(); import torch\n"
         "(_out if '_out' in dir() else sys.__stdout__).write('loaded in %.1f s\\n' % (time.time() - t)); "
         "(_out if '_out' in dir() else sys.__stdout__).flush()\n")

READER = ("import threading; threading.Thread(target=lambda: sys.stdin.read(1), daemon=True).start(); "
          "time.sleep(0.5)\n")             # the worker's old way of noticing the app closed
PID_WATCH = ("import threading, ctypes\n"
             "def _w(pid=os.getppid() if os.name != 'nt' else PARENT):\n"
             "    if os.name == 'nt':\n"
             "        k = ctypes.WinDLL('kernel32'); k.OpenProcess.restype = ctypes.c_void_p\n"
             "        h = k.OpenProcess(0x00100000, False, pid); k.WaitForSingleObject(ctypes.c_void_p(h), 0xFFFFFFFF)\n"
             "    else:\n"
             "        time.sleep(10**6)\n"
             "threading.Thread(target=_w, daemon=True).start(); time.sleep(0.5)\n")   # the new way

TESTS = [
    ("1. PyTorch alone", ""),
    ("2. + crash reporter on", FH_ON),
    ("3. + 'where am I stuck' timer", FH_TIMER),
    ("4. + both of those", FH_ON + FH_TIMER),
    ("5. + Session Scribe's start-up code", LOAD_APP),
    ("6. + start-up code, output hidden", LOAD_APP + QUIET_STDOUT),
    ("7. exactly like the app's worker", LOAD_APP + QUIET_STDOUT + FH_ON + FH_TIMER),
    ("8. + waiting to read from the app (OLD)", LOAD_APP + QUIET_STDOUT + FH_ON + FH_TIMER + READER),
    ("9. + watching the app's process (NEW)", LOAD_APP + QUIET_STDOUT + FH_ON + FH_TIMER + PID_WATCH),
]


def run(setup):
    import threading
    code = f"import os, sys, time\nAPP = {APP!r}\nPARENT = {os.getpid()}\n" + setup + TORCH
    p = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,   # kept open, never written,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=HERE)   # like the app's worker
    got = {"out": b"", "err": b""}

    def drain(name, f):
        got[name] = f.read()
    ts = [threading.Thread(target=drain, args=("out", p.stdout), daemon=True),
          threading.Thread(target=drain, args=("err", p.stderr), daemon=True)]
    [t.start() for t in ts]
    try:
        p.wait(timeout=LIMIT)
    except subprocess.TimeoutExpired:
        p.kill(); p.wait()
        [t.join(5) for t in ts]
        p.stdin.close()
        return "FROZE", f"no result after {LIMIT} s", got["err"]
    [t.join(5) for t in ts]
    p.stdin.close()
    out, err = got["out"], got["err"]
    if p.returncode == 0 and b"loaded in" in out:
        return "OK", out.decode(errors="replace").strip(), b""
    return "ERROR", f"exit code {p.returncode}", err


if __name__ == "__main__":
    print(f"Session Scribe engine check — Python {sys.version.split()[0]} — {APP}\n")
    if not os.path.isfile(APP):
        sys.exit("session_scribe.py isn't next to this file.")
    results = []
    for name, setup in TESTS:
        print(f"{name:42}", end="", flush=True)
        status, detail, err = run(setup)
        print(f"{status:6} {detail}", flush=True)
        results.append((name, status, err))
    for name, status, err in results:
        if status != "OK" and err:
            tail = err.decode(errors="replace").strip().splitlines()[-12:]
            print(f"\n--- last lines from {name} ---\n" + "\n".join(tail))
    print("\nDone. Copy everything above and paste it to Claude.")
    if sys.stdin and sys.stdin.isatty():
        input("Press Enter to close.")
