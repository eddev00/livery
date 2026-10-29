"""Bubblewrap sandboxes for agents and for rendering.

Two kinds, both built from the same read-only system view (/usr and the /bin, /lib
symlinks, a handful of /etc files), a private /tmp, /proc and /dev:

* render sandbox: no network at all, one writable render root, read-only fonts and tool
  folders, a hard time limit. Used for every Typst compile of a design an agent touched
  and for real-content renders, which see only that document's render root.
* agent sandbox: its own network namespace (the provider is reached through the egress
  proxy, see netbridge), the agent's Livery profile and the sanitized workspace, and
  nothing else of the user's home. No host sockets (no /run), no inherited environment.

When Bubblewrap is missing or user namespaces are disabled, `check()` says so and the
callers refuse to run: an agent is never started without isolation.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .netbridge import BRIDGE_PORT

ETC_FILES = ("passwd", "group", "nsswitch.conf", "localtime", "ld.so.cache", "ld.so.conf", "ld.so.conf.d",
             "ssl", "ca-certificates", "pki", "crypto-policies", "alternatives", "mime.types")
SYSTEM_LINKS = ("bin", "sbin", "lib", "lib64", "lib32")
SANDBOX_HOME = "/home/agent"
WORKSPACE_MOUNT = "/work"
PROXY_MOUNT = "/run/livery"


class SandboxUnavailable(RuntimeError):
    pass


def bwrap_path() -> str | None:
    bundled = os.environ.get("LIVERY_BWRAP")
    if bundled and os.access(bundled, os.X_OK):
        return bundled
    return shutil.which("bwrap")


@dataclass
class SandboxStatus:
    ok: bool
    message: str
    bwrap: str | None = None
    version: str = ""


_status_cache: SandboxStatus | None = None


def check(refresh: bool = False) -> SandboxStatus:
    """Can we build an isolated sandbox here? Runs a trivial one to find out."""
    global _status_cache
    if _status_cache is not None and not refresh:
        return _status_cache
    b = bwrap_path()
    if b is None:
        _status_cache = SandboxStatus(False, "Bubblewrap (bwrap) is not installed. Install it with your package "
                                             "manager, e.g. `sudo apt install bubblewrap` or `sudo pacman -S bubblewrap`.")
        return _status_cache
    version = subprocess.run([b, "--version"], capture_output=True, text=True).stdout.strip()
    probe = subprocess.run(_base(b) + ["--unshare-all", "--", "/bin/sh", "-c", "echo sandbox-ok"],
                           capture_output=True, text=True, timeout=20)
    if probe.returncode != 0 or "sandbox-ok" not in probe.stdout:
        _status_cache = SandboxStatus(False, "Bubblewrap could not create a sandbox (unprivileged user namespaces "
                                             "may be disabled): " + (probe.stderr.strip() or "no output"), b, version)
    else:
        _status_cache = SandboxStatus(True, "Bubblewrap sandbox available", b, version)
    return _status_cache


def require() -> str:
    st = check()
    if not st.ok or st.bwrap is None:
        raise SandboxUnavailable(st.message)
    return st.bwrap


def _base(bwrap: str) -> list[str]:
    """Read-only system view shared by both sandbox kinds."""
    args = [bwrap, "--die-with-parent", "--new-session", "--cap-drop", "ALL",
            "--ro-bind", "/usr", "/usr", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--clearenv", "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin", "--setenv", "LANG", "C.UTF-8"]
    for name in SYSTEM_LINKS:
        p = Path("/") / name
        if p.is_symlink():
            args += ["--symlink", os.readlink(p), str(p)]
        elif p.is_dir():
            args += ["--ro-bind", str(p), str(p)]
    for name in ETC_FILES:
        p = Path("/etc") / name
        if p.exists():
            args += ["--ro-bind", str(p.resolve()), str(p)]
    return args


def _ro_binds(paths: list[Path]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for p in paths:
        rp = str(Path(p).resolve())
        if rp in seen or rp.startswith("/usr/"):
            continue
        seen.add(rp)
        out += ["--ro-bind", rp, rp]
    return out


# ------------------------------------------------------------------ render sandbox
def tool_dirs() -> list[Path]:
    """Folders holding the render tools (typst, poppler) when they are not under /usr."""
    from ..tools import find_tool
    dirs = []
    for name in ("typst", "pdftoppm", "pdftotext"):
        p = find_tool(name)
        if p is not None:
            dirs.append(Path(p).resolve().parent)
    bundled = os.environ.get("LIVERY_TOOLS_DIR")
    if bundled:
        # the whole bundled tools folder: bin/ holds wrappers, libexec/ and lib/ what they run
        dirs.append(Path(bundled).resolve().parent)
    return dirs


def render_command(cmd: list[str], root: Path, ro: list[Path], timeout_s: int = 120) -> list[str]:
    """`cmd` inside an offline sandbox that can write only `root` and read `ro` (fonts, tools)."""
    b = require()
    args = ["timeout", "-s", "KILL", str(timeout_s)] + _base(b) + ["--unshare-all"]
    args += _ro_binds(ro + tool_dirs())
    rr = str(Path(root).resolve())
    args += ["--bind", rr, rr, "--chdir", rr]
    env_keep = [k for k in ("LIVERY_TOOLS_DIR", "LD_LIBRARY_PATH") if os.environ.get(k)]
    for k in env_keep:
        args += ["--setenv", k, os.environ[k]]
    return args + ["--"] + [str(c) for c in cmd]


# ------------------------------------------------------------------ agent sandbox
def bridge_argv() -> tuple[list[str], list[Path]]:
    """How to start netbridge inside the sandbox, and what that needs mounted."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        return [str(exe), "netbridge"], [exe.parent]
    pkg_root = Path(__file__).resolve().parents[2]
    needs = [Path(sys.executable).resolve().parent, Path(sys.prefix), Path(sys.base_prefix), pkg_root / "livery"]
    return [str(Path(sys.executable).resolve()), "-I", "-c",
            f"import sys; sys.path.insert(0, {str(pkg_root)!r}); from livery.desktop.netbridge import main; main()"], needs


def agent_command(argv: list[str], *, workspace: Path, profile: Path, profile_mount: str,
                  proxy_socket: Path | None, env: dict[str, str], ro: list[Path]) -> list[str]:
    """`argv` (the agent CLI) in an agent sandbox. The workspace is /work, the profile is
    mounted at `profile_mount` under the sandbox home, the proxy socket at /run/livery."""
    b = require()
    args = _base(b) + ["--unshare-all", "--hostname", "livery",
                       "--tmpfs", SANDBOX_HOME, "--setenv", "HOME", SANDBOX_HOME,
                       "--setenv", "TERM", "dumb", "--setenv", "USER", "agent",
                       "--bind", str(Path(workspace).resolve()), WORKSPACE_MOUNT,
                       "--bind", str(Path(profile).resolve()), profile_mount,
                       "--setenv", "PATH", f"{WORKSPACE_MOUNT}/bin:/usr/local/bin:/usr/bin:/bin",
                       "--chdir", WORKSPACE_MOUNT]
    bridge, bridge_needs = bridge_argv()
    args += _ro_binds(list(ro) + (bridge_needs if proxy_socket else []))
    for k, v in env.items():
        args += ["--setenv", k, v]
    if proxy_socket is None:
        return args + ["--"] + argv
    sock = f"{PROXY_MOUNT}/proxy.sock"
    proxy = f"http://127.0.0.1:{BRIDGE_PORT}"
    args += ["--bind", str(Path(proxy_socket).resolve()), sock]
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        args += ["--setenv", k, proxy]
    args += ["--setenv", "NO_PROXY", "", "--setenv", "no_proxy", ""]
    # Start the bridge, wait until it listens, then become the agent.
    bridge_full = bridge + ["--listen", str(BRIDGE_PORT), "--socket", sock, "--ready", "/tmp/.bridge-ready"]
    return args + ["--", "/bin/sh", "-c", _launcher(len(bridge_full)), "sh"] + bridge_full + argv


def _launcher(n: int) -> str:
    """sh script: start the first n args (the bridge) in the background, wait for it, exec the rest."""
    bridge_args = " ".join(f'"${{{i}}}"' for i in range(1, n + 1))
    return (f"{bridge_args} & i=0; while [ ! -e /tmp/.bridge-ready ] && [ $i -lt 200 ]; do sleep 0.05; i=$((i+1)); done; "
            f"shift {n}; exec \"$@\"")
