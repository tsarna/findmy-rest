"""Hand freed memory back to the OS after a key walk.

A walk for a silent accessory derives every key in the 7-day window and FindMy.py
holds them all until the walk ends -- tens of thousands of KeyPairs, each wrapping a
native OpenSSL key. That is freed afterwards, but glibc keeps freed memory in its
arenas rather than returning it, so RSS ratcheted up to each walk's high-water mark
and the pod hit its 512 Mi limit every 7-8 hours (measured 2026-10-22: ~110 MiB at
start, +37..+75 MiB per large walk). `malloc_trim(0)` releases the free pages in
every arena; `MALLOC_ARENA_MAX` in the deployment limits how many arenas there are.

glibc only. Elsewhere (macOS, musl) this is a no-op, which is fine: the problem it
solves is glibc's.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os

logger = logging.getLogger(__name__)

_libc = None
_unavailable = False


def _rss_bytes() -> int | None:
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def trim() -> int | None:
    """Run malloc_trim(0). Return the RSS it gave back in bytes, or None if unavailable."""
    global _libc, _unavailable
    if _unavailable:
        return None
    if _libc is None:
        try:
            _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
            _libc.malloc_trim.argtypes = [ctypes.c_size_t]
            _libc.malloc_trim.restype = ctypes.c_int
        except (OSError, AttributeError):
            # No glibc, or a libc without malloc_trim. Say so once, not per walk.
            logger.debug("malloc_trim unavailable; not trimming after walks")
            _unavailable = True
            return None

    before = _rss_bytes()
    _libc.malloc_trim(0)
    after = _rss_bytes()
    if before is None or after is None:
        return 0
    released = max(0, before - after)
    logger.info(
        "malloc_trim released %.1f MiB (RSS %.1f -> %.1f MiB)",
        released / 2**20,
        before / 2**20,
        after / 2**20,
    )
    return released
