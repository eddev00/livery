"""In-process event bus: build progress, job states and agent activity, per project.

Subscribers get a queue; the API streams it to the app as server-sent events. Events are
small dicts with a "type"; nothing in them is ever sent to an agent.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any

_subs: dict[str, list[queue.Queue]] = {}
_lock = threading.Lock()


def subscribe(project_id: str) -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=2000)
    with _lock:
        _subs.setdefault(project_id, []).append(q)
    return q


def unsubscribe(project_id: str, q: queue.Queue) -> None:
    with _lock:
        if q in _subs.get(project_id, []):
            _subs[project_id].remove(q)


def publish(project_id: str, event: dict[str, Any]) -> None:
    event = {**event, "ts": time.time(), "project": project_id}
    with _lock:
        targets = list(_subs.get(project_id, [])) + list(_subs.get("*", []))
    for q in targets:
        try:
            q.put_nowait(event)
        except queue.Full:
            pass  # a stalled subscriber loses events rather than blocking the engine
