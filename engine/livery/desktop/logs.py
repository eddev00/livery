"""Log files of the desktop engine: <app home>/logs/engine.log (rotated, 5 x 2 MB).

What is logged: API requests (method, path, status, duration), jobs, agent sessions and
their normalized events, sandbox and render problems. Not logged: request bodies, chat
text, document content or file names beyond the opaque IDs in paths.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .paths import app_home

log = logging.getLogger("livery")


def logs_dir() -> Path:
    return app_home() / "logs"


def setup(level: int = logging.INFO) -> Path:
    d = logs_dir()
    d.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file = RotatingFileHandler(d / "engine.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file.setFormatter(fmt)
    err = logging.StreamHandler(sys.stderr)
    err.setFormatter(fmt)
    err.setLevel(logging.WARNING)
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers[:] = [file, err]
    for noisy in ("uvicorn.access", "httpx", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return d / "engine.log"
