"""Concurrency stress + performance-smoke tests for MemoryStore.

The store shares one SQLite connection across threads (`check_same_thread=False`)
and serialises every DB touch behind a single re-entrant lock, so the long-running
MCP server can safely field tool calls dispatched on worker threads. These tests
exercise that contract under contention — many threads reading and writing the same
store at once — and assert the vault stays internally consistent (no lost writes,
no crashes, counts add up).

The perf test is a *smoke* benchmark, not a micro-benchmark: it guards against a
gross regression (an accidental O(n^2), a dropped index) with a deliberately loose
ceiling so it never flakes on a slow/loaded CI box. It also prints timings for eyeballing.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from elesync.models import MemoryItem
from elesync.store import MemoryStore


def _item(content: str, source: str = "claude", type: str = "note") -> MemoryItem:
    return MemoryItem(content=content, source=source, type=type)


class ConcurrencyTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = MemoryStore(Path(self._tmp.name))
        self.addCleanup(self.store.close)

    def _run_threaded(self, worker, n_threads: int):
        """Start `n_threads` copies of `worker(tid)` at a barrier (to maximise
        overlap) and re-raise the first exception any of them hit."""
        barrier = threading.Barrier(n_threads)
        errors: list[BaseException] = []
        lock = threading.Lock()

        def runner(tid: int):
            try:
                barrier.wait()
                worker(tid)
            except BaseException as e:  # noqa: BLE001 - surface any thread failure
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=runner, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            raise errors[0]

    def test_concurrent_writers_lose_no_writes(self):
        """N threads each insert M unique memories; all N*M must land exactly once."""
        n_threads, per_thread = 8, 60

        def worker(tid: int):
            for i in range(per_thread):
                self.store.upsert(_item(f"t{tid}-note-{i}"))

        self._run_threaded(worker, n_threads)
        self.assertEqual(self.store.count(), n_threads * per_thread)
        # Every id is distinct and every expected content is present exactly once.
        contents = sorted(i.content for i in self.store.all())
        expected = sorted(f"t{tid}-note-{i}" for tid in range(n_threads) for i in range(per_thread))
        self.assertEqual(contents, expected)

    def test_concurrent_readers_and_writers_do_not_crash(self):
        """Readers (search/all/stats) running against a store being written must
        never see a half-open connection or raise — they just observe a consistent
        snapshot at whatever moment their locked call runs."""
        # Seed so readers have something from the start.
        for i in range(50):
            self.store.upsert(_item(f"seed alpha {i}", type="fact"))

        stop = threading.Event()

        def writer(tid: int):
            for i in range(80):
                self.store.upsert(_item(f"w{tid} alpha {i}", type="fact"))
            stop.set()

        def reader(tid: int):
            # Hammer reads until writers signal done, then a few more.
            while not stop.is_set():
                self.assertIsInstance(self.store.search("alpha", limit=10), list)
                self.store.all()
                self.store.stats()
            for _ in range(5):
                self.store.search("alpha", limit=10)

        def worker(tid: int):
            (writer if tid < 2 else reader)(tid)

        self._run_threaded(worker, 6)  # 2 writers, 4 readers
        # 50 seeded + 2 writers * 80.
        self.assertEqual(self.store.count(), 50 + 2 * 80)

    def test_concurrent_delete_and_search_stay_consistent(self):
        """Interleave deletes with searches: deletes either succeed or report the
        row was already gone, and searches never crash on a mid-delete index."""
        ids = [self.store.upsert(_item(f"beta target {i}", type="fact")) for i in range(120)]

        def worker(tid: int):
            if tid == 0:
                for mem_id in ids:
                    self.store.delete(mem_id)  # returns bool; must not raise
            else:
                for _ in range(200):
                    self.store.search("beta", limit=15)

        self._run_threaded(worker, 4)  # 1 deleter, 3 searchers
        self.assertEqual(self.store.count(), 0)

    def test_reentrant_lock_allows_nested_locked_calls(self):
        """sync() takes the lock and calls reindex() (which also takes it) — proof
        the lock is genuinely re-entrant and a nested locked call can't deadlock."""
        for i in range(20):
            self.store.upsert(_item(f"gamma {i}"))
        res = self.store.sync()  # would deadlock with a plain (non-reentrant) Lock
        self.assertEqual(res["total"], 20)


class PerfSmokeTestCase(unittest.TestCase):
    """Loose regression guards. Thresholds are generous on purpose — they catch a
    gross algorithmic regression, not a 20% slowdown, so they don't flake on CI."""

    N = 800  # corpus size — enough to expose a gross regression, small enough for CI
    SEARCHES = 150

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = MemoryStore(Path(self._tmp.name))
        self.addCleanup(self.store.close)

    def test_bulk_import_and_search_latency(self):
        items = [
            _item(f"memory number {i} about project helios and topic {i % 50}", type="fact")
            for i in range(self.N)
        ]
        t0 = time.perf_counter()
        self.store.import_many(items)
        import_s = time.perf_counter() - t0
        self.assertEqual(self.store.count(), self.N)

        t0 = time.perf_counter()
        for i in range(self.SEARCHES):
            hits = self.store.search(f"topic {i % 50}", limit=10)
            self.assertTrue(hits)  # FTS must actually find the seeded rows
        search_s = time.perf_counter() - t0
        per_search_ms = (search_s / self.SEARCHES) * 1000

        print(
            f"\n[perf] import {self.N} rows: {import_s:.2f}s | "
            f"{self.SEARCHES} searches: {search_s:.2f}s ({per_search_ms:.1f} ms/search)"
        )
        # Very loose ceilings: importing 2k rows or a single FTS search taking this
        # long means something is structurally wrong, not merely slow hardware.
        self.assertLess(import_s, 30.0, "bulk import unexpectedly slow — check indexing")
        self.assertLess(per_search_ms, 200.0, "search unexpectedly slow — check FTS/index")


if __name__ == "__main__":
    unittest.main()
