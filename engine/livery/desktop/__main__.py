"""Entry point of the desktop engine.

    python -m livery.desktop serve      the local API for the app (token on stdin)
    python -m livery.desktop netbridge  the in-sandbox network bridge (see netbridge.py)
    python -m livery.desktop fake-agent the deterministic test agent (LIVERY_ENABLE_FAKE_AGENT=1)

`serve` reads the access token from the first line of stdin, binds 127.0.0.1 on a free
port, prints {"port": N} as one JSON line on stdout and runs until stdin closes (the app
went away) or it is terminated. On exit it stops every agent session it started.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading


def serve() -> None:
    token = sys.stdin.readline().strip()
    if len(token) < 32:
        sys.stderr.write("serve: expected an access token of at least 32 characters on stdin\n")
        sys.exit(2)
    os.environ.setdefault("LIVERY_IGNORE_SYSTEM_FONTS", "1")
    import uvicorn

    from . import builder, conversation, jobs, logs, sandbox
    from .api import create_app
    from .store import Project

    log_file = logs.setup()
    logs.log.info("engine starting (pid %d), logs in %s", os.getpid(), log_file)

    # Without Bubblewrap every render fails with the setup message: closed, never open.
    st = sandbox.check()
    if st.ok:
        logs.log.info("sandbox: %s (%s)", st.message, st.version)
    else:
        logs.log.warning("sandbox unavailable: %s", st.message)
    builder.install_render_sandbox()
    for p in Project.all():
        try:
            for note in jobs.recover(p):
                logs.log.info("recover %s: %s", p.id, note)
        except Exception:  # noqa: BLE001
            logs.log.exception("recover %s failed", p.id)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(create_app(token), log_level="warning", access_log=False)
    server = uvicorn.Server(config)

    def shutdown(*_args: object) -> None:
        conversation.close_all()
        server.should_exit = True

    def watch_stdin() -> None:
        sys.stdin.read()  # returns at EOF: the app closed our pipe or died
        shutdown()

    threading.Thread(target=watch_stdin, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    logs.log.info("listening on 127.0.0.1:%d", port)
    print(json.dumps({"port": port}), flush=True)
    try:
        server.run(sockets=[sock])
    finally:
        conversation.close_all()
        logs.log.info("engine stopped")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "netbridge":
        from .netbridge import main as bridge_main
        bridge_main(sys.argv[2:])
    elif cmd == "fake-agent" and os.environ.get("LIVERY_ENABLE_FAKE_AGENT") == "1":
        from .fake_agent import main as fake_main
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        fake_main()
    else:
        sys.stderr.write(f"unknown command {cmd!r}; use serve or netbridge\n")
        sys.exit(2)


if __name__ == "__main__":
    main()
