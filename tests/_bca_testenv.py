"""Conditional skips for POSIX-only test mechanics (never used to hide plugin defects).

``fork`` and ``SIGKILL`` do not exist on Windows. The tests that need them keep
running on Linux/macOS (CI and the author's gate). On Windows the same guarantees
are exercised by ``tests/test_concurrency_spawn.py`` (``spawn`` workers, real
contention) and by the cross-platform store tests, so a skip here is a visible,
reasoned skip, not a silent pass.
"""
import multiprocessing as _mp
import signal as _signal
import unittest as _unittest

HAS_FORK = "fork" in _mp.get_all_start_methods()
HAS_SIGKILL = hasattr(_signal, "SIGKILL")

requires_fork = _unittest.skipUnless(
    HAS_FORK, "needs multiprocessing 'fork' (POSIX only); Windows is covered by tests/test_concurrency_spawn.py")
requires_sigkill = _unittest.skipUnless(
    HAS_SIGKILL and HAS_FORK, "needs fork + signal.SIGKILL (POSIX only); kill -9 recovery must be run on Linux/WSL")
