"""Async stdout writer for pygls.

Replaces ``pygls.io_.StdoutWriter``, whose ``write`` is synchronous and
blocks the asyncio event loop until the OS pipe drains. On a 5 MB spine
response that can stall every other LSP request for seconds while the
client reads the pipe in 64 KB chunks. Hands the blocking
``write`` + ``flush`` off to a worker thread; pygls schedules the
returned coroutine via ``asyncio.ensure_future``. A lock serialises
writes so concurrent responses don't interleave bytes on the single
pipe.
"""

from __future__ import annotations

import asyncio
import logging
from typing import BinaryIO

logger = logging.getLogger(__name__)


class AsyncStdoutWriter:
    """Drop-in replacement for ``pygls.io_.StdoutWriter`` that returns
    an awaitable from ``write`` so pygls runs it on the loop instead of
    blocking inside a sync write.
    """

    def __init__(self, stdout: BinaryIO) -> None:
        self._stdout = stdout
        # Created lazily on first use — at __init__ time the asyncio loop
        # may not yet exist (pygls constructs the writer before
        # asyncio.run).
        self._lock: asyncio.Lock | None = None

    def close(self) -> None:
        self._stdout.close()

    def write(self, data: bytes):
        return self._async_write(data)

    async def _async_write(self, data: bytes) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            await asyncio.to_thread(self._sync_write, data)

    def _sync_write(self, data: bytes) -> None:
        self._stdout.write(data)
        self._stdout.flush()


def install() -> None:
    """Replace pygls's StdoutWriter so ``server.start_io`` constructs
    our async version. Must be called before ``start_io``.
    """
    import pygls.io_ as _io
    import pygls.server as _ps

    _ps.StdoutWriter = AsyncStdoutWriter  # type: ignore[assignment]
    _io.StdoutWriter = AsyncStdoutWriter  # type: ignore[assignment]
