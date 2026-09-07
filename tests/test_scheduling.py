"""Cost-aware polling.

An accessory that reports stays aligned and costs a few hundred key derivations.
One that has been silent widens by ~96 indices a day forever and cannot be
narrowed, because the index advances with the accessory's powered time. Polling
those every cycle spends minutes of CPU looking for something that is not
answering, so they are polled in inverse proportion to what they cost.
"""

from __future__ import annotations

from pathlib import Path

from findmy_rest.backends.accessories import AccessoryBackend
from findmy_rest.config import Settings


class FakeAccessory:
    """Only what the scheduler touches: an index range and an identity."""

    def __init__(self, name: str, cost: int) -> None:
        self.name = name
        self.identifier = name
        self._cost = cost

    def get_min_index(self, dt):
        return 0

    def get_max_index(self, dt):
        return self._cost - 1


def _backend(tmp_path: Path, *accessories: FakeAccessory, budget: int = 5000):
    settings = Settings(state_dir=tmp_path, keys_dir=tmp_path / "keys", poll_budget_indices=budget)
    backend = AccessoryBackend(settings, object())
    backend._accessories = list(accessories)
    backend._stems = {id(a): a.name for a in accessories}
    return backend


def test_cheap_accessories_are_polled_every_cycle(tmp_path):
    keys = FakeAccessory("keys", 672)
    backend = _backend(tmp_path, keys)
    for _ in range(5):
        selected, deferred = backend._select()
        assert selected == [keys]
        assert deferred == []


def test_expensive_accessory_is_polled_in_proportion_to_its_cost(tmp_path):
    """12x the budget means roughly every 12th cycle, not never."""
    watch = FakeAccessory("watch", 60_000)
    backend = _backend(tmp_path, watch, budget=5000)

    polled = [bool(backend._select()[0]) for _ in range(24)]
    assert polled.count(True) == 2
    # Evenly spaced, not bunched: the first hit is at cycle 12.
    assert polled.index(True) == 11


def test_budget_is_global_so_cost_does_not_scale_with_accessory_count(tmp_path):
    """Two silent accessories must not cost twice as much per cycle."""
    a = FakeAccessory("a", 50_000)
    b = FakeAccessory("b", 50_000)
    backend = _backend(tmp_path, a, b, budget=5000)

    hits = sum(len(backend._select()[0]) for _ in range(20))
    # Each accrues half the budget, so each is due every 20th cycle.
    assert hits == 2


def test_cheap_and_expensive_mix(tmp_path):
    keys = FakeAccessory("keys", 672)
    watch = FakeAccessory("watch", 60_000)
    backend = _backend(tmp_path, keys, watch, budget=5000)

    selected, deferred = backend._select()
    assert selected == [keys]
    assert "watch" in deferred[0]

    # The cheap one is never held up by the expensive one.
    for _ in range(10):
        assert keys in backend._select()[0]


def test_zero_budget_disables_deferral(tmp_path):
    watch = FakeAccessory("watch", 500_000)
    backend = _backend(tmp_path, watch, budget=0)
    selected, deferred = backend._select()
    assert selected == [watch]
    assert deferred == []


def test_an_accessory_that_becomes_cheap_is_polled_every_cycle_again(tmp_path):
    """Reporting resets alignment, so cost collapses -- and polling must recover
    immediately rather than staying on the slow schedule."""
    tag = FakeAccessory("tag", 60_000)
    backend = _backend(tmp_path, tag, budget=5000)
    backend._select()
    assert backend._credits["tag"] > 0

    tag._cost = 672  # a report landed; alignment jumped to now
    selected, deferred = backend._select()
    assert selected == [tag]
    assert deferred == []
    assert "tag" not in backend._credits
