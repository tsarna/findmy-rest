"""Tests for the alignment cache.

Alignment is a performance cache, never a source of truth: a missing, stale or
corrupt entry must cost startup time and nothing else.
"""

from __future__ import annotations

from datetime import datetime, timezone

from findmy_rest.alignment import AlignmentStore


class FakeAccessory:
    """Stands in for FindMyAccessory: the two methods the store touches."""

    def __init__(self, index=0, date=None):
        self.index = index
        self.date = date

    def to_json(self):
        return {"alignment_index": self.index, "alignment_date": self.date}

    def update_alignment(self, dt, index):
        self.date, self.index = dt.isoformat(), index


def test_roundtrip_fast_forwards_a_stale_accessory(tmp_path):
    when = datetime.now(tz=timezone.utc)
    store = AlignmentStore(tmp_path / "a.json")
    store.record("keys", FakeAccessory(index=59983, date=when.isoformat()))
    store.save()

    stale = FakeAccessory(index=2120, date="2025-01-09T00:00:00+00:00")
    assert AlignmentStore(tmp_path / "a.json").apply("keys", stale) is True
    assert stale.index == 59983


def test_never_moves_an_accessory_backwards(tmp_path):
    """A stale cache entry must not undo progress made since."""
    store = AlignmentStore(tmp_path / "a.json")
    store.record("keys", FakeAccessory(index=100, date="2026-01-01T00:00:00+00:00"))
    store.save()

    ahead = FakeAccessory(index=5000, date="2026-09-01T00:00:00+00:00")
    assert AlignmentStore(tmp_path / "a.json").apply("keys", ahead) is False
    assert ahead.index == 5000


def test_record_keeps_the_highest_index(tmp_path):
    store = AlignmentStore(tmp_path / "a.json")
    store.record("keys", FakeAccessory(index=500, date="2026-09-01T00:00:00+00:00"))
    store.record("keys", FakeAccessory(index=100, date="2026-08-01T00:00:00+00:00"))
    store.save()

    target = FakeAccessory(index=0)
    AlignmentStore(tmp_path / "a.json").apply("keys", target)
    assert target.index == 500


def test_unknown_accessory_is_left_alone(tmp_path):
    fresh = FakeAccessory(index=7)
    assert AlignmentStore(tmp_path / "a.json").apply("unseen", fresh) is False
    assert fresh.index == 7


def test_corrupt_cache_does_not_raise(tmp_path):
    path = tmp_path / "a.json"
    path.write_text("{not json")
    assert AlignmentStore(path).apply("keys", FakeAccessory()) is False


def test_malformed_entry_is_ignored(tmp_path):
    path = tmp_path / "a.json"
    path.write_text('{"keys": {"index": "banana", "date": "nope"}}')
    accessory = FakeAccessory(index=3)
    assert AlignmentStore(path).apply("keys", accessory) is False
    assert accessory.index == 3


def test_cache_never_contains_key_material(tmp_path):
    """It lives on the state volume; keys stay in the read-only Secret."""
    path = tmp_path / "a.json"
    store = AlignmentStore(path)
    store.record("keys", FakeAccessory(index=42, date="2026-09-01T00:00:00+00:00"))
    store.save()
    text = path.read_text()
    assert "42" in text
    for forbidden in ("master_key", "skn", "sks", "private"):
        assert forbidden not in text
