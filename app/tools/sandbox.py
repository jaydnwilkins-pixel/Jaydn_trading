"""`run_python` sandbox (DESIGN.md §9): numpy/pandas over data the TOOL
fetches, executed inside a bubblewrap (bwrap) jail.

Isolation (enforced by the kernel, not by Python-level monkeypatching):
- fresh user/pid/net/ipc/uts/cgroup namespaces (`--unshare-all`): no network
  at all — only an isolated loopback, so neither `_socket`, a reloaded
  `socket` module nor `subprocess`+curl can reach the host or the internet;
- the filesystem is a fresh root containing ONLY read-only /usr (plus the
  /bin, /lib... symlinks), the venv (read-only, at /venv), a private /proc,
  a minimal /dev and size-capped tmpfs /work and /tmp. $HOME, the repo,
  etc/claire.env, var/desk.db and ~/.claude are simply not in the namespace;
- clean environment (`--clearenv`), all capabilities dropped, new session;
- RLIMIT_NPROC / AS / CPU / FSIZE / NOFILE / CORE applied before the agent
  code runs (hard limits, cannot be raised again);
- on timeout the whole process group is SIGKILLed; because the code runs in
  its own pid namespace with `--die-with-parent`, killing bwrap tears down
  every process inside the namespace, including setsid()'d grandchildren.

If bwrap is missing or cannot create the namespaces, run_python FAILS CLOSED:
it returns an error and never runs the code unsandboxed.

Data in: JSON on stdin. Result out: JSON on fd 1 (agent prints are sent to
stderr). Both captured into host temp files bounded by RLIMIT_FSIZE.
"""
import functools
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

UNAVAILABLE = ("run_python is unavailable: no working sandbox (bubblewrap) on "
               "this host, so the code was NOT executed. Do the calculation "
               "without run_python.")

_LIMITS = {
    "cpu_s": 10,
    "as_bytes": 1024 * 1024 * 1024,
    "fsize_bytes": 8 * 1024 * 1024,
    "nproc": 64,          # RLIMIT_NPROC is counted per user namespace
    "nofile": 256,
}
_TMPFS_BYTES = 64 * 1024 * 1024
_MAX_STDOUT = 4 * 1024 * 1024
_HOST_ENV = {"PATH": "/usr/bin:/bin"}

_PRELUDE = r"""
import os, sys, json, resource
_L = json.loads(sys.argv[1])
for _r, _v in ((resource.RLIMIT_CPU, _L["cpu_s"]),
               (resource.RLIMIT_AS, _L["as_bytes"]),
               (resource.RLIMIT_FSIZE, _L["fsize_bytes"]),
               (resource.RLIMIT_NPROC, _L["nproc"]),
               (resource.RLIMIT_NOFILE, _L["nofile"]),
               (resource.RLIMIT_CORE, 0)):
    resource.setrlimit(_r, (_v, _v))
_payload = json.load(sys.stdin)
_out = os.fdopen(os.dup(1), "w")
os.dup2(2, 1)                      # agent prints -> stderr; RESULT -> _out
sys.stdout = sys.stderr
del _L, _r, _v
DATA = _payload["data"]   # the OHLCV/context dict the tool fetched for the agent
RESULT = None
_code = _payload.pop("code")
del _payload
exec(compile(_code, "<agent-code>", "exec"))
json.dump({"result": RESULT}, _out, default=str)
_out.flush()
"""


def _python_layout(python: str) -> tuple[list[str], str]:
    """bwrap args exposing the interpreter (+ the venv's site-packages,
    read-only) and the in-sandbox path to execute."""
    exe = Path(python)
    venv = exe.parent.parent
    if (venv / "pyvenv.cfg").is_file():
        return ["--ro-bind", str(venv), "/venv"], f"/venv/bin/{exe.name}"
    real = exe.resolve()
    if not str(real).startswith("/usr/"):
        raise RuntimeError(f"interpreter {real} is outside /usr and no venv")
    return [], str(real)


def _root_args() -> list[str]:
    args = ["--ro-bind", "/usr", "/usr"]
    for d in ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32"):
        p = Path(d)
        if p.is_symlink():
            args += ["--symlink", os.readlink(d), d]
        elif p.is_dir():
            args += ["--ro-bind", d, d]
    args += ["--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache"]
    return args


def _bwrap_argv(bwrap: str, python: str) -> list[str]:
    py_bind, py_exe = _python_layout(python)
    return [bwrap,
            "--unshare-all", "--die-with-parent", "--new-session",
            "--cap-drop", "ALL", "--hostname", "sandbox",
            "--clearenv",
            "--setenv", "PATH", "/usr/bin:/bin",
            "--setenv", "HOME", "/work",
            "--setenv", "TMPDIR", "/tmp",
            "--setenv", "LANG", "C.UTF-8",
            "--setenv", "OMP_NUM_THREADS", "1",
            "--setenv", "OPENBLAS_NUM_THREADS", "1",
            *_root_args(), *py_bind,
            "--proc", "/proc", "--dev", "/dev",
            "--size", str(_TMPFS_BYTES), "--tmpfs", "/work",
            "--size", str(_TMPFS_BYTES), "--tmpfs", "/tmp",
            "--remount-ro", "/",
            "--chdir", "/work",
            "--", py_exe, "-I", "-B", "-c", _PRELUDE, json.dumps(_LIMITS)]


def _find_bwrap() -> str | None:
    # never resolve via the caller's PATH: only trusted system locations
    return shutil.which("bwrap", path="/usr/bin:/bin:/usr/local/bin")


@functools.lru_cache(maxsize=None)
def sandbox_available(python: str = sys.executable) -> bool:
    """True only if bwrap exists AND can actually build the jail here
    (AppArmor / unprivileged-userns restrictions can block it)."""
    bwrap = _find_bwrap()
    if not bwrap:
        return False
    try:
        argv = _bwrap_argv(bwrap, python)
        p = subprocess.run(argv, input='{"code": "RESULT = 1", "data": {}}',
                           capture_output=True, text=True, timeout=30,
                           env=_HOST_ENV, cwd="/")
        return p.returncode == 0 and json.loads(p.stdout) == {"result": 1}
    except (RuntimeError, OSError, subprocess.TimeoutExpired,
            json.JSONDecodeError):
        return False


def _killpg(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_python(code: str, data: dict, *, timeout=15,
               python=sys.executable) -> dict:
    """Execute agent-authored analysis code in the bwrap jail. The code sees
    `DATA` (dict) and must set `RESULT`. Returns {result} or {error}."""
    if not sandbox_available(python):
        return {"error": UNAVAILABLE}
    argv = _bwrap_argv(_find_bwrap(), python)
    payload = json.dumps({"code": code, "data": data}, default=str)
    with tempfile.TemporaryDirectory(prefix="claire-sbx-") as td:
        td = Path(td)
        (td / "in.json").write_text(payload)
        with open(td / "in.json", "rb") as fin, \
                open(td / "out", "w+b") as fout, \
                open(td / "err", "w+b") as ferr:
            proc = subprocess.Popen(argv, stdin=fin, stdout=fout, stderr=ferr,
                                    env=_HOST_ENV, cwd="/",
                                    start_new_session=True, close_fds=True)
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # bwrap's process group; the pid namespace (and every
                # process in it) dies with bwrap via --die-with-parent.
                _killpg(proc.pid)
                proc.kill()
                proc.wait()
                return {"error": f"timed out after {timeout}s"}
            fout.seek(0)
            out = fout.read(_MAX_STDOUT + 1)
            ferr.seek(0, os.SEEK_END)
            ferr.seek(max(0, ferr.tell() - 2000))
            err = ferr.read().decode("utf-8", "replace")
    if rc != 0:
        if err.startswith("bwrap:"):
            return {"error": f"{UNAVAILABLE} ({err.strip()[:300]})"}
        if rc < 0 or rc > 128:
            sig = -rc if rc < 0 else rc - 128
            err = f"killed by signal {sig} (resource limit hit?)\n{err}"
        return {"error": (err or "failed")[-2000:]}
    if len(out) > _MAX_STDOUT:
        return {"error": "RESULT too large"}
    try:
        return json.loads(out)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"error": "code did not produce RESULT"}
