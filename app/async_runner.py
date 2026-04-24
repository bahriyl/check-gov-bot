from __future__ import annotations

import asyncio
from concurrent.futures import Future
from threading import Event, Lock, Thread
from typing import Coroutine, TypeVar

T = TypeVar("T")


class AsyncLoopRunner:
    """Runs coroutines on a dedicated background event loop thread."""

    def __init__(self, thread_name: str) -> None:
        self._thread_name = thread_name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: Thread | None = None
        self._ready = Event()
        self._start_lock = Lock()

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        if pending:
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    def _ensure_started(self) -> asyncio.AbstractEventLoop:
        with self._start_lock:
            if self._thread and self._thread.is_alive() and self._loop:
                return self._loop
            self._ready.clear()
            self._thread = Thread(target=self._thread_main, name=self._thread_name, daemon=True)
            self._thread.start()
            self._ready.wait(timeout=5)
            if not self._loop:
                raise RuntimeError("failed to start async loop runner")
            return self._loop

    def run(self, coro: Coroutine[object, object, T]) -> T:
        loop = self._ensure_started()
        future: Future[T] = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()

    def close(self) -> None:
        loop = self._loop
        thread = self._thread
        if not loop or not thread:
            return
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        self._loop = None
        self._thread = None
