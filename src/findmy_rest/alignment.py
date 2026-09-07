"""Persisting accessory key alignment.

An accessory's keys rotate every 15 minutes, and FindMy.py finds a report by
deriving every key in the search window. Derivation ratchets forward from the
last *known* index, so an accessory whose alignment is stale by a year means
walking ~35,000 indices before a single request is sent. Measured on this data:

    aligned today      672 indices    0.9 s
    aligned Jan 2025    58,164 indices  ~66 s
    never aligned      131,827 indices  ~150 s

FindMy.py advances alignment in memory each time it decrypts a report
(`reports.py`, `accessory.update_alignment`), but nothing persists it. Without
this module every restart repeats the whole walk.

Only the alignment is stored -- an index and a timestamp per accessory -- never
key material. The keys stay in their read-only Secret mount; this file lives on
the state volume and is worthless on its own.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AlignmentStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        if not self._path.exists():
            return
        try:
            self._data = json.loads(self._path.read_text())
        except (OSError, ValueError):
            # Stale alignment costs startup time, never correctness, so a
            # corrupt file is worth a warning and nothing more.
            logger.warning("could not read alignment cache %s; starting fresh", self._path)
            self._data = {}

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        except OSError:
            logger.exception("could not write alignment cache %s", self._path)

    def apply(self, key: str, accessory) -> bool:
        """Fast-forward an accessory to its cached alignment, if that is ahead."""
        saved = self._data.get(key)
        if not saved:
            return False
        try:
            index = int(saved["index"])
            when = datetime.fromisoformat(saved["date"])
        except (KeyError, TypeError, ValueError):
            logger.warning("ignoring malformed alignment entry for %s", key)
            return False

        if index <= self._current_index(accessory):
            return False

        accessory.update_alignment(when, index)
        logger.info("fast-forwarded %s to index %d (observed %s)", key, index, when.date())
        return True

    def record(self, key: str, accessory) -> None:
        """Remember where an accessory got to, after a successful fetch."""
        state = accessory.to_json()
        index, date = state.get("alignment_index"), state.get("alignment_date")
        if not index or not date:
            return
        previous = self._data.get(key, {}).get("index", -1)
        if int(index) > int(previous):
            self._data[key] = {"index": int(index), "date": str(date)}

    @staticmethod
    def _current_index(accessory) -> int:
        try:
            return int(accessory.to_json().get("alignment_index") or 0)
        except (TypeError, ValueError):
            return 0
