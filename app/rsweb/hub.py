"""In-process fan-out of live updates to connected browsers."""
from __future__ import annotations

import asyncio
from typing import Any


class Hub:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, kind: str, data: Any) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait((kind, data))
            except asyncio.QueueFull:
                pass  # slow tab; it will catch up on reload

    @property
    def listeners(self) -> int:
        return len(self._subs)
