"""Background file watcher — polls state files, pushes SSE on changes."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Set

from . import data

logger = logging.getLogger("ark.dashboard")

# Global set of SSE client queues
_clients: Set[asyncio.Queue] = set()
# Last known mtimes per project
_last_mtimes: Dict[str, Dict[str, float]] = {}


async def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _clients.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _clients.discard(q)


async def _broadcast(event: str, payload: dict) -> None:
    msg = f"event: {event}\ndata: {json.dumps(payload)}\n\n"
    dead = []
    for q in _clients:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _clients.discard(q)


async def poll_loop(interval: float = 2.0) -> None:
    """Main polling loop — runs until cancelled."""
    heartbeat_counter = 0
    while True:
        try:
            await _check_changes()
        except Exception:
            logger.exception("Watcher poll error")

        heartbeat_counter += 1
        if heartbeat_counter >= 15:  # 30s at 2s interval
            await _broadcast("heartbeat", {"ts": asyncio.get_event_loop().time()})
            heartbeat_counter = 0

        await asyncio.sleep(interval)


async def _check_changes() -> None:
    projects = data.list_project_names()
    for name in projects:
        try:
            current = data.get_file_mtimes(name)
        except Exception:
            continue

        prev = _last_mtimes.get(name, {})
        if current != prev:
            _last_mtimes[name] = current
            if prev:  # Skip first scan (initial load)
                try:
                    summary = data.read_project_summary(name)
                    await _broadcast("project_update", {
                        "project": name,
                        "summary": summary.model_dump(),
                    })
                except Exception:
                    logger.exception(f"Error broadcasting update for {name}")

    # Detect removed projects
    known = set(_last_mtimes.keys())
    current_set = set(projects)
    for removed in known - current_set:
        del _last_mtimes[removed]
        await _broadcast("project_removed", {"project": removed})
