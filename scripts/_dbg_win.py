import os, sys, subprocess, threading, time
from ai_hydro.mcp.stdio_guard import install_stdout_guard
install_stdout_guard()
def log(m): sys.stderr.write(m + "\n"); sys.stderr.flush()
log(f"guard on; fds ok; sys.stdout is stderr: {sys.stdout is sys.stderr}")
if sys.platform == "win32":
    import ctypes
    k = ctypes.windll.kernel32
    k.GetStdHandle.restype = ctypes.c_void_p
    for n, name in ((-10, "in"), (-11, "out"), (-12, "err")):
        log(f"std{name} handle={k.GetStdHandle(n)}")
    import msvcrt
    for fd in (0, 1, 2):
        log(f"fd{fd} osfhandle={msvcrt.get_osfhandle(fd)}")
CODE = "print('child says hi')"
def run(name, **kw):
    t = time.time()
    try:
        r = subprocess.run([sys.executable, "-c", CODE], timeout=20, **kw)
        log(f"{name}: rc={r.returncode} out={r.stdout!r} {time.time()-t:.1f}s")
    except Exception as e:
        log(f"{name}: {e!r} {time.time()-t:.1f}s")
run("inherit")
run("stdout=stderr", stdout=sys.stderr)
run("pipe", stdout=subprocess.PIPE)
run("stdin=devnull", stdin=subprocess.DEVNULL)
th = threading.Thread(target=run, args=("thread-inherit",)); th.start(); th.join()
