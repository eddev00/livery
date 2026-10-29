"""Network for sandboxed agents, without giving them the host's network.

An agent runs in its own network namespace (Bubblewrap --unshare-net): it cannot reach the
host's loopback services, abstract sockets or LAN. Two small pieces give it exactly one way
out, to its provider:

* `EgressProxy` runs in the app. It listens on a unix socket that is bind-mounted into the
  sandbox and accepts HTTP CONNECT requests for an allowlist of provider hostnames only.
* `bridge` runs inside the sandbox. It listens on 127.0.0.1:<port> in the sandbox's own
  loopback and forwards every connection to that unix socket. The agent is started with
  HTTPS_PROXY pointing at it.

`python -m livery.desktop.netbridge --listen 18080 --socket /run/we/proxy.sock --ready FILE`
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import select
import socket
import threading
from pathlib import Path
from typing import Callable

BRIDGE_PORT = 18080

# Hostnames each provider's CLI needs: API, sign-in and token refresh.
ALLOWLISTS: dict[str, tuple[str, ...]] = {
    "claude": ("api.anthropic.com", "console.anthropic.com", "platform.claude.com", "claude.ai",
               "*.claude.ai", "statsig.anthropic.com", "mcp-proxy.anthropic.com"),
    "codex": ("api.openai.com", "chatgpt.com", "*.chatgpt.com", "auth.openai.com", "ab.chatgpt.com"),
    "fake": (),
}


def _pump(a: socket.socket, b: socket.socket) -> None:
    socks = [a, b]
    try:
        while True:
            readable, _, _ = select.select(socks, [], [], 300)
            if not readable:
                return
            for s in readable:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        return
    finally:
        for s in socks:
            try:
                s.close()
            except OSError:
                pass


def host_allowed(host: str, allowlist: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    if not host or host == "localhost" or host.replace(".", "").isdigit() or ":" in host:
        return False  # IP literals and loopback names never: only named provider hosts
    return any(fnmatch.fnmatchcase(host, pat) for pat in allowlist)


class EgressProxy:
    """CONNECT-only proxy on a unix socket, restricted to an allowlist of host:443."""

    def __init__(self, socket_path: Path, allowlist: tuple[str, ...],
                 log: Callable[[str, str, bool], None] | None = None,
                 connect: Callable[[str, int], socket.socket] | None = None):
        self.socket_path = Path(socket_path)
        self.allowlist = allowlist
        self.log = log or (lambda host, port, ok: None)
        self._connect = connect or (lambda h, p: socket.create_connection((h, p), timeout=20))
        self._server: socket.socket | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        srv.listen(32)
        self._server = srv
        threading.Thread(target=self._accept_loop, daemon=True, name="egress-proxy").start()

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        self.socket_path.unlink(missing_ok=True)

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(20)
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 8192:
                chunk = conn.recv(4096)
                if not chunk:
                    conn.close()
                    return
                head += chunk
            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            parts = line.split()
            if len(parts) != 3 or parts[0].upper() != "CONNECT":
                conn.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
                conn.close()
                return
            host, _, port_s = parts[1].rpartition(":")
            port = int(port_s) if port_s.isdigit() else 0
            ok = port == 443 and host_allowed(host, self.allowlist)
            self.log(host, port, ok)
            if not ok:
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                conn.close()
                return
            try:
                upstream = self._connect(host, port)
            except OSError:
                conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                conn.close()
                return
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            rest = head.split(b"\r\n\r\n", 1)[1]
            if rest:
                upstream.sendall(rest)
            conn.settimeout(None)
            upstream.settimeout(None)
            _pump(conn, upstream)
        except OSError:
            try:
                conn.close()
            except OSError:
                pass


def bridge(listen_port: int, socket_path: str, ready: str | None) -> None:
    """Inside the sandbox: 127.0.0.1:listen_port -> unix socket, one thread per connection."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", listen_port))
    srv.listen(64)
    if ready:
        Path(ready).write_text("ok")
    while True:
        client, _ = srv.accept()
        up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            up.connect(socket_path)
        except OSError:
            client.close()
            continue
        threading.Thread(target=_pump, args=(client, up), daemon=True).start()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="netbridge")
    ap.add_argument("--listen", type=int, default=BRIDGE_PORT)
    ap.add_argument("--socket", required=True)
    ap.add_argument("--ready")
    a = ap.parse_args(argv)
    bridge(a.listen, a.socket, a.ready)


if __name__ == "__main__":
    main()
